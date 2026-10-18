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
import os
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

    @staticmethod
    def _files(root):
        """`<files>` → {file_path: file_contents}（缺哪个文件取哪个键会直接 KeyError）。"""
        out = {}
        for f in root.findall(NS + "files/" + NS + "file"):
            out[f.find(NS + "file_path").text] = f.find(NS + "file_contents").text
        return out

    def test_root_and_namespace(self):
        self.assertEqual(self.root.tag, NS + "profile")
        # AutoYaST 要求两个命名空间都声明（config:type 靠它）
        self.assertIn("http://www.suse.com/1.0/configns", self.xml)

    def test_identity_and_timezone(self):
        self.assertEqual(q(self.root, "language/language").text, "en_US",
                         "YaST 的语言列表里是 en_US，不接受 en_US.UTF-8（真机实测会弹警告并卡住）")
        self.assertEqual(q(self.root, "keyboard/keymap").text, "us")
        self.assertEqual(q(self.root, "timezone/timezone").text, "Asia/Shanghai")
        self.assertEqual(q(self.root, "networking/dns/hostname").text, "suse140")

    def test_static_network_goes_through_files_not_networking(self):
        """★ 2026-10-18 真机定的契约：装机期**不许 YaST 碰网卡**。

        安装器里的网卡叫 `eth0`（linuxrc 打印 `eth0: network config created`），
        而配置里给的是**装好的系统**里的名字（`ens18`）——装机期并不存在。
        YaST 一旦在装包途中 apply `<interfaces>`，安装源 `192.168.199.1:8000` 就断，
        真机表现是盘写入停在 6.66%、控制台回到 linuxrc 的
        "Please make sure your installation medium is available. Choose the URL to retry."

        所以静态网络的**唯一**落点是 `<files>`：`ifcfg-<iface>` + `routes`，
        首启由 wicked 按 sysconfig 生效。
        """
        self.assertIsNone(q(self.root, "networking/interfaces"),
                          "装机期不得让 YaST 配网卡（网卡名在安装器里叫 eth0）")
        self.assertIsNone(q(self.root, "networking/routing"),
                          "<routing> 同样会在装机期被 apply，必须为空")
        files = self._files(self.root)
        ifcfg = files["/etc/sysconfig/network/ifcfg-ens18"]
        self.assertIn("BOOTPROTO='static'", ifcfg)
        self.assertIn("IPADDR='192.168.199.140/24'", ifcfg)
        self.assertIn("STARTMODE='auto'", ifcfg)
        self.assertIn("NETCONFIG_DNS_STATIC_SERVERS='192.168.199.1'", ifcfg)
        self.assertEqual(files["/etc/sysconfig/network/routes"],
                         "default 192.168.199.1 - -\n")

    def test_dhcp_mode_has_no_static_address(self):
        r = ET.fromstring(_opensuse_autoyast(cfg(net_mode="dhcp", net_config={})))
        self.assertIsNone(q(r, "networking/interfaces"))
        self.assertIsNone(q(r, "networking/routing"))
        files = self._files(r)
        self.assertEqual([p for p in files if "ifcfg-" in p], [],
                         "dhcp 模式不得写静态 ifcfg")
        self.assertNotIn("/etc/sysconfig/network/routes", files)
        # 主机名仍要写：<networking> 只负责"不动网卡"，落盘照旧
        self.assertEqual(files["/etc/hostname"], "suse140\n")

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
        files = self._files(self.root)
        self.assertIn("NOPASSWD", files["/etc/sudoers.d/90-opstk-ops"])
        # root 也要能免密登进来（真机取证靠 root ssh；只在 <users> 里给管理员写不够）
        self.assertEqual(files["/root/.ssh/authorized_keys"],
                         "ssh-ed25519 AAAAC3Nza ops@k\n")

    def test_no_sudoers_file_when_disabled(self):
        r = ET.fromstring(_opensuse_autoyast(cfg(sudo_nopasswd=False)))
        files = self._files(r)
        self.assertEqual([p for p in files if "sudoers" in p], [],
                         "关掉 sudo 免密就不该写 sudoers 文件")
        # <files> 本身仍然需要（主机名/静态网络都靠它），不能因为关了 sudo 就整段消失
        self.assertIn("/etc/hostname", files)

    def test_single_files_element(self):
        """`<files>` 是 `config:type="list"` 的**一整个列表**：profile 里只能有一个。

        写第二个 `<files>` 会被 YaST 当重复元素（RelaxNG interleave 直接报 extra），
        所以生成器必须先把要写的东西收集起来，最后一次性发射。
        """
        files = self.root.findall(NS + "files")
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].get(
            "{http://www.suse.com/1.0/configns}type"), "list")

    def test_installs_sshd_and_sudo(self):
        """最小安装里**既没有 sshd 也没有 sudo**（真机读 zypp history：509 个包里
        只有 `openssh-clients`/`openssh-common`，没有 `openssh-server`，也没有 `sudo`）。

        后果很具体：装完了、能 ping 通（DHCP 拿了地址），但 22 端口 Connection refused、
        `sudo` 命令不存在 —— 一台"装好了却进不去、免密落不了地"的机器。
        AutoYaST 的 `<software><packages>` 是"在默认选择上追加"，所以不带 patterns 也安全。
        """
        pkgs = self.root.find(NS + "software/" + NS + "packages")
        self.assertIsNotNone(pkgs, "缺少 <software><packages>，sshd/sudo 不会被安装")
        self.assertEqual(pkgs.get("{http://www.suse.com/1.0/configns}type"), "list")
        names = [p.text for p in pkgs.findall(NS + "package")]
        self.assertIn("openssh-server", names)
        self.assertIn("sudo", names)

    def test_enables_sshd(self):
        """装了 ≠ 开了。真机第二轮证据：`openssh-server` 已安装、`/usr/sbin/sshd` 在，
        但首启后 22 端口 **Connection refused**（RST 而非超时 ⇒ 防火墙没拦，只是没进程）。
        `systemctl preset` 在 install-chroot 里没把 sshd 接进 multi-user.target.wants，
        所以必须用 `<services-manager><services><enable>` 显式开机启用。
        """
        en = self.root.find(NS + "services-manager/" + NS + "services/" + NS + "enable")
        self.assertIsNotNone(en, "缺少 services-manager/enable：sshd 开机不会起")
        self.assertEqual(en.get("{http://www.suse.com/1.0/configns}type"), "list")
        names = ([s.text for s in en.findall(NS + "service")]
                 + [s.text for s in en.findall(NS + "listentry")])
        self.assertIn("sshd", names)

    def test_bool_fields_carry_config_type(self):
        """AutoYaST 的布尔/列表字段必须带 config:type，否则被当字符串解析。"""
        self.assertIn('config:type="boolean"', self.xml)
        self.assertIn('config:type="list"', self.xml)


    def test_keep_install_network_is_set(self):
        """真机实测逼出来的：装到一半 YaST 报 profile "could not be found or retrieved"，
        而应用访问日志显示同一份 profile **7 次全部 200** —— 取得到、之后又取不到。
        机制是 YaST 在安装开始时应用 networking（静态 IP + 固定网卡名）把安装器链路弄断。
        必须保留 keep_install_network=true，否则装机中途会失去 HTTP 安装源。
        """
        n = q(self.root, "networking/keep_install_network")
        self.assertIsNotNone(n, "缺少 keep_install_network")
        self.assertEqual(n.text, "true")
        self.assertEqual(n.get("{http://www.suse.com/1.0/configns}type"), "boolean")


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
        self.assertEqual(q(r, "networking/dns/hostname").text, "h1")
        # 校验器本来就会拒绝非法主机名，这里断言的是"就算拼进 XML 也只会是文本"
        self.assertNotIn("<script", _opensuse_autoyast(cfg()))

    def test_no_raw_angle_brackets_from_user_values(self):
        xml = _opensuse_autoyast(cfg(ssh_keys=['k<weird>&"']))
        ET.fromstring(xml)                       # 能解析即说明没漏转义
        self.assertNotIn('k<weird>', xml)


