# -*- coding: utf-8 -*-
"""本机网段扫描（`/api/it/pxe/local-networks`）的用例。

用户 2026-10-15 提的用法：应用装在配好 IP 的机器上，应能**自己扫本机网段**、
让人选哪个用于 DHCP、再选自己是 DHCP 还是 proxy。此前只有 `detect_network()`，
它只看默认路由那张卡（恰好是最危险的一张），界面上没有"从本机网段里挑一个"的入口。

本文件钉：
  1. 只列有 IPv4 的非回环/非虚拟网卡；
  2. **承载默认路由的网卡标 blocked**（standalone 会在它上面发地址），proxy/relay 仍可用；
  3. 建议池落在该网段内、避开服务器自身 IP 与网关、避开网络/广播地址；
  4. 建议池必须真能过部署侧的 range_inside_binding（与守卫同一套判据）；
  5. 只读：调用它不写任何文件、不改任何状态。
"""
import ipaddress
import sys
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe import server as pxe_server  # noqa: E402

# 真机形状：ens18 = 企业网（承载默认路由，10.128.118.113/24），
#           ens19 = 隔离装机网（192.168.199.1/24），另有 docker0 之类虚拟口。
REAL = {
    "iface_name": "ens18",
    "gateway": "10.128.118.1",
    "ifaces": ["lo", "ens18", "ens19", "docker0", "veth1234", "br-abc"],
    "iface_v4_map": {
        "ens18": ("10.128.118.113", 24),
        "ens19": ("192.168.199.1", 24),
        "docker0": ("172.17.0.1", 16),
    },
}


def by_iface(res):
    return {x["interface"]: x for x in res["interfaces"]}


