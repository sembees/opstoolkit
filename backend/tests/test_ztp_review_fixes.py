# -*- coding: utf-8 -*-
"""外部审查（小米 MiMo Token Plan `mimo-v2.6-pro`）在 ZTP 模块报出的问题 —— 逐条回归。

每一条都先由**本会话独立复核**（读码 + 复现）确认是真缺陷，再修；这里锁住修好的行为。
对应审查编号：
  F1  CSV 导入 replace=true 且零有效行 ⇒ 清空整落位表（数据丢失）
  F2  配置文件名撞名 ⇒ 多台设备共用同一份配置（主机名/管理 IP 重复）
  F3  serial/hostname 带引号或换行 ⇒ 注入宿主机 dnsmasq 配置
  F4  管理 VLAN 与接口名不一致时，端口划进另一个 VLAN
  F5  ZtpDevice 的 MAC 未归一 ⇒ dhcp-host 写错 / 与租约对不上
  F6  批量导入里两条落位互换 IP/MAC 被误判为冲突
"""
import os
import pathlib
import sys
import unittest

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.ct.ztp.generator import (  # noqa: E402
    ZtpDevice,
    ZtpProfile,
    cisco_config,
    dnsmasq,
    generate_all,
    h3c_config,
    huawei_config,
)


def prof(**kw):
    base = dict(vendor="h3c", admin_password="TestPw@123", admin_user="admin",
                snmp_community="Opstk@2026", mgmt_vlan=10,
                mgmt_interface="Vlan-interface10", mgmt_gateway="10.0.0.254",
                dhcp_iface="ens19", server_ip="192.168.199.1",
                dhcp_start="192.168.199.210", dhcp_end="192.168.199.240")
    base.update(kw)
    return ZtpProfile(**base)


# ---------------------------------------------------------------- F2 / F3
class StemCollisionTest(unittest.TestCase):
    def test_two_positions_with_same_hostname_is_rejected(self):
        """撞名会让两台设备拿到同一份配置（同一 sysname、同一静态管理 IP）。"""
        p = prof()
        with pytest.raises(ValueError) as e:
            generate_all(p, [], positions=[
                {"position": "A01", "hostname": "SW01", "mgmt_ip": "10.0.0.11", "serial": ""},
                {"position": "A02", "hostname": "SW01", "mgmt_ip": "10.0.0.12", "serial": ""},
            ])
        self.assertIn("撞名", str(e.value))

    def test_two_devices_with_same_serial_is_rejected(self):
        p = prof()
        with pytest.raises(ValueError) as e:
            generate_all(p, [ZtpDevice(hostname="a", serial="SN1"),
                             ZtpDevice(hostname="b", serial="SN1")])
        self.assertIn("撞名", str(e.value))

    def test_position_may_still_override_manual_device_with_same_stem(self):
        """兼容行为保持不变：落位覆盖同名的手工登记设备（有既有用例锁着）。"""
        p = prof()
        files = generate_all(p, [ZtpDevice(hostname="SW01", mgmt_ip="10.0.0.9")],
                             positions=[{"position": "A01", "hostname": "SW01",
                                         "mgmt_ip": "10.0.0.11", "serial": ""}])
        self.assertIn("ztp/SW01.cfg", files)
        self.assertIn("10.0.0.11", files["ztp/SW01.cfg"])   # 落位那份赢

    def test_distinct_stems_are_fine(self):
        p = prof()
        files = generate_all(p, [], positions=[
            {"position": "A01", "hostname": "SW01", "mgmt_ip": "10.0.0.11", "serial": "SN1"},
            {"position": "A02", "hostname": "SW02", "mgmt_ip": "10.0.0.12", "serial": "SN2"},
        ])
        self.assertIn("ztp/SN1.cfg", files)
        self.assertIn("ztp/SN2.cfg", files)


