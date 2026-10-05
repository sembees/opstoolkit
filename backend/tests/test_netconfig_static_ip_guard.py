# -*- coding: utf-8 -*-
"""静态 IP 守卫（schemas._check_l3，NC3 跨字段校验）的专门回归。

守卫原文（app/core/schemas.py::_check_l3，由 NetConfigRequest._check_cross_rules 对
interfaces/bonds/vlans/bridges 逐个调用）：

  · mode=static 且 ip 为空 ⇒ 拒绝 ——
    "…ip 在 mode=static 时不能为空：空地址的静态接口既不是 DHCP 也没有地址，
    请填 ip，或把 mode 改成 dhcp"
    （改前静态意图静默退化成 DHCP：netplan dhcp4: true / nmcli ipv4.method auto /
    ifcfg BOOTPROTO=dhcp，运维要的静态地址无声消失）；
  · mode=dhcp 但 ip/gateway/netmask/cidr 任一残留 ⇒ 拒绝 ——
    "…{fname} 与 mode=dhcp 冲突（会被静默丢弃）：…"
    （改前这些残留字段被静默丢弃，运维以为配了静态参数）。

必须按**代码事实**覆盖的调用面（NetConfigRequest._check_cross_rules）：
  · interfaces / vlans / bridges 有 mode(dhcp/static) 字段 ⇒ 两个方向的守卫对这三类
    对象都生效；interfaces 还有一条豁免：被 bond/bridge 收编的从接口不配地址
    （require_ip_when_static=False），"static + 空 ip" 对它是**合法**形状；
  · bonds 的 mode 是聚合模式（int 0..6），不是 dhcp/static 开关；schemas 对 bonds 调
    _check_l3 时**不传 mode** ⇒ "static+空 ip" 这条守卫对 bond 结构性不可达，
    既有行为 = 校验放行、生成器按 DHCP 处理（nmcli ipv4.method auto /
    ifcfg BOOTPROTO=dhcp；netplan 不写地址块）。所以 bond 方向在本文件里按
    "放行 + 回落 DHCP" 写，而不是"报错"——若日后把规则改成拒绝 bond 静态空地址，
    属于规则变更，请同步改本文件（与任务卡的预期不符这一点已写进测试报告）。

为什么断言报错文案：只断言"抛异常"的话，守卫被换成语义不同的另一条也会让测试通过；
断言字段路径 + 守卫独有措辞，才锁得住守卫本身（detail 会原样显示给运维，文案即契约）。
"""
import pathlib
import sys
import unittest

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.core.schemas import NetConfigRequest  # noqa: E402
from app.it.netconfig.generator import generate_netconfig  # noqa: E402

# 守卫文案的关键片段（与 schemas.py 保持一致；文案本身也是契约，schema 侧改动请同步这里）
MSG_STATIC_EMPTY = "在 mode=static 时不能为空"
MSG_STATIC_WHY = "既不是 DHCP 也没有地址"
MSG_DHCP_CLASH = "与 mode=dhcp 冲突"
MSG_DHCP_WHY = "会被静默丢弃"


def _req(**kw):
    """最小请求骨架：rhel + nmcli，按需覆盖。"""
    base = dict(os="rhel", format="nmcli")
    base.update(kw)
    return NetConfigRequest(**base)


