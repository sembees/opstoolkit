# -*- coding: utf-8 -*-
"""外部审查 U4-F6 / R3-M2 的回归：凭证密钥安全 + 部署串行化。

U4-F6 三条：
  · `credential_key` 只判非空、不判合法 ⇒ 非法 key 会在用的时候炸出英文异常，被
    `_safe_decrypt` 吞成空串，最终报成"口令不能为空"（运维按提示填了口令也没用）；
  · 生成密钥没有锁 + `_write_env` 是"读-改-写"整份 .env ⇒ 可能生成两把 key，
    或让 secret_key / credential_key 互相丢更新；
  · "解不开"与"本来没填"被混为一谈。
R3-M2：应用侧并发 /deploy 没有互斥（宿主机只给重载加了 flock）。

这些用例全部在**临时 .env**上跑（`crypto._ENV_PATH` 被替换），不碰真实 .env。
"""
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from cryptography.fernet import Fernet

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core import crypto  # noqa: E402


class _CryptoCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.env = pathlib.Path(self._tmp.name) / ".env"
        self.lock = pathlib.Path(self._tmp.name) / ".env.lock"
        self._save = (crypto._ENV_PATH, crypto._LOCK_PATH, crypto._fernet,
                      crypto.settings.credential_key, crypto.settings.secret_key)
        crypto._ENV_PATH = self.env
        crypto._LOCK_PATH = self.lock
        crypto._fernet = None
        crypto.settings.credential_key = ""
        crypto.settings.secret_key = ""
        os.environ.pop("credential_key", None)
        os.environ.pop("secret_key", None)

    def tearDown(self):
        (crypto._ENV_PATH, crypto._LOCK_PATH, crypto._fernet,
         crypto.settings.credential_key, crypto.settings.secret_key) = self._save
        crypto._fernet = None
        self._tmp.cleanup()

    def _env_lines(self):
        return self.env.read_text(encoding="utf-8").splitlines() if self.env.exists() else []


class ValidKeyTest(_CryptoCase):
    def test_valid_and_invalid_keys(self):
        good = Fernet.generate_key().decode()
        self.assertTrue(crypto.valid_fernet_key(good))
        for bad in ("", None, "abc", good[:-4], "x" * 44, good[:20]):
            with self.subTest(bad=bad):
                self.assertFalse(crypto.valid_fernet_key(bad))

    def test_health_is_silent_when_key_is_valid_or_absent(self):
        self.assertEqual(crypto.credential_key_health(), "")
        crypto.settings.credential_key = Fernet.generate_key().decode()
        self.assertEqual(crypto.credential_key_health(), "")

    def test_health_explains_an_invalid_key(self):
        crypto.settings.credential_key = "not-a-key"
        msg = crypto.credential_key_health()
        self.assertIn("credential_key", msg)
        self.assertIn("无法解密", msg)
        self.assertIn("不要重新生成", msg)

    def test_invalid_key_does_not_silently_regenerate(self):
        """非法密钥不能当"没配"处理 —— 重新生成会让已有密文永久打不开。"""
        crypto.settings.credential_key = "not-a-key"
        with self.assertRaises(RuntimeError) as e:
            crypto._ensure_key()
        self.assertIn("credential_key", str(e.exception))
        self.assertEqual(self._env_lines(), [])      # 一个字节都没写

    def test_valid_key_is_used_as_is(self):
        good = Fernet.generate_key().decode()
        crypto.settings.credential_key = good
        self.assertEqual(crypto._ensure_key().decode(), good)
        self.assertEqual(self._env_lines(), [])      # 不需要写 .env


