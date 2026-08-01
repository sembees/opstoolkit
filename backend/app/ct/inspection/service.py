"""巡检服务：netmiko 连接、命令执行、并发、解析与进度回调。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Callable, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import crud, models
from app.ct.drivers import get_driver, infer_netmiko_device_type
from app.ct.drivers.base import MetricCommand
from app.ct.inspection.parser import parse_output
from app.core.timeutil import utcnow
from app.database import async_session

ProgressCb = Callable[[dict], None]

_BACKGROUND_TASKS: set[asyncio.Task] = set()


def schedule_background(coro) -> asyncio.Task:
    """调度后台任务并保持引用，避免被 asyncio GC。"""
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return task


async def recover_interrupted_tasks() -> int:
    """服务重启后把遗留的 running 任务标记为 failed，避免永远卡住。"""
    from sqlalchemy import update

    async with async_session() as db:
        res = await db.execute(
            update(models.InspectionTask)
            .where(models.InspectionTask.status == "running")
            .values(status="failed", finished_at=utcnow())
        )
        await db.commit()
        return int(res.rowcount or 0)


async def run_task_in_background(task_id: str) -> None:
    """后台执行巡检任务并写入结果；异常时标记 failed。"""
    try:
        async with async_session() as db:
            task = await db.get(models.InspectionTask, task_id)
            if task is None:
                return
            task.status = "running"
            task.finished_at = None
            # 收集实时输出，用于任务回放
            output_log = []
            def capture_log(ev):
                # 只保存关键事件，不存大段输出
                entry = dict(ev)
                if entry.get("type") == "output":
                    entry["output"] = (entry.get("output") or "")[:500]
                output_log.append(entry)

            await db.commit()
            results = await inspect_many(
                db, task.asset_ids, task.kind, task.template, task.commands,
                on_event=capture_log
            )
            task.output_log = output_log
            await db.commit()
            # 告警检测：遍历规则检查指标超标
            alert_rules = (await db.execute(
                select(models.AlertRule).where(models.AlertRule.enabled == True)
            )).scalars().all()

            for r in results:
                # 告警检查
                if r.get("status") == "success":
                    metrics = r.get("metrics", {})
                    for rule in alert_rules:
                        mv = metrics.get(rule.metric_key, {})
                        val = None
                        if isinstance(mv.get("value"), (int, float)):
                            val = mv["value"]
                        elif isinstance(mv, dict):
                            # 尝试从 parsed 中提取数值
                            parsed = mv.get("value") or mv
                            if isinstance(parsed, dict):
                                nums = [v for v in parsed.values() if isinstance(v, (int, float))]
                                if nums:
                                    val = nums[0]  # 取第一个数值
                        if val is not None:
                            triggered = False
                            if rule.operator == "gt" and val > rule.threshold:
                                triggered = True
                            elif rule.operator == "lt" and val < rule.threshold:
                                triggered = True
                            elif rule.operator == "gte" and val >= rule.threshold:
                                triggered = True
                            elif rule.operator == "lte" and val <= rule.threshold:
                                triggered = True
                            if triggered:
                                r["error"] = (r.get("error", "") + f"[告警] {rule.name}: {rule.metric_key}={val} {rule.operator} {rule.threshold}").strip()
                                r["status"] = "failed"
                db.add(models.InspectionResult(
                    task_id=task.id,
                    asset_id=r["asset_id"],
                    asset_name=r["asset_name"],
                    status=r["status"],
                    error=r.get("error", ""),
                    metrics=r.get("metrics", {}),
                    raw=r.get("raw", []),
                ))
            task.status = "done"
            task.finished_at = utcnow()
            await db.commit()
    except Exception:  # noqa: BLE001
        try:
            async with async_session() as db:
                task = await db.get(models.InspectionTask, task_id)
                if task is not None:
                    task.status = "failed"
                    task.finished_at = utcnow()
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass




def get_pager_cmds(device_type: str) -> list:
    """返回各厂商禁用分页的命令，避免输出被截断。"""
    v = (device_type or "").lower()
    if "comware" in v or "hp" in v:
        return ["screen-length disable"]
    if "huawei" in v:
        return ["screen-length 0 temporary"]
    return ["terminal length 0"]


def _build_connect_params(asset: models.Asset, cred_plain: dict) -> dict:
    """构建 netmiko ConnectHandler 参数。

    设备类型优先使用资产显式配置，否则根据厂商+角色自动推断。
    超时、端口、密码、特权密码均可透传。
    """
    device_type = asset.device_type or infer_netmiko_device_type(asset.vendor, asset.device_role)
    params = {
        "device_type": device_type or "autodetect",
        "host": asset.host,
        "port": asset.port or cred_plain.get("port") or 22,
        "username": cred_plain.get("username", ""),
        "timeout": settings.inspection_timeout,
        "conn_timeout": settings.inspection_timeout,
    }
    if cred_plain.get("password"):
        params["password"] = cred_plain["password"]
    if cred_plain.get("enable_secret"):
        params["secret"] = cred_plain["enable_secret"]
    return params


def _connect_and_run(params, key_text, metric_cmds, custom_cmds, disable_pager, on_line):
    """连接设备，执行巡检命令，收集输出。

    流程：创建 SSH 连接 → 进入 enable 模式（若配了 secret）
    → 禁用分页 → 逐条执行指标命令 → 执行自定义命令
    → 收集所有输出并通过 on_line 回调通知上层。
    如果有 SSH 密钥，会写入临时文件并在 finally 中清理。
    """
    from netmiko import ConnectHandler
    key_file = None
    if key_text:
        fd, key_file = tempfile.mkstemp(suffix=".key")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(key_text)
        os.chmod(key_file, 0o600)
        params["use_keys"] = True
        params["key_files"] = [key_file]
        params.setdefault("allow_agent", False)
        params.setdefault("use_keys", True)
    outputs = []
    try:
        with ConnectHandler(**params) as conn:
            try:
                if params.get("secret"):
                    conn.enable()
            except Exception:
                pass
            if disable_pager and settings.enable_pager_disable:
                for c in get_pager_cmds(params.get("device_type")):
                    try:
                        conn.send_command(c, read_timeout=8, cmd_verify=False)
                    except Exception:
                        pass
            for mc in metric_cmds:
                on_line({"type": "cmd", "cmd": mc.command, "label": mc.label})
                out = conn.send_command(mc.command, read_timeout=settings.inspection_timeout)
                outputs.append({"cmd": mc.command, "label": mc.label, "key": mc.key, "unit": mc.unit, "textfsm": mc.textfsm, "output": out})
                on_line({"type": "output", "cmd": mc.command, "output": out})
            for c in custom_cmds:
                on_line({"type": "cmd", "cmd": c, "label": c})
                out = conn.send_command(c, read_timeout=settings.inspection_timeout)
                outputs.append({"cmd": c, "label": c, "key": "", "unit": "", "textfsm": "", "output": out})
                on_line({"type": "output", "cmd": c, "output": out})
    finally:
        if key_file and os.path.exists(key_file):
            os.remove(key_file)
    return outputs


async def _resolve_template_cmds(db, asset, template_name):
    """解析巡检命令列表。

    查找策略：DB 中查找指定模板 → 若未指定则取“standard”
    → 若 DB 无结果，回落到内置驱动的默认命令集。
    """
    from sqlalchemy import func, select
    from app.core.models import InspectionTemplate

    vendor = (asset.vendor or "generic").strip().lower() or "generic"
    stmt = select(InspectionTemplate).where(func.lower(InspectionTemplate.vendor) == vendor)
    if template_name:
        stmt = stmt.where(InspectionTemplate.name == template_name)
    else:
        # 无指定模板时优先取该厂商的标准模板
        stmt = stmt.where(InspectionTemplate.name == "standard")
    stmt = stmt.limit(1)
    row = (await db.execute(stmt)).scalar_one_or_none()

    if row is not None and row.items:
        return [MetricCommand(
            it["key"], it["label"], it["command"],
            it.get("textfsm", ""), it.get("unit", ""),
        ) for it in row.items]

    # 回落：驱动内置
    driver = get_driver(vendor)
    templates = driver.templates()
    return templates.get(driver.default_template, [])


# 单设备巡检主流程：加载凭据 → 构建参数 → 获取命令 → 异步连接执行 → 解析输出
async def inspect_one(db: AsyncSession, asset: models.Asset, kind: str = "default",
                      template: Optional[str] = None, commands: Optional[list] = None,
                      on_event: Optional[ProgressCb] = None) -> dict:
    emit = on_event or (lambda d: None)
    asset_name = asset.name or asset.host
    cred = await crud.get_credential_for_asset(db, asset)
    cred_plain = await crud.decrypt_credential(cred) if cred else {"username": "", "password": ""}
    key_text = cred_plain.get("ssh_key", "") if cred_plain else ""
    params = _build_connect_params(asset, cred_plain)
    metric_cmds = await _resolve_template_cmds(db, asset, template)
    custom_cmds = list(commands) if (kind == "custom" and commands) else []
    emit({"type": "start", "asset_id": asset.id, "asset_name": asset_name})

    def on_line(ev):
        ev.setdefault("asset_id", asset.id)
        ev.setdefault("asset_name", asset_name)
        emit(ev)

    try:
        outputs = await asyncio.to_thread(_connect_and_run, params, key_text,
                                          metric_cmds, custom_cmds,
                                          settings.enable_pager_disable, on_line)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
        emit({"type": "error", "asset_id": asset.id, "asset_name": asset_name, "error": err})
        return {"asset_id": asset.id, "asset_name": asset_name, "status": "failed", "error": err, "metrics": {}, "raw": []}

    metrics = {}
    raw = []
    for item in outputs:
        pr = parse_output(item["key"], item["output"], item.get("textfsm", ""), device_type=params.get("device_type", ""), command=item.get("command", ""))
        entry = {"cmd": item["cmd"], "label": item["label"], "key": item["key"],
                 "output": item["output"], "parsed": pr["parsed"],
                 "summary": pr["summary"], "status": pr["status"]}
        raw.append(entry)
        if item["key"]:
            metrics[item["key"]] = {"label": item["label"], "unit": item["unit"],
                                    "summary": pr["summary"], "status": pr["status"],
                                    "value": pr["parsed"]}
    emit({"type": "done", "asset_id": asset.id, "asset_name": asset_name})
    return {"asset_id": asset.id, "asset_name": asset_name, "status": "success",
            "error": "", "metrics": metrics, "raw": raw}


# 多设备并发巡检，用 Semaphore 限制并发数。任务失败不影响其他任务。
async def inspect_many(db: AsyncSession, asset_ids: list, kind: str = "default",
                       template: Optional[str] = None, commands: Optional[list] = None,
                       on_event: Optional[ProgressCb] = None) -> list:
    # 通过 Semaphore 限制同时连接的设备数，避免压跨网络
    sem = asyncio.Semaphore(max(1, settings.inspection_concurrency))
    results = []

    async def _run(aid):
        asset = await crud.get_asset(db, aid)
        if not asset:
            return {"asset_id": aid, "asset_name": "", "status": "failed",
                    "error": "资产不存在", "metrics": {}, "raw": []}
        async with sem:
            return await inspect_one(db, asset, kind, template, commands, on_event)

    tasks = [asyncio.create_task(_run(aid)) for aid in asset_ids]
    for t in asyncio.as_completed(tasks):
        results.append(await t)
    return results
