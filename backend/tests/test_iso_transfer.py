# -*- coding: utf-8 -*-
"""ISO 镜像传输后端单元测试 —— **全部 mock，绝不真实联网**。

被测对象：app/it/pxe/transfers.py + app/api/pxe.py 的 /iso/* 传输端点。

联网边界（读用例前先看这里）：
  · 所有外发 HTTP 都走 httpx.MockTransport（注入 transfers._TEST_TRANSPORT），
    请求根本不会离开进程 —— 不存在真实联网的可能；
  · ISO_DIR 一律 monkeypatch 到 pytest 临时目录，绝不碰 /srv/opstk/iso；
  · 进度注册表每个用例前后清空，互不污染。
"""
import asyncio
import hashlib
import pathlib
import sys
import time

import httpx
import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import Settings, settings  # noqa: E402
from app.it.pxe import server as pxe_server  # noqa: E402
from app.it.pxe import transfers  # noqa: E402

ISO = "/api/it/pxe/iso"
ADMIN = {"id": "t", "username": "t", "display_name": "t", "role": "admin"}
LABEL_LEN = 32
ERR_NOT_ISO = "不是有效的 ISO 镜像（未找到 ISO9660/UDF 卷描述符）"
ERR_BUSY = "已有一个镜像传输在进行中，请等它结束或取消后再试"


# ── 构造工具 ──────────────────────────────────────────────────────────────

def _iso_bytes(label: bytes = b"ROCKY-9-4-X86_64-DVD", magic: bytes = b"CD001",
               size: int = 0x8800) -> bytes:
    """合成一个"合法 ISO"：0x8001 魔数 + PVD 卷标（0x8028，32 字节空格补齐）。"""
    buf = bytearray(size)
    buf[0x8001:0x8001 + len(magic)] = magic
    lab = (label + b" " * LABEL_LEN)[:LABEL_LEN]
    buf[0x8028:0x8028 + LABEL_LEN] = lab
    return bytes(buf)


def _make_client(role: str = "admin"):
    """最小 FastAPI app：只挂 pxe 路由，认证用 dependency_overrides 顶掉。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import pxe as pxe_api
    from app.core.auth import get_current_user

    app = FastAPI()
    app.include_router(pxe_api.router, prefix="/api/it/pxe")
    user = dict(ADMIN, role=role)
    app.dependency_overrides[get_current_user] = lambda: user
    # with 块内所有请求共享同一个事件循环 —— 后台下载任务才能跨请求存活
    return TestClient(app)


def _patch_settings(monkeypatch, **over):
    for k, v in over.items():
        monkeypatch.setattr(settings, k, v, raising=False)


def _transport_log(data: bytes, log: list, status: int = 200,
                   respect_range: bool = False):
    """MockTransport：可记录请求、可选按 Range 回 206（其余 200 全量）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        log.append(request)
        rng = request.headers.get("range")
        if respect_range and rng:
            start = int(rng.split("=")[1].split("-")[0])
            body = data[start:]
            return httpx.Response(
                206, headers={"content-length": str(len(body))}, content=body)
        return httpx.Response(status, headers={"content-length": str(len(data))},
                              content=data)
    return httpx.MockTransport(handler)


def _wait_terminal(client, tid: str, timeout: float = 6.0) -> dict:
    deadline = time.time() + timeout
    item = None
    while time.time() < deadline:
        data = client.get(ISO + "/transfers").json()
        item = next((i for i in data["items"] if i["id"] == tid), None)
        if item and item["state"] != "running":
            return item
        time.sleep(0.02)
    raise AssertionError("transfer %s not settled, last=%r" % (tid, item))


def _item(client, tid: str) -> dict | None:
    data = client.get(ISO + "/transfers").json()
    return next((i for i in data["items"] if i["id"] == tid), None)


# ── fixtures ─────────────────────────────────────────────────────────────

@pytest.fixture()
def iso_env(tmp_path, monkeypatch):
    """ISO_DIR → 临时目录；注册表清零；_TEST_TRANSPORT 用后还原。"""
    iso_dir = tmp_path / "iso"
    iso_dir.mkdir()
    monkeypatch.setattr(pxe_server, "ISO_DIR", str(iso_dir))
    transfers._TRANSFERS.clear()
    yield iso_dir
    transfers._TRANSFERS.clear()
    transfers._TEST_TRANSPORT = None


