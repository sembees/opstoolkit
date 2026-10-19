# -*- coding: utf-8 -*-
"""仪表盘汇总 + 认证的 **HTTP 层**用例（审计 P1 补缺）。

审计统计：`/api/dashboard` 前缀命中 0/1；`/api/auth/login`、`/api/auth/me` 的 HTTP 层
也是 0（此前只测了 JWT 原语的 encode/decode，没有一条用例真的 POST 过 /login）。

这两块都是"登录后第一屏"依赖的东西：
  · 登录接口挂了 = 整个平台进不去；
  · 仪表盘挂了 = 首页白屏。
所以必须有机械回归，且要覆盖失败路径（密码错、账号不存在、token 无效、越权）。

隔离：临时 sqlite + 真实 `get_current_user`（用真 token 走依赖链，不做 override），
只有 `/dashboard` 用 override 简化（它只关心数据聚合）。
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


def _mk(tmp_path, name="t.db", include_auth=True):
    """建 App：alerts/dashboard/auth 三件套 + 临时库。"""
    from app.api import auth as auth_api
    from app.api import dashboard as dash_api
    from app.core.auth import get_current_user, hash_password
    from app.database import get_db

    db_path = tmp_path / name
    engine = create_async_engine("sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(_create())

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(dash_api.router, prefix="/api")
    app.include_router(auth_api.router, prefix="/api/auth")

    async def _seed_user():
        async with Session() as s:
            s.add(models.User(id="u1", username="admin", display_name="管理员",
                              role="admin", hashed_password=hash_password("Secret123!")))
            await s.commit()

    asyncio.run(_seed_user())
    app.dependency_overrides[get_db] = _db
    return TestClient(app), Session, app


# ── 认证：HTTP 层 ────────────────────────────────────────────────────────────

def test_login_returns_token_and_identity(tmp_path):
    c, _, _ = _mk(tmp_path)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "Secret123!"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"]
    assert body["display_name"] == "管理员"
    assert body["role"] == "admin"


def test_login_wrong_password_is_401(tmp_path):
    c, _, _ = _mk(tmp_path)
    r = c.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401
    assert "用户名或密码错误" in r.json()["detail"]


def test_login_unknown_user_is_401_not_500(tmp_path):
    """不存在的账号必须 401（且不能因为 None.hashed_password 抛 500）。"""
    c, _, _ = _mk(tmp_path)
    r = c.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert r.status_code == 401


def test_me_with_real_token(tmp_path):
    """/me 用**真 token 走真依赖链**（不 override），验证 Bearer 解析 + 载荷。"""
    c, _, _ = _mk(tmp_path)
    tok = c.post("/api/auth/login",
                 json={"username": "admin", "password": "Secret123!"}).json()["access_token"]
    r = c.get("/api/auth/me", headers={"Authorization": "Bearer " + tok})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["username"] == "admin"
    assert body["role"] == "admin"


def test_me_without_token_is_401_or_403(tmp_path):
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/auth/me")
    assert r.status_code in (401, 403), r.status_code


def test_me_with_garbage_token_is_401_or_403(tmp_path):
    c, _, _ = _mk(tmp_path)
    r = c.get("/api/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code in (401, 403), r.status_code


# ── 仪表盘：数据聚合 ────────────────────────────────────────────────────────

def test_dashboard_aggregates(tmp_path):
    """资产按 category 分组、最近任务 5 条、模板计数、安装记录 5 条。"""
    c, Session, app = _mk(tmp_path)

    from app.core.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}

    async def _seed():
        async with Session() as s:
            s.add_all([
                models.Asset(id="a1", name="sw1", category="ct", host="10.0.0.1"),
                models.Asset(id="a2", name="sw2", category="ct", host="10.0.0.2"),
                models.Asset(id="a3", name="srv1", category="server", host="10.0.0.3"),
                models.InspectionTask(id="t1", name="巡检1", kind="ct", status="done",
                                      asset_ids=["a1", "a2"]),
                models.InspectionResult(id="r1", task_id="t1", asset_id="a1",
                                        asset_name="sw1", status="ok"),
                models.InspectionResult(id="r2", task_id="t1", asset_id="a2",
                                        asset_name="sw2", status="failed", error="x"),
                models.InspectionTemplate(id="tp1", name="模板1", items=[], is_system=False),
                models.PxeInstall(id="p1", profile_id="prof1", hostname="h1",
                                  mac="bc:24:11:00:00:01", status="pending"),
            ])
            await s.commit()

    asyncio.run(_seed())

    r = c.get("/api/dashboard")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["assets"] == {"ct": 2, "server": 1}
    assert body["template_count"] == 1
    assert [t["name"] for t in body["recent_tasks"]] == ["巡检1"]
    assert body["recent_tasks"][0]["asset_count"] == 2      # len(asset_ids)
    assert body["recent_tasks"][0]["result_count"] == 2     # 结果条数
    assert [p["hostname"] for p in body["recent_installs"]] == ["h1"]


def test_dashboard_empty_db_is_all_zero_not_error(tmp_path):
    """空库（新装）必须返回全零结构，不能 500 —— 否则登录后首页白屏。"""
    c, _, app = _mk(tmp_path)
    from app.core.auth import get_current_user
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    r = c.get("/api/dashboard")
    assert r.status_code == 200
    body = r.json()
    assert body["assets"] == {}
    assert body["recent_tasks"] == []
    assert body["recent_installs"] == []
    assert body["template_count"] == 0
