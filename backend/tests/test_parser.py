"""命令输出解析回落单元测试。"""
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

    def test_unknown_key_summary(self):
        result = parse_output("version", "H3C Comware Software, Version 7.1.070", textfsm_name="")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("H3C Comware Software", result["summary"])


if __name__ == "__main__":
    unittest.main()
