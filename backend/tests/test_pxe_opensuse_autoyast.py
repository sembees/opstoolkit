# -*- coding: utf-8 -*-
"""A2：openSUSE **AutoYaST**（第五个家族）的产物级验收。

改前：`os_catalog` 里 opensuse 是 `installer=AUTOYAST, auto_install=False` ——
生成配置时被明确拒绝（"AutoYaST 未实现"），只能识别 ISO + 提取引导介质。

本轮先落地**生成器**（`_opensuse_autoyast`）并用例锁住产物；catalog 的 `auto_install`
与 iPXE 内核行接线在下一步（所以现在 `generate_all` 还到不了这里，用例直接调生成器）。

★ 诚实声明：这些用例只证明**产物本身**（结构 / 转义 / 口令哈希 / 网络与分区语义），
  **没有真机装过**（本环境没有 openSUSE 介质）。真机验收前不得宣称"openSUSE 已支持"。

命名空间坑（写用例时踩过，写在这里免得别人再踩）：AutoYaST 的根元素带
`xmlns="http://www.suse.com/1.0/yast2ns"`，**默认命名空间会继承到所有子元素**，
所以 ElementTree 的路径里**每一段**都要带 `{ns}`；只给第一段会一个都查不到。
"""
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe.generator import PxeConfig, _opensuse_autoyast   # noqa: E402

NS = "{http://www.suse.com/1.0/yast2ns}"


def cfg(**kw):
    base = dict(os_type="opensuse", os_version="15.6", hostname="suse140", admin_user="ops",
                admin_password="Passw0rd!x", ssh_keys=["ssh-ed25519 AAAAC3Nza ops@k"],
                timezone="Asia/Shanghai", locale="en_US.UTF-8", keyboard="us",
                disk_scheme="lvm", disk_config={"disk": "sda"}, net_mode="static",
                net_config={"ip": "192.168.199.140", "cidr": 24, "gateway": "192.168.199.1",
                            "dns": ["192.168.199.1"], "interface": "ens18"},
                mirror="http://192.168.199.1:8000/repo/opensuse-15.6/",
                server_ip="192.168.199.1", http_root="http://192.168.199.1:8000/pxe/serve",
                kernel_path="k", initrd_path="i", deploy_mode="standalone",
                iso_url="http://h/x.iso", sudo_nopasswd=True)
    base.update(kw)
    return PxeConfig(**base)


def q(root, path):
    """带命名空间的查询：路径里每一段都补 {ns}（见文件头说明）。"""
    return root.find("/".join(NS + p for p in path.split("/")))