class InjectionIntoDnsmasqTest(unittest.TestCase):
    def test_serial_with_newline_or_quote_is_rejected(self):
        p = prof()
        for bad in ('x"\nport=0\n#', "abc\ndef", 'quote"here'):
            with self.subTest(bad=bad):
                with pytest.raises(ValueError):
                    generate_all(p, [ZtpDevice(hostname="SW1", serial=bad,
                                               mac="00:11:22:33:44:55")])

    def test_hostname_with_newline_is_rejected(self):
        p = prof()
        with pytest.raises(ValueError):
            generate_all(p, [ZtpDevice(hostname="SW1\nport=0", mgmt_ip="10.0.0.11")])

    def test_profile_fields_with_newline_are_rejected(self):
        for field in ("domain_name", "snmp_community", "ntp_server"):
            with self.subTest(field=field):
                p = prof(**{field: "evil\nport=0"})
                with pytest.raises(ValueError):
                    h3c_config(ZtpDevice(hostname="SW1", mgmt_ip="10.0.0.11"), p)

    def test_generated_conf_has_no_extra_directive(self):
        """正常值下：dnsmasq 行数与内容必须干净（没有多出来的指令行）。"""
        p = prof()
        conf = dnsmasq(p, [ZtpDevice(hostname="SW01", serial="SN1",
                                     mac="AA:BB:CC:DD:EE:01")])
        bad = [ln for ln in conf.splitlines()
               if ln.strip() and not ln.strip().startswith("#")
               and not ln.startswith(("interface=", "bind-interfaces", "dhcp-range=",
                                      "dhcp-option=", "dhcp-host=", "enable-tftp",
                                      "tftp-root="))]
        self.assertEqual(bad, [], "出现了计划外的 dnsmasq 指令: %r" % bad)


# ---------------------------------------------------------------- F4
class PortVlanMatchesSviTest(unittest.TestCase):
    """管理 VLAN 号与接口名不一致时，端口必须跟着 **SVI 的 VLAN**（以接口名为准）。"""

    KW = dict(mgmt_vlan=10, mgmt_interface="Vlan-interface100",
              access_ports=["GigabitEthernet1/0/1"], uplink_port="GigabitEthernet1/0/2")

    def test_h3c_ports_follow_svi_vlan(self):
        out = h3c_config(ZtpDevice(hostname="SW1", mgmt_ip="10.0.0.11"), prof(**self.KW))
        lines = out.splitlines()
        self.assertIn("interface Vlan-interface100", lines)
        # 注意用**整行**比较：'port access vlan 10' 是 'port access vlan 100' 的子串，
        # assertNotIn 的字符串包含判定会误报（我第一版测试就栽在这）。
        self.assertNotIn("port access vlan 10", lines)
        self.assertEqual(out.count("port access vlan 100"), 2)   # 接入 + 上联

    def test_huawei_ports_follow_svi_vlan(self):
        out = huawei_config(ZtpDevice(hostname="SW1", mgmt_ip="10.0.0.11"), prof(**self.KW))
        self.assertIn("interface Vlanif100", out.splitlines())
        self.assertNotIn("port default vlan 10", out.splitlines())
        self.assertEqual(out.count("port default vlan 100"), 2)

    def test_cisco_ports_follow_svi_vlan(self):
        out = cisco_config(ZtpDevice(hostname="SW1", mgmt_ip="10.0.0.11"),
                           prof(**self.KW))
        self.assertIn("interface Vlan100", out.splitlines())
        self.assertNotIn("switchport access vlan 10", out.splitlines())
        self.assertEqual(out.count("switchport access vlan 100"), 2)