@pytest.fixture()
def client(iso_env):
    c = _make_client()
    with c:
        yield c


# ── A) 拉取 ──────────────────────────────────────────────────────────────

def test_fetch_success_happy_path(iso_env, monkeypatch):
    data = _iso_bytes()
    log: list = []
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", _transport_log(data, log))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://mirror.internal/rocky.iso",
                                         "filename": "rocky.iso"})
        assert r.status_code == 202, r.text
        assert set(r.json()) == {"id", "state"}
        tid = r.json()["id"]
        assert r.json()["state"] == "running"
        item = _wait_terminal(c, tid)
        assert item["state"] == "done", item
        assert item["kind"] == "fetch" and item["name"] == "rocky.iso"
        assert item["total"] == len(data) and item["received"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
        assert item["detected"] == {"os_type": "rocky", "os_version": "9.4"}
        assert item["finished_at"] > 0 and item["error"] == ""
    # 原子改名：目标文件在、.part 不在、内容逐字节一致
    assert (iso_env / "rocky.iso").read_bytes() == data
    assert not list(iso_env.glob(".*.part"))
    assert len(log) == 1 and "range" not in log[0].headers


def test_fetch_resume_with_range_206(iso_env, monkeypatch):
    """续传分支①：已有 .part + 服务器支持 Range（206）⇒ 只补断点之后的部分。"""
    data = _iso_bytes()
    have = 12345
    part = iso_env / ".rocky.iso.part"
    part.write_bytes(data[:have])
    log: list = []
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT",
                        _transport_log(data, log, respect_range=True))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/r.iso", "filename": "rocky.iso"})
        assert r.status_code == 202, r.text
        item = _wait_terminal(c, r.json()["id"])
        assert item["state"] == "done" and item["received"] == len(data)
        assert item["total"] == len(data)  # have + 206 的 Content-Length
    assert (iso_env / "rocky.iso").read_bytes() == data  # 少一段都对不上 ⇒ 证明是续传
    assert log and log[0].headers.get("range") == "bytes=%d-" % have


def test_fetch_resume_server_ignores_range_200(iso_env, monkeypatch):
    """续传分支②：服务器不支持 Range（回 200 全量）⇒ 丢弃旧 .part 从头下载。"""
    data = _iso_bytes()
    have = 5432
    (iso_env / ".rocky.iso.part").write_bytes(data[:have])
    log: list = []
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", _transport_log(data, log))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/r.iso", "filename": "rocky.iso"})
        assert r.status_code == 202, r.text
        item = _wait_terminal(c, r.json()["id"])
        assert item["state"] == "done"
    # 如果实现错误地"接着追加"，文件会变成 前缀+全量 ⇒ 这条断言必红
    assert (iso_env / "rocky.iso").read_bytes() == data
    assert log and log[0].headers.get("range") == "bytes=%d-" % have


def test_fetch_cancel_midway(iso_env, monkeypatch):
    """取消拉取：能打断卡住的流、删 .part、状态 canceled。"""
    data = _iso_bytes()
    # 把分块阈值改小：默认 8MiB 时 httpx 会把不足一块的数据缓冲到流结束，
    # 卡住的 100B 永远到不了 worker，进度/取消点就观察不到。
    monkeypatch.setattr(settings, "pxe_transfer_chunk", 64, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        async def gen():
            yield data[:100]
            await asyncio.sleep(30)  # 卡住，等 cancel 打断
            yield data[100:]
        return httpx.Response(200, headers={"content-length": str(len(data))},
                              content=gen())

    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/s.iso", "filename": "s.iso"})
        tid = r.json()["id"]
        deadline = time.time() + 5
        item = None
        while time.time() < deadline:
            item = _item(c, tid)
            if item and item["received"] >= 64:
                break
            time.sleep(0.02)
        assert item and item["received"] >= 64, item
        rc = c.post(ISO + "/transfers/%s/cancel" % tid)
        assert rc.status_code == 200 and rc.json() == {"ok": True}
        item = _wait_terminal(c, tid)
        assert item["state"] == "canceled" and item["error"] == "已取消"
        assert not list(iso_env.glob("*")), "取消后 ISO 目录应没有任何文件"


def test_fetch_source_404_is_400(iso_env, monkeypatch):
    log: list = []
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT",
                        _transport_log(b"x", log, status=404))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 400, r.text
        # 400 响应体只有 detail；注册表里的条目应记下同样的失败原因
        items = c.get(ISO + "/transfers").json()["items"]
        assert len(items) == 1 and items[0]["state"] == "failed"
        assert items[0]["error"] == r.json()["detail"]
        assert "HTTP 404" in r.json()["detail"]


