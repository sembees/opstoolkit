"""巡检服务：netmiko 连接、命令执行、并发、解析与进度回调。"""
from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace as _dc_replace
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core import crud, models
from app.core.notify.alerts import notify_alert
from app.ct.drivers import get_driver, infer_netmiko_device_type
from app.ct.drivers.base import MetricCommand
from app.ct.inspection.parser import is_command_rejected, parse_output
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


async def run_task_in_background(task_id: str, on_event=None) -> list:
    """后台执行巡检任务并写入结果；异常时标记 failed。

    `on_event`：**同步**回调（服务层就是同步 `emit` 的），用于把实时事件转发给调用方，
    例如 WebSocket 直播。★ **绝不能直接传 async 函数** —— WS 那条链路以前就是这么传的，
    结果每次 `emit` 都产生一个没人 await 的协程：**一个事件都没发出去**，前端只看到自己写的那行
    "连接已建立"，看着就像"界面卡死"。正确做法见 `api/inspection.py::inspection_ws`：
    同步回调 → `asyncio.Queue`（跨线程 `call_soon_threadsafe`）→ 单独的 drain 协程负责发送。

    返回：每台设备的结果列表（WS 需要它来发 complete 消息）。
    """
    results: list = []
    try:
        async with async_session() as db:
            task = await db.get(models.InspectionTask, task_id)
            if task is None:
                return results
            task.status = "running"
            task.finished_at = None
            # 收集实时输出，用于任务回放
            output_log = []
            def capture_log(ev):
                # ★ 直播先发**原样**事件（不截断）；只对"存进 DB 的那份"截断，
                #   否则单条长输出（如整份 running-config）会把 output_log 撑爆。
                if on_event is not None:
                    on_event(ev)
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

            # 告警外发准备（通知集成单元）：命中消息要带"设备 IP"，这里一次性把资产
            # host 查出来。★ 整段被 settings.notify_enabled 短路 —— 未启用通知时这条
            # 路径零开销（连库都不查），下面告警判定逻辑本身一行没动。
            asset_hosts: dict[str, str] = {}
            if alert_rules and settings.notify_enabled and task.asset_ids:
                try:
                    asset_hosts = {
                        a.id: (a.host or "")
                        for a in (await db.execute(
                            select(models.Asset).where(models.Asset.id.in_(task.asset_ids))
                        )).scalars().all()
                    }
                except Exception:  # noqa: BLE001 —— host 拿不到就用空串，通知是旁路
                    asset_hosts = {}

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
                                # ★ 告警外发（旁路）：发射后不管 —— create_task 不 await，
                                #   通知成不成都不得阻塞/拖垮巡检；notify_enabled=False 时
                                #   这行都不进（零开销）。实现在 app/core/notify/alerts.py。
                                if settings.notify_enabled:
                                    schedule_background(notify_alert(
                                        rule=rule, metric_key=rule.metric_key, value=val,
                                        asset_id=r["asset_id"], asset_name=r["asset_name"],
                                        asset_host=asset_hosts.get(r["asset_id"], ""),
                                        severity="critical",
                                    ))
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
        # ★ 这里**必须留下原因**。原来静默吞掉，导致"任务被标 failed、却一行结果都没落库"
        #   这种现象完全查不出所以然（实测踩到：status=failed、InspectionResult 0 行、
        #   而 output_log 里只有 start/error）。留 traceback 才能定位。
        import logging
        import traceback

        logging.getLogger(__name__).exception("巡检任务 %s 执行失败", task_id)
        traceback.print_exc()
        try:
            async with async_session() as db:
                task = await db.get(models.InspectionTask, task_id)
                if task is not None:
                    task.status = "failed"
                    task.finished_at = utcnow()
                    await db.commit()
        except Exception:  # noqa: BLE001
            pass
    return results




def get_pager_cmds(device_type: str) -> list:
    """返回各厂商禁用分页的命令，避免输出被截断。"""
    v = (device_type or "").lower()
    if "comware" in v or "hp" in v:
        return ["screen-length disable"]
    if "huawei" in v:
        return ["screen-length 0 temporary"]
    return ["terminal length 0"]


