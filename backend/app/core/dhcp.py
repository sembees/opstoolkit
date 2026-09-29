"""Unified DHCP (dnsmasq) management shared by PXE and ZTP.

Both PXE and ZTP use the SAME dnsmasq instance. Configs are written
to /etc/dnsmasq.d/ and dnsmasq auto-loads them all.
"""
from __future__ import annotations

import hashlib
import logging
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
    """把配置写进 CONF_DIR/<name>（**原地**写，不是 rename 替换）。

    为什么必须是原地写（真机实测，2026-09，RUNBOOK §5.50）：宿主机的
    opstk-dnsmasq-reload.path 监视这个文件，而 systemd 的 PathChanged/PathModified
      · **不吃 IN_ATTRIB** —— `touch` / `os.utime`（只改 mtime）根本不触发；
      · **对 rename 替换（IN_MOVED_TO）也不可靠** —— 监视挂在被替换掉的那个 inode 上，
        实测同一个部署里"第一份配置的替换触发了、紧接着第二份没有"，
        表现就是部署偶发地卡在"等宿主机重载"直到超时。
    只有**原地写入**产生 IN_CLOSE_WRITE / IN_MODIFY，落在那只 watch 命中的 inode 上，
    每次都稳定触发。可靠触发是这条链路的前提：不触发 = 配置永远不会生效。

    代价：进程在 write() 中途被杀，理论上可能留下半截文件（单次 write 一个小文本文件，
    这个窗口以微秒计）。以前为此改成过"临时文件 + rename"的原子替换，
    结果是**触发不可靠**（更难查、后果更重）。两害相权，选可触发；
    半截配置的风险由三件事兜住：部署前的红线/预检、宿主机 `dnsmasq --test` 不通过就
    不重启（dnsmasq 仍跑旧配置）、以及失败回滚。
    """
    os.makedirs(CONF_DIR, exist_ok=True)
    path = os.path.join(CONF_DIR, name)
    # ★ 外部审查 U2-F1（我复核确认，踩的正是那条红线）：`open(path,"w")` 在 open(2)
    #   那一刻就把文件截成 0 字节。若随后 write/flush/fsync 失败（ENOSPC/EIO/编码错误），
    #   磁盘上留下的是**半截甚至 0 字节**的配置 —— 而空配置能通过 `dnsmasq --test`
    #   （空配置语法合法），宿主机下一次重载（ZTP 部署、开机、人工 restart）就会加载它：
    #   enable-tftp/tftp-root/dhcp-boot 全没了，**整个装机网段失去 DHCP/TFTP**。
    #   所以：先留一份原内容，失败时尽力写回；同时调用方要按"可能已被截断"处理并回滚。
    orig = None
    try:
        with open(path, "rb") as f:
            orig = f.read()
    except OSError:
        orig = None
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        return True
    except PermissionError:
        # 非 root：退化成 sudo tee（tee 也是原地写，同样触发 IN_CLOSE_WRITE）
        rc, _, _ = _run(["tee", path], sudo=True, stdin_data=content)
        if rc == 0:
            return True
        _restore_bytes(path, orig)
        return False
    except Exception:
        _restore_bytes(path, orig)
        return False


