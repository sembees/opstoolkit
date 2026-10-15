# -*- coding: utf-8 -*-
"""A6：**ZFS 口径统一**；A4：目标盘与数据盘的冲突拦截（含"其实早已关闭"的核实结论）。

## A6 的实情（本轮核准）

改前**三处说法不一致**，其中两处会静默给出别的东西：

| 位置 | 改前行为 |
|---|---|
| `schemas.disk_scheme` 白名单 | 拒绝 zfs（422）—— 与手册一致 |
| `_disk_plan` 的 `disk_config.layout` | **接受** zfs，随后 RHEL 侧按 lvm 分支生成 ⇒ 写 zfs、装出来是 LVM，**产物上看不出问题** |
| Ubuntu 旧 `disk_scheme` 路径 | 会真的给 subiquity 发 `layout.name=zfs`（subiquity 认这个键） |

本工具的 ZFS 支持确实没做（界面选项已移除、手册写明未实现）。既然没做，就在**所有**能到达的
路径上一致拒绝，并说清替代方案 —— 不静默生成"配置写 zfs、实际是 LVM"的产物。

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

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe.generator import PxeConfig, generate_all   # noqa: E402


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


class ZfsRejectedEverywhereTest(unittest.TestCase):
    """A6：zfs 在任何能到达的路径上都拒绝，且**理由一致**（不是"非法值"）。"""

    def _assert_rejected(self, ks_callable):
        with self.assertRaises(ValueError) as ctx:
            ks_callable()
        msg = str(ctx.exception)
        self.assertIn("ZFS 未实现", msg, msg)
        self.assertIn("layout=lvm", msg, msg)          # 必须给出替代方案
        return msg

    def test_structured_layout_zfs_is_rejected_for_every_family(self):
        for os_type, ver in (("rhel", "9.3"), ("rocky", "9.4"), ("ubuntu", "22.04"),
                             ("debian", "13.7.0"), ("openeuler", "24.03")):
            self._assert_rejected(lambda o=os_type, v=ver: gen(
                os_type=o, ver=v,
                dc={"target": {"mode": "name", "name": "sda"}, "layout": "zfs"}))

    def test_legacy_disk_scheme_zfs_is_rejected_too(self):
        """旧字段也要拦：它绕过 disk_config，以前 Ubuntu 侧会真的发 layout.name=zfs 给 subiquity。"""
        self._assert_rejected(lambda: gen(os_type="ubuntu", ver="22.04", dc={"disk": "sda"},
                                          scheme="zfs"))
        self._assert_rejected(lambda: gen(os_type="rhel", ver="9.3", scheme="zfs"))

    def test_error_says_it_is_recognized_but_unimplemented(self):
        """消息不能说"非法值"：zfs 是被**认得**的键，只是没实现。混为一谈会误导运维去查拼写。"""
        msg = self._assert_rejected(lambda: gen(
            dc={"target": {"mode": "name", "name": "sda"}, "layout": "zfs"}))
        self.assertNotIn("layout 非法", msg, msg)

    def test_normal_layouts_still_generate(self):
        for dc, scheme in ((None, "lvm"), (None, "direct"), ({"disk": "sda"}, "lvm")):
            self.assertTrue(gen(dc=dc, scheme=scheme).strip())


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
