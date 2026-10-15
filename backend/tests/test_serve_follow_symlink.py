# -*- coding: utf-8 -*-
"""`/pxe/serve` 与 `/ztp` 必须**跟随符号链接**：大仓库放独立卷、web 根留软链是正常运维做法。

A2 真机准备时实测（2026-10-15）：
  · 4.5GB 的 openSUSE 仓库树放进 web 根 ⇒ 把 web 根所在的 **rl-root** 压到 96%（危险）；
  · 而 `/srv/opstk/iso` 在**另一个卷**（rl-home，88G 空闲）⇒ 挪过去 + 软链是正解；
  · starlette 0.41.3 的 `StaticFiles` 默认 `follow_symlink=False` ⇒ 软链**一律 404**
    （文件系统里明明读得到），这条路走不通。
改为 `follow_symlink=True` 后线上实测：repomd.xml HTTP=200（11060 字节）、
x86_64/7zip-…rpm HTTP=200（1022388 字节）。

这条用例在**行为层面**钉住它：同一个临时目录，软链目标在目录**之外**，
`follow_symlink=True` 能取到、`False` 取不到（证明这个开关确实是关键，
而不是"改了别的地方碰巧好了"）。
"""
import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from starlette.exceptions import HTTPException   # noqa: E402

from app.main import _TokenStatic   # noqa: E402

SCOPE = {"type": "http", "method": "GET", "headers": [], "path": "/", "root_path": ""}


def _get(static, path):
    """取响应。★ Starlette 的 `StaticFiles.get_response` 对 404 是**抛** HTTPException，
    不是返回一个 404 响应（外层 `__call__` 才转成响应）—— 我第一版按"返回 404"断言，
    拿到容器里跑就是 3 条全挂。这里统一成"拿到状态码"。"""
    try:
        return asyncio.run(static.get_response(path, dict(SCOPE)))
    except HTTPException as e:
        class _R:
            status_code = e.status_code
            body = b""
        return _R()


class FollowSymlinkTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = os.path.join(self.tmp.name, "web")
        outside = os.path.join(self.tmp.name, "other-volume")     # 模拟"另一个卷"
        os.makedirs(root)
        os.makedirs(outside)
        with open(os.path.join(outside, "repomd.xml"), "w", encoding="utf-8") as f:
            f.write("<repomd/>")
        # ★ Windows 上普通进程不能建符号链接（WinError 1314 需要特权/开发者模式）：
        #   这里**不** skipTest（否则连不依赖软链的那条文本断言也会被跳过），
        #   只记下能力；真正需要软链的三条用例各自调用 _require_symlink()。
        self.symlink_ok = True
        self.root = root
        try:
            os.symlink(outside, os.path.join(root, "repo-link"))
        except (OSError, NotImplementedError):
            self.symlink_ok = False

    def _require_symlink(self):
        if not self.symlink_ok:
            self.skipTest("本机不允许创建符号链接（Windows 需要特权/开发者模式）")

    def test_default_does_not_follow_symlink(self):
        self._require_symlink()
        """默认（未开开关）时软链是 404 —— 这正是线上踩到的现象。"""
        st = _TokenStatic(directory=self.root)
        self.assertEqual(_get(st, "repo-link/repomd.xml").status_code, 404)

    def test_enabled_serves_through_symlink(self):
        self._require_symlink()
        st = _TokenStatic(directory=self.root, follow_symlink=True)
        resp = _get(st, "repo-link/repomd.xml")
        self.assertEqual(resp.status_code, 200)
        # 200 时拿到的是 FileResponse（有 .path，**没有** .body）—— 断言路径指向软链目标
        self.assertTrue(str(getattr(resp, "path", "")).endswith("repomd.xml"), resp)

    def test_missing_file_is_still_404(self):
        self._require_symlink()
        """开了软链也不能把"不存在的文件"变成 200。"""
        st = _TokenStatic(directory=self.root, follow_symlink=True)
        self.assertEqual(_get(st, "repo-link/nope.rpm").status_code, 404)
        self.assertEqual(_get(st, "no-such-dir/repomd.xml").status_code, 404)

    def test_source_mounts_pass_the_flag(self):
        """两个无认证静态挂载都必须显式带上这个开关（漏一个就是那次 404 复发）。

        用正则而不是多行精确匹配：我第一版按缩进逐字比，结果被空格数坑成失败 ——
        断言"结构"不要断言"排版"。
        """
        import re
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app", "main.py")
        src = open(path, encoding="utf-8").read()
        for scope in ("serve_token.PXE", "serve_token.ZTP"):
            self.assertRegex(
                src, r"scope=" + re.escape(scope) + r",\s*follow_symlink=True",
                scope + " 的静态挂载没有带上 follow_symlink=True")


if __name__ == "__main__":
    unittest.main()