class TestListLocalNetworks:
    def test_lists_only_real_ipv4_ifaces(self):
        got = by_iface(pxe_server.list_local_networks(REAL))
        # lo 无 IPv4、veth/br- 是虚拟口 ⇒ 都不列；docker0 有 IPv4 但前缀 16 —— 列不列？
        # 当前实现只按"有 IPv4 且非回环/非虚拟名"筛，docker 前缀已被名字过滤掉。
        assert "ens18" in got and "ens19" in got
        assert "lo" not in got and "veth1234" not in got and "br-abc" not in got
        assert "docker0" not in got

    def test_default_route_iface_is_blocked_for_all_modes(self):
        """★ 2026-10-15 更正：承载默认路由的网卡上**三种模式都不可用**。

        我先前写成"只有 standalone 不可用、proxy/relay 可以用"，理由抄的是
        `_deploy_redline_check` 的注释（"proxy/relay 不发地址"）。但**真正生效的那一层**
        是 `deploy_files` 里的 `dhcp.check_dhcp_conf_safety`，它的第 3 条只看
        `interface=` 是不是承载默认路由的那张卡、**不看模式**。扫描器必须按实际会生效的
        守卫口径说话，否则界面会推荐一个必然被拒的选项。
        """
        got = by_iface(pxe_server.list_local_networks(REAL))
        corp = got["ens18"]
        assert corp["is_default_route"] is True
        assert corp["safety"] == "blocked"
        assert corp["blocked_modes"] == ["standalone", "proxy", "relay"], corp["blocked_modes"]
        assert corp["suggest_start"] == "" and corp["suggest_end"] == ""
        assert "默认路由" in corp["reason"]
        assert "interface=" in corp["reason"] or "红线" in corp["reason"], corp["reason"]

    def test_isolated_iface_is_ok_with_pool(self):
        got = by_iface(pxe_server.list_local_networks(REAL))
        iso = got["ens19"]
        assert iso["is_default_route"] is False
        assert iso["safety"] == "ok" and iso["blocked_modes"] == []
        assert iso["network"] == "192.168.199.0/24"
        assert iso["netmask"] == "255.255.255.0" and iso["prefixlen"] == 24
        assert iso["gateway"] == "192.168.199.1"

    def test_suggested_pool_is_inside_subnet_and_avoids_self_and_gateway(self):
        got = by_iface(pxe_server.list_local_networks(REAL))
        iso = got["ens19"]
        start, end = iso["suggest_start"], iso["suggest_end"]
        net = ipaddress.ip_network(iso["network"])
        assert ipaddress.ip_address(start) in net
        assert ipaddress.ip_address(end) in net
        assert int(ipaddress.ip_address(start)) <= int(ipaddress.ip_address(end))
        assert start != iso["ip"] and end != iso["ip"]
        assert start != iso["gateway"] and end != iso["gateway"]
        assert str(net.network_address) != start and str(net.broadcast_address) != end

    def test_suggested_pool_passes_the_deploy_time_guard(self):
        """建议池必须真能过 range_inside_binding（与部署守卫同一套判据，不新造规则）。"""
        got = by_iface(pxe_server.list_local_networks(REAL))
        iso = got["ens19"]
        bind = {"interface": iso["interface"], "ip": iso["ip"],
                "netmask": iso["netmask"], "prefixlen": iso["prefixlen"]}
        assert pxe_server.range_inside_binding(iso["suggest_start"], iso["suggest_end"], bind)

    def test_sorting_puts_safe_first(self):
        res = pxe_server.list_local_networks(REAL)
        flags = [x["is_default_route"] for x in res["interfaces"]]
        assert flags == sorted(flags), "建议用的（非默认路由）应排在前面"

    def test_no_usable_iface_reports_warning(self):
        res = pxe_server.list_local_networks({"ifaces": ["lo"], "iface_v4_map": {}})
        assert res["interfaces"] == []
        assert res["warnings"], "一张网卡都列不出来时必须给警告，不能静默空列表"

    def test_tiny_subnet_gets_no_pool_but_is_still_listed(self):
        """/30 只有 2 个可用地址 ⇒ 不猜池（caution），但仍如实列出让人自己看。"""
        res = pxe_server.list_local_networks({
            "iface_name": "ens18", "gateway": "10.0.0.1",
            "ifaces": ["ens20"], "iface_v4_map": {"ens20": ("10.0.0.1", 30)}})
        got = by_iface(res)
        assert got["ens20"]["safety"] == "caution"
        assert got["ens20"]["suggest_start"] == ""

    def test_point_to_point_subnet_skipped(self):
        """/31、/32 这类点对点/单地址网段不适合装机网 ⇒ 跳过并说明。"""
        res = pxe_server.list_local_networks({
            "iface_name": "", "ifaces": ["ens21"], "iface_v4_map": {"ens21": ("10.0.0.1", 32)}})
        assert res["interfaces"] == []
        assert any("/31" in w or "单地址" in w for w in res["warnings"])


class TestSuggestPoolHelper:
    def test_mid_range_and_skips(self):
        start, end = pxe_server.suggest_pool("192.168.199.0/24",
                                             skip=["192.168.199.1", "192.168.199.2"])
        s, e = ipaddress.ip_address(start), ipaddress.ip_address(end)
        net = ipaddress.ip_network("192.168.199.0/24")
        assert s in net and e in net
        # 跳过服务器自身与网关（.1/.2），且不碰网络地址/广播地址
        assert int(s) > int(ipaddress.ip_address("192.168.199.2"))
        assert str(e) != str(net.broadcast_address)
        # 池落在网段后半段，且跨度不超过 100 个地址
        assert int(s) >= int(ipaddress.ip_address("192.168.199.100"))
        assert 0 < int(e) - int(s) + 1 <= 100

    def test_bad_input_returns_empty(self):
        for bad in ("", "not-a-net", "192.168.1.0/33", "fe80::/64"):
            assert pxe_server.suggest_pool(bad) == ("", "")


class TestEndpointIsReadOnly:
    def test_endpoint_registered_and_requires_auth(self):
        """端点在 /it/pxe 下且要登录态（这是只读接口，但不能匿名扫本机网络）。"""
        from app.api import pxe as pxe_api
        paths = {r.path for r in pxe_api.router.routes}
        assert "/local-networks" in paths or any(p.endswith("/local-networks") for p in paths)
        route = next(r for r in pxe_api.router.routes
                     if getattr(r, "path", "").endswith("/local-networks"))
        deps = [d.call.__name__ for d in route.dependant.dependencies if getattr(d, "call", None)]
        assert "get_current_user" in deps, "本机网段扫描必须要求登录态"


