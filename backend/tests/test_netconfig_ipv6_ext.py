# -*- coding: utf-8 -*-
"""netconfig 的 IPv6 扩展（2026-10-18）：DHCPv6 / SLAAC / 多地址 / IPv6 额外路由。

这三项是上一轮"明确未完成"的清单：DHCPv6 与 SLAAC 参数、一个接口多个 IPv6 地址、
IPv6 的额外路由（原来 routes 只认 IPv4 的 to/via）。这里对**三种后端**逐一锁住，
并把 fail-closed 的边界也钉死（矛盾配置不允许静默丢弃）。

判定口径：断言具体键与值（不是"含 ipv6 字样"），否则等于没测。
"""
import sys
import unittest

import yaml

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import NetConfigRequest          # noqa: E402
from app.it.netconfig.generator import generate_netconfig   # noqa: E402

A6 = "2001:db8::5/64"
A6B = "2001:db8::6/64"
G6 = "2001:db8::1"


def gen(**kw):
    out = generate_netconfig(NetConfigRequest(**kw))
    return out[0] if isinstance(out, tuple) else out[0]


def iface(**extra):
    d = {"name": "eth0", "ip": "10.10.10.1", "cidr": 24, "gateway": "10.10.10.254"}
    d.update(extra)
    return [d]


def netplan_dev(p, kind="ethernets", name="eth0"):
    return yaml.safe_load(p)["network"][kind][name]


class Dhcpv6AndSlaacTest(unittest.TestCase):
    def test_netplan_dhcp6(self):
        p = gen(os="ubuntu", format="netplan", interfaces=iface(ip6_mode="dhcp6"))
        dev = netplan_dev(p)
        self.assertTrue(dev["dhcp6"])
        # IPv4 仍在 addresses 里（这是 IPv4 静态），要断言的是**没有 IPv6 地址**
        self.assertEqual([a for a in (dev.get("addresses") or []) if ":" in str(a)], [],
                         "DHCPv6 模式不该有静态 IPv6 地址")

    def test_netplan_slaac(self):
        p = gen(os="ubuntu", format="netplan", interfaces=iface(ip6_mode="slaac"))
        dev = netplan_dev(p)
        self.assertTrue(dev["accept-ra"])
        self.assertFalse(dev["dhcp6"])

    def test_nmcli_dhcp6_and_slaac(self):
        p = gen(os="rhel", format="nmcli", interfaces=iface(ip6_mode="dhcp6"))
        self.assertIn("ipv6.method dhcp", p)
        self.assertNotIn("ipv6.addresses", p)
        p2 = gen(os="rhel", format="nmcli", interfaces=iface(ip6_mode="slaac"))
        self.assertIn("ipv6.method auto", p2)
        self.assertNotIn("ipv6.addresses", p2)

    def test_ifcfg_dhcp6_and_slaac(self):
        p = gen(os="rhel", format="ifcfg", interfaces=iface(ip6_mode="dhcp6"))
        self.assertIn("IPV6INIT=yes", p)
        self.assertIn("DHCPV6C=yes", p)
        self.assertNotIn("IPV6ADDR=", p)
        p2 = gen(os="rhel", format="ifcfg", interfaces=iface(ip6_mode="slaac"))
        self.assertIn("IPV6_AUTOCONF=yes", p2)
        self.assertIn("DHCPV6C=no", p2)

    def test_dhcp6_conflicts_with_static_v6(self):
        """DHCPv6/SLAAC 与静态地址/网关互斥 —— 必须 422，不能静默丢一半。"""
        for mode in ("dhcp6", "slaac"):
            for extra in ({"ip6": A6}, {"ip6_addrs": [A6]}, {"gateway6": G6}):
                with self.subTest(mode=mode, extra=extra):
                    with self.assertRaises(ValueError):
                        gen(os="ubuntu", format="netplan",
                            interfaces=iface(ip6_mode=mode, **extra))

    def test_bad_mode_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            gen(os="ubuntu", format="netplan", interfaces=iface(ip6_mode="auto6"))
        self.assertIn("ip6_mode", str(cm.exception))


