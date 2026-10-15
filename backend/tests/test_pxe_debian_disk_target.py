# -*- coding: utf-8 -*-
"""A1：Debian 也用**稳定属性选盘**（不再静默钉 sda），并与 RHEL/Ubuntu 同一套语义。

背景（真机复现过，§5.83.75）：Proxmox 两块盘挂在同一个 virtio-scsi 控制器的不同 LUN 上时，
客户机对 LUN 的枚举顺序可以与槽位号**相反** ⇒ profile 里写 `sda` 的那块盘，未必是运维想装的
那块。RHEL/Ubuntu 早有 `disk_config.target`（auto/name/match，用 serial/model/id_path 定位），
**只有 Debian 完全忽略它** —— 更糟的是：界面上选「自动（最大盘）」（mode=auto）时
disk_config 里没有 `disk` 键，`_debian_preseed` 就落到默认 **sda**，"界面说自动、产物写死 sda"。

本文件钉住修复后的产出形状；"shell 真的能跑对"由 mimo/ftest_debian_pick_disk.py 在真机上
（把破坏性命令全部换成桩）验收，两者互补：用例锁形状，真机锁行为。
"""
import importlib
import io
import re
import sys
import unittest

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

gen = importlib.import_module("app.it.pxe.generator")

PLAN_SENTINEL = "/dev/opstk-no-target-disk"


def cfg(dc, os_type="debian", ver="13.7.0"):
    return gen.PxeConfig(
        os_type=os_type, os_version=ver, hostname="h", admin_user="ops",
        admin_password="passw0rdX", ssh_keys=[], disk_scheme="lvm", disk_config=dc,
        net_mode="dhcp", net_config={}, mirror="http://h/repo/", server_ip="192.168.199.1",
        http_root="http://192.168.199.1:8000/pxe/serve", kernel_path="k", initrd_path="i",
        deploy_mode="standalone", iso_url="http://h/x.iso")


def preseed(dc, **kw):
    return gen.generate_all(cfg(dc, **kw), [])["preseed.cfg"]


def line_starting(text, prefix):
    hits = [l for l in text.split("\n") if l.startswith(prefix)]
    assert len(hits) == 1, ("期望恰好一行 %r，实际 %d 行" % (prefix, len(hits)), hits)
    return hits[0]


class PreseedValueMustStayOneLineTest(unittest.TestCase):
    """preseed 的值**不能有裸换行**（会把条目截断）—— 复合 shell 语句尤其容易踩。"""

    def test_early_command_is_a_single_logical_line(self):
        for dc in ({"target": {"mode": "auto"}},
                   {"target": {"mode": "match", "model": "QEMU HARDDISK"}},
                   {"target": {"mode": "name", "name": "sdb"}}):
            p = preseed(dc)
            ln = line_starting(p, "d-i partman/early_command string ")
            # 条目本身只有一行 ⇒ 上面取到的这行必须含整条命令的收尾（我们以 "; true" 结尾）
            self.assertTrue(ln.rstrip().endswith("true"), (dc, ln[-60:]))
            self.assertNotIn("\n", ln)


class DebianLegacyPathUnchangedTest(unittest.TestCase):
    """老模板（只给 {disk: 盘名}）必须**逐字节**走原路 —— 这是不许动的回归红线。"""

    def test_legacy_disk_only_uses_static_name_without_any_resolution(self):
        p = preseed({"disk": "sda"})
        self.assertIn("d-i partman-auto/disk string /dev/sda", p)
        self.assertIn("d-i grub-installer/bootdev string /dev/sda", p)
        ec = line_starting(p, "d-i partman/early_command string ")
        self.assertIn("wipefs -a /dev/sda", ec)
        # 老路径**不该**出现任何"现场选盘"的痕迹
        for marker in ("list-devices", "debconf-set", "opstk-no-target-disk", "PXE-DISK"):
            self.assertNotIn(marker, p, (marker, dc_hint))


dc_hint = {"disk": "sda"}


class DebianStructuredNameTest(unittest.TestCase):
    def test_mode_name_pins_the_named_device_everywhere(self):
        p = preseed({"target": {"mode": "name", "name": "sdb"}})
        self.assertIn("d-i partman-auto/disk string /dev/sdb", p)
        self.assertIn("d-i grub-installer/bootdev string /dev/sdb", p)
        ec = line_starting(p, "d-i partman/early_command string ")
        self.assertIn("wipefs -a /dev/sdb", ec)
        self.assertNotIn("list-devices", ec)      # name 模式不需要现场选盘
        self.assertNotIn("opstk-no-target-disk", p)


