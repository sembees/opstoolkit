# -*- coding: utf-8 -*-
"""对抗性测试 · ISO 镜像传输（transfers.py）—— 审计方独立复核，目标是证伪。

与 test_iso_transfer.py（作者自测）的关系：只测作者**没覆盖**的边界，全部结论
来自本文件独立跑出来的结果（在当前代码上真跑，不为了变绿而改）。

覆盖的边界（作者自测没碰的）：
  1. fetch 收到 httpx 解析不了的 URL（如 http:////x.iso）——注册表名额是否归还？
     契约要求一切错误映射为中文 400；实测见 test_…_malformed_url_…。
  2. 取消落在 upload_finish 的校验窗口内（size/magic/sha256 已过、os.replace 未落）——
     finish 是 asyncio.to_thread 跑的真线程，cancel 在事件循环上跑，两者真实并发。
  3. 续传(206)但服务器不回 Content-Length——文档承诺"无 Content-Length 时边下边
     按准入规则兜底"，恢复分支里兑现了吗？
  4. 同一 offset 的两块并发到达（浏览器重试/代理重试）——"只接受顺序追加"在
     并发下是否仍然成立？
  5. .part 已存在且**比远端还大**（416 分支）——能否从头恢复？
  6. Content-Length=0 的空源——绝不能产出一个空的 .iso。
  7. 会话生命周期交叉：cancel 后 finish、finish 两次——必须都是可读的 400。

联网边界：全部 httpx.MockTransport；ISO_DIR 一律临时目录；注册表每用例清零。
"""
import asyncio
import pathlib
import sys
import threading
import time

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.it.pxe import server as pxe_server  # noqa: E402
from app.it.pxe import transfers  # noqa: E402

ISO = "/api/it/pxe/iso"
ADMIN = {"id": "t", "username": "t", "display_name": "t", "role": "admin"}


def _iso_bytes(label: bytes = b"ROCKY-9-4-X86_64-DVD", size: int = 0x8800) -> bytes:
    buf = bytearray(size)
    buf[0x8001:0x8001 + 5] = b"CD001"
    lab = (label + b" " * 32)[:32]
    buf[0x8028:0x8028 + 32] = lab
    return bytes(buf)