# ── 老设备 SSH 支持：自检 + 失败时的可执行提示 ────────────────────────────────
# 来由（2026-10-04 真机事故）：paramiko 是 netmiko 的**传递依赖**、以前没钉版本，
# 于是飘到了 5.0.0 —— 而 5.0.0 **彻底删除**了 SHA-1 那套实现（`KexGroup14`/`kex_group1`/`DSSKey` 都没了、
# `RSAKey.HASHES` 里也去掉了 `ssh-rsa`）。后果：**只支持 ssh-rsa 的老设备（大量在用机型）全部连不上**，
# 报的是英文 `Incompatible ssh peer (no acceptable host key / kex algorithm)`，运维极易误判成"账号密码错"。
# 处置：requirements.txt 里**钉住 paramiko==3.5.1**（仍带 ssh-rsa 与 group14-sha1，且含 Terrapin 修复）。
# 本模块负责两件事：
#   1) `legacy_ssh_support()`：自检运行环境是否仍带老算法（启动告警 + /health 可见），
#      这样**将来镜像重建把 paramiko 又飘上去时不会悄悄失效**；
#   2) 连接真失败时，把英文底层报错翻译成"能照着做"的中文提示（原始错误保留在末尾）。
_LEGACY_SSH_SIGNS = (
    "no acceptable host key",
    "no acceptable kex algorithm",
    "no acceptable cipher",
    "no acceptable mac",
    "ssh-rsa",
    "no matching host key",
    "no matching key exchange",
)

_LEGACY_SSH_HINT = (
    "SSH 握手失败：设备与巡检服务的算法对不上 —— **不是账号密码问题**（换密码没用）。\n"
    "  本应用会主动尝试老式算法（ssh-rsa、diffie-hellman-group14-sha1/group1-sha1、AES-CBC、hmac-sha1），"
    "正常的老设备应当能连上；出现本条说明设备用的是**更老或非标准**的算法（例如只支持 ssh-dss、SSH v1，"
    "或厂商私有算法），或者服务的 SSH 库被升级过、丢掉了老算法。排查顺序：\n"
    "  ① 看服务是否仍带老算法：`GET /api/health` 的 `legacy_ssh` 字段（或启动日志）——"
    "若显示不支持，把 paramiko 钉回 3.5.1（见 backend/requirements.txt 注释）；\n"
    "  ② 若设备确实只支持更老的算法，可在**设备侧**生成较新主机密钥（推荐，不动服务）：\n"
    "      华三 Comware： public-key local create ecdsa secp256r1   ← 本项已真机验证\n"
    "      华为 VRP    ： ecc local-key-pair create（或 rsa local-key-pair create）\n"
    "      思科 IOS    ： crypto key generate rsa modulus 2048\n"
    "  ③ 若必须支持 ssh-dss / SSH v1 这类已淘汰算法，需单独评估（安全基线问题，不建议）。"
)


def legacy_ssh_support() -> tuple:
    """自检：当前 SSH 库是否仍带老设备需要的算法。返回 `(是否支持, 说明)`。

    为什么必须有这个自检：paramiko 一旦被升到 4/5，**所有只支持 ssh-rsa 的老设备会集体连不上**，
    而故障现象是英文报错、极易被误判成"密码错"。把它暴露到 `/health` 与启动日志，运维一眼能看出根因。
    """
    try:
        from paramiko.transport import Transport

        keys = set(Transport._preferred_keys) | set(getattr(Transport, "_key_info", {}))
        kex = set(Transport._preferred_kex)
        has_key = "ssh-rsa" in keys
        has_kex = bool(kex & {"diffie-hellman-group14-sha1", "diffie-hellman-group1-sha1",
                              "diffie-hellman-group-exchange-sha1"})
        if has_key and has_kex:
            return True, "SSH 库支持老式算法（ssh-rsa + SHA-1 kex），可巡检老设备"
        missing = []
        if not has_key:
            missing.append("ssh-rsa 主机密钥")
        if not has_kex:
            missing.append("SHA-1 密钥交换")
        return False, ("当前 SSH 库缺少：%s —— 只支持老式算法的设备将无法巡检；"
                       "请把 paramiko 钉回 3.5.1（见 backend/requirements.txt）" % "、".join(missing))
    except Exception as e:  # noqa: BLE001
        return False, "SSH 库能力自检失败：%r" % (e,)


