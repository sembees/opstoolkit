"""网络配置生成器单元测试。

分三层：
  · NetConfigTest            —— 既有生成器用例（保持原样，回归基线）
  · NetConfigSchemaTest      —— NC2 输入校验辅助（schemas 里的 _require_* 白名单）
  · NetConfigApiTest         —— HTTP 层：8 个已确认缺陷必须 422 且 detail 指到字段路径，
                                以及所有既有可用流程仍 200
  · NetConfigGeneratorTest   —— 生成器第 3 层兜底（绕过 pydantic 直接调用）
"""
import types
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api import netconfig as netconfig_api
from app.core.auth import get_current_user
from app.core.schemas import (
    NetBondIn,
    NetBridgeIn,
    NetConfigRequest,
    NetInterfaceIn,
    NetVlanIn,
    _require_cidr,
    _require_dns_list,
    _require_ifname,
    _require_ipv4,
    _require_netmask,
)
from app.it.netconfig.generator import generate_netconfig


def _api_client():
    """只挂 netconfig 路由的最小 app：覆盖掉 JWT 依赖，不触碰 DB/lifespan。"""
    app = FastAPI()
    app.include_router(netconfig_api.router, prefix="/api/it/netconfig")
    app.dependency_overrides[get_current_user] = lambda: {
        "id": "t", "username": "t", "display_name": "t", "role": "admin",
    }
    return TestClient(app)


class _RawDevice:
    """绕过 schemas 的最小鸭子类型：生成器只依赖属性访问与 .model_dump()。

    用它来验证生成器自身（第 3 层）的兜底，而不是只验证 HTTP 层。
    """

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self) -> dict:
        return dict(self.__dict__)


def _detail_text(resp) -> str:
    """把 422 的 detail（list[dict] 或 str）拍平成一行，便于断言字段路径。"""
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


class NetConfigTest(unittest.TestCase):
    def test_netplan_full(self):
        req = NetConfigRequest(
            os="ubuntu",
            hostname="web01",
            format="netplan",
            netplan_renderer="networkd",
            interfaces=[
                NetInterfaceIn(name="ens33", mode="dhcp"),
                NetInterfaceIn(name="ens34", mode="static", ip="10.0.0.5", cidr=24, gateway="10.0.0.1", dns=["10.0.0.1"]),
            ],
            bonds=[NetBondIn(name="bond0", mode=4, interfaces=["ens35", "ens36"], ip="10.1.1.2", cidr=24, gateway="10.1.1.1")],
            vlans=[NetVlanIn(parent="bond0", vlan_id=10, mode="static", ip="10.1.10.2", cidr=24)],
            bridges=[NetBridgeIn(name="br0", interfaces=["ens37"], ip="10.2.2.2", cidr=24)],
        )
        script, filename = generate_netconfig(req)
        self.assertEqual(filename, "99-opstk.yaml")
        self.assertIn("renderer: networkd", script)
        self.assertIn("  ethernets:", script)
        self.assertIn("  bonds:", script)
        self.assertIn("  vlans:", script)
        self.assertIn("  bridges:", script)
        self.assertIn("bond0.10:", script)
        self.assertIn("mode: 802.3ad", script)
        self.assertIn("addresses: [10.1.10.2/24]", script)

    def test_nmcli_rhel(self):
        req = NetConfigRequest(
            os="rhel",
            hostname="db01",
            format="nmcli",
            interfaces=[NetInterfaceIn(name="ens160", mode="static", ip="192.168.1.10", cidr=24, gateway="192.168.1.1")],
        )
        script, filename = generate_netconfig(req)
        self.assertEqual(filename, "apply-network.sh")
        self.assertIn("nmcli connection add type ethernet ifname ens160", script)
        self.assertIn("ipv4.method manual", script)
        self.assertIn("hostnamectl set-hostname db01", script)


