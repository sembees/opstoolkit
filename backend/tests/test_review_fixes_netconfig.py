# -*- coding: utf-8 -*-
"""外部审查（MiMo v2.6-pro）在 netconfig / 鉴权 / 接口层报出的问题 —— 逐条回归。

对应审查编号：
  U4-F1  服务控制/部署/ISO 删除**没有角色校验**（任意登录用户能停掉 dnsmasq）
  U4-F2  bond 的 mode 是整数，冲突检查对它调 .lower() 抛异常被吞 ⇒ 整份提示丢失（我写的 bug）
  U4-F3  netplan 的 bond 参数写成 miimon（应为 mii-monitor-interval）
  U4-F5  bond 的 primary/lacp_rate/xmit_hash_policy 未白名单 ⇒ 可注入额外 bond 选项
  U4-F7  download 的 Content-Disposition 直接拼 hostname（非 ASCII 500 / 头注入）
  U4-F8  netmask/cidr 不校验（非连续掩码被静默算成 24、cidr=64 直接生成）
  U4-F9  netconfig 的 ValueError 未转 422（与 pxe 侧不一致）
  U4-F11 netplan renderer 未白名单（`|` / `>` 会吞掉整份 YAML 缩进块）
  U7-F2  bond/bridge 的从接口名绕过 `_require_ifname` 白名单
  U7-F7  bond/bridge/vlan 缺引用闭合校验 + **netplan 产物不自包含**（实证：真 netplan
         因为「从接口没在 ethernets 里定义」拒绝整份配置 —— 既有用例正是这个形状）
"""
import os
import pathlib
import sys
import types
import unittest

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.netconfig import conflicts  # noqa: E402
from app.it.netconfig.generator import (  # noqa: E402
    _bond_opt_value,
    _netmask_to_cidr,
    _prefix,
    generate_netconfig,
)


def _iface(name="eth0", ip=None, mode="static"):
    return types.SimpleNamespace(name=name, ip=ip, mode=mode)


class BondModeConflictTest(unittest.TestCase):
    """U4-F2：bond 的 mode 是整数（聚合模式），不能当 static/dhcp 用。"""

    def test_bond_with_static_ip_is_reported(self):
        req = types.SimpleNamespace(
            interfaces=[], bonds=[_iface("bond0", "192.168.199.150", mode=1)],
            vlans=[], bridges=[])
        got = conflicts.collect_static_addresses(req)
        self.assertEqual(got, [("bond bond0", "192.168.199.150")])

    def test_bond_does_not_kill_the_whole_list(self):
        """以前会 AttributeError 被吞掉 ⇒ 连接口上的真实冲突也一起丢。"""
        from app.core.schemas import NetConfigRequest
        req = NetConfigRequest(format="nmcli", os="ubuntu", hostname="h",
                               interfaces=[{"name": "eth0", "mode": "static",
                                            "ip": "192.168.199.150", "prefix": 24}],
                               bonds=[{"name": "bond0", "mode": 1, "interfaces": ["eth1"],
                                       "ip": "192.168.199.151", "cidr": 24}])
        addrs = conflicts.collect_static_addresses(req)
        self.assertIn(("接口 eth0", "192.168.199.150"), addrs)
        self.assertIn(("bond bond0", "192.168.199.151"), addrs)

    def test_dhcp_interface_is_skipped(self):
        req = types.SimpleNamespace(
            interfaces=[_iface("eth0", "10.0.0.5", mode="dhcp")], bonds=[], vlans=[],
            bridges=[])
        self.assertEqual(conflicts.collect_static_addresses(req), [])


class NetplanBondParamTest(unittest.TestCase):
    def test_uses_kebab_case_mii_monitor_interval(self):
        from app.core.schemas import NetConfigRequest
        req = NetConfigRequest(
            format="netplan", os="ubuntu", hostname="h", netplan_renderer="networkd",
            bonds=[{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"],
                    "miimon": 100, "ip": "10.0.0.5", "cidr": 24}])
        out, _ = generate_netconfig(req)
        self.assertIn("mii-monitor-interval: 100", out)
        self.assertNotIn("miimon:", out)