def test_fetch_connection_error_is_400_chinese(iso_env, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused")
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 400, r.text
        assert r.json()["detail"].startswith("无法连接镜像源：")


def test_fetch_space_insufficient_400_with_numbers(iso_env, monkeypatch):
    """空间不足：400 文案必须给出具体数字（可用/需要/保留）。"""
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 100 * 1024**3, raising=False)
    log: list = []
    need = 10 * 1024**3
    # 只回头部（body 留空即可：准入在读到 Content-Length 时就该拒绝）
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-length": str(need)},
                                   content=b"")))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/big.iso",
                                         "filename": "big.iso"})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "磁盘可用" in detail and "本次需要" in detail and "扣除保留" in detail
        assert "后不足" in detail
        assert "10.0 GB" in detail and "100.0 GB" in detail
    assert not list(iso_env.glob("*"))  # .part 都不该建


def test_fetch_over_max_limit_400_with_numbers(iso_env, monkeypatch):
    monkeypatch.setattr(settings, "pxe_iso_max_bytes", 100 * 1024**2, raising=False)
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 0, raising=False)
    need = 200 * 1024**2
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(
        lambda req: httpx.Response(200, headers={"content-length": str(need)},
                                   content=b"")))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "超过单镜像上限" in detail
        assert "0.2 GB" in detail and "0.1 GB" in detail  # 200MiB / 100MiB
    assert not list(iso_env.glob("*"))


def test_fetch_unknown_length_total_zero(iso_env, monkeypatch):
    """Content-Length 拿不到：total=0，received 持续累加，成功收尾。"""
    data = _iso_bytes()
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 0, raising=False)

    def handler(request):
        async def gen():
            for i in range(0, len(data), 7):
                yield data[i:i + 7]
        return httpx.Response(200, content=gen())  # 无 Content-Length
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 202, r.text
        item = _wait_terminal(c, r.json()["id"])
        assert item["state"] == "done"
        assert item["total"] == 0 and item["received"] == len(data)
    assert (iso_env / "x.iso").read_bytes() == data


def test_fetch_unknown_length_incremental_cap_trips(iso_env, monkeypatch):
    """无 Content-Length 时边下边兜：超上限立即中断并删 .part。"""
    monkeypatch.setattr(settings, "pxe_iso_max_bytes", 100, raising=False)
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 0, raising=False)

    def handler(request):
        async def gen():
            for _ in range(3):
                yield b"x" * 40
        return httpx.Response(200, content=gen())
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 202, r.text
        item = _wait_terminal(c, r.json()["id"])
        assert item["state"] == "failed"
        assert "超过单镜像上限" in item["error"]
        assert item["received"] == 120  # 收满第 3 块才越界（40*3=120 > 100）
    assert not list(iso_env.glob(".*"))  # .part 已删


def test_fetch_sha256_mismatch_fails(iso_env, monkeypatch):
    data = _iso_bytes()
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", _transport_log(data, []))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso",
                                         "sha256": "0" * 64})
        assert r.status_code == 202, r.text
        item = _wait_terminal(c, r.json()["id"])
        assert item["state"] == "failed"
        assert "SHA256 校验失败" in item["error"]
    assert not list(iso_env.glob(".*")) and not (iso_env / "x.iso").exists()


