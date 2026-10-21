# -*- coding: utf-8 -*-
"""装机后"节点规划 + 批量变更"的 API（M1 第一刀：规划 CRUD 与单机 dry-run 生成）。

为什么单开一个模块而不是塞进 pxe.py：
  这条链路同时服务 PXE 装好的 Linux（install_id）与 ZTP 开局过的网络设备（ztp_device_id），
  放进任何一边都会让另一边"借"得别扭；而且它有自己的生命周期（规划 → 生成 → 下发 → 校验），
  与"装机"是两个阶段。

本刀只做：规划的增删改查 + 单机 dry-run 生成（调用现有 netconfig 生成器）。
**不做**下发（M2），也**不做**批量（下一刀），所以不会有任何副作用：不碰 dnsmasq、不碰目标机。
"""
import hashlib
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import models
from app.core.auth import get_current_user, require_role
from app.core.schemas import NetConfigRequest
from app.database import get_db
from app.it.netconfig.generator import generate_netconfig

router = APIRouter()

IFACE_KINDS = ("interface", "bond", "vlan", "bridge")
# 生成后端缺省：与 netconfig 三个后端的能力边界一致（netplan 仅 Ubuntu）
_DEFAULT_FMT = {"ubuntu": "netplan", "rhel": "nmcli"}


# ── schemas（放在本模块：只被这一组端点使用，没必要污染 core/schemas.py）──────────

class PlanIfaceIn(BaseModel):
    kind: str = "interface"
    name: str = ""
    spec: dict = {}

    @field_validator("kind")
    @classmethod
    def _check_kind(cls, v: str) -> str:
        t = (v or "").strip().lower()
        if t not in IFACE_KINDS:
            raise ValueError("kind 只允许 " + " / ".join(IFACE_KINDS))
        return t


class PlanIn(BaseModel):
    install_id: Optional[str] = None
    ztp_device_id: Optional[str] = None
    serial: str = ""
    serial_trusted: bool = True
    key_source: str = ""          # 空 = 按 serial_trusted 推（可信→serial，不可信→mac）
    mac: str = ""
    rack: str = ""
    planned_hostname: str = ""
    mgmt_ip: str = ""
    os_type: str = "ubuntu"
    fmt: str = ""
    note: str = ""
    interfaces: list[PlanIfaceIn] = []

    @field_validator("os_type")
    @classmethod
    def _check_os(cls, v: str) -> str:
        t = (v or "").strip().lower()
        if t not in ("ubuntu", "rhel"):
            raise ValueError("os_type 目前只支持 ubuntu / rhel（与 netconfig 的三个后端对齐）")
        return t

    @field_validator("key_source")
    @classmethod
    def _check_src(cls, v: str) -> str:
        t = (v or "").strip().lower()
        if t and t not in ("serial", "mac"):
            raise ValueError("key_source 只允许 serial / mac")
        return t


class PlanOut(BaseModel):
    id: str
    install_id: Optional[str] = None
    ztp_device_id: Optional[str] = None
    serial: str
    serial_trusted: bool
    key_source: str
    mac: str
    rack: str
    planned_hostname: str
    mgmt_ip: str
    os_type: str
    fmt: str
    status: str
    note: str
    created_by: str
    updated_by: str
    interfaces: list[dict] = []


def _out(p: models.NodePlan, ifaces: list[models.NodePlanInterface]) -> dict:
    return {
        "id": p.id, "install_id": p.install_id, "ztp_device_id": p.ztp_device_id,
        "serial": p.serial, "serial_trusted": bool(p.serial_trusted),
        "key_source": p.key_source or "serial", "mac": p.mac or "",
        "rack": p.rack or "", "planned_hostname": p.planned_hostname or "",
        "mgmt_ip": p.mgmt_ip or "", "os_type": p.os_type or "ubuntu",
        "fmt": p.fmt or "", "status": p.status or "pending",
        "enabled": bool(getattr(p, "enabled", False)), "note": p.note or "",
        "created_by": p.created_by or "", "updated_by": p.updated_by or "",
        "interfaces": [{"kind": i.kind, "name": i.name, "spec": i.spec or {}} for i in ifaces],
    }


DISABLED_MSG = ("本功能未开启。它是 PXE 装机之后的**额外**功能，需先由管理员在"
                "「设置 → 装机后网络规划」里手动开启（全局），再对具体机器点「为该机开启」。")


async def _settings(db: AsyncSession) -> models.NodePlanSettings:
    """取全局开关（单行表，懒创建：缺行 = 未开启）。"""
    row = await db.get(models.NodePlanSettings, 1)
    if row is None:
        row = models.NodePlanSettings(id=1, enabled=False)
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


async def _require_global(db: AsyncSession) -> None:
    """全局总闸：关着就挡在**任何写操作之前**（不做事后拦截）。"""
    if not (await _settings(db)).enabled:
        raise HTTPException(status_code=409, detail=DISABLED_MSG)


async def _get_or_404(db: AsyncSession, pid: str) -> models.NodePlan:
    p = await db.get(models.NodePlan, pid)
    if p is None:
        raise HTTPException(status_code=404, detail="规划不存在")
    return p


