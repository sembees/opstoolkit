# -*- coding: utf-8 -*-
"""J：`/pxe/serve` 的 token 门禁（外部审查第 J 条）。

三组判据：
  A. **默认不改变行为**：`.env` 里没配 token 时，这个静态根与改造前**逐字相同**（谁都能取）。
     这一条最重要 —— 一次升级不该让正在装机的机器突然取不到文件。
  B. **配了 token 就必须带**：路径段（生成器默认写法）与查询串（浏览器/手工核对）都认；
     错的/缺的都要 403；**目录穿越仍然被挡住**（不能因为加了门禁就把 StaticFiles 的
     归一化丢掉 —— 那正是我选择"包一层"而不是自己写 FileResponse 的原因）。
  C. **token 会进入生成的 URL**：`serve_base()` 是 http_root 的唯一来源，
     所以内核/initrd/应答文件/菜单整条链路的 URL 都自动带上；`_local_served_path()`
     必须能在带 token 的 URL 与真实目录之间来回映射。

★ 测试卫生：`crypto._ENV_PATH` / `_LOCK_PATH` 指向**真实**的 `/app/backend/.env`
（容器里就是生产那份，bind mount）。任何会写 .env 的用例**必须**先把这两个路径
换成临时文件 —— 否则跑一次用例就把生产密钥文件改了。
"""
import os
import tempfile
import unittest
from unittest import mock


class ServeTokenPrimitivesTest(unittest.TestCase):
    TOK = "LzQ7Wm2pXk9tRv4bNc8sYd1f"

    def test_default_is_open_and_token_ok_matrix(self):
        from app.config import settings
        from app.core import serve_token

        with mock.patch.object(settings, "pxe_serve_token", ""):
            self.assertEqual(serve_token.get_token(), "")
            # A：没配 token ⇒ 一律放行（与改造前一致）
            for provided in ("", "whatever", "短"):
                self.assertTrue(serve_token.token_ok(provided), provided)
            self.assertEqual(serve_token.serve_base("10.0.0.1"),
                             "http://10.0.0.1:8000/pxe/serve")
            # 没配时不该动路径
            self.assertEqual(serve_token.take_from_path("/ks.cfg"), ("/ks.cfg", ""))
        with mock.patch.object(settings, "pxe_serve_token", self.TOK):
            self.assertTrue(serve_token.token_ok(self.TOK))
            self.assertTrue(serve_token.token_ok(" " + self.TOK + " "))   # 容错两侧空白
            for bad in ("", "x", self.TOK[:-1], self.TOK + "x", self.TOK.upper()):
                self.assertFalse(serve_token.token_ok(bad), bad)
            self.assertEqual(serve_token.serve_base("10.0.0.1"),
                             "http://10.0.0.1:8000/pxe/serve/" + self.TOK)

    def test_take_from_path_only_strips_the_first_segment(self):
        from app.config import settings
        from app.core import serve_token

        with mock.patch.object(settings, "pxe_serve_token", self.TOK):
            got = serve_token.take_from_path("/%s/ks.cfg" % self.TOK)
            self.assertEqual(got, ("/ks.cfg", self.TOK))
            got = serve_token.take_from_path("/%s/profiles/ab/boot.ipxe" % self.TOK)
            self.assertEqual(got, ("/profiles/ab/boot.ipxe", self.TOK))
            # 第一段不是 token（查询串写法）⇒ 原样返回，且**不能**把路径吃掉
            self.assertEqual(serve_token.take_from_path("/ks.cfg"), ("/ks.cfg", ""))
            self.assertEqual(serve_token.take_from_path("/profiles/ab/x"), ("/profiles/ab/x", ""))

    def test_from_query(self):
        from app.core import serve_token
        self.assertEqual(serve_token.from_query("t=abc&x=1"), "abc")
        self.assertEqual(serve_token.from_query(b"t=abc"), "abc")
        self.assertEqual(serve_token.from_query("x=1"), "")
        self.assertEqual(serve_token.from_query(""), "")

    def test_serve_url_and_local_path_round_trip_with_token(self):
        """生成出去的是带 token 的 URL，映射回本地目录时必须剥掉 token 段。"""
        from app.config import settings
        from app.api import pxe as pxe_api

        with mock.patch.object(settings, "pxe_serve_token", self.TOK):
            url = pxe_api._serve_url("/srv/opstk/pxe-web/repo/rocky-9.4", "10.0.0.1")
            self.assertEqual(
                url, "http://10.0.0.1:8000/pxe/serve/%s/repo/rocky-9.4/" % self.TOK)
            self.assertEqual(pxe_api._local_served_path(url),
                             "/srv/opstk/pxe-web/repo/rocky-9.4")
            # 老的无 token URL 也照样能映射（升级过程中库里存的 mirror 是旧的）
            self.assertEqual(
                pxe_api._local_served_path("http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/"),
                "/srv/opstk/pxe-web/repo/rocky-9.4")
            # 穿越仍然挡掉
            self.assertEqual(pxe_api._local_served_path(
                "http://10.0.0.1:8000/pxe/serve/%s/../../etc/passwd" % self.TOK), "")

    def test_set_token_rejects_short_values(self):
        from app.core import serve_token
        self._temp_env()
        with self.assertRaises(ValueError):
            serve_token.set_token("short")

    def test_ensure_token_generates_and_persists(self):
        from app.config import settings
        from app.core import serve_token

        self._temp_env()
        with mock.patch.object(settings, "pxe_serve_token", ""):
            tok = serve_token.ensure_token()
            self.assertGreaterEqual(len(tok), serve_token.MIN_LEN)
            self.assertIn("pxe_serve_token=" + tok, self._env_text())
            # 再调一次不换（幂等）—— 换 token 会让正在装机的机器取不到文件
            self.assertEqual(serve_token.ensure_token(), tok)

    # ── 辅助：把 .env 的读写引到临时文件（**绝不能**碰真实那份） ──

    def _temp_env(self):
        """把 crypto 的 .env 路径指向临时文件。

        ★ 真实路径是 `/app/backend/.env`（容器里就是生产那份，且是 bind mount）——
          不换掉就跑用例，等于用测试改生产密钥文件。
        """
        import pathlib
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = os.path.join(tmp.name, ".env")
        open(env, "w", encoding="utf-8").close()
        self._env_path = env
        for target, value in (("app.core.crypto._ENV_PATH", pathlib.Path(env)),
                              ("app.core.crypto._LOCK_PATH", pathlib.Path(tmp.name + "/.env.lock"))):
            p = mock.patch(target, value)
            p.start()
            self.addCleanup(p.stop)

    def _env_text(self):
        with open(self._env_path, encoding="utf-8") as fh:
            return fh.read()


