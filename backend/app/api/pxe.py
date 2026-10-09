"""PXE 装机接口。"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
from urllib.parse import quote, unquote
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import crypto, models, serve_token
from app.core.auth import get_current_user, require_role
from app.core.schemas import PxeGenerateIn, PxeGenerateResult, PxeInstallIn, PxeInstallOut, PxeProfileIn, PxeProfileOut
from app.core.timeutil import utcnow
from app.database import get_db
from app.core.ziputil import files_to_zip_response
from app.it.pxe import server as pxe_server
from app.it.pxe import transfers as iso_transfers
from app.it.pxe import os_catalog
from app.it.pxe.generator import (
    DEFAULT_KERNEL_CONSOLE,
    LVM_SIZE_WARNING,
    PxeConfig,
    appstream_warning,
    complete_appstream,
    generate_all,
    is_rhel_family,
    pick_iso,
)

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


def _flag(v, default: bool) -> bool:
    """布尔列兜底：SQLAlchemy 瞬态对象属性为 None（未落库/直构对象）时取默认值。"""
    return default if v is None else bool(v)


def _profile_out(p: models.PxeProfile) -> PxeProfileOut:
    return PxeProfileOut(
        id=p.id, name=p.name, os_type=p.os_type, os_version=p.os_version,
        timezone=p.timezone, locale=p.locale, keyboard=p.keyboard,
        admin_user=p.admin_user, ssh_keys=p.ssh_keys or [],
        # 装完能 SSH：两开关走 _flag 兜底 —— 测试/直构的 PxeProfile 对象可能没带新列
        allow_root=_flag(getattr(p, "allow_root", None), False),
        sudo_nopasswd=_flag(getattr(p, "sudo_nopasswd", None), True),
        disk_scheme=p.disk_scheme, disk_config=p.disk_config or {},
        net_mode=p.net_mode, net_config=p.net_config or {},
        mirror=p.mirror, extra_packages=p.extra_packages or [],
        post_script=p.post_script, remark=p.remark,
        server_ip="", http_root="", kernel_path="", initrd_path="", squashfs_path="",
        created_at=p.created_at,
    )


def _safe_decrypt(enc):
    """解密模板里的口令；**"解不开"要说清楚**（外部审查 U4-F6）。

    改前任何异常都返回 "" —— 于是一个"密钥变了/密文坏了"的模板会被报成
    "管理员密码不能为空"，运维按提示去填口令、填完还是同样的问题（因为存进去的密文
    根本不是这把 key 加的）。

    现在分两种情况：
      · 密文为空 = 真的没填（返回 ""，由下游照旧报"必填"）；
      · 密文非空却解不开 = 抛一句可操作的话，指名 credential_key。
    """
    if not enc:
        return ""
    try:
        return crypto.decrypt(enc)
    except Exception as e:  # noqa: BLE001
        raise ValueError(
            "模板里保存的口令无法解密（%s）：多半是 credential_key（凭证密钥）变了或丢了。"
            "请恢复原来的密钥（.env 里的 credential_key），或在这个模板里重新填写口令。"
            % type(e).__name__
        ) from e


def _default_media(p):
    """根据 OS 类型/版本计算默认的 kernel/initrd/squashfs 路径。

    媒体文件名按**目录条目**取（唯一定义点 os_catalog）：
      · kickstart 家族（rhel/centos/rocky/alma/almalinux/oraclelinux/openeuler/
        kylin/uos/anolis/fedora）与 rhel 完全一致，都是 `initrd.img`；
      · ubuntu 用 `initrd` + `installer.squashfs`；
      · debian 沿用 `initrd`（提取落盘名与目录条目一致）。
    曾经只特判 `ost == "rhel"`，于是 os_type 填 "rocky" 会走 anaconda 分支却拿到
    Ubuntu 风格的 `rocky/9/initrd`（ISO 里实际是 images/pxeboot/initrd.img）→ 必然 404。
    """
    ost = (p.os_type or "ubuntu").strip().lower()
    ver = (p.os_version or "22.04").strip()
    base = ost + "/" + ver + "/"
    entry = os_catalog.entry(ost)
    if entry is not None and entry.installer == os_catalog.KICKSTART:
        return base + "vmlinuz", base + "initrd.img", ""
    initrd_name = entry.dest_initrd if entry is not None else "initrd"
    squashfs = (base + entry.dest_squashfs) if (entry is not None and entry.dest_squashfs) else ""
    return base + "vmlinuz", base + initrd_name, squashfs


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


SERVE_MARK = "/pxe/serve/"
WEB_ROOT = "/srv/opstk/pxe-web"


def _local_served_path(url):
    """把本机发布的 URL 映射回真实目录（只认 /pxe/serve/ 前缀，防路径穿越）。

    ★ J：URL 里可能带 token 路径段（`/pxe/serve/<token>/…`）—— 先剥掉再映射，
    否则会去 `<web_root>/<token>/…` 找文件（必然找不到）。
    """
    if not url or SERVE_MARK not in url:
        return ""
    rel = serve_token.strip_for_local_path(str(url).split(SERVE_MARK, 1)[1].strip("/"))
    if not rel or ".." in rel:
        return ""
    return os.path.join(WEB_ROOT, rel)


def _serve_url(path, server_ip):
    rel = os.path.relpath(path, WEB_ROOT).replace(os.sep, "/")
    return serve_token.serve_base(server_ip) + "/" + rel + "/"


def _detect_rhel_media(mirror, server_ip):
    """从本机发布的目录树里自动推断 RHEL 系的 inst.repo / inst.stage2 / 额外仓库。

    为什么需要（都是真机串口实证）：把 DVD ISO 树挂出来当安装源时，结构是
      <root>/{BaseOS,AppStream}/repodata/... 以及 <root>/images/install.img
    只给 `inst.repo=<root>/BaseOS/` 会有两个坑：
      · 找不到 stage2 → dracut "Could not boot / /dev/root does not exist"；
      · 装完包后在认证步骤崩 → SecurityInstallationError: /usr/sbin/authconfig is missing
        （authconfig 在 AppStream，而 kickstart 用的是 auth --enableshadow）。
    所以这里探测出 stage2（往上找含 images/ 的那一层）与同级仓库（AppStream）。
    探不到（例如 mirror 指向远端官方镜像，那种镜像本身就是完整仓库树）就原样返回，不猜。
    """
    base = _local_served_path(mirror)
    if not base or not os.path.isdir(base):
        return mirror, "", []
    repo_url, base_dir = mirror, os.path.normpath(base)
    if not os.path.isfile(os.path.join(base_dir, "repodata", "repomd.xml")):
        # 给的是树根：取第一个含 repodata 的子目录当主仓库
        subs = [d for d in sorted(os.listdir(base_dir))
                if os.path.isfile(os.path.join(base_dir, d, "repodata", "repomd.xml"))]
        if not subs:
            return mirror, "", []
        base_dir = os.path.join(base_dir, subs[0])
        repo_url = _serve_url(base_dir, server_ip)
    stage2 = ""
    stage2_dir = ""
    cur = base_dir
    for _ in range(3):
        if os.path.isdir(os.path.join(cur, "images")):
            stage2_dir = cur
            stage2 = _serve_url(cur, server_ip)
            break
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    extra = []
    # ★ 额外仓库 = **stage2 那一层里的兄弟仓库**（2026-10-08 修正，取代原先"扫主仓库父目录"）。
    #   为什么按 stage2 定界：
    #     · RHEL/Rocky DVD：mirror 常填 `…/rocky-9.4/BaseOS/`，主仓库 = BaseOS，
    #       stage2 层 = `…/rocky-9.4/`（含 images/）⇒ 兄弟里认出 AppStream ✓
    #       （少了它会在 auth 步骤崩，见本函数 docstring；既有用例与生产 ks 都是这个行为）。
    #     · 单仓库 DVD（openEuler 24.03：`Packages/`+`repodata/` 直接躺在树根）：
    #       主仓库**就是** stage2 层 ⇒ 没有"兄弟仓库"可谈。若照"主仓库父目录"去扫，
    #       扫到的是**别的发布介质树**（我们发布过 centos-7、rocky-9.4），于是 ks 多出
    #       `repo --name="centos-7"`、iPXE 多出 `inst.addrepo=centos-7,<url>`，
    #       真机后果实测：**openEuler 24.03 根本不取 kickstart**（安装器退回交互式主菜单；
    #       手工去掉那条 addrepo 后立刻开始抓 ks）。
    #       证据：mimo/out/e2e-vm140-openeuler-console.log 与 RUNBOOK §5.83.44/45。
    if stage2_dir and os.path.normpath(stage2_dir) != os.path.normpath(base_dir) \
            and os.path.isdir(stage2_dir):
        for sib in sorted(os.listdir(stage2_dir)):
            sp = os.path.normpath(os.path.join(stage2_dir, sib))
            # 用 normpath 比较而不是字符串相等：rel 里带的是 '/'，os.path.join 在
            # Windows 下会给出 '\'，直接比会把主仓库自己也算成"额外仓库"。
            if sp == base_dir or not os.path.isdir(sp):
                continue
            if os.path.isfile(os.path.join(sp, "repodata", "repomd.xml")):
                extra.append({"name": sib, "url": _serve_url(sp, server_ip)})
    return repo_url, stage2, extra


def _to_pxeconfig(p: models.PxeProfile, server_ip="", http_root="",
                  kernel_path="", initrd_path="", squashfs_path="",
                  deploy_mode="standalone", iso_url="", kernel_console="",
                  stage2="", extra_repos=None, answer_root="") -> PxeConfig:
    _dk, _di, _ds = _default_media(p)
    _iso = _iso_url_for(p, server_ip, iso_url)
    _repo, _stage2, _extra = (p.mirror or ""), "", []
    if is_rhel_family(p.os_type):
        _repo, _stage2, _extra = _detect_rhel_media(p.mirror or "", server_ip)
    if stage2:
        _stage2 = stage2
    if extra_repos:
        _extra = extra_repos
    return PxeConfig(
        # 归一化：库里可能存在历史写入的大小写/空白差异，而分支判断与介质路径都依赖它
        os_type=(p.os_type or "ubuntu").strip().lower(), os_version=p.os_version,
        hostname="default", timezone=p.timezone, locale=p.locale, keyboard=p.keyboard,
        admin_user=p.admin_user,
        admin_password=_safe_decrypt(p.admin_password_enc),
        root_password=_safe_decrypt(p.root_password_enc),
        ssh_keys=p.ssh_keys or [],
        # 装完能 SSH：两开关进生成器（None 兜底语义同 _profile_out）
        allow_root=_flag(getattr(p, "allow_root", None), False),
        sudo_nopasswd=_flag(getattr(p, "sudo_nopasswd", None), True),
        disk_scheme=p.disk_scheme, disk_config=p.disk_config or {},
        net_mode=p.net_mode, net_config=p.net_config or {},
        mirror=_repo, stage2=_stage2, extra_repos=_extra,
        extra_packages=p.extra_packages or [],
        post_script=p.post_script,
        server_ip=server_ip or "192.168.1.100",
        # H5: 静态服务实际挂载在 :8000/pxe/serve（见 app/main.py），兜底路径必须带 /serve
        # J：启用 pxe_serve_token 时这一段是 /pxe/serve/<token>（所有由它拼出的 URL 都带上）
        http_root=http_root or serve_token.serve_base(server_ip or "192.168.1.100"),
        # E1: 只有**应答文件 / iPXE 菜单**可以按模板隔离（部署时指向 profiles/<pid>）；
        # 媒体必须继续走 http_root 的扁平路径。留空 = 与 http_root 相同。
        answer_root=answer_root,
        kernel_path=kernel_path or _dk, initrd_path=initrd_path or _di,
        squashfs_path=squashfs_path or _ds,
        # Ubuntu 必须给出可挂载介质（casper 的 url=），否则必失败：
        # 显式 iso_url 优先，其次按 os_type/os_version 自动匹配 /srv/opstk/iso
        iso_url=_iso,
        iso_size_mb=_iso_size_mb(_iso),
        # 留空则用后端默认值（带串口，便于无显示器机器的装机排障）
        kernel_console=kernel_console or DEFAULT_KERNEL_CONSOLE,
        # 32 位 UEFI(arch 6) 的 ipxe-i386.efi 在发行版包里不存在（§4-6）。按实际是否
        # 存在来生成：文件不在就不广播那条必然失败的引导项。见 generator.PxeConfig。
        ipxe_ia32_available=pxe_server.firmware_present().get("ipxe-i386.efi", False),
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
        allow_root=bool(body.allow_root), sudo_nopasswd=bool(body.sudo_nopasswd),
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
    p.allow_root = bool(body.allow_root)
    p.sudo_nopasswd = bool(body.sudo_nopasswd)
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
async def delete_profile(pid: str, db: AsyncSession = Depends(get_db), _user=Depends(require_role("admin"))):
    p = await db.get(models.PxeProfile, pid)
    if p:
        await db.delete(p)
        await db.commit()
        # 模板没了，它的「上次部署参数」也留不住（否则 finish/done 会拿着一个已删模板
        # 的参数去重部署 —— 现在还会被红线守卫兜住，但那些陈旧条目会一直堆在 state 里）
        _drop_last_deploy(pid)
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
    try:
        await db.commit()
    except IntegrityError:
        # 外部审查 U7-F9：同一模板下 MAC 唯一。不接住的话是一个 500，
        # 而运维真正需要知道的是"这台机器已经登记过了"（重复登记的后果是
        # 生成器按 MAC 展开的文件互相覆盖，只有一条生效）。
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"该模板下已经登记过 MAC {body.mac} 的装机记录："
                   f"同一台机器只能有一条（否则按 MAC 生成的菜单/应答文件会互相覆盖）；"
                   f"请先删除旧记录",
        )
    await db.refresh(inst)
    return inst


@router.delete("/installs/{iid}")
async def delete_install(iid: str, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    inst = await db.get(models.PxeInstall, iid)
    if inst:
        await db.delete(inst)
        await db.commit()
    return {"ok": True}


# ---------- 装机完成标记（防重复抹盘，2026-10-08 真机实证）----------
# 背景：装机记录表早就有 status/finished_at 两个字段，但整个后端没有任何地方把
# status 置为完成 —— 记录永远 pending，而生成器对所有已登记 MAC 都下发「按 MAC 的
# 第二阶段自动装机菜单」。于是装完的机器重启时 BIOS 先走网卡 → 又被自动装一遍
# （真机 VM140 实测只能靠人工把引导顺序改成磁盘优先才停下来）。
# 现在的闭环：应答文件里带装完回调（done_url）→ 装完回调把记录标记 installed →
# 生成器按 status 决定不再给这台机器下发装机菜单 → 网卡引导落到「未登记默认菜单」
# （拒绝自动安装并 exit）→ 固件按引导顺序继续引导本地磁盘。

def _install_done_token(install_id: str) -> str:
    """done 回调令牌：HMAC-SHA256(secret_key, id) 的前 32 位 hex。

    · 同一个 id 恒定（幂等重试、生成与回调分处两台生命周期都不受影响）；
    · **不落库**（表上一个列都不加 —— create_all 不会给已存在的表补列，动了就炸）；
    · 换 secret_key 后旧 URL 自然失效，重新部署一次即刷新。
    """
    key = (crypto.ensure_secret_key() or "opstk").encode("utf-8")
    msg = ("pxe-install-done:" + str(install_id)).encode("utf-8")
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def install_done_url(server_ip: str, install_id: str) -> str:
    """装完回调 URL（拼进该机应答文件，安装器收尾时 curl 一下）。"""
    return ("http://" + str(server_ip) + ":8000/api/it/pxe/installs/"
            + str(install_id) + "/done?t=" + _install_done_token(install_id))


async def _set_install_status(iid: str, db: AsyncSession, status: str) -> dict:
    """finish / done / reset 共用：把记录置成目标状态（幂等）→ 尽力自动重部署。

    **先提交、后重部署**，且重部署的任何异常都只进返回体的 redeploy 字段 ——
    「状态已经改了」这件事绝不因为重部署失败而回滚。
    finished_at 只在 installed 时写；退回 pending 时清空（否则界面上会显示
    「待装机 + 完成时间」这种自相矛盾的行）。
    """
    inst = await db.get(models.PxeInstall, iid)
    if not inst:
        raise HTTPException(status_code=404, detail="装机记录不存在")
    if (inst.status or "") != status:
        inst.status = status
        inst.finished_at = utcnow() if status == "installed" else None
        await db.commit()
    return {"ok": True, "id": inst.id, "status": inst.status,
            "redeploy": await _auto_redeploy(inst.profile_id, db)}


async def _finish_install(iid: str, db: AsyncSession) -> dict:
    """标记完成（= 置 installed）。"""
    return await _set_install_status(iid, db, "installed")


@router.post("/installs/{iid}/finish")
async def install_finish(iid: str, db: AsyncSession = Depends(get_db), _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/installs/{iid}/finish — 人工把装机记录标记完成（admin）。"""
    return await _finish_install(iid, db)


