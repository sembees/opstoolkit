"""PXE server management (Linux).

OpsToolkit host acts as a full PXE server:
  - Files auto-deployed to TFTP/HTTP directories
  - dnsmasq lifecycle managed by shared DHCP module (app.core.dhcp)
  - iPXE firmware preparation (ipxe.efi / undionly.kpxe)
Non-Linux or no-sudo gracefully degrades.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess

from app.core import dhcp as _dhcp

TFTP_ROOT = "/srv/tftp"
WEB_ROOT = "/srv/opstk/pxe-web"

# 固件来源表。键 = 拷进 TFTP 的目标文件名。
# M9：`ipxe.efi` 是发给 **x86_64 UEFI**(client-arch 7/9) 的，源列表里**绝不能**出现
# 32 位 EFI 二进制。原先把 /usr/share/ipxe/ipxe-i386.efi 当候选，一旦 x86_64 那份缺失，
# 就会把 32 位 EFI 二进制拷成 ipxe.efi 下发给 64 位客户端 —— 架构不符，引导必然失败。
# 32 位 EFI(client-arch 6) 走独立的 ipxe-i386.efi。
FIRMWARE = {
    "ipxe.efi": [
        "/usr/share/ipxe/ipxe-x86_64.efi",
        "/usr/share/ipxe/ipxe.efi",
        "/usr/lib/ipxe/ipxe.efi",
    ],
    "ipxe-i386.efi": [
        "/usr/share/ipxe/ipxe-i386.efi",
        "/usr/lib/ipxe/ipxe-i386.efi",
    ],
    "undionly.kpxe": [
        "/usr/share/ipxe/undionly.kpxe",
        "/usr/lib/ipxe/undionly.kpxe",
    ],
}


# ── Delegated to shared DHCP module ──

def is_linux() -> bool:
    return _dhcp.is_linux()


def sudo_ok() -> bool:
    return _dhcp.sudo_ok()


def _parse_listen_ports(out: str) -> list:
    """从 `ss -lun` 输出里取出**本机**监听的 67/69 端点。

    M8：本机端点是 Local Address:Port，**不是** Peer。
    `ss -lun` 不带 `-p` 时最后一列是 Peer（形如 `0.0.0.0:*`），本机端点是**倒数第二列**。
    旧实现取 parts[4]，在"无 Netid 前缀"的 5 列输出里恰好是 Peer 列，
    于是 /server/status 返回的 ports 全是 `0.0.0.0:*` 这种无用值（真实监听端口丢失）。

    取 parts[-2] 同时兼容三种现实差异：
      · 有 / 无 Netid 前缀（部分版本 `ss -tunl` 会多一列）
      · 有 / 无表头行（表头里倒数第二列是 `Address:Port`，端口段为 `Port`，自然被过滤掉）
      · IPv4 / IPv6（`[::]:69`）
    """
    ports = []
    for line in (out or "").splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        local = parts[-2]
        if ":" not in local:
            continue
        if local.rsplit(":", 1)[-1] in ("67", "69"):
            ports.append(local)
    return sorted(set(ports))


def _listen_ports_from_proc(udp_path="/proc/net/udp",
                             udp6_path="/proc/net/udp6") -> list:
    """`ss` 不可用时，从 /proc/net/udp{,6} 取本机监听的 67/69 端点。

    为什么需要它：opstoolkit 容器镜像里没有 iproute2，`ss` 不存在，
    `_dhcp._run(["ss","-lun"])` 返回 127 且输出为空，于是 /server/status 的 ports 恒为空。
    容器是 host 网络模式（NetworkMode=host），/proc/net/udp{,6} 直接反映宿主机的
    UDP socket，因此不需要改镜像（装 iproute2）也能拿到真实监听端点。

    /proc 的编码要点（已用真实数据逐条核对）：
      · IPv4 地址是 hex 且按**主机字节序**，需按小端重组：
        `00000000:0043` -> 0.0.0.0:67；`0100007F:0045` -> 127.0.0.1:69；
        `7176800A:0045` -> 10.128.118.113:69
      · IPv6 是 32 位 hex，同样每 4 字节一组按小端还原：
        `00000000000000000000000001000000:0045` -> [::1]:69
      · 端口是 hex（0x43=67、0x45=69）
      · dnsmasq 会同时开多个 socket，必须去重
    输出风格与 `ss` 路径保持一致，IPv6 用方括号包裹（`[::1]:69`）。
    """
    import socket
    import struct

    def read_lines(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().splitlines()
        except OSError:
            return []

    out = []
    for line in read_lines(udp_path)[1:]:
        parts = line.split()
        if len(parts) < 2 or ":" not in parts[1]:
            continue
        hexip, hexport = parts[1].rsplit(":", 1)
        try:
            if int(hexport, 16) not in (67, 69):
                continue
            out.append(socket.inet_ntoa(struct.pack("<I", int(hexip, 16)))
                       + ":" + str(int(hexport, 16)))
        except (OSError, ValueError, struct.error):
            continue
    for line in read_lines(udp6_path)[1:]:
        parts = line.split()
        if len(parts) < 2 or ":" not in parts[1]:
            continue
        hexip, hexport = parts[1].rsplit(":", 1)
        try:
            port = int(hexport, 16)
            if port not in (67, 69) or len(hexip) != 32:
                continue
            raw = b"".join(struct.pack("<I", int(hexip[i:i + 8], 16))
                           for i in range(0, 32, 8))
            out.append("[" + socket.inet_ntop(socket.AF_INET6, raw) + "]:" + str(port))
        except (OSError, ValueError, struct.error):
            continue
    return sorted(set(out))


def server_status() -> dict:
    """Combined status: shared DHCP + PXE-specific files/ports."""
    dhcp_st = _dhcp.dhcp_status()
    if not dhcp_st["supported"]:
        return {"supported": False, "platform": dhcp_st.get("platform", platform.system())}

    # TFTP files
    tftp_files = []
    if os.path.isdir(TFTP_ROOT):
        for root, _, fs in os.walk(TFTP_ROOT):
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), TFTP_ROOT)
                tftp_files.append(rel)

    # HTTP files
    web_files = []
    if os.path.isdir(WEB_ROOT):
        try:
            web_files = sorted(os.listdir(WEB_ROOT))
        except Exception:
            pass

    # Listening ports (67 dhcp, 69 tftp)
    # 优先 ss（它的输出还带接口名，如 0.0.0.0%ens18:67）；容器镜像里通常没有 iproute2，
    # 此时 `ss` 不存在会让 _run 返回 127/空输出，需回退到 /proc。
    rc, out, _ = _dhcp._run(["ss", "-lun"])
    ports = _parse_listen_ports(out)
    ports_source = "ss"
    if not ports:
        # 容器是 host 网络模式，/proc/net/udp{,6} 能看到宿主机的 UDP socket，
        # 因此没有 iproute2 也能拿到真实的 67/69 监听端点。
        ports = _listen_ports_from_proc()
        ports_source = "/proc/net/udp"

    return {
        "supported": True,
        "dnsmasq": {
            "active": dhcp_st["running"],
            "enabled": dhcp_st["has_systemd"],
        },
        "tftp_root": TFTP_ROOT,
        "web_root": WEB_ROOT,
        "tftp_files": sorted(tftp_files),
        "web_files": sorted(web_files),
        "ports": ports,
        "ports_source": ports_source,
        "sudo_ok": dhcp_st["sudo_ok"],
        "conf_files": dhcp_st["conf_files"],
    }


def service_control(action: str) -> dict:
    """Delegate to shared DHCP module."""
    result = _dhcp.dhcp_control(action)
    return {
        "ok": result["ok"],
        "msg": result["msg"],
        "active": result["running"],
    }


# ── Network detection ──

def detect_network() -> dict:
    """探测本机主用网卡 / IP / 网关 / DHCP 范围。

    不能硬依赖 `ip` 命令：实测 opstoolkit 容器镜像里没有 iproute2，
    `_dhcp._run(["ip", ...])` 会返回 127，旧实现因此静默产出
    `server_ip=""` 之类的空值，最终生成 `dhcp-range=,,12h` 这种会让
    dnsmasq 起不来的配置。这里改为多级回退，并且：
      - 取不到的值**不放进返回字典**（而不是放空字符串），
        这样下游 `nc.get(key, default)` 才能正确回退到默认值；
      - 新增 `warnings` 字段记录降级原因，失败可辨识。
    """
    if not _dhcp.is_linux():
        return {}

    import ipaddress
    import socket
    import struct

    warnings = []

    def _ioctl_ifaddr(ifname: str, code: int):
        """用 ioctl 取网卡地址/掩码。fcntl 是 Linux 专有，延迟导入。失败返回 None。"""
        if not ifname:
            return None
        try:
            import fcntl
        except ImportError:
            return None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                raw = fcntl.ioctl(
                    s.fileno(), code, struct.pack("256s", ifname[:15].encode())
                )
                return socket.inet_ntoa(raw[20:24])
            finally:
                s.close()
        except Exception:
            return None

    SIOCGIFADDR = 0x8915
    SIOCGIFNETMASK = 0x891B

    # ---- 1. 默认路由：接口 + 网关 ----
    iface = ""
    gateway = ""
    used_ip_cmd = False

    rc, out, _ = _dhcp._run(["ip", "route", "show", "default"])
    if rc == 0 and out.strip():
        for line in out.splitlines():
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "via" and i + 1 < len(parts):
                    gateway = parts[i + 1]
                elif p == "dev" and i + 1 < len(parts):
                    iface = parts[i + 1]
            if iface:
                break
        used_ip_cmd = bool(iface)

    if not iface:
        # /proc/net/route：Destination 为 00000000 的是默认路由；Gateway 是小端十六进制
        try:
            with open("/proc/net/route", encoding="utf-8") as fh:
                for line in fh.readlines()[1:]:
                    parts = line.split()
                    if len(parts) < 11 or parts[1] != "00000000":
                        continue
                    iface = parts[0]
                    try:
                        gateway = socket.inet_ntoa(struct.pack("<I", int(parts[2], 16)))
                    except Exception:
                        warnings.append("网关十六进制解析失败: " + parts[2])
                    break
            if iface:
                warnings.append("ip 命令不可用，已改用 /proc/net/route 解析默认路由")
        except FileNotFoundError:
            warnings.append("/proc/net/route 不存在")
        except Exception as e:  # noqa: BLE001
            warnings.append("解析 /proc/net/route 失败: " + str(e)[:60])

    if not iface:
        # 再退：从 /sys/class/net 里挑一个 up 的非虚拟网卡
        try:
            for name in sorted(os.listdir("/sys/class/net")):
                if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
                    continue
                try:
                    state = open(f"/sys/class/net/{name}/operstate").read().strip()
                except Exception:  # noqa: BLE001
                    state = ""
                if state == "up":
                    iface = name
                    warnings.append("已从 /sys/class/net 回退确定网卡")
                    break
        except Exception:  # noqa: BLE001
            pass

    if not iface:
        warnings.append("无法确定默认网络接口与网关")

    # ---- 2. 本机地址 + 前缀长度 ----
    server_ip = ""
    prefix = None

    if used_ip_cmd:
        rc, out, _ = _dhcp._run(["ip", "-o", "-f", "inet", "addr", "show", iface])
        if rc == 0:
            for line in out.splitlines():
                for tok in line.split():
                    if "/" in tok and tok[0].isdigit():
                        server_ip = tok.split("/")[0]
                        try:
                            prefix = int(tok.split("/")[1])
                        except Exception:  # noqa: BLE001
                            prefix = None
                        break
                if server_ip:
                    break

    if not server_ip:
        cand = _ioctl_ifaddr(iface, SIOCGIFADDR) or ""
        if not cand:
            # 到 TEST-NET-2 做一次路由查询（不实际发包），取本机源地址
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                try:
                    s.connect(("198.51.100.1", 9))
                    cand = s.getsockname()[0]
                finally:
                    s.close()
            except Exception as e:  # noqa: BLE001
                warnings.append("socket 探测本机地址失败: " + str(e)[:60])
        if cand.startswith("127."):
            warnings.append("探测到环回地址 " + cand + "，已忽略")
            cand = ""
        if cand:
            server_ip = cand
            warnings.append("ip 命令不可用，已改用 ioctl/socket 获取本机地址")
            mask = _ioctl_ifaddr(iface, SIOCGIFNETMASK)
            if mask:
                try:
                    prefix = ipaddress.IPv4Network(
                        "0.0.0.0/" + mask, strict=False
                    ).prefixlen
                except Exception:  # noqa: BLE001
                    prefix = None
            if prefix is None:
                warnings.append("未取到子网掩码，DHCP 范围无法计算")

    # ---- 3. DHCP 范围 ----
    dhcp_start = ""
    dhcp_end = ""
    if server_ip and prefix is not None:
        try:
            net = ipaddress.ip_network(server_ip + "/" + str(prefix), strict=False)
            hosts = list(net.hosts())
            if len(hosts) > 10:
                dhcp_start = str(hosts[len(hosts) * 3 // 4])
                dhcp_end = str(hosts[-2])
        except Exception as e:  # noqa: BLE001
            warnings.append("计算 DHCP 范围失败: " + str(e)[:60])

    # ---- 4. 只放入有值的键：绝不放空字符串 ----
    result = {}
    if iface:
        result["interface"] = iface
    if gateway:
        result["gateway"] = gateway
        result["dns_server"] = gateway
    if server_ip:
        result["server_ip"] = server_ip
    if dhcp_start:
        result["dhcp_start"] = dhcp_start
    if dhcp_end:
        result["dhcp_end"] = dhcp_end
    if warnings:
        result["warnings"] = warnings
    return result


# ── Directory & firmware prep ──

def prepare_dirs() -> list:
    """Create TFTP/HTTP dirs and adjust ownership."""
    log = _dhcp.ensure_dirs([TFTP_ROOT, WEB_ROOT])
    # Also create boot subdir
    boot_dir = os.path.join(TFTP_ROOT, "boot")
    if not os.path.isdir(boot_dir):
        os.makedirs(boot_dir, exist_ok=True)
        log.append("Created dir: " + boot_dir)
    _selinux_fix(log)
    return log


def _selinux_fix(log=None):
    log = log if log is not None else []
    rc, out, _ = _dhcp._run(["getenforce"])
    if out.strip() != "Enforcing":
        return log
    _dhcp._run(["semanage", "fcontext", "-a", "-t", "tftpdir_t", TFTP_ROOT + "(/.*)?"], sudo=True)
    rc2, _, _ = _dhcp._run(["restorecon", "-R", TFTP_ROOT], sudo=True)
    if rc2 == 0:
        log.append("SELinux fixed (tftpdir_t)")
    return log


def prepare_firmware() -> list:
    """Copy iPXE firmware from system packages to TFTP."""
    log = []
    for name, sources in FIRMWARE.items():
        dst = os.path.join(TFTP_ROOT, name)
        if os.path.exists(dst):
            log.append("Firmware exists: " + name)
            continue
        copied = False
        for src in sources:
            if os.path.exists(src):
                shutil.copy2(src, dst)
                log.append("Copied " + name + " <- " + os.path.basename(src))
                copied = True
                break
        if not copied:
            log.append("Missing firmware: " + name + " (install ipxe-bootimgs)")
    return log


# ── Deploy ──

def deploy_files(files, pid="") -> dict:
    """Write configs to host: dnsmasq.conf via shared DHCP module, response
       files to TFTP/HTTP, then restart dnsmasq."""
    if not _dhcp.is_linux():
        return {
            "ok": False,
            "platform": platform.system(),
            "log": ["Linux only; use Download ZIP on non-Linux"],
        }
    log = list(prepare_dirs())
    log += prepare_firmware()
    if not _dhcp.sudo_ok():
        log.append("WARNING: no sudo; dnsmasq config and restart will fail")

    # Write dnsmasq config via shared module
    dnsmasq_content = files.get("dnsmasq.conf", "")
    if dnsmasq_content:
        ok = _dhcp.write_conf("opstk-pxe.conf", dnsmasq_content)
        if ok:
            log.append("Written: /etc/dnsmasq.d/opstk-pxe.conf")
        else:
            log.append("FAILED: write dnsmasq config")

    # Write response files to HTTP directory
    web_root_abs = os.path.abspath(WEB_ROOT)
    for name, content in files.items():
        if name == "dnsmasq.conf":
            continue
        rel = os.path.normpath(name.lstrip("/"))
        if rel == "." or rel.startswith(".."):
            log.append("Skip illegal path: " + name)
            continue
        dst = os.path.abspath(os.path.join(web_root_abs, rel))
        if os.path.commonpath([dst, web_root_abs]) != web_root_abs:
            log.append("Skip boundary path: " + name)
            continue
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        log.append("Deployed: " + rel)

    # Restart dnsmasq via shared module
    svc = _dhcp.dhcp_control("restart")
    log.append("dnsmasq restart: " + svc.get("msg", "unknown"))
    return {"ok": svc["ok"], "log": log, "tftp_root": TFTP_ROOT, "web_root": WEB_ROOT}


# ── ISO management ──

ISO_DIR = "/srv/opstk/iso"
MOUNT_BASE = "/srv/opstk/mnt"

# ── U9: extract_from_iso 路径安全（先于任何目录创建校验）──
# 白名单：os_type 归一化小写后必须命中其一
_ALLOWED_OS_TYPES = ("ubuntu", "debian", "rhel", "centos", "rocky", "alma", "almalinux")
# os_version 仅允许 [A-Za-z0-9._-]+（且显式拒绝 "." 与 ".."）
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _iso_path(iso_name) -> str | None:
    raw = (iso_name or "").strip()
    if "/" in raw or "\\" in raw:
        return None
    name = os.path.basename(raw)
    if not name.lower().endswith(".iso"):
        return None
    iso_abs = os.path.abspath(ISO_DIR)
    path = os.path.abspath(os.path.join(ISO_DIR, name))
    # 与 _iso_size_mb 保持一致的显式拒绝（basename 已经挡掉了路径分隔符，
    # 这里是为了让两个入口的防御策略一眼看齐，避免后续维护者误判已有防护）
    if name in (".", ".."):
        return None
    if os.path.commonpath([path, iso_abs]) != iso_abs:
        return None
    return path


def list_isos() -> dict:
    if not _dhcp.is_linux():
        return {"supported": False, "isos": []}
    isos = []
    if os.path.isdir(ISO_DIR):
        for f in sorted(os.listdir(ISO_DIR)):
            if f.lower().endswith(".iso"):
                fp = os.path.join(ISO_DIR, f)
                st = os.stat(fp)
                isos.append({
                    "name": f,
                    "size": st.st_size,
                    "size_mb": round(st.st_size / 1048576, 1),
                })
    return {"supported": True, "isos": isos}


def iso_names() -> list:
    """返回 ISO_DIR 下的 ISO 文件名列表（纯列表，供自动挑选镜像）。

    与 list_isos() 共用同一个目录：list_isos() 面向 UI 展示（带 size/stat），
    本函数只给生成阶段的 pick_iso() 用，避免为了挑一个文件名做多余 stat。
    """
    if not _dhcp.is_linux() or not os.path.isdir(ISO_DIR):
        return []
    return sorted(f for f in os.listdir(ISO_DIR) if f.lower().endswith(".iso"))


def extract_from_iso(iso_name, os_type="ubuntu", os_version="22.04") -> dict:
    if not _dhcp.is_linux():
        return {"ok": False, "log": ["Linux only"]}
    iso_path = _iso_path(iso_name)
    if iso_path is None:
        return {"ok": False, "log": ["Invalid ISO name: " + str(iso_name)]}
    if not os.path.isfile(iso_path):
        return {"ok": False, "log": ["ISO not found: " + iso_name]}

    # ── U9: path-traversal guard —— 校验必须先于任何目录创建 ──
    ost = (os_type or "").strip().lower()
    if ost not in _ALLOWED_OS_TYPES:
        return {"ok": False, "log": ["Illegal os_type: " + str(os_type)]}
    osv = (os_version or "").strip()
    if osv in (".", "..") or not _VERSION_RE.fullmatch(osv):
        return {"ok": False, "log": ["Illegal os_version: " + str(os_version)]}
    dest = os.path.abspath(os.path.join(WEB_ROOT, ost, osv))
    web_root_abs = os.path.abspath(WEB_ROOT)
    try:
        inside_web = os.path.commonpath([dest, web_root_abs]) == web_root_abs
    except ValueError:
        inside_web = False
    if not inside_web:
        return {"ok": False, "log": ["Illegal dest: " + dest]}

    log = ["Processing: " + iso_name]
    mountpoint = os.path.join(MOUNT_BASE, os.path.basename(iso_path).replace(".iso", ""))
    if not os.path.isdir(mountpoint):
        os.makedirs(mountpoint, exist_ok=True)
        log.append("Created mount: " + mountpoint)
    _dhcp._run(["umount", "-l", mountpoint], sudo=True)
    rc, _, err = _dhcp._run(["mount", "-o", "loop,ro", iso_path, mountpoint], sudo=True)
    if rc != 0:
        log.append("Mount failed: " + err.strip()[:80])
        return {"ok": False, "log": log}
    log.append("Mounted -> " + mountpoint)

    # M4：从挂载成功起，后续所有路径（正常返回 / 未提取到文件的早退 /
    # 复制或 os.listdir 抛异常）都必须卸载。原实现只在"提取到文件之后"卸载一次，
    # 早退与异常路径会把 loop 挂载点与 loop 设备一起泄漏，导致后续提取互相干扰。
    try:
        return _extract_after_mount(mountpoint, dest, ost, log)
    finally:
        _dhcp._run(["umount", "-l", mountpoint], sudo=True)


def _extract_after_mount(mountpoint, dest, ost, log) -> dict:
    """ISO 已挂载在 mountpoint，提取引导文件到 dest。

    调用方 extract_from_iso() 用 try/finally 保证无论本函数如何返回/抛错都会 umount，
    因此本函数内部**不再**自行卸载。返回值结构与本函数拆分前完全一致。
    """
    os.makedirs(dest, exist_ok=True)
    _dhcp._run(["semanage", "fcontext", "-a", "-t", "tftpdir_t", WEB_ROOT + "(/.*)?"], sudo=True)
    _dhcp._run(["restorecon", "-R", WEB_ROOT], sudo=True)

    extracted = []
    if ost in ("ubuntu", "debian"):
        src_dir = os.path.join(mountpoint, "casper")
        if not os.path.isdir(src_dir):
            src_dir = os.path.join(mountpoint, "install")
        for fname, targets in [("vmlinuz", ["vmlinuz"]), ("initrd", ["initrd"])]:
            for t in targets:
                src = os.path.join(src_dir, t)
                if os.path.isfile(src):
                    shutil.copy2(src, os.path.join(dest, fname))
                    extracted.append(fname)
                    break
        sq_files = [f for f in os.listdir(src_dir) if f.endswith(".squashfs")] if os.path.isdir(src_dir) else []
        if sq_files:
            sq_files.sort(key=lambda f: os.path.getsize(os.path.join(src_dir, f)), reverse=True)
            shutil.copy2(os.path.join(src_dir, sq_files[0]), os.path.join(dest, "installer.squashfs"))
            extracted.append("installer.squashfs (" + sq_files[0] + ")")
    elif ost in ("rhel", "centos", "rocky", "alma", "almalinux"):
        src_dir = os.path.join(mountpoint, "images", "pxeboot")
        for fname, tname in [("vmlinuz", "vmlinuz"), ("initrd.img", "initrd.img")]:
            src = os.path.join(src_dir, tname)
            if os.path.isfile(src):
                shutil.copy2(src, os.path.join(dest, fname))
                extracted.append(fname)

    if not extracted:
        log.append("No boot files found in ISO (expected casper/ or images/pxeboot/ boot files); nothing was extracted")
        return {"ok": False, "log": log, "dest": dest, "extracted": extracted}
    log.append("Extracted: " + ", ".join(extracted))
    log.append("Dest: " + dest)

    final = []
    if os.path.isdir(dest):
        for f in os.listdir(dest):
            sz = os.path.getsize(os.path.join(dest, f))
            final.append(f + " (" + str(round(sz / 1048576, 1)) + "MB)")
    log.append("Files: " + ("; ".join(final) if final else "none"))
    return {"ok": True, "log": log, "dest": dest, "extracted": extracted}


def delete_iso(iso_name) -> dict:
    if not _dhcp.is_linux():
        return {"ok": False, "log": ["Linux only"]}
    iso_path = _iso_path(iso_name)
    if iso_path is None:
        return {"ok": False, "log": ["Invalid filename"]}
    if not os.path.isfile(iso_path):
        return {"ok": False, "log": ["File not found"]}
    os.remove(iso_path)
    return {"ok": True, "log": ["Deleted: " + iso_name]}