async def _ifaces(db: AsyncSession, pid: str) -> list[models.NodePlanInterface]:
    res = await db.execute(
        select(models.NodePlanInterface)
        .where(models.NodePlanInterface.plan_id == pid)
        .order_by(models.NodePlanInterface.sort, models.NodePlanInterface.name))
    return list(res.scalars().all())


def _resolve_key(body: PlanIn) -> tuple[str, bool]:
    """决定业务键：用户拍板 SN 做主键，但 SN 不可信时必须改走 MAC（否则白牌机/虚机卡死）。"""
    serial = (body.serial or "").strip()
    mac = (body.mac or "").strip()
    src = body.key_source or ("serial" if body.serial_trusted else "mac")
    if src == "serial":
        if not serial:
            raise HTTPException(
                status_code=422,
                detail="key_source=serial 时必须提供机箱 SN；取不到 SN 请勾选「用 MAC 登记」")
        if not body.serial_trusted:
            raise HTTPException(
                status_code=422,
                detail="SN 已被标记为不可信（serial_trusted=false），不能作为键；请改用 MAC 登记")
    else:
        if not mac:
            raise HTTPException(status_code=422, detail="key_source=mac 时必须提供 MAC")
    return src, (serial if src == "serial" else mac)


async def _check_unique(db: AsyncSession, body: PlanIn, src: str, key: str,
                        exclude: str = "") -> None:
    """SN（或 MAC）唯一：撞了就 409，并把已有那条的 id / 机架 / 状态指出来 —— 不静默覆盖。"""
    if src == "serial":
        res = await db.execute(select(models.NodePlan).where(models.NodePlan.serial == key))
    else:
        res = await db.execute(select(models.NodePlan).where(models.NodePlan.mac == key))
    for other in res.scalars().all():
        if other.id == exclude:
            continue
        raise HTTPException(
            status_code=409,
            detail="该 %s 已登记过：规划 %s（机架 %s，状态 %s）。请直接修改那条，或确认是否重复。"
                   % ("SN" if src == "serial" else "MAC", other.id,
                      other.rack or "未填", other.status or "pending"))


# ── CRUD ────────────────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)) -> list[dict]:
    res = await db.execute(select(models.NodePlan).order_by(models.NodePlan.created_at.desc()))
    plans = list(res.scalars().all())
    out = []
    for p in plans:
        out.append(_out(p, await _ifaces(db, p.id)))
    return out


@router.get("/plans/{pid}")
async def get_plan(pid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)) -> dict:
    p = await _get_or_404(db, pid)
    return _out(p, await _ifaces(db, pid))


@router.post("/plans")
async def create_plan(body: PlanIn, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)) -> dict:
    await _require_global(db)
    if not body.install_id and not body.ztp_device_id:
        raise HTTPException(status_code=422, detail="install_id（Linux）与 ztp_device_id（网络设备）必须给一个")
    if body.install_id and body.ztp_device_id:
        raise HTTPException(status_code=422, detail="install_id 与 ztp_device_id 只能给一个")
    src, key = _resolve_key(body)
    await _check_unique(db, body, src, key)

    p = models.NodePlan(
        install_id=body.install_id, ztp_device_id=body.ztp_device_id,
        serial=(body.serial or "").strip(), serial_trusted=bool(body.serial_trusted),
        key_source=src, mac=(body.mac or "").strip(), rack=body.rack,
        planned_hostname=body.planned_hostname, mgmt_ip=body.mgmt_ip,
        os_type=body.os_type, fmt=(body.fmt or "").strip(), note=body.note,
        created_by=user.get("username", ""), updated_by=user.get("username", ""),
    )
    db.add(p)
    await db.flush()
    for i, it in enumerate(body.interfaces):
        db.add(models.NodePlanInterface(plan_id=p.id, kind=it.kind, name=it.name,
                                        spec=it.spec or {}, sort=i))
    await db.commit()
    await db.refresh(p)
    return _out(p, await _ifaces(db, p.id))


@router.put("/plans/{pid}")
async def update_plan(pid: str, body: PlanIn, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)) -> dict:
    await _require_global(db)
    p = await _get_or_404(db, pid)
    src, key = _resolve_key(body)
    await _check_unique(db, body, src, key, exclude=pid)

    # install_id/ztp_device_id 允许在首次为空时补，但不允许把一台机器改成另一台
    if body.install_id and p.install_id and body.install_id != p.install_id:
        raise HTTPException(status_code=422, detail="不允许把规划改挂到另一台装机记录")
    if body.install_id:
        p.install_id = body.install_id
    if body.ztp_device_id and p.ztp_device_id and body.ztp_device_id != p.ztp_device_id:
        raise HTTPException(status_code=422, detail="不允许把规划改挂到另一台 ZTP 设备")
    if body.ztp_device_id:
        p.ztp_device_id = body.ztp_device_id

    p.serial = (body.serial or "").strip()
    p.serial_trusted = bool(body.serial_trusted)
    p.key_source = src
    p.mac = (body.mac or "").strip()
    p.rack, p.planned_hostname, p.mgmt_ip = body.rack, body.planned_hostname, body.mgmt_ip
    p.os_type, p.fmt, p.note = body.os_type, (body.fmt or "").strip(), body.note
    p.updated_by = user.get("username", "")

    # 接口整组替换（前端就是整表提交，逐条 diff 只会让"删一条"变得难以表达）
    old = await db.execute(select(models.NodePlanInterface)
                           .where(models.NodePlanInterface.plan_id == pid))
    for row in old.scalars().all():
        await db.delete(row)
    for i, it in enumerate(body.interfaces):
        db.add(models.NodePlanInterface(plan_id=pid, kind=it.kind, name=it.name,
                                        spec=it.spec or {}, sort=i))
    await db.commit()
    await db.refresh(p)
    return _out(p, await _ifaces(db, pid))


