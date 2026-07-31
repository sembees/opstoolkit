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

ProgressCb = Callable[[dict], None]


def get_pager_cmds(device_type: str) -> list:
    v = (device_type or "").lower()
    if "comware" in v or "hp" in v:
        return ["screen-length disable"]
    if "huawei" in v:
        return ["screen-length 0 temporary"]
    return ["terminal length 0"]


def _build_connect_params(asset: models.Asset, cred_plain: dict) -> dict:
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
    """按模板名+厂商从数据库解析巡检命令列表；DB 无则回落驱动内置默认。"""
    from sqlalchemy import select
    from app.core.models import InspectionTemplate

    vendor = asset.vendor or "generic"
    stmt = select(InspectionTemplate).where(InspectionTemplate.vendor == vendor)
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


async def inspect_many(db: AsyncSession, asset_ids: list, kind: str = "default",
                       template: Optional[str] = None, commands: Optional[list] = None,
                       on_event: Optional[ProgressCb] = None) -> list:
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
