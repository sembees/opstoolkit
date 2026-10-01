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
import threading
import time

from app.core import dhcp as _dhcp
from app.core import filestore as _filestore

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

def _deploy_fail(log, errors, written=None, extra=None):
    """部署失败的统一返回结构（ok=False + 可读的 errors，调用方必须据此报错）。"""
    out = {
        "ok": False,
        "supported": True,
        "errors": list(errors),
        "log": log,
        "files_written": list(written or []),
        "tftp_root": TFTP_ROOT,
        "web_root": WEB_ROOT,
    }
    if extra:
        out.update(extra)
    return out


def deploy_files(files: dict, tid: str = "") -> dict:
    """对外入口：**应用侧串行化**（外部审查 R3-M2，与 PXE 同一处理）。

    理由见 `app/it/pxe/server.py::deploy_files`：宿主机只给重载加了 flock，而写文件这一半
    没有互斥 —— 两个并发部署会交叉写共享文件，磁盘上可能落在"半甲半乙"的状态。
    """
    with _DEPLOY_LOCK:
        return _deploy_files_impl(files, tid)


_DEPLOY_LOCK = threading.Lock()


def _deploy_files_impl(files: dict, tid: str = "") -> dict:
    """一键部署：设备配置写到 TFTP/HTTP + dnsmasq 配置写到宿主机 + **确认它真的生效**。

    返回值 {"ok","supported","errors","log","files_written",...}。
    **ok=False 是硬失败**：调用方（api/ztp.py）必须据此报错。

    R4（RUNBOOK §5.50）修掉的三重问题：
      1. 它写的是 opstk-ztp.conf，而宿主机的重载单元只监视 opstk-pxe.conf ⇒
         这份配置**永远不会被加载**；而 dhcp_control 在容器里返回 ok=True，
         接口于是**报成功**（与 §5.41 同类的假成功，且更彻底：连触发都没有）。
         而 dnsmasq 是**开机自启**的 —— 文件躺在 /etc/dnsmasq.d 里，
         任何一次重启（哪怕与本模块无关）都会把它加载起来。
      2. 生成的配置里 `interface=` 是**猜**的：容器里会猜到承载企业网的
         ens18（10.128.118.113），standalone 模式还带 `dhcp-range` ⇒ 一旦加载，
         就在骨干网段上开 DHCP 池、抢答企业 DHCP。所以修 1 必须**同时**修 2，
         否则等于把一颗地雷从"没接线"变成"接上线"。
      3. 落盘用 `open(dst,"w")` 非原子（设备随时可能在下载配置），
         且 ok 只看 dhcp_control，配置写失败也报成功。

    现在的四层保护（与 it/pxe/server.py 同一套路）：
      ① 红线检查：`dhcp.check_dhcp_conf_safety` —— 占位/不存在/骨干网网卡、
         池落在骨干网段，全部**拒绝**且零落盘；
      ② 预检：ZTP 自己的握手槽位（HOST_RELOAD_STATE_ZTP）确认重载链路可用；
      ③ 原子落盘 + 握手：写完等宿主机确认"跑的就是这份"（sha + 新鲜度）；
      ④ 回滚：只有能证明宿主机没重启过 dnsmasq 时才逐字节恢复。
    """
    if not _dhcp.is_linux():
        return {
            "ok": False,
            "supported": False,
            "platform": platform.system(),
            "errors": ["not linux"],
            "log": ["Linux only; use Download ZIP on non-Linux"],
        }
    files = files or {}
    log = []
    dnsmasq_content = files.get("dnsmasq.conf", "")

    # ① 红线检查：必须在**写任何文件之前**（包括 prepare_dirs）
    if dnsmasq_content:
        safe, why = _dhcp.check_dhcp_conf_safety(
            dnsmasq_content, own_path=_dhcp.ZTP_CONF)
        if not safe:
            return _deploy_fail(
                log, ["部署前红线检查未通过【本次未写入任何文件】：" + why],
                extra={"preflight_failed": True, "preflight_reason": "dhcp-safety"},
            )
    # ② 预检
    if dnsmasq_content:
        pf = _dhcp.reload_preflight(_dhcp.ZTP_CONF, state_path=_dhcp.HOST_RELOAD_STATE_ZTP)
        log += pf["log"]
        if not pf["ok"]:
            return _deploy_fail(
                log,
                ["部署前预检失败【本次未写入任何文件】：原因 " + (pf["reason"] or "unknown")
                 + " —— " + pf["hint"]],
                extra={"preflight_failed": True, "preflight_reason": pf["reason"]},
            )

    log += prepare_dirs()
    errors = []
    if not _dhcp.sudo_ok():
        log.append("WARNING: no sudo; dnsmasq config and restart will fail")

    # ③ 设备配置落盘：TFTP（设备来取）与 HTTP（人工核对/大文件）两个根，原子写
    written = []
    # ★ U2-F7：`written` 装的是给运维看的标签（`tftp/…`、`web/…`），与回滚快照 `prev`
    #   的键（绝对路径 `dst`）**不是同一个命名空间**；"本次写入内容的 sha"必须单独记，
    #   且要用 `prev` 的键。回滚前靠它判断"磁盘上还是不是我写的那份"。
    written_sha = {}
    prev = {}
    removed_paths = []
    for name, content in files.items():
        if name == "dnsmasq.conf":
            continue
        for root in (TFTP_ROOT, WEB_ROOT):
            dst = _safe_dst(root, name)
            if dst is None:
                errors.append("非法路径，已拒绝：" + repr(name) + "（" + root + "）")
                continue
            tag = ("tftp" if root == TFTP_ROOT else "web") + "/" + \
                  os.path.relpath(dst, root).replace(os.sep, "/")
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                if dst not in prev:
                    prev[dst] = _filestore.snapshot_path(dst)
                if os.path.isdir(dst):
                    # 目录挡路：**不删**（ztp/ 目录是正常结构，删它代价远大于收益）
                    raise OSError("目标已存在且是目录")
                _filestore.atomic_write(dst, content)
                written_sha[dst] = _filestore.content_sha(content)
            except OSError as e:
                errors.append("写入失败 " + tag + "：" + str(e)[:120])
                continue
            written.append(tag)
            log.append("Deployed: " + tag)
    if errors:
        # 还没碰 dnsmasq ⇒ 回滚无条件安全
        errs, written, extra = _filestore.try_rollback(
            log, errors, prev, removed_paths, None, "opstk-ztp.conf", written,
            written_sha=written_sha)
        return _deploy_fail(log, errs, written, extra)

    # ④ dnsmasq 配置 + 握手
    want_sha = ""
    not_before = int(time.time())
    conf_prev = None
    if dnsmasq_content:
        # 写之前作废 ZTP 槽位的旧标记（同内容重复部署不能命中陈旧 OK）
        _dhcp.invalidate_host_reload_state(_dhcp.HOST_RELOAD_STATE_ZTP)
        conf_prev = _filestore.snapshot_path(_dhcp.ZTP_CONF)
        if _dhcp.write_conf("opstk-ztp.conf", dnsmasq_content):
            log.append("Written: /etc/dnsmasq.d/opstk-ztp.conf")
            want_sha = _dhcp.conf_sha(dnsmasq_content)
        else:
            errors.append("FAILED: write dnsmasq config /etc/dnsmasq.d/opstk-ztp.conf")
            # 同 PXE 侧（外部审查 U2-F1）：write_conf 失败时文件**可能已被截断**，
            # 必须把已取好的 conf_prev 交给回滚，而不是传 None 把半截配置留在原地。
            errs, written, extra = _filestore.try_rollback(
                log, errors, prev, removed_paths, conf_prev, "opstk-ztp.conf", written,
                written_sha=written_sha)
            errs = list(errs) + [
                "配置写入失败，且**文件可能已被截断**（open('w') 会先清空）："
                "已尝试用部署前的快照恢复 dnsmasq 配置。",
            ]
            return _deploy_fail(log, errs, written, extra)

    svc = _dhcp.dhcp_control("restart")
    log.append("dnsmasq: " + str(svc.get("msg", "unknown")))
    if svc.get("reload_delegated") and want_sha:
        # 容器部署路径：必须等宿主机确认**ZTP 槽位**跑的就是刚写的这份，
        # 否则就是 §5.41 那种"写下去了但没生效、接口却报成功"的假成功。
        # 用带重试的等待：path 边沿事件可能被 systemd 合并掉（见 dhcp 里的说明）。
        got, nudges = _dhcp.wait_host_reload_retrying(
            want_sha, _dhcp.ZTP_CONF, state_path=_dhcp.HOST_RELOAD_STATE_ZTP,
            not_before=not_before)
        if nudges:
            log.append("等重载超时，已补触发 %d 次（边沿事件被合并）" % nudges)
        if got.get("ok"):
            log.append("宿主机 dnsmasq 已重载，ZTP 配置 sha 核对一致（生效）")
        else:
            st = got.get("state") or {}
            detail = (
                _dhcp.HOST_RELOAD_HINT
                + ("（宿主机重载脚本报：" + str(st.get("reason"))[:120] + "）" if st.get("reason") else "")
                + ("（读状态文件失败：" + str(st.get("err"))[:80] + "）" if st.get("err") else "")
                + ("" if st else "（ZTP 槽位的状态文件在超时前后都不存在：说明宿主机那个服务没能跑起来，"
                                "最常见的是 " + _dhcp.HOST_RELOAD_UNIT + " 没启用，或者装的还是"
                                "只监视 PXE 配置的旧版脚本。）")
            )
            if _dhcp.reload_failure_is_pre_restart(st, not_before=not_before):
                errs, written, extra = _filestore.try_rollback(
                    log, [detail], prev, removed_paths, conf_prev, "opstk-ztp.conf", written,
                    written_sha=written_sha, conf_written_sha=want_sha)
                if extra.get("rolled_back") and extra.get("rollback_conf_sha"):
                    back = _dhcp.wait_host_reload(
                        extra["rollback_conf_sha"], timeout=min(8.0, _dhcp.HOST_RELOAD_WAIT),
                        not_before=int(time.time()),
                        state_path=_dhcp.HOST_RELOAD_STATE_ZTP)
                    log.append("回滚确认：宿主机" + (
                        "已确认重新加载，sha 核对一致" if back.get("ok")
                        else "未在 8s 内确认；它本来就在运行部署前的配置，通常无碍"))
                return _deploy_fail(log, errs, written, extra)
            errors.append(
                detail
                + "【未回滚】宿主机没有给出「可以安全回滚」的证据（它可能已经重启过 dnsmasq），"
                  "此时把磁盘改回旧内容只会让「磁盘 vs 正在运行的守护进程」更不一致。"
                  "**注意**：ZTP 的配置若停在磁盘上，会在 dnsmasq 下一次重启（含开机）时被加载。"
                  "请先修好重载链路，然后**重新部署一次**即可恢复一致。"
            )
    elif not svc.get("ok"):
        errors.append("dnsmasq 重启失败：" + str(svc.get("msg", ""))[:120])

    return {
        "ok": bool(svc.get("ok")) and not errors,
        "supported": True,
        "errors": errors,
        "log": log,
        "files_written": written,
        "tftp_root": TFTP_ROOT,
        "web_root": WEB_ROOT,
    }
