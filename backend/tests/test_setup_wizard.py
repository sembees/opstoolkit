# -*- coding: utf-8 -*-
"""首次访问引导向导（安装向导）的后端回归。

覆盖面（外部任务书 §测试）：
  · /setup/status 三态：未完成 / 已改密未完成 / 已完成；
  · ★ needs_setup 的数据面判据（2026-10-08 误判修正）：
      needs_setup = (setup_completed_at 不存在) AND (库看起来全新)，
      "全新" = assets / inspection_templates / pxe_profiles / ztp_templates /
      inspection_results / alert_rules / notifications 全为空 且 users 只有默认
      那一个管理员 —— 有任何业务数据 ⇒ 已在用 ⇒ 永不误判成"未安装"；
      系统内置巡检模板（启动即 seed，is_system=True）不算"在用"证据；
    语义变更说明：原"只完成未改密 ⇒ 仍 needs_setup=True"改为 False
      （完成标记只能由 POST /setup/complete 写入，其守卫已要求改密；
       在用实例不再被打扰），见 SetupStatusTest 对应用例的注释；
  · /setup/checks 清单形状、端口被占 → error+fix、目录不可写 → error、
    未配通知 → 仅 warn（绝不算 error）、重载单元"无法判断"要如实说；
  · /system/storage 形状与 free/total，默认路径逐字 = 容器内固定路径；
  · /setup/complete 守卫：未改密 → 400；有 error 项 → 400；成功后再调 → 403；
    完成后 checks / network / complete 全部 403（向导不可复用，防后门）；
  · 完成后 status 的 needs_setup 变 false；
  · ★ 安全红线：以上接口**匿名访问一律 401**（不许新增匿名可写接口，用例钉死）；
  · 改密埋点（方案 (a)）：初始管理员改密成功 → admin_password_changed_at 落库；
    其它用户改密不写标记。

DB 用临时文件 sqlite（NullPool，TestClient 每请求独立事件循环）；体检的系统探测
（端口 / dnsmasq / 目录 / 宿主单元 / 通知）全部 mock —— 用例结果只取决于代码本身。
所有用例零联网：判据用例只写本地临时 sqlite，不触任何外部服务。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402
from app.core import setup as setup_core  # noqa: E402


class _WizardCase(unittest.TestCase):
    """临时 sqlite 库 + 最小 app（setup 路由按生产口径挂 /api 前缀）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(cls._tmp.name) / "setup-wizard.db"
        cls.engine = create_async_engine(
            "sqlite+aiosqlite:///" + db_path.as_posix(), poolclass=NullPool)
        cls.SessionLocal = async_sessionmaker(
            cls.engine, class_=AsyncSession, expire_on_commit=False)

    @classmethod
    def tearDownClass(cls):
        asyncio.run(cls.engine.dispose())
        cls._tmp.cleanup()

    def setUp(self):
        self._recreate_db()

    def _recreate_db(self):
        """drop + create：每个用例/子用例都从"空库"起步（不跑 seed，保持判据纯净）。"""
        async def _reset():
            async with self.engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.drop_all)
                await conn.run_sync(models.Base.metadata.create_all)
        asyncio.run(_reset())

    # ── 基础设施 ──

    def _override_db(self):
        async def _go():
            async with self.SessionLocal() as session:
                yield session
        return _go

    def _client(self, role="admin", username="admin", override_auth=True):
        """只挂 setup 路由（生产挂载：api_router 无前缀、路径自带 /setup 与 /system）。

        override_auth=False 时不顶掉 get_current_user —— 用于"匿名必须 401"的红线用例
        （oauth2_scheme 没拿到 Authorization 头会直接 401，不触库）。
        """
        from app.api import setup as setup_api
        from app.core.auth import get_current_user
        from app.database import get_db

        app = FastAPI()
        app.include_router(setup_api.router, prefix="/api")
        app.dependency_overrides[get_db] = self._override_db()
        if override_auth:
            app.dependency_overrides[get_current_user] = lambda: {
                "id": "u1", "username": username, "display_name": username, "role": role}
        return TestClient(app)

    def _auth_client(self, username="admin"):
        """挂 auth 路由（生产前缀 /api/auth），验证改密埋点。"""
        from app.api import auth as auth_api
        from app.core.auth import get_current_user
        from app.database import get_db

        app = FastAPI()
        app.include_router(auth_api.router, prefix="/api/auth")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "u1", "username": username, "display_name": username, "role": "admin"}
        app.dependency_overrides[get_db] = self._override_db()
        return TestClient(app)

    def _seed_user(self, username="admin", password="old-pass-1234", role="admin"):
        from app.core.auth import hash_password

        async def _go():
            async with self.SessionLocal() as s:
                s.add(models.User(username=username, hashed_password=hash_password(password),
                                  display_name=username, role=role))
                await s.commit()
        asyncio.run(_go())

    def _set_meta(self, key, value="2026-10-08 00:00:00"):
        async def _go():
            async with self.SessionLocal() as s:
                s.add(models.AppMeta(key=key, value=value))
                await s.commit()
        asyncio.run(_go())

    def _read_meta(self, key):
        async def _go():
            async with self.SessionLocal() as s:
                row = await s.get(models.AppMeta, key)
                return row.value if row is not None else None
        return asyncio.run(_go())

    @staticmethod
    def _fake_dirs(writable=True, exists=True, free_bytes=80 * 1024**3,
                   total_bytes=100 * 1024**3):
        """按默认 WIZARD_DIRS 合成 storage_dirs() 的返回（用例不依赖宿主真实目录）。"""
        out = []
        for key, name, path, need_write in setup_core.WIZARD_DIRS:
            writable = bool(writable and exists)
            out.append({
                "key": key, "name": name, "path": path,
                "exists": bool(exists), "writable": writable,
                "writable_required": bool(need_write),
                "write_err": "" if (writable or not exists) else "Permission denied",
                "free_bytes": int(free_bytes) if exists else 0,
                "total_bytes": int(total_bytes) if exists else 0,
            })
        return out

    @classmethod
    def _patch_probe_clean(cls):
        """把体检涉及的所有系统探测钉成"一切正常"（用例只验证代码自身的分级逻辑）。"""
        return cls._patch_probe_custom()

    @classmethod
    def _patch_probe_custom(cls, notify_configured=None, dirs=None, **kw):
        """钉体检的探测结果（全部 mock，用例不依赖运行机的真实环境）。

        notify_configured=None 不动通知；True/False 钉 feishu_configured。
        dirs=None 用合成目录清单（全部存在且可写）；也可显式传入（可写性/空间用例）。
        """
        base = {
            "listen": {}, "dnsmasq": True,
            "host": {"files_present": 3, "link_version": 3, "link_err": ""},
            "confs": [],
        }
        base.update(kw)
        if dirs is None:
            dirs = cls._fake_dirs()
        patches = [
            mock.patch.object(setup_core, "_listen_endpoints", lambda ports: base["listen"]),
            mock.patch.object(setup_core, "_dnsmasq_running", lambda: base["dnsmasq"]),
            mock.patch.object(setup_core, "_host_unit_status", lambda: base["host"]),
            mock.patch.object(setup_core, "_opstk_conf_files", lambda: base["confs"]),
            mock.patch.object(setup_core, "storage_dirs", lambda: dirs),
        ]
        if notify_configured is not None:
            patches.append(mock.patch("app.core.notify.feishu_configured",
                                      return_value=bool(notify_configured)))
        return tuple(patches)

    @staticmethod
    def _start(patches):
        for p in patches:
            p.start()
        return patches

    @staticmethod
    def _stop(patches):
        for p in patches:
            p.stop()


