# -*- coding: utf-8 -*-
"""M4b apply-device 用例（假 shell 注入，不碰真设备）。

钉住设备下发的三条纪律：
  · 默认**不保存**（persist=false）：推 + 对账，但不动启动配置；
  · 只有显式 persist=true 且对账通过才 save；
  · 命令报错 → 判失败、状态 rolled_back（return 丢弃未保存配置）。
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
CFG = ("system-view\nvlan 10\n name MGMT\nquit\ninterface Vlan-interface10\n"
       " ip address 10.0.0.21 255.255.255.0\nquit\n")


class FakeShell:
    def __init__(self, running=None, error_on=None, save_out="[OK]", echo=True):
        self.sent, self.opened, self.closed = [], False, False
        self.running = running or "vlan 10\n ip address 10.0.0.21 255.255.255.0\n"
        self.error_on = error_on
        self.save_out = save_out
        self.echo = echo

    def open(self):
        self.opened = True
        return self

    def send(self, line):
        self.sent.append(line)
        if self.error_on and self.error_on in line:
            return "% Unrecognized command found at '^' position.\n"
        if "display current-configuration" in line:
            # 模拟"设备把刚推的配置都收下了"：回显已发送的行 + 预置内容
            return ("\n".join(self.sent) + "\n" if self.echo else "") + self.running
        if line.startswith("save") or line == "write memory":
            return self.save_out
        return "<SW1>"

    def close(self):
        self.closed = True


def _client(tmp_path):
    from app.api import nodeplan as api
    from app.core.auth import get_current_user, require_role
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "dev2.db").as_posix(),
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
    admin = {"id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[get_current_user] = lambda: admin
    app.dependency_overrides[require_role("admin")] = lambda: admin
    return TestClient(app), api, Session


def _seed(Session):
    async def _go():
        async with Session() as s:
            tpl = models.ZtpTemplate(
                name="t", vendor="h3c", mgmt_vlan=10, mgmt_interface="Vlan-interface10",
                mgmt_netmask="255.255.255.0", mgmt_gateway="10.0.0.254",
                dns_servers=[], vlans=[{"id": 10, "name": "MGMT"}], admin_user="admin",
                admin_password_enc=crypto.encrypt("x"), server_ip="10.0.0.1",
                deploy_mode="standalone")
            s.add(tpl)
            await s.flush()
            dev = models.ZtpDevice(template_id=tpl.id, hostname="sw1",
                                   mac="00:11:22:33:44:55", mgmt_ip="10.0.0.21")
            s.add(dev)
            await s.commit()
            return dev.id

    return asyncio.run(_go())


def _mk(c, dev_id, enable=True):
    c.put(BASE + "/settings", json={"enabled": True})
    r = c.post(BASE + "/plans", json={"ztp_device_id": dev_id, "serial": "SN-SW-9",
                                      "planned_hostname": "sw1", "mgmt_ip": "10.0.0.21",
                                      "interfaces": []})
    pid = r.json()["id"]
    if enable:
        c.post(BASE + "/plans/%s/enable" % pid)
    return pid


def test_default_forbids_mgmt_change(tmp_path):
    c, api, S = _client(tmp_path)
    pid = _mk(c, _seed(S))
    r = c.post(BASE + "/plans/%s/apply-device" % pid, json={})
    assert r.status_code == 409 and "allow_mgmt_change" in r.json()["detail"]


def test_linux_plan_rejected(tmp_path):
    c, api, S = _client(tmp_path)
    c.put(BASE + "/settings", json={"enabled": True})
    pid = c.post(BASE + "/plans", json={"install_id": "i1", "serial": "SN-L"}).json()["id"]
    c.post(BASE + "/plans/%s/enable" % pid)
    r = c.post(BASE + "/plans/%s/apply-device" % pid, json={"allow_mgmt_change": True})
    assert r.status_code == 422 and "ztp_device_id" in r.json()["detail"]


def test_push_verifies_but_does_not_save_by_default(tmp_path, monkeypatch):
    c, api, S = _client(tmp_path)
    pid = _mk(c, _seed(S))
    fake = FakeShell()
    monkeypatch.setattr(api, "_shell_for", lambda body, host: fake)

    r = c.post(BASE + "/plans/%s/apply-device" % pid, json={"allow_mgmt_change": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] is True and out["saved"] is False and out["status"] == "applied"
    assert out["verify"]["ok"] is True and out["expected_count"] >= 2
    assert not any(x.strip().startswith("save") for x in fake.sent)
    assert fake.closed is True
    assert c.get(BASE + "/plans/" + pid).json()["status"] == "applied"


def test_persist_saves_when_verified(tmp_path, monkeypatch):
    c, api, S = _client(tmp_path)
    pid = _mk(c, _seed(S))
    fake = FakeShell()
    monkeypatch.setattr(api, "_shell_for", lambda body, host: fake)
    r = c.post(BASE + "/plans/%s/apply-device" % pid,
               json={"allow_mgmt_change": True, "persist": True})
    assert r.status_code == 200 and r.json()["saved"] is True
    assert any(x.strip().startswith("save") for x in fake.sent)


def test_command_error_rolls_back_without_save(tmp_path, monkeypatch):
    c, api, S = _client(tmp_path)
    pid = _mk(c, _seed(S))
    fake = FakeShell(error_on="ip address")
    monkeypatch.setattr(api, "_shell_for", lambda body, host: fake)
    r = c.post(BASE + "/plans/%s/apply-device" % pid,
               json={"allow_mgmt_change": True, "persist": True})
    out = r.json()
    assert out["ok"] is False and out["status"] == "rolled_back" and out["saved"] is False
    assert "return" in fake.sent                       # 丢弃未保存配置
    assert not any(x.strip().startswith("save") for x in fake.sent)


def test_mismatch_marks_failed_and_does_not_save(tmp_path, monkeypatch):
    """设备回了运行配置但缺关键段（例如设备忽略了某条）→ 判 failed、不保存。"""
    c, api, S = _client(tmp_path)
    pid = _mk(c, _seed(S))
    fake = FakeShell(running="vlan 10\n", echo=False)   # 设备只收了 vlan，缺 ip address
    monkeypatch.setattr(api, "_shell_for", lambda body, host: fake)
    r = c.post(BASE + "/plans/%s/apply-device" % pid,
               json={"allow_mgmt_change": True, "persist": True})
    out = r.json()
    assert out["ok"] is False and out["status"] == "failed" and out["saved"] is False
