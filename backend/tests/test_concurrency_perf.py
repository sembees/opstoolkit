# -*- coding: utf-8 -*-
"""**并发**与**性能预算**用例（审计里"性能/并发"那块空白）。

定位：这是**冒烟级**，不是基准测试。
  · 并发：证明同一时刻多请求不会 500、不会互相吃掉对方的数据（SQLite 下最容易出现
    "database is locked" 或丢更新）；
  · 性能：给几个"人能感觉到的"操作设一个**宽松**上界（比实测慢 5~10 倍），
    用来抓"某次改动把它从毫秒级变成秒级"这类回归，不做精细 benchmark。

为什么用 WAL + busy_timeout：默认的 rollback journal 下并发写会直接报 locked，
那是测试配置问题而不是产品问题 —— 这里按生产库的常见配置（WAL）来跑。
"""
import asyncio
import pathlib
import sys
import time

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402


def _engine(tmp_path, name="conc.db"):
    e = create_async_engine(
        "sqlite+aiosqlite:///" + (tmp_path / name).as_posix(),
        poolclass=NullPool,
        connect_args={"timeout": 30},
    )

    @event.listens_for(e.sync_engine, "connect")
    def _pragma(dbapi_conn, _rec):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=30000")
        cur.close()

    return e


def _app(tmp_path):
    from fastapi import FastAPI
    from app.api import alerts as alerts_api, compare as compare_api, pxe as pxe_api
    from app.core.auth import get_current_user
    from app.database import get_db

    e = _engine(tmp_path)
    Session = async_sessionmaker(e, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with e.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

    asyncio.run(_create())

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(alerts_api.router, prefix="/api/alerts")
    app.include_router(pxe_api.router, prefix="/api/it/pxe")
    app.include_router(compare_api.router, prefix="/api/ct/inspection")
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    return app, Session


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t",
                             timeout=60)


# ── 并发 ────────────────────────────────────────────────────────────────────

def test_concurrent_rule_creates_all_persist(tmp_path):
    """20 个并发"新建规则"：必须全部 200，且一条都不能丢（丢更新是最隐蔽的并发缺陷）。"""
    app, _ = _app(tmp_path)

    async def _go():
        async with _client(app) as c:
            async def one(i):
                return await c.post("/api/alerts/rules", json={
                    "name": "并发-%02d" % i, "metric_key": "cpu",
                    "operator": "gt", "threshold": 80.0, "enabled": True})

            rs = await asyncio.gather(*[one(i) for i in range(20)])
            codes = [r.status_code for r in rs]
            lst = await c.get("/api/alerts/rules")
            return codes, lst.json()

    codes, rules = asyncio.run(_go())
    assert set(codes) == {200}, "并发建规则出现非 200: %r" % (codes,)
    names = {r["name"] for r in rules}
    assert len([n for n in names if n.startswith("并发-")]) == 20, \
        "并发写丢了数据：只落库 %d 条" % len([n for n in names if n.startswith("并发-")])


def test_concurrent_reads_are_stable(tmp_path):
    """20 个并发读：全部 200、返回结构一致（读路径不该因为并发出现空/错）。"""
    app, _ = _app(tmp_path)

    async def _go():
        async with _client(app) as c:
            rs = await asyncio.gather(*[c.get("/api/it/pxe/profiles") for _ in range(20)])
            return [(r.status_code, isinstance(r.json(), list)) for r in rs]

    out = asyncio.run(_go())
    assert all(st == 200 and ok for st, ok in out), out


def test_concurrent_mixed_read_write_no_500(tmp_path):
    """读写混跑：写 10 条规则、同时读 10 次，不能出现 5xx（SQLite 锁争用最容易在这里炸）。"""
    app, _ = _app(tmp_path)

    async def _go():
        async with _client(app) as c:
            writes = [c.post("/api/alerts/rules", json={
                "name": "混合-%02d" % i, "metric_key": "cpu", "operator": "gt",
                "threshold": 50.0, "enabled": True}) for i in range(10)]
            reads = [c.get("/api/alerts/rules") for _ in range(10)]
            rs = await asyncio.gather(*(writes + reads))
            return [r.status_code for r in rs]

    codes = asyncio.run(_go())
    assert all(st < 500 for st in codes), "并发下出现 5xx: %r" % (codes,)


# ── 性能预算（宽松上界，抓数量级回归）──────────────────────────────────────

def test_netconfig_generate_many_interfaces_budget():
    """30 个网卡的配置生成：预算 2 秒（实测远低于此；只抓"变成秒级"的回归）。"""
    from app.core.schemas import NetConfigRequest
    from app.it.netconfig.generator import generate_netconfig

    ifaces = [{"name": "eth%d" % i, "mode": "static", "ip": "10.0.%d.1" % i,
               "cidr": 24, "gateway": "10.0.%d.254" % i,
               "dns": ["10.0.0.53"], "routes": [{"to": "192.168.%d.0/24" % i,
                                                 "via": "10.0.%d.254" % i}]}
              for i in range(30)]
    req = NetConfigRequest(os="ubuntu", format="netplan", interfaces=ifaces)
    t0 = time.perf_counter()
    out, fname = generate_netconfig(req)     # 返回 (内容, 文件名)
    cost = time.perf_counter() - t0
    assert cost < 2.0, "30 网卡生成耗时 %.2fs，超出预算" % cost
    assert fname.endswith(".yaml")
    assert out.count("eth") >= 30


def test_pxe_generate_many_installs_budget():
    """30 台装机记录的文件生成：预算 3 秒。"""
    from app.it.pxe.generator import PxeConfig, generate_all

    cfg = PxeConfig(os_type="ubuntu", os_version="22.04", admin_user="ops",
                    admin_password="Test@123", server_ip="10.0.0.1",
                    http_root="http://10.0.0.1:8000/pxe/serve", deploy_mode="standalone",
                    net_mode="dhcp", iso_url="http://10.0.0.1:8000/pxe/iso/t.iso")
    inst = [{"mac": "00:11:22:33:%02x:%02x" % (i // 256, i % 256),
             "hostname": "h%02d" % i, "ip": "10.0.0.%d" % (100 + i), "status": "pending"}
            for i in range(30)]
    t0 = time.perf_counter()
    files = generate_all(cfg, inst)
    cost = time.perf_counter() - t0
    assert cost < 3.0, "30 台装机记录生成耗时 %.2fs，超出预算" % cost
    assert len([k for k in files if k.startswith("boot/")]) == 30


def test_compare_endpoint_budget(tmp_path):
    """对比接口在 200 条记录上：预算 2 秒（它只查最近 2 条，所以与总量基本无关）。"""
    import datetime as dt
    app, Session = _app(tmp_path)

    async def _seed():
        async with Session() as s:
            base = dt.datetime(2026, 10, 1)
            for i in range(200):
                s.add(models.InspectionResult(
                    task_id="t%d" % i, asset_id="a1", asset_name="dev",
                    status="success", metrics={"cpu": {"value": i, "status": "ok"}},
                    raw=[], created_at=base + dt.timedelta(minutes=i)))
            await s.commit()

    asyncio.run(_seed())

    async def _go():
        async with _client(app) as c:
            t0 = time.perf_counter()
            r = await c.get("/api/ct/inspection/results/compare", params={"asset_id": "a1"})
            return r.status_code, time.perf_counter() - t0

    st, cost = asyncio.run(_go())
    assert st == 200
    assert cost < 2.0, "对比接口耗时 %.2fs，超出预算" % cost
