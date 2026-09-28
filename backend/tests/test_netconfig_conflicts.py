# -*- coding: utf-8 -*-
"""H：netconfig 的静态 IP 与 PXE/ZTP 的 DHCP 池交叉检查。

真事故形状：静态配了池内地址 + DHCP 又把同一地址发给另一台装机中的机器
⇒ **两台机器同 IP**（时好时坏，非常难查）。两个模块各自看都没问题，只有交叉看才发现。
这里锁住：能报出来、只报真冲突、坏数据不误报、relay 模式没有池。
"""
import os
import sys
import types
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.api import netconfig as netconfig_api  # noqa: E402
from app.core.auth import get_current_user  # noqa: E402
from app.core.schemas import NetConfigRequest  # noqa: E402
from app.it.netconfig import conflicts  # noqa: E402


def _iface(name="eth0", ip=None, mode="static"):
    return types.SimpleNamespace(name=name, ip=ip, mode=mode)


def _req(**kw):
    """最小请求对象：interfaces/bonds/vlans/bridges + format/os。"""
    base = dict(interfaces=[], bonds=[], vlans=[], bridges=[],
                format="nmcli", os="ubuntu", hostname="h1")
    base.update(kw)
    return types.SimpleNamespace(**base)


class CollectStaticAddressesTest(unittest.TestCase):
    def test_only_static_with_ip(self):
        req = _req(interfaces=[_iface("eth0", "10.0.0.5"),
                               _iface("eth1", "10.0.0.6", mode="dhcp"),
                               _iface("eth2", None)],
                   bonds=[_iface("bond0", "10.0.0.7")],
                   vlans=[_iface("vlan10", "10.0.0.8")],
                   bridges=[_iface("br0", "10.0.0.9")])
        got = conflicts.collect_static_addresses(req)
        by_label = {label: ip for label, ip in got}
        self.assertEqual([ip for _l, ip in got],
                         ["10.0.0.5", "10.0.0.7", "10.0.0.8", "10.0.0.9"])
        self.assertEqual(by_label["bond bond0"], "10.0.0.7")
        self.assertEqual(by_label["vlan vlan10"], "10.0.0.8")
        self.assertEqual(by_label["bridge br0"], "10.0.0.9")

    def test_real_schema_object_works(self):
        # 用真实 pydantic 模型也走得通（不是只有鸭子类型能过）
        req = NetConfigRequest(format="nmcli", os="ubuntu", hostname="h1",
                               interfaces=[{"name": "eth0", "mode": "static",
                                            "ip": "10.0.0.5", "prefix": 24}])
        self.assertEqual(conflicts.collect_static_addresses(req),
                         [("接口 eth0", "10.0.0.5")])


class PoolConflictsTest(unittest.TestCase):
    POOL = [{"source": "PXE 模板 p1", "start": "192.168.199.100",
             "end": "192.168.199.200"}]

    def test_inside_is_reported(self):
        w = conflicts.pool_conflicts([("接口 eth0", "192.168.199.150")], self.POOL)
        self.assertEqual(len(w), 1)
        self.assertIn("192.168.199.150", w[0])
        self.assertIn("PXE 模板 p1", w[0])
        self.assertIn("同 IP", w[0])

    def test_boundaries_are_reported(self):
        for ip in ("192.168.199.100", "192.168.199.200"):
            self.assertEqual(len(conflicts.pool_conflicts([("接口 eth0", ip)], self.POOL)), 1)

    def test_outside_is_silent(self):
        for ip in ("192.168.199.99", "192.168.199.201", "10.0.0.5"):
            self.assertEqual(conflicts.pool_conflicts([("接口 eth0", ip)], self.POOL), [])

    def test_bad_pool_is_skipped_not_guessed(self):
        pools = [{"source": "坏池", "start": "not-an-ip", "end": "192.168.199.200"},
                 {"source": "缺一半", "start": "", "end": "192.168.199.200"}]
        self.assertEqual(conflicts.pool_conflicts([("接口 eth0", "192.168.199.150")], pools), [])

    def test_reversed_pool_is_normalized(self):
        pools = [{"source": "写反了", "start": "192.168.199.200",
                  "end": "192.168.199.100"}]
        self.assertEqual(len(conflicts.pool_conflicts([("接口 eth0", "192.168.199.150")], pools)), 1)

    def test_multiple_pools_report_each_hit(self):
        pools = self.POOL + [{"source": "ZTP 模板 z1",
                              "start": "192.168.199.150", "end": "192.168.199.160"}]
        w = conflicts.pool_conflicts([("接口 eth0", "192.168.199.150")], pools)
        self.assertEqual(len(w), 2)
        self.assertTrue(any("ZTP 模板 z1" in x for x in w))

    def test_same_range_reported_once_with_source_count(self):
        """同一段池被多个模板共用时只报一次（真机上 20+ 个模板共用一段池）。"""
        pools = [{"source": "模板%d" % i, "start": "192.168.199.100",
                  "end": "192.168.199.200"} for i in range(25)]
        w = conflicts.pool_conflicts([("接口 eth0", "192.168.199.150")], pools)
        self.assertEqual(len(w), 1, w)
        self.assertIn("等 25 个模板", w[0])

    def test_warning_count_is_capped(self):
        pools = [{"source": "池%d" % i, "start": "10.0.%d.1" % i, "end": "10.0.%d.9" % i}
                 for i in range(30)]
        addrs = [("接口 eth0", "10.0.%d.5" % i) for i in range(30)]
        w = conflicts.pool_conflicts(addrs, pools, max_warnings=5)
        self.assertEqual(len(w), 5)