class NetplanRendererWhitelistTest(unittest.TestCase):
    """U4-F11 的核实结论：**第 2 层（schema）早就白名单化了**，所以它不是可达漏洞。

    这里锁两件事：(1) schema 会拒；(2) 绕过 schema 直调生成器时，生成器自己也拒
    （纵深防御 —— 审查提的正是"若 schemas 白名单被放宽"）。
    """

    BAD = ("|", ">", "&a x", "*a", "networkd\n  evil:")

    def test_schema_rejects_yaml_structural_renderer(self):
        from app.core.schemas import NetConfigRequest
        for bad in self.BAD:
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    NetConfigRequest(format="netplan", os="ubuntu", hostname="h",
                                     netplan_renderer=bad,
                                     interfaces=[{"name": "eth0", "mode": "static",
                                                  "ip": "10.0.0.5", "prefix": 24}])

    def test_generator_guard_is_independent(self):
        class _R:
            def __init__(self, **kw):
                self.__dict__.update(kw)

            def model_dump(self):
                return dict(self.__dict__)

        for bad in self.BAD:
            with self.subTest(bad=bad):
                req = _R(format="netplan", os="ubuntu", hostname="h",
                         netplan_renderer=bad, bonds=[], vlans=[], bridges=[],
                         interfaces=[_R(name="eth0", mode="static", ip="10.0.0.5",
                                        cidr=24, netmask=None, gateway=None, dns=[])])
                with pytest.raises(ValueError):
                    generate_netconfig(req)

    def test_supported_renderers_pass(self):
        from app.core.schemas import NetConfigRequest
        for good in ("networkd", "NetworkManager"):
            req = NetConfigRequest(format="netplan", os="ubuntu", hostname="h",
                                   netplan_renderer=good,
                                   interfaces=[{"name": "eth0", "mode": "static",
                                                "ip": "10.0.0.5", "prefix": 24}])
            out, _ = generate_netconfig(req)
            self.assertIn("renderer: " + good, out)


class NetmaskAndCidrTest(unittest.TestCase):
    def test_non_contiguous_netmask_is_rejected(self):
        with pytest.raises(ValueError):
            _netmask_to_cidr("255.255.0.255")

    def test_wrong_segment_count_is_rejected(self):
        for bad in ("255.255", "255.255.255.0.0", "abc", "255.255.255.300"):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    _netmask_to_cidr(bad)

    def test_good_masks_still_work(self):
        self.assertEqual(_netmask_to_cidr("255.255.255.0"), 24)
        self.assertEqual(_netmask_to_cidr("255.255.254.0"), 23)

    def test_cidr_out_of_range_is_rejected(self):
        for bad in (64, -1):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    _prefix({"cidr": bad})
        self.assertEqual(_prefix({"cidr": 30}), 30)


class BondOptionInjectionTest(unittest.TestCase):
    def test_comma_value_is_rejected(self):
        with pytest.raises(ValueError):
            _bond_opt_value("eth0,mode=broadcast", "bonds[].primary", "ifname")

    def test_space_value_is_rejected(self):
        with pytest.raises(ValueError):
            _bond_opt_value("eth0 arp_interval=1", "bonds[].primary", "ifname")

    def test_enum_whitelist(self):
        with pytest.raises(ValueError):
            _bond_opt_value("layer2,arp_interval=1000", "bonds[].xmit_hash_policy")
        with pytest.raises(ValueError):
            _bond_opt_value("veryfast", "bonds[].lacp_rate")
        self.assertEqual(_bond_opt_value("fast", "bonds[].lacp_rate"), "fast")
        self.assertEqual(_bond_opt_value("layer2+3", "bonds[].xmit_hash_policy"), "layer2+3")

    def test_good_ifname_passes(self):
        self.assertEqual(_bond_opt_value("eth0", "bonds[].primary", "ifname"), "eth0")


class BondBridgeInterfaceValidationTest(unittest.TestCase):
    """U7-F2：bond/bridge 的从接口名原来完全绕过 `_require_ifname`。"""

    BAD = ("eth0;rm -rf /", "eth0\nport=0", "eth 0", "eth0`id`")

    def test_bond_slaves_must_match_whitelist(self):
        from app.core.schemas import NetBondIn
        for bad in self.BAD:
            with self.subTest(bad=bad):
                with pytest.raises(ValueError) as e:
                    NetBondIn(name="bond0", mode=1, interfaces=[bad])
                self.assertIn("interface name", str(e.value))

    def test_bridge_slaves_must_match_whitelist(self):
        from app.core.schemas import NetBridgeIn
        for bad in self.BAD:
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    NetBridgeIn(name="br0", interfaces=[bad])

    def test_empty_and_duplicate_slaves_are_rejected(self):
        from app.core.schemas import NetBridgeIn
        with pytest.raises(ValueError):
            NetBridgeIn(name="br0", interfaces=[])
        with pytest.raises(ValueError) as e:
            NetBridgeIn(name="br0", interfaces=["eth0", "eth0"])
        self.assertIn("重复", str(e.value))

    def test_bond_primary_must_be_ifname(self):
        from app.core.schemas import NetBondIn
        with pytest.raises(ValueError):
            NetBondIn(name="bond0", mode=1, interfaces=["eth0"],
                      primary="eth0,mode=broadcast")
        self.assertEqual(NetBondIn(name="bond0", mode=1, interfaces=["eth0"],
                                   primary="eth1").primary, "eth1")

    def test_valid_slaves_pass(self):
        from app.core.schemas import NetBondIn
        b = NetBondIn(name="bond0", mode=4, interfaces=["eth0", "eth1"])
        self.assertEqual(b.interfaces, ["eth0", "eth1"])