class TokenStaticGateTest(unittest.TestCase):
    """真的挂一个 StaticFiles 起来打请求（ASGI 路径、目录穿越、403 都覆盖）。"""

    TOK = "LzQ7Wm2pXk9tRv4bNc8sYd1f"

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.main import _TokenStatic

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with open(os.path.join(tmp.name, "ks.cfg"), "w", encoding="utf-8") as fh:
            fh.write("rootpw --iscrypted $6$SECRET\n")
        os.makedirs(os.path.join(tmp.name, "repo", "x"))
        with open(os.path.join(tmp.name, "repo", "x", "repomd.xml"), "w") as fh:
            fh.write("<repodata/>\n")
        app = FastAPI()
        app.mount("/pxe/serve", _TokenStatic(directory=tmp.name))
        return TestClient(app)

    def test_open_when_no_token_configured(self):
        """A：没配 token ⇒ 与改造前一样，任何路径都能取（不改变既有部署的行为）。"""
        from app.config import settings
        client = self._client()
        with mock.patch.object(settings, "pxe_serve_token", ""):
            r = client.get("/pxe/serve/ks.cfg")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("SECRET", r.text)
            self.assertEqual(client.get("/pxe/serve/repo/x/repomd.xml").status_code, 200)

    def test_requires_token_when_configured(self):
        from app.config import settings
        client = self._client()
        with mock.patch.object(settings, "pxe_serve_token", self.TOK):
            # 缺 token / 错 token ⇒ 403（而不是 404 —— 要能区分"被门禁挡住"与"文件不存在"）
            self.assertEqual(client.get("/pxe/serve/ks.cfg").status_code, 403)
            self.assertEqual(client.get("/pxe/serve/ks.cfg?t=wrong").status_code, 403)
            self.assertEqual(client.get("/pxe/serve/wrong/ks.cfg").status_code, 403)
            # 路径段写法（生成器默认）：必须把 token 段摘掉后再找文件
            r = client.get("/pxe/serve/%s/ks.cfg" % self.TOK)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("SECRET", r.text)
            # 查询串写法（浏览器方便）
            r = client.get("/pxe/serve/ks.cfg?t=%s" % self.TOK)
            self.assertEqual(r.status_code, 200, r.text)
            # 子目录也照常
            self.assertEqual(
                client.get("/pxe/serve/%s/repo/x/repomd.xml" % self.TOK).status_code, 200)

    def test_traversal_is_still_blocked_with_token(self):
        """加门禁**不能**把 StaticFiles 原有的穿越防护弄丢。"""
        from app.config import settings
        client = self._client()
        with mock.patch.object(settings, "pxe_serve_token", self.TOK):
            for path in ("/%s/../.env" % self.TOK,
                         "/%s/..%%2f.env" % self.TOK,
                         "/%s/repo/../../.env" % self.TOK):
                with self.subTest(path=path):
                    r = client.get("/pxe/serve" + path)
                    self.assertIn(r.status_code, (400, 403, 404), r.text)
                    self.assertNotIn("pxe_serve_token", r.text)


