# -*- coding: utf-8 -*-
"""netconfig：**额外路由**（用户 2026-10-15：「应该支持添加路由的功能」）。

改前的形状：三个产物都只有一条从 `gateway` 字段派生的默认路由，运维**没有任何地方**
能加静态路由（截图里 netplan 只有 `routes: - to: default`）。本文件钉住新增能力：

  · netplan  —— 默认路由与额外路由**合成一个 `routes:` 键**（YAML 重复键会让后者覆盖前者，
                那正是"加了静态路由、默认路由却没了"的失败形状）；
  · nmcli    —— 每条额外路由一个 `+ipv4.routes`（`+` 是**追加**，不加会把 ipv4.gateway
                建立的默认路由一起清掉）；
  · ifcfg    —— 落到 `/etc/sysconfig/network-scripts/route-<ifname>`（RHEL 系里 ifcfg
                只认默认网关 GATEWAY=，静态路由的规范落点就是这个文件）；
  · 校验     —— to 只能是 default 或 IPv4 CIDR、default 必须有 via、metric 有范围、
                以及"这些值会进 YAML/nmcli/ifcfg"的注入面必须被挡住。
"""
import sys
import unittest

import yaml

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import NetConfigRequest          # noqa: E402
from app.it.netconfig.generator import generate_netconfig   # noqa: E402


def gen(**kw):
    """生成并把返回值归一成字符串（netplan 返回 (text, filename)）。"""
    out = generate_netconfig(NetConfigRequest(**kw))
    return out[0] if isinstance(out, tuple) else out


EXTRA = [{"to": "10.20.0.0/16", "via": "10.10.10.1", "metric": 200},
         {"to": "192.168.5.0/24", "via": "10.10.10.5"}]


class NetplanRoutesTest(unittest.TestCase):
    def _doc(self, text):
        return yaml.safe_load(text)

    def test_default_and_extra_routes_share_one_routes_key(self):
        p = gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "gateway": "10.10.10.254", "routes": EXTRA}])
        # ★ 只能出现一个 `routes:` 键（YAML 里重复键 = 后者覆盖前者）
        self.assertEqual(p.count("      routes:"), 1, p)
        rs = self._doc(p)["network"]["ethernets"]["eth0"]["routes"]
        self.assertEqual(rs[0], {"to": "default", "via": "10.10.10.254"})
        self.assertEqual(rs[1], {"to": "10.20.0.0/16", "via": "10.10.10.1", "metric": 200})
        self.assertEqual(rs[2], {"to": "192.168.5.0/24", "via": "10.10.10.5"})

    def test_routes_without_gateway_still_emitted(self):
        """只填静态路由、不填网关是合法配置（改前这里根本不会输出 routes）。"""
        p = gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24, "routes": EXTRA}])
        rs = self._doc(p)["network"]["ethernets"]["eth0"]["routes"]
        self.assertEqual(len(rs), 2)
        self.assertNotIn("default", [r["to"] for r in rs])

    def test_dhcp_interface_keeps_its_static_routes(self):
        """DHCP 拿地址 + 手工补静态路由是常见需求，改前 dhcp 分支直接 return 会丢光。"""
        p = gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "mode": "dhcp",
                             "routes": [{"to": "10.30.0.0/16", "via": "10.10.10.3"}]}])
        d = self._doc(p)["network"]["ethernets"]["eth0"]
        self.assertTrue(d["dhcp4"])
        self.assertEqual(d["routes"], [{"to": "10.30.0.0/16", "via": "10.10.10.3"}])

    def test_bond_routes_land_under_the_bond(self):
        p = gen(os="ubuntu", format="netplan",
                bonds=[{"name": "bond0", "interfaces": ["eth0", "eth1"],
                        "ip": "10.10.20.1", "cidr": 24, "gateway": "10.10.20.254",
                        "routes": [{"to": "172.16.0.0/12", "via": "10.10.20.254",
                                    "metric": 300}]}])
        rs = self._doc(p)["network"]["bonds"]["bond0"]["routes"]
        self.assertEqual(rs[0]["to"], "default")
        self.assertEqual(rs[1], {"to": "172.16.0.0/12", "via": "10.10.20.254", "metric": 300})

    def test_on_link_is_rendered(self):
        p = gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "routes": [{"to": "10.99.0.0/16", "on_link": True}]}])
        rs = self._doc(p)["network"]["ethernets"]["eth0"]["routes"]
        self.assertEqual(rs[0], {"to": "10.99.0.0/16", "on-link": True})

    def test_no_routes_no_routes_key(self):
        """回归红线：没配路由时产物里**不该**多出 routes 段（老请求逐字节不变）。"""
        p = gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24}])
        self.assertNotIn("routes:", p)


class NmcliRoutesTest(unittest.TestCase):
    def test_each_extra_route_is_appended_not_assigned(self):
        s = gen(os="ubuntu", format="nmcli",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "gateway": "10.10.10.254",
                             "routes": [{"to": "10.20.0.0/16", "via": "10.10.10.1",
                                         "metric": 200}]}])
        self.assertIn("ipv4.gateway 10.10.10.254", s)
        # `+` 前缀 = 追加；用 `ipv4.routes`（不带 +）会把默认路由清掉
        self.assertIn("+ipv4.routes '10.20.0.0/16 10.10.10.1,200'", s)
        self.assertNotIn(" ipv4.routes '", s)

    def test_default_route_becomes_zero_slash_zero(self):
        s = gen(os="ubuntu", format="nmcli",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "routes": [{"to": "default", "via": "10.10.10.9", "metric": 50}]}])
        self.assertIn("+ipv4.routes '0.0.0.0/0 10.10.10.9,50'", s)

    def test_route_without_via_renders_destination_only(self):
        s = gen(os="ubuntu", format="nmcli",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "routes": [{"to": "10.99.0.0/16"}]}])
        # 单个词不含 shell 特殊字符 ⇒ _sh 不加引号（这是既有行为，不必强行加）
        self.assertIn("+ipv4.routes 10.99.0.0/16", s)


