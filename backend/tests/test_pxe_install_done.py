# -*- coding: utf-8 -*-
"""装机完成标记（防重复抹盘）回归。

背景（真机实证，2026-10-08）：PxeInstall 早有 status / finished_at 两个字段，但整个
后端没有任何地方把 status 置为完成 ⇒ 记录永远 pending；而生成器对**所有**已登记 MAC
都下发「按 MAC 的第二阶段自动装机菜单」 ⇒ 装完的机器重启时 BIOS 先走网卡 → 又被自动
装一遍（抹盘）。真机 VM140 实测只能靠人工把引导顺序改成磁盘优先才停下来。

本单元把修复闭环钉死在四个层面：
  · 生成器（dnsmasq）  ：status 非 pending 的记录不再下发它的 fw-menu 菜单，
    但 dhcp-host 地址预留照发；默认菜单的取反对它也不再生效 ⇒ 它落到
    「未登记默认菜单」（拒绝自动安装并 exit）⇒ 固件按引导顺序引导本地磁盘；
  · 应答文件（ks/user-data）：pending 的机器带装完回调 done_url（RHEL %post /
    Ubuntu late-commands 各一行，|| true 保证回调失败不判装机失败）；
  · 令牌（token）      ：HMAC-SHA256 前 32 hex，同一个 id 恒定、不落库；
  · API（finish/done） ：把记录标记 installed（幂等），并尽力自动重部署；
    重部署失败绝不回滚「已标记完成」。

DB 用临时文件的 sqlite（NullPool），TestClient 每请求独立事件循环；零联网、
零外部副作用（状态目录指向临时目录）。风格照 tests/test_pxe.py（unittest）。
"""
import asyncio
import json
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
from app.it.pxe.generator import PxeConfig, generate_all  # noqa: E402

MAC = "00:11:22:33:44:55"
TAG = "00-11-22-33-44-55"
IP = "10.0.0.140"


def _cfg(**kw):
    kw.setdefault("admin_password", "Test@123")
    kw.setdefault("iso_url", "http://10.0.0.1:8000/pxe/iso/test.iso")
    kw.setdefault("server_ip", "10.0.0.1")
    kw.setdefault("http_root", "http://10.0.0.1:8000/pxe/serve")
    return PxeConfig(**kw)


def _rhel_cfg(**kw):
    kw.setdefault("os_type", "rhel")
    kw.setdefault("os_version", "9.3")
    kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
    return _cfg(**kw)


# ═══════════════════════════ 生成器层 ═══════════════════════════

