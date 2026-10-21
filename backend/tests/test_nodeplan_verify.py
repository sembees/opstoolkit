# -*- coding: utf-8 -*-
"""M3 逐字段对账用例。

为什么这层必须存在：评审指出「ping 通不等于配置对」——IP 写错一位（.50 写成 .51）
仍然 ping 得通网关、DNS 也可达，但配置是错的。所以判定 = 期望与实际的字段级 diff。

这里钉住解析与判定两条线：
  · 解析 `ip -o` 的稳定格式（不是 -j，老 iproute2 未必支持）；
  · 地址/默认路由/DNS 三项：缺一项就 ok=False，并指出"期望什么、实际是什么"。
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.nodeplan.verify import diff_expected, parse_probe, verify_plan  # noqa: E402

PROBE_OK = """== ip_addr ==
2: eth0    inet 192.168.10.10/24 brd 192.168.10.255 scope global eth0\\       valid_lft forever
3: eth1    inet 10.20.30.40/24 brd 10.20.30.255 scope global eth1\\       valid_lft forever
1: lo    inet 127.0.0.1/8 scope host lo\\       valid_lft forever
== ip_route ==
default via 192.168.10.1 dev eth0 proto static
192.168.10.0/24 dev eth0 proto kernel scope link src 192.168.10.10
10.20.30.0/24 dev eth1 proto kernel scope link src 10.20.30.40
== resolv ==
nameserver 192.168.10.1
nameserver 8.8.8.8
== bonding ==
== bridge_vlan ==
"""


def test_parse_extracts_facts():
    p = parse_probe(PROBE_OK)
    assert {"iface": "eth0", "family": "inet", "cidr": "192.168.10.10/24"} in p["addresses"]
    assert {"dest": "default", "via": "192.168.10.1", "dev": "eth0"} in p["routes"]
    assert p["dns"] == ["192.168.10.1", "8.8.8.8"]


def test_all_match_is_ok():
    expected = {"addresses": ["192.168.10.10/24"], "default_gateway": "192.168.10.1",
                "dns": ["192.168.10.1", "8.8.8.8"]}
    v = verify_plan(expected, PROBE_OK)
    assert v["ok"] is True
    assert v["counts"]["failed"] == 0


def test_wrong_ip_is_detected_even_though_gateway_pings():
    """评审的经典场景：IP 少一位但网关/ DNS 都通 —— 必须判失败，并指出差在哪。"""
    expected = {"addresses": ["192.168.10.50/24"], "default_gateway": "192.168.10.1",
                "dns": ["192.168.10.1"]}
    v = verify_plan(expected, PROBE_OK)
    assert v["ok"] is False
    bad = [i for i in v["items"] if not i["ok"]]
    assert bad and bad[0]["item"] == "address"
    assert bad[0]["expected"] == "192.168.10.50/24"
    assert "192.168.10.10/24" in bad[0]["actual"]


def test_wrong_gateway_detected():
    expected = {"addresses": ["192.168.10.10/24"], "default_gateway": "192.168.99.1", "dns": []}
    v = verify_plan(expected, PROBE_OK)
    assert v["ok"] is False
    assert any(i["item"] == "default_route" and not i["ok"] for i in v["items"])


def test_missing_dns_detected():
    expected = {"addresses": ["192.168.10.10/24"], "default_gateway": "192.168.10.1",
                "dns": ["1.1.1.1"]}
    v = verify_plan(expected, PROBE_OK)
    assert v["ok"] is False
    assert any(i["item"] == "dns" and i["expected"] == "1.1.1.1" and not i["ok"] for i in v["items"])


def test_empty_probe_reports_everything_missing():
    """目标机 probe 是空的（采集失败/机器不对）→ 每一项都要判缺失，不能默认通过。"""
    expected = {"addresses": ["192.168.10.10/24"], "default_gateway": "192.168.10.1",
                "dns": ["192.168.10.1"]}
    v = verify_plan(expected, "")
    assert v["ok"] is False
    assert v["counts"]["failed"] == v["counts"]["total"] == 3


def test_no_expectations_is_vacuously_ok():
    """规划里没有可对账项时不要误判失败（例如只配了 bond 的机器）。"""
    assert diff_expected({"addresses": [], "default_gateway": "", "dns": []}, {})["ok"] is True


PROBE_RESOLVED = PROBE_OK.replace(
    "nameserver 192.168.10.1\nnameserver 8.8.8.8",
    "nameserver 127.0.0.53"
).replace(
    "== resolv ==",
    "== resolv ==",
).replace(
    "== bonding ==",
    "== resolvectl ==\nGlobal: \nLink 2 (eth0): 192.168.10.1 8.8.8.8\n== bonding ==",
)


def test_systemd_resolved_stub_is_not_mistaken_for_actual_dns():
    """真机踩过：Ubuntu 22.04 的 /etc/resolv.conf 是 127.0.0.53 桩。

    只看 resolv.conf 会把"DNS 其实配对了"误判成不一致；必须从 resolvectl 取真实上游，
    并且不能把 127.0.0.53 本身当成实际 DNS。
    """
    p = parse_probe(PROBE_RESOLVED)
    assert "127.0.0.53" not in p["dns"], p["dns"]
    assert "192.168.10.1" in p["dns"] and "8.8.8.8" in p["dns"]

    expected = {"addresses": ["192.168.10.10/24"], "default_gateway": "192.168.10.1",
                "dns": ["192.168.10.1", "8.8.8.8"]}
    v = verify_plan(expected, PROBE_RESOLVED)
    assert v["ok"] is True, v
