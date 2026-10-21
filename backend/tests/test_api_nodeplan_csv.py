# -*- coding: utf-8 -*-
"""CSV 批量导入用例。

现场表格八成是中文表头，所以列名必须中英双认；逐行隔离失败；
且导入**不能绕开**逐条创建的校验（SN 唯一、SN 不可信退 MAC、install_id 唯一）。
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
CSV_CN = """SN,机架,主机名,管理IP,系统,网卡,IP,掩码,网关,DNS
SN-A-001,A01-U10,web-01,10.1.1.11,ubuntu,eth0,10.1.1.11,24,10.1.1.1,10.1.1.1;8.8.8.8
SN-A-002,A01-U11,web-02,10.1.1.12,ubuntu,eth0,10.1.1.12,24,10.1.1.1,10.1.1.1
"""


def _client(tmp_path):
    from app.api import nodeplan as api
    from app.core.auth import get_current_user, require_role
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "csv.db").as_posix(),
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
    admin = {"id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_role("admin")] = lambda: admin
    return TestClient(app)


def _imp(c, text, **kw):
    body = {"csv": text}
    body.update(kw)
    return c.post(BASE + "/plans/import", json=body)


def test_import_chinese_headers(tmp_path):
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    r = _imp(c, CSV_CN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["created"] == 2 and out["skipped"] == 0 and out["total"] == 2

    plans = c.get(BASE + "/plans").json()
    assert {p["serial"] for p in plans} == {"SN-A-001", "SN-A-002"}
    p1 = next(p for p in plans if p["serial"] == "SN-A-001")
    assert p1["rack"] == "A01-U10" and p1["mgmt_ip"] == "10.1.1.11"
    assert p1["interfaces"][0]["spec"]["dns"] == ["10.1.1.1", "8.8.8.8"]


def test_import_skips_bad_rows_and_keeps_good_ones(tmp_path):
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    bad = "SN,IP\nSN-B-1,10.2.2.1\n,10.2.2.2\nSN-B-1,10.2.2.3\n"   # 第3行缺SN；第4行SN重复
    out = _imp(c, bad).json()
    assert out["created"] == 1 and out["skipped"] == 2
    assert {e["row"] for e in out["errors"]} == {3, 4}


def test_import_requires_serial_or_mac_and_ip(tmp_path):
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    assert _imp(c, "机架,主机名\nA01,x\n").status_code == 422
    assert _imp(c, "SN,机架\nSN-1,A01\n").status_code == 422     # 没有 IP 列


def test_import_respects_global_switch(tmp_path):
    c = _client(tmp_path)
    r = _imp(c, CSV_CN)
    assert r.status_code == 409 and "未开启" in r.json()["detail"]


def test_import_accepts_untrusted_serial_with_mac(tmp_path):
    """SN 不可信 → 走 MAC 键；这条在 CSV 里也必须生效，不能绕开校验。"""
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    text = ("serial,SN可信,mac,ip,主机名\n"
            "Not Specified,false,BC:24:11:AA:BB:CC,10.3.3.3,node-x\n")
    out = _imp(c, text).json()
    assert out["created"] == 1, out
    p = c.get(BASE + "/plans").json()[0]
    assert p["key_source"] == "mac" and p["serial_trusted"] is False


def test_install_id_must_exist(tmp_path):
    """以前假 install_id 也能建规划；现在建的时候就拦（422 + 人话）。"""
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    r = c.post(BASE + "/plans", json={"install_id": "no-such-record", "serial": "SN-X"})
    assert r.status_code == 422
    assert "装机记录不存在" in r.json()["detail"]


def test_csv_multi_nic_same_sn_merges(tmp_path):
    """同一 SN 多行 → 合并成一台机器的多块网卡（CSV 里最常见的 4 口机器）。"""
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    text = ("SN,IP,网卡,主机名\n"
            "SN-M-1,10.5.5.1,eth0,node-m\n"
            "SN-M-1,10.5.5.2,eth1,node-m\n")
    out = _imp(c, text).json()
    assert out["created"] == 2 and out["skipped"] == 0, out
    plans = c.get(BASE + "/plans").json()
    assert len(plans) == 1, plans
    assert sorted(i["name"] for i in plans[0]["interfaces"]) == ["eth0", "eth1"]


def test_csv_routes_column(tmp_path):
    """routes 列：`10.0.0.0/24>10.5.5.1; 10.9.0.0/16>` → 两条明细路由。"""
    c = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    text = ("SN,IP,网卡,routes\n"
            "SN-R-1,10.6.6.1,eth0,10.0.0.0/24>10.6.6.254; 10.9.0.0/16>\n")
    out = _imp(c, text).json()
    assert out["created"] == 1, out
    spec = c.get(BASE + "/plans").json()[0]["interfaces"][0]["spec"]
    assert spec["routes"][0] == {"to": "10.0.0.0/24", "via": "10.6.6.254"}
    assert spec["routes"][1]["to"] == "10.9.0.0/16" and spec["routes"][1]["via"] is None