class PxeMenuSuppressGeneratorTest(unittest.TestCase):
    """status 决定 dnsmasq 是否下发按机的自动装机菜单；文件照旧生成。"""

    def test_pending_install_still_gets_per_mac_menu(self):
        """(a) status=pending ⇒ 该 MAC 的 tag-if=set:fw-menu-<tag> 与对应 dhcp-boot 都在。"""
        inst = [{"mac": MAC, "hostname": "web-01", "ip": IP, "status": "pending"}]
        dns = generate_all(_cfg(), inst)["dnsmasq.conf"]
        self.assertIn(
            "tag-if=set:fw-menu-" + TAG + ",tag:fw-menu,tag:pxe_" + TAG, dns)
        self.assertIn(
            "dhcp-boot=tag:fw-menu-" + TAG + ","
            + "http://10.0.0.1:8000/pxe/serve/boot/" + TAG + ".ipxe", dns)
        # 地址预留照常
        self.assertIn("dhcp-host=" + MAC + "," + IP + ",set:pxe_" + TAG, dns)
        # 默认菜单对该机取反（互斥）：待装机时它不会落到默认菜单
        self.assertIn("tag-if=set:fw-menu-def,tag:fw-menu,tag:!pxe_" + TAG, dns)
        # 兼容：不带 status 键的记录（老调用方）视同 pending
        dns_old = generate_all(_cfg(), [{"mac": MAC, "hostname": "web-01", "ip": IP}])["dnsmasq.conf"]
        self.assertIn("tag-if=set:fw-menu-" + TAG, dns_old)

    def test_installed_install_loses_menu_but_keeps_reservation(self):
        """(b) status=installed ⇒ 不含该 MAC 的 fw-menu 两行；dhcp-host 预留仍在；
        且默认菜单的取反**不含**它 ⇒ 它落到未登记默认菜单 → 引导本地磁盘。"""
        inst = [{"mac": MAC, "hostname": "web-01", "ip": IP,
                 "status": "installed", "finished_at": "2026-10-08 12:00:00"}]
        files = generate_all(_cfg(), inst)
        dns = files["dnsmasq.conf"]
        self.assertNotIn("tag-if=set:fw-menu-" + TAG, dns)
        self.assertNotIn("dhcp-boot=tag:fw-menu-" + TAG, dns)
        # 地址预留要保持稳定：dhcp-host=<mac>,<ip>,set:pxe_<tag> 原样保留
        self.assertIn("dhcp-host=" + MAC + "," + IP + ",set:pxe_" + TAG, dns)
        # 回落路径：fw-menu-def 的取反列表里没有这台机器 ⇒ tag:fw-menu + 无一命中取反
        # ⇒ 它拿到拒绝自动安装的默认菜单，固件按引导顺序继续引导本地磁盘。
        def_line = [ln for ln in dns.splitlines()
                    if ln.startswith("tag-if=set:fw-menu-def")][0]
        self.assertEqual(def_line, "tag-if=set:fw-menu-def,tag:fw-menu", dns)
        # 生成物里要有一行可读的中文说明（为什么这台机器没有菜单）
        self.assertIn(MAC + "：该机已标记完成（finished_at=2026-10-08 12:00:00）", dns)
        # 文件照旧生成（幂等、便于复核），只是不再被下发
        self.assertIn("boot/" + TAG + ".ipxe", files)
        self.assertIn("user-data/" + TAG + "/user-data", files)

    def test_rhel_post_contains_done_callback(self):
        """(c) RHEL ks 的 %post 末尾含正确的 done_url（带 id 与 t）——用真 install_done_url 拼接。"""
        from app.api.pxe import install_done_url
        url = install_done_url("10.0.0.1", "i-abc123")
        self.assertIn("/api/it/pxe/installs/i-abc123/done?t=", url)
        self.assertIn("?t=" + _token_of("i-abc123"), url)
        inst = [{"mac": MAC, "hostname": "web-01", "ip": IP, "status": "pending",
                 "id": "i-abc123", "done_url": url}]
        ks = generate_all(_rhel_cfg(), inst)["ks/" + TAG + "/ks.cfg"]
        post = ks.split("%post --interpreter=/bin/bash", 1)[1]
        self.assertIn("curl -s -m 10 -X POST \"" + url + "\" >/dev/null 2>&1 || true", post)
        # ★ 必须是 POST：真机实测（2026-10-08）第一版发的是 GET，而 /done 只收 POST
        #   ⇒ 回调静默 404、记录永远 pending、机器被反复重装。
        self.assertIn("-X POST", post.split("curl -s -m 10", 1)[1].split("\n", 1)[0])
        # 必须在 %end 之前（%post 段内）
        self.assertLess(post.index("curl -s -m 10"), post.index("%end"))
        # 模板级 ks.cfg（不带 done_url）不得出现回调行 —— 输出与历史逐字一致
        self.assertNotIn("curl -s -m 10", generate_all(_rhel_cfg())["ks.cfg"])

    def test_ubuntu_late_commands_contains_done_callback(self):
        """Ubuntu 侧同样带回调（late-commands，跑在安装环境，不经 curtin in-target）。"""
        import yaml
        from app.api.pxe import install_done_url
        url = install_done_url("10.0.0.1", "i-ubu")
        inst = [{"mac": MAC, "hostname": "web-01", "ip": IP, "status": "pending",
                 "id": "i-ubu", "done_url": url}]
        ud = generate_all(_cfg(), inst)["user-data/" + TAG + "/user-data"]
        cmd = "curl -s -m 10 -X POST \"" + url + "\" >/dev/null 2>&1 || true"
        late = yaml.safe_load(ud)["autoinstall"]["late-commands"]
        self.assertIn(cmd, late)
        # 回调必须先于 reboot，且是 late-commands 的最后一条实际动作之后
        self.assertLess(late.index(cmd), late.index("reboot"))
        self.assertNotIn("curtin in-target", cmd)  # 跑在安装环境（live ISO 才有 curl）

    def test_done_url_rejected_on_injection(self):
        """done_url 进 %post / late-commands 的 shell 双引号：引号/换行必须被生成侧拒绝。"""
        from app.it.pxe.generator import _safe_done_url
        for bad in ('http://x/y"; rm -rf /', "http://x/y\necho hi",
                    "http://x/$(id)", "http://x/y `id`", "ftp://x/y"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _safe_done_url(bad)
        self.assertEqual(_safe_done_url(""), "")       # 空值放行 = 不带回调
        self.assertEqual(
            _safe_done_url("http://10.0.0.1:8000/api/it/pxe/installs/i/done?t=" + "a" * 32),
            "http://10.0.0.1:8000/api/it/pxe/installs/i/done?t=" + "a" * 32)


def _token_of(iid: str) -> str:
    from app.api.pxe import _install_done_token
    return _install_done_token(iid)


class PxeDoneTokenTest(unittest.TestCase):
    """(d) 令牌：同一个 id 恒定、不同 id 不同、固定 32 位 hex。"""

    def test_token_is_stable_per_id_and_distinct_across_ids(self):
        t1a, t1b = _token_of("aaa"), _token_of("aaa")
        t2 = _token_of("bbb")
        self.assertEqual(t1a, t1b)
        self.assertNotEqual(t1a, t2)
        self.assertEqual(len(t1a), 32)
        int(t1a, 16)  # 必须是 hex
        # 不依赖调用顺序：空参/None 邻近 id 也不串
        self.assertNotEqual(_token_of("aaa"), _token_of("aaab"))


# ═══════════════════════════ API 层 ═══════════════════════════

class _DbCase(unittest.TestCase):
    """临时的 sqlite 库 + 覆盖 get_db 的最小 app（不触碰 lifespan / 默认数据）。"""

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        db_path = pathlib.Path(cls._tmp.name) / "done-test.db"
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
        # last-deploy.json 的状态目录指到临时目录：用例不读不写真实 /srv/opstk/state
        self._statedir = tempfile.TemporaryDirectory()
        p = mock.patch.object(self._dhcp_mod(), "HOST_RELOAD_STATE_DIR",
                              self._statedir.name)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._statedir.cleanup)

    @staticmethod
    def _dhcp_mod():
        import app.core.dhcp as dhcp_mod
        return dhcp_mod

    def _client(self):
        app = FastAPI()
        from app.api import pxe as pxe_api
        app.include_router(pxe_api.router, prefix="/api/it/pxe")

        from app.core.auth import get_current_user
        from app.database import get_db

        async def _override_db():
            async with self.SessionLocal() as session:
                yield session

        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        app.dependency_overrides[get_db] = _override_db
        return TestClient(app)

    def _run(self, coro):
        return asyncio.run(coro)

    def _seed(self, status="pending", mac=MAC, ip=IP, profile_kw=None):
        """播种一个模板 + 一条装机记录，返回 (profile_id, install_id)。"""

        async def _go():
            from app.core import crypto
            async with self.SessionLocal() as s:
                pfkw = dict(name="p1", os_type="ubuntu", os_version="22.04")
                pfkw.update(profile_kw or {})
                p = models.PxeProfile(
                    admin_password_enc=crypto.encrypt("Test@123"), **pfkw)
                s.add(p)
                await s.flush()
                inst = models.PxeInstall(
                    profile_id=p.id, hostname="web-01", mac=mac, ip=ip,
                    status=status)
                s.add(inst)
                await s.commit()
                await s.refresh(inst)
                return p.id, inst.id
        return self._run(_go())

    def _db_status(self, iid):
        async def _go():
            async with self.SessionLocal() as s:
                inst = await s.get(models.PxeInstall, iid)
                return inst.status, inst.finished_at
        return self._run(_go())