@router.post("/installs/{iid}/reset")
async def install_reset(iid: str, db: AsyncSession = Depends(get_db), _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/installs/{iid}/reset — 把已装完的记录退回「待装机」（admin）。

    为什么必须有这条路：标记完成之后，运维若要把这台机器**重装一遍**（换盘、重做、
    交付给别的用途），没有它就只能删记录再重建。reset 会自动重部署一次，
    生成器于是重新给该 MAC 下发自动装机菜单（红线守卫照旧生效）。
    """
    return await _set_install_status(iid, db, "pending")


@router.api_route("/installs/{iid}/done", methods=["POST", "GET"])
async def install_done(iid: str, t: str = "", db: AsyncSession = Depends(get_db)):
    """POST/GET /api/it/pxe/installs/{iid}/done — 装完回调（目标机调，无登录态）。

    不鉴权，但必须带对令牌：?t= 必须等于 _install_done_token(iid)，
    没带 / 带错一律 403（令牌在生成时应答文件里，目标机之外猜不到）。

    ★ 为什么同时收 GET：这条命令是**生成器拼进应答文件**的，而路由在另一处代码里 ——
    实测（2026-10-08 真机 VM140）第一版生成的是不带 `-X POST` 的 curl（等价 GET），
    而这里只收 POST ⇒ 回调静默 404、记录永远 pending、机器被反复重装，
    而我自己的单测只断言了"URL 出现在 ks 里"，根本抓不到。
    允许 GET 之后，工件与路由再漂移也不会沉默失效（生成侧仍按 POST 发）。
    """
    if not t or t != _install_done_token(iid):
        raise HTTPException(status_code=403, detail="done 回调令牌无效：?t= 缺失或不匹配")
    return await _finish_install(iid, db)


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


async def _gen_pxe_files(pid: str, body: dict, db: AsyncSession,
                         out_warnings: list | None = None) -> dict:
    """生成全部 PXE 部署文件 (autoinstall/kickstart + iPXE + dnsmasq)。

    入参 body 约定来自 `PxeGenerateIn.model_dump(exclude_none=True)`
    （见 _gen_body_dict）：模型校验在 HTTP 层完成，这里只消费摊平后的字段。
    out_warnings：给了就把生成期的非致命警告（目前只有"服务端绑卡没确定"）追加进去 ——
    部署路径不需要（它有 fail-closed 守卫），/generate 与 /download 需要。
    """
    p = await db.get(models.PxeProfile, pid)
    if not p:
        raise HTTPException(status_code=404, detail="模板不存在")
    body = body or {}
    # `_to_pxeconfig` 里的 `_safe_decrypt` 会在"密文解不开"时抛 ValueError（U4-F6），
    # 所以它也必须落在同一个 422 边界内 —— 否则运维看到的是 500。
    try:
        cfg = _to_pxeconfig(
            p,
            server_ip=body.get("server_ip", ""),
            http_root=body.get("http_root", ""),
            kernel_path=body.get("kernel_path", ""),
            initrd_path=body.get("initrd_path", ""),
            squashfs_path=body.get("squashfs_path", ""),
            iso_url=body.get("iso_url", ""),
            kernel_console=body.get("kernel_console", ""),
            stage2=body.get("stage2", ""),
            extra_repos=body.get("extra_repos", []),
            deploy_mode=body.get("deploy_mode", "standalone"),
            # 只有 /deploy 会填它（本机部署时的 profiles/<pid> 前缀）；
            # /generate 与 /download 不填 → 输出与改造前逐字一致（向后兼容）。
            answer_root=body.get("answer_root", ""),
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    cfg.hostname = body.get("hostname", "default")
    # 部署时自动检测的网络配置覆盖
    if body.get("net_config"):
        merged = dict(cfg.net_config or {})
        merged.update(body["net_config"])
        cfg.net_config = merged
    # ★ 红线修复（2026-10-08 实测）：dnsmasq 的 interface= 必须绑到「持有 server_ip 的
    #   那张网卡」，而不是模板的 net_config.interface —— 后者是**被装机器**的网卡名
    #   （界面标签「网卡名」，占位 ens33）。实测模板 interface=ens18（客户端网卡名，
    #   正确）+ server_ip=192.168.199.1（隔离装机网）时，生成出来的 dnsmasq.conf 是
    #   `interface=ens18`，而本机 ens18=10.128.118.113 是企业网网卡：一部署就会在
    #   10.128.118.0/24 上开 DHCP。这里按 server_ip 反查真实网卡写进 server_interface，
    #   客户端那份 `network --device=` 仍然用它自己的网卡名，两者互不影响。
    #   所有模式都要写：relay 虽然不发 DHCP，但生成器对 relay 同样输出 interface= +
    #   enable-tftp（它要提供 TFTP/HTTP 文件），绑错网卡一样服务不到装机网。
    #   取不到网卡时不写，由部署侧 fail-closed 守卫兜底。
    if body.get("server_ip"):
        _bind = pxe_server.serve_binding(body.get("server_ip"))
        if _bind.get("interface"):
            _nc_bound = dict(cfg.net_config or {})
            _nc_bound["server_interface"] = _bind["interface"]
            cfg.net_config = _nc_bound
    # ★ 生成侧不硬拒，但必须把「绑卡没确定」显式交付出去（部署侧有守卫，离线产物没有）：
    #   写进 README + 响应 warnings（/download 由调用方放进 ZIP 的 WARNINGS-*.txt）。
    _bind_warn = _serve_binding_warning(
        body.get("server_ip"), (cfg.net_config or {}).get("interface"),
        _bind if body.get("server_ip") else {}, pxe_server.is_linux())
    if _bind_warn:
        cfg.warn_serve_binding = _bind_warn
        if out_warnings is not None:
            out_warnings.append(_bind_warn)
    # ★ 方案 B（2026-10-09，RUNBOOK §5.83.62）：RHEL 系缺 AppStream 时，**确认兄弟仓库存在**才自动补。
    #   为什么必须"确认"：给一个不存在的 repo 可能让装机硬失败 —— 宁可只警示（A），不可猜。
    #   探测顺序：① 本地已发布路径直接 stat（快、无网络）；② 其余（远端镜像）短超时 HTTP 探测 repomd.xml。
    #   探测失败/超时一律降级为"不补"（保留警示），探测本身不得成为新的失败面。
    def _appstream_exists(url: str) -> bool:
        _lp = ""
        try:
            _lp = _local_served_path(url) or ""
        except Exception:
            _lp = ""
        # ★ 只有**真的存在 repodata** 才算本地命中；否则继续走 HTTP ——
        #   2026-10-09 实测教训：`_local_served_path` 对"非本站发布前缀"的 URL 也会给出一个映射，
        #   若就此返回 False，远端镜像那条分支（方案 B 的核心场景）就永远不会被走到。
        if _lp and os.path.isdir(os.path.join(_lp, "repodata")):
            return True
        _u = str(url or "").rstrip("/") + "/repodata/repomd.xml"
        try:
            import urllib.request as _ureq
            _req = _ureq.Request(_u, method="HEAD")
            with _ureq.urlopen(_req, timeout=3) as _r:
                return 200 <= int(getattr(_r, "status", 0) or 0) < 400
        except Exception:
            return False

    _new_extra, _added_as = complete_appstream(
        body.get("os_type") or getattr(p, "os_type", None),
        cfg.mirror, cfg.extra_repos, _appstream_exists)
    if _added_as:
        cfg.extra_repos = _new_extra

    # ★ 2026-10-09（RUNBOOK §5.83.60）：RHEL 系若生效仓库集合里没有 AppStream，
    #   `wget` 与 `vim`（唯一 provider vim-enhanced）会被 `%packages --ignoremissing`
    #   **静默跳过**（不报错、只是少装）⇒ 与绑卡警告同样口径：生成侧不硬拒，但必须显式交付出去。
    #   注意：必须在方案 B 的自动补齐**之后**再判断，否则刚补上的情况也会被误报。
    _as_warn = appstream_warning(
        body.get("os_type") or getattr(p, "os_type", None),
        cfg.mirror, cfg.extra_repos)
    if _as_warn and out_warnings is not None:
        out_warnings.append(_as_warn)
    installs = list(body.get("installs", []))
    # ★ 装完防重复抹盘（2026-10-08 真机实证）：装机记录的 id / status 以**库里**为准。
    # 为什么显式传入 installs 也必须过这一步 —— 前端的 PxeInstallItem 只有
    # mac/hostname/ip 三个字段（D7 模型），界面路径传什么都不能绕过库里的状态：
    # status=installed 的记录要被盖上 installed，pending 的补装完回调 done_url
    # （装完回调用它把记录标记 installed，之后 dnsmasq 不再给这台机器下发装机菜单）。
    # MAC 归一化与生成器 _safe_mac 同口径（小写冒号形式）。
    rows = (await db.execute(
        select(models.PxeInstall).where(models.PxeInstall.profile_id == pid)
    )).scalars().all()
    by_mac = {str(r.mac or "").strip().lower(): r for r in rows}
    # done_url 的主机名用请求里的 server_ip；没给时退到生成器的默认服务地址。
    # （deploy 路径恒有 server_ip；/generate 预览时可能落到占位地址，无碍。）
    _srv_ip = body.get("server_ip") or cfg.server_ip

    def _with_done(item: dict, r) -> dict:
        item["id"] = r.id
        item["status"] = r.status or "pending"
        if item["status"] == "pending":
            item["done_url"] = install_done_url(_srv_ip, r.id)
        return item

    # 回落：调用方不传 installs 时，取**该模板在 DB 里的装机记录**。
    # 为什么不回落不行 —— 实测（RUNBOOK-STATE §5.29）：给模板登记了 3 台 MAC，但只要
    # deploy body 不带 installs，就完全走不到按 MAC 隔离那条分支：
    # `profiles/<pid>/boot/` 根本不生成、dnsmasq 里也没有 per-MAC 的 dhcp-boot，
    # 于是"未登记的机器会被按最后一次部署的模板装机"这个洞在实际使用中一直敞着
    # （界面部署按钮固定发 installs: []，所以界面上登记的装机条目对部署毫无影响）。
    # 优先级：显式传入 > DB 记录 > 维持旧行为（模板菜单，向后兼容）。
    if not installs:
        installs = []
        for r in rows:
            installs.append(_with_done(
                {"mac": r.mac, "hostname": r.hostname,
                 "ip": getattr(r, "ip", None) or getattr(r, "mgmt_ip", None)}, r))
    else:
        for item in installs:
            if not isinstance(item, dict):
                continue
            r = by_mac.get(str(item.get("mac") or "").strip().lower())
            if r is None:
                continue
            _with_done(item, r)
    try:
        return generate_all(cfg, installs)
    except ValueError as e:
        # 存量空口令模板等生成期校验失败：以 4xx + 中文提示暴露，而不是 500
        raise HTTPException(status_code=422, detail=str(e)) from e


@router.post("/profiles/{pid}/generate", response_model=PxeGenerateResult)
async def generate_files(pid: str, body: PxeGenerateIn = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    warns: list = []
    files = await _gen_pxe_files(pid, _gen_body_dict(body), db, out_warnings=warns)
    return {"files": files, "warnings": warns}


@router.post("/profiles/{pid}/download")
async def download_files(pid: str, body: PxeGenerateIn = None, db: AsyncSession = Depends(get_db), _user=Depends(get_current_user)):
    """下载全部 PXE 部署文件 (zip 压缩包)。"""
    warns: list = []
    files = await _gen_pxe_files(pid, _gen_body_dict(body), db, out_warnings=warns)
    # 警告也要跟着 ZIP 走：拿到包的人可能不经过界面，只在 README 里印一遍不够稳
    for _i, _w in enumerate(warns, 1):
        files["WARNINGS-%d.txt" % _i] = _w + "\n"
    return files_to_zip_response(files, "pxe-deploy.zip")


# ---------- 本机部署与服务管控 ----------
@router.get("/server/status")
async def server_status(_user=Depends(get_current_user)):
    """查看本机 PXE 服务状态 (dnsmasq + TFTP + HTTP 文件 + 端口)。"""
    out = pxe_server.server_status()
    # ★ J：把"/pxe/serve 有没有开 token 门禁"如实报出来（只报状态，不返回 token 本身）——
    #   否则运维会以为它在保护，而实际上 .env 里那行是空的。
    out["serve_token"] = serve_token.enforcement_state()
    return out


@router.post("/server/service")
async def service_control(body: dict = None, _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/server/service — 控制 dnsmasq: start/stop/restart/reload/status。"""
    action = (body or {}).get("action", "status")
    return pxe_server.service_control(action)


def _deploy_redline_check(ip: str, mode: str, net_config: dict, bind: dict,
                          auto_ip: bool = False) -> str | None:
    """本机部署前的红线判定：返回**拒绝原因**（中文），通过则 None。

    抽成纯函数是为了能被单测直接覆盖（路由层只剩「非 None 就 422」）。
    三条判据，任何一条不满足都拒绝 —— 本机部署会把配置交给**宿主 root dnsmasq**
    加载，而 standalone 模式会在 interface= 那张网卡上直接分配地址：

    1. server_ip 所在网卡必须查得到（查不到就只能回落模板里的**客户端**网卡名，
       本项目里那个名字正是企业网卡 ens18）；
    2. standalone **不接受自动探测出来的 server_ip**：探测值取的是默认路由所在网卡
       （本项目实测 = 企业网 10.128.118.113），在它上面开 DHCP 就是把地址发到
       管理/生产网 —— 而「池 ⊆ 该网卡网段」这种自洽性检查**拦不住**它（探测出来的
       pool 本来就落在同一张网卡网段里，两条判据同时为真）。
       所以 standalone 必须由运维**显式**给出隔离装机网的 server_ip；proxy/relay
       不发地址，自动探测照旧允许。
    3. DHCP 池必须整体落在该网卡网段内（池与网段不匹配时可能把地址发到别的网段）。
    """
    if not bind or not bind.get("interface"):
        return ("无法确定 server_ip " + str(ip) + " 所在网卡，拒绝部署：dnsmasq 的 "
                "interface= 必须绑到持有该地址的本机网卡上，否则可能在别的网段"
                "（例如企业网）上开 DHCP。请确认该地址已配置在本机某张网卡上，"
                "或在部署参数里显式给出正确的 server_ip。")
    if mode != "standalone":
        return None
    if auto_ip:
        return ("拒绝部署：standalone（独立 DHCP）会在 " + str(bind.get("interface"))
        + " 上直接分配地址，而本次 server_ip 是**自动探测**出来的 " + str(ip)
        + "（默认路由所在网卡）—— 那通常是管理/生产网口，在上面开 DHCP 会把地址"
          "发到该网段。请显式填写隔离装机网的 server_ip（例如模板里 PXE 网卡的地址）"
          "后再部署；若只想提供引导信息、不分配地址，请改用 proxy 模式。")
    _nc = net_config or {}
    _start = _nc.get("dhcp_start", "192.168.1.100")
    _end = _nc.get("dhcp_end", "192.168.1.200")
    if not pxe_server.range_inside_binding(_start, _end, bind):
        return ("拒绝部署：DHCP 池 " + str(_start) + "-" + str(_end)
                + " 不在 server_ip " + str(ip) + " 所在网卡 " + str(bind.get("interface"))
                + " 的网段内 —— standalone 模式会在该网卡上直接分配地址，池与网段"
                  "不匹配时可能把地址发到别的网段（例如企业网）。请把模板 net_config "
                  "的 dhcp_start/dhcp_end 改成该网卡网段内的地址。")
    return None


# ---------- 上次部署参数（finish/done 自动重部署用；best-effort，绝不影响部署）----------
# 存放位置与 dnsmasq 重载握手状态同一个目录（compose 已挂载 /srv/opstk/state）。
# 有意**不**替它建目录：目录不在 = 部署形态没挂这个盘 ⇒ 记不了就记不了，只记日志；
# 绝不为一个 best-effort 缓存在文件系统根上随手 mkdir。

def _last_deploy_path() -> str:
    from app.core import dhcp as _dhcp_mod
    return _dhcp_mod.HOST_RELOAD_STATE_DIR + "/last-deploy.json"


def _read_last_deploy() -> dict:
    try:
        with open(_last_deploy_path(), encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _drop_last_deploy(pid: str) -> bool:
    """删掉某个模板的「上次部署参数」条目（模板删除、或发现模板已不存在时调用）。

    返回是否真的删掉了。写失败只记日志 —— 它只是缓存，绝不能让删模板这种操作失败。
    """
    try:
        data = _read_last_deploy()
        if str(pid) not in data:
            return False
        data.pop(str(pid), None)
        tmp = _last_deploy_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, _last_deploy_path())
        return True
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning("清理上次部署参数条目失败（不影响主流程）：%s", e)
        return False


def _save_last_deploy(pid: str, server_ip: str, deploy_mode: str) -> None:
    """把本次部署参数读-改-写进 last-deploy.json（形如 {"<pid>": {"server_ip": …, "deploy_mode": …}}）。

    写失败只记日志 —— 它只是 finish/done 自动重部署的依据，绝不能让部署本身失败。
    """
    try:
        data = _read_last_deploy()
        data[str(pid)] = {"server_ip": str(server_ip), "deploy_mode": str(deploy_mode)}
        tmp = _last_deploy_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp, _last_deploy_path())
    except Exception as e:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "记录上次部署参数到 %s 失败（不影响本次部署）：%s", _last_deploy_path(), e)


