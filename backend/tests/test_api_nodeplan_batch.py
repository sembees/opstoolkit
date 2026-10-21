# -*- coding: utf-8 -*-
"""批量生成（config_jobs）用例。

钉住：
  · 一次多台 → 逐台产出脚本并落库（可审计）；
  · **逐台隔离失败**：某台没开启/配置非法，只红那一台，其它照常生成；
  · 谨慎模式（skip_invalid=false）→ 整批 422 且**不留脏数据**（不能建了 job 又半途而废）；
  · 全局关着时批量也要 409（闸门在任何写之前）。
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

BASE = "/api/it/nodeplan"
GOOD = {"name": "eth0", "mode": "static", "ip": "192.168.20.10", "cidr": 24,
        "gateway": "192.168.20.1"}
BAD = dict(GOOD, ip="999.1.1.1")


def _client(tmp_path):
    from app.api import nodeplan as api
    from app.core.auth import get_current_user
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "j.db").as_posix(),
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
    app.include_router(api.router, prefix=BASE)
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    return TestClient(app), Session


def _mk(c, n, serial=None, spec=None, enable=True):
    c.put(BASE + "/settings", json={"enabled": True})
    r = c.post(BASE + "/plans", json={
        "install_id": "inst-%s" % n, "serial": serial or ("SN-%s" % n),
        "rack": "A01-U%02d" % n, "planned_hostname": "node-%s" % n, "os_type": "ubuntu",
        "interfaces": [{"kind": "interface", "name": "eth0", "spec": spec or GOOD}],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    if enable:
        assert c.post(BASE + "/plans/%s/enable" % pid).status_code == 200
    return pid


def test_batch_generates_all_and_persists(tmp_path):
    c, _ = _client(tmp_path)
    ids = [_mk(c, i) for i in (1, 2, 3)]
    r = c.post(BASE + "/jobs", json={"plan_ids": ids})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["generated"] == 3 and out["failed"] == 0
    assert len(out["items"]) == 3 and all(i["status"] == "generated" for i in out["items"])
    assert all(len(i["sha256"]) == 64 for i in out["items"])

    # 落库可审计：job 能查回来，且逐台有脚本原文
    j = c.get(BASE + "/jobs/" + out["job_id"]).json()
    assert len(j["items"]) == 3 and j["change_id"]
    assert any(x["id"] == out["job_id"] for x in c.get(BASE + "/jobs").json())


def test_batch_isolates_failures(tmp_path):
    """一台没开启 + 一台配置非法 → 只红这两台，第三台照常生成。"""
    c, _ = _client(tmp_path)
    ok1 = _mk(c, 1)
    not_enabled = _mk(c, 2, enable=False)
    broken = _mk(c, 3, spec=BAD)
    ok2 = _mk(c, 4)

    r = c.post(BASE + "/jobs", json={"plan_ids": [ok1, not_enabled, broken, ok2]})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["generated"] == 2 and out["failed"] == 2
    by_plan = {i["plan_id"]: i for i in out["items"]}
    assert by_plan[not_enabled]["status"] == "failed"
    assert "未开启" in by_plan[not_enabled]["error"]
    assert by_plan[broken]["status"] == "failed"
    assert by_plan[ok1]["status"] == "generated"


def test_batch_cautious_mode_aborts_whole_batch(tmp_path):
    """skip_invalid=false：任一行不行 → 整批 422，且**不留 job/items**（不建脏数据）。"""
    c, _ = _client(tmp_path)
    ok = _mk(c, 1)
    broken = _mk(c, 2, spec=BAD)
    r = c.post(BASE + "/jobs", json={"plan_ids": [ok, broken], "skip_invalid": False})
    assert r.status_code == 422
    assert "谨慎模式" in r.json()["detail"]
    # 半途而废的 job 必须回滚掉
    assert c.get(BASE + "/jobs").json() == []


def test_batch_requires_global_switch(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c, 1)
    c.put(BASE + "/settings", json={"enabled": False})
    assert c.post(BASE + "/jobs", json={"plan_ids": [pid]}).status_code == 409


def test_batch_rejects_empty_and_missing(tmp_path):
    c, _ = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    assert c.post(BASE + "/jobs", json={"plan_ids": []}).status_code == 422
    r = c.post(BASE + "/jobs", json={"plan_ids": ["nope"]})
    assert r.status_code == 200 and r.json()["failed"] == 1
    assert "不存在" in r.json()["items"][0]["error"]