class PxeInstallDoneApiTest(_DbCase):
    """done / finish 路由与 _gen_pxe_files 的状态回写。"""

    def test_done_route_wrong_token_403_right_token_200_idempotent(self):
        """(e) t 错 → 403；没带 t → 403；t 对 → 200 且记录变 installed；再调仍 200（幂等）。"""
        from app.api.pxe import _install_done_token
        pid, iid = self._seed(status="pending")
        c = self._client()
        url = "/api/it/pxe/installs/" + iid + "/done"
        r = c.post(url + "?t=" + "0" * 32)
        self.assertEqual(r.status_code, 403, r.text)
        r = c.post(url)  # 没带 t 也是 403
        self.assertEqual(r.status_code, 403, r.text)
        # 两次 403 都没有动记录
        self.assertEqual(self._db_status(iid), ("pending", None))

        r = c.post(url + "?t=" + _install_done_token(iid))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "installed")
        self.assertIn("redeploy", body)
        st, fin = self._db_status(iid)
        self.assertEqual(st, "installed")
        self.assertIsNotNone(fin)

        # 幂等：再调一次仍 200，finished_at 保留第一次的时间
        r2 = c.post(url + "?t=" + _install_done_token(iid))
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(self._db_status(iid), (st, fin))

    def test_done_route_also_accepts_get(self):
        """★ 真机回归（2026-10-08）：生成器第一版发的是不带 -X POST 的 curl（= GET），
        而路由只收 POST ⇒ 回调静默 404、记录永远 pending、机器被反复重装。
        现在 /done 两种方法都收（生成侧仍按 POST 发），工件与路由不会再漂移成沉默失效。
        """
        from app.api.pxe import _install_done_token
        pid, iid = self._seed(status="pending")
        c = self._client()
        r = c.get("/api/it/pxe/installs/" + iid + "/done?t=" + _install_done_token(iid))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._db_status(iid)[0], "installed")
        # 令牌仍然照旧校验：GET 也不能免令牌
        pid2, iid2 = self._seed(status="pending", mac="aa:bb:cc:00:00:02")
        r2 = c.get("/api/it/pxe/installs/" + iid2 + "/done?t=" + "0" * 32)
        self.assertEqual(r2.status_code, 403, r2.text)

    def test_done_route_unknown_install_is_404(self):
        """(f) 令牌正确但记录不存在 → 404（403 判定在前，不向无令牌者泄露存在性）。"""
        from app.api.pxe import _install_done_token
        self._seed(status="pending")
        c = self._client()
        r = c.post("/api/it/pxe/installs/no-such-id/done?t=" + _install_done_token("no-such-id"))
        self.assertEqual(r.status_code, 404, r.text)

    def test_finish_route_marks_installed_and_requires_admin(self):
        """finish（admin）同样标记完成；非 admin 403；无上次部署参数时 redeploy 说明跳过。"""
        pid, iid = self._seed(status="pending")
        app = FastAPI()
        from app.api import pxe as pxe_api
        app.include_router(pxe_api.router, prefix="/api/it/pxe")

        from app.core.auth import get_current_user
        from app.database import get_db

        async def _override_db():
            async with self.SessionLocal() as session:
                yield session

        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "viewer"}
        app.dependency_overrides[get_db] = _override_db
        r = TestClient(app).post("/api/it/pxe/installs/" + iid + "/finish")
        self.assertEqual(r.status_code, 403, r.text)
        self.assertEqual(self._db_status(iid), ("pending", None))

        c = self._client()  # admin
        r = c.post("/api/it/pxe/installs/" + iid + "/finish")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["ok"])
        self.assertEqual(self._db_status(iid)[0], "installed")
        # 没写过 last-deploy.json ⇒ 自动重部署只能跳过，且要说清楚原因
        self.assertIn("未找到上次部署参数", r.json()["redeploy"])

    def test_gen_pxe_files_overrides_frontend_installs_with_db_status(self):
        """(g) 前端显式传 installs（pending-only 形状）也必须被库里的 status 盖掉。"""
        pid, _iid = self._seed(status="installed")
        self._run(self._gen_and_check(pid, expect_menu=False, expect_callback=False))

    def test_gen_pxe_files_fallback_without_installs_uses_db_status(self):
        """界面部署按钮固定发 installs:[] ⇒ 回落 DB 记录时同样按 status 抑制菜单。"""
        pid, _iid = self._seed(status="installed")
        self._run(self._gen_and_check(pid, expect_menu=False, expect_callback=False,
                                      installs=[]))

    def test_gen_pxe_files_pending_record_still_gets_menu_and_callback(self):
        """对照组：pending 记录（显式传入）菜单与回调都在 —— 证明 (g) 的抑制确由 status 驱动。"""
        pid, iid = self._seed(status="pending")
        self._run(self._gen_and_check(pid, expect_menu=True, expect_callback=True))

    async def _gen_and_check(self, pid, expect_menu, expect_callback, installs=None):
        from app.api import pxe as pxe_api
        body = {"server_ip": "10.0.0.1",
                "iso_url": "http://10.0.0.1:8000/pxe/iso/test.iso"}
        if installs is None:
            body["installs"] = [{"mac": MAC, "hostname": "web-01"}]  # 前端形状：没有 status
        else:
            body["installs"] = installs
        async with self.SessionLocal() as s:
            files = await pxe_api._gen_pxe_files(pid, body, s)
        dns = files["dnsmasq.conf"]
        if expect_menu:
            self.assertIn("tag-if=set:fw-menu-" + TAG, dns)
        else:
            self.assertNotIn("tag-if=set:fw-menu-" + TAG, dns)
            self.assertNotIn("dhcp-boot=tag:fw-menu-" + TAG, dns)
        self.assertIn("dhcp-host=" + MAC + ",", dns)  # 地址预留始终保留
        ud = files["user-data/" + TAG + "/user-data"]
        if expect_callback:
            self.assertIn("curl -s -m 10", ud)
            self.assertIn("/done?t=", ud)
        else:
            self.assertNotIn("curl -s -m 10", ud)