class MultipleIpv6AddressesTest(unittest.TestCase):
    def test_netplan_multiple(self):
        p = gen(os="ubuntu", format="netplan",
                interfaces=iface(ip6=A6, ip6_addrs=[A6B], gateway6=G6))
        dev = netplan_dev(p)
        self.assertEqual([a for a in dev["addresses"] if ":" in a], [A6, A6B])

    def test_nmcli_multiple(self):
        p = gen(os="rhel", format="nmcli",
                interfaces=iface(ip6=A6, ip6_addrs=[A6B], gateway6=G6))
        # _sh 会给"一个参数内含空格"的值加单引号（nmcli 要求整体是一个 shell 词）
        self.assertIn("ipv6.addresses '" + A6 + " " + A6B + "'", p)

    def test_ifcfg_multiple(self):
        p = gen(os="rhel", format="ifcfg",
                interfaces=iface(ip6=A6, ip6_addrs=[A6B], gateway6=G6))
        self.assertIn("IPV6ADDR=" + A6, p)
        self.assertIn("IPV6ADDR_SECONDARIES=" + A6B, p)

    def test_ip6_addrs_only_is_enough(self):
        """只填 ip6_addrs（ip6 空）也算"有 IPv6 地址"，不能被 static 必填规则误杀。"""
        p = gen(os="ubuntu", format="netplan", interfaces=iface(ip6_addrs=[A6]))
        self.assertEqual([a for a in netplan_dev(p)["addresses"] if ":" in a], [A6])

    def test_duplicate_address_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            gen(os="ubuntu", format="netplan", interfaces=iface(ip6=A6, ip6_addrs=[A6]))
        self.assertIn("重复", str(cm.exception))

    def test_bad_extra_address_is_rejected_with_index(self):
        with self.assertRaises(ValueError) as cm:
            gen(os="ubuntu", format="netplan", interfaces=iface(ip6_addrs=["192.168.1.5"]))
        self.assertIn("ip6_addrs[0]", str(cm.exception))


class Ipv6RoutesTest(unittest.TestCase):
    """额外路由支持 IPv6（原来 routes 只认 IPv4）。"""

    REQ = dict(os="ubuntu", format="netplan")

    def test_netplan_ipv6_route(self):
        p = gen(**self.REQ, interfaces=iface(routes=[
            {"to": "2001:db8:1::/64", "via": G6}]))
        routes = netplan_dev(p)["routes"]
        self.assertIn({"to": "2001:db8:1::/64", "via": G6}, routes)

    def test_netplan_ipv6_default_route_uses_slash_zero(self):
        """`::/0` 必须原样保留 —— 归一成 default 就变成 IPv4 默认路由了。"""
        p = gen(**self.REQ, interfaces=iface(routes=[{"to": "::/0", "via": G6}]))
        routes = netplan_dev(p)["routes"]
        self.assertIn({"to": "::/0", "via": G6}, routes)
        # IPv4 默认路由仍在（iface() 带了 gateway），但它是 default + IPv4 下一跳
        v4def = [r for r in routes if r["to"] == "default"]
        self.assertEqual(len(v4def), 1)
        self.assertEqual(v4def[0]["via"], "10.10.10.254")

    def test_nmcli_ipv6_route_uses_ipv6_key(self):
        p = gen(os="rhel", format="nmcli", interfaces=iface(routes=[
            {"to": "2001:db8:1::/64", "via": G6, "metric": 20}]))
        self.assertIn("+ipv6.routes '" + "2001:db8:1::/64 " + G6 + ",20'", p)
        self.assertNotIn("+ipv4.routes 2001:db8:1::/64", p)

    def test_ifcfg_ipv6_route_goes_to_route6_file(self):
        p = gen(os="rhel", format="ifcfg", interfaces=iface(routes=[
            {"to": "2001:db8:1::/64", "via": G6}]))
        self.assertIn("route6-eth0", p)
        # 文件内容里是 `目标 via 下一跳`
        idx = p.index("route6-eth0")
        self.assertIn("2001:db8:1::/64 via " + G6, p[idx:idx + 400])

    def test_v4_and_v6_routes_are_split_by_family(self):
        p = gen(os="rhel", format="ifcfg", interfaces=iface(routes=[
            {"to": "10.20.0.0/16", "via": "10.10.10.254"},
            {"to": "2001:db8:1::/64", "via": G6}]))
        self.assertIn("route-eth0", p)
        self.assertIn("route6-eth0", p)

    def test_family_mismatch_is_rejected(self):
        for to, via in (("2001:db8:1::/64", "10.10.10.254"), ("10.20.0.0/16", G6)):
            with self.subTest(to=to, via=via):
                with self.assertRaises(ValueError):
                    gen(**self.REQ, interfaces=iface(routes=[{"to": to, "via": via}]))

    def test_ipv6_default_route_requires_via(self):
        with self.assertRaises(ValueError):
            gen(**self.REQ, interfaces=iface(routes=[{"to": "::/0"}]))


if __name__ == "__main__":
    unittest.main()
