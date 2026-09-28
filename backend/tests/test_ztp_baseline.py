# -*- coding: utf-8 -*-
"""ZTP 开局基线配置的**平台差异**约束（H3C / 华为 / 思科）。

这里锁的都是真机上验证过、或真机会拒的形态：
  · H3C Comware 7 没有 `stelnet server enable`；SSH 服务是 `ssh server enable`；
  · 管理 VLAN **必须先建**，SVI 才配得上（H3C/华为/思科三家同理）；
  · 华为 VRP8/CE 的 SNMP 团体名必须 8-32 字符（默认 public 只有 6 位，设备会拒）；
  · 思科不应该主动 `no service password-encryption`（那是把弱口令按明文存）。

与本目录其它测试一致：用 unittest.TestCase（本项目的收集规则靠它）。
"""
import os
import sys
import unittest

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.ct.ztp.generator import (  # noqa: E402
    ZtpDevice,
    ZtpProfile,
    cisco_config,
    h3c_config,
    huawei_config,
)


def prof(vendor, **kw):
    base = dict(vendor=vendor, admin_password="TestPw@123", admin_user="admin",
                snmp_community="Opstk@2026", mgmt_vlan=10,
                mgmt_interface="Vlan-interface10", mgmt_gateway="10.0.0.254")
    base.update(kw)
    return ZtpProfile(**base)


DEV = ZtpDevice(hostname="SW01", mgmt_ip="10.0.0.11")


class H3cBaselineTest(unittest.TestCase):
    def test_no_stelnet_server_enable(self):
        """`stelnet server enable` 在 Comware 7 上不存在（真机发下去会报错）。"""
        out = h3c_config(DEV, prof("h3c"))
        self.assertNotIn("stelnet server enable", out)
        self.assertIn("ssh server enable", out)

    def test_hardening_disables_telnet_and_web(self):
        out = h3c_config(DEV, prof("h3c"))
        for line in ("undo telnet server enable", "undo ip http enable",
                     "undo ip https enable"):
            self.assertIn(line, out, "缺少加固命令 %s" % line)

    def test_mgmt_vlan_created_before_svi(self):
        # 模板里没登记任何 VLAN，生成器也必须把管理 VLAN 建出来
        out = h3c_config(DEV, prof("h3c", vlans=[]))
        self.assertIn("vlan 10", out)
        self.assertLess(out.index("vlan 10"), out.index("interface Vlan-interface10"),
                        "管理 VLAN 必须出现在 SVI 之前")

    def test_mgmt_vlan_not_duplicated_when_listed(self):
        out = h3c_config(DEV, prof("h3c", vlans=[{"id": 10, "name": "MGMT"}]))
        self.assertLessEqual(out.count("\nvlan 10\n"), 1)

    def test_vlan_mismatch_is_flagged_and_interface_wins(self):
        """管理 VLAN 号与接口名不一致时：以接口名为准，并在配置里写明。"""
        out = h3c_config(DEV, prof("h3c", mgmt_vlan=10,
                                  mgmt_interface="Vlan-interface100"))
        self.assertIn("不一致", out)
        self.assertIn("vlan 100", out)
        self.assertIn("interface Vlan-interface100", out)

    def test_ntp_line_only_when_configured(self):
        self.assertNotIn("ntp-service", h3c_config(DEV, prof("h3c", ntp_server="")))
        self.assertIn("ntp-service unicast-peer 10.1.1.1",
                      h3c_config(DEV, prof("h3c", ntp_server="10.1.1.1")))


class HuaweiBaselineTest(unittest.TestCase):
    def test_short_community_is_rejected(self):
        with pytest.raises(ValueError) as e:
            huawei_config(DEV, prof("huawei", snmp_community="public"))
        self.assertIn("8-32", str(e.value))

    def test_empty_community_is_rejected(self):
        with pytest.raises(ValueError):
            huawei_config(DEV, prof("huawei", snmp_community=""))

    def test_eight_char_community_is_accepted(self):
        out = huawei_config(DEV, prof("huawei", snmp_community="Opstk@26"))
        self.assertIn("snmp-agent community read Opstk@26", out)

    def test_mgmt_vlan_in_batch(self):
        out = huawei_config(DEV, prof("huawei", vlans=[]))
        self.assertIn("vlan batch 10", out)

    def test_dns_domain_uses_huawei_keyword(self):
        out = huawei_config(DEV, prof("huawei", domain_name="lab.local"))
        self.assertIn("dns domain lab.local", out)
        self.assertNotIn("\ndomain lab.local", out)