def _make_client(raise_server_exceptions: bool = True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import pxe as pxe_api
    from app.core.auth import get_current_user

    app = FastAPI()
    app.include_router(pxe_api.router, prefix="/api/it/pxe")
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    iso_dir = tmp_path / "iso"
    iso_dir.mkdir()
    monkeypatch.setattr(pxe_server, "ISO_DIR", str(iso_dir))
    transfers._TRANSFERS.clear()
    yield iso_dir
    transfers._TRANSFERS.clear()
    transfers._TEST_TRANSPORT = None


# ── 1) 恶意/畸形 URL：httpx 解析不了的 URL（urlparse 能过 scheme 白名单） ──

def test_adversarial_fetch_unparseable_url_maps_400_and_releases_slot(iso_env, monkeypatch):
    """http:////x.iso 的 scheme 是 http（白名单放行），但 httpx 解析 URL 时抛
    InvalidURL（ValueError 子类，不是 httpx.HTTPError）。

    契约（transfers.py 模块注释 + api/pxe.py）：一切可预期错误都应是中文 400；
    并发名额（注册表 running 槽位）必须归还，否则后续传输永远 409。

    审计预期：响应 400（TransferError），注册表无 running 残留。
    实测：httpx.InvalidURL 在探测阶段（注册表已占位后）逃出 except 分支
    ⇒ 接口 500（detail="ISO 传输内部错误：InvalidURL"），且注册表该条目永远
    state=running —— 后续任何 fetch/upload 一律 409，直到进程重启。
    """
    transfers._TEST_TRANSPORT = httpx.MockTransport(
        lambda req: httpx.Response(200, content=b"x"))
    with _make_client(raise_server_exceptions=False) as c:
        r = c.post(ISO + "/fetch", json={"url": "http:////x.iso",
                                         "filename": "x.iso"})
        assert r.status_code == 400, (
            "契约要求 TransferError→400；实际 %s：%s" % (r.status_code, r.text))
        items = c.get(ISO + "/transfers").json()
        assert items["busy"] is False, "畸形 URL 失败后并发名额必须归还；实际：%s" % items
    # 名额归还后，下一次正常传输必须开得出来（不是 409）
    with _make_client(raise_server_exceptions=False) as c2:
        r2 = c2.post(ISO + "/fetch", json={"url": "http://m/ok.iso",
                                           "filename": "ok.iso"})
        assert r2.status_code == 202, r2.text
        c2.post(ISO + "/transfers/%s/cancel" % r2.json()["id"])


# ── 2) cancel 落在 upload_finish 的校验窗口内 ─────────────────────────────

def test_adversarial_upload_cancel_during_finish_is_handled_error(iso_env, monkeypatch):
    """上传分块已收满，finish 正在校验（真线程，asyncio.to_thread），此时用户点取消。

    两个入口真实并发：finish 在线程里，cancel 在事件循环里。校验窗口 =
    os.path.getsize(part) 通过之后、os.replace(part, final) 之前。cancel 会
    settle canceled 并删 .part；finish 随后的 os.replace 撞 FileNotFoundError。

    契约：任何失败都必须是可读的中文 TransferError（API → 400），绝不裸抛。
    审计预期：isinstance(e, transfers.TransferError)。
    实测：os.replace 抛裸 FileNotFoundError（WinError 2）→ API 层兜底成 500
    "ISO 传输内部错误：FileNotFoundError"。
    """
    data = _iso_bytes()
    s = transfers.upload_init("fin.iso", len(data))
    tid = s["id"]
    transfers.upload_chunk(tid, 0, data)

    orig_sha = transfers._sha256_file
    in_window, release = threading.Event(), threading.Event()

    def slow_sha(path):
        h = orig_sha(path)      # 真哈希照算（内容没变）
        in_window.set()         # 此刻 finish 已过 size+magic，站在窗口里
        release.wait(5)         # 拉宽窗口，给 cancel 真实的落点
        return h

    monkeypatch.setattr(transfers, "_sha256_file", slow_sha)

    finish_out: list = []

    def run_finish():
        try:
            transfers.upload_finish(tid)
            finish_out.append(("ok", None))
        except Exception as e:  # noqa: BLE001
            finish_out.append(("raise", e))

    th = threading.Thread(target=run_finish)
    th.start()
    assert in_window.wait(5), "finish 未进入校验窗口（用例自身失败）"
    transfers.cancel_transfer(tid)          # 生产里就是"取消"按钮
    release.set()
    th.join(5)

    assert not finish_out or finish_out[0][0] == "ok" or isinstance(
        finish_out[0][1], transfers.TransferError), (
        "cancel 竞态下的 finish 必须抛可展示的 TransferError（API→400），"
        "实际：%r" % (finish_out[:1],))
    # 收尾：不留 running 槽位
    transfers._TRANSFERS.clear()


# ── 3) 续传 + 服务器不回 Content-Length：准入兜底必须仍然生效 ─────────────

def test_adversarial_resumed_fetch_without_content_length_enforces_cap(iso_env, monkeypatch):
    """.part 已有 50 字节 ⇒ 带 Range 探测 ⇒ 服务器回 206（无 Content-Length）续传。

    transfers.py 的契约注释："无 Content-Length 时边下边按准入规则兜底"。
    作者用例只测了**非续传**(200)的兜底；本条证明续传分支把兜底丢了：
      start_fetch 里 total = have + clen = 50 + 0 = 50 > 0 ⇒ 预检只按 50 过；
      worker 里 per-chunk 兜底的条件是 `if t.total == 0` —— total 已是 50，
      兜底被整体跳过 ⇒ 上限(cap=100)形同虚设，实际收到 1000 字节。
    真实影响：断点续传碰上不回 Content-Length 的镜像源（反代/CGI 常见），
    磁盘准入完全失效，只能靠魔数/写满失败兜底。
    """
    monkeypatch.setattr(settings, "pxe_iso_max_bytes", 100, raising=False)
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 0, raising=False)
    monkeypatch.setattr(settings, "pxe_transfer_chunk", 64, raising=False)
    (iso_env / ".x.iso.part").write_bytes(b"P" * 50)   # 断点：已有 50 字节
    big = b"B" * 1000                                   # 远端实际数据远超 cap

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range"):
            start = int(request.headers.get("range").split("=")[1].split("-")[0])

            async def gen():
                for i in range(start, len(big), 30):
                    yield big[i:i + 30]
            return httpx.Response(206, content=gen())   # 无 content-length
        return httpx.Response(200, content=big)

    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 202, r.text
        tid = r.json()["id"]
        deadline = time.time() + 6
        item = None
        while time.time() < deadline:
            data = c.get(ISO + "/transfers").json()
            item = next((i for i in data["items"] if i["id"] == tid), None)
            if item and item["state"] != "running":
                break
            time.sleep(0.02)
        assert item is not None and item["state"] != "running", item
        # 契约：无论哪条路径，超过 cap 的下载都必须在"超过单镜像上限"上失败，
        # 且已收字节数不得越过 cap 的量级（cap=100）。
        assert "超过单镜像上限" in (item["error"] or ""), (
            "续传 + 无 Content-Length 时准入兜底未生效，实际错误：%r" % item["error"])
        assert item["received"] <= 100, (
            "cap=100 却实际收到 %d 字节 —— 续传分支完全绕过磁盘准入" % item["received"])


