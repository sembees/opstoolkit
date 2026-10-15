# -*- coding: utf-8 -*-
"""「本站点禁止使用的网段」是**站点级配置**，不是产品规则（2026-10-15 用户澄清）。

用户原话：「在实际使用的过程中，它是可以选择任意一个已有的网段网卡的；当前环境不然，
是因为本质上我们这是一个试验环境，118 是我们企业内网，所以不让你使用 118 网段，
避免影响企业内网。」

⇒ 两件事必须分开：
  · **产品能力**：可以用本机**任意**一张已有网卡的网段做装机网（客户常常只有一张卡，
    ProxyDHCP 就必须在它上面做）。默认口径 = 允许，只给醒目告警。
  · **站点约束**：某些站点里有一张卡接的是企业/生产内网，绝不允许在那上面起 dnsmasq。
    由站点在 `.env` 里显式声明（`PXE_PROTECTED_NETWORKS`），或开启更严一档的
    `PXE_PROTECT_DEFAULT_ROUTE`。

本文件钉住这两套口径各自的行为，以及**它们之间的边界**（哪些判据与开关无关、永远 fail-closed）。
"""
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

import importlib  # noqa: E402

dhcp = importlib.import_module("app.core.dhcp")

# 真机形状：ens18 承载默认路由（企业网 10.128.118.113/24），ens19 是专用装机网。
HOST_FACTS = {"iface_name": "ens18",
              "iface_v4": ("10.128.118.113", 24),
              "iface_v4_map": {"ens18": ("10.128.118.113", 24),
                               "ens19": ("192.168.199.1", 24)}}
CORP_CONF = ("interface=ens18\nbind-interfaces\n"
             "dhcp-range=10.128.118.191,10.128.118.253,12h\n")
# 池**不落在**默认路由网段里（10.0.0.0/24）—— 用来把"网卡身份"与"池越界"两条判据分开测
CORP_IFACE_OTHER_POOL = ("interface=ens18\nbind-interfaces\n"
                         "dhcp-range=10.0.0.100,10.0.0.200,12h\n")
ISO_CONF = ("interface=ens19\nbind-interfaces\n"
            "dhcp-range=192.168.199.100,192.168.199.200,12h\n")


@contextmanager
def site(protected="", protect_default_route=False):
    """临时改站点级配置并在退出时**还原**。

    `settings` 是进程级单例，用例之间会互相污染（尤其 test_ztp_deploy 那批断言
    "企业网段被拒"的用例）—— 必须还原，否则测试顺序一变结论就飘。
    """
    from app.config import settings
    old = (getattr(settings, "pxe_protected_networks", ""),
           getattr(settings, "pxe_protect_default_route", False))
    settings.pxe_protected_networks = protected
    settings.pxe_protect_default_route = protect_default_route
    try:
        yield settings
    finally:
        settings.pxe_protected_networks, settings.pxe_protect_default_route = old


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = os.listdir
        self.real = {"lo": None, "ens18": None, "ens19": None}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_conf = dhcp.CONF_DIR
        dhcp.CONF_DIR = self._tmp.name
        self.addCleanup(lambda: setattr(dhcp, "CONF_DIR", self._orig_conf))

        def fake(path):
            return list(self.real) if path == "/sys/class/net" else self._orig(path)

        dhcp.os.listdir = fake
        self.addCleanup(lambda: setattr(dhcp.os, "listdir", self._orig))


