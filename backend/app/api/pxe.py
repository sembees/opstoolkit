"""PXE 装机接口。"""
from __future__ import annotations

import os
import socket
from urllib.parse import quote, unquote
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models
from app.core.auth import get_current_user
from app.core.schemas import PxeGenerateIn, PxeGenerateResult, PxeInstallIn, PxeInstallOut, PxeProfileIn, PxeProfileOut
from app.database import get_db
from app.core.ziputil import files_to_zip_response
from app.it.pxe import server as pxe_server
from app.it.pxe.generator import DEFAULT_KERNEL_CONSOLE, PxeConfig, generate_all, pick_iso

router = APIRouter()


def _local_ip():
    """Detect primary IPv4 without external connectivity."""
    try:
        _, _, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            if not ip.startswith("127."):
                return ip
    except Exception:
        pass
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


def _default_media(p):
    """根据 OS 类型/版本计算默认的 kernel/initrd/squashfs 路径。"""
    ost = (p.os_type or "ubuntu").strip()
    ver = (p.os_version or "22.04").strip()
    base = ost + "/" + ver + "/"
    if ost == "rhel":
        return base + "vmlinuz", base + "initrd.img", ""
    return base + "vmlinuz", base + "initrd", base + "installer.squashfs"


def _iso_url_for(p, server_ip, iso_url=""):
    """定出本次装机用的可挂载 ISO 介质 URL。

    显式指定的 iso_url 优先；否则按 os_type/os_version 在 /srv/opstk/iso 里自动匹配
    （正式环境同一目录会放多个系统镜像，必须挑准，见 generator.pick_iso）。
    匹配不到返回 ""，由 generator 抛出可读的 4xx 提示，而不是生成一份必然失败的菜单。
    注意 ISO 由 app/main.py 挂在 /pxe/iso，与应答文件的 /pxe/serve 是两个不同的前缀。
    """
    if iso_url:
        return iso_url
    name = pick_iso(pxe_server.iso_names(), p.os_type, p.os_version)
    if not name:
        return ""
    # 文件名可能含空格 / 非 ASCII / `#` / `?`。不编码的话，空格会让 iPXE 的 kernel 行
    # 在该处**断开参数**（url= 只剩前半截，后半截变成多余内核参数），`#`/`?` 会被
    # 当成 URL 片段/查询串 —— 结果都是 casper 取不到介质。
    # 这里按路径段百分号编码；文件名本身不含 `/`，所以 safe="" 是安全的。
    return ("http://" + (server_ip or "192.168.1.100") + ":8000/pxe/iso/"
            + quote(name, safe=""))


def _iso_size_mb(iso_url):
    """把 iso_url 末段当文件名，到 /srv/opstk/iso 下 stat 出 MB；取不到返回 0。

    只用于在生成的 README 里给出"目标机内存要多大"的具体数字（casper 走 HTTP 时
    会把整份 ISO 读进内存）。名字虽然来自我们自己的拼接，仍做一次 basename 与
    路径穿越防护，避免被拿来 stat 任意路径。
    """
    if not iso_url:
        return 0
    # URL 里的文件名是百分号编码过的（见 _iso_url_for），stat 前必须反解回来。
    name = unquote(str(iso_url).rstrip("/").rsplit("/", 1)[-1])
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return 0
    try:
        return int(os.stat(os.path.join("/srv/opstk/iso", name)).st_size // 1048576)
    except OSError:
        return 0


def _to_pxeconfig(p: models.PxeProfile, server_ip="", http_root="",
                  kernel_path="", initrd_path="", squashfs_path="",
                  deploy_mode="standalone", iso_url="", kernel_console="") -> PxeConfig:
    _dk, _di, _ds = _default_media(p)
    _iso = _iso_url_for(p, server_ip, iso_url)
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
        # H5: 静态服务实际挂载在 :8000/pxe/serve（见 app/main.py），兜底路径必须带 /serve
        http_root=http_root or ("http://" + (server_ip or "192.168.1.100") + ":8000/pxe/serve"),
        kernel_path=kernel_path or _dk, initrd_path=initrd_path or _di, squashfs_path=squashfs_path or _ds,
        # Ubuntu 必须给出可挂载介质（casper 的 url=），否则必失败：
        # 显式 iso_url 优先，其次按 os_type/os_version 自动匹配 /srv/opstk/iso
        iso_url=_iso,
        iso_size_mb=_iso_size_mb(_iso),
        # 留空则用后端默认值（带串口，便于无显示器机器的装机排障）
        kernel_console=kernel_console or DEFAULT_KERNEL_CONSOLE,
        deploy_mode=deploy_mode,
    )


def _require_admin_password(body: PxeProfileIn) -> str:
    """admin_password 必填非空：这是裸机 root/管理员口令，不允许留空，也不代填任何默认口令。

    创建时必填；更新时 None 表示不修改原口令（放行），显式传空串一律拒绝。
    """
    pw = body.admin_password
    if not pw:
        raise HTTPException(
            status_code=422,
            detail="管理员密码不能为空；这是裸机 root 口令，不允许留空",
        )
    return pw