# ── 4) 同一 offset 的两块并发到达 ────────────────────────────────────────

def test_adversarial_duplicate_offset_chunk_race_rejects_second(iso_env, monkeypatch):
    """契约：分块上传"只接受顺序追加——offset 必须等于当前已收长度"。

    upload_chunk 的"检查 offset == t.received → 落盘 → received += len"不是原子的：
    api/pxe.py 用 asyncio.to_thread 跑它，两个携带同一 offset 的请求（浏览器把
    同一块重发一次、或代理重试）在两个线程里都会通过检查、都会落盘。
    审计用 _append_chunk 处的 barrier 拉宽窗口（仅测试侧，不动产品代码）。

    契约预期：两块里**恰好一块**被接受，另一块得到 400（偏移量不连续）。
    实测：两块都被接受，.part 变成 A块+B块（同一逻辑块落盘两次）。
    """
    s = transfers.upload_init("race.iso", 0x8800)
    tid = s["id"]
    orig_append = transfers._append_chunk
    barrier = threading.Barrier(2)

    def twin_append(part, mode, chunk):
        barrier.wait(timeout=5)
        return orig_append(part, mode, chunk)

    monkeypatch.setattr(transfers, "_append_chunk", twin_append)

    results = []

    def chunker(tag):
        try:
            r = transfers.upload_chunk(tid, 0, (tag * 10).encode())
            results.append(("ok", r))
        except Exception as e:  # noqa: BLE001
            results.append(("err", e))

    ths = [threading.Thread(target=chunker, args=("A",)),
           threading.Thread(target=chunker, args=("B",))]
    for t in ths:
        t.start()
    for t in ths:
        t.join(6)

    accepted = [r for k, r in results if k == "ok"]
    rejected = [r for k, r in results if k == "err"]
    assert len(accepted) == 1 and len(rejected) == 1, (
        "同 offset 并发两块必须恰收一块拒一块；实际 accept=%d reject=%d：results=%r"
        % (len(accepted), len(rejected), results))
    part = iso_env / ".race.iso.part"
    assert part.stat().st_size == 10, (
        ".part 落盘 %d 字节 —— 同一块逻辑数据被写了两遍（顺序追加契约被破坏）"
        % part.stat().st_size)
    transfers._TRANSFERS.clear()


# ── 5) .part 比远端还大（416 分支）—— 应从头恢复 ─────────────────────────

def test_adversarial_fetch_part_larger_than_remote_recovers(iso_env, monkeypatch):
    """磁盘上残留一个 100000 字节的 .part，远端其实只有 34816 字节。
    正确路径：带 Range 请求 → 416 → 丢弃断点从头下载 → 最终 .iso == 远端内容。
    """
    small = _iso_bytes()
    (iso_env / ".x.iso.part").write_bytes(b"Q" * 100000)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("range"):
            return httpx.Response(416, content=b"")
        return httpx.Response(200, headers={"content-length": str(len(small))},
                              content=small)

    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 202, r.text
        tid = r.json()["id"]
        deadline = time.time() + 6
        while time.time() < deadline:
            body = c.get(ISO + "/transfers").json()
            it = next((i for i in body["items"] if i["id"] == tid), None)
            if it and it["state"] != "running":
                break
            time.sleep(0.02)
        assert it["state"] == "done", it
    final = iso_env / "x.iso"
    assert final.exists() and final.read_bytes() == small, "恢复后内容必须等于远端"


