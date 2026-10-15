# -*- coding: utf-8 -*-
"""B1 后半「一键同步」：请求参数的白名单与状态读取（模块级用例）。

为什么校验必须在这里：`dest` 会决定**宿主机**把那几个 GB 写到哪个目录 —— 放开就等于
给了一个"写任意路径"的入口（`/tmp/pwned`、`repo/../../etc` 都必须 422）。
线上另有一条端到端验收（mimo/ftest_repo_sync_live.py）：真正写 request.json、
等宿主 path 单元执行、再读回 result.json。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe import repo_sync as rs   # noqa: E402


class ValidateTest(unittest.TestCase):
    def test_defaults_are_filled(self):
        v = rs.validate({})
        self.assertEqual(v["suite"], "trixie")
        self.assertEqual(v["components"], "main,contrib,non-free,non-free-firmware")
        self.assertTrue(v["dest"].startswith(rs.REPO_ROOT + "/"))
        self.assertEqual(v["jobs"], 8)
        self.assertEqual(len(v["id"]), 32)

    def test_dest_must_stay_under_repo_root(self):
        for bad in ("/tmp/pwned", "/srv/opstk/pxe-web/repo/../../etc", "/root",
                    "/srv/opstk/pxe-web/repo/", "/srv/opstk/pxe-web/repo/a/b"):
            with self.assertRaises(ValueError, msg=bad):
                rs.validate({"dest": bad})

    def test_suite_and_components_and_mirror_are_whitelisted(self):
        for bad in ({"suite": "trixie; rm -rf /"}, {"suite": "-bad"},
                    {"components": "main;drop"}, {"mirror": "file:///etc"},
                    {"mirror": "not-a-url"}, {"jobs": 0}, {"jobs": 999}, {"jobs": "abc"}):
            with self.assertRaises(ValueError, msg=bad):
                rs.validate(bad)

    def test_empty_strings_fall_back_to_defaults(self):
        """空串 = "没填"（前端提交空输入框很常见），不是非法值 —— 与本项目其它接口同一约定。"""
        v = rs.validate({"suite": "", "components": "", "mirror": "", "dest": ""})
        self.assertEqual(v["suite"], "trixie")
        self.assertEqual(v["components"], rs.DEFAULT_COMPONENTS)
        self.assertEqual(v["mirror"], rs.DEFAULT_MIRROR)
        self.assertTrue(v["dest"].startswith(rs.REPO_ROOT + "/"))

    def test_good_values_pass_and_are_echoed(self):
        v = rs.validate({"suite": "trixie-updates", "dest": "/srv/opstk/pxe-web/repo/debian-tuna",
                         "components": "main,contrib", "jobs": 4, "dry_run": True})
        self.assertEqual(v["suite"], "trixie-updates")
        self.assertEqual(v["jobs"], 4)
        self.assertTrue(v["dry_run"])


class StartAndStatusTest(unittest.TestCase):
    """用临时目录替换 state 目录，验证"写请求 / 读状态"的真实文件行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self._saved = (rs.STATE_DIR, rs.REQUEST, rs.STATUS, rs.RESULT, rs.LOG)
        rs.STATE_DIR = self.tmp.name
        rs.REQUEST = os.path.join(self.tmp.name, "request.json")
        rs.STATUS = os.path.join(self.tmp.name, "status.json")
        rs.RESULT = os.path.join(self.tmp.name, "result.json")
        rs.LOG = os.path.join(self.tmp.name, "sync.log")

        def restore():
            (rs.STATE_DIR, rs.REQUEST, rs.STATUS, rs.RESULT, rs.LOG) = self._saved

        self.addCleanup(restore)

    def test_idle_when_nothing_requested(self):
        self.assertEqual(rs.sync_status()["state"], "idle")

    def test_start_writes_request_and_status_reads_it_back(self):
        req = rs.start_sync({"suite": "trixie", "dry_run": True})
        self.assertTrue(os.path.exists(rs.REQUEST))
        with open(rs.REQUEST, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["id"], req["id"])
        s = rs.sync_status()
        self.assertEqual(s["state"], "queued")          # 已请求、宿主还没处理
        self.assertEqual(s["pending_request"]["id"], req["id"])

    def test_start_clears_previous_result(self):
        """★ 不能"刚点同步就看到上一次的结果" —— 运维会以为这次已经跑完了。"""
        with open(rs.RESULT, "w", encoding="utf-8") as f:
            json.dump({"state": "done", "rc": 0, "id": "old"}, f)
        rs.start_sync({})
        self.assertIsNone(rs.sync_status()["result"])
        self.assertEqual(rs.sync_status()["state"], "queued")

    def test_status_reports_host_result_and_log_tail(self):
        with open(rs.STATUS, "w", encoding="utf-8") as f:
            json.dump({"state": "done", "rc": 0, "id": "x"}, f)
        with open(rs.RESULT, "w", encoding="utf-8") as f:
            json.dump({"state": "done", "rc": 0, "id": "x", "tail": ["a", "b"]}, f)
        with open(rs.LOG, "w", encoding="utf-8") as f:
            f.write("\n".join("line%d" % i for i in range(50)))
        s = rs.sync_status(tail_lines=5)
        self.assertEqual(s["state"], "done")
        self.assertEqual(s["result"]["rc"], 0)
        self.assertEqual(s["log_tail"], ["line45", "line46", "line47", "line48", "line49"])

    def test_corrupt_state_files_do_not_crash(self):
        for p in (rs.STATUS, rs.RESULT):
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
        s = rs.sync_status()
        self.assertIsNone(s["status"])
        self.assertIsNone(s["result"])


if __name__ == "__main__":
    unittest.main()