class ZtpScopeTest(unittest.TestCase):
    """J 的第二个落点：`/ztp`（设备配置里是**明文**口令，比 PXE 那侧更敏感）。

    与 PXE 侧共用一套实现（`serve_token.StaticScope`），差别只在：
      · `http_root` 是模板里存好的**完整 URL**（不是从 IP 拼的）⇒ 用 `with_token()` 接；
      · 所有 ZTP 生成/部署/下载都经过 `_gen_ztp_files` 这一处，token 只在那里接一次。
    """

    TOK = "Zt9Qw3pLm7Xr5bNc1sYd4fGh"

    def test_with_token_idempotent_and_scoped(self):
        from app.config import settings
        from app.core import serve_token

        with mock.patch.object(settings, "ztp_serve_token", self.TOK):
            want = "http://10.0.0.1:8000/ztp/" + self.TOK
            self.assertEqual(serve_token.ZTP.with_token("http://10.0.0.1:8000/ztp"), want)
            self.assertEqual(serve_token.ZTP.with_token("http://10.0.0.1:8000/ztp/"), want)
            self.assertEqual(serve_token.ZTP.with_token(want), want)          # 幂等
            self.assertEqual(serve_token.ZTP.with_token(want + "/"), want)
            # 不是本 scope 的根 ⇒ 原样返回（别把别人的 URL 改坏）
            self.assertEqual(serve_token.ZTP.with_token("http://10.0.0.1:8000/pxe/serve"),
                             "http://10.0.0.1:8000/pxe/serve")
            self.assertEqual(serve_token.ZTP.with_token("http://x/other"), "http://x/other")
            self.assertEqual(serve_token.ZTP.with_token(""), "")
            # 两个 scope 的开关互不影响
            self.assertEqual(serve_token.PXE.get_token(), "")
            self.assertEqual(serve_token.ZTP.get_token(), self.TOK)

    def test_default_off_keeps_old_behaviour(self):
        from app.config import settings
        from app.core import serve_token
        with mock.patch.object(settings, "ztp_serve_token", ""):
            self.assertEqual(serve_token.ZTP.with_token("http://10.0.0.1:8000/ztp"),
                             "http://10.0.0.1:8000/ztp")
            self.assertTrue(serve_token.ZTP.token_ok(""))

    def test_ztp_gate_403_without_token_200_with(self):
        """真的把 `/ztp` 挂起来打请求（含路径段与查询串两种写法）。"""
        import os
        import tempfile

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.config import settings
        from app.core import serve_token
        from app.main import _TokenStatic

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        os.makedirs(os.path.join(tmp.name, "ztp"))
        with open(os.path.join(tmp.name, "ztp", "SW1.cfg"), "w", encoding="utf-8") as fh:
            fh.write("password simple MINGWEN-SECRET\n")
        app = FastAPI()
        app.mount("/ztp", _TokenStatic(directory=tmp.name, scope=serve_token.ZTP))
        client = TestClient(app)
        with mock.patch.object(settings, "ztp_serve_token", self.TOK):
            self.assertEqual(client.get("/ztp/ztp/SW1.cfg").status_code, 403)
            self.assertEqual(client.get("/ztp/ztp/SW1.cfg?t=wrong").status_code, 403)
            r = client.get("/ztp/%s/ztp/SW1.cfg" % self.TOK)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("MINGWEN-SECRET", r.text)
            r = client.get("/ztp/ztp/SW1.cfg?t=%s" % self.TOK)
            self.assertEqual(r.status_code, 200, r.text)
        # 关掉之后与改造前一样（不校验）
        with mock.patch.object(settings, "ztp_serve_token", ""):
            self.assertEqual(client.get("/ztp/ztp/SW1.cfg").status_code, 200)

    def test_generated_artifacts_carry_the_token(self):
        """生成侧：给带 token 的 http_root，产物里的 HTTP 地址就该带 token。

        华为中间文件的 `"HTTP file server"` 与思科 bootstrap 的 `server` 变量都取自
        `http_root` —— 所以只要 API 那一步把 token 接上（`with_token`），产物自然带上。
        真正消费这个 HTTP 地址的是**华为/思科**；**H3C 走 TFTP（option 67）不经过 HTTP**，
        见 test_h3c_note_is_tftp_only。
        """
        import importlib

        from app.config import settings
        from app.core import serve_token
        from app.ct.ztp.generator import generate_all

        m = importlib.import_module("tests.test_ztp_baseline")
        with mock.patch.object(settings, "ztp_serve_token", self.TOK):
            url = serve_token.ZTP.with_token("http://10.0.0.1:8000/ztp")
            p = m.prof("huawei-ce", http_root=url, admin_user="opstkadm",
                       snmp_community="Opstk@2026")
            files = generate_all(p, [m.DEV])
        mid = next(v for k, v in files.items() if k.endswith("ztp_intermediate.txt"))
        self.assertIn('"HTTP file server"', mid)
        self.assertIn(url, mid, mid)

    def test_h3c_note_is_tftp_only(self):
        """把"H3C 那条链路不经过 HTTP"钉成事实 —— 免得以后有人以为 token 保护了它。"""
        import importlib

        from app.ct.ztp.generator import generate_all

        m = importlib.import_module("tests.test_ztp_baseline")
        p = m.prof("h3c", http_root="http://10.0.0.1:8000/ztp")
        files = generate_all(p, [m.DEV])
        note = next(v for k, v in files.items() if k.endswith("ztp_note.txt"))
        self.assertIn("DHCP option", note)
        self.assertNotIn("http://", note)      # H3C 的说明文件里根本没有 HTTP 地址