async def _generate_and_deploy(pid: str, server_ip: str, deploy_mode: str,
                               db: AsyncSession, auto_ip: bool = False,
                               payload: dict | None = None) -> dict:
    """deploy_to_host 的核心：server_ip 已定 → 红线守卫 → 生成 → 落地 → ok 判定。

    deploy_to_host 与 finish/done 的自动重部署都走这里（deploy_to_host 传入请求体
    摊平的 payload 保持原语义；重部署只带 last-deploy.json 里的两个参数）。
    红线守卫（_deploy_redline_check + serve_binding 那段）判据与文案**一字未动**。
    """
    ip = str(server_ip or "")
    # 解析到环回地址说明无法确定对外 IP，必须报错而不是生成不可用的配置
    if ip.startswith("127.") or ip == "::1":
        raise HTTPException(
            status_code=400,
            detail="无法确定本机对外 IP：解析到环回地址 " + ip
            + "。请在前端显式填写 server_ip，或修复主机名解析 /etc/hosts",
        )

    # http_root 强制为本机静态服务地址（app/main.py 挂载在 /pxe/serve，见 H5）。
    # **保持扁平、不要指向 profiles/<pid>**：http_root 是**媒体根**，
    # kernel_path / initrd_path / iso_url 都拼它，而介质只存在于
    # /srv/opstk/pxe-web/<os_type>/<os_version>/ 这些扁平路径上。
    # 上一版修复把 http_root 整体指到 profiles/<pid>，媒体 URL 于是变成
    # profiles/<pid>/ubuntu/22.04/vmlinuz（文件不在那儿）→ iPXE "Could not boot image"，
    # 生产 PXE 被打断，因此被回退（190c1f8）。隔离必须只作用于**应答/引导脚本**。
    http_root = serve_token.serve_base(ip)

    # 模板作用域：pid 校验失败直接 400（不静默脱敏 —— 否则会以为隔离了、实际落到别处）
    try:
        scope = pxe_server.safe_pid(pid)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="非法的模板 id：" + str(e)) from e
    # 该前缀必须与 deploy_files 的落盘前缀一一对应：WEB_ROOT/profiles/<pid>。
    # 目录名常量取自 server.PROFILE_SCOPE_DIR，避免两边各写一份字符串而漂移。
    # J：serve_base 已经含 /pxe/serve（启用时还含 token 段），所以这里只接作用域目录。
    answer_root = (serve_token.serve_base(ip)
                   + "/" + pxe_server.PROFILE_SCOPE_DIR + "/" + scope)

    payload = dict(payload or {})
    net = pxe_server.detect_network()
    # 合并 net_config：以 detect_network() 为底，调用方显式传入的键覆盖它。
    # 注意必须在 dict 层合并：detect_network() 的结果含 server_ip、warnings
    # 等模型字段之外的键，须原样保留并传给生成器，不能经过模型二次过滤。
    caller_net_config = payload.get("net_config") or {}
    # 回落：请求体没给 net_config 时，用**该模板在 DB 里存的** net_config。
    # 为什么必须回落 —— 实测（RUNBOOK-STATE §5.39）：detect_network() 取的是
    # **默认路由所在网卡**（本项目里是接企业网的 ens18），所以只要部署时不显式覆盖，
    # 生成出来的 dnsmasq 就会在**企业网段**上开 DHCP 池，把 10.128.118.0/24 的地址发出去。
    # 而模板里明明配了"PXE 走哪个网卡/哪个池"，却完全没被读 —— 与 §5.29 的 installs
    # 是同一个模式（DB 里存了设置、部署却不看）。优先级：请求体 > DB 模板 > 自动探测。
    if not caller_net_config:
        _pf = await db.get(models.PxeProfile, pid)
        caller_net_config = ((_pf.net_config if _pf else None) or {})
    if net:
        merged = dict(net)
        merged.update(caller_net_config)
        payload["net_config"] = merged
    # 应答文件 / iPXE 菜单的 URL 前缀（不是模型字段，客户端无法注入；由上面算出）
    payload["answer_root"] = answer_root
    payload["server_ip"] = ip
    payload["http_root"] = http_root
    payload["deploy_mode"] = deploy_mode
    # ★ 红线守卫（判据与文案见 _deploy_redline_check）：
    #   ① 先把「server_ip 所在网卡」**写进 payload**，再让 _gen_pxe_files 用同一个值
    #      —— 之前是生成期自己再查一次，第二次查不到就静默回落到模板的客户端网卡名
    #      （= 修复前的错绑），而守卫已经跑完、拦不住；现在只有一份判据。
    #   ② 只在 Linux 上判：非 Linux 本来走 deploy_files 的 supported=False 优雅降级
    #      （提示去下载 ZIP），不能因为查不到网卡就把那条路变成 422。
    if pxe_server.is_linux():
        _bind = pxe_server.serve_binding(ip)
        if _bind.get("interface"):
            _nc_bound = dict(payload.get("net_config") or {})
            _nc_bound["server_interface"] = _bind["interface"]
            payload["net_config"] = _nc_bound
        _err = _deploy_redline_check(
            ip, (payload.get("deploy_mode") or "standalone"),
            payload.get("net_config") or {}, _bind, auto_ip=auto_ip)
        if _err:
            raise HTTPException(status_code=422, detail=_err)
    files = await _gen_pxe_files(pid, payload, db)
    # deploy_files 是同步函数，而容器部署路径里它会**阻塞等待**宿主机重载完成
    # （最长 OPS_HOST_RELOAD_TIMEOUT，默认 25s）。直接在 async 路由里调用会把整个
    # 事件循环卡住 25 秒 —— 并发装机时所有 API（包括别的部署、进度查询）全部停摆。
    # 丢到线程里执行，事件循环继续服务。
    res = await asyncio.to_thread(pxe_server.deploy_files, files, pid)
    # 部署成功后把「lvm 简写的固定容量」警告也送进部署日志（P2：小盘用户要能看见）。
    if res.get("supported") and res.get("ok"):
        _pf = await db.get(models.PxeProfile, pid)
        if _pf is not None and (_pf.disk_scheme or "") == "lvm":
            res.setdefault("log", []).append(LVM_SIZE_WARNING)
    return res


