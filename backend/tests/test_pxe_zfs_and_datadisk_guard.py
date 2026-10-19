# -*- coding: utf-8 -*-
"""ZFS 磁盘布局（2026-10-19 起：**仅 Ubuntu**）与 A4 数据盘护栏。

## ZFS 的语义（本文件锁住的行为）

subiquity 原生支持 `storage.layout.name = zfs`，且 `match` 的写法与 lvm/direct **完全同构**
（`_ubuntu_storage_obj` 对 layout 是数据驱动的），所以 Ubuntu(autoinstall) 这条路径放行：

| target.mode | 生成的 storage |
|---|---|
| auto  | `{"layout": {"name": "zfs", "match": {"size": "largest"}}}` |
| name  | `{"layout": {"name": "zfs", "match": {"path": "/dev/<盘名>"}}}` |
| match | `{"layout": {"name": "zfs", "match": {"serial"/"model": …}}}`（id_path 仍拒绝） |

其余安装器**没有原生 ZFS root**，必须继续拒绝（静默按 LVM 生成 = "配置写 zfs、装出来是
LVM"，产物上看不出问题 —— 这正是 A6 要消除的偏差）：
  · RHEL 系（anaconda/kickstart）：明确说"RHEL 系没有原生 ZFS root（anaconda 不支持）"；
  · Debian（preseed/partman）：partman-auto 没有 zfs root 方案；
  · schema 保存期：`disk_scheme=zfs` 与 `disk_config.layout=zfs` 都按 os_type 分岔，
    非 Ubuntu 一律 422 且点名字段。

## A4 的实情（**与我原先的预期不同，如实记**）

RUNBOOK 里那条"data_disks 与目标盘的一致性校验仍未做"**其实已经被两道既有机制关掉了**：
1. `name` 模式 + `wipe=true` 数据盘：**生成期就拒绝**（"name 模式没有 %pre，无法现场解析设备名"）；
2. `auto`/`match` 模式：数据盘**进不了目标盘候选集**（awk 里 `if (dmatched(L)) next`）。

本轮另外补了一道**运行时**比对（%pre 里比较 `$ddevN` 与 `$target`，相同即中止）作为纵深防御 ——
真机桩测（mimo/ftest_a4_target_vs_datadisk.py）证明：它在当前产物里**够不到**（更早的闸先拦下），
但逻辑正确、正常拓扑不误伤。本文件钉住"生成期拒绝"与"%pre 里有这道后备"两件事。
"""
import sys
import unittest

import yaml
from pydantic import ValidationError

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import PxeProfileIn                      # noqa: E402
from app.it.pxe.generator import PxeConfig, generate_all       # noqa: E402


def cfg(os_type="rhel", ver="9.3", dc=None, scheme="lvm"):
    return PxeConfig(
        os_type=os_type, os_version=ver, hostname="h", admin_user="ops",
        admin_password="passw0rdX", ssh_keys=[], disk_scheme=scheme,
        disk_config=dc if dc is not None else {"disk": "sda"},
        net_mode="dhcp", net_config={}, mirror="http://h/repo/", server_ip="192.168.199.1",
        http_root="http://192.168.199.1:8000/pxe/serve", kernel_path="k", initrd_path="i",
        deploy_mode="standalone", iso_url="http://h/x.iso")


def gen(**kw):
    f = generate_all(cfg(**kw), [])
    return f.get("ks.cfg") or f.get("preseed.cfg") or f.get("user-data") or ""


def storage_of(user_data):
    """从 user-data 取 autoinstall.storage —— 用 YAML 解析，不靠字符串切片。"""
    return yaml.safe_load(user_data)["autoinstall"]["storage"]


