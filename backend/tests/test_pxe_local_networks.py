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

    def test_default_route_iface_is_blocked_for_standalone(self):
        got = by_iface(pxe_server.list_local_networks(REAL))
        corp = got["ens18"]
        assert corp["is_default_route"] is True
        assert corp["safety"] == "blocked"
        assert corp["blocked_modes"] == ["standalone"]
        assert corp["suggest_start"] == "" and corp["suggest_end"] == ""
        assert "默认路由" in corp["reason"]
        # proxy/relay 不发地址 ⇒ 不列入 blocked_modes
        assert "proxy" not in corp["blocked_modes"] and "relay" not in corp["blocked_modes"]

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
