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


def _remove_quiet(path):
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def write_conf(name, content):
    """把配置写进 CONF_DIR/<name>。**原子替换**：先写同目录临时文件，再 rename。

    为什么必须原子（MiMo R3 的附带修复）：原来是 `open(path, "w")` 直接写，
    写失败（磁盘满 / 进程被杀 / 被中断）会留下**截断的配置** —— 而这份配置会在
    dnsmasq 下一次启动（包含开机）时被读取，语法不合法 ⇒ dnsmasq 起不来 ⇒
    整个装机网段没有 DHCP/TFTP。原子替换让"写失败"只意味着"文件没变"。

    临时文件必须和目标**同目录**（同一文件系统），rename 才是原子的。
    宿主机的重载单元同时监视 PathChanged 与 PathModified：rename 产生的是
    IN_MOVED_TO（PathModified 语义），两条都挂着，不会漏触发。
    """
    os.makedirs(CONF_DIR, exist_ok=True)
    path = os.path.join(CONF_DIR, name)
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except PermissionError:
        # 非 root（没走容器 root / 没配 sudo 免密）：退化成 sudo tee + mv，
        # 同样是"临时文件 + 原子替换"，不会把半截内容留在正式路径上。
        rc, _, _ = _run(["tee", tmp], sudo=True, stdin_data=content)
        if rc == 0:
            rc2, _, _ = _run(["mv", "-f", tmp, path], sudo=True)
            if rc2 == 0:
                return True
        _remove_quiet(tmp)
        return False
    except Exception:
        _remove_quiet(tmp)
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
HOST_RELOAD_WAIT = 25.0

# 应用侧要求的**宿主脚本最低版本**。宿主脚本每次运行都会把 "RUN <版本> <时间>"
# 写进 HOST_RELOAD_LINK，应用在**落盘之前**读它：既证明"单元被触发过、脚本跑得起来"，
# 也证明"装的不是旧版脚本"。不写标记的旧版脚本会被判成 version=0 → 预检直接失败。
# 抬高这个数字 = 强制运维在宿主机重跑 install 脚本，而且是**零落盘**地失败。
# 实测踩过两次：宿主机的单元/脚本改过之后忘了重跑 install 脚本，于是 /deploy 每次
# 都"写完了才超时失败"。版本标记把这件事故变成一条可照做的指令。
HOST_RELOAD_MIN_SCRIPT_VERSION = 2
HOST_RELOAD_LINK = HOST_RELOAD_STATE_DIR + "/.dnsmasq-reload.link"
# 活体探测的等待上限。比 HOST_RELOAD_WAIT 短：探测只是"碰一下看单元响不响"，
# 而真正那一次重载可能正被 flock 串行化（另一个部署在跑 restart）。
HOST_RELOAD_PROBE_WAIT = 15.0

# 宿主机脚本在**重启 dnsmasq 之前**就会 FAIL 的原因码（脚本里的 fail 参数）。
# 只有这些能**证明**守护进程仍在跑旧配置，因此只有这些允许应用侧回滚磁盘内容：
# 回滚在"重启可能已经发生"的情况下会让磁盘与守护进程更不一致，比不回滚更糟。
# 对照 deploy/host/opstk-dnsmasq-reload.sh 的执行顺序：
#   flock → missing-conf → empty-sha → dnsmasq --test(test-failed) → 内容未变则直接 OK
#   → systemctl restart(restart-failed) → is-active(not-active) → config-changed
HOST_RELOAD_PRE_RESTART_FAILURES = ("test-failed:", "missing-conf", "empty-sha")

HOST_RELOAD_HINT = (
    "宿主机 dnsmasq 未加载新配置：磁盘上的配置已更新，但正在运行的守护进程仍是旧的，"
    "两者不一致会导致 PXE 下发与实际菜单对不上（未登记的机器会拿到'拒绝安装'菜单而卡在循环里）。"
    "请在宿主机安装并启用重载单元：deploy/host/install-opstk-dnsmasq-reload.sh"
    "（它启用 " + HOST_RELOAD_UNIT + "，监视 /etc/dnsmasq.d/opstk-pxe.conf 并在校验后重启 dnsmasq）。"
)


