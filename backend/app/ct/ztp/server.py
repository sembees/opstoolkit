"""ZTP 服务本场管控 (Linux)。

让 OpsToolkit 本机直接作为 ZTP 开局服务器:
  - 配置文件自动落地 TFTP/HTTP 目录
  - dnsmasq 服务启停重载
非 Linux 或无 sudo 权限时优雅降级，返回明确提示。
"""
from __future__ import annotations

import os
import platform
import subprocess

TFTP_ROOT = "/srv/tftp"
WEB_ROOT = "/srv/opstk/ztp-web"
DNSMASQ_CONF = "/etc/dnsmasq.d/opstk-ztp.conf"


def is_linux() -> bool:
    return platform.system() == "Linux"


def _is_root() -> bool:
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _run(cmd, sudo=False, timeout=15):
    """执行命令，返回 (rc, stdout, stderr)。sudo 用 -n 免密。"""
    need_sudo = sudo and not _is_root()
    full = (["sudo", "-n"] if need_sudo else []) + list(cmd)
    try:
        p = subprocess.run(full, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def sudo_ok() -> bool:
    if not is_linux():
        return False
    rc, _, _ = _run(["true"], sudo=True)
    return rc == 0


def prepare_dirs() -> list:
    """创建 TFTP / HTTP 目录并调整属主。"""
    log = []
    for d in (TFTP_ROOT, os.path.join(TFTP_ROOT, "ztp"), WEB_ROOT):
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            log.append("创建目录 " + d)
    if is_linux():
        rc, _, err = _run(["chown", "-R", str(os.getuid()) + ":" + str(os.getgid()), TFTP_ROOT, WEB_ROOT], sudo=True)
        if rc == 0:
            log.append("已调整目录属主")
        else:
            log.append("调整属主跳过(可能需 root): " + err.strip()[:60])
    return log


def server_status() -> dict:
    """查看本机 ZTP 服务状态 (TFTP/HTTP 目录 + dnsmasq)。"""
    if not is_linux():
        return {"supported": False, "dirs": {}, "dnsmasq": {"installed": False, "active": False, "message": "仅支持 Linux"}}
    dirs = {}
    for name, d in [("tftp", TFTP_ROOT), ("tftp_ztp", os.path.join(TFTP_ROOT, "ztp")), ("web", WEB_ROOT)]:
        dirs[name] = os.path.isdir(d)
    rc, out, _ = _run(["systemctl", "is-active", "dnsmasq"])
    active = rc == 0
    return {
        "supported": True,
        "dirs": dirs,
        "dnsmasq": {"installed": True, "active": active, "message": out.strip() or ("active" if active else "inactive")},
        "sudo_ok": sudo_ok(),
    }


def service_control(action: str) -> dict:
    """控制 dnsmasq: start/stop/restart/reload/status。"""
    action = (action or "status").strip().lower()
    if action not in ("start", "stop", "restart", "reload", "status"):
        return {"ok": False, "log": ["不支持的操作: " + action]}
    if not is_linux():
        return {"ok": False, "log": ["仅支持 Linux 环境"]}
    rc, out, err = _run(["systemctl", action, "dnsmasq"], sudo=(action != "status"))
    log = [out.strip()] if out.strip() else []
    if err.strip():
        log.append(err.strip()[:120])
    return {"ok": rc == 0, "action": action, "log": log, "active": server_status().get("dnsmasq", {}).get("active", False)}


def _safe_dst(root: str, name: str):
    """核算安全的落地路径，防止路径穿越。"""
    root_abs = os.path.abspath(root)
    rel = os.path.normpath(name.lstrip("/"))
    if rel == "." or rel.startswith("..") or os.path.isabs(rel):
        return None
    dst = os.path.abspath(os.path.join(root_abs, rel))
    if os.path.commonpath([dst, root_abs]) != root_abs:
        return None
    return dst


def deploy_files(files: dict, tid: str = "") -> dict:
    """一键部署: 写 dnsmasq 配置 + 应答文件落地 TFTP/HTTP + 重启服务。"""
    if not is_linux():
        return {"ok": False, "log": ["仅支持 Linux 环境"]}
    log = prepare_dirs()
    files = files or {}

    if files.get("dnsmasq.conf"):
        try:
            with open(DNSMASQ_CONF, "w", encoding="utf-8") as fh:
                fh.write(files["dnsmasq.conf"])
            log.append("落地 " + DNSMASQ_CONF)
        except Exception as e:  # noqa: BLE001
            log.append("写 dnsmasq 配置失败: " + str(e)[:120])

    written = 0
    for name, content in files.items():
        if name == "dnsmasq.conf":
            continue
        for root in (TFTP_ROOT, WEB_ROOT):
            dst = _safe_dst(root, name)
            if dst is None:
                log.append("跳过非法路径 " + name)
                continue
            parent = os.path.dirname(dst)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
            written += 1
    log.append("落地文件 " + str(written) + " 个 (TFTP + HTTP)")

    svc = service_control("restart")
    log.extend(svc.get("log", []))
    return {"ok": bool(svc.get("ok")), "log": log, "dnsmasq": svc}