class DebianAutoAndMatchResolveAtInstallTimeTest(unittest.TestCase):
    """auto/match：静态值指向**哨兵**，真正的盘由 early_command 现场选出并 debconf-set。"""

    def test_auto_emits_sentinel_plus_resolution(self):
        p = preseed({"target": {"mode": "auto"}})
        # 静态值必须是哨兵 —— 否则选盘没跑成时会静默装到 sda（正是本缺陷的形状）
        self.assertIn("d-i partman-auto/disk string " + PLAN_SENTINEL, p)
        self.assertIn("d-i grub-installer/bootdev string " + PLAN_SENTINEL, p)
        ec = line_starting(p, "d-i partman/early_command string ")
        self.assertIn("list-devices disk", ec)
        self.assertIn('debconf-set partman-auto/disk "$_ot"', ec)
        self.assertIn('debconf-set grub-installer/bootdev "$_ot"', ec)
        self.assertIn("/sys/block/$_n/size", ec)          # 按容量挑，不用 lsblk
        self.assertNotIn("lsblk", ec)                     # d-i 的 initrd 不保证有 lsblk
        # 抹盘必须在**解析出来之后**、且先确认它真是块设备
        self.assertIn('[ -b "$_ot" ]', ec)
        self.assertLess(ec.index("_ot=''"), ec.index("[ -b \"$_ot\" ]"))
        # fail-closed：没选出来 ⇒ 落哨兵（不许回退到某个设备名）
        self.assertIn(PLAN_SENTINEL, ec)

    def test_auto_with_min_size_adds_the_threshold(self):
        ec = line_starting(preseed({"target": {"mode": "auto", "min_size_gb": 100}}),
                           "d-i partman/early_command string ")
        self.assertIn(str(100 * 1024 ** 3), ec)

    def test_match_without_id_path_matches_serial_or_model_case_insensitively(self):
        ec = line_starting(preseed({"target": {"mode": "match", "serial": "drive-scsi0"}}),
                           "d-i partman/early_command string ")
        self.assertIn("device/serial", ec)
        self.assertIn("device/model", ec)
        self.assertIn("tr 'A-Z' 'a-z'", ec)               # 大小写不敏感（与 RHEL %pre 等价）

    def test_match_with_id_path_prefers_udev_path(self):
        ec = line_starting(preseed({"target": {"mode": "match",
                                               "id_path": "pci-0000:00:05.0-scsi-0:0:0:1"}}),
                           "d-i partman/early_command string ")
        self.assertIn("/dev/disk/by-path/pci-0000:00:05.0-scsi-0:0:0:1", ec)
        self.assertIn("readlink -f", ec)

    def test_model_with_space_is_accepted(self):
        """`QEMU HARDDISK` 这种带空格的型号必须能用（RHEL 侧曾经因此直接报错）。"""
        ec = line_starting(preseed({"target": {"mode": "match", "model": "QEMU HARDDISK"}}),
                           "d-i partman/early_command string ")
        # Debian 侧比较前把空格去掉 ⇒ key 也去空格（留着也匹配不上，反而不一致）
        self.assertIn("QEMUHARDDISK", ec.replace(" ", ""))


class MatcherValueWhitelistTest(unittest.TestCase):
    """`_safe_matcher_value` 的白名单：放行空格后仍必须挡住注入字符。"""

    def test_space_allowed_only_when_asked(self):
        self.assertEqual("QEMU HARDDISK",
                         gen._safe_matcher_value("QEMU HARDDISK", "t", 0, allow_space=True))
        with self.assertRaises(ValueError):
            gen._safe_matcher_value("QEMU HARDDISK", "t", 0)

    def test_injection_characters_are_still_rejected(self):
        for bad in ("a;b", "a|b", "a&b", "a$b", "a`b`", "a'b", 'a"b', "a\\b", "a\nb",
                    "a  b", ""):
            with self.assertRaises(ValueError, msg=bad):
                gen._safe_matcher_value(bad, "t", 0, allow_space=True)

    def test_surrounding_whitespace_is_stripped_not_rejected(self):
        """首尾空白是先 strip 再校验（不是非法）—— 真实型号常从别处粘来带空格。"""
        self.assertEqual("lead", gen._safe_matcher_value(" lead ", "t", 0, allow_space=True))


class RhelMatchByModelRegressionTest(unittest.TestCase):
    """RHEL 侧同一个口径问题：用 model 匹配曾经**生成即失败**（空格被白名单拒）。"""

    def test_rhel_match_by_model_with_space_generates(self):
        ks = gen.generate_all(cfg({"target": {"mode": "match", "model": "QEMU HARDDISK"}},
                                  os_type="rhel", ver="9.3"), [])["ks.cfg"]
        self.assertIn("%pre", ks)
        self.assertIn("QEMU HARDDISK", ks)


if __name__ == "__main__":
    unittest.main()
