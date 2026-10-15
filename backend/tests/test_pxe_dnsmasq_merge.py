# -*- coding: utf-8 -*-
"""多模板 dnsmasq 合并的用例（2026-10-15 用户报障："部署 B 模板会把 A 的 dhcp-host 冲掉"）。

背景：`/etc/dnsmasq.d/opstk-pxe.conf` 是**单文件**，每次部署整体覆盖。于是
"A 模板有两台待装机、随后部署 B 模板"会把 A 的 `dhcp-host` 地址预留与每机引导菜单
一起冲掉 —— A 的机器 IP 会漂、也拿不到自己的装机菜单。

修法：生成时把**其它已部署模板**（`last-deploy.json` 里记着的）的按 MAC 条目合并进来；
被合并的机器必须指向**它们自己模板**的 `profiles/<pid>/boot/<mac>.ipxe`。
全局段（interface / DHCP 池 / 引导骨架）仍只由本次部署的模板提供 —— 一个 dnsmasq
实例只能服务一个装机网段。

本文件钉：
  1. 单模板输出**逐字节不变**（extra=None 时走原路径）；
  2. 合并后两边的 dhcp-host 都在，且各自指向自己的菜单前缀；
  3. `fw-menu-def` 的取反覆盖**所有**待装机 MAC（否则合并进来的机器会掉进默认菜单）；
  4. 同一 MAC 出现在两个模板 ⇒ 去重、主模板优先（dnsmasq 对同一 MAC 两条 dhcp-host
     只有一条生效且顺序未定义，不能听天由命）；
  5. 已标记完成的机器：dhcp-host 照发（IP 不漂），但不发装机菜单；
  6. 扁平 boot.ipxe 的全机口径：只要本机（含合并进来的模板）有过装机记录，就必须是
     "拒绝自动安装"的安全菜单（§5.29 那条实测事故）；
  7. `_collect_deployed_extras`：跳过自身/已删除/非法 pid；answer_root 用**它自己的**。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe.generator import (  # noqa: E402
    UNREGISTERED_DEFAULT_MARK, PxeConfig, _unregistered_default_menu, generate_all,
)

BASE = dict(
    os_type="debian", os_version="13.7.0", hostname="h", admin_user="ops",
    admin_password="x", ssh_keys=[], disk_scheme="lvm", disk_config={"disk": "sda"},
    net_mode="dhcp", net_config={}, mirror="http://h/repo/",
    server_ip="192.168.199.1", http_root="http://192.168.199.1:8000/pxe/serve",
    kernel_path="k", initrd_path="i", deploy_mode="standalone",
)

MAC_A, MAC_B = "BC:24:11:66:8B:EA", "BC:24:11:2C:BE:A0"
TAG_A, TAG_B = "bc-24-11-66-8b-ea", "bc-24-11-2c-be-a0"
ROOT = "http://192.168.199.1:8000/pxe/serve"


def cfg(**over):
    kw = dict(BASE)
    kw.update(over)
    return PxeConfig(**kw)


def inst(mac, ip, status="pending", fin=None):
    return {"mac": mac, "ip": ip, "hostname": "h", "status": status, "finished_at": fin}


A_INST = [inst(MAC_A, "192.168.199.140")]
B_INST = [inst(MAC_B, "192.168.199.141")]


def conf_of(**kw):
    return generate_all(**kw)["dnsmasq.conf"]


def lines(conf, prefix):
    return [l for l in conf.splitlines() if l.startswith(prefix)]


class TestSingleTemplateUnchanged(unittest.TestCase):
    """回归红线：没有别的模板时，输出必须与改造前逐字节一致。"""

    def test_extra_none_or_empty_is_identical(self):
        a = conf_of(c=cfg(), installs=B_INST)
        b = conf_of(c=cfg(), installs=B_INST, extra=None)
        c = conf_of(c=cfg(), installs=B_INST, extra=[])
        self.assertEqual(a, b)
        self.assertEqual(a, c)

    def test_registered_anywhere_none_keeps_legacy_default_menu(self):
        """单模板 + 无记录 ⇒ 默认菜单仍是本模板的装机菜单（既有行为，不能破）。"""
        files = generate_all(cfg(), [])
        self.assertNotIn(UNREGISTERED_DEFAULT_MARK, files["boot.ipxe"])
        # 单模板 + 有记录 ⇒ 安全菜单
        files2 = generate_all(cfg(), B_INST)
        self.assertIn(UNREGISTERED_DEFAULT_MARK, files2["boot.ipxe"])


class TestMerge(unittest.TestCase):
    def _merged(self):
        cb = cfg(hostname="b", net_config={"dhcp_start": "192.168.199.100",
                                          "dhcp_end": "192.168.199.200"},
                 answer_root=ROOT + "/profiles/bbbb")
        # 真机里 answer_root = <http_root>/profiles/<pid>，两个模板前缀不同
        ca = cfg(hostname="a", net_config={"dhcp_start": "192.168.199.100",
                                          "dhcp_end": "192.168.199.200"},
                 answer_root=ROOT + "/profiles/aaaa")
        return generate_all(cb, B_INST, extra=[{"c": ca, "installs": A_INST}],
                            registered_anywhere=True)["dnsmasq.conf"]

    def test_both_profiles_dhcp_host_present(self):
        conf = self._merged()
        hosts = lines(conf, "dhcp-host=")
        self.assertIn("dhcp-host=" + MAC_A.lower() + ",192.168.199.140,set:pxe_" + TAG_A, hosts)
        self.assertIn("dhcp-host=" + MAC_B.lower() + ",192.168.199.141,set:pxe_" + TAG_B, hosts)
        self.assertEqual(len(hosts), 2)

    def test_each_machine_points_at_its_own_menu(self):
        """★ 关键：合并进来的机器必须指向**它自己模板**的 boot/<mac>.ipxe。"""
        conf = self._merged()
        boots = lines(conf, "dhcp-boot=tag:fw-menu-" + TAG_A)
        self.assertEqual(len(boots), 1, boots)
        self.assertIn("/profiles/aaaa/boot/" + TAG_A + ".ipxe", boots[0])
        boots_b = lines(conf, "dhcp-boot=tag:fw-menu-" + TAG_B)
        self.assertEqual(len(boots_b), 1, boots_b)
        self.assertIn("/profiles/bbbb/boot/" + TAG_B + ".ipxe", boots_b[0])

    def test_default_menu_negates_all_pending_macs(self):
        """默认菜单必须对**两边**的待装机 MAC 取反，否则合并进来的机器会掉进默认菜单。"""
        conf = self._merged()
        defline = lines(conf, "tag-if=set:fw-menu-def,")
        self.assertEqual(len(defline), 1, defline)
        self.assertIn("tag:!pxe_" + TAG_A, defline[0])
        self.assertIn("tag:!pxe_" + TAG_B, defline[0])

    def test_duplicate_mac_deduped_primary_wins(self):
        """同一 MAC 出现在两个模板：去重，主模板优先（顺序不能听天由命）。"""
        ca = cfg(hostname="a", answer_root=ROOT + "/profiles/aaaa")
        cb = cfg(hostname="b", answer_root=ROOT + "/profiles/bbbb")
        conf = generate_all(cb, [inst(MAC_A, "192.168.199.141", status="pending", fin="X")],
                            extra=[{"c": ca, "installs": A_INST}],
                            registered_anywhere=True)["dnsmasq.conf"]
        hosts = lines(conf, "dhcp-host=")
        self.assertEqual(len(hosts), 1, hosts)
        # 主模板的记录是"已完成"，所以这条 MAC 不发装机菜单（见下条用例的同一条规则）
        self.assertEqual(hosts[0], "dhcp-host=" + MAC_A.lower() + ",192.168.199.141,set:pxe_" + TAG_A)

    def test_completed_machine_keeps_reservation_without_menu(self):
        ca = cfg(hostname="a", answer_root=ROOT + "/profiles/aaaa")
        cb = cfg(hostname="b", answer_root=ROOT + "/profiles/bbbb")
        conf = generate_all(cb, B_INST,
                            extra=[{"c": ca, "installs": [inst(MAC_A, "192.168.199.140",
                                                               status="installed", fin="done")]}],
                            registered_anywhere=True)["dnsmasq.conf"]
        self.assertEqual(len(lines(conf, "dhcp-host=")), 2, "已完成的机器地址预留必须保留")
        self.assertEqual(lines(conf, "dhcp-boot=tag:fw-menu-" + TAG_A), [], "已完成的机器不发装机菜单")
        self.assertEqual(len(lines(conf, "dhcp-boot=tag:fw-menu-" + TAG_B)), 1)

    def test_global_section_comes_from_primary_only(self):
        """全局段只由主模板给：一份 dnsmasq 只能服务一个装机网段。"""
        ca = cfg(hostname="a", server_ip="10.9.9.9", answer_root=ROOT + "/profiles/aaaa")
        cb = cfg(hostname="b", answer_root=ROOT + "/profiles/bbbb")
        conf = generate_all(cb, B_INST, extra=[{"c": ca, "installs": A_INST}],
                            registered_anywhere=True)["dnsmasq.conf"]
        self.assertEqual(len(lines(conf, "dhcp-range=")), 1)
        self.assertIn("192.168.199.", lines(conf, "dhcp-range=")[0])
        self.assertNotIn("10.9.9.9", conf, "被合并模板的服务端地址不得混进全局段")
        self.assertEqual(len(lines(conf, "interface=")), 1)


class TestCollectDeployedExtras(unittest.IsolatedAsyncioTestCase):
    """`_collect_deployed_extras`：从 last-deploy.json + DB 取其它已部署模板。"""

    def _db(self, profiles, installs_by_pid):
        db = mock.AsyncMock()

        async def _get(model, pid):
            return profiles.get(pid)

        async def _execute(_stmt):
            # 返回"该次查询对应模板"的记录：调用方顺序是逐个 pid 查的，这里按调用次数发牌
            rows = installs_by_pid[_execute.calls]
            _execute.calls += 1
            res = mock.MagicMock()
            res.scalars.return_value.all.return_value = rows
            return res

        _execute.calls = 0
        db.get = mock.AsyncMock(side_effect=_get)
        db.execute = mock.AsyncMock(side_effect=_execute)
        return db

    async def test_skips_self_deleted_and_illegal(self):
        from app.api import pxe as api_pxe
        from app.core import models
        prof_ok = models.PxeProfile(id="a" * 32, name="A", os_type="debian", os_version="13",
                                    admin_user="ops", ssh_keys=[], disk_scheme="lvm",
                                    disk_config={}, net_mode="dhcp", net_config={},
                                    mirror="", extra_packages=[], post_script="")
        row = mock.MagicMock(mac=MAC_A, hostname="a140", ip="192.168.199.140",
                             status="pending", finished_at=None)
        db = self._db({"a" * 32: prof_ok}, [[row]])
        with mock.patch.object(api_pxe, "_read_last_deploy",
                               lambda: {"a" * 32: {"deploy_mode": "standalone"},
                                        "e" * 32: {},              # 已删除的模板
                                        "../bad": {}}):            # 非法 pid
            extra, names = await api_pxe._collect_deployed_extras(db, "b" * 32, "192.168.199.1", "standalone")
        self.assertEqual(len(extra), 1, extra)
        self.assertEqual(extra[0]["installs"][0]["mac"], MAC_A)
        self.assertTrue(names and "A" in names[0])

    async def test_excludes_current_profile(self):
        from app.api import pxe as api_pxe
        from app.core import models
        prof = models.PxeProfile(id="a" * 32, name="A", os_type="debian", os_version="13",
                                 admin_user="ops", ssh_keys=[], disk_scheme="lvm",
                                 disk_config={}, net_mode="dhcp", net_config={},
                                 mirror="", extra_packages=[], post_script="")
        db = self._db({"a" * 32: prof}, [[]])
        with mock.patch.object(api_pxe, "_read_last_deploy", lambda: {"a" * 32: {}}):
            extra, names = await api_pxe._collect_deployed_extras(db, "a" * 32, "192.168.199.1", "standalone")
        self.assertEqual(extra, [])
        self.assertEqual(names, [])

    async def test_profile_without_records_contributes_nothing(self):
        from app.api import pxe as api_pxe
        from app.core import models
        prof = models.PxeProfile(id="a" * 32, name="A", os_type="debian", os_version="13",
                                 admin_user="ops", ssh_keys=[], disk_scheme="lvm",
                                 disk_config={}, net_mode="dhcp", net_config={},
                                 mirror="", extra_packages=[], post_script="")
        db = self._db({"a" * 32: prof}, [[]])
        with mock.patch.object(api_pxe, "_read_last_deploy", lambda: {"a" * 32: {}}):
            extra, _names = await api_pxe._collect_deployed_extras(db, "b" * 32, "192.168.199.1", "standalone")
        self.assertEqual(extra, [], "没有装机记录的模板不该贡献任何按 MAC 条目")


class TestDefaultMenuIsTemplateIndependent(unittest.TestCase):
    def test_bytes_identical_for_any_template(self):
        self.assertEqual(_unregistered_default_menu(), _unregistered_default_menu())
        self.assertIn(UNREGISTERED_DEFAULT_MARK, _unregistered_default_menu())
