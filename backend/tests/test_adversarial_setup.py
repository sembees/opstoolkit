# -*- coding: utf-8 -*-
"""对抗性测试：首次引导向导（core/setup.py + api/setup.py）—— 审计方独立复核。

只测作者自测（test_setup_wizard.py）没覆盖的边界：

  1. **app_meta 表不存在**（老库在新版本第一次启动前被直接指过来 / 迁移半途）：
     core/setup.py 自己的哲学是"宁可少弹一条横幅，也绝不让 /setup/status 打挂"
     （_count_rows 把计数失败降级为 None）。但 setup_state 第一行的 get_meta
     **没有**这层保护 —— 本用例证明它在缺表库上直接 OperationalError → API 500。
     /setup/status 是"登录后的第一跳"，打挂 = 登录后整页白屏。
  2. **只有 1 条 notifications、没有任何资产/模板**：判"已在用"还是"全新"？
     现实现把 notifications 当业务数据（机器生成的发送记录）——一条由告警规则
     触发的通知就能让全新安装永远看不到向导。本用例固定该现状供人工判断。

DB 一律临时 sqlite（aiosqlite + NullPool），零联网、零外部副作用。
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
from app.core import setup as setup_core  # noqa: E402


def _session(db_path: pathlib.Path):
    engine = create_async_engine(
        "sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
    return engine, async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _app(SessionLocal, role="admin"):
    from app.api import setup as setup_api
    from app.core.auth import get_current_user
    from app.database import get_db

    app = FastAPI()
    app.include_router(setup_api.router, prefix="/api")
    app.dependency_overrides[get_db] = _app_db(SessionLocal)
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "u1", "username": "admin", "display_name": "admin", "role": role}
    return TestClient(app, raise_server_exceptions=False)


def _app_db(SessionLocal):
    async def _go():
        async with SessionLocal() as session:
            yield session
    return _go


def test_adversarial_setup_status_survives_missing_app_meta_table(tmp_path):
    """库存在、但 app_meta 表不存在（老库第一次启动前）。

    契约（core/setup.py 模块注释）："宁可少弹一条横幅，也绝不让一个计数查询把
    /setup/status（登录后的第一跳）打挂" —— 按同一口径，缺表也应当降级
    （needs_setup=False），而不是 500。
    """
    engine, SessionLocal = _session(tmp_path / "legacy.db")

    async def build():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
            await conn.exec_driver_sql("DROP TABLE app_meta")  # 老库：无 app_meta
    asyncio.run(build())

    # 直调核心：判定函数绝不能把状态接口打挂
    raised = None
    async def probe():
        async with SessionLocal() as db:
            try:
                st = await setup_core.setup_state(db)
                return st
            except Exception as e:  # noqa: BLE001
                nonlocal_raised.append(e)
                return None
    nonlocal_raised: list = []
    st = asyncio.run(probe())
    assert st is not None, (
        "setup_state 在缺 app_meta 表的库上直接抛 %s —— /setup/status 会 500"
        % (type(nonlocal_raised[0]).__name__ if nonlocal_raised else "?"))
    assert st["needs_setup"] is False

    # API 层：/setup/status 是登录后第一跳，必须可用
    with _app(SessionLocal) as c:
        r = c.get("/api/setup/status")
        assert r.status_code == 200, (
            "缺 app_meta 表时 /setup/status 返回 %s（契约：永远可用，最多降级 needs_setup）"
            % r.status_code)
        assert r.json().get("needs_setup") is False
    asyncio.run(engine.dispose())


def test_adversarial_single_notification_marks_db_as_in_use(tmp_path):
    """库里只有 1 条 notifications、没有任何资产/模板/用户：判"已在用"还是"全新"？

    现状固定（作者自测只测了"业务行=0 即全新"）：notifications 是机器生成的
    发送记录 —— 巡检告警一旦触发过一次，全新安装也会出现 1 行，此后 needs_setup
    恒为 False，向导永不再出现。这是当前实现的真实语义（判"已在用"），记录在案。
    """
    engine, SessionLocal = _session(tmp_path / "notify-only.db")

    async def seed():
        async with engine.begin() as conn:
            await conn.run_sync(models.Base.metadata.create_all)
        async with SessionLocal() as s:
            s.add(models.Notification(event_key="opstk:cpu:host1", metric_key="cpu",
                                      ok=True))
            await s.commit()
    asyncio.run(seed())

    async def judge():
        async with SessionLocal() as db:
            fp = await setup_core.db_usage_fingerprint(db)
            fresh = await setup_core.db_looks_fresh(db)
            st = await setup_core.setup_state(db)
            return fp, fresh, st
    fp, fresh, st = asyncio.run(judge())
    assert fp["notifications"] == 1
    assert st["needs_setup"] is False, (
        "1 条 notifications + 全库空 ⇒ 当前实现判'已在用'(needs_setup=False)；"
        "现状固定为 %r" % st["needs_setup"])
    asyncio.run(engine.dispose())
