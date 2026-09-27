"""Unified DHCP (dnsmasq) management shared by PXE and ZTP.

Both PXE and ZTP use the SAME dnsmasq instance. Configs are written
to /etc/dnsmasq.d/ and dnsmasq auto-loads them all.
"""
from __future__ import annotations

import hashlib
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


# ── 宿主机重载握手（容器部署下唯一可行的通道：共享文件系统）──────────────
# 背景（RUNBOOK-STATE §5.41，真机并发装机时实测发现）：本项目的容器是
# network_mode: host + privileged，但**既没挂 /run/dbus，也没有 pid: host** ——
# 它物理上无法命令宿主机的 systemd，因此 dhcp_control() 在容器里只能"写文件、
# 不重载"。而 /etc/dnsmasq.d 与 /srv/opstk 是挂载进来的，于是用标记文件握手：
#
#   宿主机的 opstk-dnsmasq-reload.path 监视 opstk-pxe.conf
#     → 触发 opstk-dnsmasq-reload.service
#       → 跑 opstk-dnsmasq-reload.sh（dnsmasq --test 通过才 restart）
#         → 把 "OK <配置sha> <时间>" 写进 HOST_RELOAD_STATE
#
# 应用侧据此核对"跑起来的确实是刚写的这份"。**核不过必须报失败**：
# 只写文件不重载时，磁盘上的文件与正在跑的守护进程会不一致 —— 而磁盘上的
# 扁平 boot.ipxe 可能已被改成"未登记机器一律拒绝安装"的安全菜单，于是
# 旧配置把所有机器都指向它 → 全网装机循环卡死，接口却返回 ok=true、
# 界面显示"部署完成"。这个假成功正是本缺陷被发现的方式。
# 状态目录**必须**是 compose 挂进来的路径。踩过的坑：最初把标记放在
# /srv/opstk/.dnsmasq-reload.state —— 而 compose 只挂了
# /srv/opstk/{pxe-web,iso,mnt}，没挂 /srv/opstk 本身，于是宿主机脚本写得好好的，
# 容器里 open() 永远 ENOENT，表现为"重载明明成功却一直报失败"。
# 因此单独开一个 /srv/opstk/state 并在 compose 里挂上（不放在 pxe-web 下，
# 那个目录是 HTTP 根，标记文件会被暴露到装机网络上）。
HOST_RELOAD_STATE_DIR = "/srv/opstk/state"
HOST_RELOAD_STATE = HOST_RELOAD_STATE_DIR + "/.dnsmasq-reload.state"
HOST_RELOAD_UNIT = "opstk-dnsmasq-reload.path"
HOST_RELOAD_WAIT = 12.0

HOST_RELOAD_HINT = (
    "宿主机 dnsmasq 未加载新配置：磁盘上的配置已更新，但正在运行的守护进程仍是旧的，"
    "两者不一致会导致 PXE 下发与实际菜单对不上（未登记的机器会拿到'拒绝安装'菜单而卡在循环里）。"
    "请在宿主机安装并启用重载单元：deploy/host/install-opstk-dnsmasq-reload.sh"
    "（它启用 " + HOST_RELOAD_UNIT + "，监视 /etc/dnsmasq.d/opstk-pxe.conf 并在校验后重启 dnsmasq）。"
)


def conf_sha(content: str) -> str:
    """与宿主机重载脚本对齐的配置指纹（脚本用 sha256sum，输出同值的小写十六进制）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def host_reload_status():
    """读宿主机重载单元写下的完成标记。

    返回 {"state": "OK"|"FAIL", "sha": str, "ts": str, "reason": str} 或 None（标记不存在）。
    标记缺失通常意味着宿主机还没装重载单元 —— 这本身就是要报的错，不能当成功。
    """
    try:
        with open(HOST_RELOAD_STATE, "r", encoding="utf-8", errors="replace") as fh:
            parts = fh.read().strip().split()
    except OSError:
        return None
    if not parts:
        return None
    st = {"state": parts[0], "sha": "", "ts": "", "reason": ""}
    if parts[0] == "OK":
        st["sha"] = parts[1] if len(parts) > 1 else ""
        st["ts"] = parts[2] if len(parts) > 2 else ""
    else:
        st["reason"] = " ".join(parts[1:])
    return st


def wait_host_reload(sha: str, timeout: float = HOST_RELOAD_WAIT):
    """等宿主机把**这一份**配置真正加载完（标记里的 sha 必须等于刚写的那份）。

    只认 sha 相等，不认"标记比刚才新"：否则连续两次部署时，第一次的成功标记
    会让第二次误判为已生效 —— 又一次假成功。
    """
    deadline = time.time() + max(0.0, timeout)
    last = None
    while True:
        st = host_reload_status()
        if st:
            last = st
            if st["state"] == "OK" and st["sha"] == sha:
                return {"ok": True, "state": st}
        if time.time() >= deadline:
            return {"ok": False, "state": last}
        time.sleep(0.4)


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
        # ports 67/69 and breaks PXE (see P0/U8 incident).
        #
        # 配置文件的写入由调用方完成；**重载委托给宿主机的
        # opstk-dnsmasq-reload.path/.service 单元**（见本模块顶部的握手说明）。
        # 注意 managed=False 只表示"我不拥有这个守护进程"，**不代表配置已生效** ——
        # 调用方（deploy_files）必须再用 wait_host_reload() 核对 sha，
        # 核对不过就报失败。这里绝不返回 ok=True 来暗示"已完成"。
        return {"ok": True, "action": action, "managed": False, "reload_delegated": True,
                "msg": "运行在容器内，dnsmasq 由宿主机管理；配置已写入，等待宿主机重载单元加载",
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
