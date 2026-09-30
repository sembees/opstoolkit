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
        # ★ 外部审查 U6-F1：这里原来写的是 `assertLessEqual(count, 1)` —— 等于允许
        #   "一次都不生成"，断言形同虚设（管理 VLAN 没建出来也照样通过）。
        out = h3c_config(DEV, prof("h3c", vlans=[{"id": 10, "name": "MGMT"}]))
        self.assertEqual(out.count("\nvlan 10\n"), 1)

    def test_vlan_mismatch_is_flagged_and_interface_wins(self):
        """管理 VLAN 号与接口名不一致时：以接口名为准，并在配置里写明。"""
        out = h3c_config(DEV, prof("h3c", mgmt_vlan=10,
                                  mgmt_interface="Vlan-interface100"))
        self.assertIn("不一致", out)
        self.assertIn("vlan 100", out)
        self.assertIn("interface Vlan-interface100", out)
        # ★ 外部审查 U6-F4：还要锁"错的那个 VLAN **不**出现"，以及 VLAN 必须先于 SVI 生成
        self.assertNotIn("\nvlan 10\n", out)
        self.assertLess(out.index("vlan 100"), out.index("interface Vlan-interface100"))

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


class MgmtInterfaceShapeTest(unittest.TestCase):
    """U3-2nd-F6：管理接口 = VLAN 接口（SVI）**或物理口**。

    改前（真缺陷）：`_vlan_id_of_interface` 用 `(\\d+)$` 从 `GigabitEthernet1/0/24` 抠出 24，
    于是模板里写"把管理 IP 配在这个物理口上"，生成出来的却是「新建 vlan 24 + 把上联/接入口
    划进 VLAN 24 + 在物理口上配 IP」—— 端口被挪出它原本的 VLAN（上联口可能就是 trunk）。

    第一版修复是 fail-closed（拒绝物理口）；**真机验证完成后**（RUNBOOK §5.74）改成真支持：
      · H3C S6850 真机：切成 bridge 后 `ip address` 被拒
        （`% Unrecognized command found at '^' position.`，回读配置里也确实没有它），
        `port link-mode route` 之后配得上（回读原文在手册里）；
      · 华为 CE6800（VRP8）真机：二层口 `ip address` → `Error: Unrecognized command found at
        '^' position.`，`undo portswitch` + `commit` 之后配得上（回读原文在手册里）；
      · VRP5 / 思科：本环境**没有镜像可验** ⇒ 按文档生成并在产物里显式标注"未真机验证"。
    这一组用例钉死三件事：① 物理口不再被拒；② 产物里必须先切三层再配地址；
    ③ **绝不**再从物理口名字里抠出 VLAN 号（不建 vlan 24、不把端口划进它）。
    """

    PHYSICAL = ("GigabitEthernet1/0/24", "Ten-GigabitEthernet1/0/1", "GE1/0/24",
                "M-GigabitEthernet0/0/0", "MEth0/0/0", "WGE1/0/4", "10GE1/0/1")

    @staticmethod
    def _gen(vendor, fn, iface, **kw):
        """调某个厂商的生成器。两点必须显式处理，否则"验的是另一个平台/另一个报错"：
          · **huawei-ce 必须 `vrp8=True`** —— 不传拿到的是 VRP5 语法
            （`privilege level` / `ntp-service`），用例就名不副实了；
          · **huawei-ce 的用户名必须 ≥6 位**（VRP8 真机实测要求），默认的 `admin` 是 5 位，
            不换掉会直接撞"用户名太短"的 ValueError。
        """
        if vendor == "huawei-ce":
            kw.setdefault("admin_user", "opstkadm")
        p = prof(vendor, mgmt_interface=iface, **kw)
        return fn(DEV, p, vrp8=(vendor == "huawei-ce")) if vendor.startswith("huawei") \
            else fn(DEV, p)

    def test_physical_mgmt_interface_is_accepted_and_switches_to_l3(self):
        """四种厂商都要先生成"切三层"的命令，再配地址。"""
        for vendor, fn, expect in (("h3c", h3c_config, "port link-mode route"),
                                   ("huawei", huawei_config, "undo portswitch"),
                                   ("huawei-ce", huawei_config, "undo portswitch"),
                                   ("cisco", cisco_config, "no switchport")):
            with self.subTest(vendor=vendor):
                out = self._gen(vendor, fn, "GigabitEthernet1/0/24")
                self.assertIn("interface GigabitEthernet1/0/24", out)
                self.assertIn(expect, out)
                # 切模式必须在配地址**之前**（否则设备上按顺序执行时地址配不上）
                self.assertLess(out.index(expect), out.index("ip address"))

    def test_no_vlan_is_invented_from_the_physical_port_name(self):
        """★ 这条是 U3-2nd-F6 的核心：`GigabitEthernet1/0/24` 里的 24 **不是** VLAN 号。"""
        for vendor, fn, want_vlan in (("h3c", h3c_config, "vlan 10"),
                                      ("huawei", huawei_config, "vlan batch 10"),
                                      ("huawei-ce", huawei_config, "vlan batch 10"),
                                      ("cisco", cisco_config, "vlan 10")):
            with self.subTest(vendor=vendor):
                out = self._gen(vendor, fn, "GigabitEthernet1/0/24", mgmt_vlan=10)
                # 只看 VLAN 相关的行（"24" 当然会出现在接口名里，那是合法的）
                vlan_lines = [ln.strip() for ln in out.splitlines()
                              if ln.strip().startswith(("vlan", "!", "#"))
                              and "vlan" in ln.lower()]
                self.assertNotIn("24", " ".join(vlan_lines),
                                 "又从物理口名字里抠出 VLAN 号了：" + str(vlan_lines))
                self.assertIn(want_vlan, out)        # 端口仍按模板的「管理 VLAN 号」
                self.assertNotIn("不一致", out)        # 物理口名字与 VLAN 号无关，不该报"不一致"
                self.assertNotIn("Vlan-interface24", out)

    def test_vrp5_and_cisco_are_marked_unverified(self):
        """验不了的平台必须**写在产物里**，不能让运维以为都验过。"""
        for vendor, fn in (("huawei", huawei_config), ("cisco", cisco_config)):
            with self.subTest(vendor=vendor):
                out = self._gen(vendor, fn, "GE1/0/24")
                self.assertIn("没有真机可验", out)
        # 验过的两个平台不该带这句话
        for vendor, fn in (("h3c", h3c_config), ("huawei-ce", huawei_config)):
            with self.subTest(vendor=vendor + "-verified"):
                out = self._gen(vendor, fn, "GE1/0/24")
                self.assertNotIn("没有真机可验", out)
                self.assertIn("commit", out) if vendor == "huawei-ce" else None

    def test_vlan_interface_forms_are_accepted(self):
        for vendor, fn, iface in (("h3c", h3c_config, "Vlan-interface10"),
                                  ("huawei", huawei_config, "Vlanif10"),
                                  ("huawei-ce", huawei_config, "Vlanif10"),
                                  ("cisco", cisco_config, "Vlan10")):
            with self.subTest(vendor=vendor, iface=iface):
                out = fn(DEV, prof(vendor, mgmt_interface=iface))
                self.assertIn("interface " + iface, out)
                # SVI 路径不能被切模式的命令污染（老输出必须逐字不变）
                self.assertNotIn("port link-mode route", out)
                self.assertNotIn("undo portswitch", out)
                self.assertNotIn("no switchport", out)

    def test_empty_mgmt_interface_is_derived_from_mgmt_vlan(self):
        """空值不能再生成 `interface `（一个没有名字的接口，设备必拒）——按厂商推导 SVI。"""
        for vendor, fn, expect in (("h3c", h3c_config, "Vlan-interface10"),
                                   ("huawei", huawei_config, "Vlanif10"),
                                   ("huawei-ce", huawei_config, "Vlanif10"),
                                   ("cisco", cisco_config, "Vlan10")):
            with self.subTest(vendor=vendor):
                out = fn(DEV, prof(vendor, mgmt_interface="", mgmt_vlan=10))
                self.assertIn("interface " + expect, out)
                self.assertNotIn("interface \n", out)

    def test_generate_all_accepts_physical_mgmt_interface(self):
        """generate_all（API 用的主路径）也要放行，并产出切三层的命令。"""
        from app.ct.ztp.generator import generate_all
        files = generate_all(prof("h3c", mgmt_interface="GigabitEthernet1/0/24"), [DEV])
        body = "\n".join(files.values())
        self.assertIn("interface GigabitEthernet1/0/24", body)
        self.assertIn("port link-mode route", body)
        self.assertNotIn("vlan 24", body)

    def test_junk_looking_interface_names_are_still_rejected(self):
        """放开物理口 ≠ 什么都收：不像接口名的写法一律拒绝（这个值会拼进命令行）。"""
        for bad in ("uplink", "eth0", "foo bar", "1", "port-channel"):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    h3c_config(DEV, prof("h3c", mgmt_interface=bad))

    def test_schema_and_generator_agree_on_physical_forms(self):
        """两层必须同口径（正则只有一份，但入口与生成期都要走到）。"""
        from app.core.schemas import ZtpTemplateIn
        from app.ct.ztp.generator import _PHYS_IFACE_RE
        for iface in self.PHYSICAL + ("Vlan-interface10", "Vlanif10", "Vlan10"):
            with self.subTest(iface=iface):
                self.assertEqual(
                    ZtpTemplateIn(name="t", vendor="h3c",
                                  mgmt_interface=iface).mgmt_interface, iface)
                self.assertTrue(_PHYS_IFACE_RE.match(iface) or iface.lower().startswith("vlan"))
        for bad in ("uplink", "eth0", "foo bar", "1"):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    ZtpTemplateIn(name="t", vendor="h3c", mgmt_interface=bad)

    def test_schema_rejects_control_chars_in_mgmt_interface(self):
        """老防线不许因为这次放开而丢掉。"""
        from app.core.schemas import ZtpTemplateIn
        for bad in ("Vlan-interface10\nsysname pwn", "GE1/0/1\rsysname pwn"):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError) as e:
                    ZtpTemplateIn(name="t", vendor="h3c", mgmt_interface=bad)
                self.assertIn("控制字符", str(e.value))

    def test_generator_rejects_control_chars_in_mgmt_interface(self):
        """绕过 schema 直接调生成器时也要挡住（第 3 层）。"""
        from app.ct.ztp.generator import generate_all
        with pytest.raises(ValueError) as e:
            generate_all(prof("h3c", mgmt_interface="Vlan-interface10\nsysname pwned"), [DEV])
        self.assertIn("控制字符", str(e.value))
