# -*- coding: utf-8 -*-
"""`/iso/{name}/extract` 必须**按 ISO 自己是谁**去提取，不能写死 ubuntu。

改前的实现（线上实测抓出来的真 bug）：
    extract_from_iso(iso_name, os_type=body.get("os_type", "ubuntu"),
                     os_version=body.get("os_version", "22.04"))
缺省 ubuntu/22.04 ⇒ **任何**非 Ubuntu 的 ISO 都被按 casper/ 找引导文件，必然失败。
实测证据（openSUSE Leap 15.6 DVD）：
    {"ok": false, "log": [..., "No boot files found in ISO (expected casper/ boot files)"],
     "dest": "/srv/opstk/pxe-web/ubuntu/22.04", "extracted": []}
修后实测：dest=/srv/opstk/pxe-web/opensuse/15.6，extracted=[vmlinuz, initrd.img]。

这条用例把"按文件名探测 + 显式传参优先 + 探测不出来就 422"三件事钉住。
"""
import asyncio
import sys
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from fastapi import HTTPException            # noqa: E402

from app.api import pxe as api               # noqa: E402


def _call(iso_name, body):
    """直接调端点函数，并把真正落到 extract_from_iso 的参数**截下来**。"""
    captured = {}
    orig = api.pxe_server.extract_from_iso

    def fake(name, **kw):
        captured["name"] = name
        captured.update(kw)
        return {"ok": True, "log": [], "dest": "/x", "extracted": []}

    api.pxe_server.extract_from_iso = fake
    try:
        asyncio.run(api.extract_iso(iso_name, body))
    finally:
        api.pxe_server.extract_from_iso = orig
    return captured


class ExtractDetectsOsTest(unittest.TestCase):
    def test_opensuse_iso_is_not_extracted_as_ubuntu(self):
        """★ 核心：openSUSE 的 ISO 必须按 opensuse 提取（改前会按 ubuntu）。"""
        got = _call("openSUSE-Leap-15.6-DVD-x86_64-Media.iso", {})
        self.assertEqual(got["os_type"], "opensuse")
        self.assertEqual(got["os_version"], "15.6")
        self.assertEqual(got["name"], "openSUSE-Leap-15.6-DVD-x86_64-Media.iso")

    def test_other_families_are_detected_too(self):
        for name, want in (("debian-13.7.0-amd64-DVD-1.iso", "debian"),
                           ("Rocky-9.4-x86_64-dvd.iso", "rocky"),
                           ("openEuler-24.03-LTS-SP4.iso", "openeuler"),
                           ("ubuntu-22.04.5-live-server-amd64.iso", "ubuntu"),
                           ("CentOS-7-x86_64-7.9-Minimal-2009.iso", "centos")):
            with self.subTest(name=name):
                self.assertEqual(_call(name, {})["os_type"], want)

    def test_explicit_body_wins(self):
        got = _call("openSUSE-Leap-15.6-DVD-x86_64-Media.iso",
                    {"os_type": "ubuntu", "os_version": "22.04"})
        self.assertEqual(got["os_type"], "ubuntu")
        self.assertEqual(got["os_version"], "22.04")

    def test_unrecognisable_name_is_422_not_silent_ubuntu(self):
        """★ 探测不出来时**必须报错**：静默按 ubuntu 猜正是这个 bug 的形状。"""
        with self.assertRaises(HTTPException) as ctx:
            _call("mystery-media-1.iso", {})
        self.assertEqual(ctx.exception.status_code, 422)
        self.assertIn("无法从文件名识别", str(ctx.exception.detail))


if __name__ == "__main__":
    unittest.main()
