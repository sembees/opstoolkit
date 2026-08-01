"""ZTP 生成器单元测试。"""
import unittest

from app.ct.ztp.generator import ZtpDevice, ZtpProfile, generate_all


class ZtpGeneratorTest(unittest.TestCase):
    def setUp(self):
        self.devices = [
            ZtpDevice(hostname="Core-SW01", mac="00:11:22:33:44:55", serial="SN001", mgmt_ip="10.0.0.2"),
            ZtpDevice(hostname="Core-SW02", mac="aa:bb:cc:dd:ee:ff", serial="SN002", mgmt_ip="10.0.0.3"),
        ]

    def test_h3c_generate_and_vendor_normalize(self):
        p = ZtpProfile(vendor="H3C", server_ip="10.0.0.250")
        files = generate_all(p, self.devices)
        self.assertIn("ztp/SN001.cfg", files)
        self.assertIn("ztp/default.cfg", files)
        self.assertIn("dnsmasq.conf", files)
        self.assertIn("dhcp-option=66,10.0.0.250", files["dnsmasq.conf"])
        self.assertIn("sysname Core-SW01", files["ztp/SN001.cfg"])
        self.assertNotIn("{p.http_root}", files["README.txt"])

    def test_huawei_midfile(self):
        p = ZtpProfile(vendor="huawei", server_ip="10.0.0.250")
        files = generate_all(p, self.devices)
        self.assertIn("ztp/ztp_intermediate.txt", files)
        self.assertIn('"ZTP file server" : "tftp://10.0.0.250"', files["ztp/ztp_intermediate.txt"])

    def test_cisco_options(self):
        p = ZtpProfile(vendor="cisco", server_ip="10.0.0.250")
        files = generate_all(p, self.devices)
        self.assertIn("dhcp-option=150,10.0.0.250", files["dnsmasq.conf"])
        self.assertIn("ztp/ztp_bootstrap.py", files)


if __name__ == "__main__":
    unittest.main()