class TestIPv6Coverage:
    """IPv6 覆盖（用户 2026-10-15 要求「这个需要覆盖测试」）。

    PXE/DHCP 这条链是 **IPv4**：dnsmasq 的 `dhcp-range=<v4>,<v4>`、`dhcp-host=<mac>,<v4>`、
    以及扫描器给出的建议池，全部只认 IPv4。IPv6 在这里不是"未实现"，而是**必须被安全地
    排除** —— 不能出现"把 IPv6 地址当成 IPv4 塞进配置"或"因为网卡只有 IPv6 而崩掉"。
    本类逐条钉住这些边界。
    """

    def test_ipv6_only_iface_is_skipped_not_crashing(self):
        """只有 IPv6 的网卡（_iface_v4 返回 None）必须被跳过，且不影响其它网卡。"""
        res = pxe_server.list_local_networks({
            "iface_name": "ens18", "gateway": "10.128.118.1",
            "ifaces": ["ens18", "ens19", "ens20"],
            "iface_v4_map": {"ens18": ("10.128.118.113", 24),
                             "ens19": ("192.168.199.1", 24),
                             "ens20": None},          # 只有 IPv6 / 无 IPv4
        })
        got = by_iface(res)
        assert "ens20" not in got, "没有 IPv4 的网卡不该出现在装机网候选里"
        assert set(got) == {"ens18", "ens19"}

    def test_all_ipv6_host_yields_empty_with_warning(self):
        res = pxe_server.list_local_networks({
            "iface_name": "", "ifaces": ["ens18", "ens19"],
            "iface_v4_map": {"ens18": None, "ens19": None}})
        assert res["interfaces"] == []
        assert res["warnings"], "整机没有 IPv4 时必须给警告，不能静默空列表"

    def test_ipv6_like_strings_never_leak_into_output(self):
        """输出里任何地址字段都不得是 IPv6 字面量（生成器只吃 IPv4）。"""
        import ipaddress
        res = pxe_server.list_local_networks({
            "iface_name": "ens18", "gateway": "10.128.118.1",
            "ifaces": ["ens18", "ens19"],
            "iface_v4_map": {"ens18": ("10.128.118.113", 24), "ens19": ("192.168.199.1", 24)}})
        for x in res["interfaces"]:
            for key in ("ip", "netmask", "network", "gateway"):
                assert x[key], (key, x)
                ipaddress.IPv4Address(x[key] if key != "network" else x["ip"])
                assert ":" not in x[key], "%s 里出现了 IPv6 字面量: %r" % (key, x[key])
            for key in ("suggest_start", "suggest_end"):
                if x[key]:
                    ipaddress.IPv4Address(x[key])

    def test_suggest_pool_rejects_ipv6_networks(self):
        assert pxe_server.suggest_pool("fe80::/64") == ("", "")
        assert pxe_server.suggest_pool("2001:db8::/32") == ("", "")
        assert pxe_server.suggest_pool("::1/128") == ("", "")

    def test_range_guard_is_fail_closed_for_ipv6(self):
        """红线守卫对 IPv6 必须 fail-closed（False），不能因为"算不出来"就放行。"""
        bind = {"interface": "ens19", "ip": "192.168.199.1",
                "netmask": "255.255.255.0", "prefixlen": 24}
        for a, b in (("fe80::1", "fe80::ff"), ("192.168.199.10", "fe80::1"),
                     ("fe80::1", "192.168.199.200")):
            assert pxe_server.range_inside_binding(a, b, bind) is False, (a, b)

    def test_schema_rejects_ipv6_in_server_side_fields(self):
        """模板保存期就拒绝：这些键最终会拼进 IPv4 的 dnsmasq 指令。"""
        import pytest
        from pydantic import ValidationError

        from app.core.schemas import PxeNetConfigIn
        for key in ("dhcp_start", "dhcp_end", "gateway", "dns_server"):
            with pytest.raises(ValidationError):
                PxeNetConfigIn.model_validate({key: "fe80::1"})
            with pytest.raises(ValidationError):
                PxeNetConfigIn.model_validate({key: "2001:db8::5"})
        # IPv4 正常通过（对照组，防止把校验写死成"一律拒绝"）
        ok = PxeNetConfigIn.model_validate({"dhcp_start": "10.99.0.50", "dhcp_end": "10.99.0.150"})
        assert ok.model_dump()["dhcp_start"] == "10.99.0.50"

    def test_generator_keeps_ipv4_pool_when_server_ip_is_ipv6(self):
        """server_ip 给 IPv6 ⇒ 池/网关退回 IPv4 默认值，绝不把 v6 拼进 dhcp-range。"""
        from app.it.pxe.generator import PxeConfig, generate_all
        c = PxeConfig(os_type="debian", os_version="13.7.0", hostname="h", admin_user="ops",
                      admin_password="x", ssh_keys=[], disk_scheme="lvm",
                      disk_config={"disk": "sda"}, net_mode="dhcp", net_config={},
                      mirror="http://h/repo/", server_ip="fe80::1",
                      http_root="http://192.168.199.1:8000/pxe/serve",
                      kernel_path="k", initrd_path="i", deploy_mode="standalone")
        conf = generate_all(c, [])["dnsmasq.conf"]
        rng = [l for l in conf.splitlines() if l.startswith("dhcp-range=")][0]
        assert rng == "dhcp-range=192.168.1.100,192.168.1.200,12h"
        assert ":" not in rng, "dhcp-range 里出现 IPv6 即非法"
        for l in conf.splitlines():
            if l.startswith("dhcp-option="):
                assert ":" not in l.split(",", 1)[1], l