def _restore_bytes(path, data):
    """把原内容尽力写回（失败就放弃 —— 这是"挽回截断"的最后一步，不能再抛异常）。"""
    if data is None:
        return False
    try:
        with open(path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        return True
    except Exception:      # noqa: BLE001
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
HOST_RELOAD_MIN_SCRIPT_VERSION = 3
HOST_RELOAD_LINK = HOST_RELOAD_STATE_DIR + "/.dnsmasq-reload.link"
# ZTP 的握手槽位（与 PXE 各一个，互不冒充）。
# 为什么必须分开：宿主脚本监视的是 /etc/dnsmasq.d 下的**两个**配置文件
# （opstk-pxe.conf 与 opstk-ztp.conf），一次重载可能由其中任何一个触发。
# 只用一个状态文件时，ZTP 只能拿 PXE 的 sha 去核对，永远核不上（或者更糟：
# 把"PXE 那份生效了"当成"ZTP 那份也生效了"）。
HOST_RELOAD_STATE_ZTP = HOST_RELOAD_STATE_DIR + "/.dnsmasq-reload.state.ztp"
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


def host_reload_state_path(name: str = "pxe") -> str:
    """握手槽位：pxe（默认）或 ztp。两个槽位各自记自己那份配置的 sha。"""
    return HOST_RELOAD_STATE_ZTP if (name or "").lower() == "ztp" else HOST_RELOAD_STATE


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


def host_reload_status(state_path=None):
    """读宿主机重载单元写下的完成标记（默认读 PXE 槽位，传 state_path 读别的槽位）。

    返回 {"state","sha","ts","reason","err"} 或 None（标记确实不存在）。
    区分 ENOENT 与其他 OSError：把权限/SELinux 导致的读失败也说成"没装单元"，
    会把排查引到完全错误的方向（MiMo R1 的低危项）。err 非空时调用方要把它显示出来。
    """
    state_path = state_path or HOST_RELOAD_STATE
    try:
        with open(state_path, "r", encoding="utf-8", errors="replace") as fh:
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


def invalidate_host_reload_state(state_path=None):
    """部署前作废旧标记（best-effort）。

    为什么必须做：`wait_host_reload` 只比对 sha，而**连续两次部署内容相同**时，
    第二次会在宿主机还没做任何事之前就命中上一次留下的 OK 标记 —— 等于整条校验被跳过
    （MiMo R1 的中危项）。删掉它，任何读到的 OK 都必然来自本次写入之后。
    """
    try:
        os.remove(state_path or HOST_RELOAD_STATE)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False


def wait_host_reload(sha: str, timeout: float = None, not_before: float = None,
                     state_path=None):
    """等宿主机把**这一份**配置真正加载完（state_path 决定等哪个槽位：PXE / ZTP）。

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
        st = host_reload_status(state_path)
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


def rewrite_conf_unchanged(conf_path):
    """对配置文件做一次**原地**写入，内容一模一样（触发宿主机 path 单元的最小动作）。

    两个地方用它，理由相同：systemd 的 PathChanged/PathModified
      · 不吃 IN_ATTRIB ⇒ `touch`/`os.utime` 完全不触发；
      · 对 rename 替换不可靠 ⇒ 监视挂在被换掉的 inode 上；
    只有原地写入的 IN_CLOSE_WRITE 稳定触发。内容不变 ⇒ 宿主脚本判定 unchanged
    ⇒ 只写状态、不重启 dnsmasq。
    返回 (ok, err)。
    """
    try:
        with open(conf_path, "rb") as fh:
            cur = fh.read()
        with open(conf_path, "wb") as fh:
            fh.write(cur)
            fh.flush()
            os.fsync(fh.fileno())
        return True, ""
    except OSError as e:
        return False, (e.strerror or str(e))


def wait_host_reload_retrying(sha, conf_path, state_path=None, not_before=None,
                              timeout=None, nudge=None):
    """等宿主机加载这一份配置，**超时就再触发一次**。

    为什么必须重试（真机实测）：path 单元是**边沿触发**，而 systemd 对"已经在运行/
    正在启动"的服务，start 是空操作 —— 一次配置写入如果正好落在上一次运行还没结束的
    窗口里（部署前的活体探测刚碰过同一个文件），这个事件就被吃掉，之后不会再有事件，
    应用只能等满超时（现象：部署偶发卡在"等宿主机重载"，重试一次又好了）。
    所以超时后**再原地写一次同样的内容**，把丢失的那个边沿补上。

    总耗时不超过 timeout（默认 OPS_HOST_RELOAD_TIMEOUT / HOST_RELOAD_WAIT）。
    返回 (结果, 触发次数)。
    """
    if timeout is None:
        try:
            timeout = float(os.environ.get("OPS_HOST_RELOAD_TIMEOUT", HOST_RELOAD_WAIT))
        except ValueError:
            timeout = HOST_RELOAD_WAIT
    deadline = time.monotonic() + max(0.0, timeout)
    nudge = nudge or (lambda: rewrite_conf_unchanged(conf_path)[0])
    got = None
    attempts = 0
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            break
        # 单次等待取总预算的 1/3（至少 1.5s）：保证任何合理超时下都至少补触发一次
        got = wait_host_reload(sha, timeout=min(left, max(1.5, left / 3.0)),
                               not_before=not_before, state_path=state_path)
        if got.get("ok"):
            return got, attempts
        if time.monotonic() >= deadline:
            break
        attempts += 1
        nudge()
    return (got or {"ok": False, "state": None}), attempts


def conf_sha_matches(path, want_sha) -> bool:
    """磁盘上这份配置的 sha 是否仍等于我们刚写的那份。

    用途（外部审查 U2-F6）：并发部署时，宿主机重载单元确认的是"**某一次**写入"，
    而磁盘可能已经被后一次部署覆盖 —— 报成功之前必须再核对一次磁盘内容，
    否则会出现"守护进程跑的是别人那份，我这个接口却报成功"。
    """
    if not want_sha:
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            return conf_sha(f.read()) == want_sha
    except (OSError, UnicodeDecodeError):
        return False


def reload_failure_is_pre_restart(state, not_before=None) -> bool:
    """宿主机报的这个失败能否**证明** dnsmasq 没被重启过。

    只有能证明时才允许应用侧回滚磁盘内容（见 HOST_RELOAD_PRE_RESTART_FAILURES）。
    状态文件缺失/读不了（state 为 None 或 UNREADABLE）一律算"不能证明"：
    那说明链路本身有问题，重启到底发没发生过无从判断。

    ★ 外部审查 U2-F8：还要看**新鲜度**。状态槽里的 FAIL 可能是上一次运行（或并发部署
    的另一次运行）留下的陈旧标记 —— 那时宿主机可能早已因为别的事件重启过 dnsmasq，
    据此授权回滚会正好制造这个判据想避免的"磁盘 vs 守护进程更不一致"。
    传了 `not_before`（本次部署开始的时间戳）时，比它旧的 FAIL 一律不算数。
    """
    if not state or state.get("state") != "FAIL":
        return False
    if not_before is not None:
        try:
            ts = int(str(state.get("ts") or "0"))
        except (TypeError, ValueError):
            ts = 0
        if ts < int(not_before):
            return False
    return (state.get("reason") or "").startswith(HOST_RELOAD_PRE_RESTART_FAILURES)


def reload_preflight(conf_path=None, timeout=None, state_path=None):
    """部署**之前**确认"把配置写下去之后，宿主机真的能让它生效"。

    为什么要有预检（MiMo R3）：原来的顺序是"先落盘、再核对重载"，于是链路坏掉时
    /deploy 只会等到**文件已经改完**之后才报失败，留下半新半旧的网络状态。
    预检把这些失败挪到落盘之前 —— 状态目录没挂、单元没装/是旧版、单元没反应，
    这几种都能在写任何文件之前判定，失败就是"什么都没动"。

    判据两层：
      · 静态：状态目录存在且可写；宿主机脚本留下的版本标记存在且 >=
        HOST_RELOAD_MIN_SCRIPT_VERSION；
      · 活体：对配置做一次**内容完全相同**的写入（rename 替换），等宿主机脚本写出
        一个**新的**状态文件。它证明"单元真的在触发、脚本真的跑得起来"。
        ⚠ 不能改用 `touch`/`os.utime`：systemd 的 PathChanged/PathModified 不含
        IN_ATTRIB，只改 mtime **根本不会触发**（真机实测，2026-09 —— 这曾让每个
        部署都误报"预检超时"）。内容没变 ⇒ 宿主脚本判定 unchanged ⇒ 不重启 dnsmasq。
        陈旧判定只看时间戳（ts >= not_before），所以单元已经死掉时必然超时失败。
        **故意不删旧标记**：删掉它会让正在等重载的并发部署失去它要等的那个标记，
        而时间戳判陈旧已经足够。

    返回 {"ok", "skipped", "probed", "reason", "hint", "state", "log"}。
    reason 是机器可判定的原因码，hint 是可以直接照做的中文说明（调用方拼进错误里）。
    """
    log = []
    conf_path = conf_path if conf_path is not None else PXE_CONF
    state_path = state_path if state_path is not None else HOST_RELOAD_STATE
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
    ok, err = rewrite_conf_unchanged(conf_path)
    if not ok:
        log.append("预检失败：无法探测监视目标 " + conf_path + "（" + err + "）")
        return _ret(False, "probe-touch-failed",
                    "无法对监视目标 " + conf_path + " 做一次写入探测（" + err
                    + "）：容器对这个目录必须是可写的，否则连配置本身也写不下去。"
                      "请检查 /etc/dnsmasq.d 的挂载与权限。")

    deadline = time.monotonic() + max(0.0, timeout)
    last = None
    while True:
        st = host_reload_status(state_path)
        if st:
            last = st
            fresh = bool(st["ts"].isdigit()) and int(st["ts"]) >= not_before
            if fresh:
                log.append("预检通过：重载链路有响应（宿主机脚本报 %s %s）"
                           % (st["state"], (st.get("reason") or "")[:60]))
                # 让宿主机把这次运行**跑完**再返回：path 是边沿触发，而 systemd 对
                # "正在运行"的服务 start 是空操作 —— 紧接着的部署写入如果撞进这个
                # 窗口，事件会被吃掉（部署偶发卡在等重载）。等一下能显著减少这种相撞，
                # 真正的兜底是 wait_host_reload_retrying 的"再触发一次"。
                time.sleep(0.4)
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


# ── "不许把 DHCP 池开到骨干网上"的护栏 ──────────────────────────────────
# 背景（RUNBOOK §5.50）：ZTP 生成器在没有显式给网卡时会去猜，容器里猜到的是
# **企业网那张卡**（ens18/10.128.118.113），而 standalone 模式的配置里还带
# dhcp-range —— 这份配置一旦被 dnsmasq 加载，就会在骨干网段上开一个 DHCP 池、
# 抢答企业 DHCP（分出去的路由器/网关全是错的）。而 dnsmasq 是**开机自启**的：
# 只要那份文件躺在 /etc/dnsmasq.d 里，任何一次重启（哪怕与本模块无关）都会加载它。
# 所以这类配置必须在**落盘之前**就被拒绝，而不是靠"它还没生效"侥幸。

def _default_route_iface(proc_route="/proc/net/route"):
    """默认路由所在的网卡名；读不到返回 ""。

    /proc/net/route 每行：<iface> <dest> <gw> <flags> ...，Destination 为 00000000
    的那行就是默认路由（字段是**主机字节序**的十六进制，网关需要小端还原）。
    """
    try:
        with open(proc_route, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == "00000000":
                    return parts[0]
    except OSError:
        return ""
    return ""


def gateway_from_route(proc_route="/proc/net/route"):
    """默认路由的网关地址（拿不到返回 ""）。与 _default_route_iface 读同一个文件。"""
    import socket
    import struct
    try:
        with open(proc_route, encoding="utf-8") as fh:
            for line in fh.read().splitlines()[1:]:
                parts = line.split()
                if len(parts) >= 3 and parts[1] == "00000000":
                    return socket.inet_ntoa(struct.pack("<I", int(parts[2], 16)))
    except (OSError, ValueError, struct.error):
        return ""
    return ""


def _iface_v4(iface):
    """网卡的 (IPv4, 前缀长度)；取不到返回 None。ioctl，不依赖 ip 命令。"""
    if not iface:
        return None
    try:
        import fcntl
        import socket
        import struct
    except ImportError:
        return None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            raw = fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", iface[:15].encode()))
            ip = socket.inet_ntoa(raw[20:24])
            mask = fcntl.ioctl(s.fileno(), 0x891B, struct.pack("256s", iface[:15].encode()))
            m = socket.inet_ntoa(mask[20:24])
        finally:
            s.close()
        bits = bin(int.from_bytes(socket.inet_aton(m), "big")).count("1")
        return ip, bits
    except Exception:
        return None


def protected_networks(providers=None):
    """宿主机的"骨干网事实"：默认路由网卡名 + 该网卡承载的 IPv4 网段列表。

    用途：拦住"把 DHCP 池开到承载默认路由的网段上"。取不到时返回 ("", [], 原因)，
    调用方必须据此**拒绝部署**（fail-closed）—— 宁可部署不了，也不能赌。
    providers 可注入，便于单测（{"route": 文件路径, "iface": (ip,prefix)}）。
    """
    import ipaddress
    p = providers or {}
    try:
        iface = p["iface_name"] if "iface_name" in p else _default_route_iface(
            p.get("route", "/proc/net/route"))
    except OSError:
        iface = ""
    if p.get("iface_v4") is not None:
        got = p["iface_v4"]
    else:
        got = _iface_v4(iface)
    nets = []
    if got:
        try:
            nets.append(ipaddress.ip_network(got[0] + "/" + str(got[1]), strict=False))
        except ValueError:
            nets = []
    if not iface:
        return "", [], "读不到默认路由（/proc/net/route），无法判断哪张网卡是骨干网"
    if not nets:
        return iface, [], "读不到 %s 上的 IPv4 地址/掩码，无法算出骨干网段" % iface
    return iface, nets, ""


def parse_dnsmasq_dhcp(conf_text):
    """从生成的配置里取出 (interface 列表, dhcp-range 的地址段列表)。

    只认 dnsmasq 自己的写法：`interface=<name>`、`dhcp-range=<a>,<b>[,<lease>]`
    （proxy 模式的 `dhcp-range=<ip>,proxy` 只取到第一个地址，不构成"池"，不算越界）。
    """
    import ipaddress
    ifaces = []
    ranges = []
    for line in (conf_text or "").splitlines():
        s = line.strip()
        if s.startswith("#"):
            continue
        if s.startswith("interface="):
            ifaces.append(s.split("=", 1)[1].strip())
        elif s.startswith("dhcp-range="):
            parts = [x.strip() for x in s.split("=", 1)[1].split(",")]
            ips = []
            for x in parts[:2]:
                try:
                    ips.append(ipaddress.ip_address(x))
                except ValueError:
                    pass
            if len(ips) == 2:
                ranges.append((ips[0], ips[1]))
    return ifaces, ranges


# dnsmasq 里**只能出现一次**的关键字（出现两次就 `illegal repeated keyword`，
# 而且它拒绝的是**整份**配置：dnsmasq 直接起不来，开机也起不来 ⇒ 装机网段没有 DHCP）。
# 实测（10.128.118.113）：/etc/dnsmasq.d 下 opstk-pxe.conf 已有 `port=0` 时，
# 再加一份也写 `port=0` 的 opstk-ztp.conf ⇒ `dnsmasq --test` 报
# `illegal repeated keyword at line 2 of /etc/dnsmasq.d/opstk-ztp.conf`；
# 而同场的 `interface=` / `bind-interfaces` / `enable-tftp` / `tftp-root` /
# `dhcp-range=` / `dhcp-option=` 都可以重复（逐个删掉验证过）。
# 只登记**验证过**的关键字，不凭印象扩充。
_SINGLETON_KEYWORDS = ("port",)


def _other_conf_has_keyword(keyword, exclude_path, conf_dir=None):
    """CONF_DIR 下**除自己以外**的 .conf 里有没有这个关键字。返回文件名或 ""。"""
    d = conf_dir if conf_dir is not None else CONF_DIR
    try:
        names = sorted(os.listdir(d))
    except OSError:
        return ""
    for n in names:
        if not n.endswith(".conf"):
            continue
        p = os.path.join(d, n)
        if os.path.abspath(p) == os.path.abspath(exclude_path or ""):
            continue
        try:
            with open(p, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    s = line.strip()
                    if s.startswith(keyword + "=") or s == keyword:
                        return n
        except OSError:
            continue
    return ""


def check_dhcp_conf_safety(conf_text, providers=None, conf_dir=None, own_path=None,
                          iface_v4=None):
    """落盘前的红线检查。返回 (ok, 原因) —— 原因是要显示给运维的中文说明。

    六条，全部 fail-closed：
      1. `interface=` 是占位值（eth0/eth1/空）⇒ 拒绝（说明生成器没拿到真实网卡）；
      2. 目标网卡在宿主机上**不存在** ⇒ 拒绝。这条尤其重要：dnsmasq 配了
         `bind-interfaces` + 不存在的网卡会**起不来**，而 dnsmasq 同时服务着 PXE ——
         一个 ZTP 模板里的网卡笔误就能把整个装机网段的 DHCP/TFTP 一起打掉；
      3. 目标网卡就是承载**默认路由**的那张（骨干网/企业网）⇒ 拒绝；
      4. `dhcp-range` 与骨干网段有重叠 ⇒ 拒绝（哪怕网卡名写对了，池也不能落在骨干网段）；
      5. 用了**不可重复**的关键字（如 `port=`）而配置目录里别的 .conf 已经用过
         ⇒ 拒绝。这条修的是真机实测到的"两份配置互斥"：dnsmasq 会连整份配置一起拒绝，
         结果是守护进程起不来。
      6. `dhcp-range` 不在目标网卡自己的网段里 ⇒ 拒绝。dnsmasq 是按**网卡地址**推掩码、
         算广播地址的；池子跨到别的网段时客户端会拿到不可用的地址（或干脆拿不到），
         而且这通常意味着"池开到了不该开的网段上"。网卡上读不到 IPv4 也算 ⇒ 拒绝。

    `iface_v4` 可注入（测试用），默认取真实的网卡地址/掩码。
    """
    import ipaddress
    prov = providers or {}
    if own_path is None:
        # ★ 外部审查 U2-F10：`own_path` 为空时第 5 条的"排除自己"就不生效 ——
        #   目标配置**自己**用过的不可重复关键字（如 `port=0`）会被当成"别的 .conf 也用了"
        #   而误报（结果是把一次合法部署拦下）。本仓库两个调用点（PXE / ZTP 的 deploy_files）
        #   都显式传了它，所以这是 API 易用性缺口而不是可达缺陷；留一条日志，
        #   让以后新的调用点踩到时能立刻定位。
        logging.getLogger(__name__).warning(
            "check_dhcp_conf_safety 未传 own_path：本次检查不会排除任何文件，"
            "配置自身的不可重复关键字（如 port=0）可能被误判成与别的 .conf 冲突。")
    # 网卡地址事实：优先用注入的映射（单测用，跨平台确定），其次用可注入的 iface_v4 函数，
    # 最后才是真实读取。
    injected_map = prov.get("iface_v4_map")
    if iface_v4 is not None:
        get_v4 = iface_v4
    elif injected_map is not None:
        get_v4 = lambda name: injected_map.get(name)  # noqa: E731
    else:
        get_v4 = _iface_v4
    ifaces, ranges = parse_dnsmasq_dhcp(conf_text)
    if not ifaces:
        return False, "生成的配置里没有 interface=（不知道要服务哪张网卡），拒绝部署"
    placeholders = {"eth0", "eth1", "ens0", ""}
    for i in ifaces:
        if i in placeholders:
            return False, ("配置里的 DHCP 网卡是占位值 `" + i + "`：请在模板/参数里**明确填写**"
                           "要把 DHCP 池开在哪张网卡上（容器内自动探测网卡不可靠 —— "
                           "实测会猜到承载企业网的那张卡）。")
    try:
        present = set(os.listdir("/sys/class/net"))
    except OSError as e:
        # ★ 外部审查 U2-F3：这里原来是 `present = set()` 然后靠 `if present:` 跳过第 2/6 条
        #   —— 也就是**读不到网卡事实时反而放行**（fail-open），与 docstring 里
        #   "六条全部 fail-closed" 自相矛盾。后果是把"网卡名笔误"这种配置放过去，
        #   而 bind-interfaces + 不存在的网卡会让 dnsmasq 起不来（它同时服务着 PXE）。
        return False, ("读不到 /sys/class/net（" + type(e).__name__ + "）：无法确认配置里的 "
                       "interface= 指的是真实存在的网卡。dnsmasq 配 bind-interfaces 时网卡不存在会"
                       "**启动失败**，而它同时服务着 PXE 与 ZTP ⇒ 按 fail-closed 口径拒绝部署。")
    if present:
        for i in ifaces:
            if i not in present:
                return False, ("宿主机上不存在网卡 `" + i + "`：dnsmasq 配了 bind-interfaces + "
                               "不存在的网卡会**启动失败**，而它同时服务着 PXE —— "
                               "请改成宿主机上真实存在的网卡（当前有：" +
                               ", ".join(sorted(present)[:8]) + " …）")
    iface, nets, why = protected_networks(providers)
    if why:
        return False, ("无法判断哪张网卡/哪个网段是骨干网：" + why
                       + "。为避免把 DHCP 池开到骨干网上，这里**拒绝部署**。")
    for i in ifaces:
        if i == iface:
            return False, ("DHCP 网卡 `" + i + "` 正是承载默认路由的骨干网卡"
                           "（" + ", ".join(str(n) for n in nets) + "）："
                           "在它上面开 DHCP 会抢答骨干网的地址/网关，必须改到专用装机网卡。")
    for lo, hi in ranges:
        # ★ 外部审查 U2-F2：原来只判"两个**端点** ∈ 骨干网段"，池子**跨过**骨干网段
        #   （端点都在外面、范围却盖住了它）会被漏判 —— 而 docstring/注释写的是
        #   "池与骨干网段有重叠"。改成真正的**区间重叠**判定。
        try:
            lo_i = int(ipaddress.ip_address(lo))
            hi_i = int(ipaddress.ip_address(hi))
        except ValueError:
            continue
        if hi_i < lo_i:
            lo_i, hi_i = hi_i, lo_i
        for net in nets:
            if int(net.network_address) <= hi_i and lo_i <= int(net.broadcast_address):
                return False, ("dhcp-range " + str(lo) + "-" + str(hi) + " 与骨干网段 "
                               + str(net) + " 有重叠：这会把骨干网的地址分给客户端，"
                                 "请把池改到专用装机网段。")
    # 6) 地址池必须落在目标网卡**自己**的网段里（读不到网卡地址也算拒绝）
    #    只在能列出真实网卡时做（非 Linux / 容器里读不到 /sys/class/net 时跳过，
    #    与第 2 条同一口径，避免在没有真实网卡事实的环境里误拒）。
    if present and ranges:
        nets_by_iface = []
        for i in ifaces:
            got = get_v4(i)
            if not got:
                continue
            try:
                nets_by_iface.append((i, ipaddress.ip_network(
                    str(got[0]) + "/" + str(got[1]), strict=False)))
            except (ValueError, TypeError, IndexError):
                continue
        if not nets_by_iface:
            return False, ("读不到 DHCP 网卡（" + ", ".join(ifaces) + "）上的 IPv4 地址/掩码："
                           "dnsmasq 要按这张卡的网段推掩码与广播地址，读不到就没法确认池子"
                           "落在正确的网段里。请先给它配上地址（例如 192.168.199.1/24）再部署。")
        for lo, hi in ranges:
            if not any((lo in n and hi in n) for _i, n in nets_by_iface):
                return False, ("dhcp-range " + str(lo) + "-" + str(hi)
                               + " 不落在 DHCP 网卡（" + ", ".join(ifaces) + "）的网段（"
                               + ", ".join(str(n) for _i, n in nets_by_iface) + "）内："
                                 "dnsmasq 按网卡地址推掩码/广播地址，池子跨网段时客户端会拿到"
                                 "不可用的地址，而且这通常意味着池开到了别的网段上。"
                                 "请把池改到该网卡自己的网段内。")
    # 5) 不可重复关键字冲突（真机实测：两份配置都写 port=0 ⇒ dnsmasq 拒绝整份配置）
    for kw in _SINGLETON_KEYWORDS:
        used_here = any(
            (ln.strip().startswith(kw + "=") or ln.strip() == kw)
            for ln in (conf_text or "").splitlines() if not ln.strip().startswith("#"))
        if not used_here:
            continue
        other = _other_conf_has_keyword(kw, own_path, conf_dir)
        if other:
            return False, ("这份配置用了 `" + kw + "=`，而配置目录里的 " + other
                           + " 已经用过它：dnsmasq 的 `" + kw + "` 是**不可重复**的关键字，"
                             "重复会让 dnsmasq 以 `illegal repeated keyword` 拒绝**整份**配置 —— "
                             "结果是守护进程起不来（含开机），整个装机网段没有 DHCP/TFTP。"
                             "请把 `" + kw + "` 留给主配置（例如 opstk-pxe.conf）只写一次；"
                             "若 " + other + " 是旧版生成器留下的，"
                             "重新部署一次对应的模板即可修正。")
    return True, ""


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
        # 容器里做不到：不拥有守护进程，也不该在这个网络命名空间里起第二个 dnsmasq
        # （会和宿主机的抢 67/69，P0/U8 事故）。
        # ★ 外部审查 U2-F5：以前这里对**任何** action 都回 ok=True —— 于是 stop/start
        #   报成功却什么都没做（假成功）。现在只对 restart/status 保持原语义。
        if action in ("start", "stop"):
            return {"ok": False, "action": action, "managed": False, "reload_delegated": True,
                    "msg": ("容器内不能 " + action + " dnsmasq（它由宿主机管理）；"
                            "请在宿主机上执行 `systemctl " + action + " dnsmasq`"),
                    "running": len(_find_dnsmasq_pids(skip_zombies=True)) > 0
                               or _port_67_listening()}
        return {"ok": True, "action": action, "managed": False, "reload_delegated": True,
                "msg": "运行在容器内，dnsmasq 由宿主机管理；配置已写入，等待宿主机重载单元加载",
                # running 必须用 :67 监听状态判断：容器与宿主机共享网络命名空间但不共享 PID，
                # _find_dnsmasq_pids 在容器里**恒为空**（会把在跑的 dnsmasq 显示成 stopped）。
                "running": len(_find_dnsmasq_pids(skip_zombies=True)) > 0 or _port_67_listening()}
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