def _serve_binding_warning(server_ip: str, client_iface: str, bind: dict,
                           is_linux: bool = True) -> str:
    """**生成侧**的绑卡警告（/generate、/download 用）：返回中文警告，或空串表示无话可说。

    与部署侧守卫的分工（两者缺一不可）：
      · `/deploy` —— 有 fail-closed 守卫 `_deploy_redline_check`：查不到 server_ip 所在
        网卡、或 DHCP 池不在该网卡网段内，直接 422，绝不生成可疑配置；
      · `/generate`、`/download` —— 产物是**要交给别人落地**的（离线 ZIP、手工安装、
        拷到另一台机器），没有「本机网卡事实」可以依赖，硬拒会把离线流程打死；
        但也**绝不能沉默**：dnsmasq 的 `interface=` 会沿用模板里的**客户端**网卡名，
        落到别的机器上就可能把 DHCP 开在非装机网段（本项目里就是企业网）。

    所以这里只产出「一句必须让人看见的警告」，由 api 层同时写进三处：
    README 的【服务端绑卡警告】、/generate 响应的 warnings、/download 包里的 WARNINGS-*.txt。
    非 Linux 返回空串：那种环境下 serve_binding 本来就查不到网卡，警告只会变成噪音
    （与部署侧"非 Linux 不做守卫"保持一致）。
    """
    if not is_linux:
        return ""
    iface = client_iface or "eth0"
    if not server_ip:
        return ("本次生成没有指定 server_ip，dnsmasq 的 interface= 只能沿用模板里的"
                "客户端网卡名「" + iface + "」。如果这份配置被放到别的机器上、而那张网卡"
                "接的不是装机网，就会在非装机网段（例如企业网）上开 DHCP。"
                "请在生成/部署时显式填写 server_ip（服务端 PXE 网卡的地址）。")
    if not (bind or {}).get("interface"):
        return ("已指定 server_ip=" + str(server_ip) + "，但本机没有网卡持有该地址，"
                "无法确定服务端该绑哪张网卡；dnsmasq 的 interface= 只能沿用模板里的"
                "客户端网卡名「" + iface + "」。请确认该地址已配置在服务端某张网卡上 —— "
                "在服务端本机部署时，这一条会被部署侧守卫直接拒绝。")
    return ""