class TestSegmentModeCouplingBackedByGuard(unittest.TestCase):
    """界面把「选网段」和「选模式」绑在一起，所依据的**后端事实**必须成立。

    用户 2026-10-15 描述的流程：基础网络配好 → 装平台 → 平台扫出本机网段 →
    选定用哪个网段做「DHCP 广播」还是「proxy」。界面对承载默认路由的网段会说
    「独立 DHCP 会被守卫拒绝」并自动改成 proxy —— 这三条承诺必须真能兑现，
    否则界面就是在骗人。这里用**真实生成出来的 dnsmasq 配置** + 真机形状的宿主事实
    去过落盘前的红线检查（check_dhcp_conf_safety），而不是只测一个布尔开关。
    """

    HOST_FACTS = {"iface_name": "ens18",           # 承载默认路由（企业网）
                  "iface_v4": ("10.128.118.113", 24),
                  "iface_v4_map": {"ens18": ("10.128.118.113", 24),
                                   "ens19": ("192.168.199.1", 24)}}
    # 扫描器还需要"本机有哪些网卡"这份清单（真机上来自 socket.if_nameindex()，
    # 单测里必须注入，否则会去读这台 Windows 机器的真实网卡）
    SCAN_FACTS = dict(HOST_FACTS, ifaces=["lo", "ens18", "ens19"])

    def setUp(self):
        import os
        import tempfile

        from app.core import dhcp
        self.dhcp = dhcp
        self._orig = os.listdir
        real = {"lo": None, "ens18": None, "ens19": None}
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_conf = dhcp.CONF_DIR
        dhcp.CONF_DIR = self._tmp.name
        self.addCleanup(lambda: setattr(dhcp, "CONF_DIR", self._orig_conf))

        def fake(path):
            return list(real) if path == "/sys/class/net" else self._orig(path)

        dhcp.os.listdir = fake
        self.addCleanup(lambda: setattr(dhcp.os, "listdir", self._orig))

    def _conf(self, mode, iface, start, end, server_ip):
        from app.it.pxe.generator import PxeConfig, generate_all
        c = PxeConfig(os_type="debian", os_version="13.7.0", hostname="h", admin_user="ops",
                      admin_password="x", ssh_keys=[], disk_scheme="lvm",
                      disk_config={"disk": "sda"}, net_mode="dhcp",
                      net_config={"server_interface": iface, "dhcp_start": start,
                                  "dhcp_end": end},
                      mirror="http://h/repo/", server_ip=server_ip,
                      http_root="http://%s:8000/pxe/serve" % server_ip,
                      kernel_path="k", initrd_path="i", deploy_mode=mode)
        return generate_all(c, [])["dnsmasq.conf"]

    def test_isolated_segment_standalone_passes(self):
        conf = self._conf("standalone", "ens19", "192.168.199.100", "192.168.199.200",
                          "192.168.199.1")
        ok, why = self.dhcp.check_dhcp_conf_safety(conf, self.HOST_FACTS, own_path="x.conf")
        assert ok, why

    def test_corporate_segment_standalone_is_refused(self):
        """★ 界面承诺「承载默认路由的网段做独立 DHCP 会被守卫拒绝」—— 必须为真。"""
        conf = self._conf("standalone", "ens18", "10.128.118.191", "10.128.118.253",
                          "10.128.118.113")
        ok, why = self.dhcp.check_dhcp_conf_safety(conf, self.HOST_FACTS, own_path="x.conf")
        assert not ok, "企业网段上开独立 DHCP 竟然通过了红线检查"
        assert ("默认路由" in why) or ("骨干" in why), why

    def test_corporate_segment_proxy_is_also_refused(self):
        """★ 更正：界面原先承诺「改用 proxy 就可以」—— **是错的**。

        `check_dhcp_conf_safety` 第 3 条不看模式，proxy 的配置同样带
        `interface=ens18`（承载默认路由），落盘前一样被拒。扫描器把它标 blocked
        正是这个原因；界面已改成禁用该行、不再推荐 proxy。
        """
        conf = self._conf("proxy", "ens18", "", "", "10.128.118.113")
        assert "dhcp-range=10.128.118.113,proxy" in conf     # 配置本身长得对
        ok, why = self.dhcp.check_dhcp_conf_safety(conf, self.HOST_FACTS, own_path="x.conf")
        assert not ok, "承载默认路由的网卡上 proxy 竟然通过了红线检查"
        assert ("默认路由" in why) or ("骨干" in why), why

    def test_relay_is_refused_too(self):
        """中继同理：relay 的配置也带 interface=（它要提供 TFTP/HTTP 文件）。"""
        conf = self._conf("relay", "ens18", "", "", "10.128.118.113")
        assert "interface=ens18" in conf
        ok, _why = self.dhcp.check_dhcp_conf_safety(conf, self.HOST_FACTS, own_path="x.conf")
        assert not ok

    def test_isolated_segment_proxy_passes(self):
        """隔离网卡上 proxy 是正常可用的（对照组：别把"一律拒绝"当成修好了）。"""
        conf = self._conf("proxy", "ens19", "", "", "192.168.199.1")
        ok, why = self.dhcp.check_dhcp_conf_safety(conf, self.HOST_FACTS, own_path="x.conf")
        assert ok, why

    def test_scanner_is_consistent_with_the_guard_for_every_mode(self):
        """扫描器的 safety 判定与**落盘前那道**红线检查必须**逐模式同结论**。

        这是界面敢不敢推荐某个组合的唯一依据：扫出来说能用、部署却被拒 = 骗人。
        """
        for iface, ip, start, end in (
                ("ens19", "192.168.199.1", "192.168.199.128", "192.168.199.227"),
                ("ens18", "10.128.118.113", "10.128.118.191", "10.128.118.253")):
            row = [x for x in pxe_server.list_local_networks(self.SCAN_FACTS)["interfaces"]
                   if x["interface"] == iface][0]
            for mode in ("standalone", "proxy", "relay"):
                conf = self._conf(mode, iface, start, end, ip)
                ok, _why = self.dhcp.check_dhcp_conf_safety(conf, self.HOST_FACTS, own_path="x.conf")
                usable = (row["safety"] == "ok")
                assert ok == usable, (
                    "%s 模式=%s：扫描器 safety=%s（blocked_modes=%s），守卫 ok=%s —— 两者必须一致"
                    % (iface, mode, row["safety"], row["blocked_modes"], ok))
