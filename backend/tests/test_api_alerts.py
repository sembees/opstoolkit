# -*- coding: utf-8 -*-
"""告警规则 CRUD + 告警记录的 **HTTP 层**用例（审计 P1 补缺）。

为什么补这一组：`/api/alerts/*` 共 5 个端点，此前**一次都没在测试里出现过**
（审计统计：前缀命中 0/5）。规则误删/改错在运维里代价很高，HTTP 层的状态码、
"空名字要 400"、"404 要报不存在"这些边界必须有机械回归。

隔离：临时 sqlite（aiosqlite + NullPool），不碰生产库、不联网。
"""
import pathlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402


def _client(tmp_path, role="admin"):
    from app.api import alerts as alerts_api
    from app.core.auth import get_current_user
    from app.database import get_db

    db_path = tmp_path / "alerts.db"
    engine = create_async_engine("sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    import asyncio
    asyncio.run(_create())

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(alerts_api.router, prefix="/api/alerts")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "admin", "display_name": "管理员", "role": role}
    return TestClient(app), Session


def test_create_list_update_delete_rule(tmp_path):
    """完整 CRUD 一圈：建 → 列 → 改 → 删 → 再列应为空。"""
    c, _ = _client(tmp_path)

    r = c.post("/api/alerts/rules", json={"name": "CPU 高", "metric_key": "cpu",
                                          "operator": "gt", "threshold": 90})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    assert r.json()["threshold"] == 90.0        # 入参是 int，落库是 float
    assert r.json()["enabled"] is True          # 缺省启用

    r = c.get("/api/alerts/rules")
    assert r.status_code == 200
    assert [x["name"] for x in r.json()] == ["CPU 高"]

    r = c.put("/api/alerts/rules/" + rid, json={"threshold": 75.5, "enabled": False})
    assert r.status_code == 200 and r.json() == {"ok": True}
    row = c.get("/api/alerts/rules").json()[0]
    assert row["threshold"] == 75.5 and row["enabled"] is False
    assert row["name"] == "CPU 高"              # 没传的字段不能被清空

    r = c.delete("/api/alerts/rules/" + rid)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert c.get("/api/alerts/rules").json() == []


def test_empty_name_or_metric_is_400(tmp_path):
    """名字/指标为空必须 400（不是 500，也不是静默建一条空规则）。"""
    c, _ = _client(tmp_path)
    for body in ({"name": "  ", "metric_key": "cpu"},
                 {"name": "x", "metric_key": ""},
                 {"metric_key": "cpu"}):
        r = c.post("/api/alerts/rules", json=body)
        assert r.status_code == 400, (body, r.status_code, r.text)
        assert "不能为空" in r.json()["detail"]


def test_update_missing_rule_is_404(tmp_path):
    """改不存在的规则要 404 —— 不能回 ok:true 让运维以为改上了。"""
    c, _ = _client(tmp_path)
    r = c.put("/api/alerts/rules/does-not-exist", json={"enabled": False})
    assert r.status_code == 404
    assert "不存在" in r.json()["detail"]


def test_delete_missing_rule_is_idempotent(tmp_path):
    """删不存在的规则返回 ok（幂等）—— 删两次不该报错。"""
    c, _ = _client(tmp_path)
    assert c.delete("/api/alerts/rules/nope").status_code == 200
    assert c.delete("/api/alerts/rules/nope").status_code == 200


def test_list_is_newest_first(tmp_path):
    """列表按创建时间倒序（前端"最近规则"依赖这个顺序）。"""
    c, _ = _client(tmp_path)
    ids = []
    for n in ("第一条", "第二条", "第三条"):
        ids.append(c.post("/api/alerts/rules", json={"name": n, "metric_key": "m"}).json()["id"])
    names = [x["name"] for x in c.get("/api/alerts/rules").json()]
    assert names == ["第三条", "第二条", "第一条"], names


def test_history_returns_failed_inspections_only(tmp_path):
    """告警记录只取巡检失败（status=failed）的记录，且 limit 生效。"""
    import asyncio
    c, Session = _client(tmp_path)

    async def _seed():
        async with Session() as s:
            s.add_all([
                models.InspectionResult(id="r1", task_id="t1", asset_id="a1", asset_name="sw1",
                                        status="failed", error="CPU 95%"),
                models.InspectionResult(id="r2", task_id="t1", asset_id="a2", asset_name="sw2",
                                        status="ok", error=""),
                models.InspectionResult(id="r3", task_id="t1", asset_id="a3", asset_name="sw3",
                                        status="failed", error="端口 down"),
            ])
            await s.commit()

    asyncio.run(_seed())

    r = c.get("/api/alerts/history")
    assert r.status_code == 200
    body = r.json()
    assert [x["asset_name"] for x in body] == ["sw1", "sw3"] or \
           sorted(x["asset_name"] for x in body) == ["sw1", "sw3"]
    assert all(x["error"] for x in body)

    r = c.get("/api/alerts/history?limit=1")
    assert len(r.json()) == 1


def test_history_limit_upper_bound_is_validated(tmp_path):
    """limit 超过 100 应被参数校验拦下（FastAPI 422），不能把整表拉出来。"""
    c, _ = _client(tmp_path)
    assert c.get("/api/alerts/history?limit=101").status_code == 422
    assert c.get("/api/alerts/history?limit=100").status_code == 200