class HuaweiVrp8BaselineTest(unittest.TestCase):
    """华为 **VRP8（CE/NE）** 与 VRP5 的命令差异 —— 全部是 CE6800 真机实测出来的。

    实测证据（V200R005，EVE 模拟器）：
      · `local-user admin …` → Wrong parameter（用户名 STRING<6-253>，admin 只有 5 位）
      · `local-user <6位> password irreversible-cipher <明文>` → Info: A new user is added.
      · `local-user <6位> user-group manage-ug` → 通过；`privilege level 15` → Unrecognized
      · `ntp-service unicast-server …` → Unrecognized；`ntp unicast-server …` → 通过
      · 结尾 `return` → 弹 [Y/N/C] 提交确认 ⇒ 用 `commit`
    """

    def cfg(self, **kw):
        kw.setdefault("vendor", "huawei-ce")
        kw.setdefault("admin_user", "opstkadm")
        kw.setdefault("ntp_server", "10.1.1.1")
        p = prof(**kw)
        from app.ct.ztp.generator import _norm_vendor
        return huawei_config(DEV, p, vrp8=(_norm_vendor(p.vendor) == "huawei-ce"))

    def test_username_shorter_than_six_is_rejected(self):
        with pytest.raises(ValueError) as e:
            self.cfg(admin_user="admin")
        self.assertIn("6", str(e.value))
        self.assertIn("用户名", str(e.value))

    def test_six_char_username_is_accepted(self):
        self.assertIn("local-user opstkadm password irreversible-cipher TestPw@123",
                      self.cfg())

    def test_no_privilege_level_uses_user_group(self):
        out = self.cfg()
        self.assertNotIn("privilege level", out)
        self.assertIn("local-user opstkadm user-group manage-ug", out)

    def test_ntp_uses_vrp8_form(self):
        self.assertIn("ntp unicast-server 10.1.1.1", self.cfg())
        self.assertNotIn("ntp-service", self.cfg())

    def test_commits_instead_of_return(self):
        out = self.cfg()
        self.assertTrue(out.rstrip().endswith("commit"), out[-60:])
        self.assertNotIn("return", out)


class HuaweiVrp5BaselineTest(unittest.TestCase):
    """VRP5 的老语法必须保持不变（不能被 VRP8 的改动带跑）。"""

    def cfg(self, **kw):
        kw.setdefault("vendor", "huawei")
        kw.setdefault("admin_user", "admin")
        kw.setdefault("ntp_server", "10.1.1.1")
        return huawei_config(DEV, prof(**kw))

    def test_vrp5_keeps_privilege_level_and_ntp_service(self):
        out = self.cfg()
        self.assertIn("local-user admin privilege level 15", out)
        self.assertIn("ntp-service unicast-server 10.1.1.1", out)
        self.assertTrue(out.rstrip().endswith("return"))
        self.assertNotIn("irreversible-cipher", out)

    def test_vrp5_accepts_short_username(self):
        # VRP5 没有 6 位限制，老模板/老习惯（admin）必须继续能用
        self.assertIn("local-user admin password cipher TestPw@123", self.cfg())


class VendorAliasTest(unittest.TestCase):
    def test_vrp8_aliases_normalize(self):
        from app.ct.ztp.generator import _norm_vendor
        for alias in ("VRP8", "huawei-ce", "huawei_vrp8", "ce", "CloudEngine"):
            self.assertEqual(_norm_vendor(alias), "huawei-ce", alias)

    def test_generate_all_for_huawei_ce(self):
        from app.ct.ztp.generator import generate_all
        p = ZtpProfile(vendor="huawei-ce", admin_password="TestPw@123",
                       admin_user="opstkadm", snmp_community="Opstk@2026",
                       server_ip="10.0.0.250")
        files = generate_all(p, [ZtpDevice(hostname="CE01", mac="00:11:22:33:44:55",
                                          mgmt_ip="10.0.0.21")])
        self.assertIn("ztp/CE01.cfg", files)
        self.assertIn("ztp/ztp_intermediate.txt", files)   # 华为仍然用中间文件
        self.assertIn("commit", files["ztp/CE01.cfg"])
        self.assertNotIn("ntp-service", files["ztp/CE01.cfg"])


class CiscoBaselineTest(unittest.TestCase):
    def test_does_not_disable_password_encryption(self):
        out = cisco_config(DEV, prof("cisco"))
        self.assertNotIn("no service password-encryption", out)
        self.assertIn("service password-encryption", out)

    def test_vlan_created_before_svi(self):
        out = cisco_config(DEV, prof("cisco", mgmt_interface="Vlan10", vlans=[]))
        self.assertLess(out.index("vlan 10"), out.index("interface Vlan10"))