def test_fetch_bad_magic_fails_chinese(iso_env, monkeypatch):
    junk = b"\x00" * 0x8800  # 无 CD001/UDF 魔数
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", _transport_log(junk, []))
    with _make_client() as c:
        r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": "x.iso"})
        assert r.status_code == 202, r.text
        item = _wait_terminal(c, r.json()["id"])
        assert item["state"] == "failed"
        assert item["error"] == ERR_NOT_ISO
    assert not list(iso_env.glob(".*"))


# ── 输入校验（文件名 / URL） ─────────────────────────────────────────────

def test_filename_validation_rejects(iso_env, monkeypatch):
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", _transport_log(b"x", []))
    cases = [
        ("../pwn.iso", "路径分隔符"),
        ("sub/dir.iso", "路径分隔符"),
        ("..\\pwn.iso", "路径分隔符"),
        ("not-iso.txt", ".iso 结尾"),
        ("", "不能为空"),
        ("空 格.iso", "只能包含"),
        ("镜像.iso", "只能包含"),
    ]
    with _make_client() as c:
        for name, needle in cases:
            r = c.post(ISO + "/fetch", json={"url": "http://m/x.iso", "filename": name})
            assert r.status_code == 400, (name, r.text)
            assert needle in r.json()["detail"], (name, r.json()["detail"])
            r2 = c.post(ISO + "/upload/init", json={"filename": name, "size": 1})
            assert r2.status_code == 400, (name, r2.text)
    assert not list(iso_env.glob("*"))


def test_url_scheme_rejects(iso_env, monkeypatch):
    with _make_client() as c:
        for url, needle in [("ftp://h/x.iso", "http 或 https"),
                            ("file:///etc/passwd.iso", "http 或 https"),
                            ("gopher://h/x.iso", "http 或 https"),
                            ("", "URL 不能为空")]:
            r = c.post(ISO + "/fetch", json={"url": url, "filename": "x.iso"})
            assert r.status_code == 400, (url, r.text)
            assert needle in r.json()["detail"], r.json()["detail"]
    assert not list(iso_env.glob("*"))


# ── B) 分块上传 ──────────────────────────────────────────────────────────

