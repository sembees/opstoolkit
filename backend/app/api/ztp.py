"""CT ZTP 开局接口。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models
from app.core.auth import get_current_user
from app.core.schemas import (
    ZtpDeviceIn,
    ZtpDeviceOut,
    ZtpGenerateResult,
    ZtpTemplateIn,
    ZtpTemplateOut,
)
from app.database import get_db
from app.core.ziputil import files_to_zip_response
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
        vendor=p.vendor, mgmt_vlan=p.mgmt_vlan,
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
async def list_templates(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.ZtpTemplate).order_by(models.ZtpTemplate.created_at.desc()))
    return [_template_out(t) for t in res.scalars().all()]


@router.post("/templates", response_model=ZtpTemplateOut)
async def create_template(body: ZtpTemplateIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    t = models.ZtpTemplate(
        name=body.name, vendor=body.vendor,
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
    t.vendor = body.vendor
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
    d = models.ZtpDevice(
        template_id=body.template_id,
        hostname=body.hostname, mac=body.mac, serial=body.serial, mgmt_ip=body.mgmt_ip,
    )
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
    return generate_all(prof, devices)


@router.post("/templates/{tid}/generate", response_model=ZtpGenerateResult)
async def generate_files(tid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    files = await _gen_ztp_files(tid, body, db)
    return {"files": files}


@router.post("/templates/{tid}/download")
async def download_files(tid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """下载全部 ZTP 部署文件 (zip 压缩包)。"""
    files = await _gen_ztp_files(tid, body, db)
    return files_to_zip_response(files, "ztp-deploy.zip")

