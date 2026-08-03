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
import shutil
import subprocess

from app.core import dhcp as _dhcp

TFTP_ROOT = "/srv/tftp"
WEB_ROOT = "/srv/opstk/pxe-web"

FIRMWARE = {
    "ipxe.efi": [
        "/usr/share/ipxe/ipxe-x86_64.efi",
        "/usr/share/ipxe/ipxe.efi",
        "/usr/share/ipxe/ipxe-i386.efi",
        "/usr/lib/ipxe/ipxe.efi",
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
    rc, out, _ = _dhcp._run(["ss", "-lun"])
    ports = []
    for line in out.splitlines():
        if ":67 " in line or ":69 " in line:
            parts = line.split()
            ports.append(parts[4] if len(parts) > 4 else line.strip())

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
    """Detect host primary interface, IP, gateway, DHCP range."""
    if not _dhcp.is_linux():
        return {}
    import ipaddress

    # 1. Default route -> iface + gateway
    rc, out, _ = _dhcp._run(["ip", "route", "show", "default"])
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
            rc, out, _ = _dhcp._run(["ip", "-o", "-f", "inet", "addr", "show"])
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    iface2 = parts[1]
                    if iface2 == "lo" or iface2.startswith(("docker", "veth", "br-", "virbr")):
                        continue
                    iface = iface2
                    break
        if not iface:
            return {}

    # 2. IP/prefix
    rc, out, _ = _dhcp._run(["ip", "-o", "-f", "inet", "addr", "show", iface])
    ip_addr = ""
    for line in out.splitlines():
        for tok in line.split():
            if "/" in tok and tok[0].isdigit():
                ip_addr = tok
                break
        break

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


def _iso_path(iso_name) -> str | None:
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


def extract_from_iso(iso_name, os_type="ubuntu", os_version="22.04") -> dict:
    if not _dhcp.is_linux():
        return {"ok": False, "log": ["Linux only"]}
    iso_path = _iso_path(iso_name)
    if iso_path is None:
        return {"ok": False, "log": ["Invalid ISO name: " + str(iso_name)]}
    if not os.path.isfile(iso_path):
        return {"ok": False, "log": ["ISO not found: " + iso_name]}
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

    dest = os.path.join(WEB_ROOT, os_type, os_version)
    os.makedirs(dest, exist_ok=True)
    _dhcp._run(["semanage", "fcontext", "-a", "-t", "tftpdir_t", WEB_ROOT + "(/.*)?"], sudo=True)
    _dhcp._run(["restorecon", "-R", WEB_ROOT], sudo=True)

    extracted = []
    ost = os_type.strip().lower()
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

    _dhcp._run(["umount", "-l", mountpoint], sudo=True)
    log.append("Extracted: " + ", ".join(extracted) if extracted else "No boot files found")
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