class PxeDoneRedeployTest(_DbCase):
    """finish/done 的自动重部署：参数来自 last-deploy.json；重部署失败不回滚标记。"""

    def _patch_deploy_env(self):
        """把 _generate_and_deploy 依赖的主机事实钉死：非 Linux（跳过红线守卫）、
        无网络探测、deploy_files 换成可计数的成功桩、ISO 目录换成可自动匹配的名单
        （自动重部署不带 iso_url，Ubuntu 介质要靠 /srv/opstk/iso 自动匹配）。"""
        from app.api import pxe as pxe_api
        from app.it.pxe import server as pxe_server
        calls = []

        def _fake_deploy(files, pid=""):
            calls.append({"pid": pid, "files": files})
            return {"ok": True, "supported": True, "errors": [], "log": [], "scope": pid}

        for target, attr, value in (
            (pxe_server, "detect_network", lambda: {}),
            (pxe_server, "is_linux", lambda: False),
            (pxe_server, "deploy_files", _fake_deploy),
            (pxe_server, "iso_names",
             lambda: ["ubuntu-22.04.5-live-server-amd64.iso"]),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        return calls

    def test_deploy_records_params_and_done_redeploys_without_menu(self):
        """deploy 成功 → last-deploy.json 记下参数 → done 标记完成并自动重部署；
        重部署生成的 dnsmasq **已经不含**该机的装机菜单（闭环的关键一环）。"""
        from app.api import pxe as pxe_api
        from app.core.schemas import PxeGenerateIn
        pid, iid = self._seed(status="pending")
        calls = self._patch_deploy_env()

        async def _deploy():
            async with self.SessionLocal() as s:
                return await pxe_api.deploy_to_host(
                    pid, PxeGenerateIn(server_ip="10.0.0.1",
                                       iso_url="http://10.0.0.1:8000/pxe/iso/test.iso"), s)
        res = self._run(_deploy())
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(calls), 1)

        # last-deploy.json 形如 {"<pid>": {"server_ip": …, "deploy_mode": …}}
        with open(pathlib.Path(self._statedir.name) / "last-deploy.json",
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved[pid], {"server_ip": "10.0.0.1", "deploy_mode": "standalone"})

        from app.api.pxe import _install_done_token
        c = self._client()
        r = c.post("/api/it/pxe/installs/" + iid + "/done?t=" + _install_done_token(iid))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertIn("已自动重部署", r.json()["redeploy"], r.json())
        # 第一次部署（pending）：有菜单；自动重部署（已 installed）：菜单消失
        self.assertEqual(len(calls), 2)
        self.assertIn("tag-if=set:fw-menu-" + TAG, calls[0]["files"]["dnsmasq.conf"])
        self.assertNotIn("tag-if=set:fw-menu-" + TAG, calls[1]["files"]["dnsmasq.conf"])
        self.assertEqual(self._db_status(iid)[0], "installed")

    def test_done_does_not_rollback_mark_when_redeploy_fails(self):
        """重部署抛异常/失败时：标记完成照旧生效，失败原因进 redeploy 字段。"""
        from app.api import pxe as pxe_api
        from app.it.pxe import server as pxe_server
        pid, iid = self._seed(status="pending")
        self._patch_deploy_env()
        pxe_api._save_last_deploy(pid, "10.0.0.1", "standalone")
        # 让重部署在生成阶段炸掉（deploy_files 抛错）：异常必须被吞成 redeploy 说明
        p = mock.patch.object(pxe_server, "deploy_files",
                              mock.Mock(side_effect=RuntimeError("boom")))
        p.start()
        self.addCleanup(p.stop)

        from app.api.pxe import _install_done_token
        c = self._client()
        r = c.post("/api/it/pxe/installs/" + iid + "/done?t=" + _install_done_token(iid))
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body["ok"])
        self.assertEqual(self._db_status(iid)[0], "installed")  # 绝不回滚
        self.assertIn("重部署", body["redeploy"])

    def test_lvm_warning_line_in_deploy_log(self):
        """P2：disk_scheme=lvm 的模板部署成功时，部署日志里有一行固定容量警告。"""
        from app.api import pxe as pxe_api
        from app.core.schemas import PxeGenerateIn
        from app.it.pxe.generator import LVM_SIZE_WARNING
        pid, _iid = self._seed(status="pending",
                               profile_kw={"disk_scheme": "lvm"})
        calls = self._patch_deploy_env()

        async def _deploy():
            async with self.SessionLocal() as s:
                return await pxe_api.deploy_to_host(
                    pid, PxeGenerateIn(server_ip="10.0.0.1",
                                       iso_url="http://10.0.0.1:8000/pxe/iso/test.iso"), s)
        res = self._run(_deploy())
        self.assertTrue(res["ok"], res)
        self.assertIn(LVM_SIZE_WARNING, res["log"])
        # README（给用户看的那份）同样带提示与当前模板警告
        # 2026-10-08：/home 改为随盘增长后，固定部分由 ≈40.4 GB 降到 ≈29.5 GB（30 GB 盘可用），
        # 这里跟着改口径；断言仍钉住"README 里必须出现这个数字"
        self.assertIn("29.5 GB", calls[0]["files"]["README.txt"])
        self.assertIn("【当前模板警告】", calls[0]["files"]["README.txt"])


