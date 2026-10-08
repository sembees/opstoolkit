"""PXE 装机配置生成器。

生成四类文件：
  1. Ubuntu autoinstall (user-data, cloud-init subiquity)
  2. RHEL Kickstart (ks.cfg)
  3. iPXE 启动菜单脚本
  4. dnsmasq 配置 (DHCP + TFTP + PXE)
"""
from __future__ import annotations

import ipaddress
import json
import re
from decimal import Decimal
from passlib.hash import sha512_crypt
import shlex
from dataclasses import dataclass, replace

from app.it.pxe import os_catalog


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


# ── 每机静态 IP（本单元：PXE 装完"开箱可达"）────────────────────────────
# 装机记录的 ip 允许两种字形："10.0.0.15"（掩码回退模板）或 "10.0.0.15/26"（自带掩码）。
# 模型 PxeInstall.ip 是 String(64)，两种都装得下，因此不需要新列。
# 这里是 D7 第 3 层兜底：schemas 层已校验，但生成器自己也不信任入参 ——
# 非法 ip **绝不能**静默回退模板默认（那等于两台机器拿到同一个静态 IP，比失败更糟）。
_IPV4_CIDR_RE = re.compile(r"^([0-9]{1,3}(?:\.[0-9]{1,3}){3})(?:/([0-9]|[12][0-9]|3[0-2]))?$")


def _parse_install_ip(value) -> tuple:
    """装机记录 ip -> (地址, 前缀 or None)；空/None 返回 ("", None)；形状非法返回 ("", None)。

    形状合法但不是真实 IPv4（如 999.1.1.1）也返回 ("", None) —— 调用方据此区分
    "没填 ip"（回退模板默认）与"填了非法 ip"（由调用方报错）。
    """
    s = str(value or "").strip()
    if not s:
        return "", None
    m = _IPV4_CIDR_RE.fullmatch(s)
    if m is None:
        return "", None
    addr = m.group(1)
    try:
        ipaddress.IPv4Address(addr)
    except ValueError:
        return "", None
    prefix = int(m.group(2)) if m.group(2) is not None else None
    return addr, prefix


def _prefix_to_netmask(prefix: int) -> str:
    """/N -> 点分掩码（kickstart 的 --netmask 用点分、autoinstall 用 /N，两族都要）。"""
    return str(ipaddress.IPv4Network("0.0.0.0/" + str(int(prefix)), strict=False).netmask)


def _require_install_ip(inst, where) -> tuple:
    """装机记录的 ip：没填返回 (None, None)；填了就必须可解析，否则抛 ValueError。

    为什么非法要大声失败而不是静默跳过：静默跳过 = 该机器回退模板默认网络，
    两台机器拿到同一个静态 IP（地址冲突）或 DHCP 随机地址（回到用户最初的痛点），
    运维还以为登记的 IP 已经生效 —— 这是比"生成失败 422"严重得多的静默错误。
    """
    raw = (inst or {}).get("ip")
    if raw is None or str(raw).strip() == "":
        return None, None
    addr, prefix = _parse_install_ip(raw)
    if not addr:
        raise ValueError(
            where + " 的 ip 不合法 " + repr(str(raw)) +
            "：只允许 a.b.c.d 或 a.b.c.d/N（N=0..32）"
        )
    return addr, prefix


def _machine_net_config(c, inst, where):
    """按装机记录派生该机的网络配置；该机没填 ip 时返回 None（模板默认，行为不回归）。

    规则（用户已定：用静态）：
      · 该机填了 ip ⇒ 这台机器按**静态**下发（装机记录的 ip 优先于模板 net_config 的 ip）；
      · 掩码：ip 自带 "/N" 时按 N（netmask 与 cidr 同时给 —— kickstart 用 netmask、
        autoinstall 用 cidr，两族各取所需）；没带 "/N" 时沿用模板的 netmask/cidr；
      · 网关/DNS/网卡名沿用模板 net_config（模板默认值兜底，缺省时用模板静态分支
        原有的默认值，行为与模板级 static 完全一致）；
      · 模板只有 dhcp 侧的 dns_server（单值）而没有 dns 列表时，把它换算成 dns 列表
        —— 只影响"该机有 ip"这条新路径，模板本身的输出一字不改。
    """
    addr, prefix = _require_install_ip(inst, where)
    if addr is None:
        return None
    nc = dict(c.net_config or {})
    nc["ip"] = addr
    if prefix is not None:
        nc["netmask"] = _prefix_to_netmask(prefix)
        nc["cidr"] = prefix
    if not nc.get("dns") and nc.get("dns_server"):
        nc["dns"] = [nc["dns_server"]]
    return nc


# 装完系统后 sshd_config 的 PermitRootLogin 取值（kickstart %post / late-commands 里用）。
# 默认禁止 root 登录（用户已定默认否）：RHEL 系安装器建的是普通用户、root 常被禁用，
# 所以"装完能 SSH"的默认路径是 普通管理员 + sudo，而不是 root。
def _sshd_permit_root_lines(allow_root: bool) -> list:
    """返回把 PermitRootLogin 固化成指定值的 POSIX 命令（两态都显式写，不依赖发行版默认）。

    用 sed+grep 而不是 /etc/ssh/sshd_config.d 落盘：后者只有 el8+/openEuler 等新系统
    才有 Include，CentOS 7 没有；sed 改主配置在两代上都成立。
    安装期改配置即可 —— sshd 在装机过程中由 anaconda/subiquity 起装，**首次启动**才
    读取配置，所以这里不需要（也不应该）restart sshd：装机期 restart 恰恰是
    "systemctl 返回非零 → 整个装机被判 install_fail" 的既有教训（见 _ubuntu_user_data）。
    """
    value = "yes" if allow_root else "no"
    return [
        "sed -i 's/^#\\?PermitRootLogin.*/PermitRootLogin " + value + "/' /etc/ssh/sshd_config",
        "grep -q '^PermitRootLogin' /etc/ssh/sshd_config || echo 'PermitRootLogin "
        + value + "' >> /etc/ssh/sshd_config",
    ]


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
    # 应答文件 / 引导脚本（user-data、ks.cfg、iPXE 菜单）的 URL 根，**可以与 http_root 不同**。
    #
    # 为什么必须分开（E1 修复的核心约束）：http_root 是**媒体根** ——
    # kernel_path / initrd_path / iso_url 都拼它，而介质只存在于
    # /srv/opstk/pxe-web/<os_type>/<os_version>/ 这些**扁平**路径上。
    # 上一版"按模板隔离"把整个 http_root 指到 profiles/<pid>/，媒体 URL 于是变成
    # profiles/<pid>/ubuntu/22.04/vmlinuz（文件根本不在那儿）→ iPXE 报
    # "Could not boot image"，生产 PXE 被打断，因此被回退（190c1f8）。
    # 结论：只能隔离**应答/引导脚本**，媒体必须留在原地。
    #
    # 留空 = 与 http_root 逐字相同（完全等同于改造前的行为；/generate 与 /download 走这条）。
    answer_root: str = ""
    kernel_path: str = "ubuntu/22.04/vmlinuz"
    initrd_path: str = "ubuntu/22.04/initrd"
    squashfs_path: str = "ubuntu/22.04/installer.squashfs"
    # 内核控制台，见 DEFAULT_KERNEL_CONSOLE 的说明
    kernel_console: str = DEFAULT_KERNEL_CONSOLE
    # 32 位 UEFI(client-arch 6) 的 ipxe-i386.efi **在发行版包里不存在**（§4-6 实测：
    # Ubuntu 22.04 的 apt 里只有 grub-ipxe / ipxe / ipxe-qemu，都不含它；上游也无预编译产物）。
    # 调用方（api 层）按"这个文件在 TFTP 根或候选路径里到底在不在"来传：
    # **不在就别广播一条指向不存在文件的引导项** —— 那只会给 ia32 客户端一个必然失败的承诺。
    # 默认 True = 保持既有输出完全不变（不打扰任何现存用例与部署）。
    ipxe_ia32_available: bool = True
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
    # ── 装完能 SSH（本单元）────────────────────────────────────────────
    # allow_root：是否允许 root 直接 SSH 登录（默认**否**，用户已定）。
    # RHEL 系 kickstart 建的是普通管理员 + wheel，root 常被禁用；"装完能 SSH"的默认
    # 路径必须是 普通管理员 + sudo，root 直登是显式打开的例外（PermitRootLogin yes，
    # Ubuntu 侧还会同时给 root 置上口令，否则 Ubuntu 的 root 账号默认锁定、开了也没用）。
    allow_root: bool = False
    # sudo_nopasswd：管理员是否 sudo 免密（默认**是**，用户已定）。
    # 两族都显式落盘成 /etc/sudoers.d/90-opstk-<user>（RHEL wheel 默认要密码、
    # Ubuntu subiquity 默认免密 —— 两态都由我们显式表达，不依赖安装器默认值）。
    sudo_nopasswd: bool = True
    # ── 装完回调（防重复抹盘，2026-10-08 真机实证）────────────────────────
    # 该台机器装完后的回调 URL（api 层按装机记录 id + HMAC 令牌拼出，见
    # app/api/pxe.py 的 install_done_url）。只随**每机**应答文件下发：装机完成时
    # 回调把该机记录标记 installed，dnsmasq 从此不再给这台机器下发自动装机菜单。
    # 模板级 ks.cfg / user-data 恒为空串（不带回调，输出与历史逐字一致）；
    # 生成侧只认本文件拼好的值，且必须先过 _safe_done_url 白名单。
    done_url: str = ""
    # ── 服务端绑卡警告（2026-10-08 补：/generate 与 /download 没有部署侧那道守卫）──
    # 部署（/deploy）有 fail-closed 守卫：查不到 server_ip 所在网卡就 422。但
    # /generate 与 /download **产出的是要交给别人落地的文件**（离线 ZIP、手工安装），
    # 它们既不能硬拒（离线场景没有"本机网卡事实"可查），又不能沉默 ——
    # 否则 dnsmasq 的 `interface=` 会沿用模板里的**客户端**网卡名，落到别的机器上
    # 就可能把 DHCP 开在非装机网段。所以：api 层把警告文本塞进本字段，README 里
    # 用【服务端绑卡警告】整段印出来，同时随 /generate 响应与 ZIP 里的
    # WARNINGS-*.txt 一起交付。空串 = 无警告（输出与历史逐字一致）。
    warn_serve_binding: str = ""


# 装机流程按**安装器家族**分岔（不再是"ubuntu vs 其余"）：
#   · kickstart(anaconda) 家族 —— rhel/rocky/alma/centos(含 Stream)/oraclelinux/
#     openeuler/kylin/uos/anolis/fedora，复用同一套 inst.* 参数与 kickstart 生成器，
#     介质文件名也一样是 initrd.img（不是 Ubuntu 的 initrd）；
#   · ubuntu —— autoinstall(subiquity)/casper 分支（既有实现不变）；
#   · debian(preseed)/opensuse(autoyast) —— auto_install=False：只支持识别 ISO 与
#     提取引导介质，生成配置时给出明确拒绝提示（见 _unsupported_auto_install_msg）。
# 家族成员的**唯一**定义点在 os_catalog.py；这里的常量都从目录派生，绝不再手抄一份。
# 校验层 schemas._OS_TYPE_ALLOWED 必须与 os_catalog 派生的白名单一致，有测试锁住两者不漂移。
RHEL_FAMILY = os_catalog.kickstart_family() + tuple(
    a for e in os_catalog.entries() if e.installer == os_catalog.KICKSTART for a in e.aliases
)


def is_rhel_family(os_type) -> bool:
    """是否属于 kickstart(anaconda) 家族（决定走 anaconda 分支与 initrd.img 介质路径）。

    兼容历史调用方：入参可能是 "RHEL " 这类带空白/大小写的值（DB 归一化只发生在
    保存入口），这里先归一化再查目录 —— 与旧行为相比只多不少（旧表查不到就 False）。
    """
    return os_catalog.is_kickstart(os_type)


# Ubuntu ISO 文件名里 OS 类型的关键字（用于自动挑选镜像）。
# 旧实现是一张手抄的两键表（"ubuntu" / "rhel 家族联合"）；现在派生自 os_catalog：
# 每个系统用自己的文件名关键字，只有 rhel 保留"家族联合关键字"的伞语义
# （pick_iso(rhel) 在 RHEL 家族内按版本挑 —— 既有行为，有测试锁住）。
_ISO_TYPE_KEYS = {
    e.key: (e.pick_keywords or e.filename_keywords) for e in os_catalog.entries()
}


# 版本号按"数字段边界"抽取，避免子串误配：ver="9" 不该命中 "Rocky-19.4"，
# "22.04" 也不该命中 "122.04"。
_ISO_VER_RE = re.compile(r"(?<![0-9])(\d+(?:\.\d+)*)(?![0-9])")


def _iso_versions(name: str) -> list:
    """取出文件名里的所有版本号候选（小写）。"""
    return _ISO_VER_RE.findall(name.lower())


# 无安装器的"纯 live/桌面"形态 token（审计 D10）：这些 ISO 里没有 anaconda/
# subiquity 安装器，按它们生成的装机配置必然失败 —— 同版本时必须排在
# dvd/everything/netinst/boot/minimal 等**可安装形态**之后。ubuntu 例外：它的
# 官方安装介质本身就是 live-server（目录字段 live_is_installer 驱动，见
# _edition_rank），保持既有"live-server 优先"。
_UNINSTALLABLE_TOKENS = ("live", "desktop", "workstation")


def _edition_rank(low: str, live_is_installer: bool) -> int:
    """形态分（值大者优先）：可安装形态 > 纯 live/桌面形态。"""
    if live_is_installer:
        # ubuntu 的安装介质就是 live-server —— "live-server 优先"只对它适用
        return 1 if "live-server" in low else 0
    return 0 if any(t in low for t in _UNINSTALLABLE_TOKENS) else 1


