"""CT ZTP 开局接口。"""
from __future__ import annotations

import asyncio
import ipaddress

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models
from app.core.auth import get_current_user
from app.core.schemas import (
    ZtpClaimIn,
    ZtpDeviceIn,
    ZtpDeviceOut,
    ZtpGenerateResult,
    ZtpImportIn,
    ZtpImportOut,
    ZtpObservation,
    ZtpObservationsOut,
    ZtpPositionIn,
    ZtpPositionOut,
    ZtpTemplateIn,
    ZtpTemplateOut,
)
from app.core.timeutil import utcnow
from app.database import get_db
from app.core.ziputil import files_to_zip_response
from app.ct.ztp import positions as ztp_positions
from app.ct.ztp import server as ztp_server
from app.ct.ztp.generator import ZtpDevice as GenDevice
from app.ct.ztp.generator import ZtpProfile, generate_all

router = APIRouter()


def _safe_decrypt(enc):
    try:
        return crypto.decrypt(enc)
    except Exception:
        return ""


def _template_out(p: models.ZtpTemplate) -> ZtpTemplateOut:
    return ZtpTemplateOut(
        id=p.id, name=p.name, vendor=p.vendor,
        mgmt_vlan=p.mgmt_vlan, mgmt_interface=p.mgmt_interface,
        mgmt_netmask=p.mgmt_netmask, mgmt_gateway=p.mgmt_gateway,
        dns_servers=p.dns_servers or [], ntp_server=p.ntp_server,
        snmp_community=p.snmp_community, domain_name=p.domain_name,
        vlans=p.vlans or [], admin_user=p.admin_user, ssh_keys=p.ssh_keys or [],
        uplink_port=p.uplink_port, access_ports=p.access_ports or [],
        extra_config=p.extra_config,
        server_ip=p.server_ip, tftp_root=p.tftp_root, http_root=p.http_root,
        deploy_mode=p.deploy_mode, dhcp_iface=p.dhcp_iface,
        dhcp_start=p.dhcp_start, dhcp_end=p.dhcp_end,
        remark=p.remark, created_at=p.created_at,
    )


def _to_profile(p: models.ZtpTemplate) -> ZtpProfile:
    return ZtpProfile(
        vendor=(p.vendor or "h3c").strip().lower(), mgmt_vlan=p.mgmt_vlan,
        mgmt_interface=p.mgmt_interface, mgmt_netmask=p.mgmt_netmask,
        mgmt_gateway=p.mgmt_gateway, dns_servers=p.dns_servers or [],
        ntp_server=p.ntp_server, snmp_community=p.snmp_community,
        domain_name=p.domain_name, vlans=p.vlans or [],
        admin_user=p.admin_user,
        admin_password=_safe_decrypt(p.admin_password_enc),
        enable_secret=_safe_decrypt(p.enable_secret_enc),
        ssh_keys=p.ssh_keys or [],
        uplink_port=p.uplink_port, access_ports=p.access_ports or [],
        extra_config=p.extra_config,
        server_ip=p.server_ip, tftp_root=p.tftp_root, http_root=p.http_root,
        deploy_mode=p.deploy_mode, dhcp_iface=p.dhcp_iface,
        dhcp_start=p.dhcp_start, dhcp_end=p.dhcp_end,
    )


# ---------- 模板 CRUD ----------
@router.get("/templates", response_model=list[ZtpTemplateOut])
# GET /api/ct/ztp/templates — ZTP 开局模板列表
async def list_templates(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.ZtpTemplate).order_by(models.ZtpTemplate.created_at.desc()))
    return [_template_out(t) for t in res.scalars().all()]


