# -*- coding: utf-8 -*-
"""PXE 的 ISO / 仓库 / 站点策略 + ZTP 服务端点：**HTTP 层**用例（审计 P1 补缺）。

审计统计里这些前缀在测试源码中一次都没出现过：
  /api/it/pxe/iso/{list,space,fetch}、/api/it/pxe/repo/{status,sync}、
  /api/it/pxe/site-policy、/api/ct/ztp/server/{status,service}

写法：把**真正碰宿主**的底层函数 monkeypatch 掉（列表/空间/下载/仓库核查/服务控制），
这样用例只验证"HTTP 层的鉴权、参数校验、状态码、载荷透传"，绝不去碰 /srv/tftp、
/srv/opstk 或宿主机 dnsmasq（容器里也没有 systemctl）。

site-policy 走**真库**（AppMeta 表），因为它的价值就在"写进去 → 读出来 → 生效"这条链。
"""
import asyncio
import pathlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402


def _mk(tmp_path, role="admin"):
    """把 pxe / ztp 两个 router 挂到一个临时 App 上（临时 sqlite）。"""
    from app.api import pxe as pxe_api
    from app.api import ztp as ztp_api
    from app.core.auth import get_current_user, require_role
    from app.database import get_db

    db_path = tmp_path / "surface.db"
    engine = create_async_engine("sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(_create())

    async def _db():
        async with Session() as s:
            yield s

    user = {"id": "u1", "username": "admin", "display_name": "管理员", "role": role}
    app = FastAPI()
    app.include_router(pxe_api.router, prefix="/api/it/pxe")
    app.include_router(ztp_api.router, prefix="/api/ct/ztp")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: user
    # require_role 是个返回依赖的工厂，逐个角色重写：不能只 override get_current_user
    app.dependency_overrides[require_role("admin")] = lambda: user
    return TestClient(app), Session, app


@pytest.fixture(autouse=True)
def _restore_dhcp_policy_cache(monkeypatch):
    """保护邻居用例：`dhcp._SITE_POLICY_CACHE` 是**模块级缓存**，site-policy 的 PUT 会改它。

    真改了而不还原，后续 `test_dhcp_site_protection.py` 会以为站点策略被改过而集体变红
    （第一次跑完整套时就是这么红的 6 条）。
    monkeypatch.setattr 会在用例结束时把原值放回去，所以这里"设成原值"就是在打快照。
    """
    from app.core import dhcp
    monkeypatch.setattr(dhcp, "_SITE_POLICY_CACHE", dhcp._SITE_POLICY_CACHE, raising=False)
    yield


# ── ISO ─────────────────────────────────────────────────────────────────────

def test_iso_list_passes_through(tmp_path, monkeypatch):
    from app.api import pxe as pxe_api
    monkeypatch.setattr(pxe_api.pxe_server, "list_isos",
                        lambda: {"supported": True,
                                 "isos": [{"name": "a.iso", "size": 1, "size_mb": 0.0}]})
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/it/pxe/iso/list")
    assert r.status_code == 200, r.text
    assert r.json()["isos"][0]["name"] == "a.iso"


def test_iso_list_unsupported_host(tmp_path, monkeypatch):
    """非 Linux 宿主（或没有目录）时如实返回 supported=False，不能 500。"""
    from app.api import pxe as pxe_api
    monkeypatch.setattr(pxe_api.pxe_server, "list_isos", lambda: {"supported": False, "isos": []})
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/it/pxe/iso/list")
    assert r.status_code == 200 and r.json()["supported"] is False


def test_iso_space_requires_admin(tmp_path, monkeypatch):
    """磁盘水位（暴露路径与容量）只给 admin。"""
    from app.api import pxe as pxe_api
    monkeypatch.setattr(pxe_api.iso_transfers, "space",
                        lambda: {"dir": "/srv/opstk/iso", "total": 1, "used": 0, "free": 1,
                                 "max_upload": 1})
    c, _, _ = _mk(tmp_path, role="viewer")
    assert c.get("/api/it/pxe/iso/space").status_code in (401, 403)

    c2, _, _ = _mk(tmp_path, role="admin")
    r = c2.get("/api/it/pxe/iso/space")
    assert r.status_code == 200 and r.json()["dir"] == "/srv/opstk/iso"


def test_iso_fetch_returns_202_and_passes_params(tmp_path, monkeypatch):
    """按 URL 拉取是后台任务：必须 202，且把 url/filename/sha256 原样交给底层。"""
    from app.api import pxe as pxe_api
    seen = {}

    async def _fake_start(url, filename, sha256=""):
        seen.update(url=url, filename=filename, sha256=sha256)
        return {"id": "t1", "status": "starting"}

    monkeypatch.setattr(pxe_api.iso_transfers, "start_fetch", _fake_start)
    c, _, _ = _mk(tmp_path)
    r = c.post("/api/it/pxe/iso/fetch",
               json={"url": "http://mirror/x.iso", "filename": "x.iso", "sha256": "ab" * 32})
    assert r.status_code == 202, r.text
    assert r.json()["id"] == "t1"
    assert seen == {"url": "http://mirror/x.iso", "filename": "x.iso", "sha256": "ab" * 32}


def test_iso_fetch_error_mapping_table(tmp_path, monkeypatch):
    """把 `_iso_transfer_http_error` 的映射表逐条钉住（写错一个就是运维看到 500）。

    这是读代码时确认过的契约：
      BusyError   → 409（已有下载在跑）
      NotFoundError → 404
      TransferError → 400（参数/协议/文件名非法；它是 ValueError 的子类）
      其它意外     → 500，但**只暴露异常类型名**，不把栈/路径丢给前端
    """
    from app.api import pxe as pxe_api

    cases = [
        (pxe_api.iso_transfers.BusyError("已有下载"), 409),
        (pxe_api.iso_transfers.NotFoundError("没有这个传输"), 404),
        (pxe_api.iso_transfers.TransferError("URL 协议必须是 http 或 https"), 400),
        (ValueError("不是 http(s) URL"), 500),      # 普通 ValueError 不在契约里 → 兜底
    ]
    for exc, want in cases:
        async def _boom(url, filename, sha256="", _e=exc):
            raise _e

        monkeypatch.setattr(pxe_api.iso_transfers, "start_fetch", _boom)
        c, _, _ = _mk(tmp_path)
        r = c.post("/api/it/pxe/iso/fetch", json={"url": "ftp://x"})
        assert r.status_code == want, (type(exc).__name__, r.status_code, r.text)
        # 兜底分支不许把内部细节漏出去
        if want == 500:
            assert "ISO 传输内部错误" in r.json()["detail"]
            assert "Traceback" not in r.text


# ── 仓库 ────────────────────────────────────────────────────────────────────

def test_repo_status_aggregates_each_repo(tmp_path, monkeypatch):
    from app.api import pxe as pxe_api
    monkeypatch.setattr(pxe_api.repo_verify, "list_repos", lambda: [
        {"name": "jammmy", "path": "/nonexistent/a", "size_bytes": 10, "suites": ["main"]},
    ])
    monkeypatch.setattr(pxe_api.repo_verify, "verify_repo", lambda p: {
        "verdict": "warn", "reason": "未签名", "suites": {"main": {"checked": 1}}})
    monkeypatch.setattr(pxe_api.repo_verify, "REPO_ROOT", "/srv/opstk/pxe-web/repo")
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/it/pxe/repo/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["root"].endswith("/repo")
    assert body["repos"][0]["verdict"] == "warn"
    assert body["repos"][0]["reason"] == "未签名"


def test_repo_sync_start_is_202_and_admin_only(tmp_path, monkeypatch):
    from app.api import pxe as pxe_api
    monkeypatch.setattr(pxe_api.repo_sync, "start_sync",
                        lambda body: {"suite": "bookworm", "dest": "/tmp/r",
                                      "mirror": "m", "dry_run": True})
    c, _, _ = _mk(tmp_path, role="viewer")
    assert c.post("/api/it/pxe/repo/sync", json={}).status_code in (401, 403)

    c2, _, _ = _mk(tmp_path, role="admin")
    r = c2.post("/api/it/pxe/repo/sync", json={})
    assert r.status_code == 200 and r.json()["accepted"] is True
    assert r.json()["request"]["suite"] == "bookworm"


def test_repo_sync_bad_body_is_422(tmp_path, monkeypatch):
    """底层 ValueError（例如 suite 名非法）要 422，而不是 500。"""
    from app.api import pxe as pxe_api

    def _bad(body):
        raise ValueError("suite 只能是 bookworm/trixie")

    monkeypatch.setattr(pxe_api.repo_sync, "start_sync", _bad)
    c, _, _ = _mk(tmp_path)
    r = c.post("/api/it/pxe/repo/sync", json={"suite": "../etc"})
    assert r.status_code == 422
    assert "bookworm" in r.json()["detail"]


def test_repo_sync_status_passes_through(tmp_path, monkeypatch):
    from app.api import pxe as pxe_api
    monkeypatch.setattr(pxe_api.repo_sync, "sync_status",
                        lambda: {"state": "idle", "log_tail": []})
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/it/pxe/repo/sync")
    assert r.status_code == 200 and r.json()["state"] == "idle"


# ── 站点策略（真库往返）────────────────────────────────────────────────────

def test_site_policy_roundtrip_and_validation(tmp_path):
    """写进 AppMeta → 读出来一致；坏 CIDR 必须 422（不能静默存下去）。"""
    from app.core import dhcp
    c, _, _ = _mk(tmp_path)

    r = c.get("/api/it/pxe/site-policy")
    assert r.status_code == 200
    assert r.json()["source"] in ("env", "default", "db")   # 未配置时的来源标注

    r = c.put("/api/it/pxe/site-policy",
              json={"protected_networks": "192.168.250.0/24,172.31.0.0/16",
                    "protect_default_route": True})
    assert r.status_code == 200, r.text

    body = c.get("/api/it/pxe/site-policy").json()
    assert "192.168.250.0/24" in body["protected_networks"]
    assert body["protect_default_route"] is True

    # 关键：写下去的策略必须**真的进了 dhcp 层的内存缓存**（否则只是存了个寂寞）
    cache = dhcp._SITE_POLICY_CACHE or {}
    assert any("192.168.250.0/24" in str(n) for n in cache.get("protected_networks", [])), cache

    r = c.put("/api/it/pxe/site-policy", json={"protected_networks": "10.0.0.0/33"})
    assert r.status_code == 422, r.text


def test_site_policy_put_requires_admin(tmp_path):
    c, _, _ = _mk(tmp_path, role="viewer")
    assert c.put("/api/it/pxe/site-policy",
                 json={"protected_networks": "192.168.250.0/24"}).status_code in (401, 403)


# ── ZTP 服务端点 ────────────────────────────────────────────────────────────

def test_ztp_server_status_merges_serve_token_state(tmp_path, monkeypatch):
    from app.api import ztp as ztp_api
    monkeypatch.setattr(ztp_api.ztp_server, "server_status",
                        lambda: {"tftp_root": "/srv/tftp", "dnsmasq": "running"})
    monkeypatch.setattr(ztp_api.serve_token, "states",
                        lambda: {"pxe": {"enabled": False}, "ztp": {"enabled": False}})
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/ct/ztp/server/status")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dnsmasq"] == "running"
    assert body["serve_token"]["ztp"]["enabled"] is False   # J 的状态被如实合并进来


def test_ztp_service_control_passes_action_and_is_admin_only(tmp_path, monkeypatch):
    from app.api import ztp as ztp_api
    seen = {}
    monkeypatch.setattr(ztp_api.ztp_server, "service_control",
                        lambda action: seen.setdefault("action", action) or {"ok": True})

    c, _, _ = _mk(tmp_path, role="viewer")
    assert c.post("/api/ct/ztp/server/service", json={"action": "status"}).status_code in (401, 403)

    c2, _, _ = _mk(tmp_path, role="admin")
    r = c2.post("/api/ct/ztp/server/service", json={"action": "restart"})
    assert r.status_code == 200, r.text
    assert seen["action"] == "restart"


def test_ztp_service_control_defaults_to_status(tmp_path, monkeypatch):
    """不传 body / 不传 action 时默认 status（只读），不会误重启服务。"""
    from app.api import ztp as ztp_api
    seen = {}
    monkeypatch.setattr(ztp_api.ztp_server, "service_control",
                        lambda action: seen.setdefault("action", action) or {"ok": True})
    c, _, _ = _mk(tmp_path)
    assert c.post("/api/ct/ztp/server/service", json={}).status_code == 200
    assert seen["action"] == "status"
