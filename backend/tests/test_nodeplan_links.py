# -*- coding: utf-8 -*-
"""MTU / VLAN / bond 模式对账用例。"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.nodeplan.links import diff_links, parse_links  # noqa: E402

PROBE = """== ip_link ==
2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 9000 qdisc mq state UP mode DEFAULT qlen 1000
3: bond0: <BROADCAST,MULTICAST,MASTER,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT qlen 1000
4: bond0.20@bond0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue state UP mode DEFAULT qlen 1000
    vlan protocol 802.1Q id 20 <REORDER_HDR>
== bonding ==
--- /proc/net/bonding/bond0
Bonding Mode: IEEE 802.3ad Dynamic link aggregation
== bridge_vlan ==
port    vlan-id
bond0   20 PVID Egress Untagged
"""


def test_parse_links():
    p = parse_links(PROBE)
    assert p["mtu"]["eth0"] == 9000 and p["mtu"]["bond0"] == 1500
    assert "802.3ad" in p["bond_mode"]["bond0"]
    assert 20 in p["vlan_ids"]


def test_mtu_mismatch_detected():
    d = diff_links({"mtu": [{"iface": "eth0", "mtu": 1500}]}, parse_links(PROBE))
    assert d["ok"] is False
    assert d["items"][0]["actual"] == "eth0 mtu 9000"


def test_mtu_match_ok():
    d = diff_links({"mtu": [{"iface": "eth0", "mtu": 9000}]}, parse_links(PROBE))
    assert d["ok"] is True


def test_bond_mode_partial_match_is_ok():
    """规划写 802.3ad，实际是 "IEEE 802.3ad Dynamic link aggregation" —— 应判一致。"""
    d = diff_links({"bond_mode": [{"iface": "bond0", "mode": "802.3ad"}]}, parse_links(PROBE))
    assert d["ok"] is True, d


def test_vlan_missing_detected():
    d = diff_links({"vlan_ids": [20, 30]}, parse_links(PROBE))
    assert d["ok"] is False
    assert any(i["item"] == "vlan" and i["expected"] == 30 and not i["ok"] for i in d["items"])


def test_nothing_expected_is_ok():
    assert diff_links({}, parse_links(PROBE))["ok"] is True


def test_verify_plan_merges_links():
    from app.it.nodeplan.verify import verify_plan
    expected = {"addresses": [], "dns": [], "mtu": [{"iface": "eth0", "mtu": 1500}],
                "vlan_ids": [30]}
    v = verify_plan(expected, PROBE)
    assert v["ok"] is False
    names = {i["item"] for i in v["items"]}
    assert "mtu" in names and "vlan" in names
    assert v["counts"]["failed"] == 2
