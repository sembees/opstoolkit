# -*- coding: utf-8 -*-
"""多设备并发巡检的**会话隔离**回归（生产缺陷：≥2 台设备 ⇒ 0 条结果）。

### 修的是什么

生产容器（10.128.118.113）实测：多设备巡检任务只要 ≥2 台，结果就是 **0 条**、任务被标
failed。根因：`inspect_many` 的并发分支共用调用方传入的同一个 `AsyncSession` ——
SQLAlchemy 的同一会话不允许并发使用，第二个并发操作会在 `_connection_for_bind` 抛

    InvalidRequestError: This session is provisioning a new connection;
    concurrent operations are not permitted

随后会话关闭时再叠一个 IllegalStateChangeError，异常被 `run_task_in_background`
的 except 静默吞掉 ⇒ 0 条 InspectionResult、任务被标 failed。单台设备
（asset_ids 长度 1）没有并发 ⇒ 一直正常，所以之前一直没暴露。
受控 A/B 已排除告警通知（notify_enabled 开/关各 3 轮全部失败，见 mimo/ab_notify_race.py）。

### 这一组用例守什么

  · (a) ≥2 台设备：**每台都能拿到结果**，结果数 = 2，两台都是 success；
  · (b) ★ 本缺陷的直接护栏：两个并发分支拿到的会话**不是同一个对象**，而且
        **不是**调用方传入的那个会话 —— 用"记录每次资产查询收到的 session
        身份（id + 对象）"的方式断言，并核对每个分支的会话确实来自
        `service.async_session` 新建（而不是复用外部传入的）；
  · (c) 1 台设备的老行为不变：结果数 = 1、成功；
  · 端到端对齐生产现象：`run_task_in_background` 修后要落库 2 条
    InspectionResult、任务 done（修前是 0 条 + failed）。

mock 边界（不连任何真设备）：TCP 预检 `_preflight` 置空；SSH 执行
`_connect_and_run` 换成假桩（返回固定回显，解析层照常工作）；资产查询包一层
spy（记录会话身份 + 两分支都到齐才放行，保证真并发、不许串行蒙混），底层仍是
临时 sqlite 库里的真实查询，这样修前能在用例里**复现**生产同款
InvalidRequestError，修后每个分支各自查询各自的会话。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.core import models  # noqa: E402
from app.ct.inspection import service as inspection_service  # noqa: E402


class _BranchSpy:
    """记录并发分支实际拿到的会话；parties 个分支都到齐后一起放行（保证真并发）。

    · make_session —— 顶替 service.async_session：每建一个会话记一笔，
      用来断言"分支的会话是新建的，不是外部传入的那个"。
    · observe —— 包住 crud.get_asset：记录本次查询用的是哪个会话（这是
      分支里第一处 DB 操作，最能代表该分支实际使用的会话），然后等所有
      分支都进来再放行 —— 修前这会让两个分支**真正同时**挤同一个会话，
      复现生产上的 InvalidRequestError，而不是碰运气。
    """

    def __init__(self, session_factory, parties):
        self.session_factory = session_factory
        self.parties = parties
        self.created = []           # 经 service.async_session 创建的每个会话
        self.branch_sessions = []   # 分支实际使用的会话（按到达顺序）
        self._arrived = 0
        self._release = None

    def make_session(self):
        s = self.session_factory()
        self.created.append(s)
        return s

    async def observe(self, db, aid):
        self.branch_sessions.append(db)
        if self._release is None:
            self._release = asyncio.Event()
        self._arrived += 1
        if self._arrived >= self.parties:
            self._release.set()
        await self._release.wait()


class _ConcurrentInspectionCase(unittest.TestCase):
    """临时 sqlite 库（NullPool）+ 全 mock 的 SSH/预检，不碰生产库、不连设备。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(cls._tmp.name) / "concurrent-inspection.db"
        cls.engine = create_async_engine(
            "sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
        cls.SessionLocal = async_sessionmaker(
            cls.engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.engine.dispose())
        cls._tmp.cleanup()

    def setUp(self):
        async def _reset():
            async with self.engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
        asyncio.run(_reset())

    def _seed_assets(self, aids):
        async def _seed():
            async with self.SessionLocal() as db:
                for i, aid in enumerate(aids):
                    db.add(models.Asset(id=aid, name="sw-%02d" % (i + 1),
                                        category="ct", vendor="h3c",
                                        host="10.0.0.%d" % (9 + i)))
                await db.commit()
        asyncio.run(_seed())

    def _patch_env(self, spy):
        """把真 SSH / TCP 预检 / 会话工厂 / 资产查询都换成可控桩（恢复由 addCleanup 保证）。"""
        real_get_asset = inspection_service.crud.get_asset

        # spy_get_asset 走闭包里的真函数（比挂在类上的 _real_get_asset 更直白）
        async def spy_get_asset(db, aid):
            await spy.observe(db, aid)
            return await real_get_asset(db, aid)

        def fake_connect_and_run(params, key_text, metric_cmds, custom_cmds,
                                 disable_pager, on_line):
            """假 SSH：不连设备，按命令清单返回固定回显（结构同 _connect_and_run）。"""
            outputs = []
            for mc in metric_cmds:
                out = "fake output for %s" % (mc.key or mc.command)
                outputs.append({"cmd": mc.command, "label": mc.label, "key": mc.key,
                                "unit": mc.unit, "textfsm": mc.textfsm, "output": out,
                                "tried": [mc.command], "rejected": False, "is_alt": False})
                on_line({"type": "output", "cmd": mc.command, "label": mc.label,
                         "key": mc.key, "output": out, "is_alt": False,
                         "rejected": False})
            return outputs

        patches = [
            # 会话工厂 → spy：记录每个新建会话（分支自建会话的证据）
            mock.patch.object(inspection_service, "async_session", spy.make_session),
            # 资产查询 → spy 包装（记录该分支用的会话；底层仍是临时库真查询）
            mock.patch.object(inspection_service.crud, "get_asset", spy_get_asset),
            # 真 SSH → 假桩；TCP 预检 → 直接放行
            mock.patch.object(inspection_service, "_connect_and_run", fake_connect_and_run),
            mock.patch.object(inspection_service, "_preflight", lambda *a, **k: None),
            # 并发度钉成 2：与被测的"两台同时跑"对齐，且不受 .env 影响
            # （settings 上必有该属性，无需 raising=False；mock.patch.object 收 new 值时不再收 kwargs）
            mock.patch.object(settings, "inspection_concurrency", 2),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_two_assets_get_two_results_with_isolated_sessions(self):
        """(a)(b) 2 台设备：结果数=2 都成功；两个分支各用各的会话，且都不是调用方的。"""
        self._seed_assets(["a1", "a2"])

        async def _go():
            spy = _BranchSpy(self.SessionLocal, parties=2)
            self._patch_env(spy)
            async with self.SessionLocal() as caller_db:
                results = await inspection_service.inspect_many(caller_db, ["a1", "a2"])
            return results, spy, caller_db

        results, spy, caller_db = asyncio.run(_go())

        # (a) 每台都有结果、两台都成功
        self.assertEqual(len(results), 2, "2 台设备必须出 2 条结果（缺陷时是 0 条）")
        self.assertEqual(sorted(r["asset_id"] for r in results), ["a1", "a2"])
        self.assertEqual({r["status"] for r in results}, {"success"})

        # (b) ★ 直接护栏：两个分支用的会话不是同一个对象，也不是调用方传入的
        self.assertEqual(len(spy.branch_sessions), 2, "两个分支都应各查过一次资产")
        s1, s2 = spy.branch_sessions
        self.assertIsNot(s1, s2, "两个并发分支不能共用同一个 AsyncSession（本缺陷根因）")
        self.assertIsNot(s1, caller_db, "分支会话不能是调用方传入的那个会话")
        self.assertIsNot(s2, caller_db, "分支会话不能是调用方传入的那个会话")
        self.assertNotEqual(id(s1), id(s2))
        # 且都是经 service.async_session 新建的（每个分支恰一个）
        self.assertEqual(len(spy.created), 2, "每个分支应恰好自建一个会话")
        for s in spy.branch_sessions:
            self.assertTrue(any(s is c for c in spy.created),
                            "分支会话必须来自 async_session 自建，而不是外部传入")

    def test_single_asset_behavior_unchanged(self):
        """(c) 1 台设备：老行为不变（结果数=1、成功）；分支会话仍是自建的。"""
        self._seed_assets(["solo"])

        async def _go():
            spy = _BranchSpy(self.SessionLocal, parties=1)
            self._patch_env(spy)
            async with self.SessionLocal() as caller_db:
                results = await inspection_service.inspect_many(caller_db, ["solo"])
            return results, spy, caller_db

        results, spy, caller_db = asyncio.run(_go())
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["asset_id"], "solo")
        self.assertEqual(results[0]["status"], "success")
        self.assertEqual(len(spy.branch_sessions), 1)
        self.assertIsNot(spy.branch_sessions[0], caller_db,
                         "单台也应走分支自建会话（同一套代码路径，不应有特例）")
        self.assertEqual(len(spy.created), 1)

    def test_runner_persists_two_results_for_two_assets(self):
        """端到端对齐生产现象：修前 2 台 ⇒ 0 条结果 + 任务 failed；修后 ⇒ 2 条 + done。"""
        self._seed_assets(["a1", "a2"])

        async def _seed_task():
            async with self.SessionLocal() as db:
                db.add(models.InspectionTask(id="task-e2e", name="巡检 2 台",
                                             kind="default", asset_ids=["a1", "a2"],
                                             status="pending"))
                await db.commit()
        asyncio.run(_seed_task())

        spy = _BranchSpy(self.SessionLocal, parties=2)
        self._patch_env(spy)
        out = asyncio.run(inspection_service.run_task_in_background("task-e2e"))

        self.assertEqual(len(out), 2, "后台任务要返回 2 条结果（缺陷时 except 吞掉 ⇒ 0 条）")
        self.assertEqual({r["status"] for r in out}, {"success"})

        async def _q():
            async with self.SessionLocal() as db:
                task = await db.get(models.InspectionTask, "task-e2e")
                rows = (await db.execute(select(models.InspectionResult).where(
                    models.InspectionResult.task_id == "task-e2e"))).scalars().all()
                return task.status, sorted(r.asset_id for r in rows)
        status, asset_ids = asyncio.run(_q())
        self.assertEqual(status, "done")
        self.assertEqual(asset_ids, ["a1", "a2"], "结果必须按调用方会话顺序落库")

        # 会话隔离在整条链路上同样成立：1 个调用方会话 + 每分支 1 个自建会话
        self.assertEqual(len(spy.created), 3)
        self.assertEqual(len({id(s) for s in spy.branch_sessions}), 2,
                         "两个分支的会话必须互不相同")


if __name__ == "__main__":
    unittest.main()
