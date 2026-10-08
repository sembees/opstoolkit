# -*- coding: utf-8 -*-
"""serve_binding / range_inside_binding / dnsmasq 绑卡 的单元测试。

背景（红线）——模板字段 net_config.interface 的语义是**被装机器**的网卡名
（前端标签「网卡名」，占位符 ens33），但生成器早期把它也当成服务端 dnsmasq 的
`interface=`。多网卡 PXE 服务器上两者不同：本项目实测 ens18 = 10.128.118.113
（企业网，绝对不许在上面开 DHCP）、ens19 = 192.168.199.1（隔离装机网）。
按前者绑就会把 DHCP 开到企业网上，因此服务端绑卡改为按 server_ip 反查。
"""
import socket
import unittest
from unittest import mock

from app.it.pxe import generator, server

try:  # fcntl 只在 Linux 上有：Windows 开发机上这些用例必须显式跳过，而不是 error
    import fcntl  # noqa: F401
    HAS_FCNTL = True
except ImportError:  # pragma: no cover
    HAS_FCNTL = False

SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B


def _ioctl_blob(ip: str) -> bytes:
    """ifreq 结构：地址在偏移 20..24（与 detect_network 的取法一致）。"""
    return b"\x00" * 20 + socket.inet_aton(ip) + b"\x00" * 232


def _fake_ioctl(addr_map, mask_map, explode=False):
    def _ioctl(_fd, code, data):
        if explode:
            raise OSError(99, "Cannot assign requested address")
        name = data.rstrip(b"\x00").decode()
        if code == SIOCGIFADDR:
            if name in addr_map:
                return _ioctl_blob(addr_map[name])
            raise OSError(99, "Cannot assign requested address")
        if code == SIOCGIFNETMASK:
            if name in mask_map:
                return _ioctl_blob(mask_map[name])
            raise OSError(99, "Cannot assign requested address")
        raise OSError(22, "bad code")
    return _ioctl


ADDRS = {"ens18": "10.128.118.113", "ens19": "192.168.199.1"}
MASKS = {"ens18": "255.255.255.0", "ens19": "255.255.255.0"}
NICS = [(1, "lo"), (2, "ens18"), (3, "ens19")]


@unittest.skipUnless(HAS_FCNTL, "需要 Linux 的 fcntl 才能打桩 ioctl")
class ServeBindingTest(unittest.TestCase):
    def _patch(self, addr_map=None, mask_map=None, explode=False):
        return [
            mock.patch.object(server._dhcp, "is_linux", return_value=True),
            mock.patch.object(socket, "if_nameindex", return_value=list(NICS)),
            mock.patch("fcntl.ioctl",
                       _fake_ioctl(ADDRS if addr_map is None else addr_map,
                                   MASKS if mask_map is None else mask_map,
                                   explode=explode)),
        ]

    def test_picks_nic_holding_target_ip(self):
        patches = self._patch()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        got = server.serve_binding("192.168.199.1")
        self.assertEqual(got.get("interface"), "ens19")
        self.assertEqual(got.get("ip"), "192.168.199.1")
        self.assertEqual(got.get("netmask"), "255.255.255.0")
        self.assertEqual(got.get("prefixlen"), 24)

    def test_picks_management_nic_for_its_own_ip(self):
        """反查是对称的：企业网地址照样反查得到，但它只是被识别，不会被当成绑卡默认值。"""
        patches = self._patch()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(server.serve_binding("10.128.118.113").get("interface"), "ens18")

    def test_unknown_ip_returns_empty(self):
        patches = self._patch()
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(server.serve_binding("10.0.0.99"), {})

    def test_empty_ip_returns_empty(self):
        self.assertEqual(server.serve_binding(""), {})
        self.assertEqual(server.serve_binding(None), {})

    def test_non_linux_returns_empty(self):
        with mock.patch.object(server._dhcp, "is_linux", return_value=False):
            self.assertEqual(server.serve_binding("192.168.199.1"), {})

    def test_ioctl_failure_returns_empty(self):
        patches = self._patch(explode=True)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.assertEqual(server.serve_binding("192.168.199.1"), {})

    def test_netmask_failure_keeps_interface_without_prefix(self):
        """掩码取不到时仍要给出网卡名（绑卡这件事已确定），只把前缀长留空。"""
        patches = self._patch(mask_map={})
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        got = server.serve_binding("192.168.199.1")
        self.assertEqual(got.get("interface"), "ens19")
        self.assertIsNone(got.get("prefixlen"))
        self.assertFalse(server.range_inside_binding("192.168.199.100", "192.168.199.200", got))


