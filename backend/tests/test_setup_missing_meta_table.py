# -*- coding: utf-8 -*-
"""缺 app_meta 表（老库 / 迁移半途 / 首次启动前）时向导接口的容错回归。

背景（审计红测试 tests/test_adversarial_setup.py 已钉死的事实）：
  app_meta 表不存在时，setup_state 第一行 get_meta 直接抛 OperationalError
  ⇒ GET /setup/status 500 ⇒ 登录后第一跳白屏；同一路径的 api/setup.py
  _setup_done 也走 get_meta ⇒ checks / complete 同样 500。
  修复口径（与 _count_rows"计数失败降级"同一哲学，见 app/core/setup.py
  的 _get_meta_checked / setup_state 注释）：
    · 读 meta 一律不因缺表抛错（按"无记录"降级）；
    · 缺表 ⇒ 标记面"未知" ⇒ needs_setup 按既有"未知 ⇒ 可能在用"口径降级
      为 False（宁可少弹一条横幅，也绝不把在用系统拽进向导）—— 该取值由
      审计红测试断言钉死，本文件不再重复断言其正当性，只补审计没覆盖的面；
    · 数据面判据（db_looks_fresh）本身不受缺表影响，语义逐字不变。

本文件覆盖（审计用例之外的边界）：
  1. 缺表 + 空库：/setup/status 200 + 契约字段逐字不变，且同库
     db_looks_fresh 仍如实判"全新"（证明判据层没有被替换成硬编码）；
  2. 缺表 + 有业务数据：数据面判据主导，needs_setup=False（与有数据
     有标记的库同口径）；
  3. 缺表下 checks 返回可读清单、complete 返回可读的业务前置 400（不 500）；
  4. get_meta / password_changed / 改密埋点写路径的直调容错语义。
DB 一律临时 sqlite（aiosqlite + NullPool），零联网、零外部副作用。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402
from app.core import setup as setup_core  # noqa: E402


class _MissingMetaCase(unittest.TestCase):
    """缺 app_meta 表的临时 sqlite + 最小 app（setup 路由按生产口径挂 /api 前缀）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        cls.engine = create_async_engine(
            "sqlite+aiosqlite:///"
            + (pathlib.Path(cls._tmp.name) / "legacy.db").as_posix(),
            poolclass=NullPool)
        cls.SessionLocal = async_sessionmaker(
            cls.engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.engine.dispose())
        cls._tmp.cleanup()

    def setUp(self):
        self._rebuild()

    def _rebuild(self, drop_app_meta=True):
        """建全部表后按需 DROP app_meta（模拟老库在新版本第一次启动前被直接指过来）。"""
        async def _go():
            async with self.engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
                if drop_app_meta:
                    await conn.exec_driver_sql("DROP TABLE app_meta")
        asyncio.run(_go())

    # ── 基础设施（与 test_setup_wizard.py / 审计用例同款） ──

    def _override_db(self):
        SessionLocal = self.SessionLocal

        async def _go():
            async with SessionLocal() as session:
                yield session
        return _go

    def _client(self, role="admin", username="admin"):
        from app.api import setup as setup_api
        from app.core.auth import get_current_user
        from app.database import get_db

        app = FastAPI()
        app.include_router(setup_api.router, prefix="/api")
        app.dependency_overrides[get_db] = self._override_db()
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1", "username": username, "display_name": username, "role": role}
        # raise_server_exceptions=False：回归时以 500 状态呈现，断言信息更可读
        return TestClient(app, raise_server_exceptions=False)

    def _seed_asset(self):
        async def _go():
            async with self.SessionLocal() as s:
                s.add(models.Asset(name="core-sw-01", category="ct", host="10.0.0.11"))
                await s.commit()
        asyncio.run(_go())

    @staticmethod
    def _patch_probes():
        """把体检的系统探测钉成"一切正常 + 通知未配"（用例不依赖运行机真实环境）。"""
        def _fake_dirs():
            return [{"key": key, "name": name, "path": path,
                     "exists": True, "writable": True,
                     "writable_required": bool(need_write), "write_err": "",
                     "free_bytes": 80 * 1024**3, "total_bytes": 100 * 1024**3}
                    for key, name, path, need_write in setup_core.WIZARD_DIRS]

        return (
            mock.patch.object(setup_core, "_listen_endpoints", lambda ports: {}),
            mock.patch.object(setup_core, "_dnsmasq_running", lambda: True),
            mock.patch.object(setup_core, "_host_unit_status",
                              lambda: {"files_present": 3, "link_version": 3,
                                       "link_err": ""}),
            mock.patch.object(setup_core, "_opstk_conf_files", lambda: []),
            mock.patch.object(setup_core, "storage_dirs", _fake_dirs),
            mock.patch("app.core.notify.feishu_configured", return_value=False),
        )

    def _probe_core(self):
        """直调核心：同一会话先 setup_state（内部 get_meta 失败→继续计数）再
        db_looks_fresh，调用顺序与生产一致；返回 (setup_state, db_looks_fresh)。"""
        SessionLocal = self.SessionLocal

        async def _go():
            async with SessionLocal() as db:
                st = await setup_core.setup_state(db)
                fresh = await setup_core.db_looks_fresh(db)
                return st, fresh
        return asyncio.run(_go())