@router.delete("/plans/{pid}")
async def delete_plan(pid: str, db: AsyncSession = Depends(get_db),
                      _user=Depends(get_current_user)) -> dict:
    p = await db.get(models.NodePlan, pid)
    if p is None:
        return {"ok": True}          # 幂等：重复点删除不报错
    await db.delete(p)
    await db.commit()
    return {"ok": True}


# ── 生成（dry-run，不碰任何目标机）──────────────────────────────────────────

@router.post("/plans/{pid}/generate")
async def generate_plan(pid: str, db: AsyncSession = Depends(get_db),
                        _user=Depends(get_current_user)) -> dict:
    """把规划翻译成 netconfig 请求并生成脚本（**只生成，不下发**）。

    为什么要走现有生成器而不是自己拼字符串：三个后端（netplan/nmcli/ifcfg）的差异、
    转义、幂等、IPv6 三模式等规则都在生成器里沉淀过（含真机事故），重写一遍必然漏。
    """
    p = await _get_or_404(db, pid)
    await _require_global(db)
    if not bool(getattr(p, "enabled", False)):
        raise HTTPException(
            status_code=409,
            detail="这台机器还没开启（请在记录行上点「为该机开启」）。装机完成不会自动开启。")
    ifaces = await _ifaces(db, pid)
    if not ifaces:
        raise HTTPException(status_code=422, detail="规划里还没有任何网卡配置，先加至少一项再生成")

    buckets = {"interface": [], "bond": [], "vlan": [], "bridge": []}
    for it in ifaces:
        buckets.setdefault(it.kind, []).append(it.spec or {})

    fmt = p.fmt or _DEFAULT_FMT.get(p.os_type or "ubuntu", "nmcli")
    try:
        req = NetConfigRequest(
            os=p.os_type or "ubuntu",
            hostname=(p.planned_hostname or None),
            format=fmt,
            interfaces=buckets["interface"], bonds=buckets["bond"],
            vlans=buckets["vlan"], bridges=buckets["bridge"],
        )
        content, filename = generate_netconfig(req)
    except ValueError as e:
        # 生成器的报错本身就是要给运维看的那句话，原样透出（不要包成 500）
        raise HTTPException(status_code=422, detail=str(e))

    sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"plan_id": pid, "format": fmt, "file_name": filename,
            "sha256": sha, "content": content,
            "interface_count": len(ifaces),
            "status_hint": "dry-run：未下发，可下载或人工核对后再进入下发流程"}


# ── 全局开关 & 每机开关 ─────────────────────────────────────────────────────

@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)) -> dict:
    row = await _settings(db)
    return {"enabled": bool(row.enabled), "updated_by": row.updated_by or "",
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "hint": "默认关闭；开启后才允许登记规划。每台机器还要单独开启。"}


@router.put("/settings")
async def put_settings(body: dict = None, db: AsyncSession = Depends(get_db),
                       user=Depends(require_role("admin"))) -> dict:
    row = await _settings(db)
    row.enabled = bool((body or {}).get("enabled"))
    row.updated_by = user.get("username", "")
    await db.commit()
    await db.refresh(row)
    return {"enabled": bool(row.enabled), "updated_by": row.updated_by or ""}


@router.post("/plans/{pid}/enable")
async def enable_plan(pid: str, db: AsyncSession = Depends(get_db),
                      user=Depends(get_current_user)) -> dict:
    """对**某一台机器**手动开启（= 用户说的"由用户手动点击开启"）。"""
    await _require_global(db)
    p = await _get_or_404(db, pid)
    p.enabled = True
    p.updated_by = user.get("username", "")
    await db.commit()
    await db.refresh(p)
    return {"ok": True, "id": p.id, "enabled": True}


@router.post("/plans/{pid}/disable")
async def disable_plan(pid: str, db: AsyncSession = Depends(get_db),
                       user=Depends(get_current_user)) -> dict:
    await _require_global(db)
    p = await _get_or_404(db, pid)
    p.enabled = False
    p.updated_by = user.get("username", "")
    await db.commit()
    await db.refresh(p)
    return {"ok": True, "id": p.id, "enabled": False}