class ProductDefaultTest(_Base):
    """产品默认口径：任何网段都能选（这是"能力"，不是"疏漏"）。

    注意一个**由两条 fail-closed 判据共同导出的结论**（不是特例，是用例逼出来的）：
    在承载默认路由的网卡上，"独立 DHCP"**永远过不了** —— 第 6 条要求池落在该网卡自己的
    网段内，第 4 条又拒绝池与该（默认路由）网段重叠。所以那张卡上实际只能用
    ProxyDHCP / 中继。扫描器必须这么说（caution + blocked_modes=["standalone"]），
    否则界面会推荐一个必然被拒的组合。
    """

    def test_proxy_and_relay_are_allowed_on_any_iface_by_default(self):
        with site("", False):
            proxy = ("interface=ens18\nbind-interfaces\n"
                     "dhcp-range=10.128.118.113,proxy\n")
            relay = "interface=ens18\nbind-interfaces\nenable-tftp\ntftp-root=/srv/tftp\n"
            for conf in (proxy, relay):
                ok, why = dhcp.check_dhcp_conf_safety(conf, HOST_FACTS, own_path="x.conf")
                assert ok, why

    def test_isolated_iface_standalone_passes_by_default(self):
        with site("", False):
            ok, why = dhcp.check_dhcp_conf_safety(ISO_CONF, HOST_FACTS, own_path="x.conf")
            assert ok, why

    def test_standalone_on_default_route_iface_can_never_pass(self):
        """★ 上联网卡上的独立 DHCP：池在本网段 ⇒ 被第 4 条拒；池在别的网段 ⇒ 被第 6 条拒。

        两条都 fail-closed，所以结论是"这张卡只能 proxy/中继" —— 与扫描器给出的
        `blocked_modes=["standalone"]` 一致。
        """
        with site("", False):
            ok1, why1 = dhcp.check_dhcp_conf_safety(CORP_CONF, HOST_FACTS, own_path="x.conf")
            ok2, why2 = dhcp.check_dhcp_conf_safety(CORP_IFACE_OTHER_POOL, HOST_FACTS,
                                                    own_path="x.conf")
            assert not ok1 and not ok2
            assert "重叠" in why1, why1                      # 池在本网段 → 第 4 条
            assert "不在" in why2 or "网段" in why2, why2      # 池在别的网段 → 第 6 条

    def test_pool_inside_default_route_subnet_is_still_refused(self):
        """★ 与开关**无关**的边界：池落在默认路由网段里 = 抢答上联地址，永远拒绝。

        这一条正是本项目那次真机事故的形状（差点在 10.128.118.0/24 上开池），
        它不该因为"允许选任意网卡"而放开 —— 允许的是"在那张卡上提供引导信息"，
        不是"把上联网段的地址分出去"。
        """
        with site("", False):
            ok, why = dhcp.check_dhcp_conf_safety(CORP_CONF, HOST_FACTS, own_path="x.conf")
            assert not ok, "池落在默认路由网段里竟然通过了"
            assert ("重叠" in why) or ("默认路由" in why), why


class SiteProtectedTest(_Base):
    """站点显式声明受保护网段：**任何模式**都不许碰（不受其它开关影响）。"""

    def test_declared_net_is_refused(self):
        with site("10.128.118.0/24", False):
            ok, why = dhcp.check_dhcp_conf_safety(CORP_IFACE_OTHER_POOL, HOST_FACTS,
                                                  own_path="x.conf")
            assert not ok
            assert "受保护网段" in why and "10.128.118.0/24" in why, why

    def test_other_nets_unaffected(self):
        with site("10.128.118.0/24", False):
            ok, why = dhcp.check_dhcp_conf_safety(ISO_CONF, HOST_FACTS, own_path="x.conf")
            assert ok, why

    def test_multiple_nets_and_separators(self):
        with site("10.9.9.0/24; 172.16.0.0/12 , 10.128.118.0/24", False):
            assert len(dhcp.site_protected_nets()) == 3
            ok, _why = dhcp.check_dhcp_conf_safety(CORP_IFACE_OTHER_POOL, HOST_FACTS,
                                                   own_path="x.conf")
            assert not ok

    def test_bad_entries_are_ignored_not_fatal(self):
        """笔误只忽略那一条（并写日志），既不静默关掉整车间的保护，也不把部署全堵死。"""
        with site("10.128.118.007/24, not-a-net, 10.128.118.0/24", False):
            nets = dhcp.site_protected_nets()
            assert [str(n) for n in nets] == ["10.128.118.0/24"], nets
            ok, _why = dhcp.check_dhcp_conf_safety(CORP_IFACE_OTHER_POOL, HOST_FACTS,
                                                   own_path="x.conf")
            assert not ok, "坏条目把好条目一起带没了"


