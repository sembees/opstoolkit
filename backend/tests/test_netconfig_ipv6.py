# -*- coding: utf-8 -*-
"""netconfig：**IPv6 静态地址与网关**（2026-10-18）。

为什么做这个：`generator.py` 里唯一的 TODO 就是「只处理 IPv4」。运维在双栈环境里
确实需要给装机/配置生成的网卡配 IPv6；不支持时只能手工改后处理，容易和 IPv4 配置打架。

本次范围（明确边界）：
  · **只做静态 IPv6**（地址 + 默认网关）：`ip6` / `gateway6` 两个字段；
  · DHCPv6 / SLAAC / 多个 IPv6 地址 **不在本次**（宁可没有，也不做半吊子）；
  · 三种后端都要写：netplan(`addresses6` + `gateway6`)、nmcli(`ipv6.*`)、ifcfg(`IPV6INIT/IPV6ADDR/IPV6_DEFAULTGW`)；
  · 非法值 **fail-closed**（生成期 ValueError），绝不写出半截配置。
"""
import sys
import unittest

import yaml

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import NetConfigRequest          # noqa: E402
from app.it.netconfig.generator import generate_netconfig   # noqa: E402

A6 = "2001:db8::5/64"
G6 = "2001:db8::1"


def gen(**kw):
    out = generate_netconfig(NetConfigRequest(**kw))
    return out[0] if isinstance(out, tuple) else out


def iface(**extra):
    d = {"name": "eth0", "ip": "10.10.10.1", "cidr": 24, "gateway": "10.10.10.254"}
    d.update(extra)
    return [d]


class NetplanIpv6Test(unittest.TestCase):
    def test_addresses6_and_gateway6(self):
        p = gen(os="ubuntu", format="netplan", interfaces=iface(ip6=A6, gateway6=G6))
        doc = yaml.safe_load(p)
        eth0 = doc["network"]["ethernets"]["eth0"]
        self.assertEqual(eth0["addresses6"], [A6])
        self.assertEqual(eth0["gateway6"], G6)
        # IPv4 部分必须原样保留（不能因为加了 IPv6 把 IPv4 丢掉）
        self.assertIn("10.10.10.1/24", eth0["addresses"])

    def test_no_ipv6_keys_when_absent(self):
        """没填就不该出现任何 IPv6 键（避免给单栈机器塞空配置）。"""
        p = gen(os="ubuntu", format="netplan", interfaces=iface())
        self.assertNotIn("addresses6", p)
        self.assertNotIn("gateway6", p)


class NmcliIpv6Test(unittest.TestCase):
    def test_ipv6_lines(self):
        p = gen(os="rhel", format="nmcli", interfaces=iface(ip6=A6, gateway6=G6))
        self.assertIn("ipv6.method manual", p)
        self.assertIn("ipv6.addresses " + A6, p)
        self.assertIn("ipv6.gateway " + G6, p)
        # IPv4 仍在
        self.assertIn("ipv4.addresses 10.10.10.1/24", p)
        self.assertIn("ipv4.gateway 10.10.10.254", p)

    def test_gateway_only(self):
        p = gen(os="rhel", format="nmcli", interfaces=iface(ip6=A6))
        self.assertIn("ipv6.addresses " + A6, p)
        self.assertNotIn("ipv6.gateway", p)


class IfcfgIpv6Test(unittest.TestCase):
    def test_ipv6_keys(self):
        p = gen(os="rhel", format="ifcfg", interfaces=iface(ip6=A6, gateway6=G6))
        self.assertIn("IPV6INIT=yes", p)
        self.assertIn("IPV6ADDR=" + A6, p)
        self.assertIn("IPV6_DEFAULTGW=" + G6, p)
        self.assertIn("IPADDR=10.10.10.1", p)

    def test_no_ipv6_when_absent(self):
        p = gen(os="rhel", format="ifcfg", interfaces=iface())
        self.assertNotIn("IPV6INIT", p)
        self.assertNotIn("IPV6ADDR", p)


class Ipv6ValidationTest(unittest.TestCase):
    """非法值必须 fail-closed —— 半截配置比没有更糟。"""

    def test_ipv4_in_ip6_field_is_rejected(self):
        for bad in ("192.168.1.5", "10.0.0.1/24", "not-an-ip", "2001:db8::zz"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    gen(os="ubuntu", format="netplan", interfaces=iface(ip6=bad))

    def test_gateway6_with_prefix_is_rejected(self):
        with self.assertRaises(ValueError):
            gen(os="ubuntu", format="netplan", interfaces=iface(ip6=A6, gateway6="2001:db8::1/64"))

    def test_gateway6_must_be_ipv6(self):
        with self.assertRaises(ValueError):
            gen(os="ubuntu", format="netplan", interfaces=iface(ip6=A6, gateway6="10.10.10.254"))

    def test_bare_ip6_without_prefix_is_accepted(self):
        p = gen(os="ubuntu", format="netplan", interfaces=iface(ip6="2001:db8::5"))
        self.assertIn("2001:db8::5", p)


if __name__ == "__main__":
    unittest.main()
