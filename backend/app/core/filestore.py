"""落盘原语：原子写、快照、回滚。

从 `it/pxe/server.py` 搬出来的（PXE 与 ZTP 都要"要么整套生效、要么什么都没变"，
而这两条正是安全关键逻辑 —— 复制一份必然会在某次修复里漂移）。

三件事：
  · `atomic_write`：临时文件 + rename。读方（设备 / iPXE / cloud-init）永远看不到
    半截内容，写失败也不留半截文件。
  · `snapshot_path`：记下部署前的内容，供失败回滚。
  · `restore_previous` / `try_rollback`：把本次写下的东西逐字节恢复成部署前，
    **且只在调用方能证明"宿主机没有重启过 dnsmasq"时才允许调用**
    （见 dhcp.reload_failure_is_pre_restart 与各调用点的说明）。
"""
from __future__ import annotations

import os
import uuid
from contextlib import nullcontext

from app.core import dhcp as _dhcp


def atomic_write(dst: str, content) -> None:
    """写临时文件 + os.replace：原子替换，读方永远看不到"写了一半"的文件。

    装机中的机器随时可能在 GET 这些文件（iPXE 取 boot/<mac>.ipxe、交换机取 ztp 配置、
    casper 取 user-data），半截内容会让它装错或直接失败，而调用方完全看不出来。
    临时文件放在**目标同目录**（同一文件系统），os.replace 才是原子的（Windows 亦然）。
    临时名带 pid + 随机串：同一目标被两个并发部署写时，不会互相踩对方的临时文件。

    统一用 LF：这些文件在 Linux 上被 iPXE / dnsmasq / cloud-init / 网络设备读取，
    在 Windows 上开发测试时也不该因为换行翻译而与实际落盘内容不一致。
    content 传 bytes 时**逐字节原样落盘**（不做换行翻译）—— 回滚要把旧文件恢复成
    部署前的字节，若经过一次换行翻译，"恢复"出来的就不是原来那份了。
    """
    tmp = "%s.tmp.%d.%s" % (dst, os.getpid(), uuid.uuid4().hex[:8])
    try:
        if isinstance(content, bytes):
            with open(tmp, "wb") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        else:
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, dst)
    except BaseException:
        # 失败不许留下 .tmp 垃圾（也不能让半个文件被下一次部署/读方当成有效文件）
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def snapshot_path(path: str) -> dict:
    """记下某个落盘目标在**本次部署之前**的内容，供失败回滚使用。

    返回 {"existed": bool, "data": bytes|None, "why": str}。
    data=None 且 existed=True 表示"存在，但不是本工具能原样还原的普通文件"（目录、
    读不了的东西）—— 此时回滚必须**整体放弃**：半套回滚比不回滚更难解释，
    而运维要能相信"它说回滚了就是回到了原样"。
    """
    try:
        if os.path.isdir(path):
            return {"existed": True, "data": None, "why": "部署前它是目录"}
        if not os.path.exists(path):
            return {"existed": False, "data": None, "why": ""}
        with open(path, "rb") as fh:
            return {"existed": True, "data": fh.read(), "why": ""}
    except OSError as e:
        return {"existed": True, "data": None, "why": (e.strerror or str(e))}