def conf_sha(content: str) -> str:
    """与宿主机重载脚本对齐的配置指纹（脚本用 sha256sum，输出同值的小写十六进制）。"""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def host_reload_link_status():
    """读宿主机脚本写下的"我跑过、我是第几版"标记（HOST_RELOAD_LINK）。

    返回 {"version", "ts", "err"}，三种状态必须分开：
      · version > 0 —— 可用（版本号由脚本自己写，见 HOST_RELOAD_MIN_SCRIPT_VERSION）
      · version == 0 —— 文件不存在：没装单元，或装的是不写标记的旧版脚本
      · version < 0 —— 存在但读不了/格式不对（err 里有原因）
    把"读不了"说成"没装"会把排查引到完全错误的方向（与 host_reload_status 的处理一致）。
    """
    try:
        with open(HOST_RELOAD_LINK, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
    except FileNotFoundError:
        return {"version": 0, "ts": "", "err": ""}
    except OSError as e:
        return {"version": -1, "ts": "", "err": (e.strerror or str(e))}
    parts = raw.split()
    if len(parts) < 2 or parts[0] != "RUN" or not parts[1].isdigit():
        return {"version": -1, "ts": "", "err": "malformed:" + raw[:40]}
    return {"version": int(parts[1]), "ts": parts[2] if len(parts) > 2 else "", "err": ""}


def _writable_dir_probe(path):
    """状态目录能不能写（能不能删掉旧的完成标记同样重要）。返回 (ok, err)。"""
    probe = os.path.join(path, ".opstk-write-probe.%d" % os.getpid())
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
    except OSError as e:
        return False, (e.strerror or str(e))
    try:
        os.remove(probe)
    except OSError:
        pass
    return True, ""


def host_reload_status():
    """读宿主机重载单元写下的完成标记。

    返回 {"state","sha","ts","reason","err"} 或 None（标记确实不存在）。
    区分 ENOENT 与其他 OSError：把权限/SELinux 导致的读失败也说成"没装单元"，
    会把排查引到完全错误的方向（MiMo R1 的低危项）。err 非空时调用方要把它显示出来。
    """
    try:
        with open(HOST_RELOAD_STATE, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
    except FileNotFoundError:
        return None
    except OSError as e:
        return {"state": "UNREADABLE", "sha": "", "ts": "", "reason": "",
                "err": (e.strerror or str(e))}
    parts = raw.split()
    if not parts:
        return {"state": "UNREADABLE", "sha": "", "ts": "", "reason": "", "err": "empty-state-file"}
    st = {"state": parts[0], "sha": "", "ts": "", "reason": "", "err": ""}
    if parts[0] == "OK":
        st["sha"] = parts[1] if len(parts) > 1 else ""
        st["ts"] = parts[2] if len(parts) > 2 else ""
        # 宿主机脚本理论上不会写出空 sha，但"OK <时间>"被按 sha/ts 错位解析的后果
        # 是拿时间戳当 sha 比对，这里显式判成不可用。
        if not st["sha"] or not st["ts"].isdigit():
            st["state"], st["err"] = "UNREADABLE", "malformed-ok-line"
    else:
        # FAIL <原因> <时刻>。**必须把时刻解析出来**：部署前的预检要靠它判断
        # "这个 FAIL 是不是我这次探测引起的"（是 ⇒ 链路活着，只是配置本身不合法）。
        # 解析不出来时预检只能一直等到超时，把一次真实的校验失败报成
        # "重载单元没反应"，把排查引向完全错误的方向（真机实测踩过）。
        # 原因本身不含空格：宿主脚本用 bash 参数展开把所有空白抹掉了。
        st["reason"] = parts[1] if len(parts) > 1 else ""
        if len(parts) > 2 and parts[2].isdigit():
            st["ts"] = parts[2]
        else:
            st["reason"] = " ".join(parts[1:])
    return st


def invalidate_host_reload_state():
    """部署前作废旧标记（best-effort）。

    为什么必须做：`wait_host_reload` 只比对 sha，而**连续两次部署内容相同**时，
    第二次会在宿主机还没做任何事之前就命中上一次留下的 OK 标记 —— 等于整条校验被跳过
    （MiMo R1 的中危项）。删掉它，任何读到的 OK 都必然来自本次写入之后。
    """
    try:
        os.remove(HOST_RELOAD_STATE)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def wait_host_reload(sha: str, timeout: float = None, not_before: float = None):
    """等宿主机把**这一份**配置真正加载完。

    判据三条同时成立：state == OK、sha 相同、且标记的时间戳 >= 本次部署开始的时刻。
    只认 sha 相等不够 —— 同内容重复部署会命中陈旧标记（见 invalidate_host_reload_state）。
    时间戳可以直接和宿主机的 `date +%s` 比：容器与宿主机共用同一个内核时钟
    （compose 没给独立 time namespace），不存在漂移。

    用 monotonic 计超时：NTP 往回跳时 time.time() 会让等待循环远超过预期。
    """
    if timeout is None:
        try:
            timeout = float(os.environ.get("OPS_HOST_RELOAD_TIMEOUT", HOST_RELOAD_WAIT))
        except ValueError:
            timeout = HOST_RELOAD_WAIT
    deadline = time.monotonic() + max(0.0, timeout)
    last = None
    while True:
        st = host_reload_status()
        if st:
            last = st
            fresh = True
            if not_before is not None and st["ts"].isdigit():
                fresh = float(st["ts"]) >= not_before
            if st["state"] == "OK" and st["sha"] == sha and fresh:
                return {"ok": True, "state": st}
        if time.monotonic() >= deadline:
            return {"ok": False, "state": last}
        time.sleep(0.4)


def reload_failure_is_pre_restart(state) -> bool:
    """宿主机报的这个失败能否**证明** dnsmasq 没被重启过。

    只有能证明时才允许应用侧回滚磁盘内容（见 HOST_RELOAD_PRE_RESTART_FAILURES）。
    状态文件缺失/读不了（state 为 None 或 UNREADABLE）一律算"不能证明"：
    那说明链路本身有问题，重启到底发没发生过无从判断。
    """
    if not state or state.get("state") != "FAIL":
        return False
    return (state.get("reason") or "").startswith(HOST_RELOAD_PRE_RESTART_FAILURES)


def reload_preflight(conf_path=None, timeout=None):
    """部署**之前**确认"把配置写下去之后，宿主机真的能让它生效"。

    为什么要有预检（MiMo R3）：原来的顺序是"先落盘、再核对重载"，于是链路坏掉时
    /deploy 只会等到**文件已经改完**之后才报失败，留下半新半旧的网络状态。
    预检把这些失败挪到落盘之前 —— 状态目录没挂、单元没装/是旧版、单元没反应，
    这几种都能在写任何文件之前判定，失败就是"什么都没动"。

    判据两层：
      · 静态：状态目录存在且可写；宿主机脚本留下的版本标记存在且 >=
        HOST_RELOAD_MIN_SCRIPT_VERSION；
      · 活体：碰一下被监视的那份配置文件（只改 mtime，不动内容），等宿主机脚本写出
        一个**新的**状态文件。它证明"单元真的在触发、脚本真的跑得起来"。
        陈旧判定只看时间戳（ts >= not_before），所以单元已经死掉时必然超时失败。
        **故意不删旧标记**：删掉它会让正在等重载的并发部署失去它要等的那个标记，
        而时间戳判陈旧已经足够。

    返回 {"ok", "skipped", "probed", "reason", "hint", "state", "log"}。
    reason 是机器可判定的原因码，hint 是可以直接照做的中文说明（调用方拼进错误里）。
    """
    log = []
    conf_path = conf_path if conf_path is not None else PXE_CONF
    if timeout is None:
        try:
            timeout = float(os.environ.get("OPS_HOST_RELOAD_PROBE_TIMEOUT",
                                           HOST_RELOAD_PROBE_WAIT))
        except ValueError:
            timeout = HOST_RELOAD_PROBE_WAIT

    def _ret(ok, reason="", hint="", state=None, probed=False, skipped=False):
        return {"ok": ok, "skipped": skipped, "probed": probed, "reason": reason,
                "hint": hint, "state": state, "log": log}

    if not _in_container():
        # 裸机/非容器：dnsmasq 由本机 systemd 直接管，dhcp_control 自己会重启，
        # 不存在"写下去了但没生效"这条缝隙。
        log.append("预检跳过：不在容器内，dnsmasq 由本机 systemd 直接管理，无需宿主机重载握手")
        return _ret(True, "not-delegated", skipped=True)

    if not os.path.isdir(HOST_RELOAD_STATE_DIR):
        log.append("预检失败：状态目录不存在 " + HOST_RELOAD_STATE_DIR)
        return _ret(False, "state-dir-missing",
                    "容器里看不到状态目录 " + HOST_RELOAD_STATE_DIR
                    + "：宿主机还没创建它，或者 docker-compose.yml 没把它挂进容器"
                      "（状态文件是容器与宿主机之间唯一的通道，缺了就无法确认配置是否生效）。"
                      "请在宿主机 mkdir -p /srv/opstk/state 并确认 compose 里有 "
                    + HOST_RELOAD_STATE_DIR + ":" + HOST_RELOAD_STATE_DIR
                    + " 这一行，然后 docker compose up -d（改挂载必须 up -d，restart 不够）。")

    wok, werr = _writable_dir_probe(HOST_RELOAD_STATE_DIR)
    if not wok:
        log.append("预检失败：状态目录不可写 " + HOST_RELOAD_STATE_DIR + "（" + werr + "）")
        return _ret(False, "state-dir-unwritable",
                    "状态目录 " + HOST_RELOAD_STATE_DIR + " 不可写（" + werr
                    + "）：容器既要写它、也要能删掉旧的完成标记，否则每次部署都会"
                      "被上一次留下的标记骗成成功或失败。请检查该目录的属主/权限与挂载方式。")

    link = host_reload_link_status()
    if link["version"] == 0:
        log.append("预检失败：宿主脚本版本标记不存在 " + HOST_RELOAD_LINK)
        return _ret(False, "unit-not-installed",
                    "没有找到宿主脚本的版本标记（" + HOST_RELOAD_LINK + "）："
                    "要么没装重载单元，要么装的是不写标记的旧版脚本。" + HOST_RELOAD_HINT)
    if link["version"] < 0:
        log.append("预检失败：版本标记不可用（" + link["err"] + "）")
        return _ret(False, "link-unreadable",
                    "宿主脚本的版本标记存在但读不了/格式不对（" + HOST_RELOAD_LINK
                    + "：" + link["err"] + "）。请检查属主/权限/SELinux，或在宿主机重跑 "
                      "deploy/host/install-opstk-dnsmasq-reload.sh。")
    if link["version"] < HOST_RELOAD_MIN_SCRIPT_VERSION:
        log.append("预检失败：宿主脚本版本 %d < 要求的 %d"
                   % (link["version"], HOST_RELOAD_MIN_SCRIPT_VERSION))
        return _ret(False, "script-outdated",
                    "宿主机装的重载脚本是旧版（v%d，本应用要求 >= v%d），"
                    "旧版无法被确认可用。" % (link["version"], HOST_RELOAD_MIN_SCRIPT_VERSION)
                    + HOST_RELOAD_HINT)

    if not os.path.exists(conf_path):
        # 还没部署过：没有可碰的监视目标，活体探测做不了（也不会为了探测去造一个
        # 空配置）。静态证据（版本标记 = 单元被触发过、脚本跑得起来）已足够，
        # 真正的判定仍然由落盘后的 sha 核对负责。
        log.append("预检：%s 还不存在，跳过活体探测（静态证据：宿主脚本 v%d 跑过）"
                   % (conf_path, link["version"]))
        return _ret(True, "no-target-to-probe", state=None, probed=False)

    not_before = int(time.time())
    try:
        os.utime(conf_path, None)
    except OSError as e:
        err = e.strerror or str(e)
        log.append("预检失败：无法碰触监视目标 " + conf_path + "（" + err + "）")
        return _ret(False, "probe-touch-failed",
                    "无法修改被监视的配置文件 " + conf_path + " 的 mtime（" + err
                    + "）：容器对这个目录必须是可写的，否则连配置本身也写不下去。"
                      "请检查 /etc/dnsmasq.d 的挂载与权限。")

    deadline = time.monotonic() + max(0.0, timeout)
    last = None
    while True:
        st = host_reload_status()
        if st:
            last = st
            fresh = bool(st["ts"].isdigit()) and int(st["ts"]) >= not_before
            if fresh:
                log.append("预检通过：重载链路有响应（宿主机脚本报 %s %s）"
                           % (st["state"], (st.get("reason") or "")[:60]))
                return _ret(True, "", state=st, probed=True)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.4)

    log.append("预检失败：等待 %.0fs 未见宿主机写出新的状态文件" % timeout)
    return _ret(False, "no-response",
                "重载单元没有反应：已经碰过 " + conf_path + " 并等了 %.0fs，"
                "宿主机脚本没有写出新的状态文件。" % timeout
                + "常见原因：" + HOST_RELOAD_UNIT + " 没在运行（改过单元文件后要 "
                  "daemon-reload + 重跑 install 脚本）、脚本路径不对、"
                  "或者连续失败触发了 systemd 启动限流。"
                  "查：systemctl status " + HOST_RELOAD_UNIT
                + "；journalctl -u opstk-dnsmasq-reload --since '-5 min'",
                state=last)


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