class PxeInstallResetTest(_DbCase):
    """reset：把已装完的记录退回「待装机」（可重装），并让生成器重新下发装机菜单。

    为什么必须有这条路：标记完成之后运维若要把机器重装一遍（换盘/重做），
    没有它就只能删记录再重建 —— 真实现场一定会撞上。
    """

    def test_reset_installed_back_to_pending_clears_finished_at(self):
        pid, iid = self._seed(status="pending")
        c = self._client()
        r = c.post("/api/it/pxe/installs/" + iid + "/finish")
        self.assertEqual(r.status_code, 200, r.text)
        st, fin = self._db_status(iid)
        self.assertEqual(st, "installed")
        self.assertIsNotNone(fin)

        r2 = c.post("/api/it/pxe/installs/" + iid + "/reset")
        self.assertEqual(r2.status_code, 200, r2.text)
        body = r2.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["status"], "pending")
        self.assertIn("redeploy", body)
        st2, fin2 = self._db_status(iid)
        self.assertEqual(st2, "pending")
        # 退回待装机必须清掉完成时间，否则界面上会出现「待装机 + 完成时间」这种自相矛盾的行
        self.assertIsNone(fin2)

    def test_reset_is_idempotent_when_already_pending(self):
        pid, iid = self._seed(status="pending")
        c = self._client()
        r = c.post("/api/it/pxe/installs/" + iid + "/reset")
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self._db_status(iid)[0], "pending")

    def test_reset_unknown_install_is_404(self):
        self._seed(status="pending")
        c = self._client()
        r = c.post("/api/it/pxe/installs/no-such-id/reset")
        self.assertEqual(r.status_code, 404, r.text)

    def test_generator_emits_install_menu_again_after_reset(self):
        """判据：installed 时不下发按 MAC 的装机菜单；退回 pending 后**必须回来**
        —— 这才是"reset 之后重装能生效"，而不只是数据库里一个字段变了。"""
        from app.it.pxe.generator import generate_all
        off = generate_all(_rhel_cfg(), [{"mac": MAC, "hostname": "web-01", "ip": IP,
                                          "status": "installed"}])["dnsmasq.conf"]
        self.assertNotIn("tag-if=set:fw-menu-" + TAG, off)
        self.assertIn("dhcp-host=" + MAC.lower() + "," + IP, off)
        on = generate_all(_rhel_cfg(), [{"mac": MAC, "hostname": "web-01", "ip": IP,
                                         "status": "pending"}])["dnsmasq.conf"]
        self.assertIn("tag-if=set:fw-menu-" + TAG, on)
        self.assertIn("dhcp-boot=tag:fw-menu-" + TAG, on)


