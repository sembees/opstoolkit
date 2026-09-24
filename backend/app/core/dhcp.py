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


def _in_container():
    """Best-effort container detection (union of both signals).

    True when /.dockerenv exists, or when /proc/1/cgroup mentions
    docker/containerd. Any read error counts as "not a container".
    """
    try:
        if os.path.exists("/.dockerenv"):
            return True
    except Exception:
        pass
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as f:
            cgroup = f.read()
    except Exception:
        return False
    return ("docker" in cgroup) or ("containerd" in cgroup)


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


def _port_67_listening() -> bool:
    """共享网络命名空间里是否已有 UDP :67 监听（即宿主机 dnsmasq 在跑）。

    为什么不能靠进程判断：容器与宿主机共享**网络**命名空间，但**不共享 PID**
    命名空间，所以容器内 `_find_dnsmasq_pids()` 看不到宿主机的 dnsmasq。
    /proc/net/udp 反映的是网络命名空间，因此用它判断监听状态是可靠的
    （端口是 hex：0x43 = 67）。`it/pxe/server.py` 的 `_listen_ports_from_proc()`
    用的是同一套事实。
    """
    try:
        with open("/proc/net/udp", encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2 and parts[1].endswith(":0043"):
                    return True
    except OSError:
        pass
    return False


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
        # 容器内看不到宿主机的 dnsmasq 进程，但共享网络命名空间：
        # :67 有监听即说明 dnsmasq 在跑（宿主机的那个）。
        result["running"] = len(pids) > 0 or _port_67_listening()
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
    if _in_container():
        # Never kill or spawn dnsmasq from inside a container: the daemon is
        # owned by the host. A second dnsmasq here races the host one for
        # ports 67/69 and breaks PXE (see P0/U8 incident). Config files are
        # still written by the callers; reloading is delegated to the host,
        # e.g. via the opstk-dnsmasq-reload.path/.service units.
        return {"ok": True, "action": action, "managed": False,
                "msg": "运行在容器内，dnsmasq 由宿主机管理；配置已写入，请由宿主机外部重载",
                "running": len(_find_dnsmasq_pids(skip_zombies=True)) > 0}
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