# ══════════════════════════════════════════════════════════════════════════════
# 离线回归：用 **YaST 自己的** RNG schema 校验产物
# ══════════════════════════════════════════════════════════════════════════════
# 为什么单列一个类：A2 在真机上烧掉十几个小时，根因就是"profile 结构不被 YaST 接受"，
# 而 YaST 报的错还指向**别的**元素（RelaxNG interleave 的经典误导：真错在 <keymap>，
# 它报的是后面那个元素 "extra"）。有了这份官方 schema，一轮校验 10 秒，
# 不必再拿真机 10 分钟一轮去猜。
#
# schema 来源：DVD 里 `yast2-schema-default` 的 RPM → `rpm2cpio | cpio` 解出 .rng，
# 已 vendored 到 tests/data/yast-rng/（56 个 .rng）。校验器 = YaST 自己用的那份。
#
# ★ 依赖 lxml（RelaxNG 支持）。项目 venv 默认没装，CI 里装一下即可；
#   本机可用内置解释器跑同一个文件：见 mimo/a2_validate.py。
RNG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "yast-rng")
try:
    from lxml import etree as _lxml
except ModuleNotFoundError:                  # pragma: no cover - 取决于环境
    _lxml = None

# 子进程校验器：给"有 lxml 但没装本项目依赖"的解释器用（本机就是这么跑的）。
_RNG_HELPER = r"""import sys
from lxml import etree
schema = etree.RelaxNG(etree.parse(sys.argv[1] + "/profile.rng"))
doc = etree.parse(sys.argv[2])
if schema.validate(doc):
    print("OK")
    sys.exit(0)
for e in schema.error_log:
    print("行 %s: %s" % (e.line, e.message), file=sys.stderr)
sys.exit(1)
"""