class PxeServeBindingWarningTest(_DbCase):
    """生成侧绑卡警告（2026-10-08 补）：/generate 与 /download 没有部署侧那道 fail-closed
    守卫，产物是要交给别人落地的（离线 ZIP、手工安装）—— 不能硬拒，但绝不能沉默，
    否则 dnsmasq 的 interface= 会沿用模板里的**客户端**网卡名，落到别的机器上
    就可能把 DHCP 开在非装机网段。"""

    def _rocky_profile(self):
        return self._seed(status="pending", profile_kw={
            "os_type": "rocky", "os_version": "9.4",
            "mirror": "http://mirror.example/rocky/9/BaseOS/x86_64/os/"})

    def test_helper_branches(self):
        from app.api.pxe import _serve_binding_warning
        # ① 没给 server_ip：警告里必须点名"客户端网卡名"与具体网卡名
        w1 = _serve_binding_warning("", "ens18", {}, True)
        self.assertIn("客户端网卡名", w1)
        self.assertIn("ens18", w1)
        # ② 给了 server_ip 但本机没有网卡持有它
        w2 = _serve_binding_warning("192.168.199.1", "ens18", {}, True)
        self.assertIn("192.168.199.1", w2)
        self.assertIn("没有网卡持有该地址", w2)
        # ③ 绑卡查到了 → 无话可说
        self.assertEqual(_serve_binding_warning(
            "192.168.199.1", "ens18",
            {"interface": "ens19", "ip": "192.168.199.1", "prefixlen": 24}, True), "")
        # ④ 非 Linux：查不到网卡是常态，警告只会变噪音（与部署侧口径一致）
        self.assertEqual(_serve_binding_warning("", "ens18", {}, False), "")

    def test_readme_carries_warning_only_when_set(self):
        off = generate_all(_rhel_cfg())["README.txt"]
        self.assertNotIn("【服务端绑卡警告】", off)
        on = generate_all(_rhel_cfg(warn_serve_binding="测试警告正文"))["README.txt"]
        self.assertIn("【服务端绑卡警告】", on)
        self.assertIn("测试警告正文", on)

    def test_generate_returns_warning_and_download_zip_carries_it(self):
        import io
        import zipfile

        from app.api import pxe as pxe_api
        pid, iid = self._rocky_profile()
        c = self._client()
        with mock.patch.object(pxe_api.pxe_server, "serve_binding", lambda ip: {}), \
                mock.patch.object(pxe_api.pxe_server, "is_linux", lambda: True):
            r = c.post("/api/it/pxe/profiles/" + pid + "/generate",
                       json={"kernel_path": "rocky/9.4/vmlinuz", "initrd_path": "rocky/9.4/initrd.img"})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertTrue(body.get("warnings"), body.get("warnings"))
            self.assertIn("客户端网卡名", body["warnings"][0])
            self.assertIn("【服务端绑卡警告】", body["files"]["README.txt"])

            d = c.post("/api/it/pxe/profiles/" + pid + "/download",
                       json={"kernel_path": "rocky/9.4/vmlinuz", "initrd_path": "rocky/9.4/initrd.img"})
            self.assertEqual(d.status_code, 200, d.text)
            names = zipfile.ZipFile(io.BytesIO(d.content)).namelist()
            self.assertIn("WARNINGS-1.txt", names)

        # 绑卡能查到时不产生警告（输出回到常态）
        with mock.patch.object(pxe_api.pxe_server, "serve_binding",
                               lambda ip: {"interface": "ens19", "ip": ip,
                                           "netmask": "255.255.255.0", "prefixlen": 24}), \
                mock.patch.object(pxe_api.pxe_server, "is_linux", lambda: True):
            r2 = c.post("/api/it/pxe/profiles/" + pid + "/generate",
                        json={"server_ip": "192.168.199.1"})
            self.assertEqual(r2.status_code, 200, r2.text)
            # 绑卡没问题时**不该出现绑卡警告**（用 membership 而不是整体相等：
            # 2026-10-09 起 RHEL 系还可能带一条"仓库集合里没有 AppStream"的静默少装警告，
            # 本用例的 mirror 形如 …/BaseOS/x86_64/os/ ⇒ 那条警告是**预期内**的正确行为）。
            self.assertFalse([w for w in (r2.json().get("warnings") or []) if "绑卡" in w],
                             r2.json().get("warnings"))
            self.assertNotIn("【服务端绑卡警告】", r2.json()["files"]["README.txt"])