# ---------------------------------------------------------------- F5
class DeviceMacNormalizationTest(unittest.TestCase):
    def test_dot_form_mac_is_normalized_in_dhcp_host(self):
        conf = dnsmasq(prof(), [ZtpDevice(hostname="SW1", serial="SN1",
                                          mac="AABB.CCDD.EEFF")])
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:ff,set:set_aabbccddeeff", conf)
        self.assertNotIn("AABB.CCDD.EEFF", conf)

    def test_invalid_mac_is_rejected_not_silently_defaulted(self):
        """写错的 MAC 不能静默变成"没填"（那会让设备只拿到 default.cfg）。"""
        with pytest.raises(ValueError) as e:
            dnsmasq(prof(), [ZtpDevice(hostname="SW1", mac="not-a-mac")])
        self.assertIn("MAC", str(e.value))

    def test_empty_mac_still_falls_back_to_default(self):
        conf = dnsmasq(prof(), [ZtpDevice(hostname="SW1", mac="")])
        self.assertIn("缺少 MAC", conf)
        self.assertNotIn("dhcp-host=,", conf)


# ---------------------------------------------------------------- F1 / F5 / F6（HTTP 层）
from test_ztp_positions import _ApiTestBase  # noqa: E402


class ImportDataSafetyTest(_ApiTestBase):
    """F1：replace=true 且零有效行时**不能**清空落位表。"""

    CSV_BAD_IP = "落位,管理IP\nA02,999.1.1.1\n"
    CSV_EMPTY = ""
    CSV_NO_POSITION = "落位,管理IP\n,10.0.0.99\n"
    CSV_OK = "落位,管理IP\nA02,10.0.0.99\n"

    def _seed_two(self, client, tid):
        self._create(client, tid, position="A01", mgmt_ip="10.0.0.11")
        self._create(client, tid, position="A02", mgmt_ip="10.0.0.12")

    def _positions(self, client, tid):
        r = client.get("/api/ct/ztp/positions", params={"template_id": tid})
        return sorted((x["position"], x["mgmt_ip"]) for x in r.json())

    def test_replace_with_all_invalid_rows_is_refused_and_keeps_data(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._seed_two(client, tid)
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "csv": self.CSV_BAD_IP, "replace": True})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertIn("未删除任何落位", r.text)
        self.assertEqual(self._positions(client, tid),
                         [("A01", "10.0.0.11"), ("A02", "10.0.0.12")])

    def test_replace_with_empty_csv_is_refused(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._seed_two(client, tid)
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "csv": self.CSV_EMPTY, "replace": True})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(len(self._positions(client, tid)), 2)

    def test_replace_with_rows_missing_position_is_refused(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._seed_two(client, tid)
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "csv": self.CSV_NO_POSITION,
                              "replace": True})
        self.assertEqual(r.status_code, 422, r.text)
        self.assertEqual(len(self._positions(client, tid)), 2)

    def test_replace_with_valid_rows_replaces(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._seed_two(client, tid)
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "csv": self.CSV_OK, "replace": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["created"], 1)
        self.assertEqual(self._positions(client, tid), [("A02", "10.0.0.99")])