class StaticEmptyIpGuardTest(unittest.TestCase):
    """方向一：mode=static 且 ip 为空 ⇒ 必须拒绝，且报错带字段路径与守卫文案。"""

    def test_interface_static_empty_ip_rejected(self):
        """物理口：None 与 "" 都算"没填"（前端表单空值就是这两种形状）。"""
        for ip in (None, ""):
            with self.subTest(ip=repr(ip)):
                with pytest.raises(ValueError) as e:
                    _req(interfaces=[{"name": "eth0", "mode": "static", "ip": ip}])
                msg = str(e.value)
                self.assertIn("interfaces[0].ip " + MSG_STATIC_EMPTY, msg)
                self.assertIn(MSG_STATIC_WHY, msg)
                self.assertIn("请填 ip", msg)
                self.assertIn("或把 mode 改成 dhcp", msg)

    def test_interface_static_empty_ip_rejected_regardless_of_case(self):
        """mode 先经 _require_mode 归一（小写化）再进守卫："STATIC" 也是 static。"""
        with pytest.raises(ValueError) as e:
            _req(interfaces=[{"name": "eth0", "mode": "STATIC", "ip": None}])
        self.assertIn("interfaces[0].ip " + MSG_STATIC_EMPTY, str(e.value))

    def test_vlan_static_empty_ip_rejected(self):
        """VLAN：显式 mode="static" 与省略（模型缺省即 static）都必须被拦。"""
        for note, kw in (("显式 static", {"mode": "static"}), ("mode 省略", {})):
            with self.subTest(case=note):
                with pytest.raises(ValueError) as e:
                    _req(os="ubuntu", format="netplan",
                         vlans=[{"parent": "eth0", "vlan_id": 100, "ip": None, **kw}])
                msg = str(e.value)
                self.assertIn("vlans[0].ip " + MSG_STATIC_EMPTY, msg)
                self.assertIn(MSG_STATIC_WHY, msg)

    def test_bridge_static_empty_ip_rejected(self):
        """网桥：只有显式 mode="static" 才触发守卫（省略 mode = L2 自动态，不许误杀）。"""
        for ip in (None, ""):
            with self.subTest(ip=repr(ip)):
                with pytest.raises(ValueError) as e:
                    _req(os="ubuntu", format="netplan",
                         bridges=[{"name": "br0", "interfaces": ["eth1"],
                                   "mode": "static", "ip": ip}])
                msg = str(e.value)
                self.assertIn("bridges[0].ip " + MSG_STATIC_EMPTY, msg)
                self.assertIn(MSG_STATIC_WHY, msg)

    def test_bond_static_empty_ip_is_accepted_per_code_facts(self):
        """bond 的"static + 空 ip"**不报错** —— 按代码事实写（不是拍脑袋放行）。

        bond.mode 是聚合模式（int 0..6），schemas 校验 bond 时不把 mode 传给 _check_l3
        （调用点注释："bond 的 mode 是聚合模式（int），不是 dhcp 开关：无 ip 时保持
        既有语义（按 DHCP 处理）"）⇒ require_ip_when_static 这条守卫对 bond 不可达。
        生成器的既有行为也是同一语义：无 ip 的 bond 按 DHCP 生成，而不是生成一份
        "既不 DHCP 也没地址"的哑接口。netplan 侧对无地址 bond 不写任何地址块
        （连 dhcp4: true 都没有，属 L2 语义），所以这里只用 nmcli/ifcfg 断言 DHCP 回落。
        """
        for fmt, os_, dhcp_marker, bond_marker, addr_marker in (
                ("nmcli", "rhel", "ipv4.method auto", "type bond ifname bond0", "ipv4.addresses"),
                ("ifcfg", "rhel", "BOOTPROTO=dhcp", "DEVICE=bond0", "IPADDR=")):
            with self.subTest(fmt=fmt):
                req = _req(os=os_, format=fmt,
                           bonds=[{"name": "bond0", "mode": 1,
                                   "interfaces": ["eth0", "eth1"], "ip": None}])
                script, filename = generate_netconfig(req)
                self.assertIn(bond_marker, script)
                self.assertIn(dhcp_marker, script)
                self.assertNotIn(addr_marker, script)
        # 生成文件名照旧（nmcli → apply-network.sh），空 ip 不改变既有产物形状
        script, filename = generate_netconfig(_req(bonds=[{"name": "bond0", "mode": 1,
                                                           "interfaces": ["eth0", "eth1"],
                                                           "ip": None}]))
        self.assertEqual(filename, "apply-network.sh")

    def test_static_slave_interface_without_ip_is_allowed(self):
        """豁免条款：被 bond/bridge 收编的从接口 static+空 ip 必须放行。

        从接口不配地址（生成器整段跳过它、由聚合口承载地址），
        require_ip_when_static=False 就是为这个形状存在的 —— 守卫不能把它误杀。
        """
        req = _req(interfaces=[{"name": "eth0", "mode": "static", "ip": None}],
                   bonds=[{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"],
                           "ip": "10.10.0.10", "cidr": 24}])
        script, _ = generate_netconfig(req)
        self.assertIn("bond0-slave-eth0", script)
        req2 = _req(interfaces=[{"name": "eth0", "mode": "static", "ip": None}],
                    bridges=[{"name": "br0", "interfaces": ["eth0"],
                              "ip": "10.20.0.10", "cidr": 24}])
        script2, _ = generate_netconfig(req2)
        self.assertIn("br0-port-eth0", script2)