class MissingMetaStatusTest(_MissingMetaCase):
    """/setup/status 在缺表库上必须可用（登录后的第一跳，绝不 500）。"""

    def test_status_survives_missing_meta_on_empty_db(self):
        """缺表 + 空库：200 正常 JSON、契约字段逐字不变；数据面判据照样判"全新"。"""
        # 直调核心（同一会话：get_meta 降级后，计数查询照常可跑）
        st, fresh = self._probe_core()
        self.assertIsNone(st["setup_completed_at"])
        self.assertFalse(st["steps_done"]["completed"])
        self.assertFalse(st["steps_done"]["password_changed"])
        self.assertTrue(fresh, "数据面判据应照常判'全新'——缺 app_meta 不影响它")
        # 取值按审计红测试钉死的口径：标记面"未知" ⇒ 降级不引导（宁可少弹横幅）
        self.assertIs(st["needs_setup"], False)

        # API 层：200 + 契约字段逐字不变（needs_setup / steps_done / 两个时间戳）
        r = self._client().get("/api/setup/status")
        self.assertEqual(r.status_code, 200,
                         "缺 app_meta 表时 /setup/status 绝不能 500（登录后第一跳）")
        body = r.json()
        self.assertEqual(set(body), {"needs_setup", "steps_done",
                                     "password_changed_at", "setup_completed_at"})
        self.assertEqual(set(body["steps_done"]), {"password_changed", "completed"})
        self.assertIs(body["needs_setup"], False)
        self.assertIsNone(body["setup_completed_at"])
        self.assertIsNone(body["password_changed_at"])

    def test_missing_meta_with_business_data_is_judged_in_use(self):
        """缺表 + 有资产：数据面判据主导 ⇒ needs_setup=False（数据面口径未被替换）。

        缺表只影响"标记面"；库里有业务数据时数据面判据说"在用"，空库时判据说
        "全新" —— 两种缺表库的 needs_setup 都不是硬编码，而是判据照常工作。
        """
        self._seed_asset()
        async def _fp():
            async with self.SessionLocal() as s:
                return await setup_core.db_usage_fingerprint(s)
        fp = asyncio.run(_fp())
        self.assertEqual(fp["assets"], 1)

        st, fresh = self._probe_core()
        self.assertFalse(fresh, "有业务数据 ⇒ 数据面判据说'在用'（与缺表无关）")
        self.assertIs(st["needs_setup"], False)

        r = self._client().get("/api/setup/status")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIs(r.json()["needs_setup"], False)


class MissingMetaChecksCompleteTest(_MissingMetaCase):
    """checks / complete 在缺表下也要给可读结果（同路径 _setup_done 读 app_meta）。"""

    def test_checks_return_readable_list_when_meta_missing(self):
        """缺表：完成标记读不到 ⇒ 视为"未完成" ⇒ 体检清单照常返回（200），不 500。"""
        patches = self._patch_probes()
        for p in patches:
            p.start()
        try:
            r = self._client().get("/api/setup/checks")
            self.assertEqual(r.status_code, 200, r.text)
            items = r.json()
            self.assertIsInstance(items, list)
            self.assertGreaterEqual(len(items), 12)
            for it in items:
                self.assertEqual(set(it.keys()),
                                 {"id", "title", "level", "detail", "fix"}, it)
                self.assertIn(it["level"], ("ok", "warn", "error"), it)
            ids = [i["id"] for i in items]
            self.assertEqual(len(ids), len(set(ids)), "检查项 id 不得重复")
            self.assertIn("port_dns", ids)
            self.assertIn("notify", ids)
        finally:
            for p in patches:
                p.stop()

    def test_complete_blocked_with_readable_reason_when_meta_missing(self):
        """缺表：POST /setup/complete 按业务前置 400（改密标记读不到 ⇒ "尚未修改
        初始口令"），绝不 500，也绝不在标记面不可用时"完成"。"""
        patches = self._patch_probes()
        for p in patches:
            p.start()
        try:
            r = self._client().post("/api/setup/complete")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("口令", r.json()["detail"])
        finally:
            for p in patches:
                p.stop()


class MetaReadWriteToleranceTest(_MissingMetaCase):
    """core/setup.py 里读 / 读写 app_meta 的每一处的直调容错语义。"""

    def test_get_meta_returns_none_without_raising(self):
        async def _go():
            async with self.SessionLocal() as db:
                return (await setup_core.get_meta(db, setup_core.KEY_SETUP_COMPLETED_AT),
                        await setup_core.get_meta(db, setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT))
        completed, changed = asyncio.run(_go())
        self.assertIsNone(completed)
        self.assertIsNone(changed)

    def test_password_changed_reads_false_when_meta_missing(self):
        async def _go():
            async with self.SessionLocal() as db:
                return await setup_core.password_changed(db)
        self.assertIs(asyncio.run(_go()), False)

    def test_password_marker_write_path_tolerates_missing_table(self):
        """改密埋点（缺表库上唯一可达的写路径）：如实返回 False，不抛、不假成功。"""
        from app.config import settings

        async def _go():
            async with self.SessionLocal() as db:
                wrote = await setup_core.record_admin_password_changed(
                    db, settings.admin_username)
                changed_after = await setup_core.password_changed(db)
                return wrote, changed_after
        wrote, changed_after = asyncio.run(_go())
        self.assertFalse(wrote, "标记确实没写成 —— 必须如实返回 False")
        self.assertFalse(changed_after, "读回来也必须是'没做过'（不许假成功）")

    def test_non_admin_username_still_short_circuits(self):
        """非初始管理员改密与此无关：既不写标记也不触库报错（既有语义不回归）。"""
        async def _go():
            async with self.SessionLocal() as db:
                return await setup_core.record_admin_password_changed(db, "ops1")
        self.assertIs(asyncio.run(_go()), False)


if __name__ == "__main__":
    unittest.main()