class UbuntuZfsTest(unittest.TestCase):
    """Ubuntu(autoinstall/subiquity)：zfs 是**一等布局**，产物与官方 layout 简写同构。"""

    def _ud(self, dc, scheme="zfs"):
        return generate_all(cfg(os_type="ubuntu", ver="22.04", dc=dc, scheme=scheme), [])["user-data"]

    def test_layout_zfs_auto_uses_size_largest(self):
        """target.mode=auto → subiquity 的 `match: {size: largest}`（与 lvm/direct 同一写法）。"""
        st = storage_of(self._ud({"target": {"mode": "auto"}, "layout": "zfs"}))
        self.assertEqual(st, {"layout": {"name": "zfs", "match": {"size": "largest"}}})

    def test_layout_zfs_name_uses_device_path(self):
        """target.mode=name(sda) → `match: {path: /dev/sda}`。"""
        st = storage_of(self._ud({"target": {"mode": "name", "name": "sda"}, "layout": "zfs"}))
        self.assertEqual(st, {"layout": {"name": "zfs", "match": {"path": "/dev/sda"}}})

    def test_layout_zfs_match_uses_serial_and_model(self):
        """target.mode=match → 沿用 `_ubuntu_disk_match` 的 serial/model 规则。"""
        st = storage_of(self._ud({
            "target": {"mode": "match", "serial": "S3Z1NB0K123456", "model": "INTEL SSDSC2KB480G8"},
            "layout": "zfs"}))
        self.assertEqual(st, {"layout": {"name": "zfs", "match": {
            "serial": "S3Z1NB0K123456", "model": "INTEL SSDSC2KB480G8"}}})

    def test_layout_zfs_match_still_rejects_id_path(self):
        """id_path 的拒绝是 subiquity 行为决定的，与 layout 无关 —— zfs 也不能放行它。"""
        with self.assertRaises(ValueError) as ctx:
            self._ud({"target": {"mode": "match", "id_path": "pci-0000:00:05.0-scsi-0:0:0:1"},
                      "layout": "zfs"})
        self.assertIn("id_path", str(ctx.exception))

    def test_legacy_disk_scheme_zfs_is_supported_too(self):
        """旧字段 disk_scheme=zfs + 历史键 {disk: sda}：同一 subiquity 路径，同样放行。"""
        st = storage_of(self._ud({"disk": "sda"}, scheme="zfs"))
        self.assertEqual(st, {"layout": {"name": "zfs", "match": {"path": "/dev/sda"}}})

    def test_lvm_direct_outputs_unchanged(self):
        """回归红线：放行 zfs 不能顺手改动 lvm/direct 的既有产物。"""
        self.assertEqual(storage_of(self._ud({}, scheme="lvm")), {"layout": {"name": "lvm"}})
        self.assertEqual(storage_of(self._ud({}, scheme="direct")), {"layout": {"name": "direct"}})


class ZfsRejectedOutsideUbuntuTest(unittest.TestCase):
    """除 Ubuntu 外：没有原生 ZFS root，必须在生成期明确拒绝（不是静默换方案）。"""

    def test_rhel_structured_layout_zfs_is_rejected(self):
        for os_type, ver in (("rhel", "9.3"), ("rocky", "9.4"), ("openeuler", "24.03")):
            with self.subTest(os_type=os_type):
                with self.assertRaises(ValueError) as ctx:
                    gen(os_type=os_type, ver=ver,
                        dc={"target": {"mode": "name", "name": "sda"}, "layout": "zfs"})
                msg = str(ctx.exception)
                self.assertIn("仅支持 Ubuntu", msg, msg)
                self.assertIn("anaconda", msg, msg)          # 说清是安装器能力边界

    def test_rhel_legacy_disk_scheme_zfs_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            gen(os_type="rhel", ver="9.3", scheme="zfs")
        self.assertIn("仅支持 Ubuntu", str(ctx.exception))

    def test_debian_structured_layout_zfs_is_rejected_with_partman_reason(self):
        with self.assertRaises(ValueError) as ctx:
            gen(os_type="debian", ver="13.7.0",
                dc={"target": {"mode": "name", "name": "sda"}, "layout": "zfs"})
        msg = str(ctx.exception)
        self.assertIn("disk_config.layout", msg, msg)         # 指到字段
        self.assertIn("partman", msg, msg)                    # 说清安装器限制
        self.assertIn("仅支持 Ubuntu", msg, msg)

    def test_debian_legacy_disk_scheme_zfs_is_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            gen(os_type="debian", ver="13.7.0", scheme="zfs")
        self.assertIn("partman", str(ctx.exception))

    def test_normal_layouts_still_generate(self):
        for dc, scheme in ((None, "lvm"), (None, "direct"), ({"disk": "sda"}, "lvm")):
            with self.subTest(dc=dc, scheme=scheme):
                self.assertTrue(gen(dc=dc, scheme=scheme).strip())