class ImportSwapTest(_ApiTestBase):
    """F6：批内互换 IP/MAC 是合法操作，不能按"当前状态"误判成冲突。"""

    def _positions(self, client, tid):
        r = client.get("/api/ct/ztp/positions", params={"template_id": tid})
        return {x["position"]: (x["mgmt_ip"], x["mac"]) for x in r.json()}

    def test_swap_mgmt_ips_between_two_positions(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._create(client, tid, position="A01", mgmt_ip="10.0.0.11")
        self._create(client, tid, position="A02", mgmt_ip="10.0.0.12")
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "replace": False,
                              "csv": "落位,管理IP\nA01,10.0.0.12\nA02,10.0.0.11\n"})
        self.assertEqual(r.status_code, 200, r.text)
        js = r.json()
        self.assertEqual(js["errors"], [], js)
        self.assertEqual(js["updated"], 2)
        got = self._positions(client, tid)
        self.assertEqual(got["A01"][0], "10.0.0.12")
        self.assertEqual(got["A02"][0], "10.0.0.11")

    def test_swap_macs_between_two_positions(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._create(client, tid, position="A01", mgmt_ip="10.0.0.11",
                     mac="aa:bb:cc:dd:ee:01")
        self._create(client, tid, position="A02", mgmt_ip="10.0.0.12",
                     mac="aa:bb:cc:dd:ee:02")
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "replace": False,
                              "csv": ("落位,管理IP,MAC\n"
                                      "A01,10.0.0.11,aa:bb:cc:dd:ee:02\n"
                                      "A02,10.0.0.12,aa:bb:cc:dd:ee:01\n")})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["errors"], [], r.json())
        got = self._positions(client, tid)
        self.assertEqual(got["A01"][1], "aa:bb:cc:dd:ee:02")
        self.assertEqual(got["A02"][1], "aa:bb:cc:dd:ee:01")

    def test_real_duplicate_inside_batch_is_still_reported(self):
        client = self._client()
        tid = self._seed_template("T1")
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "replace": False,
                              "csv": "落位,管理IP\nA01,10.0.0.50\nA02,10.0.0.50\n"})
        self.assertEqual(r.status_code, 200, r.text)
        js = r.json()
        self.assertEqual(js["created"], 1)
        self.assertEqual(js["skipped"], 1)
        self.assertTrue(any("本批次内重复" in e for e in js["errors"]), js)

    def test_conflict_with_existing_position_not_in_batch_is_reported(self):
        client = self._client()
        tid = self._seed_template("T1")
        self._create(client, tid, position="A01", mgmt_ip="10.0.0.11")
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "replace": False,
                              "csv": "落位,管理IP\nA09,10.0.0.11\n"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["created"], 0)
        self.assertTrue(any("重复" in e for e in r.json()["errors"]), r.json())

    def test_invalid_mac_row_is_reported_not_silently_unclaimed(self):
        client = self._client()
        tid = self._seed_template("T1")
        r = client.post("/api/ct/ztp/positions/import",
                        json={"template_id": tid, "replace": False,
                              "csv": "落位,管理IP,MAC\nA01,10.0.0.11,zz:zz:zz:zz:zz:zz\n"})
        js = r.json()
        self.assertEqual(js["created"], 0)
        self.assertTrue(any("MAC 格式不正确" in e for e in js["errors"]), js)


class CreateDeviceMacTest(_ApiTestBase):
    """F5（HTTP 层）：设备清单的 MAC 入口也要归一/校验。"""

    def _template_with_pw(self) -> str:
        """生成配置需要模板里有设备管理员口令（生成器 fail-closed），这里种一个。"""
        import asyncio

        from app.core import crypto, models

        async def _go():
            async with self.SessionLocal() as s:
                t = models.ZtpTemplate(name="T-pw", vendor="h3c",
                                       admin_password_enc=crypto.encrypt("TestPw@123"))
                s.add(t)
                await s.commit()
                await s.refresh(t)
                return t.id
        return asyncio.run(_go())

    def _device_payload(self, tid, mac):
        return {"template_id": tid, "hostname": "SW1", "mac": mac,
                "serial": "SN1", "mgmt_ip": "10.0.0.11"}

    def test_dot_form_mac_is_stored_normalized(self):
        client = self._client()
        tid = self._template_with_pw()
        r = client.post("/api/ct/ztp/devices", json=self._device_payload(tid, "AABB.CCDD.EEFF"))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mac"], "aa:bb:cc:dd:ee:ff")
        # 生成时必须用归一值（否则 dhcp-host 与租约对不上）
        g = client.post("/api/ct/ztp/templates/%s/generate" % tid, json={})
        self.assertEqual(g.status_code, 200, g.text)
        self.assertIn("dhcp-host=aa:bb:cc:dd:ee:ff,set:set_aabbccddeeff",
                      g.json()["files"]["dnsmasq.conf"])

    def test_invalid_mac_is_422(self):
        client = self._client()
        tid = self._seed_template("T1")
        r = client.post("/api/ct/ztp/devices", json=self._device_payload(tid, "not-a-mac"))
        self.assertEqual(r.status_code, 422, r.text)

    def test_empty_mac_is_still_allowed(self):
        client = self._client()
        tid = self._seed_template("T1")
        r = client.post("/api/ct/ztp/devices", json=self._device_payload(tid, ""))
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["mac"], "")
