"""ZTP server management (Linux).

OpsToolkit host acts as a ZTP startup server:
  - Config files auto-deployed to TFTP/HTTP directories
  - dnsmasq lifecycle managed by shared DHCP module (app.core.dhcp)
Non-Linux or no-sudo gracefully degrades.
"""
from __future__ import annotations

import os
import platform
import subprocess

from app.core import dhcp as _dhcp

TFTP_ROOT = "/srv/tftp"
WEB_ROOT = "/srv/opstk/ztp-web"


# ── Delegated to shared DHCP module ──

def is_linux() -> bool:
    return _dhcp.is_linux()


def sudo_ok() -> bool:
    return _dhcp.sudo_ok()


def server_status() -> dict:
    """Combined status: shared DHCP + ZTP-specific directories."""
    dhcp_st = _dhcp.dhcp_status()
    if not dhcp_st["supported"]:
        return {
            "supported": False,
            "dirs": {},
            "dnsmasq": {"installed": False, "active": False, "message": "Linux only"},
        }
    dirs = {}
    for name, d in [
        ("tftp", TFTP_ROOT),
        ("tftp_ztp", os.path.join(TFTP_ROOT, "ztp")),
        ("web", WEB_ROOT),
    ]:
        dirs[name] = os.path.isdir(d)
    return {
        "supported": True,
        "dirs": dirs,
        "dnsmasq": {
            "installed": True,
            "active": dhcp_st["running"],
            "message": "running" if dhcp_st["running"] else "stopped",
        },
        "sudo_ok": dhcp_st["sudo_ok"],
    }


def service_control(action: str) -> dict:
    """Delegate to shared DHCP module."""
    result = _dhcp.dhcp_control(action)
    return {
        "ok": result["ok"],
        "action": result["action"],
        "log": [result["msg"]],
        "active": result["running"],
    }


# ── Directory prep ──

def prepare_dirs() -> list:
    """Create TFTP/HTTP dirs and adjust ownership."""
    log = _dhcp.ensure_dirs([TFTP_ROOT, os.path.join(TFTP_ROOT, "ztp"), WEB_ROOT])
    return log


def _safe_dst(root: str, name: str):
    """Compute safe landing path, prevent traversal."""
    root_abs = os.path.abspath(root)
    rel = os.path.normpath(name.lstrip("/"))
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        return None
    dst = os.path.abspath(os.path.join(root_abs, rel))
    if os.path.commonpath([dst, root_abs]) != root_abs:
        return None
    return dst


# ── Deploy ──

def deploy_files(files: dict, tid: str = "") -> dict:
    """One-click deploy: write dnsmasq config + response files to TFTP/HTTP
       + restart dnsmasq."""
    if not _dhcp.is_linux():
        return {"ok": False, "log": ["Linux only"]}
    log = prepare_dirs()
    files = files or {}

    # Write dnsmasq config via shared module
    dnsmasq_content = files.get("dnsmasq.conf", "")
    if dnsmasq_content:
        ok = _dhcp.write_conf("opstk-ztp.conf", dnsmasq_content)
        if ok:
            log.append("Written: /etc/dnsmasq.d/opstk-ztp.conf")
        else:
            log.append("FAILED: write dnsmasq config")

    # Write response files to both TFTP and HTTP
    written = 0
    for name, content in files.items():
        if name == "dnsmasq.conf":
            continue
        for root in (TFTP_ROOT, WEB_ROOT):
            dst = _safe_dst(root, name)
            if dst is None:
                log.append("Skip illegal path: " + name)
                continue
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
            written += 1
    log.append("Deployed " + str(written) + " files (TFTP + HTTP)")

    # Restart via shared module
    svc = _dhcp.dhcp_control("restart")
    log.append("dnsmasq: " + svc.get("msg", "unknown"))
    return {"ok": svc.get("ok", False), "log": log, "dnsmasq": svc}
