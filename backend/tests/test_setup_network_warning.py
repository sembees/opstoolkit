# -*- coding: utf-8 -*-
"""向导第 4 步「网络准备」的风险提示回归（2026-10-08 审计发现）。

背景：`/setup/network` 直通 `pxe_server.detect_network()`，在 .113 上默认路由是
**ens18 / 企业网**，向导于是把 `ens18 / 10.128.118.113 / 池 10.128.118.191-253`
当"建议值"展示 —— 照它部署就是把 DHCP 开在企业网上。部署侧守卫虽会拒
（standalone + 自动探测 IP → 422；池不在绑卡网段 → 422），但**向导本身必须说清风险**。

本单元钉两件事：
  · 纯函数 `_default_route_warning(iface, gateway)` 的三种取值；
  · `detect_network()` 一旦给出网关（= 建议值来自默认路由），输出里就必须带上这条告警。
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.pxe import server as pxe_server  # noqa: E402


class DefaultRouteWarningTest(unittest.TestCase):
    def test_warns_when_iface_and_gateway_present(self):
        msg = pxe_server._default_route_warning("ens18", "10.128.118.1")
        self.assertIn("默认路由", msg)
        self.assertIn("ens18", msg)
        self.assertIn("10.128.118.1", msg)
        self.assertIn("隔离网卡", msg)
        # 必须点名红线网段，运维一眼能看懂为什么危险
        self.assertIn("10.128.118.0/24", msg)

    def test_silent_without_iface_or_gateway(self):
        self.assertEqual(pxe_server._default_route_warning("", "10.128.118.1"), "")
        self.assertEqual(pxe_server._default_route_warning("ens18", ""), "")
        self.assertEqual(pxe_server._default_route_warning("", ""), "")

    def test_detect_network_surfaces_warning_when_gateway_found(self):
        """有网关 ⇒ 建议值来自默认路由 ⇒ 输出里必须带告警。

        没有默认路由的机器上这条自动变成空断言（不依赖运行环境）。
        """
        res = pxe_server.detect_network()
        self.assertIsInstance(res, dict)
        if res.get("gateway"):
            warns = res.get("warnings") or []
            self.assertTrue(any("默认路由" in w for w in warns),
                            "detect_network 给出了网关却没提示默认路由风险：%r" % (warns,))


if __name__ == "__main__":
    unittest.main()