class DhcpLeftoverIpGuardTest(unittest.TestCase):
    """方向二：mode=dhcp 却残留静态字段 ⇒ 拒绝（另一侧的守卫）。"""

    def test_interface_dhcp_with_leftover_fields_rejected(self):
        """四个静态字段都在守卫的枚举里；报错必须点名 interfaces[0].<字段>。"""
        cases = {"ip": {"ip": "10.0.0.5"},
                 "gateway": {"gateway": "10.0.0.1"},
                 "netmask": {"netmask": "255.255.255.0"},
                 "cidr": {"cidr": 24}}
        for field, extra in cases.items():
            with self.subTest(field=field):
                with pytest.raises(ValueError) as e:
                    _req(os="ubuntu", format="netplan",
                         interfaces=[{"name": "eth0", "mode": "dhcp", **extra}])
                msg = str(e.value)
                self.assertIn(f"interfaces[0].{field} " + MSG_DHCP_CLASH, msg)
                self.assertIn(MSG_DHCP_WHY, msg)
                self.assertIn("要静态地址请把 mode 改成 static", msg)

    def test_vlan_dhcp_with_ip_rejected(self):
        with pytest.raises(ValueError) as e:
            _req(vlans=[{"parent": "eth0", "vlan_id": 100, "mode": "dhcp",
                         "ip": "10.0.0.5"}])
        msg = str(e.value)
        self.assertIn("vlans[0].ip " + MSG_DHCP_CLASH, msg)
        self.assertIn(MSG_DHCP_WHY, msg)

    def test_bridge_dhcp_with_ip_rejected(self):
        with pytest.raises(ValueError) as e:
            _req(bridges=[{"name": "br0", "interfaces": ["eth1"], "mode": "dhcp",
                           "ip": "10.0.0.5"}])
        msg = str(e.value)
        self.assertIn("bridges[0].ip " + MSG_DHCP_CLASH, msg)
        self.assertIn(MSG_DHCP_WHY, msg)

    def test_dhcp_with_empty_string_fields_is_accepted(self):
        """" = 表单没填，守卫按"空"放行（前端常把空输入框发成 ""/null）。"""
        req = _req(os="ubuntu", format="netplan",
                   interfaces=[{"name": "eth0", "mode": "dhcp",
                                "ip": "", "gateway": ""}])
        script, _ = generate_netconfig(req)
        self.assertIn("dhcp4: true", script)
        self.assertNotIn("addresses:", script)


class GuardPositiveCasesTest(unittest.TestCase):
    """正例：static + 合法 ip、dhcp + 空 ip 都必须通过（守卫不许扩大化误杀）。"""

    def test_static_with_valid_ip_accepted_all_formats(self):
        expect = {"nmcli": ("rhel", "ipv4.addresses 10.0.0.5/24", "ipv4.method manual"),
                  "netplan": ("ubuntu", "addresses: [10.0.0.5/24]", "dhcp4: false"),
                  "ifcfg": ("rhel", "IPADDR=10.0.0.5", "BOOTPROTO=static")}
        for fmt, (os_, addr, static_marker) in expect.items():
            with self.subTest(fmt=fmt):
                script, _ = generate_netconfig(_req(
                    os=os_, format=fmt,
                    interfaces=[{"name": "eth0", "mode": "static",
                                 "ip": "10.0.0.5", "cidr": 24, "gateway": "10.0.0.1"}]))
                self.assertIn(addr, script)
                self.assertIn(static_marker, script)

    def test_dhcp_without_ip_accepted_all_formats(self):
        expect = {"nmcli": ("rhel", "ipv4.method auto"),
                  "netplan": ("ubuntu", "dhcp4: true"),
                  "ifcfg": ("rhel", "BOOTPROTO=dhcp")}
        for fmt, (os_, dhcp_marker) in expect.items():
            with self.subTest(fmt=fmt):
                script, _ = generate_netconfig(_req(
                    os=os_, format=fmt,
                    interfaces=[{"name": "eth0", "mode": "dhcp", "ip": None}]))
                self.assertIn(dhcp_marker, script)

    def test_vlan_and_bridge_static_with_ip_accepted(self):
        """VLAN/网桥显式 static + 有 ip：守卫只拦"空 ip"，带 ip 的必须照常生成。"""
        script, _ = generate_netconfig(_req(
            vlans=[{"parent": "eth0", "vlan_id": 100, "mode": "static",
                    "ip": "10.100.0.10", "cidr": 24}],
            bridges=[{"name": "br0", "interfaces": ["eth1"], "mode": "static",
                      "ip": "10.20.0.10", "cidr": 24}]))
        self.assertIn("10.100.0.10", script)
        self.assertIn("10.20.0.10", script)


