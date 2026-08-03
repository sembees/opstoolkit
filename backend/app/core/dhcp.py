"""Unified DHCP (dnsmasq) management shared by PXE and ZTP.

Both PXE and ZTP use the SAME dnsmasq instance. Configs are written
to /etc/dnsmasq.d/ and dnsmasq auto-loads them all.
"""
from __future__ import annotations

import os
import platform
import subprocess
import time

CONF_DIR = "/etc/dnsmasq.d"
PXE_CONF = os.path.join(CONF_DIR, "opstk-pxe.conf")
ZTP_CONF = os.path.join(CONF_DIR, "opstk-ztp.conf")
PID_FILE = "/var/run/dnsmasq-opstk.pid"


def is_linux():
    return platform.system() == "Linux"


def _is_root():
    try:
        return os.geteuid() == 0
    except AttributeError:
        return False


def _run(cmd, sudo=False, timeout=15, stdin_data=None):
    need_sudo = sudo and not _is_root()
    prefix = ["sudo", "-n"] if need_sudo else []
    full = prefix + list(cmd) if isinstance(cmd, list) else prefix + [cmd]
    data = stdin_data.encode() if isinstance(stdin_data, str) else stdin_data
    try:
        p = subprocess.run(full, input=data, capture_output=True, timeout=timeout)
        return p.returncode, p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
    except FileNotFoundError as e:
        return 127, "", str(e)
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"


def sudo_ok():
    if not is_linux():
        return False
    rc, _, _ = _run(["true"], sudo=True)
    return rc == 0


def write_conf(name, content):
    os.makedirs(CONF_DIR, exist_ok=True)
    path = os.path.join(CONF_DIR, name)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except PermissionError:
        rc, _, err = _run(["tee", path], sudo=True, stdin_data=content)
        return rc == 0
    except Exception:
        return False


def remove_conf(name):
    path = os.path.join(CONF_DIR, name)
    if not os.path.exists(path):
        return True
    try:
        os.remove(path)
        return True
    except PermissionError:
        rc, _, _ = _run(["rm", "-f", path], sudo=True)
        return rc == 0


def _find_dnsmasq_pids(skip_zombies=True):
    pids = []
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                comm = open(f"/proc/{pid}/comm").read().strip()
            except Exception:
                continue
            if comm != "dnsmasq":
                continue
            if skip_zombies:
                try:
                    status = open(f"/proc/{pid}/status").read()
                    for line in status.splitlines():
                        if line.startswith("State:"):
                            if "zombie" in line.lower():
                                pid = -1
                            break
                except Exception:
                    pass
            if pid > 0:
                pids.append(pid)
    except Exception:
        pass
    return pids


def dhcp_status():
    result = {
        "supported": is_linux(),
        "running": False,
        "pids": [],
        "conf_files": [],
        "pid_file": PID_FILE,
        "has_systemd": False,
        "sudo_ok": sudo_ok(),
    }
    if not is_linux():
        result["platform"] = platform.system()
        return result
    has_systemd = os.path.isfile("/run/systemd/system")
    result["has_systemd"] = has_systemd
    if has_systemd:
        rc, out, _ = _run(["systemctl", "is-active", "dnsmasq"])
        result["running"] = out.strip() == "active"
    else:
        pids = _find_dnsmasq_pids(skip_zombies=True)
        result["pids"] = pids
        result["running"] = len(pids) > 0
    conf_files = []
    if os.path.isdir(CONF_DIR):
        try:
            conf_files = sorted(f for f in os.listdir(CONF_DIR) if f.endswith(".conf"))
        except Exception:
            pass
    result["conf_files"] = conf_files
    return result


def dhcp_control(action):
    action = (action or "status").strip().lower()
    if action not in ("start", "stop", "restart", "status"):
        return {"ok": False, "action": action, "msg": "unsupported: " + action, "running": False}
    if not is_linux():
        return {"ok": False, "action": action, "msg": "Linux only", "running": False}
    has_systemd = os.path.isfile("/run/systemd/system")
    if action == "status":
        st = dhcp_status()
        return {"ok": True, "action": "status", "msg": "running" if st["running"] else "stopped", "running": st["running"]}
    if not has_systemd:
        if action in ("stop", "restart"):
            pids = _find_dnsmasq_pids(skip_zombies=True)
            for pid in pids:
                try:
                    os.kill(pid, 15)
                except Exception:
                    pass
            time.sleep(0.5)
            survivors = _find_dnsmasq_pids(skip_zombies=True)
            for pid in survivors:
                try:
                    os.kill(pid, 9)
                except Exception:
                    pass
            time.sleep(0.2)
            if os.path.exists(PID_FILE):
                try:
                    os.remove(PID_FILE)
                except Exception:
                    pass
        if action in ("start", "restart"):
            os.makedirs(CONF_DIR, exist_ok=True)
            try:
                subprocess.Popen([
                    "dnsmasq", "--pid-file=" + PID_FILE
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                 start_new_session=True, close_fds=True)
                time.sleep(0.5)
                running = len(_find_dnsmasq_pids(skip_zombies=True)) > 0
                return {"ok": running, "action": action, "msg": "dnsmasq started" if running else "dnsmasq start failed", "running": running}
            except Exception as e:
                return {"ok": False, "action": action, "msg": "start failed: " + str(e)[:80], "running": False}
        return {"ok": True, "action": action, "msg": action + " OK", "running": dhcp_status()["running"]}
    rc, out, err = _run(["systemctl", action, "dnsmasq"], sudo=(action != "status"))
    running = False
    if action != "stop":
        rc2, out2, _ = _run(["systemctl", "is-active", "dnsmasq"])
        running = out2.strip() == "active"
    return {"ok": rc == 0, "action": action, "msg": (out.strip() or err.strip() or ("ok" if rc == 0 else "fail"))[:120], "running": running}


def ensure_dirs(extra_dirs=None):
    log = []
    dirs = [CONF_DIR]
    if extra_dirs:
        dirs.extend(extra_dirs)
    for d in dirs:
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
            log.append("Created dir: " + d)
    if is_linux():
        uid = os.getuid()
        gid = os.getgid()
        for d in dirs:
            if d:
                rc, _, err = _run(["chown", "-R", str(uid) + ":" + str(gid), d], sudo=True)
                if rc != 0:
                    log.append("chown skip: " + d)
        rc_enf, enf_out, _ = _run(["getenforce"])
        if enf_out.strip() == "Enforcing":
            for d in extra_dirs or []:
                if d:
                    _run(["semanage", "fcontext", "-a", "-t", "tftpdir_t", d + "(/.*)?"], sudo=True)
                    rc2, _, _ = _run(["restorecon", "-R", d], sudo=True)
                    if rc2 == 0:
                        log.append("SELinux fixed: " + d)
    return log
