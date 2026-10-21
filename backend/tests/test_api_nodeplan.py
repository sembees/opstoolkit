# -*- coding: utf-8 -*-
"""`/api/it/nodeplan` M1 第一刀的用例：规划 CRUD + 单机 dry-run 生成。

钉住三件事：
  1. **SN 做主键**（用户 2026-10-21 拍板）——但重复 SN 必须 409 并指出已有那条，不许静默覆盖；
  2. **SN 不可信时走 MAC 键**——白牌机/虚机取不到 SN 也能登记，不卡死流程；
  3. 生成**只生成不下发**，且直接复用 netconfig 生成器（报错原样透传，不吞成 500）。
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
IFACE = {"name": "eth0", "mode": "static", "ip": "192.168.10.10", "cidr": 24,
         "gateway": "192.168.10.1", "dns": ["192.168.10.1"]}


def _client(tmp_path, role="admin"):
    from app.api import nodeplan as api
    from app.core.auth import get_current_user
    from app.database import get_db

    engine = create_async_engine("sqlite+aiosqlite:///" + (tmp_path / "np.db").as_posix(),
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
        "id": "u1", "username": "admin", "display_name": "管理员", "role": role}
    return TestClient(app), Session


def _enable_global(c):
    """开全局总闸（每个用例独立库，所以每次都要开一次）。"""
    r = c.put(BASE + "/settings", json={"enabled": True})
    assert r.status_code == 200, r.text
    return r


def _enable_plan(c, pid):
    """对某台机器手动开启（用户要求：装机后由人点开，不自动）。"""
    r = c.post(BASE + "/plans/%s/enable" % pid)
    assert r.status_code == 200, r.text
    return r


def _mk(c, **kw):
    _enable_global(c)
    body = {"install_id": "inst-1", "serial": "SN-AAA-001", "rack": "A01-03-U12",
            "planned_hostname": "web-01", "os_type": "ubuntu",
            "interfaces": [{"kind": "interface", "name": "eth0", "spec": IFACE}]}
    body.update(kw)
    return c.post(BASE + "/plans", json=body)


def test_requires_auth(tmp_path):
    c, _ = _client(tmp_path)
    c.app.dependency_overrides.clear()
    assert c.get(BASE + "/plans").status_code == 401


def test_create_and_list(tmp_path):
    c, _ = _client(tmp_path)
    r = _mk(c)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["serial"] == "SN-AAA-001" and out["key_source"] == "serial"
    assert out["status"] == "pending" and len(out["interfaces"]) == 1

    lst = c.get(BASE + "/plans").json()
    assert len(lst) == 1 and lst[0]["id"] == out["id"]
    assert c.get(BASE + "/plans/" + out["id"]).json()["rack"] == "A01-03-U12"


def test_duplicate_serial_is_409_with_pointer(tmp_path):
    """同一 SN 再登记 → 409，且 detail 里要指出**已有那条**（id/机架/状态）。"""
    c, _ = _client(tmp_path)
    first = _mk(c).json()
    r = _mk(c, install_id="inst-2", planned_hostname="web-02")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert first["id"] in detail and "A01-03-U12" in detail


def test_serial_key_requires_serial(tmp_path):
    c, _ = _client(tmp_path)
    r = _mk(c, serial="")
    assert r.status_code == 422
    assert "SN" in r.json()["detail"]


def test_untrusted_serial_falls_back_to_mac(tmp_path):
    """SN 不可信：默认键改走 MAC；没给 MAC 要 422，给了就成功且 key_source=mac。"""
    c, _ = _client(tmp_path)
    r = _mk(c, serial="Not Specified", serial_trusted=False)
    assert r.status_code == 422 and "MAC" in r.json()["detail"]

    r = _mk(c, serial="Not Specified", serial_trusted=False,
            mac="BC:24:11:AA:BB:CC")
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["key_source"] == "mac" and out["serial_trusted"] is False
    assert out["serial"] == "Not Specified"     # SN 仍留档，只是不当键


def test_need_exactly_one_owner(tmp_path):
    c, _ = _client(tmp_path)
    assert _mk(c, install_id=None).status_code == 422          # 两个都没给
    r = _mk(c, ztp_device_id="ztp-1")                          # 两个都给
    assert r.status_code == 422


def test_generate_dry_run_uses_generator(tmp_path):
    """生成：ubuntu 默认 netplan，内容里要有网卡与地址；必须是 dry-run（带 sha256 与提示）。"""
    c, _ = _client(tmp_path)
    pid = _mk(c).json()["id"]
    _enable_plan(c, pid)
    r = c.post(BASE + "/plans/%s/generate" % pid)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["format"] == "netplan"
    assert out["file_name"].endswith(".yaml")
    assert "eth0" in out["content"] and "192.168.10.10" in out["content"]
    assert len(out["sha256"]) == 64
    assert "dry-run" in out["status_hint"]
    assert out["interface_count"] == 1


def test_generate_requires_interfaces(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c, interfaces=[]).json()["id"]
    _enable_plan(c, pid)
    r = c.post(BASE + "/plans/%s/generate" % pid)
    assert r.status_code == 422 and "网卡" in r.json()["detail"]


def test_generate_surfaces_generator_error(tmp_path):
    """生成器说配置非法时，HTTP 要 422 并把那句话透出来（不能包成 500）。"""
    c, _ = _client(tmp_path)
    bad = dict(IFACE, ip="999.1.1.1")
    pid = _mk(c, interfaces=[{"kind": "interface", "name": "eth0", "spec": bad}]).json()["id"]
    _enable_plan(c, pid)
    r = c.post(BASE + "/plans/%s/generate" % pid)
    assert r.status_code == 422, r.text
    assert "999.1.1.1" in r.json()["detail"] or "IPv4" in r.json()["detail"]


def test_update_replaces_interfaces(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c).json()["id"]
    body = {"install_id": "inst-1", "serial": "SN-AAA-001", "rack": "B02-01-U01",
            "planned_hostname": "web-01", "os_type": "rhel", "fmt": "ifcfg",
            "interfaces": [{"kind": "interface", "name": "eth1", "spec": dict(IFACE, name="eth1")}]}
    r = c.put(BASE + "/plans/" + pid, json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rack"] == "B02-01-U01" and out["os_type"] == "rhel"
    assert [i["name"] for i in out["interfaces"]] == ["eth1"]


def test_update_cannot_move_to_another_install(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c).json()["id"]
    r = c.put(BASE + "/plans/" + pid, json={"install_id": "other", "serial": "SN-AAA-001"})
    assert r.status_code == 422 and "另一台" in r.json()["detail"]


def test_delete_is_idempotent(tmp_path):
    c, _ = _client(tmp_path)
    pid = _mk(c).json()["id"]
    assert c.delete(BASE + "/plans/" + pid).status_code == 200
    assert c.delete(BASE + "/plans/" + pid).status_code == 200
    assert c.get(BASE + "/plans").json() == []


# ── 双重开关（用户 2026-10-21：额外功能，必须手动开启，不能自动）─────────────

def test_feature_is_off_by_default(tmp_path):
    """默认**关闭**：连"建规划"都不允许，并给出明确提示（不是 500/静默）。"""
    c, _ = _client(tmp_path)
    st = c.get(BASE + "/settings").json()
    assert st["enabled"] is False
    r = c.post(BASE + "/plans", json={"install_id": "i1", "serial": "SN-1"})
    assert r.status_code == 409
    assert "未开启" in r.json()["detail"]


def test_global_on_but_device_not_enabled(tmp_path):
    """开了全局也不能直接生成：每台机器还要人点"为该机开启"。"""
    c, _ = _client(tmp_path)
    pid = _mk(c).json()["id"]                      # _mk 里已开全局
    assert c.get(BASE + "/plans/" + pid).json()["enabled"] is False
    r = c.post(BASE + "/plans/%s/generate" % pid)
    assert r.status_code == 409
    assert "还没开启" in r.json()["detail"]

    _enable_plan(c, pid)
    assert c.get(BASE + "/plans/" + pid).json()["enabled"] is True
    assert c.post(BASE + "/plans/%s/generate" % pid).status_code == 200

    c.post(BASE + "/plans/%s/disable" % pid)
    assert c.post(BASE + "/plans/%s/generate" % pid).status_code == 409


def test_global_off_blocks_everything_even_if_device_enabled(tmp_path):
    """关总闸后：已开启的机器也不能生成、不能新建 —— 闸门在任何写操作之前。"""
    c, _ = _client(tmp_path)
    pid = _mk(c).json()["id"]
    _enable_plan(c, pid)
    c.put(BASE + "/settings", json={"enabled": False})

    assert c.post(BASE + "/plans/%s/generate" % pid).status_code == 409
    assert c.post(BASE + "/plans", json={"install_id": "i2", "serial": "SN-2"}).status_code == 409
    # 连"开/关某台机器"也不许（否则等于绕过总闸）
    assert c.post(BASE + "/plans/%s/disable" % pid).status_code == 409


def test_settings_update_requires_admin(tmp_path):
    """全局开关只有 admin 能改；viewer 403。"""
    c, _ = _client(tmp_path, role="viewer")
    assert c.put(BASE + "/settings", json={"enabled": True}).status_code == 403
    assert c.get(BASE + "/settings").status_code == 200      # 读可以


def test_install_path_does_not_touch_nodeplan():
    """装机链路里**不许**出现 nodeplan 引用（保证"不自动开启"不是靠自觉）。"""
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "app" / "api"
    for name in ("pxe.py",):
        text = (root / name).read_text(encoding="utf-8")
        assert "nodeplan" not in text and "NodePlan" not in text, name
