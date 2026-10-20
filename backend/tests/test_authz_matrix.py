# -*- coding: utf-8 -*-
"""**越权**用例：角色矩阵（审计里"越权测试"那块空白）。

为什么要单独一组：`require_role` 存在的理由本身就是一次真实的越权风险 ——
任何登录用户都能 `POST /it/pxe/server/service {"action":"stop"}` 把 dnsmasq 停掉，
而它是 IT PXE 与 CT ZTP **共用**的 DHCP/TFTP，停掉等于整个装机网段失联。

这组用例只做两件事：
  · viewer 调 admin-only 端点 → **403**（而且是在产生副作用之前就被拦）；
  · admin 调同样的端点 → **不是 403**（证明拦截不是"全都拒"）。

刻意避开会真写盘/真重启服务的端点（例如 deploy、iso 删除）——那些只验 viewer 403，
不验 admin 通过，避免测试本身去动宿主。
"""
import pathlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import asyncio

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_dhcp_policy_cache(monkeypatch):
    """保护邻居用例：`dhcp._SITE_POLICY_CACHE` 是**模块级缓存**，本文件会 PUT site-policy。

    不还原的话，后续 `test_dhcp_site_protection.py` 会以为站点策略被改过而集体变红
    —— 这个坑本项目已经踩过一次（那次红了 6 条），所以这里照 `test_api_pxe_ztp_surface.py`
    的做法打快照：monkeypatch 在 teardown 时把原值放回去。
    """
    from app.core import dhcp
    monkeypatch.setattr(dhcp, "_SITE_POLICY_CACHE", dhcp._SITE_POLICY_CACHE, raising=False)
    yield



def _client(tmp_path, role):
    """只挂 pxe + ztp 路由；用 dependency_overrides 直接给角色（不真登录）。"""
    from app.api import pxe as pxe_api, ztp as ztp_api
    from app.core.auth import get_current_user
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "authz.db").as_posix(),
                                 poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(_create())

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(pxe_api.router, prefix="/api/it/pxe")
    app.include_router(ztp_api.router, prefix="/api/ct/ztp")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "someone", "display_name": "某人", "role": role}
    return TestClient(app), Session


# (method, path, json) —— 全部是 require_role("admin") 的端点
ADMIN_CALLS = [
    ("get", "/api/it/pxe/iso/space", None),
    ("get", "/api/it/pxe/iso/transfers", None),
    ("put", "/api/it/pxe/site-policy", {"protected_networks": [], "allow_any_interface": False}),
    ("post", "/api/it/pxe/repo/sync", {}),
    ("post", "/api/it/pxe/profiles/nope/deploy", {}),
    ("post", "/api/it/pxe/installs/nope/finish", None),
    ("delete", "/api/it/pxe/iso/nope.iso", None),
    ("post", "/api/ct/ztp/server/service", {"action": "status"}),
]


def _call(c, method, path, body):
    fn = getattr(c, method)
    if body is not None:
        return fn(path, json=body)
    return fn(path)


def test_viewer_is_denied_everywhere(tmp_path):
    """viewer 对 8 个 admin-only 端点必须**全部 403**（不是 401、不是 500）。"""
    c, _ = _client(tmp_path, "viewer")
    bad = []
    for method, path, body in ADMIN_CALLS:
        st = _call(c, method, path, body).status_code
        if st != 403:
            bad.append((method, path, st))
    assert not bad, "这些端点没拦住 viewer: %r" % (bad,)


def test_admin_is_not_denied_on_safe_endpoints(tmp_path):
    """admin 调"只读/只写库"的端点不该是 403（否则等于把功能全锁死）。

    只挑不碰宿主的：iso/space、iso/transfers、site-policy(PUT)。
    """
    c, _ = _client(tmp_path, "admin")
    for method, path, body in [
        ("get", "/api/it/pxe/iso/space", None),
        ("get", "/api/it/pxe/iso/transfers", None),
        ("put", "/api/it/pxe/site-policy", {"protected_networks": [], "allow_any_interface": False}),
    ]:
        st = _call(c, method, path, body).status_code
        assert st != 403, (path, st)


def test_unknown_role_is_denied(tmp_path):
    """角色拼错/为空时按"没有权限"处理，不能因为匹配不上就放行。"""
    c, _ = _client(tmp_path, "")
    for method, path, body in ADMIN_CALLS[:3]:
        assert _call(c, method, path, body).status_code == 403, path


def test_non_admin_read_endpoints_still_work(tmp_path):
    """越权收紧要精确：普通登录用户该能读的（模板列表）不能被一起锁掉。"""
    c, _ = _client(tmp_path, "viewer")
    assert c.get("/api/it/pxe/profiles").status_code == 200
    assert c.get("/api/it/pxe/os-catalog").status_code == 200