def _detail_text(resp) -> str:
    """把 422 的 detail（list[dict] 或 str）拍平成一行，便于断言文案。"""
    detail = resp.json().get("detail")
    if isinstance(detail, str):
        return detail
    parts = []
    for d in detail or []:
        if isinstance(d, dict):
            loc = ".".join(str(x) for x in d.get("loc", []) if x != "body")
            parts.append((loc + ": " if loc else "") + str(d.get("msg", "")))
        else:
            parts.append(str(d))
    return " | ".join(parts)


class GuardApiSurfaceTest(unittest.TestCase):
    """HTTP 层：守卫必须以 422 抵达接口且 detail 含原文（detail 直接进前端提示）。"""

    @classmethod
    def setUpClass(cls):
        from app.api import netconfig as netconfig_api
        from app.core.auth import get_current_user

        app = FastAPI()
        app.include_router(netconfig_api.router, prefix="/api/it/netconfig")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        app.dependency_overrides[netconfig_api.get_db] = lambda: None
        cls.client = TestClient(app)

    def _gen(self, payload):
        return self.client.post("/api/it/netconfig/generate", json=payload)

    def test_static_empty_ip_rejected_422_with_message(self):
        """有 mode 字段的三类对象：static + 空 ip 必须 422 且 detail 带守卫文案。"""
        cases = {
            "interfaces": {"interfaces": [{"name": "eth0", "mode": "static", "ip": None}]},
            "vlans": {"vlans": [{"parent": "eth0", "vlan_id": 100, "mode": "static",
                                 "ip": None}]},
            "bridges": {"bridges": [{"name": "br0", "interfaces": ["eth1"],
                                     "mode": "static", "ip": None}]},
        }
        for name, extra in cases.items():
            with self.subTest(obj=name):
                resp = self._gen({"os": "rhel", "format": "nmcli", **extra})
                text = _detail_text(resp)
                self.assertEqual(resp.status_code, 422,
                                 f"应 422，实际 {resp.status_code}: {text}")
                self.assertIn(MSG_STATIC_EMPTY, text)
                self.assertIn(MSG_STATIC_WHY, text)

    def test_dhcp_leftover_ip_rejected_422_with_message(self):
        resp = self._gen({"os": "rhel", "format": "nmcli",
                          "interfaces": [{"name": "eth0", "mode": "dhcp",
                                          "ip": "10.0.0.5"}]})
        text = _detail_text(resp)
        self.assertEqual(resp.status_code, 422, text)
        self.assertIn("interfaces[0].ip " + MSG_DHCP_CLASH, text)

    def test_bond_static_empty_ip_still_200(self):
        """HTTP 层钉同一个代码事实：bond 无 dhcp/static 开关，空 ip 回落 DHCP 而非 422。"""
        resp = self._gen({"os": "rhel", "format": "nmcli",
                          "bonds": [{"name": "bond0", "mode": 1,
                                     "interfaces": ["eth0", "eth1"], "ip": None}]})
        self.assertEqual(resp.status_code, 200, _detail_text(resp))
        self.assertIn("ipv4.method auto", resp.json()["script"])

    def test_positive_cases_200(self):
        ok = self._gen({"os": "rhel", "format": "nmcli",
                        "interfaces": [{"name": "eth0", "mode": "static",
                                        "ip": "10.0.0.5", "cidr": 24}]})
        self.assertEqual(ok.status_code, 200, _detail_text(ok))
        self.assertIn("ipv4.addresses 10.0.0.5/24", ok.json()["script"])
        dhcp = self._gen({"os": "rhel", "format": "nmcli",
                          "interfaces": [{"name": "eth0", "mode": "dhcp", "ip": None}]})
        self.assertEqual(dhcp.status_code, 200, _detail_text(dhcp))
        self.assertIn("ipv4.method auto", dhcp.json()["script"])


if __name__ == "__main__":
    unittest.main()
