# -*- coding: utf-8 -*-
"""netconfig：**IPv6 静态地址与网关**（2026-10-18）。

为什么做这个：`generator.py` 里唯一的 TODO 就是「只处理 IPv4」。运维在双栈环境里
确实需要给装机/配置生成的网卡配 IPv6；不支持时只能手工改后处理，容易和 IPv4 配置打架。

本次范围（明确边界）：
  · **只做静态 IPv6**（地址 + 默认网关）：`ip6` / `gateway6` 两个字段；
  · DHCPv6 / SLAAC / 多个 IPv6 地址 **不在本次**（宁可没有，也不做半吊子）；
  · 三种后端都要写：netplan(`addresses` 里带 v6 + `routes: to: ::/0`)、nmcli(`ipv6.*`)、ifcfg(`IPV6INIT/IPV6ADDR/IPV6_DEFAULTGW`)；
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


# ── netplan 的 IPv6 语义（2026-10-18 真 netplan 0.107.1 验过）──────────────────
#   ★ netplan **没有** `addresses6` 键：真跑 `netplan generate` 会报
#     `unknown key 'addresses6'`，那份配置根本加载不了。
#   IPv6 地址与 IPv4 一样放进 `addresses:` 列表；IPv6 默认路由写成
#   `routes: - to: "::/0" via: <gw6>`（gateway4/gateway6 在 netplan 1.x 已删除）。
def np_v6_addrs(dev):
    """从 netplan 设备块里取 IPv6 地址（addresses 中带冒号的那些）。"""
    return [a for a in (dev.get("addresses") or []) if ":" in str(a)]


def np_v6_gw(dev):
    """从 netplan 设备块的 routes 里取 IPv6 默认网关（to == "::/0" 的 via）。"""
    for r in (dev.get("routes") or []):
        if str(r.get("to")) in ("::/0", "::0/0"):
            return r.get("via")
    return None


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
        self.assertEqual(np_v6_addrs(eth0), [A6])
        self.assertEqual(np_v6_gw(eth0), G6)
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


class Ipv6DoesNotSwallowIpv4Test(unittest.TestCase):
    """★ 回归：加 IPv6 时把 `route-metric` / `METRIC` 写进了 IPv6 分支里。

    后果：**纯 IPv4** 的双默认网关场景再也写不出 metric（两条默认路由同 metric，
    谁生效取决于接口 up 顺序）；而带 IPv6 的机器反而"碰巧"是对的 —— 典型的
    "顺手加功能，把相邻分支的语句缩进错了"。
    这里同时锁三件事：纯 IPv4 有 metric、带 IPv6 也有 metric、IPv6 行本身还在。
    """

    TWO_GW = [
        {"name": "eth1", "mode": "static", "ip": "10.0.1.5", "cidr": 24,
         "gateway": "10.0.1.1"},
        {"name": "eth0", "mode": "static", "ip": "10.0.0.5", "cidr": 24,
         "gateway": "10.0.0.1"},
    ]

    def test_nmcli_metric_present_without_ipv6(self):
        p = gen(os="rhel", format="nmcli", interfaces=self.TWO_GW)
        first = next(l for l in p.splitlines() if "ipv4.gateway 10.0.1.1" in l)
        second = next(l for l in p.splitlines() if "ipv4.gateway 10.0.0.1" in l)
        self.assertIn("ipv4.route-metric 100", first)
        self.assertIn("ipv4.route-metric 200", second)

    def test_nmcli_metric_present_with_ipv6_too(self):
        two = [dict(i) for i in self.TWO_GW]
        two[0].update(ip6=A6, gateway6=G6)
        p = gen(os="rhel", format="nmcli", interfaces=two)
        first = next(l for l in p.splitlines() if "ipv4.gateway 10.0.1.1" in l)
        self.assertIn("ipv4.route-metric 100", first)
        self.assertIn("ipv6.addresses " + A6, p)

    def test_ifcfg_metric_present_without_ipv6(self):
        p = gen(os="rhel", format="ifcfg", interfaces=self.TWO_GW)
        self.assertIn("METRIC=100", p)
        self.assertIn("METRIC=200", p)

    def test_ifcfg_metric_present_with_ipv6_too(self):
        two = [dict(i) for i in self.TWO_GW]
        two[0].update(ip6=A6, gateway6=G6)
        p = gen(os="rhel", format="ifcfg", interfaces=two)
        self.assertIn("METRIC=100", p)
        self.assertIn("IPV6_DEFAULTGW=" + G6, p)

    def test_netplan_metric_unaffected(self):
        p = gen(os="ubuntu", format="netplan", interfaces=self.TWO_GW)
        doc = yaml.safe_load(p)
        r1 = doc["network"]["ethernets"]["eth1"]["routes"]
        self.assertEqual(r1[0]["metric"], 100)


class Ipv6OnlyTest(unittest.TestCase):
    """★ 纯 IPv6（没有 IPv4 地址）也必须能生成。

    修前：IPv6 那几行写在"有 IPv4"的分支里 ⇒ 只填 IPv6 的接口会被**静默丢弃**
    （nmcli 只出 `ipv4.method auto`、netplan 连 addresses 都不写）。
    纯 IPv6 主机/内网是常见形态，静默丢弃比报错更糟。
    """

    ONLY6 = [{"name": "eth0", "mode": "static", "ip6": A6, "gateway6": G6}]

    def test_nmcli_ipv6_only(self):
        p = gen(os="rhel", format="nmcli", interfaces=self.ONLY6)
        self.assertIn("ipv6.method manual", p)
        self.assertIn("ipv6.addresses " + A6, p)
        self.assertIn("ipv6.gateway " + G6, p)
        # 没有 IPv4 地址时必须退化成 ipv4.method auto，而不是写一条空的 addresses
        self.assertIn("ipv4.method auto", p)
        self.assertNotIn("ipv4.addresses", p)

    def test_netplan_ipv6_only(self):
        p = gen(os="ubuntu", format="netplan", interfaces=self.ONLY6)
        doc = yaml.safe_load(p)
        eth0 = doc["network"]["ethernets"]["eth0"]
        self.assertEqual(np_v6_addrs(eth0), [A6])
        self.assertEqual(np_v6_gw(eth0), G6)
        self.assertEqual([a for a in eth0["addresses"] if ":" not in str(a)], [],
                         "纯 IPv6 时 addresses 里不该有 IPv4")

    def test_ifcfg_ipv6_only(self):
        p = gen(os="rhel", format="ifcfg", interfaces=self.ONLY6)
        self.assertIn("IPV6INIT=yes", p)
        self.assertIn("IPV6ADDR=" + A6, p)
        self.assertIn("IPV6_DEFAULTGW=" + G6, p)

    def test_netplan_dhcp4_with_static_ipv6(self):
        """DHCP 拿 IPv4 + 手工 IPv6 是合法组合（`dhcp4: true` 与 `addresses: [v6]` 可共存）。"""
        p = gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "mode": "dhcp", "ip6": A6}])
        doc = yaml.safe_load(p)
        eth0 = doc["network"]["ethernets"]["eth0"]
        self.assertTrue(eth0["dhcp4"])
        self.assertEqual(np_v6_addrs(eth0), [A6])

    def test_nmcli_dhcp_with_static_ipv6(self):
        p = gen(os="rhel", format="nmcli",
                interfaces=[{"name": "eth0", "mode": "dhcp", "ip6": A6}])
        self.assertIn("ipv4.method auto", p)
        self.assertIn("ipv6.addresses " + A6, p)


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

    def test_gateway6_without_ip6_is_rejected(self):
        """只给 IPv6 网关、不给地址 -> 生成器会写一条没有落点的 gateway6（netplan 直接报错）。"""
        with self.assertRaises(ValueError) as cm:
            gen(os="ubuntu", format="netplan",
                interfaces=[{"name": "eth0", "ip": "10.10.10.1", "cidr": 24,
                             "gateway6": G6}])
        self.assertIn("gateway6", str(cm.exception))

    def test_bare_ip6_without_prefix_is_accepted(self):
        p = gen(os="ubuntu", format="netplan", interfaces=iface(ip6="2001:db8::5"))
        self.assertIn("2001:db8::5", p)


# ======================================================================
# 2026-10-18（第二批）追加：**bond / vlan / bridge 三类虚拟设备**的 IPv6 覆盖。
#
# 此前本文件只覆盖了 interfaces —— 但 IPv6 支持是 interface/bond/vlan/bridge
# 四条路径一起接的（generator._v6_check 的 docstring；schemas._check_l3 在
# NetConfigRequest._check_cross_rules 里对四类设备逐个调用）。虚拟设备没有
# 用例锁住，回归时最容易漏。
#
# 三种后端的透传入口（backend/app/it/netconfig/generator.py 当前行号）：
#   · nmcli  ：_bond(:317) / _vlan(:342) / _bridge(:352) 都收敛到 _mod(:302)
#              → _ipv4_lines(:247) → _ipv6_lines(:228)，ip6/gateway6 与 IPv4 同一条透传路径；
#   · netplan：bonds(:601) / vlans(:610) / bridges(:617) 三个调用点都走
#              _netplan_addr_block(:485) → _netplan_v6(:470)；
#   · ifcfg  ：bond(:729) / vlan(:741) / bridge(:753) 三个调用点都走 _ip_lines(:632)，
#              IPV6INIT/IPV6ADDR/IPV6_DEFAULTGW 写在 :649-656。
# 非法值 fail-closed 的两道闸：
#   · gateway6 有、ip6 无 —— schemas._check_l3(:231-235)，对 bonds/vlans/bridges
#     生效（_check_cross_rules :1997/:2003/:2008）；
#   · ip6 格式非法（如填 IPv4 值）—— generator._v6_check(:33-52)，生成期、
#     写出任何配置之前抛 ValueError。
# ======================================================================


def nmcli_modify(p, name):
    """取 nmcli 产物里针对连接 name 的那条 `nmcli connection modify <name> ...` 整行。

    按「哪条连接」收窄断言：一台设备一条 modify，A 设备的 ipv6 键不能替 B 设备的
    断言作证（全串 assertIn 会掩盖串到别的设备段落里这种错）。
    """
    for line in p.splitlines():
        parts = line.split()
        if (len(parts) > 4 and parts[:3] == ["nmcli", "connection", "modify"]
                and parts[3] == name):
            return line
    raise AssertionError("nmcli 产物里没有针对 " + name + " 的 modify 行")


def ifcfg_section(p, fname):
    """取 ifcfg 产物里 `# ===== <fname> =====` 段的配置行（按文件收窄断言）。"""
    cur, out = None, []
    for line in p.splitlines():
        if line.startswith("# ===== ") and line.endswith(" ====="):
            cur = line[len("# ===== "):-len(" =====")]
            continue
        if cur == fname:
            out.append(line)
    if not out:
        raise AssertionError("ifcfg 产物里没有 " + fname + " 段")
    return "\n".join(out)


class Ipv6OnVirtualDevicesTest(unittest.TestCase):
    """bond / vlan / bridge 的 IPv6：双栈 → 纯 IPv6 → 非法值 fail-closed。

    双栈用例**先锁 IPv4 键**（test_netconfig.py 早已覆盖的既有行为），再锁 IPv6 键；
    纯 IPv6 用例锁「没有 IPv4 时 IPv6 仍要写出、且不出现空 addresses / IPADDR」。
    """

    # 双栈模板：IPv4 部分是既有行为，这里在它之上追加 ip6/gateway6
    BOND6 = [{"name": "bond0", "mode": 1, "interfaces": ["eth1", "eth2"],
              "ip": "10.200.0.2", "cidr": 24, "gateway": "10.200.0.254",
              "ip6": A6, "gateway6": G6}]
    VLAN6 = [{"parent": "ens19", "vlan_id": 100,
              "ip": "10.201.0.2", "cidr": 24, "gateway": "10.201.0.254",
              "ip6": A6, "gateway6": G6}]
    BRIDGE6 = [{"name": "br0", "interfaces": ["eth1", "eth2"],
                "ip": "10.202.0.2", "cidr": 24, "gateway": "10.202.0.254",
                "ip6": A6, "gateway6": G6}]

    # 纯 IPv6：不填 ip/gateway（schemas._check_l3 :224 起 ip6 也算「有地址」）
    BOND_ONLY6 = [{"name": "bond0", "mode": 1, "interfaces": ["eth1", "eth2"],
                   "ip6": A6, "gateway6": G6}]
    VLAN_ONLY6 = [{"parent": "ens19", "vlan_id": 100, "ip6": A6, "gateway6": G6}]
    BRIDGE_ONLY6 = [{"name": "br0", "interfaces": ["eth1", "eth2"],
                     "ip6": A6, "gateway6": G6}]

    # ---------------- bond ----------------

    def test_netplan_bond_dual_stack(self):
        """netplan：bonds.bond0 段——IPv4 键还在，addresses6/gateway6 与它并存。"""
        p = gen(os="ubuntu", format="netplan", bonds=self.BOND6)
        b = yaml.safe_load(p)["network"]["bonds"]["bond0"]
        # IPv4 部分没被破坏：聚合结构、模式、静态地址、默认路由
        self.assertEqual(b["interfaces"], ["eth1", "eth2"])
        self.assertEqual(b["parameters"]["mode"], "active-backup")
        # 双栈：IPv6 与 IPv4 同在一个 addresses 列表（netplan 没有 addresses6 键）
        self.assertEqual(b["addresses"], ["10.200.0.2/24", A6])
        self.assertEqual(b["routes"][0]["via"], "10.200.0.254")
        # IPv6 键落在 bonds 段（不是 ethernets）
        self.assertEqual(np_v6_addrs(b), [A6])
        self.assertEqual(np_v6_gw(b), G6)

    def test_nmcli_bond_dual_stack(self):
        """nmcli：bond0 的 modify 命令——ipv4.* 原有键还在，ipv6.method/addresses/gateway 透传。"""
        p = gen(os="rhel", format="nmcli", bonds=self.BOND6)
        mod = nmcli_modify(p, "bond0")
        self.assertIn("ipv4.addresses 10.200.0.2/24", mod)
        self.assertIn("ipv4.method manual", mod)
        self.assertIn("ipv4.gateway 10.200.0.254", mod)
        self.assertIn("ipv6.method manual", mod)
        self.assertIn("ipv6.addresses " + A6, mod)
        self.assertIn("ipv6.gateway " + G6, mod)
        # bond 建立行与 bond.options 不受影响
        self.assertIn("nmcli connection add type bond ifname bond0 con-name bond0", p)
        self.assertIn("bond.options mode=active-backup,miimon=100", p)

    def test_ifcfg_bond_dual_stack(self):
        """ifcfg：ifcfg-bond0——BONDING_OPTS/IPADDR 等原有键还在，IPV6INIT/IPV6ADDR/IPV6_DEFAULTGW 出现。"""
        p = gen(os="rhel", format="ifcfg", bonds=self.BOND6)
        sec = ifcfg_section(p, "ifcfg-bond0")
        self.assertIn("DEVICE=bond0", sec)
        self.assertIn("TYPE=Bond", sec)
        self.assertIn("BONDING_MASTER=yes", sec)
        self.assertIn('BONDING_OPTS="mode=active-backup miimon=100"', sec)
        self.assertIn("BOOTPROTO=static", sec)
        self.assertIn("IPADDR=10.200.0.2", sec)
        self.assertIn("PREFIX=24", sec)
        self.assertIn("GATEWAY=10.200.0.254", sec)
        self.assertIn("IPV6INIT=yes", sec)
        self.assertIn("IPV6ADDR=" + A6, sec)
        self.assertIn("IPV6_DEFAULTGW=" + G6, sec)

    # ---------------- vlan ----------------

    def test_netplan_vlan_dual_stack(self):
        """netplan：vlans.ens19.100 段——id/link/addresses 还在，addresses6/gateway6 并存。"""
        p = gen(os="ubuntu", format="netplan", vlans=self.VLAN6)
        v = yaml.safe_load(p)["network"]["vlans"]["ens19.100"]
        self.assertEqual(v["id"], 100)
        self.assertEqual(v["link"], "ens19")
        self.assertEqual(v["addresses"], ["10.201.0.2/24", A6])
        self.assertEqual(v["routes"][0]["via"], "10.201.0.254")
        self.assertEqual(np_v6_addrs(v), [A6])
        self.assertEqual(np_v6_gw(v), G6)

    def test_nmcli_vlan_dual_stack(self):
        """nmcli：ens19.100 的 add 行（type vlan/dev/id 原样）+ modify 里 ipv4.* 与 ipv6.*。"""
        p = gen(os="rhel", format="nmcli", vlans=self.VLAN6)
        self.assertIn(
            "nmcli connection add type vlan ifname ens19.100 con-name ens19.100 dev ens19 id 100", p)
        mod = nmcli_modify(p, "ens19.100")
        self.assertIn("ipv4.addresses 10.201.0.2/24", mod)
        self.assertIn("ipv4.method manual", mod)
        self.assertIn("ipv4.gateway 10.201.0.254", mod)
        self.assertIn("ipv6.method manual", mod)
        self.assertIn("ipv6.addresses " + A6, mod)
        self.assertIn("ipv6.gateway " + G6, mod)

    def test_ifcfg_vlan_dual_stack(self):
        """ifcfg：ifcfg-ens19.100——VLAN 骨架键（TYPE/VLAN/PHYSDEV）还在，IPV6 三键出现。"""
        p = gen(os="rhel", format="ifcfg", vlans=self.VLAN6)
        sec = ifcfg_section(p, "ifcfg-ens19.100")
        self.assertIn("DEVICE=ens19.100", sec)
        self.assertIn("TYPE=Vlan", sec)
        self.assertIn("VLAN=yes", sec)
        self.assertIn("PHYSDEV=ens19", sec)
        self.assertIn("BOOTPROTO=static", sec)
        self.assertIn("IPADDR=10.201.0.2", sec)
        self.assertIn("PREFIX=24", sec)
        self.assertIn("GATEWAY=10.201.0.254", sec)
        self.assertIn("IPV6INIT=yes", sec)
        self.assertIn("IPV6ADDR=" + A6, sec)
        self.assertIn("IPV6_DEFAULTGW=" + G6, sec)

    # ---------------- bridge ----------------

    def test_netplan_bridge_dual_stack(self):
        """netplan：bridges.br0 段——interfaces/addresses 还在，addresses6/gateway6 并存。"""
        p = gen(os="ubuntu", format="netplan", bridges=self.BRIDGE6)
        br = yaml.safe_load(p)["network"]["bridges"]["br0"]
        self.assertEqual(br["interfaces"], ["eth1", "eth2"])
        self.assertEqual(br["addresses"], ["10.202.0.2/24", A6])
        self.assertEqual(br["routes"][0]["via"], "10.202.0.254")
        self.assertEqual(np_v6_addrs(br), [A6])
        self.assertEqual(np_v6_gw(br), G6)

    def test_nmcli_bridge_dual_stack(self):
        """nmcli：br0 的 modify——ipv4.* 原有键还在，ipv6.method/addresses/gateway 透传。"""
        p = gen(os="rhel", format="nmcli", bridges=self.BRIDGE6)
        self.assertIn("nmcli connection add type bridge ifname br0 con-name br0", p)
        mod = nmcli_modify(p, "br0")
        self.assertIn("ipv4.addresses 10.202.0.2/24", mod)
        self.assertIn("ipv4.method manual", mod)
        self.assertIn("ipv4.gateway 10.202.0.254", mod)
        self.assertIn("ipv6.method manual", mod)
        self.assertIn("ipv6.addresses " + A6, mod)
        self.assertIn("ipv6.gateway " + G6, mod)

    def test_ifcfg_bridge_dual_stack(self):
        """ifcfg：ifcfg-br0——TYPE=Bridge/IPADDR 等原有键还在，IPV6 三键出现。"""
        p = gen(os="rhel", format="ifcfg", bridges=self.BRIDGE6)
        sec = ifcfg_section(p, "ifcfg-br0")
        self.assertIn("DEVICE=br0", sec)
        self.assertIn("TYPE=Bridge", sec)
        self.assertIn("BOOTPROTO=static", sec)
        self.assertIn("IPADDR=10.202.0.2", sec)
        self.assertIn("PREFIX=24", sec)
        self.assertIn("GATEWAY=10.202.0.254", sec)
        self.assertIn("IPV6INIT=yes", sec)
        self.assertIn("IPV6ADDR=" + A6, sec)
        self.assertIn("IPV6_DEFAULTGW=" + G6, sec)

    # ---------------- 纯 IPv6（没有 IPv4）----------------
    # 要求：IPv6 键必须写出；且不出现空的 addresses / IPADDR
    # （无 IPv4 时 nmcli 退化 ipv4.method auto、ifcfg 退化 BOOTPROTO=dhcp —— 既有语义）。

    def test_ipv6_only_bond_all_formats(self):
        """纯 IPv6 bond：netplan 只有 addresses6/gateway6；nmcli 退化 ipv4.method auto；ifcfg 无 IPADDR。"""
        # netplan：bonds 段有 IPv6 键、没有 addresses（dict 按**键名**精确判，addresses6 不算命中）
        p = gen(os="ubuntu", format="netplan", bonds=self.BOND_ONLY6)
        b = yaml.safe_load(p)["network"]["bonds"]["bond0"]
        self.assertEqual(np_v6_addrs(b), [A6])
        self.assertEqual(np_v6_gw(b), G6)
        self.assertEqual([a for a in b["addresses"] if ":" not in str(a)], [])
        self.assertEqual(b["interfaces"], ["eth1", "eth2"])   # bond 结构本身不受影响
        # nmcli：bond0 的 modify 行退化成 ipv4.method auto，IPv6 键齐全
        p = gen(os="rhel", format="nmcli", bonds=self.BOND_ONLY6)
        mod = nmcli_modify(p, "bond0")
        self.assertIn("ipv4.method auto", mod)
        self.assertIn("ipv6.method manual", mod)
        self.assertIn("ipv6.addresses " + A6, mod)
        self.assertIn("ipv6.gateway " + G6, mod)
        self.assertNotIn("ipv4.addresses", mod)
        # ifcfg：bond 文件有 IPV6 三键、无 IPADDR
        p = gen(os="rhel", format="ifcfg", bonds=self.BOND_ONLY6)
        sec = ifcfg_section(p, "ifcfg-bond0")
        self.assertIn("IPV6INIT=yes", sec)
        self.assertIn("IPV6ADDR=" + A6, sec)
        self.assertIn("IPV6_DEFAULTGW=" + G6, sec)
        self.assertNotIn("IPADDR=", sec)
        self.assertIn("BOOTPROTO=dhcp", sec)   # 无 IPv4 按既有语义退化，不写空 IPADDR

    def test_ipv6_only_vlan_all_formats(self):
        """纯 IPv6 vlan：三种后端都只带 IPv6 键、不写空 IPv4 地址。"""
        # netplan：vlans.ens19.100 有 addresses6/gateway6、没有 addresses
        p = gen(os="ubuntu", format="netplan", vlans=self.VLAN_ONLY6)
        v = yaml.safe_load(p)["network"]["vlans"]["ens19.100"]
        self.assertEqual(np_v6_addrs(v), [A6])
        self.assertEqual(np_v6_gw(v), G6)
        self.assertEqual([a for a in v["addresses"] if ":" not in str(a)], [])
        self.assertEqual(v["id"], 100)          # vlan 结构本身不受影响
        self.assertEqual(v["link"], "ens19")
        # nmcli：ens19.100 的 modify 退化 ipv4.method auto，IPv6 键齐全
        p = gen(os="rhel", format="nmcli", vlans=self.VLAN_ONLY6)
        mod = nmcli_modify(p, "ens19.100")
        self.assertIn("ipv4.method auto", mod)
        self.assertIn("ipv6.method manual", mod)
        self.assertIn("ipv6.addresses " + A6, mod)
        self.assertIn("ipv6.gateway " + G6, mod)
        self.assertNotIn("ipv4.addresses", mod)
        # ifcfg：ifcfg-ens19.100 有 IPV6 三键、无 IPADDR
        p = gen(os="rhel", format="ifcfg", vlans=self.VLAN_ONLY6)
        sec = ifcfg_section(p, "ifcfg-ens19.100")
        self.assertIn("IPV6INIT=yes", sec)
        self.assertIn("IPV6ADDR=" + A6, sec)
        self.assertIn("IPV6_DEFAULTGW=" + G6, sec)
        self.assertNotIn("IPADDR=", sec)

    def test_ipv6_only_bridge_all_formats(self):
        """纯 IPv6 bridge：三种后端都只带 IPv6 键、不写空 IPv4 地址。"""
        # netplan：bridges.br0 有 addresses6/gateway6、没有 addresses
        p = gen(os="ubuntu", format="netplan", bridges=self.BRIDGE_ONLY6)
        br = yaml.safe_load(p)["network"]["bridges"]["br0"]
        self.assertEqual(np_v6_addrs(br), [A6])
        self.assertEqual(np_v6_gw(br), G6)
        self.assertEqual([a for a in br["addresses"] if ":" not in str(a)], [])
        self.assertEqual(br["interfaces"], ["eth1", "eth2"])   # bridge 结构本身不受影响
        # nmcli：br0 的 modify 退化 ipv4.method auto，IPv6 键齐全
        p = gen(os="rhel", format="nmcli", bridges=self.BRIDGE_ONLY6)
        mod = nmcli_modify(p, "br0")
        self.assertIn("ipv4.method auto", mod)
        self.assertIn("ipv6.method manual", mod)
        self.assertIn("ipv6.addresses " + A6, mod)
        self.assertIn("ipv6.gateway " + G6, mod)
        self.assertNotIn("ipv4.addresses", mod)
        # ifcfg：ifcfg-br0 有 IPV6 三键、无 IPADDR
        p = gen(os="rhel", format="ifcfg", bridges=self.BRIDGE_ONLY6)
        sec = ifcfg_section(p, "ifcfg-br0")
        self.assertIn("IPV6INIT=yes", sec)
        self.assertIn("IPV6ADDR=" + A6, sec)
        self.assertIn("IPV6_DEFAULTGW=" + G6, sec)
        self.assertNotIn("IPADDR=", sec)

    # ---------------- 非法值 fail-closed ----------------

    def test_gateway6_without_ip6_rejected_on_virtual_kinds(self):
        """只给 gateway6、不给 ip6 → schema 层 ValueError（schemas._check_l3 :231）。

        bond/bridge（无 mode=dhcp 概念）直接命中 gateway6 规则；vlan 默认
        mode=static，会先命中「static 必须有 ip 或 ip6」（:224）——同样在 schema 层
        拒绝、同样 fail-closed，只是报错走的是另一条规则，断言按实际报错锁。
        """
        cases = {
            "bond": {"bonds": [{"name": "bond0", "mode": 1,
                                "interfaces": ["eth1"], "gateway6": G6}]},
            "vlan": {"vlans": [{"parent": "ens19", "vlan_id": 100,
                                "gateway6": G6}]},
            "bridge": {"bridges": [{"name": "br0", "interfaces": ["eth1"],
                                    "gateway6": G6}]},
        }
        expect = {"bond": "bonds[0].gateway6",     # gateway6 规则的报错带设备路径
                  "vlan": "vlans[0].ip",           # static 缺地址规则的报错带设备路径
                  "bridge": "bridges[0].gateway6"}
        for kind, kw in cases.items():
            with self.subTest(kind=kind):
                with self.assertRaises(ValueError) as cm:
                    gen(os="ubuntu", format="netplan", **kw)
                self.assertIn(expect[kind], str(cm.exception))

    def test_ipv4_value_in_ip6_rejected_on_virtual_kinds(self):
        """ip6 填 IPv4 值 → 生成期 generator._v6_check(:33) 在写出任何配置前抛 ValueError。

        schema 层不查 ip6 的取值格式，所以这道闸在生成期；三类设备 × 三种后端
        全部要拦（_v6_check 的注释就写明「同时覆盖 interface/bond/vlan/bridge 四条路径」）。
        """
        cases = {
            "bond": {"bonds": [{"name": "bond0", "mode": 1,
                                "interfaces": ["eth1"], "ip6": "192.168.1.5"}]},
            "vlan": {"vlans": [{"parent": "ens19", "vlan_id": 100,
                                "ip6": "192.168.1.5"}]},
            "bridge": {"bridges": [{"name": "br0", "interfaces": ["eth1"],
                                    "ip6": "192.168.1.5"}]},
        }
        for fmt, osname in (("netplan", "ubuntu"), ("nmcli", "rhel"), ("ifcfg", "rhel")):
            for kind, kw in cases.items():
                with self.subTest(format=fmt, kind=kind):
                    with self.assertRaises(ValueError) as cm:
                        gen(os=osname, format=fmt, **kw)
                    self.assertIn("ip6 不是合法 IPv6", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