@router.post("/profiles/{pid}/deploy")
async def deploy_to_host(pid: str, body: PxeGenerateIn = None, db: AsyncSession = Depends(get_db), _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/profiles/{pid}/deploy — 一键部署到本机：生成配置→落地文件→重启 dnsmasq。"""
    body = body or PxeGenerateIn()
    net = pxe_server.detect_network()

    # 1. 调用方显式给出的 server_ip —— 最高优先级
    # 2. 否则用 detect_network()["server_ip"]
    # 3. 否则回退 _local_ip()
    if body.server_ip:
        auto_ip = False
    elif net and net.get("server_ip"):
        body.server_ip = net["server_ip"]
        auto_ip = True
    else:
        body.server_ip = _local_ip()
        auto_ip = True

    payload = body.model_dump(exclude_none=True)
    res = await _generate_and_deploy(
        pid, body.server_ip, body.deploy_mode or "standalone",
        db, auto_ip=auto_ip, payload=payload)

    # 部署结果必须可判定：ok=False 时配置可能已经处于"指向不存在的文件"的状态，
    # 绝不能当成功返回（前端会显示"部署完成"）。
    if not res.get("supported"):
        # 非 Linux：保持既有的"优雅降级"语义（ok=False + log，前端提示去下载 ZIP）
        return res
    if not res.get("ok"):
        detail = "PXE 部署失败：" + "；".join(res.get("errors") or ["未知错误"])
        raise HTTPException(status_code=500, detail=detail[:800])
    # 成功才记「上次部署参数」（finish/done 的自动重部署靠它还原 server_ip/deploy_mode）
    _save_last_deploy(pid, body.server_ip, payload.get("deploy_mode") or "standalone")
    return res


async def _auto_redeploy(pid: str, db: AsyncSession) -> str:
    """finish/done 之后尽力自动重部署（一句话结论，绝不抛异常出去）。"""
    # 模板已经删了就别再拿它的参数去部署：顺手把陈旧条目清掉（否则 state 会越堆越多）
    if await db.get(models.PxeProfile, pid) is None:
        return ("模板已删除，已清理上次部署参数（无需重部署）"
                if _drop_last_deploy(pid) else "模板已删除，无需重部署")
    try:
        params = _read_last_deploy().get(str(pid))
    except Exception as e:  # noqa: BLE001
        return "读取上次部署参数失败：" + type(e).__name__
    if not isinstance(params, dict) or not params.get("server_ip"):
        return "未找到上次部署参数，请手动点一次部署"
    try:
        res = await _generate_and_deploy(
            pid, str(params.get("server_ip")),
            str(params.get("deploy_mode") or "standalone"), db, auto_ip=False)
    except HTTPException as e:
        return "自动重部署被拒绝：" + str(e.detail)[:200]
    except Exception as e:  # noqa: BLE001
        return "自动重部署失败：" + type(e).__name__ + " " + str(e)[:120]
    if not res.get("supported"):
        return "跳过：当前主机不支持自动部署（非 Linux）"
    if not res.get("ok"):
        return "自动重部署失败：" + "；".join(res.get("errors") or ["未知错误"])[:200]
    return "已自动重部署：完成的机器不再收到自动装机菜单"


# ---------- ISO ?? ----------
@router.get("/iso/list")
async def list_isos(_user=Depends(get_current_user)):
    """GET /api/it/pxe/iso/list — 列出 /srv/opstk/iso 中的 ISO 文件。"""
    return pxe_server.list_isos()


@router.get("/media/list")
async def list_media(_user=Depends(get_current_user)):
    """GET /api/it/pxe/media/list — 列出已提取好的引导介质（os_type/os_version）。

    UI 的"版本"下拉用它取值：介质路径是按 os_type+os_version 拼的，版本填错
    生成出来的 kernel URL 必然是 404（iPXE 只报 Could not boot image）。
    """
    return pxe_server.media_list()


@router.get("/os-catalog")
async def os_catalog_catalog(_user=Depends(get_current_user)):
    """GET /api/it/pxe/os-catalog — 支持的系统目录（唯一来源 os_catalog.py）。

    前端三个下拉（装机模板「系统」、ISO 卡片「系统/版本」）与"从文件名识别"
    都渲染这份载荷；后端的 pick_iso / 提取白名单 / 安装器家族判断同样派生自
    这同一个模块 —— 加新系统只改 os_catalog.py 一处，不会再出现
    "加了镜像却选不到"。
    """
    return os_catalog.api_payload()


@router.post("/iso/{iso_name}/extract")
async def extract_iso(iso_name: str, body: dict = None, _user=Depends(require_role("admin"))):
    """从 ISO 提取 PXE 引导文件 (vmlinuz/initrd/squashfs)。"""
    body = body or {}
    return pxe_server.extract_from_iso(
        iso_name,
        os_type=body.get("os_type", "ubuntu"),
        os_version=body.get("os_version", "22.04"),
    )


@router.delete("/iso/{iso_name}")
async def delete_iso(iso_name: str, _user=Depends(require_role("admin"))):
    """删除 ISO 文件。"""
    return pxe_server.delete_iso(iso_name)


# ---------- ISO 镜像传输（契约冻结：拉取 A + 分块上传 B） ----------
# 路由前缀与本文件既有 /iso/list 一组一致（实际挂载在 /api/it/pxe 下）。
# 全部 require_role("admin")，写法照 delete_iso。错误映射见 _iso_transfer_error：
# 一律中文 detail，绝不把异常栈抛给前端。

def _iso_transfer_http_error(e: Exception) -> HTTPException:
    """transfers 的异常 → 契约状态码：Busy→409、NotFound→404、其余 TransferError→400。"""
    if isinstance(e, iso_transfers.BusyError):
        return HTTPException(status_code=409, detail=str(e))
    if isinstance(e, iso_transfers.NotFoundError):
        return HTTPException(status_code=404, detail=str(e))
    if isinstance(e, iso_transfers.TransferError):
        return HTTPException(status_code=400, detail=str(e))
    # 兜底：未预期异常只暴露类型名，不抛栈
    return HTTPException(status_code=500, detail="ISO 传输内部错误：" + type(e).__name__)


@router.get("/iso/space")
async def iso_space(_user=Depends(require_role("admin"))):
    """GET /api/it/pxe/iso/space — ISO 目录磁盘水位 + 单镜像上限（供前端预检）。"""
    return iso_transfers.space()


@router.get("/iso/transfers")
async def iso_transfer_list(_user=Depends(require_role("admin"))):
    """GET /api/it/pxe/iso/transfers — 传输进度列表（内存注册表，不落库）。"""
    return iso_transfers.list_transfers()


@router.post("/iso/fetch", status_code=202)
async def iso_fetch(body: dict = None, _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/iso/fetch — 按 URL 拉取 ISO（202 后台进行，进度看 /iso/transfers）。"""
    body = body or {}
    try:
        return await iso_transfers.start_fetch(
            body.get("url"), body.get("filename"), sha256=body.get("sha256") or "")
    except Exception as e:
        raise _iso_transfer_http_error(e) from e


@router.post("/iso/upload/init")
async def iso_upload_init(body: dict = None, _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/iso/upload/init — 创建分块上传会话（返回 id + chunk_size）。"""
    body = body or {}
    try:
        return iso_transfers.upload_init(
            body.get("filename"), body.get("size"), sha256=body.get("sha256") or "")
    except Exception as e:
        raise _iso_transfer_http_error(e) from e


@router.post("/iso/upload/chunk")
async def iso_upload_chunk(id: str = Form(...), offset: int = Form(...),
                           chunk: UploadFile = File(...),
                           _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/iso/upload/chunk — 顺序追加一块（offset 必须等于当前已收长度）。"""
    try:
        data = await chunk.read()
        # fsync 在线程里做，别让 8MiB 块的落盘阻塞事件循环
        return await asyncio.to_thread(iso_transfers.upload_chunk, id, offset, data)
    except Exception as e:
        raise _iso_transfer_http_error(e) from e


@router.post("/iso/upload/finish")
async def iso_upload_finish(body: dict = None, _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/iso/upload/finish — 校验大小+魔数后原子改名，返回识别结果。"""
    body = body or {}
    try:
        # 整文件 sha256 + 魔数/卷标读取是同步块 I/O，放线程里跑
        return await asyncio.to_thread(iso_transfers.upload_finish, body.get("id"))
    except Exception as e:
        raise _iso_transfer_http_error(e) from e


@router.post("/iso/transfers/{tid}/cancel")
async def iso_transfer_cancel(tid: str, _user=Depends(require_role("admin"))):
    """POST /api/it/pxe/iso/transfers/{id}/cancel — 取消拉取/丢弃上传会话（幂等）。"""
    try:
        return iso_transfers.cancel_transfer(tid)
    except Exception as e:
        raise _iso_transfer_http_error(e) from e