class DataDiskIdentFormatTest(unittest.TestCase):
    """U7-F1（按我复核后的准确范围）：稳定标识的**格式**校验。

    "必须有 size/serial/wwid 之一"这条 schema 里早就有；缺的是格式 ——
    size 写错格式会导致 %pre 匹配不上任何盘（装机白跑一趟），
    serial/wwid 里的引号/空白会破坏被拼进去的 awk 匹配串。
    """

    PARTS = [{"mount": "/boot/efi", "size": "512M", "fstype": "fat32"},
             {"mount": "/boot", "size": "1G", "fstype": "ext4"},
             {"mount": "swap", "size": "8G"},
             {"mount": "/", "size": "rest", "fstype": "ext4"}]

    def _profile(self, dd):
        from app.core.schemas import PxeProfileIn
        return PxeProfileIn(
            name="t", os_type="ubuntu", os_version="22.04",
            admin_password="Test@123", disk_scheme="custom",
            disk_config={"target": {"mode": "name", "name": "sda"},
                         "layout": "custom", "partitions": self.PARTS,
                         "data_disks": [dd]})

    def test_bad_size_format_is_rejected(self):
        for bad in ("30GB x", "abc", "30G G"):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError) as e:
                    self._profile({"name": "sdb", "size": bad})
                self.assertIn("size", str(e.value))

    def test_good_sizes_pass(self):
        for good in ("30G", "500M", "32212254720", "1.5T"):
            with self.subTest(good=good):
                self.assertTrue(self._profile({"name": "sdb", "size": good}))

    def test_serial_or_wwid_with_shell_chars_is_rejected(self):
        for key in ("serial", "wwid"):
            with self.subTest(key=key):
                with pytest.raises(ValueError) as e:
                    self._profile({"name": "sdb", key: 'x"; rm -rf /'})
                self.assertIn(key, str(e.value))

    def test_good_serial_and_wwid_pass(self):
        self.assertTrue(self._profile({"name": "sdb", "serial": "S3Z1NB0K123456"}))
        self.assertTrue(self._profile({"name": "sdb", "wwid": "naa.6000c29a-1b2c-3d4e"}))


class PxeInputSurfaceTest(unittest.TestCase):
    """U7-F5 / F6 / F11：PXE 入参模型的三个洞（保存能过、生成期才炸或可注入）。"""

    def test_disk_scheme_whitelist(self):
        from app.core.schemas import PxeProfileIn
        for bad in ("zfs", "lvm2", "raid"):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError) as e:
                    PxeProfileIn(name="t", os_type="ubuntu", admin_password="Test@123",
                                 disk_scheme=bad)
                self.assertIn("disk_scheme", str(e.value))
        # 大小写/空白归一后合法（"CUSTOM " 是**应当接受**的写法）
        self.assertEqual(PxeProfileIn(name="t", os_type="ubuntu", admin_password="T@1",
                                      disk_scheme="Direct").disk_scheme, "direct")
        self.assertEqual(PxeProfileIn(name="t", os_type="ubuntu", admin_password="T@1",
                                      disk_scheme="CUSTOM ").disk_scheme, "custom")

    def test_extra_repos_element_validation(self):
        from app.core.schemas import PxeGenerateIn
        with pytest.raises(ValueError) as e:
            PxeGenerateIn(extra_repos=["http://ok/repo\nrepo --name=x"])
        self.assertIn("控制字符", str(e.value))
        with pytest.raises(ValueError):
            PxeGenerateIn(extra_repos=["repo --name=evil"])
        got = PxeGenerateIn(extra_repos=["http://10.0.0.1/repo", " https://x/y "])
        self.assertEqual(got.extra_repos, ["http://10.0.0.1/repo", "https://x/y"])

    def test_id_path_charset(self):
        from app.core.schemas import PxeDiskTargetIn
        with pytest.raises(ValueError) as e:
            PxeDiskTargetIn(mode="match", id_path="pci-0000:00:05.0-scsi-0:0:0:1\nx")
        self.assertIn("id_path", str(e.value))
        good = "pci-0000:00:05.0-scsi-0:0:0:1"
        self.assertEqual(PxeDiskTargetIn(mode="match", id_path=good).id_path, good)

    def test_target_matchers_reject_control_chars(self):
        from app.core.schemas import PxeDiskTargetIn
        with pytest.raises(ValueError):
            PxeDiskTargetIn(mode="match", serial="SN1\nport=0")


