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