# ---------- 模板 CRUD ----------
@router.get("/profiles", response_model=list[PxeProfileOut])
async def list_profiles(db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    res = await db.execute(select(models.PxeProfile).order_by(models.PxeProfile.created_at.desc()))
    return [_profile_out(p) for p in res.scalars().all()]


@router.post("/profiles", response_model=PxeProfileOut)
async def create_profile(body: PxeProfileIn, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    _require_admin_password(body)
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
        # 更新时显式传空串同样拒绝（None 表示不修改原口令，保持放行）
        _require_admin_password(body)
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
def _gen_body_dict(body: PxeGenerateIn | None) -> dict:
    """PxeGenerateIn → 与旧版 dict 载荷逐键等价的摊平 dict。

    D7 第 2 层：HTTP 层的输入校验由 PxeGenerateIn（U-C 交付）完成，
    非法载荷在进入本函数之前就已被 FastAPI 以 422 拒绝；这里只做
    模型 → dict 的摊平，供 _gen_pxe_files 按原有 body.get(...) 语义消费。
    exclude_none 保证"未填写"的可选键保持缺键状态（而不是出现 None 值），
    从而维持 _gen_pxe_files / deploy_to_host 中所有回退与合并语义不变：
    否则一个全 None 的 net_config 会把模板或 detect_network() 探测到的
    网络值覆盖成 None。
    """
    if body is None:
        return {}
    return body.model_dump(exclude_none=True)


async def _gen_pxe_files(pid: str, body: dict, db: AsyncSession) -> dict:
    """生成全部 PXE 部署文件 (autoinstall/kickstart + iPXE + dnsmasq)。

    入参 body 约定来自 `PxeGenerateIn.model_dump(exclude_none=True)`
    （见 _gen_body_dict）：模型校验在 HTTP 层完成，这里只消费摊平后的字段。
    """
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
        iso_url=body.get("iso_url", ""),
        kernel_console=body.get("kernel_console", ""),
        deploy_mode=body.get("deploy_mode", "standalone"),
    )
    cfg.hostname = body.get("hostname", "default")
    # 部署时自动检测的网络配置覆盖
    if body.get("net_config"):
        merged = dict(cfg.net_config or {})
        merged.update(body["net_config"])
        cfg.net_config = merged
    installs = list(body.get("installs", []))
    try:
        return generate_all(cfg, installs)
    except ValueError as e:
        # 存量空口令模板等生成期校验失败：以 4xx + 中文提示暴露，而不是 500
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/profiles/{pid}/generate", response_model=PxeGenerateResult)
async def generate_files(pid: str, body: PxeGenerateIn = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    files = await _gen_pxe_files(pid, _gen_body_dict(body), db)
    return {"files": files}


@router.post("/profiles/{pid}/download")
async def download_files(pid: str, body: PxeGenerateIn = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """下载全部 PXE 部署文件 (zip 压缩包)。"""
    files = await _gen_pxe_files(pid, _gen_body_dict(body), db)
    return files_to_zip_response(files, "pxe-deploy.zip")


# ---------- 本机部署与服务管控 ----------
@router.get("/server/status")
async def server_status(_user=Depends(get_current_user)):
    """查看本机 PXE 服务状态 (dnsmasq + TFTP + HTTP 文件 + 端口)。"""
    return pxe_server.server_status()


@router.post("/server/service")
async def service_control(body: dict = None, _user=Depends(get_current_user)):
    """POST /api/it/pxe/server/service — 控制 dnsmasq: start/stop/restart/reload/status。"""
    action = (body or {}).get("action", "status")
    return pxe_server.service_control(action)


@router.post("/profiles/{pid}/deploy")
async def deploy_to_host(pid: str, body: PxeGenerateIn = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """POST /api/it/pxe/profiles/{pid}/deploy — 一键部署到本机：生成配置→落地文件→重启 dnsmasq。"""
    body = body or PxeGenerateIn()
    net = pxe_server.detect_network()

    # 1. 调用方显式给出的 server_ip —— 最高优先级
    # 2. 否则用 detect_network()["server_ip"]
    # 3. 否则回退 _local_ip()
    if body.server_ip:
        pass
    elif net and net.get("server_ip"):
        body.server_ip = net["server_ip"]
    else:
        body.server_ip = _local_ip()

    # 解析到环回地址说明无法确定对外 IP，必须报错而不是生成不可用的配置
    ip = body.server_ip
    if ip.startswith("127.") or ip == "::1":
        raise HTTPException(
            status_code=400,
            detail="无法确定本机对外 IP：解析到环回地址 " + ip
            + "。请在前端显式填写 server_ip，或修复主机名解析 /etc/hosts",
        )

    # http_root 强制为本机静态服务地址（app/main.py 挂载在 /pxe/serve，见 H5）
    body.http_root = "http://" + body.server_ip + ":8000/pxe/serve"

    payload = body.model_dump(exclude_none=True)
    # 合并 net_config：以 detect_network() 为底，调用方显式传入的键覆盖它。
    # 注意必须在 dict 层合并：detect_network() 的结果含 server_ip、warnings
    # 等模型字段之外的键，须原样保留并传给生成器，不能经过模型二次过滤。
    caller_net_config = payload.get("net_config") or {}
    if net:
        merged = dict(net)
        merged.update(caller_net_config)
        payload["net_config"] = merged
    files = await _gen_pxe_files(pid, payload, db)
    return pxe_server.deploy_files(files, pid)


# ---------- ISO ?? ----------
@router.get("/iso/list")
async def list_isos(_user=Depends(get_current_user)):
    """GET /api/it/pxe/iso/list — 列出 /srv/opstk/iso 中的 ISO 文件。"""
    return pxe_server.list_isos()


@router.post("/iso/{iso_name}/extract")
async def extract_iso(iso_name: str, body: dict = None, _user=Depends(get_current_user)):
    """从 ISO 提取 PXE 引导文件 (vmlinuz/initrd/squashfs)。"""
    body = body or {}
    return pxe_server.extract_from_iso(
        iso_name,
        os_type=body.get("os_type", "ubuntu"),
        os_version=body.get("os_version", "22.04"),
    )


@router.delete("/iso/{iso_name}")
async def delete_iso(iso_name: str, _user=Depends(get_current_user)):
    """删除 ISO 文件。"""
    return pxe_server.delete_iso(iso_name)
