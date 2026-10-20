# -*- coding: utf-8 -*-
"""`GET /api/ct/inspection/results/compare` 的 HTTP 层用例（补齐 compare.py 的空白）。

这端点服务界面的"与上次对比"，只有 100 行左右的纯计算逻辑，但此前**一条用例都没有**。
它的失效方式很隐蔽：不报错、数字却算错（比如把 0 当成"没有数值"），运维照着错的
趋势判断"容量还够"。所以这里把数值/趋势/非数值/记录不足/参数校验逐条钉住。

隔离：临时 sqlite，不碰生产库、不联网。
"""
import pathlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import asyncio
import datetime as _dt

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402

URL = "/api/ct/inspection/results/compare"


def _app(tmp_path, auth=True):
    from app.api import compare as cmp_api
    from app.core.auth import get_current_user
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "c.db").as_posix(),
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
    app.include_router(cmp_api.router, prefix="/api/ct/inspection")
    app.dependency_overrides[get_db] = _db
    if auth:
        app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "username": "admin"}
    return app, Session


def _add(Session, asset_id, metrics, when):
    """插一条巡检结果（created_at 显式给，保证"最新/上次"的顺序可断言）。"""

    async def _go():
        async with Session() as s:
            r = models.InspectionResult(
                task_id="t-" + when.strftime("%H%M%S%f"), asset_id=asset_id,
                asset_name="dev-1", status="success", metrics=metrics, raw=[],
                created_at=when,
            )
            s.add(r)
            await s.commit()

    asyncio.run(_go())


def _metrics(cpu, extra=None):
    m = {"cpu": {"summary": "cpu %s%%" % cpu, "value": cpu, "status": "ok"}}
    if extra:
        m.update(extra)
    return m


T0 = _dt.datetime(2026, 10, 19, 10, 0, 0)
T1 = _dt.datetime(2026, 10, 19, 11, 0, 0)


def test_requires_auth(tmp_path):
    app, _ = _app(tmp_path, auth=False)
    assert TestClient(app).get(URL, params={"asset_id": "a1"}).status_code == 401


def test_query_validation(tmp_path):
    app, _ = _app(tmp_path)
    c = TestClient(app)
    assert c.get(URL).status_code == 422                       # 缺 asset_id
    assert c.get(URL, params={"asset_id": "a1", "limit": 1}).status_code == 422
    assert c.get(URL, params={"asset_id": "a1", "limit": 11}).status_code == 422
    assert c.get(URL, params={"asset_id": "a1", "limit": 2}).status_code == 200


def test_no_records(tmp_path):
    app, _ = _app(tmp_path)
    body = TestClient(app).get(URL, params={"asset_id": "ghost"}).json()
    assert body["snapshots"] == [] and body["delta"] == {}
    assert "至少 2 次" in (body.get("note") or "")


def test_single_record_has_note(tmp_path):
    app, S = _app(tmp_path)
    _add(S, "a1", _metrics(30), T0)
    body = TestClient(app).get(URL, params={"asset_id": "a1"}).json()
    assert len(body["snapshots"]) == 1 and body["delta"] == {}
    assert "至少 2 次" in (body.get("note") or "")


def test_numeric_up_down_same(tmp_path):
    """数值变化：涨/跌/不变三种趋势与 change 文案都要对，且 latest 是较新的那条。"""
    app, S = _app(tmp_path)
    _add(S, "a1", _metrics(30), T0)
    _add(S, "a1", _metrics(45), T1)
    d = TestClient(app).get(URL, params={"asset_id": "a1"}).json()["delta"]["cpu"]
    assert (d["prev"], d["latest"], d["trend"]) == (30, 45, "up")
    assert d["change"] == "+15"

    _add(S, "a2", _metrics(45), T0)
    _add(S, "a2", _metrics(30), T1)
    d = TestClient(app).get(URL, params={"asset_id": "a2"}).json()["delta"]["cpu"]
    assert d["trend"] == "down" and d["change"] == "-15"

    _add(S, "a3", _metrics(30), T0)
    _add(S, "a3", _metrics(30), T1)
    d = TestClient(app).get(URL, params={"asset_id": "a3"}).json()["delta"]["cpu"]
    assert d["trend"] == "same" and d["change"] == "0"


def test_zero_value_is_a_number_not_missing(tmp_path):
    """★ 0 是合法数值：`lv.get("value") or lv` 会把 0 当成"没有数值"而退回整个 dict，
    于是 "0 → 45" 被判成"变了但没有数值"，趋势/change 全丢（这是一个真缺陷）。

    这条用例就是钉这个边界：prev=0、latest=45 必须得到 trend=up、change="+45"。
    """
    app, S = _app(tmp_path)
    _add(S, "a1", _metrics(0), T0)
    _add(S, "a1", _metrics(45), T1)
    d = TestClient(app).get(URL, params={"asset_id": "a1"}).json()["delta"]["cpu"]
    assert d.get("trend") == "up", d
    assert d.get("prev") == 0 and d.get("latest") == 45, d
    assert d.get("change") == "+45", d


def test_dict_metrics_same_and_changed(tmp_path):
    """对象型指标（温度/设备/风扇）没有数值：相同判 same，不同判 changed 并带摘要。"""
    app, S = _app(tmp_path)
    t = {"temperature": {"summary": "温度 42C", "status": "ok",
                         "value": {"temperature": 42}}}
    _add(S, "a1", dict(_metrics(10), **t), T0)
    _add(S, "a1", dict(_metrics(10), **t), T1)
    d = TestClient(app).get(URL, params={"asset_id": "a1"}).json()["delta"]["temperature"]
    assert d["trend"] == "same"

    t2 = {"temperature": {"summary": "温度 55C", "status": "ok",
                          "value": {"temperature": 55}}}
    _add(S, "a2", dict(_metrics(10), **t), T0)
    _add(S, "a2", dict(_metrics(10), **t2), T1)
    d = TestClient(app).get(URL, params={"asset_id": "a2"}).json()["delta"]["temperature"]
    assert d["trend"] == "changed"
    assert d["prev_summary"] == "温度 42C" and d["latest_summary"] == "温度 55C"


def test_limit_and_ordering(tmp_path):
    """limit 生效（默认 2 / 最多 10），且 snapshots[0] 是最新一条。"""
    app, S = _app(tmp_path)
    for i, cpu in enumerate((10, 20, 30)):
        _add(S, "a1", _metrics(cpu), T0 + _dt.timedelta(hours=i))
    body = TestClient(app).get(URL, params={"asset_id": "a1"}).json()
    assert len(body["snapshots"]) == 2
    assert body["snapshots"][0]["metrics"]["cpu"]["value"] == 30      # 最新在前

    body = TestClient(app).get(URL, params={"asset_id": "a1", "limit": 3}).json()
    assert len(body["snapshots"]) == 3
    assert body["delta"]["cpu"]["prev"] == 20 and body["delta"]["cpu"]["latest"] == 30


def test_other_asset_not_mixed_in(tmp_path):
    """按 asset_id 隔离：别的设备的记录不能掺进来影响趋势。"""
    app, S = _app(tmp_path)
    _add(S, "a1", _metrics(30), T0)
    _add(S, "a2", _metrics(99), T1)      # 更晚，但不属于 a1
    body = TestClient(app).get(URL, params={"asset_id": "a1"}).json()
    assert len(body["snapshots"]) == 1 and body["delta"] == {}