def _friendly_ssh_error(exc: Exception) -> str:
    """把连接类异常翻译成人能照着做的提示；**原始错误始终保留**在末尾（不掩盖真相）。"""
    raw = "%s: %s" % (type(exc).__name__, exc)
    low = raw.lower()
    if any(s in low for s in _LEGACY_SSH_SIGNS):
        return "%s\n  （原始错误：%s）" % (_LEGACY_SSH_HINT, raw[:300])
    return raw


def _preflight(host: str, port, timeout: float) -> None:
    """连接设备前先做一次 TCP 预检；不通就抛一句**能看懂**的错。

    为什么必须做（实测教训）：像 `192.168.1.2` 这种"不在任何本地网段、被丢给默认网关"的地址，
    TCP SYN 会被静默丢弃，而**连接阶段不受 netmiko 的 conn_timeout 管** —— 由操作系统的
    SYN 重试决定（Linux 默认 ~127 秒）。用户点了「开始巡检」后界面就一直卡着，
    既没有进度也没有报错。预检把这个等待压到数秒，并明确指出"不可达"而不是抛一个晦涩的异常。
    """
    import socket
    try:
        with socket.create_connection((host, int(port or 22)), timeout=timeout):
            return
    except OSError as e:
        raise RuntimeError(
            "设备不可达：%s:%s 在 %.0f 秒内建不起 TCP 连接（%s）。"
            "请确认地址与端口正确、设备在线，且本机到该地址有路由。"
            % (host, port or 22, timeout, e.__class__.__name__)
        ) from e


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
        # ★ 连接超时与读取超时**分开**：
        #   · timeout      = 通道读取超时 → 60s（长命令如 display current-configuration 需要）
        #   · conn_timeout = TCP/SSH 握手超时 → 10s（快速失败）
        #   以前两者都是 60s，配上"TCP 阶段没人管"，不可达设备会让人以为界面卡死。
        "timeout": settings.inspection_timeout,
        "conn_timeout": settings.inspection_connect_timeout,
    }
    if cred_plain.get("password"):
        params["password"] = cred_plain["password"]
    if cred_plain.get("enable_secret"):
        params["secret"] = cred_plain["enable_secret"]
    return params


def _run_metric_command(conn, mc: MetricCommand, on_line, timeout: float) -> tuple:
    """执行单个指标命令；主命令被设备拒绝时**自动依次尝试候选命令**（alt_commands）。

    "被拒绝"判定用 parser.is_command_rejected：整段输出很短（≤6 行）且命中
    "Unrecognized command / Too many parameters / % Invalid input …" 特征串 ——
    即设备 CLI 明确报了"不认识这条命令"，而不是命令执行成功但内容异常。

    返回 (output, tried, rejected, used_cmd)：
      · tried    —— 实际依次执行过的命令（含主命令与已尝试的候选）；
      · rejected —— 全部候选都被拒绝（True ⇒ 上层标 unsupported）；
      · used_cmd —— 最后实际执行的命令（回显属于它）。
    """
    candidates = [mc.command] + [c for c in (mc.alt_commands or ()) if c and c != mc.command]
    out = ""
    tried = []
    rejected = False
    used = candidates[0]
    for idx, cand in enumerate(candidates):
        tried.append(cand)
        # on_line 事件带上"实际执行的命令"与"是否是候选命令"（is_alt），前端据此展示
        on_line({"type": "cmd", "cmd": cand, "label": mc.label, "key": mc.key,
                 "is_alt": idx > 0, "attempt": idx + 1, "attempts": len(candidates)})
        out = conn.send_command(cand, read_timeout=timeout)
        rejected = is_command_rejected(out)
        if rejected and idx < len(candidates) - 1:
            on_line({"type": "cmd_fallback", "cmd": cand, "next_cmd": candidates[idx + 1],
                     "label": mc.label, "key": mc.key, "reason": "设备不识别该命令"})
            continue
        used = cand
        break
    return out, tried, rejected, used