class KeyGenerationTest(_CryptoCase):
    def test_concurrent_first_start_generates_exactly_one_key(self):
        """8 个线程同时首次启动：只能生成一把 key，且所有调用方拿到同一把。"""
        out, errs = [], []
        barrier = threading.Barrier(8)

        def go():
            try:
                barrier.wait(timeout=10)
                out.append(crypto._ensure_key().decode())
            except Exception as e:  # noqa: BLE001
                errs.append(repr(e))

        ts = [threading.Thread(target=go) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        self.assertEqual(errs, [])
        self.assertEqual(len(set(out)), 1, out)
        lines = [ln for ln in self._env_lines() if ln.startswith("credential_key=")]
        self.assertEqual(len(lines), 1, lines)
        self.assertEqual(lines[0].split("=", 1)[1], out[0])

    def test_write_env_does_not_lose_the_other_key(self):
        """读-改-写整份 .env：两个键并发写不能互相丢更新（各 20 轮）。"""
        for i in range(20):
            crypto.settings.credential_key = ""
            crypto.settings.secret_key = ""
            if self.env.exists():
                self.env.unlink()
            barrier = threading.Barrier(2)

            def w(key, val):
                barrier.wait(timeout=10)
                crypto._write_env(key, val)

            t1 = threading.Thread(target=w, args=("credential_key", "CK%d" % i))
            t2 = threading.Thread(target=w, args=("secret_key", "SK%d" % i))
            t1.start()
            t2.start()
            t1.join(timeout=10)
            t2.join(timeout=10)
            lines = self._env_lines()
            self.assertIn("credential_key=CK%d" % i, lines, lines)
            self.assertIn("secret_key=SK%d" % i, lines, lines)

    def test_secret_key_generation_is_locked_too(self):
        crypto.settings.secret_key = ""
        vals = []
        barrier = threading.Barrier(6)

        def go():
            barrier.wait(timeout=10)
            vals.append(crypto.ensure_secret_key())

        ts = [threading.Thread(target=go) for _ in range(6)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        self.assertEqual(len(set(vals)), 1, vals)
        lines = [ln for ln in self._env_lines() if ln.startswith("secret_key=")]
        self.assertEqual(len(lines), 1, lines)


class SafeDecryptTest(_CryptoCase):
    def test_empty_ciphertext_is_not_an_error(self):
        from app.api.pxe import _safe_decrypt as pxe_dec
        from app.api.ztp import _safe_decrypt as ztp_dec
        for fn in (pxe_dec, ztp_dec):
            with self.subTest(fn=fn.__module__):
                self.assertEqual(fn(""), "")
                self.assertEqual(fn(None), "")

    def test_wrong_key_gives_an_actionable_error(self):
        """密文非空却解不开 ⇒ 必须指名 credential_key，而不是模糊的"口令不能为空"。"""
        from app.api.pxe import _safe_decrypt
        crypto.settings.credential_key = Fernet.generate_key().decode()
        crypto._fernet = None
        token = crypto.encrypt("TestPw@123")
        self.assertTrue(token)
        # 换一把密钥（模拟 credential_key 被换/丢）
        crypto.settings.credential_key = Fernet.generate_key().decode()
        crypto._fernet = None
        with self.assertRaises(ValueError) as ctx:
            _safe_decrypt(token)
        msg = str(ctx.exception)
        self.assertIn("无法解密", msg)
        self.assertIn("credential_key", msg)


class DeploySerializationTest(unittest.TestCase):
    """R3-M2：应用侧两次并发部署不能交叉执行（写入 + 等重载必须是原子的）。"""

    def _assert_no_overlap(self, module_name):
        import importlib
        mod = importlib.import_module(module_name)
        spans = []
        lock = threading.Lock()

        def fake(*a, **k):
            start = time.monotonic()
            time.sleep(0.2)
            with lock:
                spans.append((start, time.monotonic()))
            return {"ok": True, "supported": True, "log": [], "errors": [], "files_written": []}

        with mock.patch.object(mod, "_deploy_files_impl", fake):
            ts = [threading.Thread(target=lambda: mod.deploy_files({}, "x"))
                  for _ in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join(timeout=30)
        self.assertEqual(len(spans), 3)
        spans.sort()
        for (s1, e1), (s2, _e2) in zip(spans, spans[1:]):
            self.assertGreaterEqual(s2, e1 - 0.01,
                                    "%s 的部署发生了重叠：%s" % (module_name, spans))

    def test_pxe_deploy_is_serialized(self):
        self._assert_no_overlap("app.it.pxe.server")

    def test_ztp_deploy_is_serialized(self):
        self._assert_no_overlap("app.ct.ztp.server")

    def test_public_entry_keeps_returning_a_dict(self):
        import importlib
        for name in ("app.it.pxe.server", "app.ct.ztp.server"):
            with self.subTest(name=name):
                mod = importlib.import_module(name)
                with mock.patch.object(mod, "_deploy_files_impl",
                                       lambda *a, **k: {"ok": True, "sentinel": 1}):
                    self.assertEqual(mod.deploy_files({}, ""), {"ok": True, "sentinel": 1})


class OwnPathGuardTest(unittest.TestCase):
    """U2-F10：`check_dhcp_conf_safety` 不传 own_path 时自排除失效 —— 至少要吱一声。

    本仓库两个调用点都显式传了 own_path，所以这是 API 易用性缺口；留日志是为了让以后
    新加的调用点踩到时能立刻定位（否则表现是"一次合法部署被莫名其妙拦下"）。
    """

    def test_missing_own_path_logs_a_warning(self):
        from app.core import dhcp
        with self.assertLogs("app.core.dhcp", level="WARNING") as cm:
            dhcp.check_dhcp_conf_safety(
                "interface=ens19\nbind-interfaces\ndhcp-range=192.168.199.100,192.168.199.200,12h\n",
                providers={"iface_v4_map": {"ens19": "192.168.199.1/24"},
                           "protected": ("ens18", [])},
                conf_dir=tempfile.gettempdir())
        self.assertIn("own_path", "\n".join(cm.output))

    def test_with_own_path_no_warning_about_it(self):
        from app.core import dhcp
        with self.assertNoLogs("app.core.dhcp", level="WARNING"):
            dhcp.check_dhcp_conf_safety(
                "interface=ens19\nbind-interfaces\ndhcp-range=192.168.199.100,192.168.199.200,12h\n",
                providers={"iface_v4_map": {"ens19": "192.168.199.1/24"},
                           "protected": ("ens18", [])},
                conf_dir=tempfile.gettempdir(),
                own_path=os.path.join(tempfile.gettempdir(), "opstk-ztp.conf"))


if __name__ == "__main__":
    unittest.main()
