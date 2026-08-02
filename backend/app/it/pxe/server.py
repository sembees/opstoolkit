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
    "ipxe.efi": ["/usr/share/ipxe/ipxe-x86_64.efi", "/usr/share/ipxe/ipxe.efi", "/usr/share/ipxe/ipxe-i386.efi", "/usr/lib/ipxe/ipxe.efi"],
    "undionly.kpxe": ["/usr/share/ipxe/undionly.kpxe", "/usr/lib/ipxe/undionly.kpxe"],
}

# 需要落地到 HTTP 目录的应答文件


def is_linux() -> bool:
    return platform.system() == "Linux"


def _is_root() -> bool:
    """?????? root ??, ?? sudo?"""
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False  # Windows ? geteuid


def _run(cmd, sudo=False, timeout=15, stdin_data=None):
    """执行命令，返回 (rc, stdout, stderr)。sudo 用 -n 免密。"""
    # ??? root ??? sudo ???
    need_sudo = sudo and not _is_root()
    prefix = ["sudo", "-n"] if need_sudo else []
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
        # 没有默认路由时，取第一个活的物理接口作为回退（不依赖 ip 命令）
        try:
            for iface2 in os.listdir("/sys/class/net"):
                if iface2 == "lo" or iface2.startswith(("docker", "veth", "br-", "virbr")):
                    continue
                try:
                    state = open(f"/sys/class/net/{iface2}/operstate").read().strip()
                except Exception:
                    state = ""
                if state == "up":
                    iface = iface2
                    break
        except Exception:
            pass
        if not iface:
            # 再试 ip 命令（宿主机环境）
            rc, out, _ = _run(["ip", "-o", "-f", "inet", "addr", "show"])
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    iface2 = parts[1]
                    if iface2 == "lo" or iface2.startswith(("docker", "veth", "br-", "virbr")):
                        continue
                    iface = iface2
                    for tok in parts[2:]:
                        if "/" in tok and tok[0].isdigit():
                            ip_addr = tok
                            break
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

    # 2. 应答文件 -> HTTP 目录 (支持嵌套目录, 如 boot/<mac>.ipxe)
    web_root_abs = os.path.abspath(WEB_ROOT)
    for name, content in files.items():
        if name == "dnsmasq.conf":
            continue
        rel = os.path.normpath(name.lstrip("/"))
        if rel == "." or rel.startswith(".."):
            log.append("跳过非法路径 " + name)
            continue
        dst = os.path.abspath(os.path.join(web_root_abs, rel))
        if os.path.commonpath([dst, web_root_abs]) != web_root_abs:
            log.append("跳过越界路径 " + name)
            continue
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content)
        log.append("落地 " + rel)

    # 3. 重启 dnsmasq
    svc = service_control("restart")
    log.append("dnsmasq restart: " + svc.get("msg", "unknown"))
    return {"ok": svc["ok"], "log": log, "tftp_root": TFTP_ROOT, "web_root": WEB_ROOT}


def service_control(action) -> dict:
    """start/stop/restart/reload/status。支持 systemd 和直接进程管理。"""
    if not is_linux():
        return {"ok": False, "msg": "非 Linux"}
    action = (action or "status").lower()

    # 检测是否有 systemd（容器内通常没有）
    has_systemd = os.path.isfile("/run/systemd/system")

    if action == "status":
        if has_systemd:
            rc, out, _ = _run(["systemctl", "is-active", "dnsmasq"])
            active = out.strip() == "active"
            rc2, out2, _ = _run(["systemctl", "is-enabled", "dnsmasq"])
            return {"ok": True, "active": active, "enabled": out2.strip() == "enabled",
                    "msg": "active" if active else "inactive"}
        else:
            # 直接检查 dnsmasq 进程
            # 排除僵尸进程：用 ps 检查活着的 dnsmasq
            rc, out, _ = _run(["pgrep", "-x", "dnsmasq"])
            active = rc == 0
            if active:
                # 确认不是僵尸进程
                rc2, out2, _ = _run(["ps", "-p", out.strip().split()[0] if out.strip() else "", "-o", "stat="])
                if rc2 == 0 and "Z" in (out2 or ""):
                    active = False
            return {"ok": True, "active": active, "enabled": False,
                    "msg": "active" if active else "inactive"}

    # 容器环境：直接管理 dnsmasq 进程
    if not has_systemd:
        pid_file = "/var/run/dnsmasq-opstk.pid"
        if action in ("stop", "restart"):
            # 停止现有进程
            _run(["pkill", "dnsmasq"])
            # 清理旧 PID 文件
            if os.path.exists(pid_file):
                os.remove(pid_file)
        if action in ("start", "restart"):
            if os.path.exists(DNSMASQ_CONF):
                rc, out, err = _run(
                    ["dnsmasq", "--conf-file=" + DNSMASQ_CONF, "--pid-file=" + pid_file,
                     "--keep-in-foreground"], timeout=5
                )
                # dnsmasq with --keep-in-foreground will block, run it in background via shell
                import subprocess as _sp
                try:
                    _sp.Popen(
                        ["dnsmasq", "--conf-file=" + DNSMASQ_CONF, "--pid-file=" + pid_file],
                        stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
                        start_new_session=True, close_fds=True
                    )
                    return {"ok": True, "msg": "dnsmasq 已启动"}
                except Exception as e:
                    return {"ok": False, "msg": "启动失败: " + str(e)[:80]}
            else:
                return {"ok": False, "msg": "配置文件不存在: " + DNSMASQ_CONF}
        return {"ok": True, "msg": action + " OK"}

    # systemd 环境
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


# ---------- ISO ?? ----------