@router.post("/templates", response_model=ZtpTemplateOut)
# POST /api/ct/ztp/templates — 新建 ZTP 模板
async def create_template(body: ZtpTemplateIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = models.ZtpTemplate(
        name=body.name, vendor=(body.vendor or "h3c").strip().lower(),
        mgmt_vlan=body.mgmt_vlan, mgmt_interface=body.mgmt_interface,
        mgmt_netmask=body.mgmt_netmask, mgmt_gateway=body.mgmt_gateway,
        dns_servers=body.dns_servers, ntp_server=body.ntp_server,
        snmp_community=body.snmp_community, domain_name=body.domain_name,
        vlans=body.vlans, admin_user=body.admin_user,
        admin_password_enc=crypto.encrypt(body.admin_password or ""),
        enable_secret_enc=crypto.encrypt(body.enable_secret or ""),
        ssh_keys=body.ssh_keys, uplink_port=body.uplink_port,
        access_ports=body.access_ports, extra_config=body.extra_config,
        server_ip=body.server_ip, tftp_root=body.tftp_root, http_root=body.http_root,
        deploy_mode=body.deploy_mode, dhcp_iface=body.dhcp_iface,
        dhcp_start=body.dhcp_start, dhcp_end=body.dhcp_end,
        remark=body.remark,
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return _template_out(t)


@router.put("/templates/{tid}", response_model=ZtpTemplateOut)
async def update_template(tid: str, body: ZtpTemplateIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = await db.get(models.ZtpTemplate, tid)
    if not t:
        raise HTTPException(status_code=404, detail="模板不存在")
    t.name = body.name
    t.vendor = (body.vendor or "h3c").strip().lower()
    t.mgmt_vlan = body.mgmt_vlan
    t.mgmt_interface = body.mgmt_interface
    t.mgmt_netmask = body.mgmt_netmask
    t.mgmt_gateway = body.mgmt_gateway
    t.dns_servers = body.dns_servers
    t.ntp_server = body.ntp_server
    t.snmp_community = body.snmp_community
    t.domain_name = body.domain_name
    t.vlans = body.vlans
    t.admin_user = body.admin_user
    if body.admin_password is not None:
        t.admin_password_enc = crypto.encrypt(body.admin_password)
    if body.enable_secret is not None:
        t.enable_secret_enc = crypto.encrypt(body.enable_secret)
    t.ssh_keys = body.ssh_keys
    t.uplink_port = body.uplink_port
    t.access_ports = body.access_ports
    t.extra_config = body.extra_config
    t.server_ip = body.server_ip
    t.tftp_root = body.tftp_root
    t.http_root = body.http_root
    t.deploy_mode = body.deploy_mode
    t.dhcp_iface = body.dhcp_iface
    t.dhcp_start = body.dhcp_start
    t.dhcp_end = body.dhcp_end
    t.remark = body.remark
    await db.commit()
    await db.refresh(t)
    return _template_out(t)


@router.delete("/templates/{tid}")
async def delete_template(tid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = await db.get(models.ZtpTemplate, tid)
    if t:
        await db.delete(t)
        await db.commit()
    return {"ok": True}


# ---------- 设备清单 ----------
@router.get("/devices", response_model=list[ZtpDeviceOut])
async def list_devices(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.ZtpDevice).order_by(models.ZtpDevice.created_at.desc()))
    return list(res.scalars().all())


@router.post("/devices", response_model=ZtpDeviceOut)
async def create_device(body: ZtpDeviceIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    # MAC 与落位接口用**同一套归一/校验**（外部审查 F5）：以前这里原样存库，
    # 而生成时又原样写进 `dhcp-host=` —— 大写或 aabb.ccdd.eeff 写法的 MAC
    # 要么让 dnsmasq 配置非法，要么与租约里的归一 MAC 对不上、设备静默只拿到 default.cfg。
    d = models.ZtpDevice(
        template_id=body.template_id,
        hostname=(body.hostname or "").strip(),
        mac=ztp_positions.norm_mac(body.mac),
        serial=(body.serial or "").strip(), mgmt_ip=body.mgmt_ip,
    )
    if (body.mac or "").strip() and not d.mac:
        raise HTTPException(status_code=422, detail="MAC 格式不正确：%s" % body.mac)
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


@router.delete("/devices/{did}")
async def delete_device(did: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    d = await db.get(models.ZtpDevice, did)
    if d:
        await db.delete(d)
        await db.commit()
    return {"ok": True}


# ---------- 落位登记（落位 + 认领） ----------
# 背景：设备到货时只有「落位 + 规划管理 IP + 规划主机名」，没有 MAC（还没上电）。
# 先按落位登记（MAC 留空）；设备上电后从 dnsmasq 租约里自动学到 MAC（/observations），
# 运维做一步「认领」（/claim）把它指到落位，之后即可按 MAC 下发各自配置。
def _check_mgmt_ip(raw: str) -> str:
    """管理 IP 必须能被 ipaddress 解析；合法时原样保留（strip 过首尾空白）。"""
    v = (raw or "").strip()
    try:
        ipaddress.ip_address(v)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"管理 IP 格式不正确：{v}")
    return v


def _norm_mac_or_422(raw: str) -> str:
    """MAC 归一；非空但解析不了 → 422（空串 = 待认领，合法）。"""
    s = (raw or "").strip()
    if not s:
        return ""
    m = ztp_positions.norm_mac(s)
    if not m:
        raise HTTPException(status_code=422, detail=f"MAC 格式不正确：{s}")
    return m


def _dup_conflicts(existing, position: str, mgmt_ip: str, mac: str, exclude_id=None):
    """返回与 (position, mgmt_ip, mac) 冲突的既有落位（供 409 文案点名）。"""
    for e in existing:
        if exclude_id and e.id == exclude_id:
            continue
        if e.position == position:
            return e, "落位"
        if e.mgmt_ip == mgmt_ip:
            return e, "管理 IP"
        if mac and e.mac == mac:
            return e, "MAC"
    return None, ""


@router.get("/positions", response_model=list[ZtpPositionOut])
# GET /api/ct/ztp/positions — 落位登记列表（template_id 为空返回全部），按落位编码排序
async def list_positions(template_id: str = "", db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    q = select(models.ZtpPosition)
    if template_id:
        q = q.where(models.ZtpPosition.template_id == template_id)
    res = await db.execute(q.order_by(models.ZtpPosition.position))
    return list(res.scalars().all())


@router.post("/positions", response_model=ZtpPositionOut)
# POST /api/ct/ztp/positions — 新建落位（MAC 留空 = 待认领，上电后从租约学到再认领）
async def create_position(body: ZtpPositionIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = await db.get(models.ZtpTemplate, body.template_id) if body.template_id else None
    if not t:
        raise HTTPException(status_code=404, detail="模板不存在")
    position = (body.position or "").strip()
    mgmt_ip = _check_mgmt_ip(body.mgmt_ip)
    mac = _norm_mac_or_422(body.mac)
    res = await db.execute(
        select(models.ZtpPosition).where(models.ZtpPosition.template_id == body.template_id)
    )
    owner, what = _dup_conflicts(list(res.scalars().all()), position, mgmt_ip, mac)
    if owner is not None:
        if what == "落位":
            raise HTTPException(status_code=409, detail=f"该模板下已存在落位 {position}")
        if what == "管理 IP":
            raise HTTPException(status_code=409, detail=f"该模板下管理 IP {mgmt_ip} 已被落位 {owner.position} 使用")
        raise HTTPException(status_code=409, detail=f"该 MAC 已被落位 {owner.position} 认领")
    p = models.ZtpPosition(
        template_id=body.template_id, position=position,
        hostname=(body.hostname or "").strip(), mgmt_ip=mgmt_ip,
        serial=(body.serial or "").strip(), mac=mac, remark=body.remark or "",
        source="manual", claimed_at=(utcnow() if mac else None),
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


@router.put("/positions/{pid}", response_model=ZtpPositionOut)
# PUT /api/ct/ztp/positions/{pid} — 编辑落位（同样的唯一性校验；找不到 → 404）
async def update_position(pid: str, body: ZtpPositionIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    p = await db.get(models.ZtpPosition, pid)
    if not p:
        raise HTTPException(status_code=404, detail="落位不存在")
    tid = (body.template_id or "").strip() or p.template_id
    if tid != p.template_id:
        t = await db.get(models.ZtpTemplate, tid)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
    position = (body.position or "").strip()
    mgmt_ip = _check_mgmt_ip(body.mgmt_ip)
    mac = _norm_mac_or_422(body.mac)
    res = await db.execute(
        select(models.ZtpPosition).where(models.ZtpPosition.template_id == tid)
    )
    owner, what = _dup_conflicts(list(res.scalars().all()), position, mgmt_ip, mac, exclude_id=pid)
    if owner is not None:
        if what == "落位":
            raise HTTPException(status_code=409, detail=f"该模板下已存在落位 {position}")
        if what == "管理 IP":
            raise HTTPException(status_code=409, detail=f"该模板下管理 IP {mgmt_ip} 已被落位 {owner.position} 使用")
        raise HTTPException(status_code=409, detail=f"该 MAC 已被落位 {owner.position} 认领")
    p.template_id = tid
    p.position = position
    p.hostname = (body.hostname or "").strip()
    p.mgmt_ip = mgmt_ip
    p.serial = (body.serial or "").strip()
    p.remark = body.remark or ""
    # MAC 变更时同步认领状态：清空 = 回到待认领；手工填新 MAC = 手工认领。
    if mac != (p.mac or ""):
        p.mac = mac
        p.claimed_at = utcnow() if mac else None
        p.source = "manual"
    await db.commit()
    await db.refresh(p)
    return p


@router.delete("/positions/{pid}")
# DELETE /api/ct/ztp/positions/{pid} — 删除落位
async def delete_position(pid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    p = await db.get(models.ZtpPosition, pid)
    if p:
        await db.delete(p)
        await db.commit()
    return {"ok": True}


@router.post("/positions/import", response_model=ZtpImportOut)
# POST /api/ct/ztp/positions/import — 批量导入落位。
# replace=True 先清空该模板下**所有**落位（破坏性操作，由调用方显式指定）；
# 同一模板内已存在相同 position 的行做更新，否则新增；解析/校验失败的行计入 errors+skipped。
async def import_positions(body: ZtpImportIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    if not body.template_id:
        raise HTTPException(status_code=404, detail="模板不存在")
    t = await db.get(models.ZtpTemplate, body.template_id)
    if not t:
        raise HTTPException(status_code=404, detail="模板不存在")
    rows, errors = ztp_positions.parse_positions_csv(body.csv)
    # ⚠ 外部审查 F1（我复核确认，属**数据丢失**级）：`replace=true` 以前是**无条件先删**，
    # 再按 rows 插入。CSV 用了错的分隔符 / 全部行都非法 / 干脆是空串时 rows 为空，
    # 于是这次提交等于"纯删除"——整张落位表被清空，接口还返回 200（只显示"跳过 N 行"）。
    # 现在：没有任何可用行时**直接拒绝**，一行都不删。
    if body.replace and not rows:
        raise HTTPException(
            status_code=422,
            detail=("replace=true，但这次 CSV 里没有任何一行可用（跳过 %d 行）；"
                    "为防误清空，**已取消本次导入，未删除任何落位**。"
                    "请修好 CSV（表头/列顺序/管理 IP）后重试。" % len(errors)),
        )
    if body.replace:
        res = await db.execute(
            select(models.ZtpPosition).where(models.ZtpPosition.template_id == body.template_id)
        )
        for e in res.scalars().all():
            await db.delete(e)
        await db.flush()
    res = await db.execute(
        select(models.ZtpPosition).where(models.ZtpPosition.template_id == body.template_id)
    )
    existing = list(res.scalars().all())
    by_position = {e.position: e for e in existing}

    # ── 两遍处理：**先算出"这份 CSV 的最终状态"，再落库**。
    # 为什么不能边遍历边判重（外部审查 F6，我复核后确认第一版修法仍不够）：
    # 把两条既有落位的管理 IP **互换**时，第一行换过去撞到"当前还属于另一条"的 IP，
    # 只要拿当前状态判重就必然误报。批内互换是合法操作，必须按"最终状态"判。
    planned = {}
    for row in rows:
        lineno = row.get("_line", "?")
        position, mgmt_ip = row["position"], row["mgmt_ip"]
        raw_mac = (row.get("mac") or "").strip()
        mac = ztp_positions.norm_mac(raw_mac)
        if raw_mac and not mac:
            # 写了 MAC 但解析不出来：不能静默当成"没填"（那会让这条落位悄悄保持未认领）
            errors.append("第%s行: MAC 格式不正确：%s" % (lineno, raw_mac))
            continue
        planned[position] = {"row": row, "lineno": lineno, "mgmt_ip": mgmt_ip, "mac": mac}

    # 批内唯一：同一 IP / 同一 MAC 出现在两个不同落位 → 报错并丢掉**后出现**的那条
    ip_seen, mac_seen = {}, {}
    for position, item in list(planned.items()):
        ip, mac, ln = item["mgmt_ip"], item["mac"], item["lineno"]
        if ip in ip_seen:
            errors.append("第%s行: 管理 IP %s 与落位 %s 在本批次内重复" % (ln, ip, ip_seen[ip]))
            del planned[position]
            continue
        if mac and mac in mac_seen:
            errors.append("第%s行: MAC %s 与落位 %s 在本批次内重复" % (ln, mac, mac_seen[mac]))
            del planned[position]
            continue
        ip_seen[ip] = position
        if mac:
            mac_seen[mac] = position

    # 批外冲突：被本批"用到"的 IP/MAC 若属于**不在本批里**的既有落位 → 报错
    batch_positions = set(planned)
    for e in existing:
        if e.position in batch_positions:
            continue
        if (e.mgmt_ip or "") in ip_seen:
            owner = ip_seen[e.mgmt_ip or ""]
            errors.append("第%s行: 管理 IP %s 与落位 %s 重复"
                          % (planned[owner]["lineno"], e.mgmt_ip, e.position))
            del planned[owner]
            batch_positions.discard(owner)
        if e.mac and e.mac in mac_seen:
            owner = mac_seen[e.mac]
            if owner in planned:
                errors.append("第%s行: MAC %s 已被落位 %s 认领"
                              % (planned[owner]["lineno"], e.mac, e.position))
                del planned[owner]

    created = updated = 0
    # 第一趟：把**要改值**的行先挪到"临时唯一值"。
    # 为什么必须这样：唯一约束 (template_id, mgmt_ip) 是**立即**生效的（SQLite 没有延迟唯一约束），
    # 批内互换两条落位的管理 IP 时，第一条 UPDATE 就会撞上第二条**当前**还占着的地址，
    # 直接 IntegrityError。先挪到 `__tmp__<行id>` 把"值"腾空，第二趟再写最终值。
    for position, item in planned.items():
        target = by_position.get(position)
        if target is None:
            continue
        new_ip, new_mac = item["mgmt_ip"], item["mac"]
        if (target.mgmt_ip or "") != new_ip or (new_mac and (target.mac or "") != new_mac):
            target.mgmt_ip = "__tmp__%s" % target.id
            target.mac = ""       # 非空 MAC 的部分唯一索引，空串不参与约束
    await db.flush()

    # 第二趟：写最终值
    for position, item in planned.items():
        row, mgmt_ip, mac = item["row"], item["mgmt_ip"], item["mac"]
        target = by_position.get(position)
        if target is not None:
            target.hostname = row["hostname"]
            target.serial = row["serial"]
            target.remark = row["remark"]
            target.mgmt_ip = mgmt_ip
            # CSV 行 MAC 留空 = 不动已有认领状态（避免批量导入误清认领）
            if mac and mac != (target.mac or ""):
                target.mac = mac
                if not target.claimed_at:
                    target.claimed_at = utcnow()
            updated += 1
        else:
            target = models.ZtpPosition(
                template_id=body.template_id, position=position,
                hostname=row["hostname"], mgmt_ip=mgmt_ip, serial=row["serial"],
                mac=mac, remark=row["remark"], source="manual",
                claimed_at=(utcnow() if mac else None),
            )
            db.add(target)
            created += 1
        by_position[position] = target
    await db.commit()
    return ZtpImportOut(created=created, updated=updated, skipped=len(errors), errors=errors)


@router.get("/observations", response_model=ZtpObservationsOut)
# GET /api/ct/ztp/observations — 从 dnsmasq 租约里"学"到的上电设备列表，
# 并用（该模板的）落位表按 MAC 反查：命中 = 已认领，填 position_id/position/claimed_hostname。
# 租约文件读不到**不会 500**：positions.read_leases 把异常转成 note 说明文字。
async def list_observations(template_id: str = "", db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    leases_path, note, records = ztp_positions.read_leases()
    q = select(models.ZtpPosition)
    if template_id:
        q = q.where(models.ZtpPosition.template_id == template_id)
    res = await db.execute(q)
    by_mac = {}
    for pos in res.scalars().all():
        m = ztp_positions.norm_mac(pos.mac or "")
        if m:
            by_mac[m] = pos
    observations = []
    for rec in records:
        obs = ZtpObservation(
            mac=rec["mac"], ip=rec["ip"], hostname=rec["hostname"],
            client_id=rec["client_id"], expires=rec["expires"],
        )
        pos = by_mac.get(rec["mac"])
        if pos is not None:
            obs.position_id = pos.id
            obs.position = pos.position
            obs.claimed_hostname = pos.hostname
        observations.append(obs)
    return ZtpObservationsOut(leases_path=leases_path, note=note, observations=observations)


@router.post("/claim")
# POST /api/ct/ztp/claim — 认领：把租约里学到的 MAC 指到一条落位。
# 成功后必须**重新生成并部署**，该设备才会按 MAC 拿到自己落位的配置。
async def claim_position(body: ZtpClaimIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    p = await db.get(models.ZtpPosition, body.position_id)
    if not p:
        raise HTTPException(status_code=404, detail="落位不存在")
    mac = ztp_positions.norm_mac(body.mac)
    if not mac:
        raise HTTPException(status_code=422, detail="MAC 格式不正确")
    res = await db.execute(
        select(models.ZtpPosition).where(models.ZtpPosition.template_id == p.template_id)
    )
    for e in res.scalars().all():
        if e.id != p.id and (e.mac or "") == mac:
            raise HTTPException(status_code=409, detail=f"该 MAC 已被落位 {e.position} 认领")
    p.mac = mac
    p.claimed_at = utcnow()
    p.source = "claim"
    await db.commit()
    await db.refresh(p)
    return {
        "ok": True,
        "position": ZtpPositionOut.model_validate(p),
        "next": ["请重新生成并部署，让该设备取到自己的配置"],
    }


# ---------- 生成 ----------
async def _gen_ztp_files(tid: str, body: dict, db: AsyncSession) -> dict:
    """生成 ZTP 全部部署文件 (设备配置 + dnsmasq + 中间文件 + README)。"""
    t = await db.get(models.ZtpTemplate, tid)
    if not t:
        raise HTTPException(status_code=404, detail="模板不存在")
    body = body or {}
    prof = _to_profile(t)
    if body.get("deploy_mode"):
        prof.deploy_mode = body["deploy_mode"]
    if body.get("server_ip"):
        prof.server_ip = body["server_ip"]
    if body.get("http_root"):
        prof.http_root = body["http_root"]

    res = await db.execute(
        select(models.ZtpDevice).where(models.ZtpDevice.template_id == tid)
    )
    db_devices = [
        GenDevice(hostname=d.hostname, mac=d.mac, serial=d.serial, mgmt_ip=d.mgmt_ip or "")
        for d in res.scalars().all()
    ]
    inline_devices = [
        GenDevice(hostname=x.get("hostname", ""), mac=x.get("mac", ""),
                  serial=x.get("serial", ""), mgmt_ip=x.get("mgmt_ip", ""))
        for x in body.get("devices", [])
    ]
    devices = db_devices + inline_devices
    # 落位登记（落位 + 认领）：落位设备排在 devices 之后，一并喂给生成器 ——
    # 已认领的落位按 MAC 下发各自配置，未认领的落位只生成注释占位。
    res_pos = await db.execute(
        select(models.ZtpPosition).where(models.ZtpPosition.template_id == tid)
    )
    positions = list(res_pos.scalars().all())
    try:
        return generate_all(prof, devices, positions=positions)
    except ValueError as e:
        # 生成期校验失败（例如模板没填设备管理员口令）以 4xx + 中文提示暴露，而不是 500。
        # 与 api/pxe.py 的 _gen_pxe_files 同一处理口径。
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/templates/{tid}/generate", response_model=ZtpGenerateResult)
async def generate_files(tid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    files = await _gen_ztp_files(tid, body, db)
    return {"files": files}


@router.post("/templates/{tid}/download")
async def download_files(tid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """下载全部 ZTP 部署文件 (zip 压缩包)。"""
    files = await _gen_ztp_files(tid, body, db)
    return files_to_zip_response(files, "ztp-deploy.zip")


# ---------- 本机部署与服务管控 ----------
@router.get("/server/status")
async def server_status(_user=Depends(get_current_user)):
    """查看本机 ZTP 服务状态 (TFTP/HTTP 目录 + dnsmasq)。"""
    return ztp_server.server_status()


@router.post("/server/service")
async def service_control(body: dict = None, _user=Depends(get_current_user)):
    """控制 dnsmasq 服务: start/stop/restart/reload/status。"""
    action = (body or {}).get("action", "status")
    return ztp_server.service_control(action)


@router.post("/templates/{tid}/deploy")
async def deploy_to_host(tid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """一键部署到本机: 生成配置 -> 落地 TFTP/HTTP -> 让宿主机真正加载并核对生效。

    与 PXE 的 /deploy 同一口径：**ok=False 必须报错**，不能让"配置写下去了但
    dnsmasq 还是旧配置"这种状态被当成成功（R4 / RUNBOOK §5.50）。
    """
    body = body or {}
    srv = body.get("server_ip", "")
    if not srv:
        t = await db.get(models.ZtpTemplate, tid)
        srv = t.server_ip if t else ""
    body.setdefault("server_ip", srv)
    body.setdefault("http_root", ("http://" + srv + ":8000/ztp") if srv else "")
    files = await _gen_ztp_files(tid, body, db)
    # deploy_files 是同步函数，容器部署路径里它会**阻塞等待**宿主机重载完成
    # （最长 OPS_HOST_RELOAD_TIMEOUT，默认 25s）。与 api/pxe.py 一样丢到线程里，
    # 否则并发部署时整个事件循环停摆。
    res = await asyncio.to_thread(ztp_server.deploy_files, files, tid)
    if not res.get("supported"):
        return res
    if not res.get("ok"):
        detail = "ZTP 部署失败：" + "；".join(res.get("errors") or ["未知错误"])
        raise HTTPException(status_code=500, detail=detail[:800])
    return res
