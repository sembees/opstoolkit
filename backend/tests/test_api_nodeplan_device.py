# -*- coding: utf-8 -*-
"""M4a：网络设备规划的"生成"用例（复用 ZTP 渲染器）。

钉住：
  · 网络设备规划必须有 ztp_device_id（Linux 规划调这个端点要 422，不能瞎生成）；
  · 闸门：全局关 / 该设备未开启 → 409；
  · 正常路径：模板 + 设备 → 渲染出该设备的配置文件，内容含厂商关键字（system-view / vlan 等），
    并返回 sha256 与"未下发"提示。
"""
import pathlib
import sys

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import asyncio

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import crypto, models  # noqa: E402

BASE = "/api/it/nodeplan"


def _client(tmp_path):
    from app.api import nodeplan as api
    from app.core.auth import get_current_user, require_role
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "dev.db").as_posix(),
                                 poolclass=NullPool)
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def _create():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)

            # 夹具：存在性校验要求 install_id 真实存在
            for _iid in ("i1", "i2"):
                await conn.execute(models.PxeInstall.__table__.insert().values(
                    id=_iid, profile_id="p1", hostname=_iid, mac="",
                    ip="10.0.0.9", status="installed"))

    asyncio.run(_create())

    async def _db():
        async with Session() as s:
            yield s

    app = FastAPI()
    app.include_router(api.router, prefix=BASE)
    app.dependency_overrides[get_db] = _db
    admin = {"id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_role("admin")] = lambda: admin
    return TestClient(app), Session


def _seed(Session):
    """建一个 H3C 模板 + 一台设备（含加密口令，否则渲染器会拒绝）。"""
    async def _go():
        async with Session() as s:
            tpl = models.ZtpTemplate(
                name="lab-h3c", vendor="h3c", mgmt_vlan=10,
                mgmt_interface="Vlan-interface10", mgmt_netmask="255.255.255.0",
                mgmt_gateway="10.0.0.254", dns_servers=["10.0.0.254"],
                vlans=[{"id": 10, "name": "MGMT"}, {"id": 20, "name": "BIZ"}],
                admin_user="admin",
                admin_password_enc=crypto.encrypt("OpsAdm!n2026#Lan"),
                server_ip="10.0.0.113", deploy_mode="standalone",
            )
            s.add(tpl)
            await s.flush()
            dev = models.ZtpDevice(template_id=tpl.id, hostname="sw-lab-01",
                                   mac="00:11:22:33:44:55", mgmt_ip="10.0.0.21")
            s.add(dev)
            await s.commit()
            return tpl.id, dev.id

    return asyncio.run(_go())


def _mk_plan(c, ztp_device_id, enable=True):
    c.put(BASE + "/settings", json={"enabled": True})
    r = c.post(BASE + "/plans", json={
        "ztp_device_id": ztp_device_id, "serial": "SN-SW-01", "rack": "A02-U10",
        "planned_hostname": "sw-lab-01", "mgmt_ip": "10.0.0.21", "os_type": "ubuntu",
        "interfaces": [],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    if enable:
        c.post(BASE + "/plans/%s/enable" % pid)
    return pid


def test_linux_plan_cannot_generate_device_config(tmp_path):
    c, _ = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    r = c.post(BASE + "/plans", json={"install_id": "i1", "serial": "SN-LINUX"})
    pid = r.json()["id"]
    c.post(BASE + "/plans/%s/enable" % pid)
    r = c.post(BASE + "/plans/%s/generate-device" % pid)
    assert r.status_code == 422 and "ztp_device_id" in r.json()["detail"]


def test_device_generate_requires_switch(tmp_path):
    c, S = _client(tmp_path)
    _, dev_id = _seed(S)
    pid = _mk_plan(c, dev_id, enable=False)
    r = c.post(BASE + "/plans/%s/generate-device" % pid)
    assert r.status_code == 409 and "没开启" in r.json()["detail"]


def test_device_generate_renders_vendor_config(tmp_path):
    c, S = _client(tmp_path)
    _, dev_id = _seed(S)
    pid = _mk_plan(c, dev_id)
    r = c.post(BASE + "/plans/%s/generate-device" % pid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["vendor"] == "h3c"
    body = str(out["content"])
    assert "system-view" in body or "vlan 10" in body.lower()
    assert out["sha256"] and len(out["sha256"]) == 64
    assert "dry-run" in out["status_hint"]
    assert out["file_name"]