class SetupStatusTest(_WizardCase):
    """status 三态（任务书 §2）。"""

    def test_status_fresh_install(self):
        """未完成：全新库 → needs_setup=True，两步都未做。"""
        body = self._client().get("/api/setup/status").json()
        self.assertTrue(body["needs_setup"])
        self.assertFalse(body["steps_done"]["password_changed"])
        self.assertFalse(body["steps_done"]["completed"])
        self.assertIsNone(body["password_changed_at"])
        self.assertIsNone(body["setup_completed_at"])

    def test_status_password_changed_but_not_completed(self):
        """已改密未完成：needs_setup 仍 True，但第一步视为已做。"""
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        body = self._client().get("/api/setup/status").json()
        self.assertTrue(body["needs_setup"])
        self.assertTrue(body["steps_done"]["password_changed"])
        self.assertFalse(body["steps_done"]["completed"])

    def test_status_completed_and_password_changed(self):
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        self._set_meta(setup_core.KEY_SETUP_COMPLETED_AT)
        body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])
        self.assertTrue(body["steps_done"]["completed"])

    def test_status_completed_but_password_never_changed(self):
        """★ 语义变更（2026-10-08 误判修正）：有 setup_completed_at ⇒ needs_setup=False。

        旧口径"未改密不算完"返回 True；新口径以完成标记为单一闸门 —— 该标记只能
        由 POST /setup/complete 写入，而 complete 的守卫本来就要求"已改初始口令"，
        所以"有标记 ⇒ 必然改过密"；即便出现异常库（完成标记在、改密标记丢了），
        也不再打扰用户（向导不可重复进，横幅另有"稍后"可压）。
        """
        self._set_meta(setup_core.KEY_SETUP_COMPLETED_AT)
        body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])
        self.assertTrue(body["steps_done"]["completed"])
        self.assertFalse(body["steps_done"]["password_changed"])

    def test_status_open_to_operator(self):
        """status 只要求登录态：operator 也要能拿到横幅依据。"""
        c = self._client(role="operator", username="ops1")
        self.assertEqual(c.get("/api/setup/status").status_code, 200)

    def test_status_contract_shape_unchanged(self):
        """★ 契约不漂移：/setup/status 返回字段与旧版逐字相同（本次只改算法）。

        前端 stores/setup.js 与 Login.vue 按 needs_setup / steps_done 解析，
        字段名一个都不能动。
        """
        body = self._client().get("/api/setup/status").json()
        self.assertEqual(set(body), {"needs_setup", "steps_done",
                                     "password_changed_at", "setup_completed_at"})
        self.assertEqual(set(body["steps_done"]), {"password_changed", "completed"})