class ProtectDefaultRouteFlagTest(_Base):
    """更严一档的站点开关 `PXE_PROTECT_DEFAULT_ROUTE`。"""

    def test_flag_on_refuses_default_route_iface(self):
        with site("", True):
            ok, why = dhcp.check_dhcp_conf_safety(CORP_IFACE_OTHER_POOL, HOST_FACTS,
                                                  own_path="x.conf")
            assert not ok
            assert "PXE_PROTECT_DEFAULT_ROUTE" in why, why

    def test_flag_on_is_fail_closed_when_route_unknown(self):
        """开了这个开关就 fail-closed：读不到默认路由（判断不了）时**拒绝**，不赌。"""
        with site("", True):
            ok, why = dhcp.check_dhcp_conf_safety(ISO_CONF, {"iface_name": "", "iface_v4": None},
                                                  own_path="x.conf")
            assert not ok, "开了保护却判断不了骨干网，竟然放行"
            assert "拒绝" in why

    def test_flag_off_does_not_need_route_facts(self):
        """不开这个开关就不依赖"默认路由是谁"：给得出网卡地址就能过。

        （仍要给网卡地址 —— 第 6 条"池必须落在网卡自己网段里"是**另一条**永远生效的判据。）
        """
        with site("", False):
            facts = {"iface_name": "", "iface_v4": None,
                     "iface_v4_map": {"ens19": ("192.168.199.1", 24)}}
            ok, why = dhcp.check_dhcp_conf_safety(ISO_CONF, facts, own_path="x.conf")
            assert ok, why


class ScannerFollowsSiteConfigTest(_Base):
    """扫描器与文档必须**同一口径**：产品默认给 caution（只能 proxy），站点声明才 blocked。"""

    def _rows(self):
        from app.it.pxe import server as pxe_server
        facts = dict(HOST_FACTS, ifaces=["lo", "ens18", "ens19"])
        return {x["interface"]: x for x in pxe_server.list_local_networks(facts)["interfaces"]}

    def test_product_default_marks_default_route_as_caution_for_proxy_only(self):
        with site("", False):
            got = self._rows()
            row = got["ens18"]
            assert row["safety"] == "caution", row
            assert row["blocked_modes"] == ["standalone"], row["blocked_modes"]
            assert row["suggest_start"] == "" and row["suggest_end"] == "", "这张卡给不出独立 DHCP 的池"
            assert "默认路由" in row["reason"] and "ProxyDHCP" in row["reason"]

    def test_site_declared_net_is_blocked_for_all_modes(self):
        with site("10.128.118.0/24", False):
            got = self._rows()
            assert got["ens18"]["safety"] == "blocked"
            assert got["ens18"]["blocked_modes"] == ["standalone", "proxy", "relay"]
            assert "受保护网段" in got["ens18"]["reason"]
            assert got["ens19"]["safety"] == "ok"

    def test_scanner_and_guard_agree_under_both_policies(self):
        """★ 最要紧的一条：扫描器的 safety/blocked_modes 与落盘前那道守卫必须**逐模式同结论**。

        界面敢不敢推荐某个组合，唯一依据就是这个。三种站点口径都要成立。
        """
        from app.it.pxe import server as pxe_server
        from app.it.pxe.generator import PxeConfig, generate_all
        facts = dict(HOST_FACTS, ifaces=["lo", "ens18", "ens19"])

        def conf_for(iface, ip, mode, start, end):
            c = PxeConfig(os_type="debian", os_version="13.7.0", hostname="h", admin_user="ops",
                          admin_password="x", ssh_keys=[], disk_scheme="lvm",
                          disk_config={"disk": "sda"}, net_mode="dhcp",
                          net_config={"server_interface": iface, "dhcp_start": start,
                                      "dhcp_end": end},
                          mirror="http://h/repo/", server_ip=ip,
                          http_root="http://%s:8000/pxe/serve" % ip,
                          kernel_path="k", initrd_path="i", deploy_mode=mode)
            return generate_all(c, [])["dnsmasq.conf"]

        cases = (("ens19", "192.168.199.1", "192.168.199.100", "192.168.199.200"),
                 ("ens18", "10.128.118.113", "10.128.118.191", "10.128.118.253"))
        for protected, protect_route in (("", False), ("10.128.118.0/24", False), ("", True)):
            with site(protected, protect_route):
                rows = {x["interface"]: x
                        for x in pxe_server.list_local_networks(facts)["interfaces"]}
                for iface, ip, start, end in cases:
                    for mode in ("standalone", "proxy", "relay"):
                        conf = conf_for(iface, ip, mode, start, end)
                        ok, _why = dhcp.check_dhcp_conf_safety(conf, HOST_FACTS, own_path="x.conf")
                        row = rows[iface]
                        allowed = (row["safety"] != "blocked") and (mode not in row["blocked_modes"])
                        assert ok == allowed, (
                            "站点(%r, %s) %s 模式=%s：扫描器 safety=%s blocked_modes=%s ⇒ allowed=%s，"
                            "守卫 ok=%s —— 必须一致"
                            % (protected, protect_route, iface, mode, row["safety"],
                               row["blocked_modes"], allowed, ok))
