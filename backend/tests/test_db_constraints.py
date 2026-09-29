# -*- coding: utf-8 -*-
"""外部审查 U7-F9 / F10 / F12 的库级与接口级回归。

U7-F9  同一模板（profile/template）下同一 MAC 只能有一条记录 —— 改前只有普通索引，
       生成器按 MAC 展开文件 key 与 `dhcp-host=` 行，两条记录会互相**静默覆盖**。
       这一条对**存量库**要靠 _ensure_additive_indexes 补索引（create_all 不补），
       所以这里连"库里已有重复数据时不让应用起不来"这条迁移行为一起测。
U7-F10 CredentialIn/AssetIn 的 port 范围、category 白名单、mac 格式。
U7-F12 凭据被删而资产还指着它（悬挂引用）时，巡检要说出真实原因，而不是拿空口令去连。

DB 用临时文件的 sqlite（NullPool）：TestClient 的每个请求跑在自己的事件循环里，
池化连接会绑死在创建它的循环上，所以连接即用即建。
"""
import asyncio
import json
import logging
import pathlib
import sys
import tempfile
import unittest

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import models  # noqa: E402
from app.database import _ADDITIVE_UNIQUE_INDEXES, _ensure_additive_indexes  # noqa: E402


class _DbCase(unittest.TestCase):
    """临时的 sqlite 库 + 覆盖 get_db 的最小 app（不触碰 lifespan / 默认数据）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(cls._tmp.name) / "constraints-test.db"
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

    def _client(self, router, prefix):
        app = FastAPI()
        app.include_router(router, prefix=prefix)

        from app.core.auth import get_current_user
        from app.database import get_db

        async def _override_db():
            async with self.SessionLocal() as session:
                yield session

        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        app.dependency_overrides[get_db] = _override_db
        return TestClient(app)

    def _pxe_client(self):
        from app.api import pxe as pxe_api
        return self._client(pxe_api.router, "/api/it/pxe")

    def _ztp_client(self):
        from app.api import ztp as ztp_api
        return self._client(ztp_api.router, "/api/ct/ztp")

    def _assets_client(self):
        from app.api import assets as assets_api
        # 生产挂载：api_prefix("/api") + assets 路由自身前缀("/assets")
        return self._client(assets_api.router, "/api/assets")

    def _run(self, coro):
        return asyncio.run(coro)


class PxeInstallMacUniqueTest(_DbCase):
    """U7-F9（PXE 侧）：同一模板内 MAC 唯一，跨模板允许（同一台机器改天换模板重装）。"""

    def _seed_profile(self, name="p1"):
        async def _go():
            async with self.SessionLocal() as s:
                p = models.PxeProfile(name=name, os_type="ubuntu")
                s.add(p)
                await s.commit()
                await s.refresh(p)
                return p.id
        return self._run(_go())

    def test_duplicate_mac_in_same_profile_is_409(self):
        pid = self._seed_profile()
        c = self._pxe_client()
        body = {"profile_id": pid, "hostname": "web-01", "mac": "00:11:22:33:44:55"}
        self.assertEqual(c.post("/api/it/pxe/installs", json=body).status_code, 200)
        r = c.post("/api/it/pxe/installs", json=body)
        self.assertEqual(r.status_code, 409, r.text)
        self.assertIn("已经登记过", r.json()["detail"])

    def test_same_mac_in_another_profile_is_allowed(self):
        pid1 = self._seed_profile("p1")
        pid2 = self._seed_profile("p2")
        c = self._pxe_client()
        mac = "00:11:22:33:44:55"
        self.assertEqual(c.post("/api/it/pxe/installs", json={
            "profile_id": pid1, "hostname": "a", "mac": mac}).status_code, 200)
        self.assertEqual(c.post("/api/it/pxe/installs", json={
            "profile_id": pid2, "hostname": "a", "mac": mac}).status_code, 200)

    def test_empty_mac_is_rejected_by_the_api_but_excluded_from_uniqueness(self):
        """PXE 装机记录**必须**给 MAC（`PxeInstallIn._check_mac` 用 `_require_mac`）。

        唯一索引上的 `WHERE mac <> ''` 仍然必要：存量库里可能有空 MAC 的历史行
        （列是 NOT NULL 但没有格式约束），不能让它们互相撞唯一性。
        这里两头都钉住：接口 422；DB 层两条空 MAC 能共存。
        """
        pid = self._seed_profile()
        c = self._pxe_client()
        for i in range(2):
            r = c.post("/api/it/pxe/installs", json={
                "profile_id": pid, "hostname": "h%d" % i, "mac": ""})
            self.assertEqual(r.status_code, 422, r.text)

        async def _seed_empty():
            async with self.engine.begin() as conn:
                for i in range(2):
                    await conn.execute(text(
                        "INSERT INTO pxe_installs (id, profile_id, hostname, mac, status, "
                        "log, created_at) VALUES (:i, :p, 'h', '', 'pending', '', '2026-01-01')"),
                        {"i": "e%d" % i, "p": pid})
        self._run(_seed_empty())


class ZtpDeviceMacUniqueTest(_DbCase):
    """U7-F9（ZTP 侧）：同一模板内非空 MAC 唯一。"""

    def _seed_template(self, name="t1"):
        async def _go():
            async with self.SessionLocal() as s:
                t = models.ZtpTemplate(name=name, vendor="h3c")
                s.add(t)
                await s.commit()
                await s.refresh(t)
                return t.id
        return self._run(_go())

    def test_duplicate_mac_in_same_template_is_409(self):
        tid = self._seed_template()
        c = self._ztp_client()
        body = {"template_id": tid, "hostname": "sw1", "mac": "aa:bb:cc:dd:ee:01"}
        self.assertEqual(c.post("/api/ct/ztp/devices", json=body).status_code, 200)
        r = c.post("/api/ct/ztp/devices", json=body)
        self.assertEqual(r.status_code, 409, r.text)

    def test_duplicate_empty_mac_is_allowed(self):
        tid = self._seed_template()
        c = self._ztp_client()
        for i in range(3):
            r = c.post("/api/ct/ztp/devices", json={
                "template_id": tid, "hostname": "sw%d" % i, "mac": ""})
            self.assertEqual(r.status_code, 200, r.text)


class AdditiveIndexMigrationTest(_DbCase):
    """存量库补唯一索引：干净库建得上，脏库只告警、不让应用起不来。"""

    def _index_exists(self, name):
        async def _go():
            async with self.engine.begin() as conn:
                row = (await conn.execute(text(
                    "SELECT 1 FROM sqlite_master WHERE type='index' AND name=:n"),
                    {"n": name})).first()
                return bool(row)
        return self._run(_go())

    def _run_migration(self):
        async def _go():
            async with self.engine.begin() as conn:
                await _ensure_additive_indexes(conn)
        self._run(_go())

    def test_clean_db_gets_the_index(self):
        self._drop_index("uq_pxe_install_profile_mac")
        self.assertFalse(self._index_exists("uq_pxe_install_profile_mac"))
        self._run_migration()
        self.assertTrue(self._index_exists("uq_pxe_install_profile_mac"))

    def _drop_index(self, name):
        async def _go():
            async with self.engine.begin() as conn:
                await conn.execute(text("DROP INDEX IF EXISTS %s" % name))
        self._run(_go())

    def test_dirty_db_is_left_alone_but_logged(self):
        """库里已有重复 MAC 时：不建索引（否则启动就崩），但把冲突行打出来。"""
        name = "uq_ztp_dev_template_mac"
        self._drop_index(name)

        async def _seed_dup():
            async with self.engine.begin() as conn:
                for i in range(2):
                    await conn.execute(text(
                        "INSERT INTO ztp_devices (id, template_id, hostname, mac, serial, "
                        "mgmt_ip, created_at) VALUES (:i, 't1', 'sw', 'aa:bb:cc:dd:ee:01', "
                        "'', NULL, '2026-01-01')"), {"i": "d%d" % i})
        self._run(_seed_dup())

        with self.assertLogs("app.database", level=logging.WARNING) as cm:
            self._run_migration()
        self.assertIn("重复 MAC", "\n".join(cm.output))
        self.assertFalse(self._index_exists(name), "有重复数据时不应建唯一索引")

        # 清理干净后下一次启动就补上
        async def _clean():
            async with self.engine.begin() as conn:
                await conn.execute(text("DELETE FROM ztp_devices WHERE id = 'd1'"))
        self._run(_clean())
        self._run_migration()
        self.assertTrue(self._index_exists(name))

    def test_index_names_match_the_model_declarations(self):
        """迁移用的索引名/列必须与模型声明一致，否则模型与存量库会各有一套索引。"""
        declared = {ix.name for t in models.Base.metadata.sorted_tables for ix in t.indexes}
        for name, _table, _cols in _ADDITIVE_UNIQUE_INDEXES:
            self.assertIn(name, declared)


class AssetCredentialInputTest(_DbCase):
    """U7-F10：port 范围 / category 白名单 / mac 格式。"""

    def test_port_range(self):
        from app.core.schemas import AssetIn, CredentialIn
        for bad in (0, -1, 65536, 99999):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    CredentialIn(name="c", username="u", port=bad)
                with pytest.raises(ValueError):
                    AssetIn(name="a", category="ct", host="10.0.0.1", port=bad)
        self.assertEqual(CredentialIn(name="c", username="u", port=2222).port, 2222)
        self.assertEqual(AssetIn(name="a", category="ct", host="h", port=65535).port, 65535)

    def test_category_whitelist_and_normalization(self):
        from app.core.schemas import AssetIn
        self.assertEqual(AssetIn(name="a", category="CT", host="h").category, "ct")
        self.assertEqual(AssetIn(name="a", category=" it ", host="h").category, "it")
        with pytest.raises(ValueError) as e:
            AssetIn(name="a", category="windows", host="h")
        self.assertIn("category", str(e.value))

    def test_mac_is_normalized_and_empty_becomes_none(self):
        from app.core.schemas import AssetIn
        for raw in ("AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AA:BB:CC:DD:EE:FF"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    AssetIn(name="a", category="ct", host="h", mac=raw).mac,
                    "aa:bb:cc:dd:ee:ff")
        self.assertIsNone(AssetIn(name="a", category="ct", host="h", mac="").mac)
        with pytest.raises(ValueError):
            AssetIn(name="a", category="ct", host="h", mac="aa-bb-cc")

    def test_api_creates_normalized_asset_and_filters_case_insensitively(self):
        c = self._assets_client()
        r = c.post("/api/assets", json={"name": "core-sw", "category": "CT",
                                        "host": "10.0.0.1", "port": 22,
                                        "mac": "AA-BB-CC-DD-EE-FF"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["category"], "ct")
        self.assertEqual(r.json()["mac"], "aa:bb:cc:dd:ee:ff")
        # 筛选侧同样归一小写：?category=CT 不能静默返回空列表
        got = c.get("/api/assets?category=CT").json()
        self.assertEqual([a["name"] for a in got], ["core-sw"])

    def test_api_rejects_bad_port_and_category(self):
        c = self._assets_client()
        self.assertEqual(c.post("/api/assets", json={
            "name": "a", "category": "ct", "host": "h", "port": 99999}).status_code, 422)
        self.assertEqual(c.post("/api/assets", json={
            "name": "a", "category": "windows", "host": "h"}).status_code, 422)
        self.assertEqual(c.post("/api/assets/credentials", json={
            "name": "c", "username": "u", "port": 0}).status_code, 422)


class DanglingCredentialTest(_DbCase):
    """U7-F12：凭据被删而资产仍指着它 —— 巡检要说出真实原因。"""

    def test_inspect_reports_missing_credential_instead_of_empty_login(self):
        from app.ct import inspection
        from app.ct.inspection import service as insp

        async def _go():
            async with self.SessionLocal() as s:
                # 直接塞一个指向不存在凭据的资产：模拟"改前没有外键约束时留下的悬挂引用"
                a = models.Asset(name="sw1", category="ct", host="10.0.0.1",
                                 vendor="h3c", credential_id="ghost-id")
                s.add(a)
                await s.commit()
                await s.refresh(a)
                out = await insp.inspect_one(s, a)
                return a.id, out
        _aid, out = self._run(_go())
        self.assertEqual(out["status"], "failed")
        self.assertIn("凭据已不存在", out["error"])
        self.assertIn("ghost-id", out["error"])
        _ = inspection   # 只是让 import 顺序与生产一致