def pick_iso(names, os_type: str, os_version: str) -> str:
    """从 /srv/opstk/iso 的文件名列表里挑出最匹配 os_type/os_version 的那个 ISO。

    纯函数（不做 I/O），便于单测。返回文件名，挑不到返回 ""。

    匹配规则（正式环境里同目录会同时存在多个系统镜像，必须挑准）：
      1. 必须含该 OS 类型的任一关键字，且**不含**该条目的 pick_exclude_keywords
         （审计 D9：kylin 排除含 "ubuntu" 的文件名 —— 优麒麟(UbuntuKylin) 是
         casper 家族镜像，拿 kylin 的 kickstart/inst.* 去装必然失败，宁可不挑、
         由上层走"识别不出来"的可读提示；银河麒麟官方 ISO 文件名不含 ubuntu，
         正例不误伤）；
      2. 版本按**数字段边界**匹配：查询 `22.04` 命中文件名里的 `22.04` 或 `22.04.5`，
         但不命中 `24.04`；查询 `9` 命中 `9`/`9.4`，但不命中 `19.4`
         （旧实现用 `ver in low` 子串匹配，会被 `Rocky-19.x` 这类名字误配）；
      3. 只认 .iso；
      4. 优先级：版本号段数多者优先（22.04.5 比 22.04 更精确）→ **可安装形态
         优先**（dvd/everything/netinst/boot/minimal 压过 live/desktop/
         workstation；ubuntu 的 live-server 例外，见 _edition_rank）→ 文件名
         排序兜底。排序键 (版本段数, 形态分, 文件名) 是全序，结果确定、
         与输入顺序无关（既有守护用例不变）。
    """
    ost = (os_type or "ubuntu").strip().lower()
    ver = (os_version or "").strip().lower()
    e = os_catalog.entry(ost)
    keys = _ISO_TYPE_KEYS.get(ost, (ost,))
    excludes = tuple(e.pick_exclude_keywords) if e else ()
    live_is_installer = bool(e and e.live_is_installer)
    cands = []
    for n in names or []:
        base = str(n).strip()
        low = base.lower()
        if not low.endswith(".iso"):
            continue
        if not any(k in low for k in keys):
            continue
        if any(x in low for x in excludes):
            # 跨家族镜像（审计 D9）：没有 anaconda，命中即装机必败 —— 跳过，
            # 让上层走"挑不到镜像"的可读提示，而不是生成一份必败的装机配置。
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
        cands.append((score, _edition_rank(low, live_is_installer), base))
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
# bios_grub：**不是文件系统**，而是一个 1MiB、无文件系统、带 bios_grub 标记的分区。
# 为什么需要它（RUNBOOK-STATE §5.46 缺陷 3，真机实测）：BIOS + GPT 下 subiquity 会报
#   "autoinstall config did not create needed bootloader partition"
# 然后拒绝装机 —— GRUB 在 BIOS+GPT 上必须有一个地方放 core.img。
# RHEL 侧的对应物是 `part biosboot --fstype=biosboot`。
# 用一个"伪 fstype"来表达它（而不是新增字段）是为了让前端/存量配置的改动面最小。
BIOS_GRUB_FSTYPE = "bios_grub"
_FSTYPE_ALLOWED = ("ext4", "xfs", "btrfs", "fat32", "vfat", "swap", BIOS_GRUB_FSTYPE)
_MOUNT_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_RAID_LEVELS = (0, 1, 5, 6, 10)
_DISK_MODES = ("auto", "name", "match")
_DISK_LAYOUTS = ("lvm", "direct", "zfs", "custom")
_SIZE_UNIT = {"M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4, "P": 1024 ** 5}
# "吃掉剩余空间"的两种写法：kickstart 用 --grow，subiquity 用字符串 "rest"
_GROW_SIZES = ("rest", "100%FREE")
# /boot/efi 与 /boot 在 GPT 上的分区标志（规格 §3.4）
_PART_FLAGS = {"/boot/efi": "esp", "/boot": "boot"}
# kickstart 里 raid 成员/物理卷的标识形式；与 §3.3 的 part.NN 同属一套"第 N 个分区"语义。
# 两个捕获组：组 1 = 盘名，组 2 = 分区号。
_RAID_DEV_RE = re.compile(
    r"^((?:sd|vd|hd|xvd)[a-z]|nvme[0-9]+n[0-9]+|mmcblk[0-9]+)p?([0-9]+)$"
)
# raid.devices 的三种写法 + 各自的**编号基准**，写成显式表而不是散在 if/else 里。
# 基准不同是刻意的（三种写法各自对着一套既有标识），因此"同一个分区"的等价写法是：
#   第 3 个分区 == part.03（1 起）== part2（0 起）== sda3（1 起）
# 三者必须换算到**同一个下标**：换算一旦漂移，校验层放行的 RAID 成员在生成物里会指到
# 另一个分区上（= 把那块分区上的数据做成 RAID 成员并抹掉）。有测试逐写法断言。
# 表尾的 True = "盘名前缀只用于校验，不改变分区归属"（见 _raid_part_ref）。
_RAID_ID_FORMS = (
    (re.compile(r"^part\.0*([0-9]+)$"), 1, False),   # ks 规范写法，1 起
    (re.compile(r"^part([0-9]+)$"), 0, False),       # subiquity 的 id，0 起（part0 = 第 1 个）
    (_RAID_DEV_RE, 1, True),                         # 盘名+分区号，1 起
)


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


def _raid_part_ref(tok, field):
    """raid.devices 里的分区标识 → (partitions 下标, 盘名前缀)。

    **本函数是唯一的分区标识换算实现**：schemas 层（HTTP 入口）直接 import 它，两层
    共用一份基准。旧实现两层各抄一份正则，基准一旦漂移，同一个 RAID 成员会在校验层与
    生成层指到**不同的分区**（校验通过、生成物却把另一块分区做成了 RAID 成员）。

    三种写法见 _RAID_ID_FORMS。盘名前缀（sda3 → "sda"）只用于校验（它是不是被声明成
    数据盘），**不改变分区归属**：§2 要求 auto 忽略写死的盘名，这里与之一致 ——
    `sda3` 就是"第 3 个分区"。返回下标 None 表示标识无法解析，由调用方按自己的措辞报错。
    """
    s = _safe_ident(tok, field)
    for pattern, base, disk_qualified in _RAID_ID_FORMS:
        m = pattern.fullmatch(s)
        if not m:
            continue
        if disk_qualified:
            return int(m.group(2)) - 1, m.group(1)
        return int(m.group(1)) - base, ""
    return None, ""


def _raid_member_ref(tok, partitions, field):
    """第 3 层：分区标识 → (下标, 盘名前缀)；越界/无法解析一律报错并列出本次可用标识。"""
    avail = ", ".join(p["id_ks"] for p in partitions) or "(partitions 为空)"
    idx, disk = _raid_part_ref(tok, field)
    if idx is None or not 0 <= idx < len(partitions):
        raise ValueError(
            field + " 引用了未定义的分区标识 " + repr(tok) + "；本次可用标识：" + avail
        )
    return idx, disk


# 功能 G：挂载数据盘上**已有**的文件系统（不新建分区、不格式化）。
# 用 UUID / LABEL 定位，而不是设备名 —— 依据是 pykickstart 3.78 的原文（不是我印象里的写法）：
#   · `part` 命令说明："All partitions created will be formatted as part of the installation
#     process unless ``--noformat`` and ``--onpart`` are used."
#   · `--noformat`："Tells the installation program not to format the partition, for use with
#     the ``--onpart`` command."
#   · `--onpart`："Put the partition on an already existing device. Use ``--onpart=LABEL=name``
#     or ``--onpart=UUID=name`` to specify a partition by label or uuid respectively."
#     "Anaconda may create partitions in any particular order, so it is safer to use labels
#     than absolute partition names."
#   ⇒ 与 §5.42「设备名不是身份」同一口径：产物里根本不出现设备名，枚举顺序反转也不受影响。
_EXISTING_KEYS = ("existing_uuid", "existing_label")
# 值会原样拼进 ks 的 `--onpart=`，因此与 serial/wwid 同一套白名单（不含空格/引号/等号）。
_EXISTING_VALUE_RE = re.compile(r"[A-Za-z0-9._:+-]{1,64}")


def _existing_fs_id(d, field) -> str:
    """把 `existing_uuid` / `existing_label` 归一成 `--onpart=` 的取值（`UUID=<u>`/`LABEL=<l>`）。

    返回 "" 表示"这条数据盘声明不是用来挂既有文件系统的"。
    """
    got = [(k, str(d.get(k) or "").strip()) for k in _EXISTING_KEYS]
    got = [(k, v) for k, v in got if v]
    if not got:
        return ""
    if len(got) > 1:
        raise ValueError(
            field + "：existing_uuid 与 existing_label 只能给一个"
            "（两个都给无法确定用哪个去匹配既有文件系统）")
    k, v = got[0]
    if not _EXISTING_VALUE_RE.fullmatch(v):
        raise ValueError(
            field + "." + k + " 含非法字符（只允许字母数字与 . _ : + -，最多 64 字符）："
            + repr(v[:40]) + " —— 这个值会被拼进 kickstart 的 --onpart=；"
            "卷标里带空格/引号/等号的，请改用 existing_uuid（文件系统 UUID）")
    return ("UUID=" if k == "existing_uuid" else "LABEL=") + v


def _disk_plan(c, dc):
    """把 disk_config 归一化为内部结构；返回 None 表示走【既有路径】（逐字不变）。

    这里做第 3 层校验（第 2 层在 schemas.PxeDiskConfigIn）：白名单、rest 只能一次且在最后、
    /boot/efi 与 / 的搭配、custom 必须有 /、挂载点/RAID 名/data_disks 名的唯一性、
    vg/lv 必须成对、raid 引用必须已定义、data_disks 不得与目标盘同名。

    另一条硬规则：**用户给的值绝不允许被静默丢弃**。非 custom 布局下 partitions/raid
    完全不会出现在产物里，所以给了就拒绝（只有 data_disks 的 wipe=false+无 mount 例外，
    它有真实语义 —— "别碰这块盘"，RHEL 侧靠 %pre 的候选排除集落地）。
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
    # id_path：udev 的物理路径（pci-0000:00:05.0-scsi-0:0:0:1）。Ubuntu 侧必须优先用它 ——
    # subiquity 的 serial 取 sysfs，QEMU 下为空（§5.45 实测）。字符集收窄到 udev 实际会用的集合。
    id_path = re.sub(r"[^A-Za-z0-9._:+-]", "", str(tgt.get("id_path") or "").strip())
    if mode == "auto":
        # 规格 §2 明文要求：auto 必须忽略 name，避免"以为自动其实写死"
        name = ""
    if mode == "name" and not name:
        raise ValueError("disk_config.target.mode=name 但没有给出 disk_config.target.name")
    # id_path 也算合法的匹配键 —— Ubuntu 侧甚至**只能**用它（subiquity 的 serial 取 sysfs，
    # QEMU 下为空，见 §5.45）。RHEL 侧的 %pre 目前只用 serial/model 匹配，给了 id_path
    # 也不会被用上，所以下面按"是否已有 RHEL 能用的键"分别判断，避免生成一份
    # 看着合法却在 RHEL 上选不出盘的配置。
    if mode == "match" and not (serial or model or id_path):
        raise ValueError(
            "disk_config.target.mode=match 但没有给出 id_path、serial 或 model")

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

    custom = layout == "custom"
    parts_in = dc.get("partitions") or []
    if not isinstance(parts_in, list):
        raise ValueError("disk_config.partitions 必须是数组")
    if not custom:
        # layout 简写（lvm/direct/zfs）把分区工作整体交给 anaconda/subiquity，我们的产物里
        # 不会出现 partitions/raid 的任何一行 —— 给了就必须拒绝，绝不静默丢弃运维的配置。
        if parts_in:
            raise ValueError(
                "disk_config.partitions：layout=" + layout
                + " 的分区由安装器自动完成，不能同时给自定义分区表；要自定义请设 layout=custom"
            )
        if dc.get("raid"):
            raise ValueError(
                "disk_config.raid：layout=" + layout
                + " 不生成 RAID（只支持 layout=custom），该配置会被丢弃，故拒绝"
            )
    # Ubuntu 的非 custom 布局只有 subiquity 的 layout.match（size: largest / path / serial /
    # model），**没有任何排除语法** —— 机器上"小系统盘 + 大数据盘"时 largest 正好会选中数据盘，
    # 随后 wipe 把它抹掉（本缺陷的 Ubuntu 侧）。表达不出来就不能猜，必须拒绝。
    if (not custom) and mode == "auto" and dc.get("data_disks") and not is_rhel_family(c.os_type):
        raise ValueError(
            "disk_config.data_disks：Ubuntu 的非 custom 布局没有排除语法（layout.match 只能选最大盘），"
            "target.mode=auto 时可能选中并抹掉数据盘；请把 target.mode 设为 name 或 match，或改用 layout=custom"
        )
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
    # 挂载点唯一性：同一个挂载点建两遍，anaconda/subiquity 都会在安装中途报错
    # （subiquity 直接拒绝重复 mount path），甚至把两块分区反复格式化。
    seen_mount = {}
    for p in partitions:
        if not p["mount"]:
            continue                      # 空挂载点（只建分区）不参与唯一性
        if p["mount"] in seen_mount:
            raise ValueError(
                "disk_config.partitions[%d].mount：挂载点 %s 与 disk_config.partitions[%d] 重复"
                % (p["index"], p["mount"], seen_mount[p["mount"]])
            )
        seen_mount[p["mount"]] = p["index"]
    # custom 布局必须自己给出 /：安装器不会替你补，装完的系统起不来（规格 §4）。
    if custom and "/" not in mounts:
        raise ValueError(
            "disk_config.partitions：layout=custom 必须有一个 / 分区（没有 / 的系统起不来）"
        )
    # 同一个 (vg, lv) 只能出现一次：重复的 logvol 名会让 anaconda 报 "logical volume ... "
    # 或者把两个分区叠在同一个 LV 上。
    seen_lv = {}
    for p in partitions:
        if not p["vg"]:
            continue
        key = (p["vg"], p["lv"])
        if key in seen_lv:
            raise ValueError(
                "disk_config.partitions[%d].lv：%s/%s 与 disk_config.partitions[%d] 重复"
                % (p["index"], p["vg"], p["lv"], seen_lv[key])
            )
        seen_lv[key] = p["index"]

    raid_out = []
    raid_member_indexes = set()
    raid_disk_specs = []          # [(field, 用户写的标识, 盘名前缀)] —— 用于 data_disks 交叉检查
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
        idxs = []
        for d in devs:
            idx, disk_prefix = _raid_member_ref(d, partitions, f + ".devices")
            if disk_prefix:
                raid_disk_specs.append((f + ".devices", d, disk_prefix))
            idxs.append(idx)
        rmount = _safe_mount(r.get("mount", ""), f + ".mount")
        raid_member_indexes.update(idxs)
        raid_out.append({
            "name": _safe_ident(r.get("name", ""), f + ".name") or ("md%d" % j),
            "level": level,
            "member_indexes": idxs,
            "mount": rmount,
            "fstype": _safe_fstype(r.get("fstype", ""), f + ".fstype", rmount),
            "id_sub": "md%d" % j,
        })
    raid_names = [x["name"] for x in raid_out]
    if len(set(raid_names)) != len(raid_names):
        dup = [n for n in raid_names if raid_names.count(n) > 1][0]
        raise ValueError(
            "disk_config.raid[].name：" + repr(dup) + " 重复（RAID 设备名必须唯一）"
        )
    # 同一个分区不能既当 LVM PV 又当 RAID 成员：anaconda 会把它同时塞进 volgroup 与
    # mdadm，结果是"先建 PV 再被 RAID 元数据覆盖"或直接安装失败 —— 用户配错了，必须点名拒绝。
    for p in partitions:
        if p["vg"] and p["index"] in raid_member_indexes:
            raise ValueError(
                "disk_config.partitions[%d]：同一个分区不能既是 LVM PV（.vg/.lv 已给）"
                "又是 RAID 成员（disk_config.raid[].devices 引用了它）" % p["index"]
            )

    data = []
    data_names = []
    for k, d in enumerate(dc.get("data_disks") or []):
        if not isinstance(d, dict):
            raise ValueError("disk_config.data_disks[%d] 必须是对象" % k)
        f = "disk_config.data_disks[%d]" % k
        dname = _safe_ident(d.get("name", ""), f + ".name") if d.get("name") else ""
        # §5.42：设备名不是身份 —— sdX 由内核探测顺序决定，同一台机器两次启动都可能互换。
        # 排除数据盘必须靠稳定属性（size/serial/wwid），只写 name 直接拒绝。
        if not any(str(d.get(k) or "").strip() for k in ("size", "serial", "wwid")):
            raise ValueError(
                f + "：必须给出稳定匹配条件之一（size / serial / wwid）。"
                "只写 name（设备名）不可靠 —— 设备名会随内核探测顺序变化，"
                "用它做排除集会抹掉数据盘（真机三台并发实测，中了一台）。例如 size=\"30G\"。")
        if dname:
            if dname in data_names:
                raise ValueError(f + ".name：数据盘 " + repr(dname) + " 重复声明")
            data_names.append(dname)
        # 名字为空时下游仍有几处需要盘名才能生成**合法**产物：
        #   · wipe=True：要显式把这块盘写进 clearpart/ignoredisk
        #   · Ubuntu：subiquity 的 storage 配置只认 path（/dev/sdX）
        # 这些场景下 name 仍是必填；只有"纯排除用途"（RHEL + wipe=false）才允许只给稳定属性。
        # 加这道护栏是为了不生成"ignoredisk --only-use=  "这种残缺行（那会把目标盘也弄丢）。
        # 什么时候仍然必须要盘名：**产物里要显式写到这块盘、又没有 %pre 可以现场反解**。
        #   · Ubuntu（任意模式）：curtin 的 storage 只认 path=/dev/<name>；
        #   · RHEL 的 mode=name：没有 %pre，--ondisk= 只能写盘名。
        # 而 RHEL 的 auto/match 现在会在 %pre 里按该声明自己的稳定条件反解出 $ddevN
        # （R2-H3），所以**不再需要**盘名 —— 之前那条"wipe=true 就必须要 name"
        # 在修好 R2-H3 之后反而变成过严的拦截。
        if not dname and not is_rhel_family(c.os_type):
            raise ValueError(
                f + ".name：Ubuntu 布局必须给出盘名 —— curtin 的 storage 配置只认 "
                "path=/dev/<name>，只给 size/serial/wwid 无法表达要写到哪块盘。")
        if not dname and mode == "name" and (d.get("wipe")
                                             or any(str(d.get(k) or "").strip()
                                                    for k in _EXISTING_KEYS)):
            raise ValueError(
                f + ".name：target.mode=name 下要在产物里**显式引用**这块数据盘"
                "（wipe=true 要清它，或用 existing_uuid/existing_label 挂它）时必须给出盘名 —— "
                "该模式下没有 %pre 可以现场反解设备名。")
        # 缺陷 #1 的 schema 侧交叉检查：mode=name 时 target.name 与数据盘同名是自相矛盾
        # （同一块盘既当系统盘又当"别碰"的数据盘）。auto/match 下 name 不参与选盘，不做要求。
        if mode == "name" and name and dname and dname == name:
            raise ValueError(f + ".name 不能与目标盘同名 " + repr(dname))
        dmount = _safe_mount(d.get("mount", ""), f + ".mount")
        dwipe = _as_bool(d.get("wipe"), f + ".wipe", default=False)
        # 功能 G：这条声明是不是"挂载既有文件系统"（UUID/LABEL 定位，不格式化）
        ex_id = _existing_fs_id(d, f)
        if ex_id and not dmount:
            raise ValueError(
                f + ".existing_uuid/existing_label：挂载**已有**文件系统必须同时给出 mount"
                "（挂到哪个目录）—— 只给 UUID 我们不知道要挂到哪儿。")
        if ex_id and dwipe:
            raise ValueError(
                f + "：existing_uuid/existing_label 与 wipe=true 互相矛盾 —— wipe=true 会清掉"
                "这块盘的分区表，既有的文件系统就没了。要保留既有数据请设 wipe=false"
                "（或去掉 wipe），两者只能选一个。")
        # ★ 功能 G：fstype **允许**给（并会透传成 `--fstype=`）。
        #   为什么不拦：我最初想按"不格式化 ⇒ fstype 没意义 ⇒ 给了就拒绝"处理，但查了
        #   pykickstart 的官方文档后**放弃了那个判断** —— 文档对 `--onpart --noformat`
        #   组合下 `--fstype` 是否必需/是否被读**没有任何一句话**（原文只定义 fstype 是
        #   "Sets the file system type for the partition"）。既然"必需"与"无用"都无法确证，
        #   就给运维一个**可用**的表达方式（万一 anaconda 需要它，拦住等于让人装不上）；
        #   留空也照常工作。**不拦 ≠ 静默丢弃**：给了就写进产物，产物里看得见。
        if not custom:
            # 非 custom 布局下我们不给数据盘生成任何一行，所以这两个值一定会被丢掉。
            if dmount:
                raise ValueError(
                    f + ".mount：layout=" + layout
                    + " 不生成数据盘分区/挂载点（只有 layout=custom 才生成），该值会被丢弃，故拒绝"
                )
            if dwipe:
                raise ValueError(
                    f + ".wipe=true：layout=" + layout
                    + " 不清数据盘（只有 layout=custom 才生成 clearpart），该值会被丢弃，故拒绝"
                )
        elif dmount and not dwipe and not ex_id:
            # custom 布局：wipe=false 表示"不碰这块盘的既有分区表"，那就没有地方可以安全地
            # 新建并挂载一个分区 —— mount 会被丢掉。
            raise ValueError(
                f + ".mount：wipe=false 时不会给数据盘建分区/格式化，挂载点会被丢弃，故拒绝；"
                "要建分区并挂载请设 wipe=true（或去掉 mount）；"
                "要挂**已有**文件系统，请额外给出 existing_uuid（或 existing_label）"
            )
        if ex_id and not is_rhel_family(c.os_type):
            # 依据不足就不做（★ 这段文案在**读到 curtin 实现**之后第二次改写，见 §5.80）：
            # RHEL 侧有 pykickstart 原文可依（--onpart + --noformat）。
            # Ubuntu 侧**读实现验过了**（本机 installer.squashfs → subiquity_6066.snap → curtin 源码），
            # 结论是**表达不出来**，而不是"没试过"：
            #   · `PARTITION` 的 `required = ['id','type','device','size']` —— **size 必填**；
            #   · v2 用 `_find_part_info(sfdisk_info, offset)` **按 offset** 匹配既有分区，
            #     匹配不上直接抛 `could not find existing partition by offset`；
            #   · `_wipe_for_action()`：`preserve: true` ⇒ 不擦，否则 ⇒ `'superblock'`；
            #     且未进 `preserved_offsets` 的既有分区会被 `wipe_volume(..., 'superblock')` 擦掉。
            #   ⇒ 要挂既有文件系统，就必须在配置里写出那个分区**既有的 size 与 offset**
            #     （生成期拿不到；让运维去查 offset 是把风险转嫁给最容易出错的一方）。
            raise ValueError(
                f + ".existing_uuid/existing_label：挂载**已有**文件系统目前只在 RHEL 系实现"
                "（kickstart 的 `part <挂载点> --onpart=UUID=… --noformat`，有 pykickstart "
                "原文依据）。Ubuntu(subiquity/curtin) 侧**表达不出来**：curtin 的 `partition` 条目"
                "**必须给出既有分区的 size 与 offset**（schema 里 size 必填，v2 按 offset 匹配既有"
                "分区，匹配不上会直接报错），而生成期拿不到这两个值；并且未声明为 `preserve: true` "
                "的既有分区会被 superblock 擦除。因此按 fail-closed 拒绝生成，"
                "不拿客户的数据赌。请改用 RHEL 系模板，或先手工挂载该文件系统。")
        entry = {
            "name": dname, "mount": dmount,
            "fstype": _safe_fstype(d.get("fstype", ""), f + ".fstype", dmount),
            # 生产红线：数据盘默认不碰，必须显式 wipe=true 才动（规格 §2）
            "wipe": dwipe,
            # §5.42：稳定匹配条件必须带进 plan —— 否则 %pre 拿不到它们，
            # 排除集为空，又会退回到"按名字排除"那条会抹盘的老路。
            "size": str(d.get("size") or "").strip(),
            "serial": _safe_matcher_value(d["serial"], "serial", k) if d.get("serial") else "",
            "wwid": _safe_matcher_value(d["wwid"], "wwid", k) if d.get("wwid") else "",
        }
        if ex_id:
            # 功能 G：非空表示"挂既有文件系统"（UUID=<u> / LABEL=<l>），与 wipe 互斥。
            # **只在真的用到时才加这个键** —— 没给 existing_* 的老配置，plan 的字面形状
            # 与改动前逐字一致（有 golden 用例对整个 plan 做等值断言）。
            entry["existing"] = ex_id
        data.append(entry)

    # ── 目标盘与数据盘的"身份重叠"校验（生产安全，§5.47）────────────────────
    # 同一块盘不能**既当系统盘、又被声明为要保护的数据盘** —— 那不是配置笔误这么简单：
    # 目标盘会被 clearpart/wipe 清掉，等于"你让我别碰的那块盘，正是你让我装系统的那块"。
    # 为什么不能只比名字：数据盘可以只给 size/serial/wwid（§5.42 之后这是推荐写法），
    # 那时名字是空的，纯名字比较恒不成立 = 这道防线形同虚设。
    # 所以凡是**两边都有、语义相同**的身份都比一遍：name 与 serial。
    # （外部审查 U1-F2 建议把 model/size/wwid 也比一遍 —— 实际比不了：target 只有
    #  name/serial/model/id_path，数据盘只有 name/size/serial/wwid，**交集只有 name 与 serial**。
    #  所以这里改为对"只用 model 定位目标盘"这种无法交叉校验的写法直接拒绝，见下。）
    _t_ident = {"name": name, "serial": serial}
    for k, d in enumerate(data):
        for key in ("name", "serial"):
            tv = (_t_ident.get(key) or "").strip().lower()
            dv = str(d.get(key) or "").strip().lower()
            if tv and dv and tv == dv:
                raise ValueError(
                    "disk_config：目标盘与 data_disks[%d] 的 %s 完全相同（%r）—— "
                    "同一块盘不能既是系统盘、又是声明为要保护的数据盘；"
                    "目标盘会被清空分区，这等于把要保护的数据盘抹掉。" % (k, key, dv))

    # 外部审查 U1-F2（我复核确认机制成立）：mode=match **只用 model** 定位目标盘、
    # 同时又声明了 data_disks 时，上面那道交叉校验**没有可比的字段**
    # （数据盘没有 model 这个键）⇒ 防线形同虚设。而 model 在同类盘之间不唯一：
    # 两块同型号盘时它可能正好命中被声明为"别碰"的那块，目标盘一分区就等于抹掉数据盘。
    # 这与本项目"不猜、宁可显式失败"的一贯口径一致：这种配置直接拒绝。
    if mode == "match" and model and not serial and data:
        raise ValueError(
            "disk_config：target.mode=match 只给了 model（没有 serial），同时又声明了 "
            "data_disks —— 无法交叉校验目标盘与数据盘是不是同一块（数据盘没有 model 键），"
            "而 model 在同型号盘之间不唯一，一旦命中数据盘就会把它抹掉。"
            "请给 target 补 serial（数据盘有 serial 时即可交叉校验），"
            "或改用 target.mode=name 显式给盘名。")

    # RAID 成员的"盘名前缀"与数据盘同名 = 用户想在这块盘上做 RAID，又声明它是"别碰"的数据盘。
    # 两者的语义在本规格里不可能同时成立（RAID 成员是**目标盘**上的分区），必须拒绝而不是猜。
    for f, tok, disk_prefix in raid_disk_specs:
        if disk_prefix in data_names:
            raise ValueError(
                f + " 的 " + repr(tok) + " 把 " + repr(disk_prefix)
                + " 当作 RAID 成员，但该盘在 disk_config.data_disks 里声明为数据盘 —— 二者矛盾"
            )

    return {
        "mode": mode, "name": name, "serial": serial, "model": model,
        "id_path": id_path,
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
    """目标盘在 subiquity 里的表达（规格 §3.1）。

    ⚠️ `serial` 在虚拟化环境下**不可靠**（RUNBOOK-STATE §5.45 真机实测）：
    subiquity 取的是 sysfs 的 `/sys/block/sdX/device/serial`，而 QEMU/virtio-scsi
    **不填这个属性**（实测为空）。`lsblk -o SERIAL` 与 udev 报的 `drive-scsiN`
    来自 SCSI VPD 页，是**另一个来源** —— 于是 `serial: drive-scsi1` 会让 subiquity 报
    `matched no disk`，装机直接失败。**同一个字段在 RHEL(%pre 用 lsblk) 与
    Ubuntu(subiquity 用 sysfs) 两条路径上含义不同。**
    `id_path`（udev 的 ID_PATH，如 `pci-0000:00:05.0-scsi-0:0:0:1`）是物理路径，
    与内核枚举顺序无关，且在 QEMU 与真机上都由 udev 填充 —— 因此**优先用它**。
    """
    if plan["mode"] == "name":
        return {"path": "/dev/" + plan["name"]}
    if plan["mode"] == "match":
        # ⚠️ 生产标准：**只允许"不生效就会大声报错"的匹配键**。
        # 真机实测（§5.46 追加 5）：给 subiquity 一个它不认识的键（如 id_path），
        # 它**不报错**，而是把 disk 条目当成"没有匹配条件"，然后**匹配第一块盘** ——
        # 在"数据盘排在前面"的机器上，这就等于**直接抹掉数据盘**（实测：连写两次，
        # 两个不同的 id_path 值都打到了同一块 30G 数据盘，20G 系统盘一根没动）。
        # 这种"静默退回"的失效方式在生产里是不可接受的：
        #   · `serial` / `model` 不匹配 → subiquity 明确报 matched no disk（大声失败，可接受）
        #   · 未知键              → 静默装到第一块盘（数据丢失，不可接受）
        if plan.get("id_path"):
            raise ValueError(
                "Ubuntu 侧不支持 disk_config.target.id_path：subiquity 不识别这个键时"
                "**不会报错**，而是退回\"匹配第一块盘\"，在数据盘排在系统盘前面的机器上"
                "会直接抹掉数据盘（真机实测）。请改用 target.serial 或 target.model"
                "（不匹配时会明确报 matched no disk，属于安全失败），"
                "或改用 target.mode=name 显式给出盘名。")
        m = {}
        if plan["serial"]:
            m["serial"] = plan["serial"]
        if plan["model"]:
            m["model"] = plan["model"]
        if not m:
            raise ValueError(
                "Ubuntu 的 target.mode=match 需要至少一个匹配键：serial 或 model。"
                "注意 subiquity 的 serial 取 sysfs 的 /sys/block/sdX/device/serial，"
                "虚拟化环境（QEMU/virtio-scsi）下该属性为空，会导致 matched no disk（§5.45）。")
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
    # ★ curtin 的 disk.wipe 是**模式字符串**，不是布尔！
    #   传 `true` 会让整个 curtin install 失败：
    #     ValueError - wipe mode True not supported
    #     curtin/commands/block_meta.py disk_handler -> block.wipe_volume(disk, mode=info.get('wipe'))
    #   （§5.46 缺陷 4，真机取证）。"superblock" 是 curtin 的标准模式：
    #   清掉分区表/文件系统签名，不做整盘清零（正是重装系统想要的语义）。
    #   plan["wipe"]=False 时**不要**写这个键（而不是写 False —— 同一处类型问题）。
    if plan["wipe"]:
        disk["wipe"] = "superblock"
    # ★ grub_device 是**必需**的：curtin/subiquity 靠它知道把引导装到哪块盘。
    #   漏掉就报 "autoinstall config did not create needed bootloader partition" 并拒绝装机。
    #   这正是 §5.46 缺陷 3 的真正原因 —— 用 ~200 秒一轮的真机迭代（e_try_ptable.py）
    #   试出来的：先试过 bios_grub 分区、ptable=msdos，都不是；grub_device 才是。
    #   subiquity 自己生成的简单布局里一直带着这个键，手写 storage.config 时极易漏。
    disk["grub_device"] = True
    # 分区表：有 ESP ⇒ 必须 GPT（UEFI）；否则用 msdos(MBR) —— 与 RHEL 侧的
    # `bootloader --location=mbr` 保持一致，BIOS 下也就不需要额外的 bios_grub 分区。
    disk["ptable"] = "gpt" if any(
        p["mount"] == "/boot/efi" for p in plan["partitions"]) else "msdos"
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
        # **`size: "rest"` 不能直接发给 subiquity**（§5.45 真机实测）：
        # 它报 `'rest' is not valid input.` 然后整机装机失败。
        # curtin/subiquity 里"占满剩余空间"的写法是 **-1**。
        # 这是产品内部约定（RHEL 侧用 --grow）到各 OS 的翻译，不能原样透传。
        psize = str(p["size"] or "").strip().lower()
        size_val = -1 if psize in ("rest", "remaining", "grow", "-1") \
            else _human_size_to_bytes(p["size"])
        e = {"type": "partition", "id": p["id_sub"], "device": "disk0",
             "size": size_val}
        if _PART_FLAGS.get(p["mount"]):
            e["flag"] = _PART_FLAGS[p["mount"]]
        if p["fstype"] == BIOS_GRUB_FSTYPE:
            # BIOS + GPT 的引导分区：**只建分区 + 打标记，不能有 format 条目**
            # （它没有文件系统）。subiquity 认这个 flag 就不再报
            # "did not create needed bootloader partition"（§5.46 缺陷 3）。
            e["flag"] = "bios_grub"
        cfg.append(e)
        # LVM PV / RAID 成员 / bios_grub 都不建文件系统
        if p["vg"] or p["index"] in raid_members or p["fstype"] == BIOS_GRUB_FSTYPE:
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
                    "name": p["lv"],
                    # 同 partition：subiquity 不接受 "rest"，占满剩余空间要用 -1（§5.45 真机实测）
                    "size": (-1
                             if str(p["size"] or "").strip().lower() in
                             ("rest", "remaining", "grow", "-1")
                             else _human_size_to_bytes(p["size"]))})
        fid = _format(lv_id, p["fstype"] or ("swap" if p["mount"] == "swap" else "ext4"))
        if p["mount"] and p["mount"] != "swap":
            mounts.append((fid, p["mount"]))

    # 数据盘（规格 §2/§3.4）：只有 wipe=true 才动 —— 默认 wipe=false 时产物里一个字都不出现
    # （生产红线：没确认就不碰数据盘）。写法与 disk0 完全同构，只是挂载点由用户给。
    # _disk_plan 已经保证：走到这里的数据盘若给了 mount，wipe 必然是 true。
    for n, d in enumerate(plan["data_disks"]):
        if not d["wipe"]:
            continue
        did = "data%d" % n
        # 同目标盘：curtin 的 wipe 必须是模式字符串（"superblock"），传 True 会 ValueError。
        # ★ 外部审查 U1-F4（我复核确认）：**能不用盘名就不用** —— `/dev/sda` 这种名字
        #   在枚举顺序变化时会指向另一块物理盘，而这条 disk 条目带 wipe:"superblock"，
        #   等于"按猜的名字抹盘"。目标盘早就改用稳定键了（_ubuntu_disk_match），
        #   数据盘这里原来漏了。subiquity 的 disk 条目认顶层 serial（目标盘就用的它）；
        #   给了 serial 但不匹配时它会**明确报 matched no disk**（安全失败），
        #   而写 /dev/<name> 出错时会**静默抹错盘**。没有 serial 才退回 path。
        entry = {"type": "disk", "id": did}
        if d.get("serial"):
            entry["serial"] = d["serial"]
        else:
            entry["path"] = "/dev/" + d["name"]
        entry["wipe"] = "superblock"
        cfg.append(entry)
        if not d["mount"]:
            continue                      # 只清盘不建分区（与 RHEL 侧 clearpart 的语义一致）
        pid = "datap%d" % n
        # ★ 外部审查 U1-F1（我复核确认，**严重**）：主盘分区(825)与 LVM(866) 都把
        #   `rest` 转成了 -1，唯独数据盘这里直接发了字符串 "rest" ——
        #   而 subiquity 对 "rest" 会报 `'rest' is not valid input.` 整机中止。
        #   最糟的是顺序：curtin 先按上面的 disk 条目把数据盘 **wipe:sulperblock 抹掉**，
        #   之后才在这里失败 ⇒ 数据盘已经空了、系统也没装完。
        cfg.append({"type": "partition", "id": pid, "device": did, "size": -1})
        fid = _format(pid, d["fstype"] or "ext4")
        mounts.append((fid, d["mount"]))

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
        # ★ 2026-10-08：/home 由固定 10240 改成 `--size=1 --grow`（随盘增长）。
        # 为什么必须改：原来四项固定尺寸合计 20480+8192+10240+1024+512 = 40448 MiB ≈ 40.4 GB，
        # 而本项目测试机就是 30 GB 盘（PVE "30G" = 30720 MiB）⇒ 用 lvm 简写装机在分区阶段
        # 必然失败（我上一轮只能改用 direct 绕开，等于默认方案在小盘上不可用）。
        # 改后固定部分降到 30208 MiB ≈ 29.5 GiB，30 GB 盘可用，多出来的空间全部给 /home。
        # 注意顺序：`logvol /home` 必须是**最后一个** LV，anaconda 的 --grow 才会吃掉剩余空间。
        "logvol /home --vgname=vg0 --name=home --size=1 --grow --fstype=ext4\n"
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


def _rhel_custom_lines(plan, disk, dd_dev=None) -> list:
    """dd_dev：{数据盘下标: 设备令牌}。use_pre=True 时传 {"$ddev1": …}，
    让"要主动清除的数据盘"也走 %pre 反解出来的设备名，而不是写死的盘名（R2-H3）。"""
    dd_dev = dd_dev or {}

    def _dev(k, d):
        return dd_dev.get(k) or d["name"]
    """layout=custom 的 ks 行（规格 §3.3/§3.4）：part/volgroup/logvol/raid 全部 --ondisk=<disk>。

    **ignoredisk 只能有一条**（本次修复的实测结论）：pykickstart 的 F8_IgnoreDisk.parse 在
    --drives 与 --only-use 同时被置上时直接抛

        KickstartParseError: One of --drives or --only-use must be specified for ignoredisk command.

    实测 pykickstart 3.78 / RHEL9：先 `ignoredisk --only-use=sda` 再 `ignoredisk --drives=sdc`，
    **第二行即解析失败**，整份 ks 读不进去（anaconda 会停在 "An error occurred during reading
    the kickstart file"）。旧实现正是这个形状（wipe=false 的数据盘会再发一条 --drives=sdc），
    而 wipe=false 又是数据盘的默认值 —— 也就是说"配了数据盘"的 RHEL custom 模板生成出来的
    ks 根本装不了。现在只发一条 --only-use：
      · 要用的盘（目标盘 + 显式 wipe=true 的数据盘）全部列进去；
      · 不用的盘（wipe=false 的数据盘）**不列** —— --only-use 的语义就是"只有列出的盘可用"，
        没列出的盘 anaconda 一概不碰（pykickstart: "only disks listed here will be used
        during installation"），比"--drives= 再声明一次"更强也更省事。
    """
    # ★ 功能 G：`--only-use`（哪些盘**可见**）与 `clearpart --drives`（哪些盘**会被清**）
    #   从此是**两个集合** —— 挂既有文件系统的数据盘必须"可见"（否则 anaconda 的函数树里
    #   根本没有它的分区，`--onpart=UUID=…` 找不到设备），但**绝不能出现在 clearpart 里**
    #   （那会把要保留的数据抹掉）。改动前这两个集合是同一个 `used`，直接加进去就会抹盘。
    only_use = [disk] + [_dev(k, d) for k, d in enumerate(plan["data_disks"])
                         if d["wipe"] or d.get("existing")]
    clear_drives = [disk] + [_dev(k, d) for k, d in enumerate(plan["data_disks"]) if d["wipe"]]
    lines = ["ignoredisk --only-use=" + ",".join(only_use)]
    if plan["wipe"]:
        lines.append("clearpart --drives=" + ",".join(clear_drives) + " --all --initlabel")
    else:
        lines.append("# disk_config.wipe=false：不执行 clearpart（沿用磁盘上已有分区表）")
    for d in plan["data_disks"]:
        if d.get("existing"):
            lines.append("# 数据盘 " + (d["name"] or "(未命名)") + " (existing_"
                         + ("uuid" if d["existing"].startswith("UUID=") else "label")
                         + "=" + d["existing"].split("=", 1)[1] + ")：**挂载既有文件系统，"
                         "不新建分区、不格式化** —— 已加入 ignoredisk --only-use（否则装不到），"
                         "但**不在** clearpart --drives 里（保存数据）")
        elif not d["wipe"]:
            # 生产红线：没确认就不动数据盘。
            # 这里**故意不再发 `ignoredisk --drives=<d>`**：第二条 ignoredisk 会让 pykickstart
            # 直接报 "One of --drives or --only-use must be specified"（见函数开头），
            # 而"没被 --only-use 列出的盘一律不碰"已经覆盖了这条语义。
            lines.append("# 数据盘 " + d["name"] + " (disk_config.wipe=false)：只声明不碰，"
                         "已由上面的 ignoredisk --only-use 排除")

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
        if p["fstype"] == BIOS_GRUB_FSTYPE:
            # BIOS+GPT 的引导分区（§5.46 缺陷 3）。kickstart 侧的对应物就是
            # `part biosboot`（anaconda 的 mntpoint 明确接受 biosboot，见下面那段注释），
            # 固定 1MiB、无文件系统 —— 用户填的 size 在这里被忽略（biosboot 分区大小没有意义）。
            lines.append("part biosboot --fstype=biosboot --size=1 --ondisk=" + disk)
            continue
        ident = pv_of.get(p["index"]) or raid_members.get(p["index"]) or p["id_ks"]
        is_pv = p["index"] in pv_of
        is_raid = p["index"] in raid_members
        if not is_pv and not is_raid and not p["mount"]:
            # kickstart 的 `part` 行首字段是 <mntpoint>，anaconda/pykickstart 只认
            #   /<path> | swap | raid.<id> | pv.<id> | btrfs.<id> | biosboot
            # （pykickstart/commands/partition.py 的 mntpoint 帮助文本；pykickstart/options.py
            #  的 mountpoint() 只对 "/" 开头的值做 normpath，其余原样透传 —— 它不做任何校验，
            #  所以 `part part.01 ...` 能过解析、却会在格式化/挂载阶段出问题）。
            # part.01 是**我们自己的**下标标识，anaconda 既不认识它、也不会把它当"无挂载点"。
            # 因此这种分区在 kickstart 里无法安全表达 —— 拒绝，绝不发一条我们不确定的 ks 行。
            raise ValueError(
                "disk_config.partitions[%d].mount：没有挂载点、又不是 PV(vg/lv)、也不是 RAID 成员的"
                "分区无法用 kickstart 表达（anaconda 只接受 /<path>|swap|raid.<id>|pv.<id>|btrfs.<id>|biosboot）；"
                "请给它挂载点，或把它配成 PV / RAID 成员" % p["index"]
            )
        opts = ["--ondisk=" + disk, _rhel_ondisk(p["size"])]
        if not is_pv and not is_raid:
            # PV / RAID 成员自身不建文件系统：fstype 属于 logvol / raid 那一行
            if p["mount"] == "/boot/efi":
                opts.insert(0, "--fstype=efi")
            elif p["fstype"]:
                opts.insert(0, "--fstype=" + p["fstype"])
        lines.append("part " + (ident if (is_pv or is_raid) else p["mount"])
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

    for k, d in enumerate(plan["data_disks"]):
        if d.get("existing") and d["mount"]:
            # ★ 功能 G：挂**已有**文件系统。按 pykickstart 原文，`--noformat` + `--onpart=`
            #   是"不格式化、用既有分区"的**唯一**写法（见 _existing_fs_id 上方的引文）。
            #   用 UUID/LABEL 而不是 /dev/sdX1：枚举顺序与分区编号都不用管（§5.42 同口径）。
            #   `--fstype` 只在运维显式给了才发（文档没说该组合下它是否必需，所以给一个
            #   可用的表达方式，也**不**替运维猜）。
            fs = (" --fstype=" + d["fstype"]) if d["fstype"] else ""
            lines.append("part %s%s --onpart=%s --noformat"
                         % (d["mount"], fs, d["existing"]))
            # 官方明写的边界（pykickstart 的 clearpart 一节原文）："If the clearpart command
            # is used, then the --onpart command cannot be used on a logical partition."
            # 本模板上面就发了 clearpart ⇒ 既有分区必须是**主分区**；逻辑分区挂不上。
            # 生成期无法知道那块盘上的分区是主分区还是逻辑分区（拿不到现场信息），
            # 所以把边界写进产物里，让人在装之前就能看到。
            lines.append("# ⚠ 上面这一行要求 %s 是**主分区**：本模板用了 clearpart，而 pykickstart"
                         " 原文写明" % d["mount"])
            lines.append("#   \"If the clearpart command is used, then the --onpart command cannot"
                         " be used on a logical partition.\"（逻辑分区挂不上）")
            continue
        if d["wipe"] and d["mount"]:
            # R2-H3：要**主动分区/格式化**的数据盘，盘名必须来自 %pre 的反解结果，
            # 不能写死 —— 否则枚举顺序一反转就格式化到别的盘上。
            lines.append("part %s --fstype=%s --size=1 --grow --ondisk=%s"
                         % (d["mount"], d["fstype"] or "ext4", _dev(k, d)))
    return lines


_SIZE_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
_SIZE_UNITS_10 = {"K": 1000, "M": 1000 ** 2, "G": 1000 ** 3, "T": 1000 ** 4}


def _size_to_bytes(v) -> int:
    """把 "30G" / "500M" / "32212254720" 转成字节数，供 %pre 与 lsblk 的 SIZE 做等值比对。

    裸 G/M/T 按**二进制**解释（G = GiB）—— 与 PVE/`qm` 的 size=30G 语义一致；
    显式写 GB/MB/TB 时按十进制。写成 "30GiB" 也认。
    """
    s = str(v or "").strip().upper().replace(" ", "")
    if not s:
        return 0
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)([KMGT]?)(I?B)?$", s)
    if not m:
        return 0
    num, unit, suffix = float(m.group(1)), m.group(2), (m.group(3) or "")
    if unit and suffix == "B":
        return int(num * _SIZE_UNITS_10[unit])
    return int(num * _SIZE_UNITS[unit])


def _safe_matcher_value(v, what, i) -> str:
    """洗掉 serial/wwid 里的危险字符 —— 它们会被拼进 %pre 的 shell/awk 字符串。

    只放行字母数字与 . _ : + -（真实序列号/wwid 都在这个集合里）。不放行引号、
    反斜杠、$、空格，否则又是一条配置注入面（与 §5.9 的 os/netplan_renderer 同类）。
    """
    s = str(v or "").strip()
    if not re.match(r"^[A-Za-z0-9._:+-]+$", s):
        raise ValueError(
            "disk_config.data_disks[%d].%s 含非法字符（只允许字母数字与 . _ : + -）：%r"
            % (i, what, v))
    return s


def _data_disk_matcher_specs(plan) -> list:
    """把 data_disks 转成"稳定属性"匹配串；只给了设备名就报错（拒绝生成）。

    为什么必须这样（RUNBOOK-STATE §5.42，真机三台并发实测，数据丢失级）：
    模板写 data_disks=[{name:"sda"}] + target.mode=auto 时，旧实现拿**设备名**当排除集，
    而 `sdX` 由内核探测顺序决定。同一套虚机配置（scsi0=30G 数据盘、scsi1=20G 系统盘）：
        VM142/VM144:  0:0:0:0 -> sda(30G)   0:0:0:1 -> sdb(20G)
        VM143:        0:0:0:0 -> sdb(30G)   0:0:0:1 -> sda(20G)   ← 反了
    于是 `excl='sda'` 排除掉的是**系统盘**，target 落到数据盘上，
    紧接着 `clearpart --drives=$target --all --initlabel` **把数据盘抹了**，系统盘一根没动。
    VM143 同一台机器两次启动还出现过两种顺序 —— 这是**非确定性**的，单机验证永远碰不到。

    ⇒ 设备名不是身份，只是备注；排除必须靠 size / serial / wwid。
    返回**按声明分组**的条件（每组 = 一个 data_disks 条目，组内取"或"）。
    为什么要分组（MiMo R2 的 H1）：原先把各条声明的条件拍平成一个 OR 列表，
    再用"命中盘数 == 0"判护栏 —— 那只能证明"至少有一块盘被排除"，
    证不了"每个声明都命中"。声明两块只命中一块时护栏放行，没命中的那块数据盘
    就会被当成目标盘抹掉。分组后可以逐组要求"必须命中"。
    """
    groups, bad = [], []
    for i, d in enumerate(plan.get("data_disks") or []):
        one = []
        if d.get("size"):
            b = _size_to_bytes(d["size"])
            if b <= 0:
                raise ValueError(
                    "disk_config.data_disks[%d].size 无法解析：%r（形如 30G / 500M / 32212254720）"
                    % (i, d["size"]))
            one.append("s%d" % b)
        # serial/wwid 会被拼进 %pre 的 shell/awk 字符串，必须先洗掉引号等危险字符
        # （否则就是又一条配置注入面）。
        if d.get("serial"):
            one.append("n" + _safe_matcher_value(d["serial"], "serial", i))
        if d.get("wwid"):
            one.append("w" + _safe_matcher_value(d["wwid"], "wwid", i))
        if not one:
            bad.append(d.get("name") or ("[%d]" % i))
        groups.append(one)
    if bad:
        raise ValueError(
            "disk_config.data_disks 只给了设备名（%s）—— 拒绝生成安装配置。"
            "设备名 sda/sdb 由内核探测顺序决定，同一台机器两次启动都可能互换；"
            "拿它当排除集会把系统盘排除掉、让安装落到数据盘上并把它抹掉（真机实测过，三台里中一台）。"
            "请改用稳定属性之一：size（如 \"30G\"）、serial、wwid。" % "、".join(bad))
    return groups



# ── %pre 里解析 lsblk 的 awk 前置段 ──
# 为什么不能用 `lsblk -dn -o NAME,TYPE,RM,TRAN,SIZE | awk '$2=="disk" && $3=="0" && $4!="usb" ...'`
# （旧实现）：那是**按列位置**取值，任何一列为空都会让后面的列整体左移。TRAN 为空时
# （virtio-blk 实测就是空）$4 变成 SIZE、$5 变空，`$5>=<字节>` 恒为假 → 一块盘也选不出来
# （装机直接中止）；反过来 SIZE 为空时 $5 会取到别的字段，可能选中**错误**的盘然后 clearpart。
# 现在改用 `-P/--pairs`（每行 KEY="value"，空值也保留成 KEY=""），用 gv() 按 key 取值，
# 与列位置/空列彻底无关。gv 用 index() 找 ` KEY="`，所以带空格的 MODEL 也能完整取出。
_LSBLK_AWK_PRELUDE = (
    "function gv(l, k,   p, q) {"
    " p = index(l, \" \" k \"=\\\"\"); if (p == 0) return \"\";"
    " p += length(k) + 3; q = index(substr(l, p), \"\\\"\");"
    " return (q == 0) ? \"\" : substr(l, p, q - 1) } "
    # dm = 数据盘稳定属性匹配串。**分组**：';' 分组（一个 data_disks 条目一组），
    # 组内 '|' 取或。条件形如 s<字节> / n<序列号> / w<wwid>。
    "BEGIN { npg = split(dm, grp, \";\") } "
    "function dgrp(L, gi,   n, dc, i, c, k, v, sz, d) {"
    " n = split(grp[gi], dc, \"|\");"
    " for (i = 1; i <= n; i++) { c = dc[i]; if (c == \"\") continue;"
    " k = substr(c, 1, 1); v = substr(c, 2);"
    # SIZE 用**容差**比对：真实物理盘容量从来不是整数（"300GB" 盘 ≈ 3000592982016 B），
    # 写 "300G" 永远精确匹配不上；而用户也常把 GB/MB 当二进制写。
    # 因此：声明值与盘的字节数相差 <=2%（十进制/二进制两种解释都试）即算命中。
    " if (k == \"s\") { sz = gv(L, \"SIZE\") + 0; d = v + 0;"
    "   if (d > 0 && sz > 0 && (sz - d) / d <= 0.02 && (d - sz) / d <= 0.02) return 1;"
    "   if (d > 0 && sz > 0 && (sz - d * 0.9537) / (d * 0.9537) <= 0.02"
    "       && (d * 0.9537 - sz) / (d * 0.9537) <= 0.02) return 1 }"
    # 序列号/WWID 大小写不敏感（用户从文档抄的大写写法不该因此不命中）
    " if (k == \"n\" && v != \"\" && tolower(gv(L, \"SERIAL\")) == tolower(v)) return 1;"
    " if (k == \"w\" && v != \"\" && tolower(gv(L, \"WWN\")) == tolower(v)) return 1 }"
    " return 0 } "
    "function dmatched(L,   gi) {"
    " for (gi = 1; gi <= npg; gi++) if (dgrp(L, gi)) return 1; return 0 } "
)
# 选盘主体：只认整盘、非可移动、非 usb、非 RAM 盘、容量达标、且**不是**数据盘。
# zram 必须显式排除：`lsblk` 把 /dev/zram0 报成 TYPE=disk、RM=0、TRAN 空，
# 它**能通过之前的所有过滤条件**；之所以一直没出事故，只是因为 "zram0" 在字母序里
# 排在 nvme*/vd*/sd* 之后（靠 sort|head -1 侥幸）。真机上少一块盘就会选中内存盘。
# 这是新加的 %pre 留痕第一次跑就暴露出来的。
_AWK_PICK_BY_SIZE = (
    "{ L = \" \" $0;"
    " if (gv(L, \"TYPE\") != \"disk\") next;"
    " if (gv(L, \"RM\") != \"0\") next;"
    " if (gv(L, \"TRAN\") == \"usb\") next;"
    " nm = gv(L, \"NAME\");"
    " if (nm == \"\" || nm ~ /^(zram|ram|loop|sr|dm-|md)/) next;"
    " if (dmatched(L)) next;"
    " if (min > 0 && gv(L, \"SIZE\") + 0 < min) next;"
    " print nm }"
)
# match 模式：按 SERIAL / MODEL 精确命中。
_AWK_PICK_BY_KEY = (
    "{ L = \" \" $0;"
    " if (gv(L, \"SERIAL\") == s || gv(L, \"MODEL\") == s) {"
    " nm = gv(L, \"NAME\");"
    " if (nm != \"\" && !dmatched(L)) print nm } }"
)
# 按"第 gi 个数据盘声明"的条件反解出它的设备名（MiMo R2 的 H3）：
# 产物里凡是**要主动清除/格式化**某块数据盘的地方，都不能写死盘名 ——
# 枚举顺序一反转就会作用到别的盘上（与 §5.42 同类，只是从"排除"变成"清除"）。
_AWK_PICK_BY_GROUP = (
    "{ L = \" \" $0;"
    " if (gv(L, \"TYPE\") != \"disk\") next;"
    " if (gv(L, \"RM\") != \"0\") next;"
    " nm = gv(L, \"NAME\");"
    " if (nm == \"\" || nm ~ /^(zram|ram|loop|sr)/) next;"
    " if (dgrp(L, gi)) print nm }"
)
# 选盘留痕 + **逐声明护栏**（一次 awk 同时做，靠退出码传递判定，避免解析数字）：
# 打出磁盘清单（命中的标 [数据盘]），逐组统计命中数，任一**声明**一块都没命中就 exit 3。
# 为什么用退出码而不是"回声一个数字再 -eq 0"（MiMo R2 的 M3）：输出为空或非数字时
# `[ "" -eq 0 ]` 在 bash 里报错返回非 0 → if 判假 → **跳过中止分支继续装**（fail-open）。
# 这块命令本身就是安全护栏，必须 fail-closed。
_AWK_LIST_AND_CHECK = (
    "{ L = \" \" $0;"
    " if (gv(L, \"TYPE\") != \"disk\") next;"
    " nm = gv(L, \"NAME\");"
    # zram/loop/sr 这类**不是候选盘**，它们在清单里标出来但不算"数据盘"，
    # 也不参与选盘；否则 zram0 会被误当成一块可用目标盘（它确实通过了旧过滤条件）。
    " if (nm ~ /^(zram|ram|loop|sr)/) {"
    "   printf \"PXE-DISK: [ 跳过 ] %-9s size=%-14s (非候选盘)\\n\", nm, gv(L, \"SIZE\"); next }"
    " m = dmatched(L);"
    " if (m) { dmc++; for (gi = 1; gi <= npg; gi++) if (dgrp(L, gi)) gcnt[gi]++ }"
    " printf \"PXE-DISK: %s %-9s size=%-14s serial=%s wwn=%s\\n\","
    " (m ? \"[数据盘]\" : \"[  --  ]\"), nm, gv(L, \"SIZE\"),"
    " gv(L, \"SERIAL\"), gv(L, \"WWN\") }"
    " END { bad = 0;"
    " for (gi = 1; gi <= npg; gi++) if (gcnt[gi] + 0 == 0) {"
    "   printf \"PXE-DISK: !! 第 %d 个数据盘声明一块都没命中\\n\", gi; bad++ }"
    " printf \"PXE-DISK: 命中数据盘 %d 块；声明 %d 个，未命中 %d 个\\n\", dmc + 0, npg, bad;"
    " exit (bad > 0 ? 3 : 0) }"
)


def _rhel_pick_target_lines(plan) -> list:
    """%pre 里现场挑目标盘（规格 §3.1）：ks 没有"自动选盘"原语，只能脚本化。

    排除集只认**稳定属性**（size/serial/wwid），不认设备名 —— 见 §5.42：
    设备名 sda/sdb 由内核探测顺序决定，同一台机器两次启动都可能互换，
    拿它做排除集会排掉系统盘、把安装落到数据盘上并抹掉它。
    """
    dm = ";".join("|".join(g) for g in _data_disk_matcher_specs(plan))
    lines = [
        "set -o pipefail",     # 下面那条 lsblk|awk 是安全护栏，管道任一段失败都要算失败
        "# 选盘留痕：磁盘清单 + 命中情况 + 最终 target 全部打到串口（console）与 %pre 日志。",
        "# 为什么值得占这几行：§5.42 那类事故（三台里中一台）事后只能靠这些日志定位。",
        "pxelog() { echo \"PXE-DISK: $*\"; echo \"PXE-DISK: $*\" > /dev/console 2>/dev/null || true; }",
        "pxelog '开始选盘；数据盘稳定匹配串=%s'" % (dm or "<无>"),
        "pxelog '磁盘清单（[数据盘]=按稳定属性命中，会被排除）- - - - - - - - - - - - -'",
    ]
    if dm:
        # 声明了数据盘 ⇒ **逐个声明**都必须在真机上命中，否则中止。
        # 判据用 awk 的退出码（3=有声明未命中），不再回声一个数字回来做 `-eq` ——
        # 数字为空/非数字时 `[ "" -eq 0 ]` 会报错并让 if 判假，等于护栏失效（fail-open）。
        lines += [
            "if ! lsblk -bdnP -o NAME,TYPE,RM,TRAN,SIZE,SERIAL,WWN"
            " | awk -v dm='%s' '%s' | tee /dev/console; then" % (dm, _LSBLK_AWK_PRELUDE + _AWK_LIST_AND_CHECK),
            "  pxelog '!! 有数据盘声明一块都没命中 —— 拒绝继续（排除集不完整会把数据盘当目标盘抹掉）'",
            "  pxelog '!! 请核对 data_disks 的 size / serial / wwid 是否与这台机器相符；装机中止'",
            "  echo 'PXE: 数据盘稳定匹配失败，装机中止（拒绝在排除集不完整的情况下选盘）' >&2; exit 1",
            "fi",
        ]
    else:
        lines.append("lsblk -bdnP -o NAME,TYPE,RM,TRAN,SIZE,SERIAL,WWN"
                     " | awk -v dm='' '%s' | tee /dev/console"
                     % (_LSBLK_AWK_PRELUDE + _AWK_LIST_AND_CHECK))
    if plan["mode"] == "match":
        # 与本文件里 data_disks 的 serial/wwid 同样处理：这个值会拼进 %pre 的 shell 单引号串，
        # 不洗就是一条 root 权限的配置注入面（MiMo R2 的 M1）。
        if not (plan["serial"] or plan["model"]):
            # 空 key 会让 `awk -v s=''` 命中**任意 SERIAL/MODEL 为空的盘**（第一块）。
            # 这是"静默选错盘"的失效方式 —— 生产上不可接受，直接拒绝生成。
            raise ValueError(
                "disk_config.target.mode=match 在 RHEL 侧需要 serial 或 model："
                "两者都为空时 %pre 会用一个空匹配串，命中任意盘（静默装错盘）。")
        key = _safe_matcher_value(plan["serial"] or plan["model"], "target.serial/model", 0)
        lines.append("target=$(lsblk -dnP -o NAME,SERIAL,MODEL,WWN | "
                     "awk -v s='%s' -v dm='%s' '" % (key, dm))
        lines.append(_LSBLK_AWK_PRELUDE + _AWK_PICK_BY_KEY + "' | sort | head -1)")
    else:
        # 恒用 -b（字节）比较：未给下限时 min=0，等价于"只看是不是整盘/可移动/usb"。
        lines.append("target=$(lsblk -bdnP -o NAME,TYPE,RM,TRAN,SIZE,SERIAL,WWN | "
                     "awk -v min=%d -v dm='%s' '" % (int(plan["min_size_gb"]) * 1024 ** 3, dm))
        lines.append(_LSBLK_AWK_PRELUDE + _AWK_PICK_BY_SIZE + "' | sort | head -1)")
    # ★ 对每个 **wipe=true** 的数据盘，用**它自己那条声明的稳定条件**反解出设备名。
    # 为什么（MiMo R2 的 H3）：`ignoredisk --only-use=` / `clearpart --drives=` /
    # `part --ondisk=` 这几处是**主动清除**动作，写死 `sdb` 的话，枚举顺序一反转
    # 就会清到另一块盘上 —— 与 §5.42 同类，只是从"该排除的没排除"变成"主动清错盘"。
    # 反解不出来就中止：宁可不装，绝不按猜的盘去格式化。
    groups = _data_disk_matcher_specs(plan)
    for i, d in enumerate(plan["data_disks"]):
        # ★ 功能 G：**挂既有文件系统**的数据盘也要反解设备名 —— 它必须出现在
        #   `ignoredisk --only-use=` 里（否则 anaconda 看不到这块盘上的既有分区，
        #   `--onpart=UUID=…` 就找不到设备）。它**不**进 clearpart，所以只是"可见"。
        if not (d["wipe"] or d.get("existing")):
            continue
        what = "wipe=true" if d["wipe"] else "existing_" + (
            "uuid" if d["existing"].startswith("UUID=") else "label")
        g = "|".join(groups[i])
        lines += [
            "ddev%d=$(lsblk -bdnP -o NAME,TYPE,RM,TRAN,SIZE,SERIAL,WWN | "
            "awk -v gi=%d -v dm='%s' '%s' | sort | head -1)"
            # ★ 外部审查 U1-F3（我复核确认）：`dm` 这里传的是**单组**条件
            #   （`"|".join(groups[i])`，组间分隔符是 ';'，而此处只有一个组），
            #   awk 的 `BEGIN { npg = split(dm, grp, ";") }` 于是 npg=1；
            #   而 gi 原来传 i+1 ⇒ 第 2 个及以后的 wipe 数据盘查 grp[2..] 全空、
            #   dgrp 恒返回 0 ⇒ ddevN 为空 ⇒ 下面的护栏永远 exit 1，
            #   "两条数据盘"这种常见配置直接装不下去。**单组就必须传 gi=1**。
            % (i + 1, 1, g, _LSBLK_AWK_PRELUDE + _AWK_PICK_BY_GROUP),
            "if [ -z \"$ddev%d\" ]; then" % (i + 1),
            "  pxelog '!! 第 %d 个数据盘（%s）按稳定条件找不到设备 —— 拒绝继续"
            "（按猜的盘格式化会清错盘；挂既有文件系统也会挂错盘）'" % (i + 1, what),
            "  echo 'PXE: 待格式化的数据盘无法按稳定条件定位，装机中止' >&2; exit 1",
            "fi",
            "pxelog \"第 %d 个数据盘（%s）解析为 $ddev%d\"" % (i + 1, what, i + 1),
        ]
    lines.append("pxelog \"选定目标盘 target='${target:-<空>}'\"")
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
        # use_pre 时把"要主动清除的数据盘"与"要挂既有文件系统的数据盘"都换成
        # %pre 反解出来的 $ddevN（R2-H3 / 功能 G：后者要进 ignoredisk --only-use）
        dd_dev = ({i: "$ddev%d" % (i + 1) for i, d in enumerate(plan["data_disks"])
                   if d["wipe"] or d.get("existing")} if use_pre else None)
        body = _rhel_custom_lines(plan, disk, dd_dev)
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
        # 回归红线 §5.1：合法盘名的输出必须与改造前逐字一致。历史键 disk 会被拼进
        # ks 的 --drives=<disk>（`clearpart --drives=` 是**逗号分隔的多盘**语法），
        # 所以这里必须用盘名白名单 _safe_ident，而不是只拦控制字符的 _safe_line：
        #   disk="sda,sdb" 在旧实现下生成 `clearpart --drives=sda,sdb --all --initlabel`
        #   → 把第二块（数据）盘一起抹掉。合法盘名（sda/nvme0n1/…）恒等，输出不变。
        disk = dc.get("disk")
        if disk is not None:
            disk = _safe_ident(disk, "disk_config.disk")
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
    # ── 装完能 SSH（本单元）──
    # 两态都显式写 PermitRootLogin（不依赖发行版默认：Ubuntu 默认 prohibit-password、
    # 老镜像默认过松）。sed + grep 兜底，保证无论镜像主配置里有没有注释行，
    # 最终文件里都有一行我们指定的值。
    for _cmd in _sshd_permit_root_lines(bool(c.allow_root)):
        lines.append("    - " + json.dumps(
            "curtin in-target --target=/target -- bash -c " + shlex.quote(_cmd)))
    if c.allow_root:
        # Ubuntu 的 root 账号默认锁定（/etc/shadow 无口令）——只放开 PermitRootLogin
        # 还是进不去。与 RHEL 的 rootpw 同一条回退链：root_password 优先，其次管理员口令；
        # 存的是 sha512 **密文**（chpasswd -e），不在生成物里落明文。
        lines.append("    - " + json.dumps(
            "curtin in-target --target=/target -- bash -c " + shlex.quote(
                "echo " + shlex.quote("root:" + _hash_pw(c.root_password or c.admin_password))
                + " | chpasswd -e")))
    # sudo 免密（默认是）：显式落一个 sudoers.d 覆盖文件（subiquity 默认就给
    # 安装期用户免密，但"默认行为"对存量镜像/后续版本是口头承诺，写出来才算数）。
    # 关掉时显式**摘除**所有 NOPASSWD 条目 —— sudo 的多条规则是"最宽者胜"，
    # 事后叠加一条要密码的规则抵消不了免密条目，只能删。
    if c.sudo_nopasswd:
        lines.append("    - " + json.dumps(
            "curtin in-target --target=/target -- bash -c " + shlex.quote(
                "echo " + shlex.quote(admin + " ALL=(ALL) NOPASSWD:ALL")
                + " > /etc/sudoers.d/90-opstk-" + admin
                + " && chmod 440 /etc/sudoers.d/90-opstk-" + admin)))
    else:
        lines.append("    - " + json.dumps(
            "curtin in-target --target=/target -- bash -c " + shlex.quote(
                "grep -rls NOPASSWD /etc/sudoers.d/ 2>/dev/null | xargs -r rm -f")))
    # ── 装完回调（防重复抹盘，2026-10-08 真机实证）──
    # 仅按装机记录展开（done_url 非空）时追加：装机收尾回调服务端，把该机的装机
    # 记录标记 installed —— 之后 dnsmasq 不再给这台机器下发自动装机菜单。
    # **不**经 curtin in-target：curl 在 live ISO（安装环境）里现成，target 最小装
    # 未必有；这条命令本来就该在安装环境里跑（跑完紧接着就是 reboot）。
    # done_url 已过 _safe_done_url 白名单（无引号/$/分号/空格），双引号里安全；
    # || true 保证回调失败绝不把装好的系统判成装机失败（与上面 systemctl 同理）。
    _done = _safe_done_url(c.done_url)
    if _done:
        lines.append("    - " + json.dumps(
            "curl -s -m 10 -X POST " + chr(34) + _done + chr(34) + " >/dev/null 2>&1 || true"))
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
        # 历史键 disk 先过 _safe_ident（盘名白名单）：它直接进 --drives=/--ondisk=/
        # --boot-drive=，而 --drives= 是**逗号分隔的多盘**语法 —— 旧实现只拦控制字符，
        # disk="sda,sdb" 会生成 `clearpart --drives=sda,sdb --all --initlabel`，
        # 把数据盘 sdb 也抹掉。合法盘名恒等，故输出不变。
        disk = _safe_ident(disk, "disk_config.disk") if disk is not None else "sda"
        parts = _rhel_layout_lines(c.disk_scheme, disk)
        # §5.14 修复（用户已签核"重新基线化 golden + 重做真机验收"）：
        # 本分支的 part 行**大多没有 --ondisk**（/boot/efi、/boot、pv.01、swap 都没有），
        # 而 `clearpart --drives=<disk>` 只说明"清哪块盘"，**并不约束 part 落在哪块盘** ——
        # 多盘机器上这些分区可能被 anaconda 放到别的盘上（装坏或毁数据）。
        # `ignoredisk --only-use=<disk>` 的语义就是"本次安装只使用这些盘"（pykickstart 原文），
        # 正好把无 --ondisk 的 part 行钉在目标盘上。
        # 注意：一份 ks 里**只能有一条 ignoredisk**（两条是 anaconda 硬解析错误），
        # 本分支原本一条都没有，所以这里加一条是安全的。
        disk_lines = ["ignoredisk --only-use=" + disk,
                      parts.rstrip(), "bootloader --location=mbr --boot-drive=" + disk]
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

    pkgs = list(c.extra_packages or [])
    if not pkgs:
        pkgs = ["vim", "net-tools", "bash-completion", "tar", "wget", "curl"]
    # 配了公钥就必须保证 sshd 在装（与 Ubuntu 侧"配了公钥自动补 openssh-server"
    # 同一条规则；RHEL 最小装通常自带 openssh-server，显式列出只是把"能 SSH"
    # 从概率变成确定，不改变其它包的选择）。
    if c.ssh_keys and "openssh-server" not in pkgs:
        pkgs.append("openssh-server")

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
    # %post 段（以 root 执行）：固化 root 登录策略 + sudo 免密 + 注入公钥。
    # 既有实现只在 ssh_keys/post_script 时输出 %post；本单元把"装完能 SSH"变成
    # 默认产物 —— root 登录两态与 sudo 免密默认值都是确定性行为，所以 %post
    # 常驻（最小段 = PermitRootLogin 两行）。新增字段为默认值时多出的行已随
    # golden 重新基线化（见 tests/test_pxe.py 的 sha256 注释）。
    L.append("")
    L.append("%post --interpreter=/bin/bash")
    # root 直登策略：默认 no（用户已定默认否）。RHEL 系安装器建的是普通管理员，
    # "装完能 SSH"的默认路径是 管理员 + sudo，root 直登必须显式打开才可用。
    for _cmd in _sshd_permit_root_lines(bool(c.allow_root)):
        L.append(_cmd)
    if c.sudo_nopasswd:
        # wheel 组默认 sudo 要密码（%wheel ALL=(ALL) ALL）—— 免密由这个覆盖文件给。
        # sudoers.d 文件名里不能有 '.'（sudo 会忽略带点的文件），admin 白名单里没有 '.'。
        L.append("echo " + shlex.quote(admin + " ALL=(ALL) NOPASSWD:ALL")
                 + " > /etc/sudoers.d/90-opstk-" + admin)
        L.append("chmod 440 /etc/sudoers.d/90-opstk-" + admin)
    for k in c.ssh_keys or []:
        # 原实现用 chr(39)+k+chr(39) 手工拼单引号：key 里含单引号即可逃出引号再注入命令。
        # 改用 shlex.quote（对正常公钥是恒等变换，见 NC1 的恒等性论证）。
        L.append("mkdir -p /home/" + admin + "/.ssh && echo "
                 + shlex.quote(str(k)) + " >> /home/" + admin + "/.ssh/authorized_keys")
    if c.ssh_keys:
        L.append("chown -R " + admin + ":" + admin + " /home/" + admin + "/.ssh")
    if c.post_script:
        L.append(c.post_script)
    # ── 装完回调（防重复抹盘，2026-10-08 真机实证）──
    # 仅按装机记录展开（done_url 非空）时追加：装机收尾回调服务端，把该机的
    # 装机记录标记 installed —— 之后 dnsmasq 不再给这台机器下发自动装机菜单，
    # 重启走网卡引导也不会被重装。done_url 已过 _safe_done_url 白名单
    # （无引号/$/分号/空格），放在 shell 双引号里是安全的；|| true 保证回调
    # 失败（网络没通、服务端重启中等）绝不把装好的系统判成装机失败。
    _done = _safe_done_url(c.done_url)
    if _done:
        L.append("curl -s -m 10 -X POST " + q + _done + q + " >/dev/null 2>&1 || true")
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


# 装完回调 URL 的字符白名单：http(s):// 加常规 URL 字符（字母数字与 :/._%~?=&-）。
# 为什么比 _safe_line 严：done_url 会被拼进 %post / late-commands 的 shell **双引号**
# 里（`curl -s -m 10 "<done_url>" >/dev/null 2>&1 || true`）—— 引号、$、反引号、
# 分号、空格都会改变 shell 语法结构，而 _safe_line 只拦控制字符，挡不住这些。
_DONE_URL_RE = re.compile(r"https?://[0-9A-Za-z:/._%~?=&-]+")


def _safe_done_url(v) -> str:
    """done_url 白名单校验；空值放行（空串 = 该机不带回调，模板级产物恒如此）。

    D7 第 3 层的同一份思路：不信任任何调用方 —— api 层拼的 URL 也要在这里
    再体检一次，注入不进 %post / late-commands。
    """
    s = _safe_line(v, "done_url").strip()
    if not s:
        return ""
    if not _DONE_URL_RE.fullmatch(s):
        raise ValueError(
            "done_url 不合法 " + repr(s)
            + "：只允许 http(s):// 加常规 URL 字符（字母数字与 :/._%~?=&-）"
        )
    return s


# ===== iPXE 菜单 =====

def _answer_base(c) -> str:
    """应答/引导脚本的 URL 根：answer_root 优先，留空则回落到 http_root（=改造前行为）。

    **媒体绝不能走这里**：kernel_path / initrd_path / iso_url 拼的是 http_root
    （扁平路径 /srv/opstk/pxe-web/<os>/<ver>/），本函数只用于
    user-data / ks.cfg / iPXE 菜单这些"每个模板都同名、需要按模板隔离"的产物。
    历史教训见 PxeConfig.answer_root 的注释（把 http_root 整体隔离 → 媒体 404 → 回退）。
    """
    base = _safe_line(c.answer_root, "answer_root").strip()
    if not base:
        base = _safe_line(c.http_root, "http_root")
    return base.rstrip("/")


# 未登记机器默认菜单的标记行。单测靠它钉住"默认菜单不是某次部署的装机菜单"。
UNREGISTERED_DEFAULT_MARK = "# opstk-unregistered-default"


def _unregistered_default_menu() -> str:
    """没有被任何模板认领的机器拿到的默认菜单：**绝不自动安装**。

    背景（实测，不是推断）：默认 dhcp-boot 原先指向的就是各模板生成的那份
    `boot.ipxe`，而它是**单份共享文件** —— 于是"任何 PXE 起来、又没显式登记的机器，
    都会被按**最后一次部署的模板**装机"，实测出现过不需要装机的机器被重新分区。

    现在的分工：
      · 已登记 MAC：dnsmasq 的第二阶段 dhcp-boot 直接指向它自己的
        `<answer_root>/boot/<mac>.ipxe`（见 _dnsmasq），与全局文件无关；
      · 未登记 MAC：只能落到本文件。本菜单不装任何系统，打印提示后 `exit`
        把控制权交回固件（固件通常接着从本地硬盘引导）。
    `exit` 而不是 `chain` 本地盘：iPXE 里没有可移植的"从本地盘启动"原语
    （BIOS/UEFI 各不相同），`exit` 是唯一两条固件都认的做法。

    本函数**不接收 PxeConfig**：内容与模板无关，因此任何模板部署出来的默认菜单都是
    逐字节相同的 —— 不存在"后部署的模板把默认菜单改写成另一种行为"这回事。
    """
    return "\n".join([
        "#!ipxe",
        UNREGISTERED_DEFAULT_MARK,
        "# 未登记的机器：本菜单不执行任何安装 / 分区操作。",
        "# 已登记机器不会被带到这里 —— 它们的第二阶段引导由 dnsmasq 按 MAC 指向",
        "# <answer_root>/boot/<mac>.ipxe（每个模板一套，互不覆盖）。",
        "# echo 一律用 ASCII：装机机的 VGA/串口基本都是 ASCII，中文会显示成乱码。",
        "echo OpsToolkit: this machine is not registered; refusing to auto-install.",
        "echo Register its MAC in OpsToolkit (install records) and deploy again.",
        "echo Returning to firmware / local disk in 5 seconds...",
        "sleep 5",
        "exit",
        "",
    ])


def _unsupported_auto_install_msg(os_type) -> str:
    """debian/openSUSE 等 auto_install=False 的系统：生成配置时的明确拒绝提示。

    要求（用户反馈语境）：能识别、能提取引导介质，但**不能硬凑**一套假应答文件 ——
    明确说清"只支持识别 + 提取"，并给出可行出路。
    """
    e = os_catalog.entry(os_type)
    display = e.display if e else str(os_type or "")
    family_hint = ""
    if e is not None and e.installer == os_catalog.AUTOYAST:
        family_hint = "（该家族的自动装机机制是 AutoYaST，本项目未实现）"
    elif e is not None and e.installer == os_catalog.PRESEED:
        family_hint = ("（debian-installer 的 preseed 与 Ubuntu autoinstall 不是同一套语法，"
                       "本项目未实现）")
    return (
        "暂不支持 " + display + " 的自动安装：本项目目前只实现了 kickstart 家族"
        "（RHEL/Rocky/Alma/CentOS/Oracle Linux/openEuler/Kylin/UOS/Anolis/Fedora）"
        "与 Ubuntu autoinstall 的自动装机生成" + family_hint + "；" + display +
        " 目前支持识别 ISO 与提取引导介质，自动装机请改用已支持的系统或手工安装。"
    )


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
    # 媒体（kernel/initrd/ISO）**只**拼 http_root —— 见 _answer_base 的说明。
    # 应答文件（user-data / ks.cfg）走 answer_root：它按模板隔离，
    # 而媒体不能隔离（介质只存在于扁平路径）。
    answer_base = _answer_base(c)
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
    # 按目录条目分岔（不是按散落的字符串比较）：unknown / auto_install=False 一律
    # 显式失败，绝不生成一份必然失败的装机菜单。
    entry = os_catalog.entry(c.os_type)
    if entry is None:
        raise ValueError(
            "未知的系统类型 " + repr(c.os_type or "") + "：请从系统目录中选择"
        )
    if not entry.auto_install:
        raise ValueError(_unsupported_auto_install_msg(entry.key))
    if entry.key == "ubuntu":
        seed = _safe_line(answer_url, "answer_url") or (answer_base + "/")
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
        answer = _safe_line(answer_url, "answer_url") or (answer_base + "/ks.cfg")
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
    # 服务端绑卡：**只认 server_interface**（由 api 层按 server_ip 反查持有它的网卡写入）。
    # nc["interface"] 是**被装机器**的网卡名（界面标签「网卡名」，占位 ens33）—— 早期
    # 这里直接用它，多网卡服务器上就会把 DHCP 绑到错误的网卡：本项目实测模板
    # interface=ens18（客户端网卡名，正确）+ server_ip=192.168.199.1（隔离装机网）时，
    # 生成物是 `interface=ens18`，而 ens18=10.128.118.113 是企业网卡 —— 一部署就在
    # 10.128.118.0/24 上开 DHCP。没有 server_interface 时保持原样（向后兼容）。
    iface = nc.get("server_interface") or nc.get("interface", "eth0")
    gateway = nc.get("gateway", "192.168.1.1")
    mode = c.deploy_mode or "standalone"
    # 每台【已登记】机器的第二阶段引导脚本放在 answer_root 下（按模板隔离）。
    # 未登记机器的默认引导脚本仍然是**扁平**的 <http_root>/boot.ipxe：
    # 它是全局唯一、内容由 generator 定死的"拒绝自动安装"菜单（有装机记录时），
    # 见 _unregistered_default_menu()。默认菜单**不能**指向 answer_root，
    # 否则又变成"谁最后部署谁决定未登记机器装什么"。
    answer_base = _answer_base(c)

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
    # 元组第 3 位 = 该机登记的静态 ip（没登记为 None）：有它 dnsmasq 才按 MAC 下发
    # DHCP 预留（重装/改配置时该机也拿到同一地址，防止 IP 漂移）。
    # 元组第 4/5 位 = 装机状态与完成时间：status 非 pending 的记录**不再下发**按机
    # 的自动装机菜单（防重复抹盘，2026-10-08 真机实证：装完的机器重启走网卡引导
    # 又被装了一遍），但 dhcp-host 地址预留照发（该机的 IP 稳定性不能丢）。
    reg = []
    for inst in installs:
        mac = _safe_mac(inst.get("mac"))
        if not mac:
            continue
        addr, _prefix = _require_install_ip(inst, "装机记录 " + mac)
        _fin = " ".join(str(inst.get("finished_at") or "").split())
        reg.append((mac, _mac_tag(mac), addr,
                    str(inst.get("status") or "pending"), _fin or "已完成"))

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
        L.append("#   option 67 = <answer_root>/boot/<mac>.ipxe   例如 " + answer_base
                 + "/boot/" + (_mac_tag(reg[0][0]) if reg else "aa-bb-cc-dd-ee-ff") + ".ipxe")
        L.append("# 否则机器只会被带到通用 boot.ipxe（未登记的默认菜单），"
                 "按 MAC 定制的 hostname 不会生效。")
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
    for mac, tag, ip, _st, _fin in reg:
        # 照抄同文件既有拼法：dhcp-host=<mac>,set:pxe_<tag>；该机登记了 ip 时
        # 在中间插入 <ip>（dnsmasq 的 dhcp-host=MAC,IP,set:tag 是同一指令的预留写法）。
        # ★ 已标记完成的机器这行**照发**：地址预留要保持稳定（IP 不漂移），
        #   只是它对应的自动装机菜单不再下发（见下面第二阶段循环）。
        L.append("dhcp-host=" + mac + ("," + ip if ip else "") + ",set:pxe_" + tag)
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
    for mac, tag, _ip, st, fin in reg:
        if st != "pending":
            # ★ 防重复抹盘（2026-10-08 真机实证）：该机已标记完成（finished_at=…），
            #   不再下发自动装机菜单 → 回落未登记默认菜单 → 引导本地磁盘。
            #   它的两条 per-MAC 行（tag-if=set:fw-menu-<tag> 与 dhcp-boot）就此缺席：
            #   机器重启走网卡时拿不到装机菜单，固件按引导顺序继续引导本地盘。
            L.append("# " + mac + "：该机已标记完成（finished_at=" + fin
                     + "），不再下发自动装机菜单 → 回落未登记默认菜单 → 引导本地磁盘")
            continue
        L.append("tag-if=set:fw-menu-" + tag + ",tag:fw-menu,tag:pxe_" + tag)
        L.append("dhcp-boot=tag:fw-menu-" + tag + "," + answer_base + "/boot/" + tag + ".ipxe")
    # 默认菜单对**仍待装机**的 MAC 取反，从而与上面每台机的专属菜单互斥。
    # 已标记完成的机器**不**取反：它的 dhcp-host 仍在（set:pxe_<tag> 照发），
    # 而专属菜单已不下发 ⇒ tag:fw-menu 成立且所有 tag:!pxe_<待装> 都成立
    # ⇒ 落到 fw-menu-def（未登记默认菜单：拒绝自动安装并 exit）⇒ 固件继续
    # 按引导顺序引导本地磁盘 —— 这正是本单元要的目标行为。
    # 它指向扁平的 <http_root>/boot.ipxe（全局唯一），内容见 _unregistered_default_menu()。
    neg = "".join(",tag:!pxe_" + t for _m, t, _ip, st, _fin in reg if st == "pending")
    L.append("tag-if=set:fw-menu-def,tag:fw-menu" + neg)
    L.append("# 未登记机器的默认菜单：扁平全局文件（不是 profiles/<pid>/ 下的那份）")
    L.append("dhcp-boot=tag:fw-menu-def," + c.http_root + "/boot.ipxe")
    if not c.ipxe_ia32_available:
        # 只摘掉"下发 32 位固件"那两条。**必须保留** dhcp-match 里的 efi-ia32 与
        # tag-if=set:fw-ia32：否则 ia32 客户端会因为 tag:!efi-ia32 成立而落进 fw-bios，
        # 拿到 undionly.kpxe（BIOS 固件）—— 架构不符，比"没有引导项"更糟。
        L = [x for x in L if not x.startswith(("pxe-service=tag:fw-ia32",
                                               "dhcp-boot=tag:fw-ia32"))]
    L.append("")
    return "\n".join(L) + "\n"


def _mode_label(mode):
    return {
        "standalone": "standalone - 独立 DHCP (专用装机网络)",
        "proxy": "proxy - ProxyDHCP (与现有 DHCP 并存)",
        "relay": "relay - 中继模式 (仅 TFTP, 依赖交换机中继)",
    }.get(mode, mode)


# ── P2：lvm 简写的固定容量提示（README 与部署日志共用同一份文案）──
# 数字来源：_rhel_layout_lines("lvm", …) 的固定尺寸合计
#   20480 MiB(root) + 8192 MiB(swap) + 10240 MiB(/home) + 1024 MiB(/boot)
#   + 512 MiB(ESP) = 40448 MiB ≈ 40.4 GB（按十进制 GB 读）。
# 小盘（如 40 GB 的系统盘实际可用只有 ~37 GiB）装不下这套固定尺寸，分区阶段就失败。
LVM_SIZE_WARNING = (
    "警告：本模板用 lvm 简写分区，固定尺寸合计 ≈ 29.5 GB"
    "（root 20 GiB + swap 8 GiB + /boot 1 GiB + ESP 0.5 GiB，/home 随盘增长）；"
    "目标引导盘小于约 30 GB 时装机仍会失败，请改用 direct（part / --grow，随盘缩放）"
    "或自定义分区。"
)





def _readme(c, has_registered=False):
    iso_mb = int(c.iso_size_mb or 0)
    ram_mb = (iso_mb + 1536) if iso_mb else 0
    # admin_user 会落进 README 文本；与其它汇点同一份白名单（不拒绝，只剔除）
    admin = _safe_username(c.admin_user)
    if has_registered:
        unreg = (
            "未登记的机器（默认菜单 <http_root>/boot.ipxe）\n"
            "------------------------------------------\n"
            "本模板有已登记的装机记录，所以默认菜单是【拒绝自动安装】的安全菜单：\n"
            "未登记的机器 PXE 起来后只会被打回固件/本地硬盘，不会被重新分区。\n"
            "要装一台新机器，请先在 OpsToolkit 里为它的 MAC 建装机记录并重新部署。\n"
            "已登记机器各自的菜单在同目录的 boot/<mac>.ipxe，互相不覆盖。\n\n"
        )
    else:
        unreg = (
            "未登记的机器（默认菜单 <http_root>/boot.ipxe）\n"
            "------------------------------------------\n"
            "本模板**没有**任何已登记装机记录，因此默认菜单就是本模板的装机菜单\n"
            "（向后兼容：单模板部署的既有行为不变）。\n"
            "注意：默认菜单是全局唯一的一份文件，同一引导网段里再部署另一个\n"
            "同样没有装机记录的模板，会把它覆盖成那个模板 —— 多模板请为机器\n"
            "建装机记录（每个 MAC 会拿到自己的 boot/<mac>.ipxe）。\n\n"
        )
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
        "磁盘容量要求（必读）\n"
        "----------------\n"
        "lvm 简写的固定尺寸合计 ≈ 29.5 GB\n"
        "（root 20 GiB + swap 8 GiB + /boot 1 GiB + ESP 0.5 GiB；/home 用 --grow 吃掉剩余空间）。\n"
        "目标引导盘小于约 30 GB 时请改用 direct（`part / --grow`，随盘缩放）或自定义分区，\n"
        "否则装机在分区阶段就会失败（盘放不下这套固定尺寸）。\n"
        + ("【当前模板警告】" + LVM_SIZE_WARNING + "\n\n"
           if (c.disk_scheme or "") == "lvm" else "\n") +
        (("【服务端绑卡警告】" + c.warn_serve_binding + "\n\n")
         if c.warn_serve_binding else "") +
        "引导顺序（必读）\n"
        "----------------\n"
        "目标机固件请设为【先硬盘、后网卡】，例如 PVE：\n"
        "  qm set <vmid> --boot \"order=scsi0;net0\"\n"
        "  - 空盘机器：硬盘引导失败 → 落到网卡 → PXE 装机\n"
        "  - 装好的机器：硬盘有 grub → 直接起系统，不会被 PXE 重装\n"
        "若设成只从网卡引导，装完自动重启后会再次 PXE 并**重装一遍**（反复抹盘）。\n"
        "装机收尾会回调服务端把该机标记完成（finish/done），标记完成后 dnsmasq\n"
        "不再给这台机器下发自动装机菜单 —— 网卡引导只会落到拒绝安装的默认菜单后\n"
        "交回固件，不会再抹盘；把引导顺序改成磁盘优先仍是最稳妥的兜底。\n\n"
        "内核控制台\n"
        "----------\n"
        "本次参数: " + (c.kernel_console or "(未设置)") + "\n"
        "无显示器的机器请接串口(115200)看装机过程与失败原因。\n\n"
        "装完怎么登录（必读）\n"
        "--------------------\n"
        "装好后的机器请用【管理员账号】SSH 登录，而不是 root：\n"
        "  ssh " + admin + "@<该机IP>\n"
        "RHEL 系（anaconda 建的是普通用户，root 常被禁用）：装完的默认路径就是\n"
        "普通管理员 + sudo，root 直登默认是关的（PermitRootLogin no）。\n"
        "  · sudo：管理员账号已在 wheel/sudo 组，"
        + ("sudo 免密（/etc/sudoers.d/90-opstk-" + admin + "）。"
           if c.sudo_nopasswd else "sudo 需要输该用户自己的密码。") + "\n"
        "  · root 直登：本次模板" + ("已打开（PermitRootLogin yes，root 口令同 root密码/管理员密码）"
                                       if c.allow_root else "未打开；要开请在模板里打开“允许 root 登录”后重新部署。") + "\n"
        "  · SSH 公钥：模板里填了公钥的，已写入 /home/" + admin +
        "/.ssh/authorized_keys，\n    并确保 openssh-server 在装（Ubuntu 侧配了公钥自动补装）。\n"
        "登不进去时：优先接串口确认装机是否完成、该机是否拿到登记的 IP（见下）。\n\n"
        "每机静态 IP\n"
        "-----------\n"
        "装机记录里登记了 ip 的机器，应答文件按该机地址生成静态网络配置\n"
        "（RHEL：network --bootproto=static --ip=<该机ip> …；Ubuntu：autoinstall 的\n"
        "network: 段），装完即开箱可达，不再依赖 DHCP 随机取址。\n"
        "dnsmasq.conf 里登记了 ip 的机器还会带 DHCP 预留行\n"
        "（dhcp-host=<mac>,<ip>,set:…）：重装或改配置时该机也拿到同一地址，IP 不漂移。\n"
        "装机记录没填 ip 的机器仍按模板的网络配置（DHCP/模板级静态）下发。\n\n"
        + unreg +
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
    answer_base = _answer_base(c)
    files = {}
    # 按**目录条目**分岔（唯一定义点 os_catalog）：
    #   · kickstart 家族（rhel/rocky/alma/centos/oraclelinux/openeuler/kylin/uos/
    #     anolis/fedora）复用同一份 ks 生成器 —— 按"安装器家族"而不是按发行版复制逻辑；
    #   · ubuntu 保持 autoinstall(subiquity) 既有生成（字节级 golden 锁住）；
    #   · debian(preseed)/opensuse(autoyast) 目前只支持识别 + 提取引导介质，
    #     在这里就明确拒绝，绝不硬凑一份假应答文件。
    entry = os_catalog.entry(c.os_type)
    if entry is None:
        raise ValueError(
            "未知的系统类型 " + repr(c.os_type or "") + "：请从系统目录中选择"
        )
    if not entry.auto_install:
        raise ValueError(_unsupported_auto_install_msg(entry.key))
    if entry.key == "ubuntu":
        files["user-data"] = _ubuntu_user_data(c)
        files["meta-data"] = "local-hostname: " + _safe_hostname(c.hostname) + "\n"
    else:
        files["ks.cfg"] = _rhel_ks(c)
    # 默认菜单（= dnsmasq 里 tag:fw-menu-def 指向的那份扁平 boot.ipxe）取什么内容：
    #   · 本模板**有**已登记装机记录 → "拒绝自动安装"的安全菜单。未登记的机器不再
    #     按"最后一次部署的模板"装系统（实测发生过不该装的机器被重新分区）；
    #   · 本模板**没有**任何装机记录 → 仍是本模板自己的菜单：这是既有行为，
    #     单模板部署（前端"部署"按钮就是发 installs:[]）靠它才能装机，不能破。
    # 两种内容都是**显式定义**的，且安全菜单与模板无关（逐字节相同），
    # 因此多模板场景下不存在"后部署者把默认行为改写成另一种"。
    has_registered = any(_safe_mac(i.get("mac")) for i in (installs or []) if isinstance(i, dict))
    files["boot.ipxe"] = _unregistered_default_menu() if has_registered else _ipxe_menu(c)
    files["dnsmasq.conf"] = _dnsmasq(c, installs)
    files["README.txt"] = _readme(c, has_registered=has_registered)
    # 每台装机记录生成独立菜单与应答文件，使 hostname 生效。
    # 应答文件与菜单都落在 answer_base（= 部署时的 profiles/<pid>，按模板隔离）之下。
    for inst in installs or []:
        # D7 第 3 层：mac 会变成【文件名】与 dhcp-host 的值，必须是合法 MAC
        mac = _safe_mac(inst.get("mac"))
        if not mac:
            continue
        tag = _mac_tag(mac)
        hostname = _safe_hostname(inst.get("hostname"), "") or _safe_hostname(c.hostname)
        # ── 装完回调（防重复抹盘）──
        # done_url 只随**每机**应答文件下发（api 层按装机记录拼好带来）；模板级
        # ks.cfg/user-data 用的是 c 本体，done_url 恒为空串，不带回调。
        # 无论该机 status 是什么，文件都照旧生成（幂等、便于复核）——是否下发
        # 由 _dnsmasq 按 status 决定，与文件在不在无关。
        ic = replace(c, hostname=hostname, done_url=str(inst.get("done_url") or ""))
        # ── 每机静态 IP（本单元核心）───────────────────────────────────
        # 该机登记了 ip ⇒ 按【静态】生成这份机器自己的应答文件（装机记录的 ip
        # 优先于模板 net_config 的 ip；掩码取该机 ip 的 "/N"，缺省沿用模板），
        # hostname 也已经是该机自己的 —— ks 家族的 `network … --hostname=` 与
        # Ubuntu 的 identity.hostname 因此各就各位。
        # 该机**没填** ip ⇒ 不做任何覆盖，仍用模板 net_mode/net_config（不回归）。
        # 每个只作用于 replace 出来的 ic：模板级 ks.cfg/user-data 与 dnsmasq 不受
        # 影响，且同一模板两台机器各生成各的文件（一个 MAC 一套），不会串台。
        mnet = _machine_net_config(c, inst, "装机记录 " + mac)
        if mnet is not None:
            ic = replace(ic, net_mode="static", net_config=mnet)
        if entry.key == "ubuntu":
            seed = answer_base + "/user-data/" + tag + "/"
            files["user-data/" + tag + "/user-data"] = _ubuntu_user_data(ic)
            files["user-data/" + tag + "/meta-data"] = "local-hostname: " + hostname + "\n"
            answer = seed
        else:
            answer = answer_base + "/ks/" + tag + "/ks.cfg"
            files["ks/" + tag + "/ks.cfg"] = _rhel_ks(ic)
        files["boot/" + tag + ".ipxe"] = _ipxe_menu(ic, mac=mac, answer_url=answer)
    return files