def restore_previous(prev, removed_paths, conf_prev, conf_name, log,
                     lock=None, lock_path=None) -> dict:
    """把本次部署已经写下的文件恢复成部署前的内容。

    只在**能证明宿主机的 dnsmasq 没被重启过**时才调用（见
    dhcp.reload_failure_is_pre_restart）：那时把磁盘恢复成原样就等于回到部署前，
    网络是完全一致的。若重启可能已经发生，恢复磁盘只会让"磁盘 vs 守护进程"
    更不一致 —— 那种情况必须**保持现状并大声报告**，交给运维决定。

    整体成败：只要能还原的路径都还原成功，返回 ok=True；任何一处失败，
    返回 ok=False + why，并且**不做**部分回滚（见 snapshot_path 的说明）。
    conf_name 是 dnsmasq 配置的文件名（"opstk-pxe.conf" / "opstk-ztp.conf"），
    它由 dhcp.write_conf 写、可能走 sudo tee，所以单独处理。
    """
    why = ""
    if prev:
        blockers = [(p, s["why"]) for p, s in prev.items() if s["existed"] and s["data"] is None]
        if blockers:
            why = "；".join(p + "：" + w for p, w in blockers[:3])
    if removed_paths:
        why = (why + "；" if why else "") + ("本次部署为让路删除过 " + str(len(removed_paths))
                                            + " 个路径（" + ", ".join(sorted(removed_paths)[:3]) + "），删掉的内容无法恢复")
    conf_text = None
    conf_existed = bool(conf_prev is not None and conf_prev["existed"])
    if conf_prev is not None and conf_prev["existed"]:
        if conf_prev["data"] is None:
            why = (why + "；" if why else "") + conf_name + " 在部署前不可读"
        else:
            try:
                conf_text = conf_prev["data"].decode("utf-8")
            except UnicodeDecodeError:
                why = (why + "；" if why else "") + conf_name + " 在部署前不是 UTF-8 文本"
    if why:
        return {"ok": False, "restored": 0, "why": why, "conf_text": None, "conf_existed": False}

    failed = []
    restored = 0
    for path, snap in prev.items():
        guard = lock if (lock is not None and path == lock_path) else nullcontext()
        try:
            with guard:
                if snap["existed"]:
                    atomic_write(path, snap["data"])
                elif os.path.exists(path):
                    os.remove(path)
            restored += 1
            log.append("Rolled back: " + path)
        except OSError as e:
            failed.append(path + "（" + (e.strerror or str(e)) + "）")
    if conf_prev is not None:
        try:
            if conf_text is not None:
                if _dhcp.write_conf(conf_name, conf_text):
                    restored += 1
                    log.append("Rolled back: " + os.path.join(_dhcp.CONF_DIR, conf_name))
                else:
                    failed.append(conf_name + "（写入失败）")
            else:
                # 部署前不存在 → 删掉本次新建的这份，回到"没有这个配置文件"的状态
                if _dhcp.remove_conf(conf_name):
                    restored += 1
                    log.append("Rolled back: 删除本次新建的 " + conf_name)
                else:
                    failed.append(conf_name + "（删除失败）")
        except Exception as e:  # noqa: BLE001  回滚路径绝不能因为异常而崩掉
            failed.append(conf_name + "（" + str(e)[:60] + "）")
    if failed:
        return {"ok": False, "restored": restored, "why": "；".join(failed[:3]),
                "conf_text": conf_text, "conf_existed": conf_existed}
    return {"ok": True, "restored": restored, "why": "", "conf_text": conf_text,
            "conf_existed": conf_existed}


def try_rollback(log, errors, prev, removed_paths, conf_prev, conf_name, written,
                 lock=None, lock_path=None):
    """失败收尾：把本次已经写下的东西恢复成部署前的内容。

    **只在能证明宿主机没有重启过 dnsmasq 时才调用** —— 回滚的正确性完全建立在这条前提上。

    返回 (errors, written, extra)：
      · 回滚成功 → written 清空（本次没有净写入任何东西）、extra["rolled_back"]=True
      · 回滚放弃/失败 → written 原样保留，并追加一条说明为什么没回滚，
        绝不让调用方以为"已经恢复原样了"。
    """
    if not written and conf_prev is None:
        return errors, written, {}
    rb = restore_previous(prev, removed_paths, conf_prev, conf_name, log,
                          lock=lock, lock_path=lock_path)
    if rb["ok"]:
        errs = list(errors) + [
            "【已回滚】本次已写入的 " + str(rb["restored"])
            + " 个文件（含 dnsmasq 配置）已全部恢复成部署前的内容；宿主机没有被重启过，"
              "因此网络状态与部署前完全一致 —— 本次部署没有任何实际效果。"
        ]
        extra = {"rolled_back": True, "files_restored": rb["restored"]}
        if rb.get("conf_text"):
            # 回滚把旧配置写回去了 ⇒ 宿主机脚本会再触发一次。调用方可以用这个 sha
            # 去确认"宿主机重新与磁盘一致"（旧配置内容未变，通常不会重启 dnsmasq）。
            extra["rollback_conf_sha"] = _dhcp.conf_sha(rb["conf_text"])
        return errs, [], extra
    errs = list(errors) + [
        "【无法回滚】" + (rb["why"] or "未知原因")
        + "。已写入的文件保持现状（**不做**半套回滚，以免运维以为已经恢复原样）；"
          "请按上面的路径人工核对内容。"
    ]
    return errs, written, {"rolled_back": False, "rollback_error": rb["why"]}