def _connect_and_run(params, key_text, metric_cmds, custom_cmds, disable_pager, on_line):
    """连接设备，执行巡检命令，收集输出。

    流程：创建 SSH 连接 → 进入 enable 模式（若配了 secret）
    → 禁用分页 → 逐条执行指标命令（主命令被拒时自动试候选命令 alt_commands）
    → 执行自定义命令
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
                out, tried, rejected, used = _run_metric_command(
                    conn, mc, on_line, settings.inspection_timeout)
                outputs.append({"cmd": used, "label": mc.label, "key": mc.key,
                                "unit": mc.unit, "textfsm": mc.textfsm, "output": out,
                                "tried": tried, "rejected": rejected,
                                "is_alt": used != mc.command})
                on_line({"type": "output", "cmd": used, "label": mc.label, "key": mc.key,
                         "output": out, "is_alt": used != mc.command, "rejected": rejected})
            for c in custom_cmds:
                on_line({"type": "cmd", "cmd": c, "label": c})
                out = conn.send_command(c, read_timeout=settings.inspection_timeout)
                outputs.append({"cmd": c, "label": c, "key": "", "unit": "", "textfsm": "", "output": out})
                on_line({"type": "output", "cmd": c, "output": out})
    finally:
        if key_file and os.path.exists(key_file):
            os.remove(key_file)
    return outputs


def _finalize_metric_parse(item: dict, device_type: str = "") -> dict:
    """单条命令输出 → 解析结果 {parsed, summary, status}。

    ★ 所有候选命令都被设备拒绝 ⇒ status="unsupported"，摘要写成人话：
      "该机型不支持此指标（已尝试：display environment）" ——
      明确区分"设备没有这个指标特性"与"命令支持但没解析出来（unknown）"。

    注：`command` 这里保持与历史行为一致不传（旧代码 `item.get("command")` 是个
    永远取到空串的笔误，等于 ntc-templates 在服务链路从未生效）。command 参数
    只影响 ntc 模板选择，若现在把它接通，华为 version/device/temperature 等
    指标会被 reroute 到未经真机验证的 ntc 模板（无法用基线证明不回归），故不动。
    """
    if item.get("rejected"):
        tried = [c for c in (item.get("tried") or [item.get("cmd") or ""]) if c]
        return {"parsed": None, "status": "unsupported",
                "summary": "该机型不支持此指标（已尝试：%s）" % "、".join(tried)}
    return parse_output(item.get("key", ""), item.get("output") or "",
                        item.get("textfsm", ""), device_type=device_type)


def _inherit_alt_commands(cmds: list, vendor: str) -> list:
    """DB 模板项未配置 alt_commands 时，从内置驱动同 key+同命令的指标继承候选命令。

    为什么必须做：存量部署的 DB 里已经种过系统模板（items 里没有 alt_commands
    字段），seeding 只在"表里没有任何系统模板"时才写库 —— 只改驱动、不改这里，
    候选回退对老库永远不会生效。按 (key, command) 精确匹配回填；用户显式配置过
    alt_commands 的项不被覆盖。
    """
    if not cmds:
        return cmds
    try:
        driver = get_driver(vendor)
        fallback = {(m.key, m.command): tuple(m.alt_commands)
                    for m in driver.standard_metrics() if m.alt_commands}
    except Exception:  # noqa: BLE001 — 继承失败不阻塞巡检
        return cmds
    if not fallback:
        return cmds
    out = []
    for mc in cmds:
        if not mc.alt_commands:
            alt = fallback.get((mc.key, mc.command))
            if alt:
                mc = _dc_replace(mc, alt_commands=alt)
        out.append(mc)
    return out


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
        # ★ unit/textfsm/alt_commands 必须用关键字传参：此前按位置传会把 unit 落进
        #   regex 字段（MetricCommand 第 5 个位置参数是 regex），unit 一直悄悄丢失。
        cmds = [MetricCommand(
            it.get("key", ""), it.get("label", ""), it.get("command", ""),
            textfsm=it.get("textfsm", ""), unit=it.get("unit", ""),
            alt_commands=tuple(it.get("alt_commands") or ()),
        ) for it in row.items]
        return _inherit_alt_commands(cmds, vendor)

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
    if asset.credential_id and cred is None:
        # 外部审查 U7-F12：列上原本没有 ondelete，SQLite 默认又不校验外键，
        # 于是"凭据被删、资产还指着它"会留下悬挂引用。改前这里会静默退化成
        # 空用户名/空口令去连设备，报出来的是一句莫名其妙的认证失败 ——
        # 真实原因是凭据没了，就直接说清楚。
        err = (f"该资产关联的凭据已不存在（credential_id={asset.credential_id}）："
               f"请到「资产管理」重新为该资产选择凭据")
        emit({"type": "error", "asset_id": asset.id, "asset_name": asset_name, "error": err})
        return {"asset_id": asset.id, "asset_name": asset_name, "status": "failed",
                "error": err, "metrics": {}, "raw": []}
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
        # ★ 先做 TCP 预检（默认 3s）：不可达就**立刻**报错，不要等 OS 的 SYN 重试（~127s）。
        _preflight(asset.host, params.get("port"), settings.inspection_preflight_timeout)
        outputs = await asyncio.to_thread(_connect_and_run, params, key_text,
                                          metric_cmds, custom_cmds,
                                          settings.enable_pager_disable, on_line)
    except Exception as e:
        # ★ 不直接把原始异常抛给运维：老设备的 SSH 握手失败要翻译成"能照着做"的提示，
        #   但**原始错误仍然附在末尾**（保留证据，不掩盖）。
        err = _friendly_ssh_error(e)
        emit({"type": "error", "asset_id": asset.id, "asset_name": asset_name, "error": err})
        return {"asset_id": asset.id, "asset_name": asset_name, "status": "failed", "error": err, "metrics": {}, "raw": []}

    metrics = {}
    raw = []
    for item in outputs:
        pr = _finalize_metric_parse(item, device_type=params.get("device_type", ""))
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
    # ★ 参数 db 仅为兼容外部签名而保留（调用方 run_task_in_background / API 不改），
    #   并发分支**绝不能**再用它：同一 AsyncSession 不允许并发使用，≥2 台设备时
    #   第二个并发 DB 操作会在 _connection_for_bind 抛 InvalidRequestError
    #   （"This session is provisioning a new connection; concurrent operations are
    #   not permitted"），会话关闭时再叠一个 IllegalStateChangeError，异常被调用方
    #   except 吞掉 ⇒ 多设备任务 0 条结果、被标 failed（生产实测；回归护栏见
    #   tests/test_inspection_concurrent_sessions.py）。因此本函数体不再触碰 db，
    #   每个并发分支在下面自建会话，并沿 inspect_one 把会话传给该分支全部 DB 读取
    #   （get_asset / get_credential_for_asset / _resolve_template_cmds）。
    # 通过 Semaphore 限制同时连接的设备数，避免压跨网络
    sem = asyncio.Semaphore(max(1, settings.inspection_concurrency))
    results = []

    async def _run(aid):
        # 会话在拿到并发名额后再开：同一时刻打开的分支会话数被 Semaphore 钉在
        # inspection_concurrency 上，不随设备数量无限占用连接池；分支内的所有
        # DB 读取都走这个自建会话，某台失败不影响其它台。
        async with sem:
            async with async_session() as s:
                asset = await crud.get_asset(s, aid)
                if not asset:
                    return {"asset_id": aid, "asset_name": "", "status": "failed",
                            "error": "资产不存在", "metrics": {}, "raw": []}
                return await inspect_one(s, asset, kind, template, commands, on_event)

    tasks = [asyncio.create_task(_run(aid)) for aid in asset_ids]
    for t in asyncio.as_completed(tasks):
        results.append(await t)
    return results
