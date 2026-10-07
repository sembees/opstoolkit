# -*- coding: utf-8 -*-
"""ISO 镜像传输后端：A) 服务端按 URL 拉取  B) 浏览器分块上传。

契约（冻结，见 docs 与前端单元）：/iso/space、/iso/transfers、/iso/fetch、
/iso/upload/init、/iso/upload/chunk、/iso/upload/finish、/iso/transfers/{id}/cancel，
全部挂在现有 PXE 路由前缀下（app/api/pxe.py 的 router → /api/it/pxe/iso/…）。

设计硬约束（为什么这样写）：
  · **并发上限 1**：同一时刻只允许一个传输（fetch 或 upload 会话）。准入用
    "注册表里有没有 state=running"来判定，有就 409（中文文案，前端直接展示）。
    拉取在发起**探测请求之前**就先把注册表占住（先注册再 await），单事件循环内
    不存在"两个 fetch 同时过了 busy 检查"的窗口。
  · **进度只在内存**：注册表是模块级 dict，**不建表、不写数据库**；进程重启即清空
    （.part 会在下次拉取时按续传逻辑接管，见下）。
  · **半截文件绝不叫 .iso**：所有落盘先写 `<ISO_DIR>/.<name>.part`，收尾校验
    （大小 / 魔数 / 可选 sha256）全部通过后才 os.replace() 原子改名 ——
    list_isos() 只认 *.iso，所以 .part 天然不出现在列表里。
  · **拉取续传**：.part 已存在时带 `Range: bytes=<已收>-` 发起请求；服务器回 206
    ⇒ 从断点续传；回 200（忽略 Range）⇒ 丢弃已有部分从头下载；回 416（例如
    .part 已完整）⇒ 也不带 Range 重发一次。sha256 无法"从中间续"，续传前先把
    .part 已有内容补算进哈希（一次性顺序读，代价可接受）。
  · **Content-Length 拿不到**：total 记 0 并持续累加 received；此时无法预先做
    空间准入，改为**每收一块就按同一条准入规则兜底**，超限立即中断并删 .part。
  · **错误一律中文**，HTTP 层把 TransferError 映射成 400（BusyError → 409、
    NotFoundError → 404），绝不把异常栈抛给前端（见 api/pxe.py 的映射）。
  · **不新增依赖**：下载用项目里已有的 httpx（飞书通知在用）；魔数/卷标识别纯 Python。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.it.pxe import server as pxe_server
from app.it.pxe.generator import _ISO_TYPE_KEYS, _ISO_VER_RE

# ── 常量 ──────────────────────────────────────────────────────────────────

CONCURRENCY = 1  # 契约字段：/iso/transfers 的 "concurrency"

# ISO9660 卷描述符从第 16 扇区（0x8000）开始：描述符类型 1 字节 + 标识 5 字节，
# 所以魔数在 0x8001。ISO9660 是 b"CD001"；UDF 桥接描述符是 BEA01 / NSR02 / NSR03 /
# TEA01（NSR0 是它们共同的前 4 字节）。通过其一即认为"是个 ISO/UDF 镜像"。
_PVD_OFFSET = 0x8000
_ISO_MAGICS = (b"CD001", b"BEA01", b"TEA01")
_UDF_MAGIC_PREFIX = b"NSR0"
# PVD 内卷标识（Volume Identifier）：偏移 40、长 32 字节，a-character，空格补齐。
_LABEL_AT = 40
_LABEL_LEN = 32

ERR_NOT_ISO = "不是有效的 ISO 镜像（未找到 ISO9660/UDF 卷描述符）"
ERR_BUSY = "已有一个镜像传输在进行中，请等它结束或取消后再试"

# 文件名白名单：字母/数字/._+- ，必须 .iso 结尾；路径分隔符在正则里就进不来，
# 再叠加 pxe_server._iso_path() 的 basename + commonpath 双保险。
_NAME_RE = re.compile(r"^[A-Za-z0-9._+-]+$")

_HASH_BLK = 1024 * 1024          # 补算/计算整文件 sha256 的读块大小
_KEEP_FINISHED = 50              # 注册表里最多保留多少条已结束记录（防内存膨胀）
UPLOAD_STALE_SECONDS = 30 * 60   # 上传会话无活动多久后可被清扫（契约：30 分钟）
SWEEP_INTERVAL = 300             # 清扫线程周期（秒）——"后台清扫即可，不必精确"

# 测试注入点：httpx.MockTransport 塞进来后 _new_client() 会用它；生产恒为 None。
_TEST_TRANSPORT = None
_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


# ── 错误类型（api 层据此映射状态码，detail 全部中文） ────────────────────

class TransferError(ValueError):
    """可对前端展示的 400 类错误（中文说明，带具体数字）。"""


class BusyError(TransferError):
    """已有传输在进行 → 409。"""


class NotFoundError(LookupError):
    """id 不存在 → 404。"""


# ── 注册表（内存） ────────────────────────────────────────────────────────

class _Transfer:
    """一条传输的进度记录。字段名与契约逐字对应（to_dict）。"""

    __slots__ = ("id", "kind", "name", "total", "received", "state", "error",
                 "started_at", "finished_at", "sha256", "detected", "part_path",
                 "expected_sha256", "cancel_flag", "task", "last_activity")

    def __init__(self, tid: str, kind: str, name: str, total: int, part_path: str):
        self.id = tid
        self.kind = kind            # "fetch" | "upload"
        self.name = name
        self.total = total          # 字节；未知（拉取无 Content-Length）时为 0
        self.received = 0
        self.state = "running"      # running | done | failed | canceled
        self.error = ""
        self.started_at = time.time()
        self.finished_at = 0.0
        self.sha256 = ""
        self.detected = {"os_type": "", "os_version": ""}
        self.part_path = part_path
        self.expected_sha256 = ""
        self.cancel_flag = False    # threading 安全的普通布尔（GIL 下读写原子）
        self.task = None            # fetch 的 asyncio.Task；upload 恒为 None
        self.last_activity = time.monotonic()  # 上传会话活动时间（清扫用）

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "name": self.name,
            "total": self.total, "received": self.received,
            "state": self.state, "error": self.error,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "sha256": self.sha256, "detected": dict(self.detected),
        }


_TRANSFERS: dict[str, _Transfer] = {}
_sweeper_started = False


def _reset_registry_for_tests() -> None:
    """测试辅助：清空注册表（生产不调用；进程重启等价于清空）。"""
    _TRANSFERS.clear()


def _busy_transfer() -> _Transfer | None:
    for t in _TRANSFERS.values():
        if t.state == "running":
            return t
    return None


def _prune_finished() -> None:
    dones = sorted((t for t in _TRANSFERS.values() if t.state != "running"),
                   key=lambda t: t.finished_at)
    while len(dones) > _KEEP_FINISHED:
        _TRANSFERS.pop(dones.pop(0).id, None)


def _settle(t: _Transfer, state: str, error: str = "") -> None:
    """终态只写一次：cancel 可能已抢先落终态，worker 收尾不得覆盖。"""
    if t.state == "running":
        t.state = state
        t.error = error
        t.finished_at = time.time()


# ── 小工具 ────────────────────────────────────────────────────────────────

def _fmt_gb(n: int) -> str:
    return "%.1f GB" % (n / 1024**3)


def _err_text(e: BaseException) -> str:
    return (str(e) or type(e).__name__)[:200]


def _disk_usage_safe() -> tuple[int, int, int]:
    """ISO 目录的 (total, used, free)；目录尚不存在时向上找最近的存在祖先。"""
    probe = pxe_server.ISO_DIR
    for _ in range(8):
        if os.path.isdir(probe):
            break
        parent = os.path.dirname(probe)
        if not parent or parent == probe:
            probe = pxe_server.ISO_DIR
            break
        probe = parent
    try:
        u = shutil.disk_usage(probe)
        return u.total, u.used, u.free
    except OSError:
        return 0, 0, 0


def _disk_free() -> int:
    return _disk_usage_safe()[2]


def _admission_error(need: int, free: int) -> str:
    """准入规则（契约）：free - need >= reserve 且 need <= min(max_bytes, free*0.6)。

    不满足时返回**带具体数字**的中文说明；满足返回 ""。
    """
    reserve = settings.pxe_iso_reserve_bytes
    if free - need < reserve:
        return ("磁盘可用 %s，本次需要 %s，扣除保留 %s 后不足"
                % (_fmt_gb(free), _fmt_gb(need), _fmt_gb(reserve)))
    cap = min(settings.pxe_iso_max_bytes, int(free * 0.6))
    if need > cap:
        return ("本次需要 %s，超过单镜像上限 %s（取磁盘可用 %s 的 60%% 与配置上限的较小值）"
                % (_fmt_gb(need), _fmt_gb(cap), _fmt_gb(free)))
    return ""


def validate_iso_filename(name) -> str:
    """文件名白名单（契约安全要求）：[A-Za-z0-9._+-]+ 且 .iso 结尾，无路径分隔符。

    通过后仍走 pxe_server._iso_path() 做 basename/commonpath 复核 —— 两层防护
    保持与既有 ISO 接口同一标准，避免新入口成为更弱的一环。
    """
    raw = str(name or "").strip()
    if not raw:
        raise TransferError("文件名不能为空")
    if "/" in raw or "\\" in raw or raw in (".", ".."):
        raise TransferError("文件名不能包含路径分隔符")
    if not _NAME_RE.fullmatch(raw):
        raise TransferError("文件名只能包含字母、数字和 . _ + - 字符，且必须以 .iso 结尾")
    if not raw.lower().endswith(".iso"):
        raise TransferError("文件名必须以 .iso 结尾")
    if pxe_server._iso_path(raw) is None:
        raise TransferError("文件名非法")
    return raw


def _part_path(name: str) -> str:
    # name 已过白名单（无分隔符、非 . / ..），join 结果必然在 ISO_DIR 内。
    return os.path.join(pxe_server.ISO_DIR, "." + name + ".part")


def _silent_remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for blk in iter(lambda: fh.read(_HASH_BLK), b""):
            h.update(blk)
    return h.hexdigest()


def _append_chunk(part: str, mode: str, chunk: bytes) -> None:
    """顺序追加一块并 flush+fsync —— 断电也不丢已确认的字节。"""
    with open(part, mode) as fh:
        fh.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())


# ── 魔数校验 + 卷标识别（纯 Python，无外部命令） ─────────────────────────

def check_iso_magic(path: str) -> bool:
    """读偏移 0x8001 起 5 字节：CD001（ISO9660）或 BEA01/NSR0*/TEA01（UDF）之一。"""
    try:
        with open(path, "rb") as fh:
            fh.seek(_PVD_OFFSET + 1)
            head = fh.read(5)
    except OSError:
        return False
    return head in _ISO_MAGICS or head[:4] == _UDF_MAGIC_PREFIX


def read_volume_label(path: str) -> str:
    """读 PVD 卷标识（0x8000+40，32 字节，空白/NUL 补齐）→ 去空白小写返回。

    读不出（文件太小/IO 错误）返回 ""，**不抛错** —— 卷标识别是尽力而为。
    """
    try:
        with open(path, "rb") as fh:
            fh.seek(_PVD_OFFSET)
            pvd = fh.read(_LABEL_AT + _LABEL_LEN)
    except OSError:
        return ""
    if len(pvd) < _LABEL_AT + _LABEL_LEN:
        return ""
    raw = pvd[_LABEL_AT:_LABEL_AT + _LABEL_LEN]
    return raw.decode("latin-1", "replace").strip().strip("\x00").strip()


def detect_os_from_label(label: str) -> dict:
    """卷标 → (os_type, os_version)，规则与 generator.pick_iso 同源（规格修订版）。

    ISO9660 卷标不允许 '.'，真实镜像形如 Rocky-9-4-x86_64-dvd / CENTOS_7_X86_64，
    而 _ISO_VER_RE 要求点分版本 —— 所以先把 '-' 和 '_' 归一化成 '.' 再抽版本，
    取**第一个**候选（版本段在架构段之前，Rocky-9-4-x86_64 → 9.4 而不是 86）。
    os_type 用 _ISO_TYPE_KEYS 里**命中关键字本身**（rocky/centos/ubuntu/…），
    而不是它的字典键（rhel），这样 CENTOS_7 → centos/7 而非 rhel/7。
    识别不出（任一为空）→ 两者都返回空串，不抛错。
    """
    norm = (label or "").strip().lower().replace("-", ".").replace("_", ".")
    os_type = ""
    for keys in _ISO_TYPE_KEYS.values():
        for k in keys:
            if k and k in norm and len(k) > len(os_type):
                os_type = k
    vers = _ISO_VER_RE.findall(norm)
    os_version = vers[0] if vers else ""
    if not os_type or not os_version:
        return {"os_type": "", "os_version": ""}
    return {"os_type": os_type, "os_version": os_version}


def detect_os_from_file(path: str) -> dict:
    return detect_os_from_label(read_volume_label(path))


# ── 查询接口（契约 GET /iso/space、GET /iso/transfers） ──────────────────

def space() -> dict:
    total, used, free = _disk_usage_safe()
    return {
        "dir": pxe_server.ISO_DIR,
        "total": total,
        "free": free,
        "used": used,
        "reserve": settings.pxe_iso_reserve_bytes,
        "max_upload": max(0, min(settings.pxe_iso_max_bytes, int(free * 0.6))),
    }


def list_transfers() -> dict:
    items = sorted(_TRANSFERS.values(), key=lambda t: t.started_at, reverse=True)
    return {
        "items": [t.to_dict() for t in items],
        "concurrency": CONCURRENCY,
        "busy": _busy_transfer() is not None,
    }


# ── A) 服务端按 URL 拉取 ─────────────────────────────────────────────────

def _new_client() -> httpx.AsyncClient:
    kw: dict = {"timeout": _TIMEOUT, "follow_redirects": True}
    if _TEST_TRANSPORT is not None:  # 测试注入 MockTransport；生产恒为 None
        kw["transport"] = _TEST_TRANSPORT
    return httpx.AsyncClient(**kw)


async def _safe_aclose(resp) -> None:
    try:
        await resp.aclose()
    except Exception:  # noqa: BLE001
        pass


async def _safe_client_aclose(client) -> None:
    try:
        await client.aclose()
    except Exception:  # noqa: BLE001
        pass


async def start_fetch(url, filename, sha256: str = "") -> dict:
    """POST /iso/fetch：校验 → 占住并发名额 → 探测大小并做空间准入 → 202 + 后台下载。

    探测用的就是正式的流式 GET（带 Range 续传头），头部到达即做 Content-Length
    准入；准入不过就地 400（不再读正文），通过则把这条流直接交给后台任务继续读
    —— 不浪费一次请求，也不会出现"探测通过了、正式下载又开始"的双请求竞态。
    """
    name = validate_iso_filename(filename)
    u = str(url or "").strip()
    if not u:
        raise TransferError("URL 不能为空")
    scheme = urlparse(u).scheme.lower()
    if scheme not in ("http", "https"):
        raise TransferError("URL 协议必须是 http 或 https")
    if _busy_transfer():
        raise BusyError(ERR_BUSY)

    t = _Transfer(uuid.uuid4().hex, "fetch", name, 0, _part_path(name))
    t.expected_sha256 = str(sha256 or "").strip().lower()
    _TRANSFERS[t.id] = t  # 先占住名额（探测期间 busy=true，别的传输会被 409）
    _prune_finished()

    client = _new_client()
    resp = None
    try:
        have = os.path.getsize(t.part_path) if os.path.isfile(t.part_path) else 0
        headers = {"Range": "bytes=%d-" % have} if have > 0 else None
        try:
            req = client.build_request("GET", u, headers=headers)
            resp = await client.send(req, stream=True)
            if have > 0 and resp.status_code == 416:
                # 服务器拒绝该 Range（.part 可能已完整）⇒ 当作不支持续传，从头再来
                await _safe_aclose(resp)
                resp = await client.send(client.build_request("GET", u), stream=True)
                have = 0
            resumed = have > 0 and resp.status_code == 206
            if not resumed and resp.status_code // 100 != 2:
                raise TransferError("镜像源返回 HTTP %d，无法下载" % resp.status_code)
            try:
                clen = int(resp.headers.get("content-length") or 0)
            except (TypeError, ValueError):
                clen = 0
            total = (have + clen) if resumed else clen
            if total > 0:
                # 拿得到总大小 ⇒ 现在就按准入规则拒绝（契约 400 + 具体数字）
                msg = _admission_error(total, _disk_free())
                if msg:
                    raise TransferError(msg)
            t.total = total
            t.received = have if resumed else 0
        except TransferError as e:
            await _safe_aclose(resp)
            await _safe_client_aclose(client)
            _settle(t, "failed", str(e))
            raise
        except (httpx.HTTPError, OSError) as e:
            await _safe_aclose(resp)
            await _safe_client_aclose(client)
            _settle(t, "failed", "无法连接镜像源：" + _err_text(e))
            raise TransferError(t.error) from e
        t.task = asyncio.create_task(_fetch_worker(t, client, resp, resumed))
        return {"id": t.id, "state": "running"}
    finally:
        if t.task is None:
            # worker 没接手（探测阶段就失败）⇒ 打开的流/客户端在这里收干净，
            # 不能泄漏到本函数之外。
            await _safe_aclose(resp)
            await _safe_client_aclose(client)


async def _fetch_worker(t: _Transfer, client, resp, resumed: bool) -> None:
    """后台下载主体：追加 .part → 实时进度 → 收尾三连校验（sha256/魔数/改名）。"""
    part = t.part_path
    final = pxe_server._iso_path(t.name)
    h = hashlib.sha256()
    try:
        exists = resumed and os.path.isfile(part)
        if exists:
            # 续传前把已有部分补进哈希（sha256 不能从中间续）
            with open(part, "rb") as fh:
                for blk in iter(lambda: fh.read(_HASH_BLK), b""):
                    h.update(blk)
            t.received = os.path.getsize(part)
        mode = "ab" if exists else "wb"
        async for chunk in resp.aiter_bytes(settings.pxe_transfer_chunk):
            if t.cancel_flag:
                raise _Cancelled()
            if not chunk:
                continue
            await asyncio.to_thread(_append_chunk, part, mode, chunk)
            mode = "ab"
            t.received += len(chunk)
            h.update(chunk)
            if t.total == 0:
                # Content-Length 拿不到 ⇒ 无法预先准入，边下边用同一条规则兜住
                msg = _admission_error(t.received, _disk_free())
                if msg:
                    raise TransferError(msg)
        if t.cancel_flag:
            raise _Cancelled()
        digest = h.hexdigest()
        if t.expected_sha256 and digest != t.expected_sha256:
            raise TransferError("SHA256 校验失败：期望 %s，实际 %s"
                                % (t.expected_sha256, digest))
        if not check_iso_magic(part):
            raise TransferError(ERR_NOT_ISO)
        t.sha256 = digest
        t.detected = detect_os_from_file(part)
        if final is None:
            raise TransferError("目标文件名非法")
        os.replace(part, final)  # 原子改名：.iso 出现即完整
        _settle(t, "done")
    except _Cancelled:
        _settle(t, "canceled", "已取消")
    except asyncio.CancelledError:
        _settle(t, "canceled", "已取消")
    except TransferError as e:
        _settle(t, "failed", str(e))
    except (httpx.HTTPError, OSError) as e:
        _settle(t, "failed", "下载中断：" + _err_text(e))
    except Exception as e:  # noqa: BLE001 —— 兜底：任何异常都转成中文失败，不抛栈
        _settle(t, "failed", "下载失败：" + _err_text(e))
    finally:
        if t.state != "done":
            _silent_remove(part)
        t.task = None
        await _safe_aclose(resp)
        await _safe_client_aclose(client)


class _Cancelled(Exception):
    """worker 内部信号：cancel_flag 置位后主动退出（区别于任务被 cancel）。"""


# ── B) 浏览器分块上传 ────────────────────────────────────────────────────

def upload_init(filename, size, sha256: str = "") -> dict:
    """POST /iso/upload/init：校验文件名/大小/空间 → 建 .part → 返回会话 id。

    init 之后该会话立即出现在 /iso/transfers（state=running，received 随分块增长）。
    """
    name = validate_iso_filename(filename)
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        raise TransferError("size 必须是大于 0 的整数（字节）")
    if _busy_transfer():
        raise BusyError(ERR_BUSY)
    msg = _admission_error(size, _disk_free())
    if msg:
        raise TransferError(msg)
    part = _part_path(name)
    _silent_remove(part)  # 同名残留的旧 .part 一律作废，重新从头收
    open(part, "wb").close()
    t = _Transfer(uuid.uuid4().hex, "upload", name, size, part)
    t.expected_sha256 = str(sha256 or "").strip().lower()
    _TRANSFERS[t.id] = t
    _prune_finished()
    _ensure_sweeper()
    return {"id": t.id, "received": 0, "chunk_size": settings.pxe_transfer_chunk}


def upload_chunk(tid, offset, data: bytes) -> dict:
    """POST /iso/upload/chunk：只接受**顺序追加**——offset 必须等于当前已收长度。"""
    t = _TRANSFERS.get(str(tid or ""))
    if t is None or t.kind != "upload":
        raise NotFoundError("上传会话不存在或已结束")
    if t.state != "running":
        raise TransferError("上传会话已结束（%s），请重新发起上传" % t.state)
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise TransferError("offset 必须是不小于 0 的整数")
    if offset != t.received:
        raise TransferError(
            "偏移量不连续：服务器已收到 %d 字节，请求却从 %d 字节开始；"
            "只支持顺序追加，下一个分块请从 offset=%d 开始"
            % (t.received, offset, t.received))
    if not data:
        raise TransferError("分块内容为空")
    if t.total and t.received + len(data) > t.total:
        raise TransferError("分块超出声明大小：已收 %d 字节，本块 %d 字节，声明共 %d 字节"
                            % (t.received, len(data), t.total))
    _append_chunk(t.part_path, "ab", data)
    t.received += len(data)
    t.last_activity = time.monotonic()
    return {"received": t.received}


def upload_finish(tid) -> dict:
    """POST /iso/upload/finish：大小一致 + 魔数通过 ⇒ 原子改名，返回识别结果。"""
    t = _TRANSFERS.get(str(tid or ""))
    if t is None or t.kind != "upload":
        raise NotFoundError("上传会话不存在或已结束")
    if t.state != "running":
        raise TransferError("上传会话已结束（%s）" % t.state)
    part = t.part_path

    def _fail(msg: str) -> TransferError:
        _settle(t, "failed", msg)
        _silent_remove(part)
        return TransferError(msg)

    actual = os.path.getsize(part) if os.path.isfile(part) else 0
    if actual != t.total:
        raise _fail("文件大小不一致：声明 %d 字节，实际收到 %d 字节" % (t.total, actual))
    if not check_iso_magic(part):
        raise _fail(ERR_NOT_ISO)
    digest = _sha256_file(part)
    if t.expected_sha256 and digest != t.expected_sha256:
        raise _fail("SHA256 校验失败：期望 %s，实际 %s" % (t.expected_sha256, digest))
    t.sha256 = digest
    t.detected = detect_os_from_file(part)
    final = pxe_server._iso_path(t.name)
    if final is None:
        raise _fail("目标文件名非法")
    os.replace(part, final)
    t.received = actual
    _settle(t, "done")
    return {"ok": True, "name": t.name, "size": actual, "detected": dict(t.detected)}


# ── 取消 + 过期会话清扫 ──────────────────────────────────────────────────

def cancel_transfer(tid) -> dict:
    """POST /iso/transfers/{id}/cancel：中断拉取流 / 丢弃上传会话；幂等，删 .part。"""
    t = _TRANSFERS.get(str(tid or ""))
    if t is None:
        raise NotFoundError("传输任务不存在")
    if t.state == "running":
        t.cancel_flag = True
        _settle(t, "canceled", "已取消")
        _silent_remove(t.part_path)
        task = t.task
        if task is not None and not task.done():
            try:
                task.cancel()  # 打断可能卡在 网络 await 上的下载循环
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True}


def sweep_stale_sessions(now: float | None = None) -> int:
    """清理超过 30 分钟无活动的上传会话（后台清扫，不必精确；拉取不归它管）。"""
    now = time.monotonic() if now is None else now
    n = 0
    for t in list(_TRANSFERS.values()):
        if (t.kind == "upload" and t.state == "running"
                and now - t.last_activity > UPLOAD_STALE_SECONDS):
            t.cancel_flag = True
            _settle(t, "canceled",
                    "上传会话超过 %d 分钟无活动，已被自动清理" % (UPLOAD_STALE_SECONDS // 60))
            _silent_remove(t.part_path)
            n += 1
    return n


def _sweep_loop() -> None:  # pragma: no cover - 守护线程，测试直接调 sweep_stale_sessions
    while True:
        time.sleep(SWEEP_INTERVAL)
        try:
            sweep_stale_sessions()
        except Exception:
            pass


def _ensure_sweeper() -> None:
    global _sweeper_started
    if _sweeper_started:
        return
    _sweeper_started = True
    threading.Thread(target=_sweep_loop, name="iso-upload-sweeper",
                     daemon=True).start()