class IfcfgRoutesTest(unittest.TestCase):
    def test_route_files_are_emitted_per_device(self):
        s = gen(os="rhel", format="ifcfg",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "gateway": "10.10.10.254", "routes": EXTRA}],
                bonds=[{"name": "bond0", "interfaces": ["eth1", "eth2"],
                        "ip": "10.10.20.1", "cidr": 24,
                        "routes": [{"to": "172.16.0.0/12", "via": "10.10.20.254"}]}])
        self.assertIn("# ===== route-eth0 =====", s)
        self.assertIn("10.20.0.0/16 via 10.10.10.1 metric 200", s)
        self.assertIn("192.168.5.0/24 via 10.10.10.5", s)
        self.assertIn("# ===== route-bond0 =====", s)
        self.assertIn("172.16.0.0/12 via 10.10.20.254", s)

    def test_vlan_and_bridge_get_route_files_too(self):
        s = gen(os="rhel", format="ifcfg",
                vlans=[{"parent": "eth0", "vlan_id": 100, "ip": "10.10.100.1", "cidr": 24,
                        "routes": [{"to": "10.9.0.0/24", "via": "10.10.100.254"}]}],
                bridges=[{"name": "br0", "interfaces": ["eth9"], "ip": "10.10.200.1",
                          "cidr": 24, "mode": "static",
                          "routes": [{"to": "10.8.0.0/24", "via": "10.10.200.254"}]}])
        self.assertIn("# ===== route-eth0.100 =====", s)
        self.assertIn("10.9.0.0/24 via 10.10.100.254", s)
        self.assertIn("# ===== route-br0 =====", s)
        self.assertIn("10.8.0.0/24 via 10.10.200.254", s)

    def test_no_routes_no_route_file(self):
        s = gen(os="rhel", format="ifcfg",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24}])
        self.assertNotIn("route-eth0", s)


class RouteValidationTest(unittest.TestCase):
    """路由字段会进 YAML 标量 / nmcli 参数 / ifcfg 文件内容 ⇒ 校验必须在 HTTP 层就 422。"""

    def _iface(self, **route):
        return NetConfigRequest(os="ubuntu", format="netplan",
                                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                                             "routes": [route]}])

    def test_to_accepts_default_cidr_and_bare_ip(self):
        self._iface(to="default", via="10.0.0.1")
        self._iface(to="10.20.0.0/16")
        # 0.0.0.0/0 与 default 同义 → 归一成 default
        r = NetConfigRequest(os="ubuntu", format="netplan",
                             interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                                          "routes": [{"to": "0.0.0.0/0", "via": "10.0.0.1"}]}])
        self.assertEqual(str(r.interfaces[0].routes[0].to), "default")
        # 裸 IP = /32 主机路由（netplan 与 nmcli 都是这个语义），归一成显式 x/32
        r2 = NetConfigRequest(os="ubuntu", format="netplan",
                              interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                                           "routes": [{"to": "10.20.0.5"}]}])
        self.assertEqual(str(r2.interfaces[0].routes[0].to), "10.20.0.5/32")

    def test_to_rejects_host_bits_and_junk(self):
        for bad in ("not-a-net", "10.20.0.0/33",
                    # ★ 主机位非零：拒绝而不是静默改成 10.20.0.0/16 ——
                    #   路由目标被悄悄改写比直接报错难查得多
                    "10.20.0.1/16",
                    "10.20.0.0/16\n  - to: default",   # 换行 = YAML 注入面
                    "2001:db8::/64"):                  # 本模块只做 IPv4，明确拒绝
            with self.assertRaises(Exception, msg=bad):
                self._iface(to=bad)

    def test_default_route_requires_via(self):
        with self.assertRaises(Exception):
            self._iface(to="default")

    def test_via_must_be_ipv4(self):
        for bad in ("10.0.0.999", "fe80::1", "10.0.0.1; rm -rf /", "$(id)"):
            with self.assertRaises(Exception, msg=bad):
                self._iface(to="10.20.0.0/16", via=bad)

    def test_metric_range_and_types(self):
        self._iface(to="10.20.0.0/16", metric=0)
        self._iface(to="10.20.0.0/16", metric=4294967295)
        # 数字字符串照旧接受（脚本/前端传 "100" 很常见，语义无歧义）
        r = NetConfigRequest(os="ubuntu", format="netplan",
                             interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                                          "routes": [{"to": "10.20.0.0/16", "metric": "100"}]}])
        self.assertEqual(r.interfaces[0].routes[0].metric, 100)
        # ★ 布尔值必须拒绝：pydantic 的 lax 模式会把 True 静默变成 1
        #   （一个布尔变成 metric 1 是"配错了但看不出来"的典型）
        for bad in (-1, 4294967296, "abc", True, False):
            with self.assertRaises(Exception, msg=bad):
                self._iface(to="10.20.0.0/16", metric=bad)


if __name__ == "__main__":
    unittest.main()
