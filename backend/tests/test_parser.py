"""命令输出解析回退单元测试。"""
import unittest

from app.ct.inspection.parser import parse_output


class ParserFallbackTest(unittest.TestCase):
    def test_cpu_fallback(self):
        result = parse_output("cpu", "CPU utilization: 45% in 5 seconds", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["cpu"], 45)

    def test_cpu_high_warning(self):
        result = parse_output("cpu", "Five seconds: 82%", textfsm_name="")
        self.assertEqual(result["status"], "warning")

    def test_memory_fallback(self):
        result = parse_output("memory", "Memory usage: 72%", textfsm_name="")
        self.assertEqual(result["parsed"]["memory"], 72)

    def test_version_h3c(self):
        result = parse_output("version", "H3C Comware Software, Version 7.1.070, ESS 0708", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["version"], "7.1.070")

    def test_version_huawei(self):
        result = parse_output("version", "Huawei Versatile Routing Platform Software, VRP (R) software, Version 8.180", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["version"], "8.180")

    def test_version_cisco(self):
        result = parse_output("version", "Cisco IOS-XE Software, Version 17.9.1", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["version"], "17.9.1")

    def test_interface_fallback(self):
        result = parse_output("interface", "GigabitEthernet1/0/1 up\nGigabitEthernet1/0/2 down", textfsm_name="")
        self.assertEqual(result["status"], "warning")
        self.assertEqual(result["parsed"]["up"], 1)
        self.assertEqual(result["parsed"]["down"], 1)
        self.assertEqual(result["parsed"]["total"], 2)

    def test_temperature_fallback(self):
        result = parse_output("temperature", "Temperature: 55 C", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["temperature"], 55)

    def test_power_fallback(self):
        result = parse_output("power", "Power 1: normal\nPower 2: fault", textfsm_name="")
        self.assertEqual(result["status"], "critical")
        self.assertEqual(result["parsed"]["power"]["bad"], 1)

    def test_fan_fallback(self):
        result = parse_output("fan", "Fan 1: normal\nFan 2: normal", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["fan"]["ok"], 2)

    def test_environment_structure(self):
        result = parse_output("environment", "Temperature: 45 C\nPower 1: normal\nFan 1: normal", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertIn("temperature", result["parsed"])
        self.assertIn("power", result["parsed"])
        self.assertIn("fan", result["parsed"])
        self.assertNotIn("parsed", result["parsed"])

    def test_inventory_fallback(self):
        result = parse_output("inventory", "Slot 0: PID: S6850-32Q\nSlot 1: PID: LS-5130", textfsm_name="")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["parsed"]["count"], 2)

    def test_unknown_key_summary(self):
        result = parse_output("bogus", "H3C Comware Software, Version 7.1.070", textfsm_name="")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("H3C Comware Software", result["summary"])


if __name__ == "__main__":
    unittest.main()