class NetConfigSchemaTest(unittest.TestCase):
    """NC2 输入校验辅助：这些 helper 同时被 PXE 复用，语义必须稳定。"""

    def test_require_ifname(self):
        for ok in ("eth0", "ens18", "bond0.100", "br-0", "enp0s3:1"):
            with self.subTest(ok=ok):
                self.assertEqual(_require_ifname(ok), ok)
        for bad in ("", "eth 0", "eth0;reboot", "eth0\nrm -rf /", "a" * 33, "eth0/x"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _require_ifname(bad)

    def test_require_ipv4(self):
        self.assertEqual(_require_ipv4("10.0.0.1", "address"), "10.0.0.1")
        for bad in ("", "10.0.0.256", "10.0.0", "::1", "10.0.0.1/24"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _require_ipv4(bad, "address")

    def test_require_netmask(self):
        self.assertEqual(_require_netmask("255.255.255.0"), "255.255.255.0")
        for bad in ("255.0.255.0", "255.255.255.1", "24", "255.255.255.256"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _require_netmask(bad)

    def test_require_cidr(self):
        self.assertEqual(_require_cidr(None), None)
        self.assertEqual(_require_cidr(0), 0)
        self.assertEqual(_require_cidr(32), 32)
        for bad in (-1, 33, 64):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _require_cidr(bad)

    def test_require_dns_list(self):
        self.assertEqual(_require_dns_list(["8.8.8.8"]), ["8.8.8.8"])
        for bad in (["8.8.8.8", "nope"], [""], ["10.0.0.1\n"]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _require_dns_list(bad)


class NetConfigApiTest(unittest.TestCase):
    """HTTP 层：缺陷 1~8 必须 422 且 detail 指到字段路径；既有流程必须仍然 200。"""

    @classmethod
    def setUpClass(cls):
        cls.client = _api_client()

    def gen(self, payload):
        return self.client.post("/api/it/netconfig/generate", json=payload)

    def assert_rejected(self, payload, *needles):
        """断言 422，且 detail 里出现每一个字段路径片段。"""
        resp = self.gen(payload)
        text = _detail_text(resp)
        self.assertEqual(resp.status_code, 422, f"应 422，实际 {resp.status_code}: {text}")
        for needle in needles:
            self.assertIn(needle, text)
        return text

    def assert_ok(self, payload):
        resp = self.gen(payload)
        self.assertEqual(resp.status_code, 200, _detail_text(resp))
        return resp.json()

    # ---------- 缺陷 1：os=rhel + format=netplan 静默降级成 nmcli ----------
    def test_defect1_rhel_netplan_rejected(self):
        text = self.assert_rejected(
            {"os": "rhel", "format": "netplan",
             "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5", "cidr": 24}]},
            "netplan", "ubuntu",
        )
        self.assertIn("os=", text)

    def test_defect1_ubuntu_ifcfg_rejected(self):
        self.assert_rejected(
            {"os": "ubuntu", "format": "ifcfg",
             "interfaces": [{"name": "eth0", "mode": "dhcp"}]},
            "ifcfg", "rhel",
        )

    # ---------- 缺陷 2：网关不在本接口子网内 ----------
    def test_defect2_gateway_outside_subnet_rejected(self):
        # 现场复现的原例：netmask=255.255.255.0 + gateway=192.168.1.1
        self.assert_rejected(
            {"os": "rhel", "format": "nmcli",
             "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.128.118.50",
                             "netmask": "255.255.255.0", "gateway": "192.168.1.1"}]},
            "interfaces[0].gateway", "10.128.118.0/24",
        )

    def test_defect2_gateway_check_applies_to_all_types(self):
        """前缀来自 cidr 或 netmask 都要查；bond/vlan/bridge 同样要查。"""
        cases = {
            "iface-via-cidr": {"interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                                               "cidr": 24, "gateway": "10.0.1.1"}]},
            "iface-via-netmask": {"interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                                                  "netmask": "255.255.255.0", "gateway": "10.0.1.1"}]},
            "bond": {"bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"],
                                "ip": "10.0.0.5", "cidr": 24, "gateway": "10.0.1.1"}]},
            "vlan": {"vlans": [{"parent": "eth0", "vlan_id": 10, "ip": "10.0.0.5", "cidr": 24,
                                "gateway": "10.0.1.1"}]},
            "bridge": {"bridges": [{"name": "br0", "interfaces": ["eth0"], "ip": "10.0.0.5",
                                    "cidr": 24, "gateway": "10.0.1.1"}]},
        }
        for name, payload in cases.items():
            with self.subTest(case=name):
                path = {"iface-via-cidr": "interfaces[0].gateway",
                        "iface-via-netmask": "interfaces[0].gateway",
                        "bond": "bonds[0].gateway", "vlan": "vlans[0].gateway",
                        "bridge": "bridges[0].gateway"}[name]
                self.assert_rejected({**payload, "os": "rhel", "format": "nmcli"}, path)

    def test_defect2_gateway_inside_subnet_accepted(self):
        body = self.assert_ok({"os": "rhel", "format": "nmcli",
                               "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                                               "netmask": "255.255.255.0", "gateway": "10.0.0.1"}]})
        self.assertIn("ipv4.gateway 10.0.0.1", body["script"])

    def test_defect2_gateway_default_prefix_is_24(self):
        """不给掩码时生成器按 /24 生成，校验必须用同一个默认值。"""
        self.assert_rejected(
            {"os": "rhel", "format": "nmcli",
             "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5", "gateway": "10.0.1.1"}]},
            "interfaces[0].gateway", "/24",
        )

    # ---------- 缺陷 3：两个默认网关 ----------
    def test_defect3_two_default_gateways_get_deterministic_metrics(self):
        """不改 metric 就会有多条同 metric 的 to: default；现在按声明顺序写死 100/200。"""
        case = {
            "nmcli": ("rhel", "ipv4.route-metric 100", "ipv4.route-metric 200"),
            "ifcfg": ("rhel", "METRIC=100", "METRIC=200"),
            "netplan": ("ubuntu", "metric: 100", "metric: 200"),
        }
        for fmt, (os_, m1, m2) in case.items():
            with self.subTest(fmt=fmt):
                body = self.assert_ok({
                    "os": os_, "format": fmt,
                    "interfaces": [
                        {"name": "eth0", "mode": "static", "ip": "10.0.0.5", "cidr": 24, "gateway": "10.0.0.1"},
                        {"name": "eth1", "mode": "static", "ip": "10.0.1.5", "cidr": 24, "gateway": "10.0.1.1"},
                    ],
                })
                script = body["script"]
                self.assertIn(m1, script)
                self.assertIn(m2, script)
                if fmt != "netplan":
                    self.assertNotIn("metric 100\n", script.replace(m1, ""))

    def test_defect3_metric_follows_declaration_order(self):
        """顺序是确定的：谁写在前面谁的 metric 更小（优先级更高）。"""
        body = self.assert_ok({
            "os": "rhel", "format": "nmcli",
            "interfaces": [
                {"name": "eth1", "mode": "static", "ip": "10.0.1.5", "cidr": 24, "gateway": "10.0.1.1"},
                {"name": "eth0", "mode": "static", "ip": "10.0.0.5", "cidr": 24, "gateway": "10.0.0.1"},
            ],
        })
        lines = body["script"].splitlines()
        first = next(l for l in lines if "ipv4.gateway 10.0.1.1" in l)
        second = next(l for l in lines if "ipv4.gateway 10.0.0.1" in l)
        self.assertIn("ipv4.route-metric 100", first)
        self.assertIn("ipv4.route-metric 200", second)

    def test_defect3_single_gateway_has_no_metric(self):
        """只有一个默认网关时输出与改前逐字节一致（不写 metric）。"""
        for fmt, os_ in (("nmcli", "rhel"), ("ifcfg", "rhel"), ("netplan", "ubuntu")):
            with self.subTest(fmt=fmt):
                body = self.assert_ok({
                    "os": os_, "format": fmt,
                    "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                                    "cidr": 24, "gateway": "10.0.0.1"}],
                })
                self.assertNotIn("metric", body["script"].lower())

    # ---------- 缺陷 4：cidr 与 netmask 冲突 ----------
    def test_defect4_cidr_netmask_conflict_rejected(self):
        self.assert_rejected(
            {"os": "rhel", "format": "nmcli",
             "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                             "netmask": "255.255.255.0", "cidr": 16}]},
            "interfaces[0].cidr", "interfaces[0].netmask",
        )

    def test_defect4_agreeing_cidr_and_netmask_accepted(self):
        body = self.assert_ok({"os": "rhel", "format": "nmcli",
                               "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                                               "netmask": "255.255.255.0", "cidr": 24}]})
        self.assertIn("ipv4.addresses 10.0.0.5/24", body["script"])

    # ---------- 缺陷 5：os / format 未校验 ----------
    def test_defect5_os_format_whitelist(self):
        self.assert_rejected({"os": "windows", "format": "nmcli",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "os")
        self.assert_rejected({"os": "rhel", "format": "poem",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "format")

    def test_defect5_uppercase_values_normalised(self):
        """大小写/空格归一化后再过白名单：'RHEL '+'NETPLAN' 不是非法值，但 RHEL+netplan 仍非法。"""
        body = self.assert_ok({"os": "RHEL ", "format": " NMCLI",
                               "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
        self.assertEqual(body["format"], "nmcli")

    def test_netplan_renderer_whitelist(self):
        self.assert_rejected({"os": "ubuntu", "format": "netplan", "netplan_renderer": "poem",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "netplan_renderer")
        self.assert_rejected({"os": "ubuntu", "format": "netplan",
                              "netplan_renderer": "networkd\n  dhcp4: true",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "netplan_renderer")
        body = self.assert_ok({"os": "ubuntu", "format": "netplan",
                               "netplan_renderer": "NetworkManager",
                               "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
        self.assertIn("renderer: NetworkManager", body["script"])

    # ---------- 命令注入：os / format / netplan_renderer 里的换行 ----------
    def test_injection_os_newline_rejected(self):
        """线上实测过的原例：os="ubuntu\\nid > /tmp/pwned #" → 200 且脚本里多一条真命令。"""
        self.assert_rejected({"os": "ubuntu\nid > /tmp/pwned #", "format": "nmcli",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "os")

    def test_injection_format_newline_rejected(self):
        self.assert_rejected({"os": "rhel", "format": "nmcli\nid > /tmp/pwned #",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "format")

    def test_injection_renderer_newline_rejected(self):
        self.assert_rejected({"os": "ubuntu", "format": "netplan",
                              "netplan_renderer": "networkd\nid > /tmp/pwned #",
                              "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "netplan_renderer")

    def test_injection_carriage_return_and_tab_rejected(self):
        for bad in ("ubuntu\rid #", "ubuntu\tid", "ubuntu\x00id", "ubuntu\x7f"):
            with self.subTest(bad=bad):
                self.assert_rejected({"os": bad, "format": "nmcli",
                                      "interfaces": [{"name": "eth0", "mode": "dhcp"}]}, "os")

    def test_bond_free_text_fields_validated(self):
        """primary/lacp_rate/xmit_hash_policy 会被拼进 BONDING_OPTS="..."，非法值必须 422。"""
        self.assert_rejected({"os": "rhel", "format": "ifcfg",
                              "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"],
                                         "primary": 'ens18" \n rm -rf /'}]}, "primary")
        self.assert_rejected({"os": "rhel", "format": "ifcfg",
                              "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"],
                                         "lacp_rate": "fast\nid"}]}, "lacp_rate")
        self.assert_rejected({"os": "rhel", "format": "ifcfg",
                              "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"],
                                         "xmit_hash_policy": "layer2;id"}]}, "xmit_hash_policy")
        body = self.assert_ok({"os": "rhel", "format": "ifcfg",
                               "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"],
                                          "primary": "ens18"}]})
        self.assertIn('BONDING_OPTS="mode=active-backup miimon=100 primary=ens18"', body["script"])

    # ---------- 缺陷 6：mode=dhcp + ip ----------
    def test_defect6_dhcp_with_ip_rejected(self):
        self.assert_rejected({"os": "ubuntu", "format": "netplan",
                              "interfaces": [{"name": "eth0", "mode": "dhcp",
                                              "ip": "10.0.0.5", "cidr": 24}]},
                             "interfaces[0].ip", "mode=dhcp")

    def test_defect6_dhcp_with_other_static_fields_rejected(self):
        cases = {
            "gateway": {"gateway": "10.0.0.1"},
            "netmask": {"netmask": "255.255.255.0"},
            "cidr": {"cidr": 24},
        }
        for field, extra in cases.items():
            with self.subTest(field=field):
                self.assert_rejected(
                    {"os": "rhel", "format": "nmcli",
                     "interfaces": [{"name": "eth0", "mode": "dhcp", **extra}]},
                    f"interfaces[0].{field}",
                )

    def test_defect6_plain_dhcp_accepted(self):
        body = self.assert_ok({"os": "ubuntu", "format": "netplan",
                               "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
        self.assertIn("dhcp4: true", body["script"])
        self.assertNotIn("addresses:", body["script"])

    # ---------- 缺陷 7：全空配置 ----------
    def test_defect7_empty_config_rejected(self):
        text = self.assert_rejected({"os": "ubuntu", "format": "netplan"},
                                    "interfaces", "bonds", "vlans", "bridges")
        self.assertNotIn("network:", text)

    def test_defect7_empty_lists_rejected(self):
        self.assert_rejected({"os": "rhel", "format": "nmcli", "hostname": "bare",
                              "interfaces": [], "bonds": [], "vlans": [], "bridges": []},
                             "interfaces")

    # ---------- 缺陷 8：mode=static 但没有 ip ----------
    def test_defect8_static_without_ip_rejected(self):
        """改前实测（三种格式都是 200，静态意图被静默丢掉）：
        netplan `dhcp4: true`（dhcp_default 回退）、nmcli `ipv4.method auto`、
        ifcfg `BOOTPROTO=dhcp`；VLAN 在 netplan 下则连地址块都不生成。
        """
        for fmt, os_ in (("nmcli", "rhel"), ("netplan", "ubuntu"), ("ifcfg", "rhel")):
            with self.subTest(fmt=fmt):
                self.assert_rejected(
                    {"os": os_, "format": fmt,
                     "interfaces": [{"name": "eth0", "mode": "static", "ip": None,
                                     "cidr": None, "netmask": None, "gateway": None}]},
                    "interfaces[0].ip", "mode=static",
                )

    def test_defect8_vlan_static_without_ip_rejected(self):
        self.assert_rejected({"os": "ubuntu", "format": "netplan",
                              "vlans": [{"parent": "eth0", "vlan_id": 100, "ip": None}]},
                             "vlans[0].ip")

    def test_defect8_bridge_static_without_ip_rejected(self):
        self.assert_rejected({"os": "ubuntu", "format": "netplan",
                              "bridges": [{"name": "br0", "interfaces": ["eth0"], "mode": "static"}]},
                             "bridges[0].ip")

    # ---------- 既有可用流程必须仍然成功 ----------
    def test_ok_single_static_interface_each_format(self):
        expect = {
            "nmcli": ("rhel", "ipv4.addresses 10.0.0.5/24", "apply-network.sh"),
            "netplan": ("ubuntu", "addresses: [10.0.0.5/24]", "99-opstk.yaml"),
            "ifcfg": ("rhel", "IPADDR=10.0.0.5", "ifcfg-files.txt"),
        }
        for fmt, (os_, needle, filename) in expect.items():
            with self.subTest(fmt=fmt):
                body = self.assert_ok({
                    "os": os_, "format": fmt, "hostname": "srv01",
                    "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.5",
                                    "cidr": 24, "gateway": "10.0.0.1", "dns": ["8.8.8.8"]}],
                })
                self.assertIn(needle, body["script"])
                self.assertEqual(body["filename"], filename)
                self.assertEqual(body["format"], fmt)

    def test_ok_plain_dhcp_each_format(self):
        expect = {"nmcli": ("rhel", "ipv4.method auto"),
                  "netplan": ("ubuntu", "dhcp4: true"),
                  "ifcfg": ("rhel", "BOOTPROTO=dhcp")}
        for fmt, (os_, needle) in expect.items():
            with self.subTest(fmt=fmt):
                body = self.assert_ok({"os": os_, "format": fmt,
                                       "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
                self.assertIn(needle, body["script"])

    def test_ok_bond_vlan_bridge_each_format(self):
        for fmt, os_ in (("nmcli", "rhel"), ("netplan", "ubuntu"), ("ifcfg", "rhel")):
            with self.subTest(fmt=fmt):
                body = self.assert_ok({
                    "os": os_, "format": fmt, "hostname": "agg01",
                    "interfaces": [{"name": "eth0", "mode": "dhcp"}, {"name": "eth1", "mode": "dhcp"}],
                    # 只留一个默认网关（bond0），网关必须在自己的子网内
                    "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"],
                               "ip": "10.10.0.10", "cidr": 24, "gateway": "10.10.0.1"}],
                    "vlans": [{"parent": "bond0", "vlan_id": 100, "ip": "10.100.0.10", "cidr": 24}],
                    "bridges": [{"name": "br0", "interfaces": ["eth2"], "ip": "10.20.0.10", "cidr": 24}],
                })
                script = body["script"]
                self.assertIn("bond0", script)
                self.assertIn("bond0.100" if fmt != "nmcli" else "bond0.100", script)
                self.assertIn("br0", script)

    def test_ok_slave_interface_without_mode_keeps_working(self):
        """被 bond 引用的从接口不配地址：即便没写 mode（默认 static）也不能判死。"""
        body = self.assert_ok({"os": "rhel", "format": "nmcli",
                               "interfaces": [{"name": "eth0"}, {"name": "eth1"}],
                               "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"],
                                          "ip": "10.10.0.10", "cidr": 24}]})
        self.assertIn("bond0-slave-eth0", body["script"])

    def test_ok_empty_string_fields_from_frontend(self):
        """前端历史载荷：hostname=""、bond ip="" —— 空串按"未填"处理，不应 422。"""
        body = self.assert_ok({
            "os": "rhel", "format": "nmcli", "hostname": "",
            "interfaces": [{"name": "eth0", "mode": "dhcp", "ip": None, "gateway": ""}],
            "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth1", "eth2"], "ip": "",
                       "cidr": None, "gateway": "", "dns": []}],
        })
        self.assertNotIn("hostnamectl", body["script"])
        self.assertIn("ipv4.method auto", body["script"])

    def test_ok_vlan_dns_and_bridge_mode_are_honoured(self):
        """前端 VLAN/网桥行的 DNS 与网桥模式改前被静默丢弃，现在必须出现在输出里。"""
        for fmt, os_ in (("nmcli", "rhel"), ("netplan", "ubuntu"), ("ifcfg", "rhel")):
            with self.subTest(fmt=fmt):
                body = self.assert_ok({
                    "os": os_, "format": fmt,
                    "vlans": [{"parent": "eth0", "vlan_id": 100, "ip": "10.100.0.10",
                               "cidr": 24, "dns": ["8.8.8.8"]}],
                    "bridges": [{"name": "br0", "interfaces": ["eth1"], "mode": "static",
                                 "ip": "10.20.0.10", "cidr": 24, "dns": ["9.9.9.9"]}],
                })
                self.assertIn("8.8.8.8", body["script"])
                self.assertIn("9.9.9.9", body["script"])

    def test_ok_bridge_dhcp_mode(self):
        body = self.assert_ok({"os": "ubuntu", "format": "netplan",
                               "bridges": [{"name": "br0", "interfaces": ["eth1"], "mode": "dhcp"}]})
        self.assertIn("dhcp4: true", body["script"])

    def test_ok_bridge_auto_mode_without_ip(self):
        """网桥不带 mode/ip（纯二层）不能判死。"""
        body = self.assert_ok({"os": "ubuntu", "format": "netplan",
                               "bridges": [{"name": "br0", "interfaces": ["eth1"]}]})
        self.assertIn("br0:", body["script"])
        self.assertNotIn("dhcp4: true", body["script"])

    def test_ok_dhcp_with_manual_dns(self):
        """DHCP 口 + 手填 DNS 改前被生成器整段丢掉，现在三种格式都要写出来。"""
        expect = {"nmcli": ("rhel", "ipv4.dns 8.8.8.8"),
                  "netplan": ("ubuntu", "addresses: [8.8.8.8]"),
                  "ifcfg": ("rhel", "DNS1=8.8.8.8")}
        for fmt, (os_, needle) in expect.items():
            with self.subTest(fmt=fmt):
                body = self.assert_ok({"os": os_, "format": fmt,
                                       "interfaces": [{"name": "eth0", "mode": "dhcp",
                                                       "dns": ["8.8.8.8"]}]})
                self.assertIn(needle, body["script"])

    def test_download_endpoint_shares_validation(self):
        ok = self.client.post("/api/it/netconfig/download", json={
            "os": "ubuntu", "format": "netplan",
            "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
        self.assertEqual(ok.status_code, 200)
        self.assertIn("99-opstk.yaml", ok.headers.get("content-disposition", ""))
        bad = self.client.post("/api/it/netconfig/download", json={
            "os": "rhel", "format": "netplan",
            "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
        self.assertEqual(bad.status_code, 422)

    def test_meta_endpoint_unchanged(self):
        meta = self.client.get("/api/it/netconfig/meta")
        self.assertEqual(meta.status_code, 200)
        body = meta.json()
        self.assertEqual({f["id"] for f in body["formats"]}, {"nmcli", "netplan", "ifcfg"})
        self.assertEqual({o["id"] for o in body["os_options"]}, {"ubuntu", "rhel"})
        self.assertEqual(len(body["bond_modes"]), 7)


class NetConfigGeneratorTest(unittest.TestCase):
    """第 3 层兜底：绕过 pydantic 直接调用生成器时，非法组合必须显式报错而不是静默换格式。"""

    @staticmethod
    def _stub(**kw):
        base = dict(os="rhel", format="nmcli", hostname=None,
                    interfaces=[], bonds=[], vlans=[], bridges=[], netplan_renderer="networkd")
        base.update(kw)
        return types.SimpleNamespace(**base)

    @staticmethod
    def _iface_stub(name="eth0", mode="dhcp", ip=None, gateway=None, dns=None):
        """手工造一个"绕过 schemas"的接口对象：字段是原始字符串，校验一概没跑过。

        生成器只依赖属性访问与 .model_dump()，所以这里给一个最小鸭子类型实现。
        """
        return _RawDevice(
            name=name, mode=mode, ip=ip, netmask=None, cidr=24, gateway=gateway,
            dns=dns or [],
        )

    def test_netplan_requires_ubuntu(self):
        with self.assertRaises(ValueError):
            generate_netconfig(self._stub(os="rhel", format="netplan"))

    def test_ifcfg_requires_rhel(self):
        with self.assertRaises(ValueError):
            generate_netconfig(self._stub(os="ubuntu", format="ifcfg"))

    def test_unknown_format_raises(self):
        with self.assertRaises(ValueError):
            generate_netconfig(self._stub(format="poem"))

    def test_nmcli_returns_shell_filename(self):
        script, filename = generate_netconfig(self._stub())
        self.assertEqual(filename, "apply-network.sh")
        self.assertIn("#!/bin/bash", script)

    # ---------- 生成器层的注入兜底（不经过 HTTP / pydantic） ----------
    def test_generator_rejects_newline_in_os(self):
        req = self._stub(os="ubuntu\nid > /tmp/pwned #")
        with self.assertRaises(ValueError) as cm:
            generate_netconfig(req)
        self.assertIn("os", str(cm.exception))

    def test_generator_rejects_newline_in_renderer(self):
        req = self._stub(os="ubuntu", format="netplan",
                         netplan_renderer="networkd\nid > /tmp/pwned #")
        with self.assertRaises(ValueError) as cm:
            generate_netconfig(req)
        self.assertIn("netplan_renderer", str(cm.exception))

    def test_generator_rejects_control_chars_in_device_fields(self):
        """接口名 / 地址 / DNS / bond 参数里的换行，三条生成路径都要在写文件前拦下。"""
        cases = {
            "nmcli-ifname": dict(os="rhel", format="nmcli",
                                 interfaces=[self._iface_stub(name="eth0\nid")]),
            "nmcli-hostname": dict(os="rhel", format="nmcli", hostname="h\nid"),
            "ifcfg-gateway": dict(os="rhel", format="ifcfg",
                                  interfaces=[self._iface_stub(mode="static", ip="10.0.0.5",
                                                               gateway="10.0.0.1\nid")]),
            "netplan-ifname": dict(os="ubuntu", format="netplan",
                                   interfaces=[self._iface_stub(name="eth0\nid")]),
            "netplan-dns": dict(os="ubuntu", format="netplan",
                                interfaces=[self._iface_stub(mode="static", ip="10.0.0.5",
                                                             dns=["8.8.8.8\nid"])]),
        }
        for name, kw in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    generate_netconfig(self._stub(**kw))


if __name__ == "__main__":
    unittest.main()