class RangeInsideBindingTest(unittest.TestCase):
    B24 = {"interface": "ens19", "ip": "192.168.199.1",
           "netmask": "255.255.255.0", "prefixlen": 24}
    B25 = {"interface": "ens19", "ip": "192.168.199.1",
           "netmask": "255.255.255.128", "prefixlen": 25}

    def test_inside_true(self):
        self.assertTrue(server.range_inside_binding("192.168.199.100", "192.168.199.200", self.B24))

    def test_outside_false(self):
        """池起点落在 /25 之外（同网段其它子网）必须为 False。"""
        self.assertFalse(server.range_inside_binding("192.168.199.200", "192.168.199.250", self.B25))

    def test_other_subnet_false(self):
        """企业网池 vs 隔离网卡：这正是红线要拦的形状。"""
        self.assertFalse(server.range_inside_binding("10.128.118.100", "10.128.118.200", self.B24))

    def test_missing_binding_false(self):
        self.assertFalse(server.range_inside_binding("192.168.199.100", "192.168.199.200", {}))
        self.assertFalse(server.range_inside_binding("192.168.199.100", "192.168.199.200", None))

    def test_invalid_input_false(self):
        self.assertFalse(server.range_inside_binding("not-an-ip", "192.168.199.200", self.B24))
        self.assertFalse(server.range_inside_binding("", "", self.B24))

    def test_reversed_range_false(self):
        self.assertFalse(server.range_inside_binding("192.168.199.200", "192.168.199.100", self.B24))


class DnsmasqBindingTest(unittest.TestCase):
    """生成器必须只认 server_interface；没有它时逐字保持旧行为。"""

    def _cfg(self, net_config):
        return generator.PxeConfig(net_config=net_config, server_ip="192.168.199.1",
                                  deploy_mode="standalone", http_root="http://192.168.199.1:8000/pxe/serve")

    def test_server_interface_wins_over_client_interface(self):
        cfg = self._cfg({"interface": "ens18", "server_interface": "ens19",
                         "dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200",
                         "gateway": "192.168.199.1"})
        out = generator._dnsmasq(cfg, [])
        self.assertIn("interface=ens19", out)
        self.assertNotIn("interface=ens18", out)
        self.assertIn("dhcp-range=192.168.199.100,192.168.199.200,12h", out)

    def test_without_server_interface_keeps_old_behaviour(self):
        cfg = self._cfg({"interface": "ens18", "dhcp_start": "192.168.199.100",
                         "dhcp_end": "192.168.199.200", "gateway": "192.168.199.1"})
        out = generator._dnsmasq(cfg, [])
        self.assertIn("interface=ens18", out)
        self.assertNotIn("interface=ens19", out)

    def test_client_interface_still_used_in_kickstart(self):
        """客户端那份 network 行必须继续用它自己的网卡名（ens18），不能被服务端绑卡污染。"""
        cfg = self._cfg({"interface": "ens18", "server_interface": "ens19",
                         "ip": "192.168.199.140", "netmask": "255.255.255.0",
                         "gateway": "192.168.199.1", "dns_server": "192.168.199.1"})
        cfg.os_type = "rocky"
        cfg.os_version = "9.4"
        cfg.net_mode = "static"
        cfg.root_password = "E2e!Rocky9#Root"
        ks = generator._rhel_ks(cfg)
        self.assertIn("--device=ens18", ks)
        self.assertIn("--ip=192.168.199.140", ks)


