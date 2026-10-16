# -*- coding: utf-8 -*-
"""红线条目：发给装机客户机的 DNS **必须在装机网内**。

线上实测（本会话反复出现）：`/etc/dnsmasq.d/opstk-pxe.conf` 里

    dhcp-option=option:dns-server,10.128.118.1

即**宿主自己的 resolver（企业网 DNS）**被上游填进 `net_config.dns_server`，于是企业网地址
被写进"发给客户机"的 DHCP 选项。装机网到企业网没有路由、客户机拿到也解析不了，
但**生成的配置里带着企业网地址本身就不该发生**。

规则（本次改动）：`dns_server` 只有与 `server_ip` **同网段**（按 `net_config.cidr`，缺省 24）
时才采用，否则回落到网关。越界的值宁可不用，也不发出去。
"""
import sys
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe.generator import generate_all          # noqa: E402
from tests.test_pxe_opensuse_autoyast import cfg       # noqa: E402


def conf_with(**nc_over):
    nc = {"ip": "192.168.199.140", "cidr": 24, "gateway": "192.168.199.1",
          "dns": ["192.168.199.1"], "interface": "ens18"}
    nc.update(nc_over)
    c = cfg(net_mode="static", net_config=nc, server_ip="192.168.199.1")
    return generate_all(c)["dnsmasq.conf"]


class DnsScopeTest(unittest.TestCase):
    def test_enterprise_resolver_is_not_handed_out(self):
        """★ 核心：net_config.dns_server 是企业网地址时，**绝不能**出现在发给客户机的选项里。"""
        conf = conf_with(dns_server="10.128.118.1")
        self.assertNotIn("10.128.118.1", conf, "企业网 DNS 被写进装机配置了")
        self.assertIn("dhcp-option=option:dns-server,192.168.199.1", conf)

    def test_in_net_resolver_is_used(self):
        """同网段的 DNS 仍然照用（不能把孩子和洗澡水一起倒掉）。"""
        conf = conf_with(dns_server="192.168.199.53")
        self.assertIn("dhcp-option=option:dns-server,192.168.199.53", conf)

    def test_other_private_ranges_are_also_rejected(self):
        """不止 10.128.118：任何不同网段的值都不发（含常见的 8.8.8.8、114.114.114.114）。"""
        for bad in ("8.8.8.8", "114.114.114.114", "192.168.100.1", "10.128.118.1"):
            with self.subTest(bad=bad):
                conf = conf_with(dns_server=bad)
                self.assertNotIn(bad, conf)
                self.assertIn("dhcp-option=option:dns-server,192.168.199.1", conf)

    def test_cidr_is_respected(self):
        """按 cidr 判同网段：/16 时应接受 192.168.0.0/16 内的地址。"""
        conf = conf_with(cidr=16, dns_server="192.168.1.1")
        self.assertIn("dhcp-option=option:dns-server,192.168.1.1", conf)
        conf2 = conf_with(cidr=24, dns_server="192.168.1.1")
        self.assertNotIn("192.168.1.1", conf2)

    def test_gateway_option_unaffected(self):
        conf = conf_with(dns_server="10.128.118.1")
        self.assertIn("dhcp-option=option:router,192.168.199.1", conf)


if __name__ == "__main__":
    unittest.main()
