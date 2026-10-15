# -*- coding: utf-8 -*-
"""A2 真机产物实证的**第二个** openSUSE 缺陷：**每机**应答文件漏了家族分派。

现象（线上真实产物）：openSUSE 模板部署后
  · `profiles/<pid>/boot/bc-24-11-66-8b-ea.ipxe` 里写的是
        `autoyast=http://…/profiles/<pid>/ks/bc-24-11-66-8b-ea/ks.cfg`
  · 而那份 `ks/<tag>/ks.cfg` 的**内容**是 kickstart：
        `# RHEL / Rocky / Alma Linux Kickstart` / `url --url=…`
  ⇒ 安装器把 kickstart 当 AutoYaST 用，必然解析失败。

根因：`generate_all` 的每机分支只有 ubuntu / debian / else(ks) 三种；openSUSE 落到 else。
**这正是同一段代码里 debian 那条注释（2026-10-09）警告过的坑，我在 openSUSE 上又踩了一次。**

注意：这条与模板根的 `autoinst.xml` 是**两条**路径（`generate_all` 的家族分派只管模板根），
所以只修模板根不够 —— 真机产物才照出来。
"""
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe import os_catalog                # noqa: E402
from app.it.pxe.generator import generate_all    # noqa: E402
from tests.test_pxe_debian_preseed import _cfg   # noqa: E402  (复用测试构造器)

MAC = "bc:24:11:66:8b:ea"
TAG = "bc-24-11-66-8b-ea"


class PerMachineOpensuseTest(unittest.TestCase):
    def setUp(self):
        self.files = generate_all(
            _cfg(os_type="opensuse", os_version="15.6"),
            installs=[{"mac": MAC, "hostname": "suse140"}])

    def test_per_machine_answer_file_is_autoyast_not_kickstart(self):
        """★ 核心：每机应答文件必须是 AutoYaST，且**不能**是 kickstart。"""
        self.assertIn("ks/" + TAG + "/autoinst.xml", self.files,
                      "openSUSE 的每机应答文件应当是 autoinst.xml")
        self.assertNotIn("ks/" + TAG + "/ks.cfg", self.files,
                         "openSUSE 不该在每机路径写 kickstart")
        body = self.files["ks/" + TAG + "/autoinst.xml"]
        self.assertNotIn("Kickstart", body)
        self.assertNotIn("--url=", body)          # anaconda 的指令，AutoYaST 不认
        ET.fromstring(body)                        # 必须是合法 XML

    def test_kernel_line_points_at_the_autoyast_file(self):
        """内核行的 `autoyast=` 必须指向那份 autoinst.xml（指错=白装）。"""
        menu = self.files["boot/" + TAG + ".ipxe"]
        self.assertIn("autoyast=", menu)
        self.assertIn("ks/" + TAG + "/autoinst.xml", menu)
        self.assertNotIn("ks/" + TAG + "/ks.cfg", menu)
        self.assertIn("install=", menu)
        self.assertNotIn("inst.ks=", menu)

    def test_other_families_are_unchanged(self):
        """kickstart 家族仍写 ks.cfg —— 别把它们的每机路径改坏了。"""
        for fam in ("openeuler", "rocky", "centos"):
            if os_catalog.entry(fam) is None:
                continue
            f = generate_all(_cfg(os_type=fam, os_version="9"),
                             installs=[{"mac": MAC, "hostname": "h"}])
            with self.subTest(fam=fam):
                self.assertIn("ks/" + TAG + "/ks.cfg", f)
                self.assertNotIn("ks/" + TAG + "/autoinst.xml", f)
                self.assertIn("inst.ks=", f["boot/" + TAG + ".ipxe"])


if __name__ == "__main__":
    unittest.main()