def _up_init(c, name="up.iso", size=0, sha256=""):
    body = {"filename": name, "size": size or 0x8800}
    if sha256:
        body["sha256"] = sha256
    r = c.post(ISO + "/upload/init", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def _up_chunk(c, tid, offset, payload):
    return c.post(ISO + "/upload/chunk",
                  data={"id": tid, "offset": str(offset)},
                  files={"chunk": ("chunk", payload, "application/octet-stream")})


def test_upload_init_chunk_finish_success(iso_env):
    data = _iso_bytes()
    with _make_client() as c:
        init = _up_init(c, "up.iso", len(data))
        assert set(init) == {"id", "received", "chunk_size"}
        assert init["received"] == 0
        assert init["chunk_size"] == settings.pxe_transfer_chunk
        tid = init["id"]
        # init 之后 transfers 里立即出现该条目（state=running）
        item = _item(c, tid)
        assert item and item["kind"] == "upload" and item["state"] == "running"
        assert item["name"] == "up.iso" and item["total"] == len(data)
        assert item["received"] == 0 and item["sha256"] == ""
        assert item["detected"] == {"os_type": "", "os_version": ""}
        r1 = _up_chunk(c, tid, 0, data[:1000])
        assert r1.status_code == 200 and r1.json() == {"received": 1000}
        assert _item(c, tid)["received"] == 1000
        r2 = _up_chunk(c, tid, 1000, data[1000:])
        assert r2.status_code == 200 and r2.json() == {"received": len(data)}
        fin = c.post(ISO + "/upload/finish", json={"id": tid})
        assert fin.status_code == 200, fin.text
        assert set(fin.json()) == {"ok", "name", "size", "detected"}
        assert fin.json()["ok"] is True
        assert fin.json()["name"] == "up.iso" and fin.json()["size"] == len(data)
        assert fin.json()["detected"] == {"os_type": "rocky", "os_version": "9.4"}
        item = _item(c, tid)
        assert item["state"] == "done" and item["received"] == len(data)
        assert item["sha256"] == hashlib.sha256(data).hexdigest()
    assert (iso_env / "up.iso").read_bytes() == data
    assert not list(iso_env.glob(".*"))


def test_upload_chunk_offset_mismatch_400_and_session_survives(iso_env):
    data = _iso_bytes()
    with _make_client() as c:
        tid = _up_init(c, "off.iso", len(data))["id"]
        assert _up_chunk(c, tid, 0, data[:5]).status_code == 200
        r = _up_chunk(c, tid, 7, data[7:10])  # 乱序：已收 5，却从 7 开始
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "偏移量" in detail and "顺序" in detail
        assert "5" in detail and "7" in detail  # 具体数字：已收 5 / 请求 7
        # 会话仍然可用：按正确 offset 续传
        ok = _up_chunk(c, tid, 5, data[5:10])
        assert ok.status_code == 200 and ok.json() == {"received": 10}
        # 超出声明大小的分块同样拒绝
        r3 = _up_chunk(c, tid, 10, b"z" * (len(data) - 10 + 1))
        assert r3.status_code == 400 and "超出声明大小" in r3.json()["detail"]


def test_upload_finish_size_mismatch_400(iso_env):
    with _make_client() as c:
        tid = _up_init(c, "size.iso", 1000)["id"]
        assert _up_chunk(c, tid, 0, b"a" * 10).status_code == 200
        r = c.post(ISO + "/upload/finish", json={"id": tid})
        assert r.status_code == 400, r.text
        detail = r.json()["detail"]
        assert "文件大小不一致" in detail
        assert "1000" in detail and "10" in detail  # 声明 1000 / 实收 10
        item = _item(c, tid)
        assert item["state"] == "failed" and item["error"] == detail
    assert not list(iso_env.glob(".*"))


def test_upload_finish_bad_magic_400(iso_env):
    junk = b"junk-not-an-iso" + b"\x00" * (0x8800 - 15)
    with _make_client() as c:
        tid = _up_init(c, "bad.iso", len(junk))["id"]
        assert _up_chunk(c, tid, 0, junk).status_code == 200
        r = c.post(ISO + "/upload/finish", json={"id": tid})
        assert r.status_code == 400, r.text
        assert r.json()["detail"] == ERR_NOT_ISO
        assert _item(c, tid)["state"] == "failed"
    assert not list(iso_env.glob(".*"))


def test_upload_sha256_mismatch_400(iso_env):
    data = _iso_bytes()
    with _make_client() as c:
        tid = _up_init(c, "s.iso", len(data), sha256="f" * 64)["id"]
        assert _up_chunk(c, tid, 0, data).status_code == 200
        r = c.post(ISO + "/upload/finish", json={"id": tid})
        assert r.status_code == 400, r.text
        assert "SHA256 校验失败" in r.json()["detail"]
    assert not list(iso_env.glob(".*"))


def test_upload_cancel_discards_session(iso_env):
    with _make_client() as c:
        tid = _up_init(c, "c.iso", 0x8800)["id"]
        assert _up_chunk(c, tid, 0, b"a" * 16).status_code == 200
        r = c.post(ISO + "/transfers/%s/cancel" % tid)
        assert r.status_code == 200 and r.json() == {"ok": True}
        item = _item(c, tid)
        assert item["state"] == "canceled"
        # 再传 → 会话已结束
        r2 = _up_chunk(c, tid, 16, b"b")
        assert r2.status_code == 400 and "已结束" in r2.json()["detail"]
        r3 = c.post(ISO + "/transfers/%s/cancel" % tid)  # 幂等
        assert r3.status_code == 200 and r3.json() == {"ok": True}
    assert not list(iso_env.glob("*"))


def test_upload_chunk_on_unknown_session_404(iso_env):
    with _make_client() as c:
        r = _up_chunk(c, "no-such-id", 0, b"x")
        assert r.status_code == 404, r.text
        assert "上传会话不存在" in r.json()["detail"]
        r2 = c.post(ISO + "/transfers/no-such-id/cancel")
        assert r2.status_code == 404 and "传输任务不存在" in r2.json()["detail"]


def test_stale_upload_session_swept(iso_env):
    with _make_client() as c:
        tid = _up_init(c, "stale.iso", 0x8800)["id"]
        assert _up_chunk(c, tid, 0, b"a" * 8).status_code == 200
        # 把活动时间回拨 31 分钟 → 触发清扫
        t = transfers._TRANSFERS[tid]
        t.last_activity = time.monotonic() - (31 * 60)
        assert transfers.sweep_stale_sessions() == 1
        item = _item(c, tid)
        assert item["state"] == "canceled"
        assert "无活动" in item["error"]
        assert not list(iso_env.glob(".*"))
        # 清扫后的会话不能再收块
        r = _up_chunk(c, tid, 8, b"b")
        assert r.status_code == 400 and "已结束" in r.json()["detail"]


# ── 并发上限 1 ───────────────────────────────────────────────────────────

def test_concurrent_transfer_409(iso_env, monkeypatch):
    data = _iso_bytes()
    # 与 cancel 用例同理：改小分块阈值，让卡住前的第一块能流到 worker
    monkeypatch.setattr(settings, "pxe_transfer_chunk", 64, raising=False)

    def handler(request):
        async def gen():
            yield data[:100]
            await asyncio.sleep(30)
            yield data[100:]
        return httpx.Response(200, headers={"content-length": str(len(data))},
                              content=gen())
    monkeypatch.setattr(transfers, "_TEST_TRANSPORT", httpx.MockTransport(handler))
    with _make_client() as c:
        r1 = c.post(ISO + "/fetch", json={"url": "http://m/a.iso", "filename": "a.iso"})
        assert r1.status_code == 202, r1.text
        tid = r1.json()["id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            if (_item(c, tid) or {}).get("received", 0) >= 64:
                break
            time.sleep(0.02)
        # 拉取进行中：再来一次 fetch → 409（文案逐字按契约）
        r2 = c.post(ISO + "/fetch", json={"url": "http://m/b.iso", "filename": "b.iso"})
        assert r2.status_code == 409, r2.text
        assert r2.json()["detail"] == ERR_BUSY
        # 拉取进行中：上传会话也开不出来（并发上限是全局的）
        r3 = c.post(ISO + "/upload/init", json={"filename": "u.iso", "size": 0x8800})
        assert r3.status_code == 409 and r3.json()["detail"] == ERR_BUSY
        # 收尾：取消，别把卡住的任务留给下一个用例
        c.post(ISO + "/transfers/%s/cancel" % tid)
        assert _wait_terminal(c, tid)["state"] == "canceled"
        data_out = c.get(ISO + "/transfers").json()
        assert data_out["busy"] is False and data_out["concurrency"] == 1


# ── /space 与 /transfers 形状（契约字段逐项核对） ────────────────────────

def test_space_shape(iso_env, monkeypatch):
    monkeypatch.setattr(settings, "pxe_iso_max_bytes", 123 * 1024**2, raising=False)
    monkeypatch.setattr(settings, "pxe_iso_reserve_bytes", 2 * 1024**3, raising=False)
    with _make_client() as c:
        r = c.get(ISO + "/space")
        assert r.status_code == 200, r.text
        body = r.json()
        assert set(body) == {"dir", "total", "free", "used", "reserve", "max_upload"}
        assert body["dir"] == str(iso_env)
        for k in ("total", "free", "used", "reserve", "max_upload"):
            assert isinstance(body[k], int), (k, body[k])
        assert body["reserve"] == 2 * 1024**3
        assert body["max_upload"] == min(123 * 1024**2, int(body["free"] * 0.6))
        assert body["total"] >= body["used"] >= 0


def test_transfers_shape(iso_env):
    with _make_client() as c:
        r = c.get(ISO + "/transfers")
        assert r.status_code == 200
        assert r.json() == {"items": [], "concurrency": 1, "busy": False}
        # init 之后：条目字段与契约逐一对应
        tid = _up_init(c, "shape.iso", 0x8800)["id"]
        body = c.get(ISO + "/transfers").json()
        assert body["busy"] is True and body["concurrency"] == 1
        assert len(body["items"]) == 1
        item = body["items"][0]
        assert set(item) == {"id", "kind", "name", "total", "received", "state",
                             "error", "started_at", "finished_at", "sha256",
                             "detected"}
        assert item["id"] == tid and item["kind"] == "upload"
        assert item["state"] == "running" and item["received"] == 0
        assert item["total"] == 0x8800 and item["name"] == "shape.iso"
        assert item["started_at"] > 0 and item["finished_at"] == 0.0
        assert item["error"] == ""
        assert item["detected"] == {"os_type": "", "os_version": ""}


# ── 权限与配置 ───────────────────────────────────────────────────────────

def test_transfer_endpoints_require_admin(iso_env):
    with _make_client(role="viewer") as c:
        for method, url, kw in [
            ("get", ISO + "/space", {}),
            ("get", ISO + "/transfers", {}),
            ("post", ISO + "/fetch", {"json": {"url": "http://m/x.iso",
                                               "filename": "x.iso"}}),
            ("post", ISO + "/upload/init", {"json": {"filename": "x.iso",
                                                     "size": 1}}),
            ("post", ISO + "/upload/chunk",
             {"data": {"id": "x", "offset": "0"},
              "files": {"chunk": ("chunk", b"x", "application/octet-stream")}}),
            ("post", ISO + "/upload/finish", {"json": {"id": "x"}}),
            ("post", ISO + "/transfers/x/cancel", {}),
        ]:
            r = getattr(c, method)(url, **kw)
            assert r.status_code == 403, (url, r.status_code, r.text)
            assert "权限" in r.json()["detail"]


def test_settings_defaults():
    s = Settings(_env_file=None)  # 不读 .env，验证代码里的默认值
    assert s.pxe_iso_max_bytes == 16 * 1024**3
    assert s.pxe_iso_reserve_bytes == 2 * 1024**3
    assert s.pxe_transfer_chunk == 8 * 1024 * 1024


# ── 识别规则单元用例（规格修订版：分隔符归一化） ─────────────────────────

def test_detect_os_from_label_four_real_shapes():
    f = transfers.detect_os_from_label
    assert f("Rocky-9-4-x86_64-dvd") == {"os_type": "rocky", "os_version": "9.4"}
    assert f("CENTOS_7_X86_64") == {"os_type": "centos", "os_version": "7"}
    assert f("UBUNTU_22.04.5_LIVE_SERVER") == {"os_type": "ubuntu",
                                               "os_version": "22.04.5"}
    assert f("SomeRandomLabel") == {"os_type": "", "os_version": ""}


def test_detect_os_more_shapes():
    f = transfers.detect_os_from_label
    assert f("RHEL-9.4-x86_64-dvd") == {"os_type": "rhel", "os_version": "9.4"}
    assert f("AlmaLinux-9-latest") == {"os_type": "almalinux", "os_version": "9"}
    # 只认出类型、认不出版本 ⇒ 两者都留空（契约：任一为空就都空）
    assert f("ubuntu-live-server") == {"os_type": "", "os_version": ""}
    assert f("Rocky-9-4-x86_64-dvd ") == {"os_type": "rocky", "os_version": "9.4"}
    assert f("") == {"os_type": "", "os_version": ""}


def test_check_iso_magic_branches(tmp_path):
    p = tmp_path / "m.iso"
    for magic, ok in [(b"CD001", True), (b"BEA01", True), (b"TEA01", True),
                      (b"NSR02", True), (b"NSR03", True),
                      (b"XXXXX", False), (b"CD00", False)]:
        buf = bytearray(0x9000)
        buf[0x8001:0x8001 + len(magic)] = magic
        p.write_bytes(bytes(buf))
        assert transfers.check_iso_magic(str(p)) is ok, magic
    p.write_bytes(b"tiny")  # 小于卷描述符偏移 ⇒ 一律不通过
    assert transfers.check_iso_magic(str(p)) is False


def test_read_volume_label_from_file(tmp_path):
    data = _iso_bytes(label=b"CENTOS_7_X86_64")
    p = tmp_path / "c.iso"
    p.write_bytes(data)
    assert transfers.read_volume_label(str(p)) == "CENTOS_7_X86_64"
    # 卷标识别接入 detect：整文件 → (os_type, os_version)
    assert transfers.detect_os_from_file(str(p)) == {"os_type": "centos",
                                                     "os_version": "7"}