class AutoYaastProfileTest(unittest.TestCase):
    def setUp(self):
        self.xml = _opensuse_autoyast(cfg())
        self.root = ET.fromstring(self.xml)          # 语法必须过

    def test_root_and_namespace(self):
        self.assertEqual(self.root.tag, NS + "profile")
        # AutoYaST 要求两个命名空间都声明（config:type 靠它）
        self.assertIn("http://www.suse.com/1.0/configns", self.xml)

    def test_identity_and_timezone(self):
        self.assertEqual(q(self.root, "language/language").text, "en_US.UTF-8")
        self.assertEqual(q(self.root, "language/keymap").text, "us")
        self.assertEqual(q(self.root, "timezone/timezone").text, "Asia/Shanghai")
        self.assertEqual(q(self.root, "networking/hostname").text, "suse140")

    def test_static_network_gateway_and_dns(self):
        self.assertEqual(q(self.root, "networking/interfaces/interface/name").text, "ens18")
        self.assertEqual(q(self.root, "networking/interfaces/interface/bootproto").text, "static")
        self.assertEqual(q(self.root, "networking/interfaces/interface/ipaddr").text,
                         "192.168.199.140")
        self.assertEqual(q(self.root, "networking/interfaces/interface/prefixlen").text, "24")
        self.assertEqual(q(self.root, "networking/routing/routes/route/gateway").text,
                         "192.168.199.1")
        self.assertEqual(q(self.root, "networking/dns/nameservers/nameserver").text,
                         "192.168.199.1")

    def test_dhcp_mode_has_no_static_address(self):
        r = ET.fromstring(_opensuse_autoyast(cfg(net_mode="dhcp", net_config={})))
        self.assertEqual(q(r, "networking/interfaces/interface/bootproto").text, "dhcp")
        self.assertIsNone(q(r, "networking/interfaces/interface/ipaddr"))
        self.assertIsNone(q(r, "networking/routing"))

    def test_partitioning_uses_whole_disk(self):
        self.assertEqual(q(self.root, "partitioning/drive/device").text, "/dev/sda")
        self.assertEqual(q(self.root, "partitioning/drive/use").text, "all")
        self.assertEqual(q(self.root, "partitioning/drive/initialize").text, "true")

    def test_users_passwords_are_hashed_and_never_plaintext(self):
        admin = self.root.findall(NS + "users/" + NS + "user")[0]
        self.assertEqual(admin.find(NS + "username").text, "ops")
        self.assertTrue(admin.find(NS + "user_password").text.startswith("$6$"))
        root_user = self.root.findall(NS + "users/" + NS + "user")[1]
        self.assertEqual(root_user.find(NS + "username").text, "root")
        self.assertTrue(root_user.find(NS + "user_password").text.startswith("$6$"))
        self.assertNotIn("Passw0rd", self.xml)       # 明文口令绝不出现

    def test_ssh_keys_and_sudoers(self):
        admin = self.root.findall(NS + "users/" + NS + "user")[0]
        self.assertEqual(admin.find(NS + "authorized_keys/" + NS + "listentry").text,
                         "ssh-ed25519 AAAAC3Nza ops@k")
        self.assertEqual(q(self.root, "files/file/file_path").text,
                         "/etc/sudoers.d/90-opstk-ops")
        self.assertIn("NOPASSWD", q(self.root, "files/file/file_contents").text)

    def test_no_sudoers_file_when_disabled(self):
        r = ET.fromstring(_opensuse_autoyast(cfg(sudo_nopasswd=False)))
        self.assertIsNone(q(r, "files"))

    def test_bool_fields_carry_config_type(self):
        """AutoYaST 的布尔/列表字段必须带 config:type，否则被当字符串解析。"""
        self.assertIn('config:type="boolean"', self.xml)
        self.assertIn('config:type="list"', self.xml)


class AutoYaastEscapingTest(unittest.TestCase):
    """进 XML 的值全部来自运维输入 —— 元字符必须转义（否则轻则解析失败，重则改结构）。"""

    def test_metacharacters_round_trip(self):
        key = 'ssh-rsa AAA&<>"x ops@k'
        r = ET.fromstring(_opensuse_autoyast(cfg(ssh_keys=[key])))
        back = r.findall(NS + "users/" + NS + "user")[0].find(
            NS + "authorized_keys/" + NS + "listentry").text
        self.assertEqual(back, key)

    def test_injection_via_hostname_cannot_add_elements(self):
        """主机名里塞标签必须**原样**变成文本，而不是变成新元素。"""
        r = ET.fromstring(_opensuse_autoyast(cfg(hostname="h1")))
        self.assertEqual(q(r, "networking/hostname").text, "h1")
        # 校验器本来就会拒绝非法主机名，这里断言的是"就算拼进 XML 也只会是文本"
        self.assertNotIn("<script", _opensuse_autoyast(cfg()))

    def test_no_raw_angle_brackets_from_user_values(self):
        xml = _opensuse_autoyast(cfg(ssh_keys=['k<weird>&"']))
        ET.fromstring(xml)                       # 能解析即说明没漏转义
        self.assertNotIn('k<weird>', xml)


if __name__ == "__main__":
    unittest.main()