class NeedsSetupDataCriteriaTest(_WizardCase):
    """★ needs_setup 的数据面判据（2026-10-08 误判修正，本任务核心用例）。

    事故：旧口径 needs_setup = (未完成) OR (初始口令未更换)，只看 app_meta 标记 ——
    人工配置好、用了数周、库里有资产/模板/巡检记录的实例（生产 10.128.118.113，
    口令是人工设的、没走改密接口）被误判成"未安装"：登录后分流进向导、每页挂横幅。
    新口径：needs_setup = (setup_completed_at 不存在) AND (库看起来全新)；
    判据实现见 app/core/setup.py 的 db_looks_fresh（全部只读 SELECT COUNT(*)，
    零写入、不写 app_meta）。判据方向：宁可漏弹横幅，绝不打扰在用实例。
    """

    # 7 张业务表（与 backend/app/core/setup.py 的 _FRESHNESS_TABLES 一一对应；
    # 表名以 app/core/models.py 为准，不猜表名）。
    BUSINESS_TABLES = ("assets", "inspection_templates", "pxe_profiles", "ztp_templates",
                       "inspection_results", "alert_rules", "notifications")

    def _seed_one(self, table: str) -> None:
        """往指定业务表塞一行最小数据（模拟"实例被真实用过"；只写本地临时 sqlite）。"""
        async def _go():
            async with self.SessionLocal() as s:
                if table == "assets":
                    s.add(models.Asset(name="core-sw-01", category="ct", host="10.0.0.11"))
                elif table == "inspection_templates":
                    # 用户自建模板（is_system=False）才是"在用"证据，见专有系统模板用例
                    s.add(models.InspectionTemplate(name="现场自建巡检模板",
                                                    vendor="h3c", is_system=False))
                elif table == "pxe_profiles":
                    s.add(models.PxeProfile(name="ubuntu-22.04", os_type="ubuntu"))
                elif table == "ztp_templates":
                    s.add(models.ZtpTemplate(name="h3c-ztp", vendor="h3c"))
                elif table == "inspection_results":
                    task = models.InspectionTask(name="夜巡", asset_ids=[])
                    s.add(task)
                    await s.flush()
                    s.add(models.InspectionResult(task_id=task.id, asset_id="a1",
                                                  asset_name="core-sw-01"))
                elif table == "alert_rules":
                    s.add(models.AlertRule(name="CPU 过高", metric_key="cpu", threshold=90.0))
                elif table == "notifications":
                    s.add(models.Notification(event_key="opstk:cpu:core-sw-01", asset_id="a1"))
                else:
                    raise AssertionError("未知业务表：" + table)
                await s.commit()
        asyncio.run(_go())

    def _seed_system_template(self) -> None:
        """模拟启动期 seed_default_templates()：写入一行系统内置巡检模板。"""
        async def _go():
            async with self.SessionLocal() as s:
                s.add(models.InspectionTemplate(name="h3c 默认巡检模板", vendor="h3c",
                                                is_system=True))
                await s.commit()
        asyncio.run(_go())

    # ── 真·全新安装：仍然引导 ──

    def test_fresh_db_without_markers_needs_setup(self):
        """空库 + 无标记 ⇒ needs_setup=True（真·全新安装仍会引导）。"""
        self._recreate_db()
        body = self._client().get("/api/setup/status").json()
        self.assertTrue(body["needs_setup"])
        self.assertIsNone(body["setup_completed_at"])

    def test_system_seeded_templates_do_not_block_fresh_install(self):
        """★ 启动即有的系统内置模板（is_system=True）≠ 已在用 ⇒ 仍 needs_setup=True。

        为什么必须有这条：应用每次启动都会跑 seed_default_templates()
        （app/ct/seeding.py）往 inspection_templates 写系统模板 —— 若把这张表的
        系统行也当"在用"证据，全新安装一启动就永远满足不了"全为空"，
        真·全新安装反而永远进不了向导（与任务书目标直接冲突）。
        """
        self._recreate_db()
        self._seed_system_template()
        body = self._client().get("/api/setup/status").json()
        self.assertTrue(body["needs_setup"])

    def test_password_changed_marker_alone_keeps_setup_needed(self):
        """只改了密、库里没有任何业务数据 ⇒ 仍要引导（改密只是第 1 步）。"""
        self._recreate_db()
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        body = self._client().get("/api/setup/status").json()
        self.assertTrue(body["needs_setup"])
        self.assertTrue(body["steps_done"]["password_changed"])

    # ── 既有实例：任一业务数据 ⇒ 永不误判 ──

    def test_any_business_row_blocks_setup(self):
        """★ 有资产/有巡检记录/有模板任一条 + 无标记 ⇒ needs_setup=False。

        对 7 张业务表逐一验证：任一表有数据都视为"已在用"，
        既有实例不再被误判成"未安装"（生产 10.128.118.113 的根因）。
        """
        for table in self.BUSINESS_TABLES:
            with self.subTest(table=table):
                self._recreate_db()
                self._seed_one(table)
                body = self._client().get("/api/setup/status").json()
                self.assertFalse(body["needs_setup"], table)

    def test_production_like_instance_is_not_misjudged(self):
        """★ 生产 10.128.118.113 镜像态：资产 + 用户模板 + 巡检记录 + 只有默认管理员
        + 无任何 app_meta 标记 ⇒ needs_setup=False（本事故的直接回归用例）。"""
        self._recreate_db()
        self._seed_system_template()          # 启动 seed 的系统模板（不算在用证据）
        for table in ("assets", "inspection_templates", "inspection_results"):
            self._seed_one(table)             # 数周真实使用留下的业务数据
        body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])
        # steps_done 仍如实回报（两步确实都没做过），供向导页展示
        self.assertFalse(body["steps_done"]["password_changed"])
        self.assertFalse(body["steps_done"]["completed"])

    def test_extra_user_blocks_setup(self):
        """users 有第 2 个账号（哪怕业务表全空）⇒ 视为已在用。"""
        self._recreate_db()
        from app.config import settings

        self._seed_user(username=settings.admin_username)   # 默认管理员
        self._seed_user("ops1", "ops-pass-1234", role="operator")
        body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])

    def test_non_default_sole_user_blocks_setup(self):
        """users 只有 1 个账号但不是默认管理员 ⇒ 视为已在用（默认账号被动过）。"""
        self._recreate_db()
        self._seed_user("renamed-admin", "some-pass-1234", role="admin")
        body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])

    # ── 完成标记：单一闸门 ──

    def test_completed_marker_wins_on_empty_db(self):
        """有 setup_completed_at + 空库 ⇒ needs_setup=False（不论库空不空）。"""
        self._recreate_db()
        self._set_meta(setup_core.KEY_SETUP_COMPLETED_AT)
        body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])
        self.assertTrue(body["steps_done"]["completed"])

    def test_completed_marker_wins_on_used_db(self):
        """有 setup_completed_at + 库里有业务数据 ⇒ 仍 needs_setup=False。"""
        self._recreate_db()
        self._seed_one("assets")
        self._set_meta(setup_core.KEY_SETUP_COMPLETED_AT)
        self.assertFalse(self._client().get("/api/setup/status").json()["needs_setup"])

    # ── 判定的健壮性：计数挂了也绝不能误判成"全新" ──

    def test_fingerprint_failure_degrades_to_not_fresh(self):
        """计数查询整体异常 ⇒ db_looks_fresh=False ⇒ 不弹横幅、不分流（宁可少打扰）。"""
        self._recreate_db()
        with mock.patch.object(setup_core, "db_usage_fingerprint",
                               side_effect=RuntimeError("database is locked")):
            body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])

    def test_unknown_table_count_degrades_to_not_fresh(self):
        """单表计数失败（返回 None）⇒ 按"可能在用"处理 ⇒ needs_setup=False。

        方向性：误判成"在用"只少一条横幅；误判成"全新"会把在用系统拽进向导。
        """
        self._recreate_db()

        async def _none(db, *a, **k):
            return None

        with mock.patch.object(setup_core, "_count_rows", _none):
            body = self._client().get("/api/setup/status").json()
        self.assertFalse(body["needs_setup"])

    def test_fingerprint_is_read_only_counts(self):
        """db_usage_fingerprint 只做计数：键集合固定，且调用前后库无任何变化（零写入，
        含 app_meta —— "稍后/判定"绝不能变成隐式安装状态）。"""
        self._recreate_db()
        self._seed_one("assets")

        async def _fingerprint():
            async with self.SessionLocal() as s:
                return await setup_core.db_usage_fingerprint(s)

        async def _snapshot():
            async with self.SessionLocal() as s:
                out = {}
                for t in self.BUSINESS_TABLES + ("users", "app_meta"):
                    out[t] = (await s.execute(
                        sqlalchemy.text("SELECT COUNT(*) FROM " + t))).scalar()
                return out

        before = asyncio.run(_snapshot())
        fp = asyncio.run(_fingerprint())
        after = asyncio.run(_snapshot())

        want = {t: 0 for t in self.BUSINESS_TABLES}
        want["assets"] = 1           # 只 seed 了这一行
        want["users"] = 0            # 空库：还没建管理员
        want["users_default_admin"] = 0
        for key, val in want.items():
            self.assertEqual(fp.get(key), val, key)
        self.assertEqual(set(fp), set(want), "fingerprint 键集合固定，内部契约不漂移")
        self.assertEqual(before, after, "判定过程零写入（含 app_meta）")

    def test_fresh_db_fingerprint_counts_are_all_zero(self):
        """空库指纹：7 张业务表全为 0、users 也是 0（管理员都还没建）。"""
        self._recreate_db()
        fp = asyncio.run(self._fingerprint_only())
        for t in self.BUSINESS_TABLES:
            self.assertEqual(fp[t], 0, t)
        self.assertEqual(fp["users"], 0)

    async def _fingerprint_only(self):
        async with self.SessionLocal() as s:
            return await setup_core.db_usage_fingerprint(s)