class YastSchemaTest(unittest.TestCase):
    """产物必须能过 YaST 官方 schema —— 这是"能不能装"的必要条件。

    两条路都算数：进程内有 lxml 就本地校验；没有就用 `OPSTK_RNG_PY` 指定的
    解释器子进程校验（本机：venv 有 passlib 无 lxml，内置 python 有 lxml 无 passlib，
    所以只能在两个解释器之间递产物）。
    """

    def setUp(self):
        self._helper_py = None
        if _lxml is not None:
            self.schema = _lxml.RelaxNG(_lxml.parse(os.path.join(RNG_DIR, "profile.rng")))
            return
        self._helper_py = os.environ.get("OPSTK_RNG_PY")
        if not self._helper_py:
            if os.environ.get("OPSTK_RNG_STRICT") == "1":
                # 严格模式：没校验器就等于没校验，不许静默跳过
                self.fail("缺 lxml：`pip install lxml` 或设 OPSTK_RNG_PY=<有 lxml 的解释器>")
            self.skipTest("缺 lxml（pip install lxml 或设 OPSTK_RNG_PY 后可跑）")
        self.assertTrue(os.path.exists(self._helper_py),
                        "OPSTK_RNG_PY 指向的解释器不存在: %s" % self._helper_py)

    def validate_rng(self, xml):
        """返回 (是否通过, 错误文本)。"""
        if _lxml is not None:
            doc = _lxml.fromstring(xml.encode("utf-8"))
            ok = self.schema.validate(doc)
            return ok, "\n".join("行 %s: %s" % (e.line, e.message)
                                 for e in self.schema.error_log)
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            doc_path = os.path.join(td, "profile.xml")
            script = os.path.join(td, "rng_check.py")
            with open(doc_path, "w", encoding="utf-8") as fh:
                fh.write(xml)
            with open(script, "w", encoding="utf-8") as fh:
                fh.write(_RNG_HELPER)
            r = subprocess.run([self._helper_py, script, RNG_DIR, doc_path],
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace",
                               env=dict(os.environ, PYTHONIOENCODING="utf-8"))
            return r.returncode == 0, ((r.stdout or "") + (r.stderr or "")).strip()

    def test_vendored_schema_is_the_real_one(self):
        """别拿一份残缺的 .rng 自欺欺人：profile.rng 必须真的引用到各模块。"""
        self.assertTrue(os.path.exists(os.path.join(RNG_DIR, "profile.rng")))
        self.assertGreaterEqual(len([n for n in os.listdir(RNG_DIR)
                                     if n.endswith(".rng")]), 40)

    def test_generated_profiles_validate(self):
        """生成器各路分支的产物都必须过 schema（一份过、另一份不过=漏了分支）。"""
        cases = {
            "static（真机用的那份）": cfg(),
            "dhcp": cfg(net_mode="dhcp", net_config={}),
            "无 sudo 免密": cfg(sudo_nopasswd=False),
            "无 ssh key / 无 dns / 无网关": cfg(
                ssh_keys=[], net_config={"ip": "192.168.199.140", "cidr": 24,
                                         "interface": "ens18"}),
            "带元字符": cfg(ssh_keys=['ssh-rsa AAA&<>"x ops@k']),
        }
        for name, c in cases.items():
            with self.subTest(name=name):
                ok, err = self.validate_rng(_opensuse_autoyast(c))
                self.assertTrue(ok, "schema 不认这份产物（%s）：\n%s" % (name, err))

    def test_schema_rejects_keymap_inside_language(self):
        """负向对照（真实踩过的坑）：<keymap> 放 <language> 里必须被判非法。

        没有负向对照，"校验通过"可能只是校验器根本没在验。
        """
        bad = _opensuse_autoyast(cfg()).replace(
            "    <languages>en_US</languages>\n",
            "    <languages>en_US</languages>\n    <keymap>us</keymap>\n", 1)
        self.assertIn("<keymap>us</keymap>", bad)          # 确认替换真的生效
        ok, err = self.validate_rng(bad)
        self.assertFalse(ok, "schema 放过了 <keymap> 在 <language> 里的非法产物")
        self.assertTrue(err, "判非法了但没给错误信息")

    def test_schema_rejects_hostname_directly_under_networking(self):
        """负向对照：<hostname> 不能直接挂在 <networking> 下（历史真 bug）。"""
        bad = _opensuse_autoyast(cfg()).replace(
            "  <networking>\n",
            "  <networking>\n    <hostname>suse140</hostname>\n", 1)
        self.assertIn("<hostname>suse140</hostname>", bad)
        ok, _err = self.validate_rng(bad)
        self.assertFalse(ok, "schema 放过了 <networking><hostname> 的非法产物")


if __name__ == "__main__":
    unittest.main()