class DeployRedlineCheckTest(unittest.TestCase):
    """部署前红线判定的纯函数（路由层只剩「非 None 就 422」）。

    判据来源：本机部署会把配置交给**宿主 root dnsmasq** 加载，standalone 会在
    interface= 那张网卡上直接发地址。本项目 ens18=10.128.118.113 是企业网。
    """
    BIND_ISOLATED = {"interface": "ens19", "ip": "192.168.199.1",
                     "netmask": "255.255.255.0", "prefixlen": 24}
    BIND_CORP = {"interface": "ens18", "ip": "10.128.118.113",
                 "netmask": "255.255.255.0", "prefixlen": 24}
    POOL_OK = {"dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200"}

    def setUp(self):
        from app.api import pxe as api_pxe
        self.api_pxe = api_pxe

    def test_isolated_explicit_ip_passes(self):
        self.assertIsNone(self.api_pxe._deploy_redline_check(
            "192.168.199.1", "standalone", self.POOL_OK, self.BIND_ISOLATED, auto_ip=False))

    def test_unknown_iface_rejected(self):
        msg = self.api_pxe._deploy_redline_check(
            "192.168.199.1", "standalone", self.POOL_OK, {}, auto_ip=False)
        self.assertIsNotNone(msg)
        self.assertIn("无法确定 server_ip", msg)

    def test_standalone_with_autodetected_ip_rejected(self):
        """★ 最关键的一条：探测出来的 server_ip 就是默认路由网卡（企业网 ens18），
        「池 ⊆ 该网卡网段」的自洽检查拦不住它，所以 standalone 一律要求显式给出。"""
        corp_pool = {"dhcp_start": "10.128.118.191", "dhcp_end": "10.128.118.253"}
        msg = self.api_pxe._deploy_redline_check(
            "10.128.118.113", "standalone", corp_pool, self.BIND_CORP, auto_ip=True)
        self.assertIsNotNone(msg)
        self.assertIn("自动探测", msg)
        # 同一个自洽的池，显式给出时不再被「自动探测」这条拦（但仍是企业网 —— 说明这条
        # 判据保护的是「手滑默认部署」，不是替运维判断哪个网段是企业网）
        self.assertIsNone(self.api_pxe._deploy_redline_check(
            "10.128.118.113", "standalone", corp_pool, self.BIND_CORP, auto_ip=False))

    def test_proxy_and_relay_with_autodetected_ip_pass(self):
        for mode in ("proxy", "relay"):
            self.assertIsNone(self.api_pxe._deploy_redline_check(
                "10.128.118.113", mode, {}, self.BIND_CORP, auto_ip=True))

    def test_pool_outside_iface_subnet_rejected(self):
        msg = self.api_pxe._deploy_redline_check(
            "192.168.199.1", "standalone",
            {"dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.250"},
            {"interface": "ens19", "ip": "192.168.199.1",
             "netmask": "255.255.255.128", "prefixlen": 25}, auto_ip=False)
        self.assertIsNotNone(msg)
        self.assertIn("不在 server_ip", msg)

    def test_default_pool_on_isolated_iface_rejected(self):
        """存量模板没写池键时生成器用 192.168.1.x 默认值：与装机网卡不同网段，必须拒。"""
        msg = self.api_pxe._deploy_redline_check(
            "192.168.199.1", "standalone", {}, self.BIND_ISOLATED, auto_ip=False)
        self.assertIsNotNone(msg)
        self.assertIn("192.168.1.100", msg)


class NetConfigSchemaSinkTest(unittest.TestCase):
    """net_config.server_interface 是生成器 `"interface=" + iface` 的新数据源，
    必须和旧的 interface 走同一套网卡名白名单 —— 否则它就是个绕过校验的注入点。"""

    def test_newline_in_server_interface_rejected(self):
        from pydantic import ValidationError
        from app.core.schemas import PxeNetConfigIn
        with self.assertRaises(ValidationError):
            PxeNetConfigIn(server_interface="ens19\ndhcp-script=/tmp/x")

    def test_shell_metachar_rejected(self):
        from pydantic import ValidationError
        from app.core.schemas import PxeNetConfigIn
        with self.assertRaises(ValidationError):
            PxeNetConfigIn(server_interface="ens19;rm -rf /")

    def test_valid_name_kept_and_serialized(self):
        from app.core.schemas import PxeNetConfigIn
        cfg = PxeNetConfigIn(interface="ens18", server_interface="ens19")
        self.assertEqual(cfg.server_interface, "ens19")
        dumped = cfg.model_dump()
        self.assertEqual(dumped.get("server_interface"), "ens19")
        self.assertEqual(dumped.get("interface"), "ens18")

    def test_unset_server_interface_serializes_absent(self):
        from app.core.schemas import PxeNetConfigIn
        dumped = PxeNetConfigIn(interface="ens18").model_dump()
        self.assertNotIn("server_interface", dumped)


if __name__ == "__main__":
    unittest.main()
