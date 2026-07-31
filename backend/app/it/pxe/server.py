"""PXE 服务器本地管控 (Linux)。

让 OpsToolkit 本机直接作为 PXE 服务器：
  - 文件自动落地到 TFTP/HTTP 目录
  - dnsmasq 服务启停重载
  - iPXE 固件准备 (ipxe.efi / undionly.kpxe)
非 Linux 或无 sudo 权限时优雅降级，返回明确提示。
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess

TFTP_ROOT = "/srv/tftp"
WEB_ROOT = "/srv/opstk/pxe-web"
DNSMASQ_CONF = "/etc/dnsmasq.d/opstk-pxe.conf"

# 固件来源 (依赖 ipxe-bootimgs 包, 支持多备选路径)
FIRMWARE = {
    "ipxe.efi": ["/usr/share/ipxe/ipxe-x86_64.efi", "/usr/share/ipxe/ipxe.efi", "/usr/share/ipxe/ipxe-i386.efi"],
    "undionly.kpxe": ["/usr/share/ipxe/undionly.kpxe"],
}

# 需要落地到 HTTP 目录的应答文件
WEB_FILES = ("user-data", "meta-data", "ks.cfg", "boot.ipxe")


def is_linux() -> bool:
    return platform.system() == "Linux"


def _run(cmd, sudo=False, timeout=15, stdin_data=None):
    """执行命令，返回 (rc, stdout, stderr)。sudo 用 -n 免密。"""
    prefix = ["sudo", "-n"] if sudo else []
    full = prefix + cmd if isinstance(cmd, list) else prefix + [cmd]
    data = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
    try:
        p = subprocess.run(full, input=data, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def sudo_ok() -> bool:
    """检测是否可免密 sudo。"""
    rc, _, _ = _run(["true"], sudo=True)
    return rc == 0



def detect_network() -> dict:
    """检测本机主要网卡: 接口名、IP、网关、DHCP 范围。"""
    if not is_linux():
        return {}
    import ipaddress
    # 1. 默认路由 -> 网卡名 + 网关
    rc, out, _ = _run(["ip", "route", "show", "default"])
    iface = ""
    gateway = ""
    for line in out.splitlines():
        parts = line.split()
        for i, p in enumerate(parts):
            if p == "via" and i + 1 < len(parts):
                gateway = parts[i + 1]
            elif p == "dev" and i + 1 < len(parts):
                iface = parts[i + 1]
        break
    if not iface:
        return {}
    # 2. 该网卡的 IP/前缀
    rc, out, _ = _run(["ip", "-o", "-f", "inet", "addr", "show", iface])
    ip_addr = ""
    for line in out.splitlines():
        for tok in line.split():
            if "/" in tok and tok[0].isdigit():
                ip_addr = tok
                break
        break
    # 3. 计算网段 + DHCP 范围 (后 1/4)
    server_ip = ip_addr.split("/")[0] if ip_addr else ""
    dhcp_start = ""
    dhcp_end = ""
    try:
        net = ipaddress.ip_network(ip_addr, strict=False)
        hosts = list(net.hosts())
        if len(hosts) > 10:
            dhcp_start = str(hosts[len(hosts) * 3 // 4])
            dhcp_end = str(hosts[-2])
    except Exception:
        pass
    return {
        "interface": iface,
        "gateway": gateway,
        "server_ip": server_ip,
        "dhcp_start": dhcp_start,
        "dhcp_end": dhcp_end,
        "dns_server": gateway,
    }


def prepare_dirs() -> list:
    """创建 TFTP / HTTP 目录并设置属主。"""
    log = []
    for d in (TFTP_ROOT, WEB_ROOT, os.path.join(TFTP_ROOT, "boot")):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            log.append("创建目录 " + d)
    # 属主设为当前用户，避免每次写文件都要 sudo
    rc, _, err = _run(["chown", "-R", str(os.getuid()) + ":" + str(os.getgid()), TFTP_ROOT, WEB_ROOT], sudo=True)
    if rc == 0:
        log.append("已调整目录属主")
    else:
        log.append("调整属主跳过(可能需 root): " + err.strip()[:60])
    # SELinux: 确保 TFTP 目录有正确的安全上下文 (Rocky/RHEL)
    _selinux_fix(log)
    return log


def _selinux_fix(log=None):
    """修复 SELinux 上下文，使 dnsmasq TFTP 能访问 /srv/tftp。"""
    log = log if log is not None else []
    rc, out, _ = _run(["getenforce"])
    if out.strip() != "Enforcing":
        return log
    # 持久 fcontext 规则 (忽略已存在的报错)
    _run(["semanage", "fcontext", "-a", "-t", "tftpdir_t", TFTP_ROOT + "(/.*)?"], sudo=True)
    rc2, _, _ = _run(["restorecon", "-R", TFTP_ROOT], sudo=True)
    if rc2 == 0:
        log.append("SELinux 上下文已修复 (tftpdir_t)")
    return log


def prepare_firmware() -> list:
    """从系统包复制 iPXE 固件到 TFTP 目录。"""
    log = []
    for name, sources in FIRMWARE.items():
        dst = os.path.join(TFTP_ROOT, name)
        if os.path.exists(dst):
            log.append("固件已存在 " + name)
            continue
        copied = False
        for src in sources:
            if os.path.exists(src):
                shutil.copy2(src, dst)
                log.append("复制固件 " + name + " <- " + os.path.basename(src))
                copied = True
                break
        if not copied:
            log.append("缺固件 " + name + " (需 dnf install ipxe-bootimgs)")
    return log


def deploy_files(files, pid="") -> dict:
    """将生成的配置文件落地到本机实际路径。"""
    if not is_linux():
        return {"ok": False, "platform": platform.system(),
                "log": ["仅支持 Linux 环境本机部署，开发环境请用「下载 ZIP」"]}
    log = list(prepare_dirs())
    log += prepare_firmware()
    if not sudo_ok():
        log.append("警告: 无免密 sudo，dnsmasq 配置与重启将失败 (需配置 sudoers)")

    # 1. dnsmasq.conf -> /etc/dnsmasq.d
    rc, _, err = _run(["tee", DNSMASQ_CONF], sudo=True, stdin_data=files.get("dnsmasq.conf", ""))
    if rc == 0:
        log.append("已写入 " + DNSMASQ_CONF)
    else:
        log.append("写 dnsmasq 配置失败: " + err.strip()[:80])

    # 2. 应答文件 -> HTTP 目录
    for name in WEB_FILES:
        if name in files:
            with open(os.path.join(WEB_ROOT, name), "w", encoding="utf-8") as f:
                f.write(files[name])
            log.append("落地 " + name)

    # 3. 重启 dnsmasq
    svc = service_control("restart")
    log.append("dnsmasq restart: " + svc.get("msg", "unknown"))
    return {"ok": svc["ok"], "log": log, "tftp_root": TFTP_ROOT, "web_root": WEB_ROOT}


def service_control(action) -> dict:
    """start/stop/restart/reload/status。"""
    if not is_linux():
        return {"ok": False, "msg": "非 Linux"}
    action = (action or "status").lower()
    if action == "status":
        rc, out, _ = _run(["systemctl", "is-active", "dnsmasq"])
        active = out.strip() == "active"
        rc2, out2, _ = _run(["systemctl", "is-enabled", "dnsmasq"])
        return {"ok": True, "active": active, "enabled": out2.strip() == "enabled",
                "msg": "active" if active else "inactive"}
    rc, out, err = _run(["systemctl", action, "dnsmasq"], sudo=True)
    ok = rc == 0
    return {"ok": ok, "msg": (out.strip() or err.strip() or ("ok" if ok else "fail"))[:120]}


def server_status() -> dict:
    """综合状态: dnsmasq + TFTP 文件 + HTTP 文件 + 端口。"""
    if not is_linux():
        return {"supported": False, "platform": platform.system()}
    svc = service_control("status")
    # TFTP 目录文件
    tftp_files = []
    if os.path.isdir(TFTP_ROOT):
        for root, _, fs in os.walk(TFTP_ROOT):
            for f in fs:
                rel = os.path.relpath(os.path.join(root, f), TFTP_ROOT)
                tftp_files.append(rel)
    # HTTP 目录文件
    web_files = []
    if os.path.isdir(WEB_ROOT):
        web_files = os.listdir(WEB_ROOT)
    # 监听端口 (67 dhcp / 69 tftp)
    rc, out, _ = _run(["ss", "-lun"])
    ports = []
    for line in out.splitlines():
        if ":67 " in line or ":69 " in line:
            ports.append(line.split()[4] if len(line.split()) > 4 else line.strip())
    return {
        "supported": True,
        "dnsmasq": svc,
        "tftp_root": TFTP_ROOT,
        "web_root": WEB_ROOT,
        "tftp_files": sorted(tftp_files),
        "web_files": sorted(web_files),
        "ports": ports,
        "sudo_ok": sudo_ok(),
    }
