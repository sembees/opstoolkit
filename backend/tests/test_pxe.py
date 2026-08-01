"""PXE 生成器单元测试。"""
import unittest

from app.it.pxe.generator import PxeConfig, generate_all


class PxeGeneratorTest(unittest.TestCase):
    def test_per_mac_files_and_hostname(self):
        cfg = PxeConfig(
            os_type="ubuntu",
            os_version="22.04",
            hostname="base-server",
            admin_user="ops",
            admin_password="Test@123",
            server_ip="10.10.10.10",
            http_root="http://10.10.10.10:8000/pxe/serve",
            net_mode="dhcp",
            deploy_mode="standalone",
        )
        installs = [
            {"mac": "00:11:22:33:44:55", "hostname": "web-01", "ip": "10.10.10.100"},
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "db-01", "ip": "10.10.10.101"},
        ]
        files = generate_all(cfg, installs)
        self.assertIn("boot/00-11-22-33-44-55.ipxe", files)
        self.assertIn("boot/aa-bb-cc-dd-ee-ff.ipxe", files)
        self.assertIn("user-data/00-11-22-33-44-55/user-data", files)
        self.assertIn("hostname: web-01", files["user-data/00-11-22-33-44-55/user-data"])
        self.assertIn("- reboot", files["user-data/00-11-22-33-44-55/user-data"])
        self.assertIn("user-data/00-11-22-33-44-55/", files["boot/00-11-22-33-44-55.ipxe"])
        dns = files["dnsmasq.conf"]
        self.assertIn("dhcp-host=00:11:22:33:44:55,set:pxe_00-11-22-33-44-55", dns)
        self.assertIn("dhcp-boot=tag:pxe_00-11-22-33-44-55,http://10.10.10.10:8000/pxe/serve/boot/00-11-22-33-44-55.ipxe", dns)

    def test_rhel_kickstart(self):
        cfg = PxeConfig(os_type="rhel", os_version="9.3", hostname="rhel-01", admin_password="x")
        files = generate_all(cfg)
        self.assertIn("ks.cfg", files)
        self.assertIn("url --url=", files["ks.cfg"])
        self.assertIn("reboot", files["ks.cfg"])


if __name__ == "__main__":
    unittest.main()
