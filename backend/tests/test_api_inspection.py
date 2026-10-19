# -*- coding: utf-8 -*-
"""`/api/ct/inspection` 的 **HTTP 层**用例（审计遗留缺口：16 个端点此前 0 覆盖）。

为什么必须补这一层：
  · 这组端点是"巡检"的全部对外面（模板 CRUD/导入导出/克隆、任务创建与查询、三种格式导出、回放、
    WebSocket 实时流）。此前只有生成器/服务层的单元测试，**路由与状态码一次都没验过**；
  · 历史上真出过路由错接：`delete_template` 的装饰器被叠到 `replay_task` 上，导致
    "删除用户模板"永远失败（拿模板 id 去查任务）。这类错误只有 HTTP 层能抓到；
  · 导出三种格式（txt/json/csv）是运维交付物，Content-Type / Content-Disposition / BOM
    都要机械回归，否则 Excel 打开中文乱码。

隔离：
  · 临时 sqlite（aiosqlite + NullPool），不碰生产库；
  · `schedule_background` 打桩（不真跑后台巡检），保证断言只依赖本次请求；
  · 不联网、不启真实 SSH。
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


def _app(tmp_path, auth=True):
    """建一个只挂 inspection 路由的 app；auth=False 用于验 401。"""
    from app.api import inspection as insp
    from app.core.auth import get_current_user
    from app.database import get_db

    db_path = tmp_path / "inspection.db"
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
    app.include_router(insp.router, prefix="/api/ct/inspection")
    app.dependency_overrides[get_db] = _db
    if auth:
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1", "username": "admin", "display_name": "管理员", "role": "admin"}
    return app, Session


def _client(tmp_path, auth=True, monkeypatch=None):
    app, Session = _app(tmp_path, auth)
    if monkeypatch is not None:
        from app.api import inspection as insp
        # 后台巡检不真跑：把协程关掉即可（否则会留在事件循环里报"never awaited"）
        monkeypatch.setattr(insp, "schedule_background", lambda coro: coro.close(), raising=False)
    return TestClient(app), Session


TPL = {
    "name": "e2e-h3c", "vendor": "h3c", "description": "用例模板",
    "items": [{"key": "cpu", "label": "CPU 使用率", "command": "display cpu-usage"}],
}


# ── 鉴权 ────────────────────────────────────────────────────────────────────

def test_endpoints_require_auth(tmp_path):
    """没有登录信息时，读写端点都必须是 401（不能匿名建任务/删模板）。"""
    c, _ = _client(tmp_path, auth=False, monkeypatch=pytest.MonkeyPatch())
    for method, path in (("get", "/api/ct/inspection/templates"),
                         ("get", "/api/ct/inspection/templates/list"),
                         ("get", "/api/ct/inspection/tasks"),
                         ("post", "/api/ct/inspection/tasks"),
                         ("post", "/api/ct/inspection/run"),
                         ("delete", "/api/ct/inspection/templates/x")):
        r = getattr(c, method)(path, json=TPL) if method == "post" else getattr(c, method)(path)
        assert r.status_code == 401, (method, path, r.status_code)


# ── 内置模板 ────────────────────────────────────────────────────────────────

def test_builtin_templates_shape(tmp_path, monkeypatch):
    """内置模板来自驱动，结构是 `{vendor: {模板名: [指标...]}}`（不是 list）。

    这个形状我第一次也猜错过（以为是 `[{vendor, items}]`）—— 所以用用例把它钉死：
    前端与文档都按"按厂商分组"来读，改成 list 会静默打空界面。
    """
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)
    r = c.get("/api/ct/inspection/templates")
    assert r.status_code == 200
    data = r.json()
    assert isinstance(data, dict) and data, type(data).__name__
    assert "h3c" in data, sorted(data)
    for vendor, per_vendor in data.items():
        assert isinstance(per_vendor, dict) and per_vendor, vendor
        for name, items in per_vendor.items():
            assert isinstance(items, list) and items, (vendor, name)
            for it in items:
                assert it.get("command"), (vendor, name, it)


# ── 模板 CRUD / 克隆 / 导入导出 ──────────────────────────────────────────────

def test_template_crud_roundtrip(tmp_path, monkeypatch):
    """建 → 列 → 改 → 删；删除后列表里不该再有它（顺带钉住"DELETE 路由没错接回放"）。"""
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)

    r = c.post("/api/ct/inspection/templates", json=TPL)
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    assert r.json()["is_system"] is False

    r = c.get("/api/ct/inspection/templates/list")
    assert r.status_code == 200
    assert any(t["id"] == tid for t in r.json())

    upd = dict(TPL, name="e2e-h3c-改", items=TPL["items"] + [
        {"key": "memory", "label": "内存", "command": "display memory"}])
    r = c.put("/api/ct/inspection/templates/" + tid, json=upd)
    assert r.status_code == 200
    assert r.json()["name"] == "e2e-h3c-改"
    assert len(r.json()["items"]) == 2

    r = c.delete("/api/ct/inspection/templates/" + tid)
    assert r.status_code == 200 and r.json().get("ok") is True
    r = c.get("/api/ct/inspection/templates/list")
    assert not any(t["id"] == tid for t in r.json())


def test_update_and_delete_system_template_forbidden(tmp_path, monkeypatch):
    """内置（is_system）模板不许改、不许删 —— 403，且提示"克隆"。"""
    c, Session = _client(tmp_path, monkeypatch=monkeypatch)

    async def _mk():
        async with Session() as s:
            t = models.InspectionTemplate(name="sys", vendor="h3c", is_system=True,
                                          items=[{"key": "cpu", "command": "display cpu-usage"}])
            s.add(t)
            await s.commit()
            await s.refresh(t)
            return t.id

    tid = asyncio.run(_mk())
    assert c.put("/api/ct/inspection/templates/" + tid, json=TPL).status_code == 403
    assert c.delete("/api/ct/inspection/templates/" + tid).status_code == 403


def test_update_missing_template_404(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)
    assert c.put("/api/ct/inspection/templates/nope", json=TPL).status_code == 404
    # 删除不存在的模板按幂等处理（返回 ok=True），这样重复点删除不会报错
    r = c.delete("/api/ct/inspection/templates/nope")
    assert r.status_code == 200 and r.json().get("ok") is True


def test_clone_and_export_import(tmp_path, monkeypatch):
    """克隆 → 导出 JSON → 导入回来：三件套要能形成闭环（备份/迁移的常用路径）。"""
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)
    tid = c.post("/api/ct/inspection/templates", json=TPL).json()["id"]

    r = c.post("/api/ct/inspection/templates/%s/clone" % tid)
    assert r.status_code == 200
    clone = r.json()
    assert clone["id"] != tid and clone["is_system"] is False
    assert "副本" in clone["name"]

    r = c.get("/api/ct/inspection/templates/%s/export" % tid)
    assert r.status_code == 200
    body = r.json()
    assert set(body) >= {"name", "vendor", "items"}

    payload = {"name": body["name"] + "-导入", "vendor": body["vendor"],
               "description": body.get("description", ""), "items": body["items"]}
    r = c.post("/api/ct/inspection/templates/import", json=payload)
    assert r.status_code == 200 and r.json()["name"].endswith("-导入")

    # 导入的非法入参：空名字 / items 不是数组
    assert c.post("/api/ct/inspection/templates/import", json={"name": "  "}).status_code == 400
    assert c.post("/api/ct/inspection/templates/import",
                  json={"name": "x", "items": "not-a-list"}).status_code == 400


def test_export_missing_template_404(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)
    assert c.get("/api/ct/inspection/templates/nope/export").status_code == 404
    assert c.post("/api/ct/inspection/templates/nope/clone").status_code == 404


# ── 任务：创建 / 列表 / 结果 / 导出 / 回放 ──────────────────────────────────

def test_task_lifecycle_and_exports(tmp_path, monkeypatch):
    """建任务（空资产，不跑真设备）→ 列表能看到 → 结果为空 → 三种格式导出 + 回放。"""
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)

    r = c.post("/api/ct/inspection/tasks", json={"name": "e2e-task", "kind": "ct",
                                                "template": "standard", "asset_ids": []})
    assert r.status_code == 200, r.text
    task = r.json()
    tid = task["id"]
    assert task["status"] in ("running", "done", "success")

    r = c.get("/api/ct/inspection/tasks")
    assert r.status_code == 200 and any(t["id"] == tid for t in r.json())

    r = c.get("/api/ct/inspection/tasks/%s/results" % tid)
    assert r.status_code == 200 and r.json() == []

    r = c.get("/api/ct/inspection/tasks/%s/replay" % tid)
    assert r.status_code == 200
    rep = r.json()
    assert rep["task_id"] == tid and rep["asset_count"] == 0

    for fmt, media, ext in (("txt", "text/plain", "txt"),
                            ("json", "application/json", "json"),
                            ("csv", "text/csv", "csv")):
        r = c.post("/api/ct/inspection/tasks/%s/export" % tid, json={"fmt": fmt})
        assert r.status_code == 200, (fmt, r.text)
        assert media in r.headers["content-type"]
        cd = r.headers["content-disposition"]
        assert cd.startswith("attachment;") and cd.rstrip('"').endswith("." + ext)
        if fmt == "csv":
            # ★ BOM 必须在：否则 Excel 打开中文列名乱码（真机踩过）
            assert r.content.startswith("\ufeff".encode("utf-8"))


def test_export_and_replay_missing_task_404(tmp_path, monkeypatch):
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)
    assert c.post("/api/ct/inspection/tasks/nope/export", json={"fmt": "txt"}).status_code == 404
    assert c.get("/api/ct/inspection/tasks/nope/replay").status_code == 404


def test_run_sync_endpoint_with_no_assets(tmp_path, monkeypatch):
    """同步巡检接口：空资产列表 → 200 + results 为空（不触网）。"""
    c, _ = _client(tmp_path, monkeypatch=monkeypatch)
    r = c.post("/api/ct/inspection/run", json={"name": "sync", "kind": "ct", "asset_ids": []})
    assert r.status_code == 200
    body = r.json()
    assert body.get("results") == [] and body.get("kind") == "ct"


# ── WebSocket 鉴权 ──────────────────────────────────────────────────────────

def test_websocket_rejects_bad_token(tmp_path, monkeypatch):
    """WS 没带/带错 token 必须 4401 关闭（否则等于匿名能拉设备输出）。"""
    from starlette.websockets import WebSocketDisconnect

    monkeypatch.setattr("app.core.auth.decode_access_token",
                        lambda tok: (_ for _ in ()).throw(ValueError("bad")), raising=False)
    app, _ = _app(tmp_path, auth=True)
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/api/ct/inspection/ws?token=bad") as ws:
            ws.receive_text()
    assert ei.value.code == 4401