class NetConfigReferenceClosureTest(unittest.TestCase):
    """U7-F7：bond/bridge/vlan 的**引用闭合**。

    规则不是拍脑袋定的，是在 ubuntu:22.04（netplan 0.107.1-3ubuntu0.22.04.5，即目标
    装机系统）里把生成结果喂给真 `netplan generate` 一条条试出来的：
      · 从接口/父接口没在本文件里定义过 → 整份配置被拒（这条由**生成器**补 ethernets 解决，
        见 NetplanImplicitEthernetsTest，所以 schema 侧**不**拒绝这种形状）；
      · 同一网卡被两个聚合引用 → "already assigned to bond bond0" → 拒绝；
      · 设备名撞名（含 VLAN 子接口名）→ "changes device type" / 静默丢一份 → 拒绝；
      · primary 不是本 bond 成员 → netplan 放过（EXIT=0），但内核不生效 → 拒绝；
      · 聚合互相嵌套成环 → netplan 静默接受 → 拒绝。
    """

    def _req(self, **kw):
        from app.core.schemas import NetConfigRequest
        base = dict(os="rhel", format="nmcli")
        base.update(kw)
        return NetConfigRequest(**base)

    def test_duplicate_device_name_is_rejected(self):
        with pytest.raises(ValueError) as e:
            self._req(interfaces=[{"name": "eth0", "mode": "dhcp"},
                                  {"name": "eth0", "mode": "dhcp"}])
        self.assertIn("重复", str(e.value))

    def test_bond_name_colliding_with_iface_is_rejected(self):
        with pytest.raises(ValueError) as e:
            self._req(interfaces=[{"name": "bond0", "mode": "dhcp"}],
                      bonds=[{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"]}])
        self.assertIn("重复", str(e.value))

    def test_duplicate_vlan_subinterface_is_rejected(self):
        with pytest.raises(ValueError) as e:
            self._req(vlans=[{"parent": "eth0", "vlan_id": 10, "mode": "dhcp"},
                             {"parent": "eth0", "vlan_id": 10, "mode": "dhcp"}])
        self.assertIn("eth0.10", str(e.value))

    def test_vlan_name_colliding_with_declared_iface_is_rejected(self):
        """真 netplan 会以 "changes device type" 拒绝整份配置。"""
        with pytest.raises(ValueError) as e:
            self._req(interfaces=[{"name": "eth0.10", "mode": "dhcp"},
                                  {"name": "eth0", "mode": "dhcp"}],
                      vlans=[{"parent": "eth0", "vlan_id": 10, "mode": "dhcp"}])
        self.assertIn("changes device type", str(e.value))

    def test_slave_in_two_aggregates_is_rejected(self):
        with pytest.raises(ValueError) as e:
            self._req(bonds=[{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"]}],
                      bridges=[{"name": "br0", "interfaces": ["eth0", "eth2"]}])
        msg = str(e.value)
        self.assertIn("eth0", msg)
        self.assertIn("同时引用", msg)

    def test_bond_primary_must_be_a_member_port(self):
        with pytest.raises(ValueError) as e:
            self._req(bonds=[{"name": "bond0", "mode": 1,
                              "interfaces": ["eth0", "eth1"], "primary": "eth9"}])
        self.assertIn("primary", str(e.value))

    def test_aggregate_cannot_be_its_own_slave(self):
        with pytest.raises(ValueError) as e:
            self._req(bonds=[{"name": "bond0", "mode": 1, "interfaces": ["bond0"]}])
        self.assertIn("自身", str(e.value))

    def test_aggregate_reference_cycle_is_rejected(self):
        """bond0 套 bond1、bond1 又套 bond0：netplan 实测静默接受，实际是自指环。"""
        with pytest.raises(ValueError) as e:
            self._req(bonds=[{"name": "bond0", "mode": 1, "interfaces": ["bond1"]},
                             {"name": "bond1", "mode": 1, "interfaces": ["bond0"]}])
        self.assertIn("成环", str(e.value))

    def test_problems_are_reported_together(self):
        """一次请求里的多个闭合问题要一并报出，而不是让运维改一个撞一个。"""
        with pytest.raises(ValueError) as e:
            self._req(interfaces=[{"name": "eth0", "mode": "dhcp"},
                                  {"name": "eth0", "mode": "dhcp"}],
                      bonds=[{"name": "bond0", "mode": 1,
                              "interfaces": ["eth9"], "primary": "eth8"}])
        msg = str(e.value)
        self.assertIn("重复", msg)
        self.assertIn("primary", msg)

    def test_undeclared_slaves_are_allowed_on_purpose(self):
        """从接口/父接口没单独声明**不**报错：生成器会补 ethernets。

        这是刻意的分工（也是 ifcfg 分支早就支持的形状 —— 从接口没声明就单独生成一份
        ifcfg-<slave>），所以这里把"允许"钉住，避免以后有人把它改成 422
        而打死前端 bond 行的默认用法（从接口默认 eth0,eth1）。
        """
        req = self._req(interfaces=[{"name": "ens33", "mode": "dhcp"}],
                        bonds=[{"name": "bond0", "mode": 1, "interfaces": ["ens35", "ens36"],
                                "ip": "10.1.1.2", "cidr": 24}],
                        vlans=[{"parent": "ens99", "vlan_id": 100, "mode": "dhcp"}])
        self.assertEqual(len(req.bonds[0].interfaces), 2)


class NetplanImplicitEthernetsTest(unittest.TestCase):
    """U7-F7 的根因修复：netplan 产物必须自包含。

    改前只有显式写了 interfaces[] 才输出 `ethernets:`，于是最常见的用法
    （加一个 bond、从接口填 ens35,ens36、不再单独加两行物理接口 —— 前端 bond 行的默认值
    就是 eth0,eth1）生成出来的 YAML 在真机上是**加载不了**的：
    `netplan generate` 报 "bond0: interface 'ens35' is not defined" 并拒绝整份文件，
    机器应用后就是没网。既有用例 test_netplan_full 正是这个形状，因为只断言字符串而没暴露。
    """

    def _script(self, **kw):
        from app.core.schemas import NetConfigRequest
        req = NetConfigRequest(os="ubuntu", format="netplan", hostname="h", **kw)
        script, filename = generate_netconfig(req)
        self.assertEqual(filename, "99-opstk.yaml")
        return script

    def _ethernet_names(self, script):
        """取出 ethernets: 段里的设备名（4 空格缩进的键）。"""
        lines = script.splitlines()
        try:
            start = lines.index("  ethernets:")
        except ValueError:
            return []
        out = []
        for ln in lines[start + 1:]:
            if not ln.startswith("    ") or ln.startswith("      "):
                if ln.strip() and not ln.startswith("    "):
                    break
                continue
            out.append(ln.strip().rstrip(":"))
        return out

    def test_undeclared_bond_slaves_and_bridge_ports_are_added(self):
        script = self._script(
            interfaces=[{"name": "ens33", "mode": "dhcp"}],
            bonds=[{"name": "bond0", "mode": 4, "interfaces": ["ens35", "ens36"],
                    "ip": "10.1.1.2", "cidr": 24}],
            bridges=[{"name": "br0", "interfaces": ["ens37"], "ip": "10.2.2.2", "cidr": 24}],
        )
        self.assertEqual(self._ethernet_names(script), ["ens33", "ens35", "ens36", "ens37"])
        for name in ("ens35", "ens36", "ens37"):
            self.assertEqual(script.count("    %s:" % name), 1, script)

    def test_undeclared_vlan_parent_is_added(self):
        script = self._script(vlans=[{"parent": "ens99", "vlan_id": 100,
                                      "mode": "static", "ip": "10.3.3.2", "cidr": 24}])
        self.assertEqual(self._ethernet_names(script), ["ens99"])
        self.assertIn("      link: ens99", script)

    def test_declared_slaves_are_not_duplicated(self):
        """声明过的从接口只出现一次（重复键 netplan 会报 changes device type）。"""
        script = self._script(
            interfaces=[{"name": "ens35", "mode": "static", "ip": "10.1.1.3", "cidr": 24},
                        {"name": "ens36", "mode": "static", "ip": "10.1.1.4", "cidr": 24}],
            bonds=[{"name": "bond0", "mode": 1, "interfaces": ["ens35", "ens36"],
                    "ip": "10.1.1.2", "cidr": 24}],
        )
        self.assertEqual(self._ethernet_names(script), ["ens35", "ens36"])
        # 从接口不配地址（生成器整段跳过），仍然是 dhcp4: false
        self.assertEqual(script.count("    ens35:"), 1)

    def test_bond_over_vlan_is_not_redeclared_as_ethernet(self):
        """从接口是 VLAN 子接口时，不能再把它当 ethernet 输出一遍（会 changes device type）。"""
        script = self._script(
            interfaces=[{"name": "ens40", "mode": "dhcp"}],
            vlans=[{"parent": "ens40", "vlan_id": 100, "mode": "dhcp"}],
            bonds=[{"name": "bond0", "mode": 1, "interfaces": ["ens40.100"]}],
        )
        self.assertEqual(self._ethernet_names(script), ["ens40"])
        self.assertEqual(script.count("    ens40.100:"), 1)

    def test_no_interfaces_rows_at_all_still_emits_ethernets(self):
        """连一行物理接口都没有（只有 bond）时也必须输出 ethernets: —— 改前这里直接没有该段。"""
        script = self._script(
            bonds=[{"name": "bond0", "mode": 1, "interfaces": ["ens50", "ens51"],
                    "primary": "ens50", "ip": "10.4.4.2", "cidr": 24}],
            bridges=[{"name": "br5", "interfaces": ["bond0"]}],
        )
        self.assertEqual(self._ethernet_names(script), ["ens50", "ens51"])
        self.assertIn("      interfaces: [bond0]", script)

    def test_output_is_byte_stable(self):
        """同一请求两次生成逐字节一致（自动补的名字按排序，不看 set 迭代顺序）。"""
        kw = dict(
            bonds=[{"name": "bond0", "mode": 1, "interfaces": ["ensB", "ensA"]}],
            bridges=[{"name": "br0", "interfaces": ["ensC"]}],
        )
        self.assertEqual(self._script(**kw), self._script(**kw))
        self.assertEqual(self._ethernet_names(self._script(**kw)), ["ensA", "ensB", "ensC"])

    def test_legacy_test_netplan_full_shape_is_self_contained(self):
        """把既有用例 test_netplan_full 的形状原样钉住（它在真 netplan 上曾是 EXIT=1）。"""
        script = self._script(
            interfaces=[{"name": "ens33", "mode": "dhcp"},
                        {"name": "ens34", "mode": "static", "ip": "10.0.0.5", "cidr": 24,
                         "gateway": "10.0.0.1", "dns": ["10.0.0.1"]}],
            bonds=[{"name": "bond0", "mode": 4, "interfaces": ["ens35", "ens36"],
                    "ip": "10.1.1.2", "cidr": 24, "gateway": "10.1.1.1"}],
            vlans=[{"parent": "bond0", "vlan_id": 10, "mode": "static",
                    "ip": "10.1.10.2", "cidr": 24}],
            bridges=[{"name": "br0", "interfaces": ["ens37"], "ip": "10.2.2.2", "cidr": 24}],
        )
        self.assertEqual(self._ethernet_names(script),
                         ["ens33", "ens34", "ens35", "ens36", "ens37"])


class RequireRoleTest(unittest.TestCase):
    """U4-F1：破坏性接口必须要求 admin 角色。"""

    def _client(self, role):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import pxe as pxe_api
        from app.core.auth import get_current_user

        app = FastAPI()
        app.include_router(pxe_api.router, prefix="/api/it/pxe")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "role": role}
        return TestClient(app)

    def test_non_admin_cannot_stop_dnsmasq(self):
        r = self._client("viewer").post("/api/it/pxe/server/service", json={"action": "stop"})
        self.assertEqual(r.status_code, 403, r.text)
        self.assertIn("权限", r.text)

    def test_missing_role_is_treated_as_no_permission(self):
        r = self._client("").post("/api/it/pxe/server/service", json={"action": "stop"})
        self.assertEqual(r.status_code, 403, r.text)

    def test_admin_passes_the_role_gate(self):
        r = self._client("admin").post("/api/it/pxe/server/service", json={"action": "status"})
        self.assertNotEqual(r.status_code, 403, r.text)
