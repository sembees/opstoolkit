"""设备驱动层单元测试。"""
import unittest

from app.ct.drivers import (
    BaseDriver,
    H3CDriver,
    HuaweiDriver,
    CiscoDriver,
    get_driver,
    infer_netmiko_device_type,
)
from app.ct.drivers.base import MetricCommand, vendor_from_device_type


class DriverStructureTest(unittest.TestCase):
    """驱动结构与元数据校验"""

    def test_base_driver_defaults(self):
        d = BaseDriver()
        self.assertEqual(d.vendor, "generic")
        self.assertEqual(d.default_template, "standard")
        self.assertEqual(d.standard_metrics(), [])
        self.assertEqual(d.disable_pager_commands(), [])

    def test_h3c_driver(self):
        d = H3CDriver()
        self.assertEqual(d.vendor, "h3c")
        pager = d.disable_pager_commands()
        self.assertEqual(pager, ["screen-length disable"])
        metrics = d.standard_metrics()
        self.assertGreater(len(metrics), 5)
        keys = {m.key for m in metrics}
        self.assertIn("cpu", keys)
        self.assertIn("memory", keys)
        self.assertIn("interface", keys)
        self.assertIn("version", keys)

    def test_huawei_driver(self):
        d = HuaweiDriver()
        self.assertEqual(d.vendor, "huawei")
        pager = d.disable_pager_commands()
        self.assertEqual(pager, ["screen-length 0 temporary"])
        metrics = d.standard_metrics()
        self.assertGreater(len(metrics), 5)
        keys = {m.key for m in metrics}
        self.assertIn("cpu", keys)
        self.assertIn("memory", keys)
        self.assertIn("interface", keys)

    def test_cisco_driver(self):
        d = CiscoDriver()
        self.assertEqual(d.vendor, "cisco")
        pager = d.disable_pager_commands()
        self.assertEqual(pager, ["terminal length 0"])
        metrics = d.standard_metrics()
        self.assertGreater(len(metrics), 5)
        keys = {m.key for m in metrics}
        self.assertIn("cpu", keys)
        self.assertIn("memory", keys)
        self.assertIn("interface", keys)
        self.assertIn("inventory", keys)  # cisco unique

    def test_metric_command_fields(self):
        mc = MetricCommand("cpu", "CPU", "display cpu", unit="%")
        self.assertEqual(mc.key, "cpu")
        self.assertEqual(mc.label, "CPU")
        self.assertEqual(mc.command, "display cpu")
        self.assertEqual(mc.unit, "%")
        self.assertEqual(mc.textfsm, "")
        self.assertEqual(mc.regex, [])

    def test_get_driver(self):
        self.assertIsInstance(get_driver("h3c"), H3CDriver)
        self.assertIsInstance(get_driver("HUAWEI"), HuaweiDriver)
        self.assertIsInstance(get_driver("cisco"), CiscoDriver)
        self.assertIsInstance(get_driver("unknown"), BaseDriver)
        self.assertIsInstance(get_driver(""), BaseDriver)

    def test_vendor_from_device_type(self):
        self.assertEqual(vendor_from_device_type("hp_comware"), "h3c")
        self.assertEqual(vendor_from_device_type("huawei"), "huawei")
        self.assertEqual(vendor_from_device_type("huawei_vrpv8"), "huawei")
        self.assertEqual(vendor_from_device_type("cisco_ios"), "cisco")
        self.assertEqual(vendor_from_device_type("cisco_xe"), "cisco")
        self.assertEqual(vendor_from_device_type("cisco_asa"), "cisco")
        self.assertEqual(vendor_from_device_type("cisco_nxos"), "cisco")
        self.assertEqual(vendor_from_device_type("cisco_xr"), "cisco")
        self.assertEqual(vendor_from_device_type(""), "generic")
        self.assertEqual(vendor_from_device_type("fake_vendor"), "generic")

    def test_infer_netmiko_device_type(self):
        self.assertEqual(infer_netmiko_device_type("h3c"), "hp_comware")
        self.assertEqual(infer_netmiko_device_type("comware"), "hp_comware")
        self.assertEqual(infer_netmiko_device_type("huawei"), "huawei")
        self.assertEqual(infer_netmiko_device_type("cisco"), "cisco_ios")
        self.assertEqual(infer_netmiko_device_type("cisco", "asa"), "cisco_asa")
        self.assertEqual(infer_netmiko_device_type("cisco", "firewall"), "cisco_asa")
        self.assertEqual(infer_netmiko_device_type("cisco", "xr"), "cisco_xr")
        self.assertEqual(infer_netmiko_device_type("cisco", "nexus"), "cisco_nxos")
        self.assertEqual(infer_netmiko_device_type("unknown"), "")


if __name__ == "__main__":
    unittest.main()