class ZfsSchemaGateTest(unittest.TestCase):
    """schema 第 2 层：保存期就按 os_type 分岔，且错误点名字段、说明"仅 Ubuntu"。"""

    def test_ubuntu_disk_scheme_zfs_passes(self):
        p = PxeProfileIn(name="t", os_type="ubuntu", admin_password="T@1", disk_scheme="zfs")
        self.assertEqual(p.disk_scheme, "zfs")

    def test_ubuntu_layout_zfs_passes(self):
        dc = {"target": {"mode": "auto"}, "layout": "zfs"}
        p = PxeProfileIn(name="t", os_type="ubuntu", admin_password="T@1", disk_config=dc)
        self.assertEqual(p.disk_config["layout"], "zfs")

    def test_other_os_disk_scheme_zfs_is_rejected_with_field_name(self):
        for os_type in ("rhel", "rocky", "debian", "openeuler"):
            with self.subTest(os_type=os_type):
                with self.assertRaises(ValueError) as ctx:
                    PxeProfileIn(name="t", os_type=os_type, admin_password="T@1",
                                 disk_scheme="zfs")
                msg = str(ctx.exception)
                self.assertIn("disk_scheme", msg, msg)
                self.assertIn("仅支持 Ubuntu", msg, msg)

    def test_other_os_layout_zfs_is_rejected_with_field_name(self):
        """结构化 layout 走 _disk_check_layout_rules：字段名必须是 disk_config.layout。"""
        with self.assertRaises(ValidationError) as ctx:
            PxeProfileIn(name="t", os_type="rhel", admin_password="T@1",
                         disk_config={"target": {"mode": "auto"}, "layout": "zfs"})
        msg = str(ctx.exception)
        self.assertIn("disk_config.layout", msg, msg)
        self.assertIn("仅支持 Ubuntu", msg, msg)


class TargetVsDataDiskTest(unittest.TestCase):
    """A4：标称的"一致性校验"由两道既有机制承担，本轮补一道运行时后备。"""

    def test_name_mode_plus_wipe_data_disk_is_rejected_at_generate_time(self):
        """★ 危险形状：目标盘写死 sda，又要 wipe 一块按稳定条件描述的数据盘 ——
        name 模式没有 %pre 去现场解析设备名，所以**生成期必须拒绝**（而不是静默丢掉这条声明）。"""
        with self.assertRaises(ValueError) as ctx:
            gen(dc={"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                    "partitions": [{"mount": "/", "size": "50G"}],
                    "data_disks": [{"serial": "DATADISK01", "wipe": True}]})
        self.assertIn("name", str(ctx.exception))

    def test_auto_mode_excludes_data_disk_from_candidates(self):
        """数据盘必须出现在排除集里（awk 的 dmatched），才谈得上"不会被选成目标盘"。"""
        ks = gen(dc={"target": {"mode": "auto"}, "layout": "custom",
                     "partitions": [{"mount": "/", "size": "50G"}],
                     "data_disks": [{"serial": "DATADISK01", "wipe": True}]})
        self.assertIn("DATADISK01", ks)                  # 条件进了 %pre
        self.assertIn("dmatched", ks)                    # 排除用的判定函数
        self.assertIn("if (dmatched(L)) next", ks)       # 候选集里排除数据盘

    def test_pre_contains_same_disk_abort_as_last_resort(self):
        """纵深防御：%pre 里必须有"目标盘与数据盘解析成同一块盘 ⇒ 中止"的比对。"""
        ks = gen(dc={"target": {"mode": "auto"}, "layout": "custom",
                     "partitions": [{"mount": "/", "size": "50G"}],
                     "data_disks": [{"serial": "DATADISK01", "wipe": True}]})
        self.assertIn('[ "$ddev1" = "$target" ]', ks)
        self.assertIn("同一块盘", ks)
        self.assertIn("装机中止", ks)
        # 顺序：比对必须在 clearpart **之前**（否则已经动过盘了）
        self.assertLess(ks.index('[ "$ddev1" = "$target" ]'), ks.index("clearpart"))

    def test_no_data_disks_no_guard_noise(self):
        ks = gen(dc={"target": {"mode": "auto"}})
        self.assertNotIn("同一块盘", ks)


if __name__ == "__main__":
    unittest.main()
