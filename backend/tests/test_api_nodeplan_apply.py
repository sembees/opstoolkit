# -*- coding: utf-8 -*-
"""M2 下发 API 用例（假 SSH 执行器，不碰真机）。

钉住：
  · 默认**禁止**下发（allow_mgmt_change 必须显式为 true）——评审：改管理口一改就失联；
  · 闸门：全局关 / 该机未开启 → 409；
  · apply 只布置 + 启 timer，**立刻返回**（不等结果），并把 change_id 落到规划上；
  · sync：读到 applied → 状态 applied；读到 rolled_back → 状态 rolled_back；
    多主机候选依次尝试（新 IP → 旧 IP），全不可达 → ok=False 且列出 tried。
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
GOOD = {"name": "eth0", "mode": "static", "ip": "192.168.40.10", "cidr": 24,
        "gateway": "192.168.40.1"}


class FakeRunner:
    """记录调用；read_file 返回预置的结果。"""

    def __init__(self, result_json=None, probe="probe-text", fail_connect=False):
        self.written, self.cmds = {}, []
        self.result_json = result_json
        self.probe = probe
        self.fail_connect = fail_connect

    def run(self, cmd, sudo=False):
        if self.fail_connect:
            raise OSError("connection refused")
        self.cmds.append(cmd)
        if "is-active" in cmd:
            return 0, "active\n", ""
        return 0, "", ""

    def write_file(self, path, content, mode=0o755, sudo=False):
        self.written[path] = content

    def read_file(self, path, sudo=False):
        if path.endswith("result.json"):
            return self.result_json
        if path.endswith("probe.txt"):
            return self.probe
        if path.endswith("apply.log"):
            return "APPLY_OK\n"
        return None

    def exists(self, path, sudo=False):
        return path in self.written


def _client(tmp_path):
    from app.api import nodeplan as api
    from app.core.auth import get_current_user, require_role
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "ap.db").as_posix(),
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
    app.dependency_overrides[require_role("admin")] = lambda: {
        "id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    return TestClient(app), api


def _mk(c, enable=True, global_on=True, ip="192.168.40.10"):
    if global_on:
        c.put(BASE + "/settings", json={"enabled": True})
    r = c.post(BASE + "/plans", json={
        "install_id": "inst-1", "serial": "SN-APPLY-1", "rack": "A01-U01",
        "planned_hostname": "n1", "mgmt_ip": ip, "os_type": "ubuntu",
        "interfaces": [{"kind": "interface", "name": "eth0", "spec": GOOD}],
    })
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    if enable:
        assert c.post(BASE + "/plans/%s/enable" % pid).status_code == 200
    return pid


def test_apply_requires_explicit_mgmt_consent(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c)
    r = c.post(BASE + "/plans/%s/apply" % pid, json={})
    assert r.status_code == 409
    assert "管理" in r.json()["detail"] and "allow_mgmt_change" in r.json()["detail"]


def test_apply_blocked_by_switch(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c, enable=False)
    r = c.post(BASE + "/plans/%s/apply" % pid, json={"allow_mgmt_change": True})
    assert r.status_code == 409 and "没开启" in r.json()["detail"]


def test_apply_deploys_and_returns_immediately(tmp_path, monkeypatch):
    c, api = _client(tmp_path)
    pid = _mk(c)
    fake = FakeRunner()
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: fake)

    r = c.post(BASE + "/plans/%s/apply" % pid, json={"allow_mgmt_change": True})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["change_id"] and out["timer_active"] is True and out["delay_sec"] == 90
    # 脚本包与 unit/timer 都写下去了
    assert any(p.endswith("/run.sh") for p in fake.written)
    assert any(p.endswith(".timer") and "/etc/systemd/system/" in p for p in fake.written)
    assert any("systemctl start opstk-netconfig-apply@" in x for x in fake.cmds)
    # 规划状态变成 applying，change_id 落库
    p = c.get(BASE + "/plans/" + pid).json()
    assert p["status"] == "applying"


def test_sync_applied(tmp_path, monkeypatch):
    c, api = _client(tmp_path)
    pid = _mk(c)
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: FakeRunner())
    c.post(BASE + "/plans/%s/apply" % pid, json={"allow_mgmt_change": True})

    fake = FakeRunner(result_json='{"result":"applied","change_id":"x","expected_hash":"h"}')
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: fake)
    r = c.post(BASE + "/plans/%s/sync" % pid, json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["status"] == "applied"
    assert "probe-text" in r.json()["probe_text"]


def test_sync_rolled_back(tmp_path, monkeypatch):
    c, api = _client(tmp_path)
    pid = _mk(c)
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: FakeRunner())
    c.post(BASE + "/plans/%s/apply" % pid, json={"allow_mgmt_change": True})

    fake = FakeRunner(result_json='{"result":"rolled_back","change_id":"x"}')
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: fake)
    r = c.post(BASE + "/plans/%s/sync" % pid, json={})
    assert r.status_code == 200 and r.json()["status"] == "rolled_back"


def test_sync_unreachable_lists_attempts(tmp_path, monkeypatch):
    c, api = _client(tmp_path)
    pid = _mk(c)
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: FakeRunner())
    c.post(BASE + "/plans/%s/apply" % pid, json={"allow_mgmt_change": True})

    monkeypatch.setattr(api, "_ssh_for", lambda body, host: FakeRunner(fail_connect=True))
    r = c.post(BASE + "/plans/%s/sync" % pid, json={})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert len(r.json()["tried"]) >= 1


def test_sync_requires_change_id(tmp_path, monkeypatch):
    c, api = _client(tmp_path)
    pid = _mk(c)
    monkeypatch.setattr(api, "_ssh_for", lambda body, host: FakeRunner())
    r = c.post(BASE + "/plans/%s/sync" % pid, json={})
    assert r.status_code == 409 and "change_id" in r.json()["detail"]