# ── 6) 空源（Content-Length: 0）──────────────────────────────────────────

def test_adversarial_fetch_empty_source_never_produces_iso(iso_env, monkeypatch):
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 0, raising=False)
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-length": "0"}, content=b"")))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/empty.iso",
                                         "filename": "empty.iso"})
        assert r.status_code == 202, r.text
        tid = r.json()["id"]
        deadline = time.time() + 6
        while time.time() < deadline:
            body = c.get(ISO + "/transfers").json()
            it = next((i for i in body["items"] if i["id"] == tid), None)
            if it and it["state"] != "running":
                break
            time.sleep(0.02)
        assert it["state"] == "failed", it
        assert "不是有效的 ISO" in it["error"], it
    assert not (iso_env / "empty.iso").exists(), "空源绝不能落地成一个 .iso"
    assert not list(iso_env.glob(".*"))


# ── 8) upload_init 的 OSError 路径必须映射成中文 400，而不是裸 500 ────────

def test_adversarial_upload_init_oserror_paths_are_handled_errors(iso_env, monkeypatch):
    """transfers.py 的契约："错误一律中文，HTTP 层把 TransferError 映射成 400，
    绝不把异常栈抛给前端"。但 upload_init 在校验之后的 open(part,"wb") 没有任何
    OSError 兜底 —— 以下三种都是小白可达的触发面：
      ① ISO 目录不存在（fresh install 还没建 /srv/opstk/iso，或挂载失败场景）；
      ② .part 路径被一个同名目录占用（残留）；
      ③ 文件名合法但超长（250 字符过 validate_iso_filename，OS 拒绝落盘）。
    契约预期：TransferError（API→400，中文）。
    实测：裸 OSError（PermissionError/FileNotFoundError/OSError[Errno 22]）
    → api/pxe.py 兜底 500 "ISO 传输内部错误：PermissionError" 等。
    """
    # ① ISO_DIR 指向不存在的目录
    missing = iso_env / "no-such-dir"
    monkeypatch.setattr(pxe_server, "ISO_DIR", str(missing))
    with pytest.raises(transfers.TransferError) as ei:
        transfers.upload_init("x.iso", 0x8800)
    assert not isinstance(ei.value, OSError) or isinstance(ei.value, transfers.TransferError)

    # ② .part 路径是目录
    monkeypatch.setattr(pxe_server, "ISO_DIR", str(iso_env))
    (iso_env / ".dir2.iso.part").mkdir()
    with pytest.raises(transfers.TransferError):
        transfers.upload_init("dir2.iso", 0x8800)

    # ③ 合法但超长的文件名
    with pytest.raises(transfers.TransferError):
        transfers.upload_init("a" * 250 + ".iso", 0x8800)
    transfers._TRANSFERS.clear()


# ── 7) 会话生命周期交叉：cancel 后 finish、finish 两次 ───────────────────

def test_adversarial_upload_session_lifecycle_edges_are_handled(iso_env):
    data = _iso_bytes()
    # cancel 后 finish → 会话已结束（canceled）
    s1 = transfers.upload_init("cf.iso", len(data))
    transfers.upload_chunk(s1["id"], 0, data)
    transfers.cancel_transfer(s1["id"])
    try:
        transfers.upload_finish(s1["id"])
        raise AssertionError("cancel 后 finish 应当报错")
    except transfers.TransferError as e:
        assert "已结束" in str(e), e
    except FileNotFoundError as e:
        raise AssertionError("裸 FileNotFoundError（API→500）：%r" % e)

    # finish 两次 → 第二次必须报"会话已结束（done）"
    s2 = transfers.upload_init("tw.iso", len(data))
    transfers.upload_chunk(s2["id"], 0, data)
    transfers.upload_finish(s2["id"])
    try:
        transfers.upload_finish(s2["id"])
        raise AssertionError("第二次 finish 应当报错")
    except transfers.TransferError as e:
        assert "已结束" in str(e), e
    assert (iso_env / "tw.iso").exists()
    transfers._TRANSFERS.clear()