class SetupChecksTest(_WizardCase):
    """checks 清单形状与分级（任务书 §4）。"""

    def test_checks_shape_and_required_ids(self):
        patches = self._start(self._patch_probe_custom(notify_configured=False))
        try:
            r = self._client().get("/api/setup/checks")
            self.assertEqual(r.status_code, 200, r.text)
            items = r.json()
            self.assertIsInstance(items, list)
            self.assertGreaterEqual(len(items), 12)
            ids = [i["id"] for i in items]
            self.assertEqual(len(ids), len(set(ids)), "检查项 id 不得重复")
            for it in items:
                self.assertEqual(set(it.keys()), {"id", "title", "level", "detail", "fix"}, it)
                self.assertIn(it["level"], ("ok", "warn", "error"), it)
                self.assertTrue(it["title"] and it["detail"], it)
            for want in ("port_dns", "port_dhcp", "port_tftp", "dnsmasq", "dir_iso", "dir_tftp",
                         "dir_pxe_web", "dir_ztp_web", "dir_state", "dir_dnsmasq_conf",
                         "dir_dnsmasq_leases", "disk_space", "host_reload_unit", "notify"):
                self.assertIn(want, ids)
            items_by_id = {i["id"]: i for i in items}
            # dnsmasq 在跑 ⇒ 三个端口"被 dnsmasq 占用"属正常
            for cid in ("port_dns", "port_dhcp", "port_tftp"):
                self.assertEqual(items_by_id[cid]["level"], "ok", items_by_id[cid])
            # 只读挂载目录（租约/挂载点）不可写不算错
            self.assertEqual(items_by_id["dir_dnsmasq_leases"]["level"], "ok")
            self.assertEqual(items_by_id["dir_mnt"]["level"], "ok")
            # 通知未配置 → 仅 warn（绝不允许是 error）
            self.assertEqual(items_by_id["notify"]["level"], "warn")
        finally:
            self._stop(patches)

    def test_port_occupied_is_error_with_copyable_fix(self):
        """端口被占（dnsmasq 没在跑）→ error + 可复制的处置命令。"""
        occupied = {53: ["udp 0.0.0.0:53"], 67: ["udp 0.0.0.0:67"], 69: ["udp 0.0.0.0:69"]}
        patches = self._start(self._patch_probe_custom(
            listen=occupied, dnsmasq=False, notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            for cid in ("port_dns", "port_dhcp", "port_tftp"):
                self.assertEqual(items[cid]["level"], "error", items[cid])
                self.assertTrue(items[cid]["fix"], "error 项必须带 fix")
            # 53 的 fix 给 systemd-resolved 的标准处置（任务书示例）
            self.assertIn("systemctl disable --now systemd-resolved", items["port_dns"]["fix"])
            # 67/69 给可复制的排查命令
            self.assertIn("grep :67", items["port_dhcp"]["fix"])
            self.assertIn("grep :69", items["port_tftp"]["fix"])
        finally:
            self._stop(patches)

    def test_port_free_is_ok(self):
        patches = self._start(self._patch_probe_custom(notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            for cid in ("port_dns", "port_dhcp", "port_tftp"):
                self.assertEqual(items[cid]["level"], "ok")
                self.assertEqual(items[cid]["fix"], "")
        finally:
            self._stop(patches)

    def test_dnsmasq_not_running_with_conf_is_warn_with_fix(self):
        patches = self._start(self._patch_probe_custom(
            dnsmasq=False, confs=["opstk-pxe.conf"], notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            self.assertEqual(items["dnsmasq"]["level"], "warn", items["dnsmasq"])
            self.assertIn("systemctl enable --now dnsmasq", items["dnsmasq"]["fix"])
        finally:
            self._stop(patches)

    def test_dnsmasq_not_running_without_conf_is_ok(self):
        """没部署过装机（无 opstk 配置）时 dnsmasq 没跑属正常 —— 与 doctor.sh 同口径。"""
        patches = self._start(self._patch_probe_custom(dnsmasq=False, notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            self.assertEqual(items["dnsmasq"]["level"], "ok", items["dnsmasq"])
        finally:
            self._stop(patches)

    def test_dir_not_writable_is_error_with_fix(self):
        patches = self._start(self._patch_probe_custom(
            dirs=self._fake_dirs(writable=False), notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            for cid in ("dir_iso", "dir_tftp", "dir_pxe_web", "dir_ztp_web",
                        "dir_state", "dir_dnsmasq_conf"):
                self.assertEqual(items[cid]["level"], "error", items[cid])
                self.assertIn("chown", items[cid]["fix"], items[cid])
            self.assertEqual(items["dir_dnsmasq_leases"]["level"], "ok")
        finally:
            self._stop(patches)

    def test_disk_low_space_is_error(self):
        patches = self._start(self._patch_probe_custom(
            dirs=self._fake_dirs(free_bytes=1 * 1024**3), notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            self.assertEqual(items["disk_space"]["level"], "error", items["disk_space"])
            self.assertIn("GB", items["disk_space"]["detail"])
        finally:
            self._stop(patches)

    def test_host_reload_unit_reports_evidence_honestly(self):
        """容器判断不了"宿主机是否已 install" ⇒ 只报证据 + 给安装命令（warn 封顶）。"""
        patches = self._start(self._patch_probe_custom(
            host={"files_present": 3, "link_version": 0, "link_err": ""},
            notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            it = items["host_reload_unit"]
            self.assertEqual(it["level"], "warn", it)
            self.assertIn("无法判断", it["detail"])
            self.assertIn("install-opstk-dnsmasq-reload.sh", it["fix"])
        finally:
            self._stop(patches)

    def test_host_reload_unit_ok_when_link_marker_present(self):
        patches = self._start(self._patch_probe_custom(notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            self.assertEqual(items["host_reload_unit"]["level"], "ok")
        finally:
            self._stop(patches)

    def test_notify_unconfigured_is_warn_never_error(self):
        patches = self._start(self._patch_probe_custom(notify_configured=False))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            self.assertEqual(items["notify"]["level"], "warn", items["notify"])
        finally:
            self._stop(patches)

    def test_notify_configured_is_ok(self):
        patches = self._start(self._patch_probe_custom(notify_configured=True))
        try:
            items = {i["id"]: i for i in self._client().get("/api/setup/checks").json()}
            self.assertEqual(items["notify"]["level"], "ok")
        finally:
            self._stop(patches)

    def test_checks_require_admin(self):
        self.assertEqual(self._client(role="operator", username="ops1")
                         .get("/api/setup/checks").status_code, 403)


class SystemStorageTest(_WizardCase):
    """/system/storage 形状与 free/total（任务书 §5）。"""

    def _dirs(self):
        exist_dir = pathlib.Path(self._tmp.name) / "exists"
        exist_dir.mkdir(exist_ok=True)
        missing = pathlib.Path(self._tmp.name) / "nope"
        return (
            ("iso", "ISO 镜像目录", str(exist_dir), True),
            ("tftp", "TFTP 根目录", str(missing), True),
        )

    def test_storage_shape_and_bytes(self):
        patches = self._start((
            mock.patch.object(setup_core, "WIZARD_DIRS", self._dirs()),
            mock.patch.object(setup_core, "_writable_dir_probe", lambda p: (True, "")),
            mock.patch.object(setup_core, "_disk_usage", lambda p: (1000, 400)),
        ))
        try:
            r = self._client().get("/api/system/storage")
            self.assertEqual(r.status_code, 200, r.text)
            got = {d["key"]: d for d in r.json()["dirs"]}
            self.assertEqual(set(got), {"iso", "tftp"})
            iso = got["iso"]
            for k in ("key", "name", "path", "exists", "writable", "writable_required",
                      "free_bytes", "total_bytes"):
                self.assertIn(k, iso)
            self.assertTrue(iso["exists"] and iso["writable"])
            self.assertEqual(iso["total_bytes"], 1000)
            self.assertEqual(iso["free_bytes"], 400)
            tftp = got["tftp"]
            self.assertFalse(tftp["exists"])
            self.assertFalse(tftp["writable"])
            self.assertEqual(tftp["free_bytes"], 0)
            self.assertEqual(tftp["total_bytes"], 0)
        finally:
            self._stop(patches)

    def test_storage_default_paths_are_container_fixed(self):
        """默认清单逐字 = 容器内固定路径（换盘走 compose 变量，向导不改路径）。"""
        paths = {d[2] for d in setup_core.WIZARD_DIRS}
        for p in ("/srv/opstk/iso", "/srv/tftp", "/etc/dnsmasq.d", "/var/lib/dnsmasq",
                  "/srv/opstk/state", "/srv/opstk/pxe-web", "/srv/opstk/ztp-web"):
            self.assertIn(p, paths)
        # 租约目录按部署设计就是 :ro 挂载，"不可写"绝不能当错误
        leases = [d for d in setup_core.WIZARD_DIRS if d[2] == "/var/lib/dnsmasq"][0]
        self.assertFalse(leases[3])

    def test_storage_requires_admin(self):
        self.assertEqual(self._client(role="operator", username="ops1")
                         .get("/api/system/storage").status_code, 403)


class SetupCompleteTest(_WizardCase):
    """complete 的守卫与完成后的关闭（任务书 §6/§7）。"""

    def test_complete_requires_password_changed(self):
        patches = self._start((mock.patch.object(setup_core, "checks", lambda: []),))
        try:
            r = self._client().post("/api/setup/complete")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("口令", r.json()["detail"])
            self.assertIsNone(self._read_meta(setup_core.KEY_SETUP_COMPLETED_AT))
        finally:
            self._stop(patches)

    def test_complete_blocked_by_error_check(self):
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        bad = [{"id": "port_dns", "title": "端口 53（DNS 服务）", "level": "error",
                "detail": "被占用", "fix": "x"}]
        patches = self._start((mock.patch.object(setup_core, "checks", lambda: bad),))
        try:
            r = self._client().post("/api/setup/complete")
            self.assertEqual(r.status_code, 400, r.text)
            self.assertIn("端口 53", r.json()["detail"])
            self.assertIsNone(self._read_meta(setup_core.KEY_SETUP_COMPLETED_AT))
        finally:
            self._stop(patches)

    def test_complete_blocked_lists_password_first_when_both(self):
        bad = [{"id": "port_dns", "title": "端口 53（DNS 服务）", "level": "error",
                "detail": "被占用", "fix": "x"}]
        patches = self._start((mock.patch.object(setup_core, "checks", lambda: bad),))
        try:
            r = self._client().post("/api/setup/complete")
            self.assertEqual(r.status_code, 400, r.text)
            detail = r.json()["detail"]
            self.assertIn("口令", detail)
            self.assertIn("端口 53", detail)
        finally:
            self._stop(patches)

    def test_complete_success_writes_marker(self):
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        patches = self._start((mock.patch.object(setup_core, "checks", lambda: []),))
        try:
            r = self._client().post("/api/setup/complete")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(r.json()["ok"])
            self.assertIsNotNone(self._read_meta(setup_core.KEY_SETUP_COMPLETED_AT))
        finally:
            self._stop(patches)

    def test_after_complete_wizard_is_closed(self):
        """完成后 checks / network / complete 一律 403（防后门）；status 永远可用。"""
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        patches = self._start((mock.patch.object(setup_core, "checks", lambda: []),))
        try:
            c = self._client()
            self.assertEqual(c.post("/api/setup/complete").status_code, 200)
            self.assertEqual(c.get("/api/setup/checks").status_code, 403)
            self.assertEqual(c.get("/api/setup/network").status_code, 403)
            self.assertEqual(c.post("/api/setup/complete").status_code, 403)
            st = c.get("/api/setup/status")
            self.assertEqual(st.status_code, 200)
            self.assertFalse(st.json()["needs_setup"])
        finally:
            self._stop(patches)

    def test_complete_requires_admin(self):
        self.assertEqual(self._client(role="operator", username="ops1")
                         .post("/api/setup/complete").status_code, 403)

    def test_needs_setup_turns_false_after_complete(self):
        """完成向导后 needs_setup 变 false（前端横幅据此消失）。"""
        self._set_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT)
        patches = self._start((mock.patch.object(setup_core, "checks", lambda: []),))
        try:
            c = self._client()
            self.assertTrue(c.get("/api/setup/status").json()["needs_setup"])
            self.assertEqual(c.post("/api/setup/complete").status_code, 200)
            self.assertFalse(c.get("/api/setup/status").json()["needs_setup"])
        finally:
            self._stop(patches)

    def test_password_change_hook_records_meta(self):
        """方案 (a) 埋点：初始管理员改密成功 → admin_password_changed_at 落库。"""
        self._seed_user("admin", "old-pass-1234")
        c = self._auth_client(username="admin")
        r = c.post("/api/auth/password",
                   json={"old_password": "old-pass-1234", "new_password": "brand-new-pw"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNotNone(self._read_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT))

    def test_password_change_by_other_user_does_not_record(self):
        self._seed_user("ops1", "old-pass-1234", role="admin")
        c = self._auth_client(username="ops1")
        r = c.post("/api/auth/password",
                   json={"old_password": "old-pass-1234", "new_password": "another-pass-1"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIsNone(self._read_meta(setup_core.KEY_ADMIN_PASSWORD_CHANGED_AT))


class SetupNetworkTest(_WizardCase):
    """/setup/network 直通 detect_network（admin，只读；完成后关闭）。"""

    FAKE = {"interface": "ens19", "server_ip": "10.0.0.1", "gateway": "10.0.0.254",
            "dhcp_start": "10.0.0.192", "dhcp_end": "10.0.0.253",
            "warnings": ["ip 命令不可用，已改用 /proc/net/route 解析默认路由"]}

    def test_network_passthrough(self):
        from app.it.pxe import server as pxe_server

        with mock.patch.object(pxe_server, "detect_network", lambda: dict(self.FAKE)):
            r = self._client().get("/api/setup/network")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json(), self.FAKE)

    def test_network_requires_admin(self):
        self.assertEqual(self._client(role="operator", username="ops1")
                         .get("/api/setup/network").status_code, 403)


class AnonymousAccessTest(_WizardCase):
    """★ 安全红线：向导接口全部要求登录态，匿名一律 401（钉死"不许匿名接口"）。"""

    def test_all_setup_endpoints_require_login(self):
        c = self._client(override_auth=False)
        for method, path in (("GET", "/api/setup/status"),
                             ("GET", "/api/setup/checks"),
                             ("GET", "/api/setup/network"),
                             ("GET", "/api/system/storage"),
                             ("POST", "/api/setup/complete")):
            with self.subTest(method=method, path=path):
                r = c.request(method, path)
                self.assertEqual(r.status_code, 401, r.text)

    def test_completed_setup_still_requires_login(self):
        """向导关闭后也不例外：匿名连 status 都进不来。"""
        self._set_meta(setup_core.KEY_SETUP_COMPLETED_AT)
        c = self._client(override_auth=False)
        self.assertEqual(c.get("/api/setup/status").status_code, 401)


if __name__ == "__main__":
    unittest.main()
