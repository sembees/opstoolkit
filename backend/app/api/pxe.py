"""PXE 装机接口。"""
import socket
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models
from app.core.auth import get_current_user
from app.core.schemas import PxeGenerateResult, PxeInstallIn, PxeInstallOut, PxeProfileIn, PxeProfileOut
from app.database import get_db
from app.core.ziputil import files_to_zip_response
from app.it.pxe import server as pxe_server
from app.it.pxe.generator import PxeConfig, generate_all

router = APIRouter()


def _local_ip():
    """检测本机主要网卡 IP。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except Exception:
        return "127.0.0.1"


def _profile_out(p: models.PxeProfile) -> PxeProfileOut:
    return PxeProfileOut(
        id=p.id, name=p.name, os_type=p.os_type, os_version=p.os_version,
        timezone=p.timezone, locale=p.locale, keyboard=p.keyboard,
        admin_user=p.admin_user, ssh_keys=p.ssh_keys or [],
        disk_scheme=p.disk_scheme, disk_config=p.disk_config or {},
        net_mode=p.net_mode, net_config=p.net_config or {},
        mirror=p.mirror, extra_packages=p.extra_packages or [],
        post_script=p.post_script, remark=p.remark,
        server_ip="", http_root="", kernel_path="", initrd_path="", squashfs_path="",
        created_at=p.created_at,
    )


def _safe_decrypt(enc):
    try:
        return crypto.decrypt(enc)
    except Exception:
        return ""


def _to_pxeconfig(p: models.PxeProfile, server_ip="", http_root="",
                  kernel_path="", initrd_path="", squashfs_path="",
                  deploy_mode="standalone") -> PxeConfig:
    return PxeConfig(
        os_type=p.os_type, os_version=p.os_version,
        hostname="default", timezone=p.timezone, locale=p.locale, keyboard=p.keyboard,
        admin_user=p.admin_user,
        admin_password=_safe_decrypt(p.admin_password_enc),
        root_password=_safe_decrypt(p.root_password_enc),
        ssh_keys=p.ssh_keys or [],
        disk_scheme=p.disk_scheme, disk_config=p.disk_config or {},
        net_mode=p.net_mode, net_config=p.net_config or {},
        mirror=p.mirror, extra_packages=p.extra_packages or [],
        post_script=p.post_script,
        server_ip=server_ip or "192.168.1.100",
        http_root=http_root or ("http://" + (server_ip or "192.168.1.100") + ":8000/pxe"),
        kernel_path=kernel_path, initrd_path=initrd_path, squashfs_path=squashfs_path,
        deploy_mode=deploy_mode,
    )


# ---------- 模板 CRUD ----------
@router.get("/profiles", response_model=list[PxeProfileOut])
async def list_profiles(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.PxeProfile).order_by(models.PxeProfile.created_at.desc()))
    return [_profile_out(p) for p in res.scalars().all()]


@router.post("/profiles", response_model=PxeProfileOut)
async def create_profile(body: PxeProfileIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    p = models.PxeProfile(
        name=body.name, os_type=body.os_type, os_version=body.os_version,
        timezone=body.timezone, locale=body.locale, keyboard=body.keyboard,
        admin_user=body.admin_user,
        admin_password_enc=crypto.encrypt(body.admin_password),
        root_password_enc=crypto.encrypt(body.root_password),
        ssh_keys=body.ssh_keys,
        disk_scheme=body.disk_scheme, disk_config=body.disk_config,
        net_mode=body.net_mode, net_config=body.net_config,
        mirror=body.mirror, extra_packages=body.extra_packages,
        post_script=body.post_script, remark=body.remark,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return _profile_out(p)


@router.put("/profiles/{pid}", response_model=PxeProfileOut)
async def update_profile(pid: str, body: PxeProfileIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    p = await db.get(models.PxeProfile, pid)
    if not p:
        raise HTTPException(status_code=404, detail="模板不存在")
    p.name = body.name
    p.os_type = body.os_type
    p.os_version = body.os_version
    p.timezone = body.timezone
    p.locale = body.locale
    p.keyboard = body.keyboard
    p.admin_user = body.admin_user
    if body.admin_password is not None:
        p.admin_password_enc = crypto.encrypt(body.admin_password)
    if body.root_password is not None:
        p.root_password_enc = crypto.encrypt(body.root_password)
    p.ssh_keys = body.ssh_keys
    p.disk_scheme = body.disk_scheme
    p.disk_config = body.disk_config
    p.net_mode = body.net_mode
    p.net_config = body.net_config
    p.mirror = body.mirror
    p.extra_packages = body.extra_packages
    p.post_script = body.post_script
    p.remark = body.remark
    await db.commit()
    await db.refresh(p)
    return _profile_out(p)


@router.delete("/profiles/{pid}")
async def delete_profile(pid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    p = await db.get(models.PxeProfile, pid)
    if p:
        await db.delete(p)
        await db.commit()
    return {"ok": True}


# ---------- 装机记录 ----------
@router.get("/installs", response_model=list[PxeInstallOut])
async def list_installs(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.PxeInstall).order_by(models.PxeInstall.created_at.desc()))
    return list(res.scalars().all())


@router.post("/installs", response_model=PxeInstallOut)
async def create_install(body: PxeInstallIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    if not await db.get(models.PxeProfile, body.profile_id):
        raise HTTPException(status_code=404, detail="模板不存在")
    inst = models.PxeInstall(
        profile_id=body.profile_id, hostname=body.hostname,
        mac=body.mac, ip=body.ip, status="pending",
    )
    db.add(inst)
    await db.commit()
    await db.refresh(inst)
    return inst


@router.delete("/installs/{iid}")
async def delete_install(iid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    inst = await db.get(models.PxeInstall, iid)
    if inst:
        await db.delete(inst)
        await db.commit()
    return {"ok": True}


# ---------- 应答文件生成 ----------
async def _gen_pxe_files(pid: str, body: dict, db: AsyncSession) -> dict:
    """生成全部 PXE 部署文件 (autoinstall/kickstart + iPXE + dnsmasq)。"""
    p = await db.get(models.PxeProfile, pid)
    if not p:
        raise HTTPException(status_code=404, detail="模板不存在")
    body = body or {}
    cfg = _to_pxeconfig(
        p,
        server_ip=body.get("server_ip", ""),
        http_root=body.get("http_root", ""),
        kernel_path=body.get("kernel_path", ""),
        initrd_path=body.get("initrd_path", ""),
        squashfs_path=body.get("squashfs_path", ""),
        deploy_mode=body.get("deploy_mode", "standalone"),
    )
    cfg.hostname = body.get("hostname", "default")
    installs = list(body.get("installs", []))
    return generate_all(cfg, installs)


@router.post("/profiles/{pid}/generate", response_model=PxeGenerateResult)
async def generate_files(pid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    files = await _gen_pxe_files(pid, body, db)
    return {"files": files}


@router.post("/profiles/{pid}/download")
async def download_files(pid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """下载全部 PXE 部署文件 (zip 压缩包)。"""
    files = await _gen_pxe_files(pid, body, db)
    return files_to_zip_response(files, "pxe-deploy.zip")


# ---------- 本机部署与服务管控 ----------
@router.get("/server/status")
async def server_status(_user=Depends(get_current_user)):
    """查看本机 PXE 服务状态 (dnsmasq + TFTP + HTTP 文件 + 端口)。"""
    return pxe_server.server_status()


@router.post("/server/service")
async def service_control(body: dict = None, _user=Depends(get_current_user)):
    """控制 dnsmasq 服务: start/stop/restart/reload/status。"""
    action = (body or {}).get("action", "status")
    return pxe_server.service_control(action)


@router.post("/profiles/{pid}/deploy")
async def deploy_to_host(pid: str, body: dict = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """一键部署到本机: 生成配置 -> 落地文件 -> 重启 dnsmasq。"""
    body = body or {}
    if not body.get("server_ip"):
        body["server_ip"] = _local_ip()
    body["http_root"] = "http://" + body["server_ip"] + ":8000/pxe/serve"
    files = await _gen_pxe_files(pid, body, db)
    return pxe_server.deploy_files(files, pid)