ISO_DIR = "/srv/opstk/iso"
MOUNT_BASE = "/srv/opstk/mnt"


def _iso_path(iso_name) -> str | None:
    """规范化并校验 ISO 文件名，防止路径穿越。"""
    raw = (iso_name or "").strip()
    if "/" in raw or "\\" in raw:
        return None
    name = os.path.basename(raw)
    if not name.lower().endswith(".iso"):
        return None
    iso_abs = os.path.abspath(ISO_DIR)
    path = os.path.abspath(os.path.join(ISO_DIR, name))
    if os.path.commonpath([path, iso_abs]) != iso_abs:
        return None
    return path


def list_isos() -> dict:
    """列出已上传的 ISO 文件。"""
    if not is_linux():
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


def extract_from_iso(iso_name, os_type="ubuntu", os_version="22.04") -> dict:
    """挂载 ISO 并提取 PXE 引导文件 (vmlinuz/initrd/squashfs)。
    Ubuntu: casper/ 目录下的 vmlinuz, initrd, *.squashfs
    RHEL:   images/pxeboot/ 目录下的 vmlinuz, initrd.img
    """
    if not is_linux():
        return {"ok": False, "log": ["仅支持 Linux 环境"]}
    iso_path = _iso_path(iso_name)
    if iso_path is None:
        return {"ok": False, "log": ["非法的 ISO 文件名: " + str(iso_name)]}
    if not os.path.isfile(iso_path):
        return {"ok": False, "log": ["ISO 不存在: " + iso_name]}
    log = ["开始处理 " + iso_name]
    mountpoint = os.path.join(MOUNT_BASE, os.path.basename(iso_path).replace(".iso", ""))
    # 确保 mountpoint 存在
    if not os.path.isdir(mountpoint):
        os.makedirs(mountpoint, exist_ok=True)
        log.append("创建挂载点 " + mountpoint)
    # 先卸载 (防止残留)
    _run(["umount", "-l", mountpoint], sudo=True)
    # 挂载 ISO
    rc, _, err = _run(["mount", "-o", "loop,ro", iso_path, mountpoint], sudo=True)
    if rc != 0:
        log.append("挂载失败: " + err.strip()[:80])
        return {"ok": False, "log": log}
    log.append("已挂载 -> " + mountpoint)
    # 插入清理钩子 (防止异常后残留挂载)
    import atexit
    atexit.register(lambda: _run(["umount", "-l", mountpoint], sudo=True))
    # 目标目录
    dest = os.path.join(WEB_ROOT, os_type, os_version)
    os.makedirs(dest, exist_ok=True)
    # SELinux 修复 (Web 目录供 dnsmasq 访问)
    _run(["semanage", "fcontext", "-a", "-t", "tftpdir_t", WEB_ROOT + "(/.*)?"], sudo=True)
    _run(["restorecon", "-R", WEB_ROOT], sudo=True)
    # 提取文件
    extracted = []
    ost = os_type.strip().lower()
    if ost in ("ubuntu", "debian"):
        # Ubuntu live-server: casper/ 下
        src_dir = os.path.join(mountpoint, "casper")
        if not os.path.isdir(src_dir):
            src_dir = os.path.join(mountpoint, "install")
        for fname, targets in [
            ("vmlinuz", ["vmlinuz"]),
            ("initrd", ["initrd"]),
        ]:
            for t in targets:
                src = os.path.join(src_dir, t)
                if os.path.isfile(src):
                    import shutil
                    shutil.copy2(src, os.path.join(dest, fname))
                    extracted.append(fname)
                    break
        # squashfs: 找最大的那个
        sq_files = [f for f in os.listdir(src_dir) if f.endswith(".squashfs")] if os.path.isdir(src_dir) else []
        if sq_files:
            sq_files.sort(key=lambda f: os.path.getsize(os.path.join(src_dir, f)), reverse=True)
            import shutil
            shutil.copy2(os.path.join(src_dir, sq_files[0]), os.path.join(dest, "installer.squashfs"))
            extracted.append("installer.squashfs (" + sq_files[0] + ")")
    elif ost in ("rhel", "centos", "rocky", "alma", "almalinux"):
        # RHEL 系: images/pxeboot/
        src_dir = os.path.join(mountpoint, "images", "pxeboot")
        for fname, tname in [("vmlinuz", "vmlinuz"), ("initrd.img", "initrd.img")]:
            src = os.path.join(src_dir, tname)
            if os.path.isfile(src):
                import shutil
                shutil.copy2(src, os.path.join(dest, fname))
                extracted.append(fname)
    # 卸载
    _run(["umount", "-l", mountpoint], sudo=True)
    log.append("已提取: " + ", ".join(extracted) if extracted else "未找到引导文件 (检查 ISO 结构)")
    log.append("目标目录: " + dest)
    # 列出提取后的文件
    final = []
    if os.path.isdir(dest):
        for f in os.listdir(dest):
            sz = os.path.getsize(os.path.join(dest, f))
            final.append(f + " (" + str(round(sz / 1048576, 1)) + "MB)")
    log.append("当前文件: " + "; ".join(final) if final else "无")
    return {"ok": True, "log": log, "dest": dest, "extracted": extracted}


def delete_iso(iso_name) -> dict:
    """删除 ISO 文件。"""
    if not is_linux():
        return {"ok": False, "log": ["仅 Linux"]}
    iso_path = _iso_path(iso_name)
    if iso_path is None:
        return {"ok": False, "log": ["非法文件名"]}
    if not os.path.isfile(iso_path):
        return {"ok": False, "log": ["文件不存在"]}
    os.remove(iso_path)
    return {"ok": True, "log": ["已删除 " + iso_name]}