class PoolsFromModelsTest(unittest.TestCase):
    def test_pxe_pool_read_from_net_config(self):
        p = types.SimpleNamespace(name="p1", net_config={
            "deploy_mode": "standalone",
            "dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200"})
        got = conflicts.pools_from_pxe_profiles([p])
        self.assertEqual(got[0]["start"], "192.168.199.100")
        self.assertIn("p1", got[0]["source"])

    def test_pxe_relay_and_incomplete_are_skipped(self):
        relay = types.SimpleNamespace(name="p2", net_config={
            "deploy_mode": "relay", "dhcp_start": "10.0.0.1", "dhcp_end": "10.0.0.9"})
        half = types.SimpleNamespace(name="p3", net_config={"dhcp_start": "10.0.0.1"})
        self.assertEqual(conflicts.pools_from_pxe_profiles([relay, half]), [])

    def test_ztp_pool_and_relay(self):
        t1 = types.SimpleNamespace(name="z1", deploy_mode="standalone",
                                   dhcp_start="10.9.9.100", dhcp_end="10.9.9.200")
        t2 = types.SimpleNamespace(name="z2", deploy_mode="relay",
                                   dhcp_start="10.9.9.100", dhcp_end="10.9.9.200")
        got = conflicts.pools_from_ztp_templates([t1, t2])
        self.assertEqual(len(got), 1)
        self.assertIn("z1", got[0]["source"])


class _FakeScalars:
    def __init__(self, items):
        self._items = items

    def scalars(self):
        return self

    def all(self):
        return self._items


class _FakeDb:
    """按 select 的实体名返回不同的集合（PxeProfile / ZtpTemplate）。"""

    def __init__(self, pxes, ztps):
        self.pxes, self.ztps = pxes, ztps

    async def execute(self, stmt):
        ent = stmt.column_descriptions[0].get("entity")
        name = getattr(ent, "__name__", "")
        return _FakeScalars(self.pxes if name == "PxeProfile" else self.ztps)


def _client(db):
    app = FastAPI()
    app.include_router(netconfig_api.router, prefix="/api/it/netconfig")
    app.dependency_overrides[get_current_user] = lambda: {"id": "t", "username": "t"}
    app.dependency_overrides[netconfig_api.get_db] = lambda: db
    return TestClient(app)


BODY = {
    "format": "nmcli", "os": "ubuntu", "hostname": "h1",
    "interfaces": [{"name": "eth0", "mode": "static", "ip": "192.168.199.150",
                    "prefix": 24, "gateway": "192.168.199.1"}],
}


class NetConfigDhcpConflictApiTest(unittest.TestCase):
    def test_warning_when_ip_inside_pxe_pool(self):
        pxe = types.SimpleNamespace(name="p1", net_config={
            "deploy_mode": "standalone",
            "dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200"})
        r = _client(_FakeDb([pxe], [])).post("/api/it/netconfig/generate", json=BODY)
        self.assertEqual(r.status_code, 200)
        js = r.json()
        self.assertTrue(js["warnings"], js)
        self.assertIn("DHCP 地址池", js["warnings"][0])

    def test_no_warning_when_ip_outside_pool(self):
        pxe = types.SimpleNamespace(name="p1", net_config={
            "deploy_mode": "standalone",
            "dhcp_start": "192.168.199.1", "dhcp_end": "192.168.199.50"})
        r = _client(_FakeDb([pxe], [])).post("/api/it/netconfig/generate", json=BODY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["warnings"], [])

    def test_warning_when_ip_inside_ztp_pool(self):
        ztp = types.SimpleNamespace(name="z1", deploy_mode="standalone",
                                    dhcp_start="192.168.199.140", dhcp_end="192.168.199.160")
        r = _client(_FakeDb([], [ztp])).post("/api/it/netconfig/generate", json=BODY)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(any("ZTP 模板 z1" in w for w in r.json()["warnings"]),
                        r.json()["warnings"])

    def test_generate_still_works_when_db_is_unavailable(self):
        """提示性检查绝不能把生成接口带崩（DB 不可用时 warnings 为空、结果照旧）。"""
        class Boom:
            async def execute(self, stmt):
                raise RuntimeError("db down")

        r = _client(Boom()).post("/api/it/netconfig/generate", json=BODY)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["warnings"], [])
        self.assertIn("script", r.json())