class PxeLastDeployCleanupTest(_DbCase):
    """「上次部署参数」条目的清理：模板删了就不该再留着（否则 state 越堆越多）。"""

    def _tmp_path(self):
        d = tempfile.mkdtemp()
        return str(pathlib.Path(d) / "last-deploy.json")

    def test_delete_profile_drops_entry(self):
        from app.api import pxe as pxe_api
        pid, iid = self._seed(status="pending")
        path = self._tmp_path()
        pathlib.Path(path).write_text(
            json.dumps({pid: {"server_ip": "192.168.199.1", "deploy_mode": "standalone"}}),
            encoding="utf-8")
        c = self._client()
        with mock.patch.object(pxe_api, "_last_deploy_path", lambda: path):
            r = c.delete("/api/it/pxe/profiles/" + pid)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertNotIn(pid, json.loads(pathlib.Path(path).read_text(encoding="utf-8")))

    def test_auto_redeploy_reports_deleted_profile_and_cleans_entry(self):
        from app.api import pxe as pxe_api
        path = self._tmp_path()
        pathlib.Path(path).write_text(
            json.dumps({"gone-profile": {"server_ip": "10.0.0.1", "deploy_mode": "standalone"}}),
            encoding="utf-8")

        async def _go():
            async with self.SessionLocal() as s:
                with mock.patch.object(pxe_api, "_last_deploy_path", lambda: path):
                    return await pxe_api._auto_redeploy("gone-profile", s)

        msg = self._run(_go())
        self.assertIn("模板已删除", msg)
        self.assertNotIn("gone-profile",
                         json.loads(pathlib.Path(path).read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
