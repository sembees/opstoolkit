# -*- coding: utf-8 -*-
"""PXE 系统目录（catalog）测试。

背景（用户实测反馈）：用户把 openEuler-24.03-LTS-SP4.iso 放进 /srv/opstk/iso 后，
「提取」界面没有对应选项、装机模板「系统」下拉也只有 Ubuntu/RHEL 两项。
本文件锁住目录化的四条主线：
  1. 目录覆盖任务要求的 13 个系统，且每条都有提取/识别/生成所需字段；
  2. `openEuler-24.03-LTS-SP4.iso` → openeuler/24.03（文件名识别 + pick_iso + 卷标识别）；
  3. pick_iso 对新增发行版挑得准、不串族（rocky 不抢 RHEL 的镜像、openeuler 不认错版本）；
  4. kickstart 家族（含 openeuler/kylin/uos/anolis/fedora/oraclelinux）复用同一份
     ks 生成器（含 %packages --ignoremissing 与 extra_packages）；debian/openSUSE
     这类暂不支持自动安装的系统在生成时给出**明确**拒绝提示，绝不硬凑应答文件。
"""
import unittest

from app.it.pxe import os_catalog
from app.it.pxe import transfers
from app.it.pxe.generator import (
    PxeConfig,
    _ISO_TYPE_KEYS,
    generate_all,
    is_rhel_family,
    pick_iso,
)

# 任务要求的 13 个系统（缺一不可）
REQUIRED_13 = {
    "rhel", "rocky", "almalinux", "centos", "oraclelinux", "openeuler", "kylin",
    "uos", "anolis", "fedora", "ubuntu", "debian", "opensuse",
}

# kickstart(anaconda) 家族：生成逻辑按家族复用，不按发行版复制
KICKSTART_IDS = ("rhel", "rocky", "almalinux", "centos", "oraclelinux", "openeuler",
                 "kylin", "uos", "anolis", "fedora")


def _ks(**kw):
    kw.setdefault("admin_password", "Test@123")
    kw.setdefault("iso_url", "http://10.0.0.1:8000/pxe/iso/test.iso")
    kw.setdefault("mirror", "http://10.0.0.1:8000/pxe/serve/repo/BaseOS/")
    return PxeConfig(**kw)


class CatalogCoverageTest(unittest.TestCase):
    """目录覆盖：13 个系统一条不少，字段齐备。"""

    def test_catalog_covers_required_13(self):
        ids = {e.key for e in os_catalog.entries()}
        self.assertTrue(REQUIRED_13 <= ids, REQUIRED_13 - ids)

    def test_every_entry_has_required_fields(self):
        families = {"kickstart", "preseed", "autoyast"}
        for e in os_catalog.entries():
            with self.subTest(entry=e.key):
                self.assertTrue(e.display, e.key)
                self.assertIn(e.installer, families)
                self.assertTrue(e.filename_keywords, e.key)
                self.assertTrue(e.label_keywords, e.key)
                self.assertTrue(e.kernel_dirs, e.key + " 需要内核候选目录")
                self.assertTrue(e.kernel_names, e.key + " 需要内核文件名候选")
                self.assertTrue(e.initrd_names, e.key + " 需要 initrd 文件名候选")
                self.assertTrue(e.version_hints, e.key + " 需要常见版本候选")

    def test_kickstart_family_membership(self):
        self.assertEqual(set(os_catalog.kickstart_family()), set(KICKSTART_IDS))
        # openeuler/kylin/uos/anolis/fedora/oraclelinux 必须都在 kickstart 家族里
        for t in KICKSTART_IDS:
            self.assertTrue(os_catalog.is_kickstart(t), t)

    def test_kernel_paths_per_family(self):
        # RHEL 系 = images/pxeboot（已验证族）；ubuntu = casper（已验证）；
        # debian / opensuse 按其惯例的候选目录。
        for t in KICKSTART_IDS:
            self.assertEqual(os_catalog.entry(t).kernel_dirs, ("images/pxeboot",), t)
            self.assertEqual(os_catalog.entry(t).dest_initrd, "initrd.img", t)
        self.assertEqual(os_catalog.entry("ubuntu").kernel_dirs, ("casper",))
        self.assertEqual(os_catalog.entry("ubuntu").dest_initrd, "initrd")
        self.assertEqual(os_catalog.entry("debian").kernel_dirs,
                         ("install.amd", "install", "install.386"))
        self.assertEqual(os_catalog.entry("opensuse").kernel_dirs, ("boot/x86_64/loader",))

    def test_unverified_flags_are_honest(self):
        """已验证的两族必须标 verified=True；其余一律 verified=False（不编造）。"""
        for t in ("ubuntu", "rhel", "centos", "rocky", "almalinux"):
            self.assertTrue(os_catalog.entry(t).verified, t)
        for t in ("openeuler", "kylin", "uos", "anolis", "fedora",
                  "oraclelinux", "debian", "opensuse"):
            self.assertFalse(os_catalog.entry(t).verified, t + " 未验证，必须如实标注")
            self.assertIn("未验证", os_catalog.entry(t).note, t)

    def test_extraction_allowlist_derived_from_catalog(self):
        from app.it.pxe import server as pxe_server
        from app.core.schemas import _OS_TYPE_ALLOWED
        self.assertEqual(set(pxe_server._ALLOWED_OS_TYPES), set(os_catalog.all_os_types()))
        self.assertEqual(set(_OS_TYPE_ALLOWED), set(os_catalog.all_os_types()))
        # 新系统（含别名）都能过提取白名单；未知类型不放行
        for t in ("openeuler", "kylin", "uos", "anolis", "fedora",
                  "oraclelinux", "debian", "opensuse", "centos-stream", "alma", "redhat"):
            self.assertTrue(os_catalog.extract_allowed(t), t)
        self.assertFalse(os_catalog.extract_allowed("windows"))
        self.assertFalse(os_catalog.extract_allowed(""))


class OpenEulerDetectionTest(unittest.TestCase):
    """用户实测场景：openEuler-24.03-LTS-SP4.iso 要能识别成 openeuler/24.03。"""

    def test_detect_from_filename(self):
        self.assertEqual(
            os_catalog.detect_os_from_name("openEuler-24.03-LTS-SP4.iso"),
            {"os_type": "openeuler", "os_version": "24.03"})

    def test_version_boundary_not_substring(self):
        # 数字段边界：24.03 不能命中 124.03；sp4 里的 "4" 不该抢走第一个候选
        self.assertEqual(
            os_catalog.detect_os_from_name("openEuler-124.03-x86_64-dvd.iso"),
            {"os_type": "openeuler", "os_version": "124.03"})
        self.assertEqual(
            os_catalog.detect_os_from_name("openeuler-24.03-lts-sp4-x86_64-dvd.iso"),
            {"os_type": "openeuler", "os_version": "24.03"})

    def test_detect_from_label_norm(self):
        # 卷标不含 '.'：归一化规则在 transfers.detect_os_from_label（'-'/'_' → '.'）。
        # 注意：下面的卷标字符串是**构造的测试输入**，openEuler 真实卷标规则未验证；
        # 这里只验证"卷标含 openeuler 时能识别"，不代表真实卷标一定长这样。
        self.assertEqual(
            transfers.detect_os_from_label("OPENEULER-24-03-LTS-SP4-X86_64"),
            {"os_type": "openeuler", "os_version": "24.03"})

    def test_pick_iso_openeuler(self):
        names = ["openEuler-24.03-LTS-SP4-x86_64-dvd.iso",
                 "openEuler-22.03-LTS-x86_64-dvd.iso",
                 "ubuntu-22.04.5-live-server-amd64.iso"]
        self.assertEqual(pick_iso(names, "openeuler", "24.03"),
                         "openEuler-24.03-LTS-SP4-x86_64-dvd.iso")
        self.assertEqual(pick_iso(names, "openeuler", "22.03"),
                         "openEuler-22.03-LTS-x86_64-dvd.iso")
        # 版本不匹配时不猜（返回空串由上层给出可读提示）
        self.assertEqual(pick_iso(names, "openeuler", "25.03"), "")
        # ubuntu 的查询不串到 openeuler 的镜像上
        self.assertEqual(pick_iso(names, "ubuntu", "22.04"),
                         "ubuntu-22.04.5-live-server-amd64.iso")


class PickIsoNewDistrosTest(unittest.TestCase):
    """pick_iso 对新增发行版挑得准、不串族。"""

    NAMES = [
        "RHEL-9.4-x86_64-dvd.iso",
        "Rocky-9.4-x86_64-minimal.iso",
        "AlmaLinux-9.4-x86_64-dvd.iso",
        "CentOS-Stream-9-latest-x86_64-dvd.iso",
        "OracleLinux-R9-U4-x86_64-dvd.iso",
        "openEuler-24.03-LTS-SP4-x86_64-dvd.iso",
        "Kylin-Server-V10-SP3-General-Release-2303-X86_64.iso",
        "UOS-Server-20-1060a-amd64.iso",
        "AnolisOS-8.9-x86_64-dvd.iso",
        "Fedora-Server-dvd-x86_64-40-1.14.iso",
        "ubuntu-22.04.5-live-server-amd64.iso",
        "debian-12.5.0-amd64-netinst.iso",
        "openSUSE-Leap-15.6-DVD-x86_64-Media.iso",
    ]

    def test_each_new_distro_picks_its_own_iso(self):
        cases = {
            "openeuler": ("24.03", "openEuler-24.03-LTS-SP4-x86_64-dvd.iso"),
            "kylin": ("10", "Kylin-Server-V10-SP3-General-Release-2303-X86_64.iso"),
            "uos": ("20", "UOS-Server-20-1060a-amd64.iso"),
            "anolis": ("8.9", "AnolisOS-8.9-x86_64-dvd.iso"),
            # Oracle Linux 官方 ISO 用 R9-U4 段（真实命名习惯），文件名里没有 "9.4"；
            # detect 取第一个数字段 → 9，查询 9 命中
            "oraclelinux": ("9", "OracleLinux-R9-U4-x86_64-dvd.iso"),
            "opensuse": ("15.6", "openSUSE-Leap-15.6-DVD-x86_64-Media.iso"),
            "debian": ("12.5", "debian-12.5.0-amd64-netinst.iso"),
        }
        for ost, (ver, want) in cases.items():
            with self.subTest(os_type=ost):
                self.assertEqual(pick_iso(self.NAMES, ost, ver), want)

    def test_rhel_umbrella_keeps_family_semantics(self):
        """rhel 的伞语义保持：pick_iso(rhel) 在家族内按版本挑（既有测试锁过 Rocky）。

        同版本 9.4 下 RHEL/Rocky/Alma 打平（段数相同），文件名排序兜底 → Rocky
        （与既有 test_pick_iso_picks_right_image 的确定性一致，不是回归）。
        """
        self.assertEqual(pick_iso(self.NAMES, "rhel", "9.4"), "Rocky-9.4-x86_64-minimal.iso")
        self.assertEqual(pick_iso(self.NAMES, "rhel", "9"), "Rocky-9.4-x86_64-minimal.iso")
        # 查询 9 也命中 Oracle 的 R9-U4（R9 里的 9 == 9），但段数 1 < 2，输给 9.4 —— 不串族。

    def test_family_queries_do_not_sweep_new_distros(self):
        """新发行版不并入 rhel 伞：rocky/rhel 的查询不会挑走 openEuler/Kylin/UOS。"""
        for ost in ("rhel", "rocky", "almalinux", "centos", "oraclelinux"):
            with self.subTest(os_type=ost):
                low = (pick_iso(self.NAMES, ost, "9.4") or "").lower()
                for foreign in ("openeuler", "kylin", "uos-server", "anolis",
                                "fedora", "opensuse", "debian"):
                    self.assertNotIn(foreign, low)

    def test_centos_stream_alias(self):
        # centos-stream 别名归到 centos；centos 查询能挑 CentOS Stream 9
        self.assertEqual(os_catalog.entry("centos-stream").key, "centos")
        self.assertEqual(pick_iso(self.NAMES, "centos", "9"),
                         "CentOS-Stream-9-latest-x86_64-dvd.iso")

    def test_iso_type_keys_derived_from_catalog(self):
        """generator._ISO_TYPE_KEYS 派生自目录（rhel = 家族伞，其余 = 自己的关键字）。"""
        for e in os_catalog.entries():
            self.assertEqual(_ISO_TYPE_KEYS[e.key],
                             e.pick_keywords or e.filename_keywords, e.key)
        self.assertEqual(
            _ISO_TYPE_KEYS["rhel"],
            ("rhel", "redhat", "centos", "rocky", "almalinux", "oraclelinux"))


class KickstartFamilyGenerationTest(unittest.TestCase):
    """kickstart 家族复用同一份 ks 生成器（含 extra_packages / inst.repo / inst.ks）。"""

    def test_ks_generated_for_every_kickstart_os(self):
        for ost in list(KICKSTART_IDS) + ["redhat", "alma", "centos-stream"]:
            with self.subTest(os_type=ost):
                files = generate_all(_ks(os_type=ost, os_version="9.4",
                                         extra_packages=["vim", "net-tools", "htop"]))
                self.assertIn("ks.cfg", files)
                ks = files["ks.cfg"]
                self.assertIn('url --url="http://10.0.0.1:8000/pxe/serve/repo/BaseOS/"', ks)
                self.assertIn("%packages --ignoremissing", ks)
                for pkg in ("vim", "net-tools", "htop"):
                    self.assertIn(pkg, ks)
                boot = files["boot.ipxe"]
                self.assertIn("inst.ks=", boot)
                self.assertIn("inst.repo=http://10.0.0.1:8000/pxe/serve/repo/BaseOS/", boot)

    def test_packages_block_carries_extra_packages(self):
        """extra_packages 原样进 %packages（额外软件可用性的机械证据）。"""
        pkgs = ["vim", "net-tools", "bash-completion", "tar", "wget", "curl", "htop"]
        ks = generate_all(_ks(os_type="openeuler", os_version="24.03",
                              extra_packages=pkgs))["ks.cfg"]
        self.assertIn("%packages --ignoremissing", ks)
        lines = ks.splitlines()
        start = lines.index("%packages --ignoremissing")
        end = lines.index("%end", start)
        block = lines[start + 1:end]
        for p in pkgs:
            self.assertIn(p, block, "%packages 块里必须有 " + p)

    def test_family_shares_the_same_installer_paths(self):
        """家族成员走的是同一段生成代码：kicksart 条目的 inst.repo/inst.ks 完全同构。"""
        ref = None
        for ost in KICKSTART_IDS:
            boot = generate_all(_ks(os_type=ost, os_version="9.4",
                                    kernel_path="x/9/vmlinuz",
                                    initrd_path="x/9/initrd.img"))["boot.ipxe"]
            norm = boot.replace(ost, "<OST>")
            if ref is None:
                ref = norm
            self.assertEqual(norm, ref, ost)


class UnsupportedFamilyTest(unittest.TestCase):
    """debian/openSUSE：只支持识别 + 提取引导介质，生成配置时明确拒绝（不硬凑）。"""

    def test_debian_generate_rejected_with_clear_message(self):
        with self.assertRaises(ValueError) as cm:
            generate_all(_ks(os_type="debian", os_version="12.5"))
        msg = str(cm.exception)
        self.assertIn("暂不支持", msg)
        self.assertIn("Debian", msg)
        self.assertIn("提取引导介质", msg)

    def test_opensuse_generate_rejected_with_clear_message(self):
        with self.assertRaises(ValueError) as cm:
            generate_all(_ks(os_type="opensuse", os_version="15.6"))
        msg = str(cm.exception)
        self.assertIn("暂不支持", msg)
        self.assertIn("openSUSE", msg)
        self.assertIn("AutoYaST", msg)

    def test_rejection_happens_before_any_answer_file_is_written(self):
        """拒绝必须发生在生成任何应答文件之前（不能产出一半的 ks/boot.ipxe）。"""
        from app.it.pxe.generator import _ipxe_menu
        with self.assertRaises(ValueError):
            _ipxe_menu(_ks(os_type="debian", os_version="12.5"))
        with self.assertRaises(ValueError):
            generate_all(_ks(os_type="debian", os_version="12.5"))

    def test_unsupported_can_still_be_detected_and_extracted(self):
        """不支持自动安装 ≠ 不能识别/提取：debian/openSUSE 仍过识别与提取白名单。"""
        # detect 取文件名里第一个数字段（12.5.0 是 Debian 官方命名的真实形状）
        det = os_catalog.detect_os_from_name("debian-12.5.0-amd64-netinst.iso")
        self.assertEqual(det, {"os_type": "debian", "os_version": "12.5.0"})
        det = os_catalog.detect_os_from_name("openSUSE-Leap-15.6-DVD-x86_64-Media.iso")
        self.assertEqual(det, {"os_type": "opensuse", "os_version": "15.6"})
        self.assertTrue(os_catalog.extract_allowed("debian"))
        self.assertTrue(os_catalog.extract_allowed("opensuse"))


class FamilyHelpersTest(unittest.TestCase):
    """家族判定：is_rhel_family 与目录同源（含归一化与别名）。"""

    def test_is_rhel_family_derived(self):
        for t in KICKSTART_IDS + ("redhat", "alma", "centos-stream"):
            self.assertTrue(is_rhel_family(t), t)
        self.assertFalse(is_rhel_family("ubuntu"))
        self.assertFalse(is_rhel_family("debian"))
        self.assertFalse(is_rhel_family("opensuse"))
        self.assertFalse(is_rhel_family("windows"))
        self.assertFalse(is_rhel_family(""))

    def test_is_rhel_family_normalizes_case_and_space(self):
        self.assertTrue(is_rhel_family(" RHEL "))
        self.assertTrue(is_rhel_family("OpenEuler"))

    def test_aliases_map_to_master_id(self):
        self.assertEqual(os_catalog.entry("alma").key, "almalinux")
        self.assertEqual(os_catalog.entry("redhat").key, "rhel")
        self.assertEqual(os_catalog.entry("centos-stream").key, "centos")

    def test_label_detection_regression_after_catalog(self):
        """目录化后卷标识别的既有形状不变（transfer.detect_os_from_label 的契约）。"""
        f = transfers.detect_os_from_label
        self.assertEqual(f("Rocky-9-4-x86_64-dvd"), {"os_type": "rocky", "os_version": "9.4"})
        self.assertEqual(f("CENTOS_7_X86_64"), {"os_type": "centos", "os_version": "7"})
        self.assertEqual(f("UBUNTU_22.04.5_LIVE_SERVER"),
                         {"os_type": "ubuntu", "os_version": "22.04.5"})
        # UbuntuKylin（优麒麟）同时命中 ubuntu/kylin：最长关键字胜出 → ubuntu
        self.assertEqual(f("UBUNTUKYLIN_24.04_LTS"),
                         {"os_type": "ubuntu", "os_version": "24.04"})

    def test_api_payload_shape(self):
        import json
        payload = os_catalog.api_payload()
        items = payload["items"]
        self.assertEqual(len(items), len(os_catalog.entries()))
        for it in items:
            self.assertTrue({"id", "name", "family", "auto_install", "aliases",
                             "filename_keywords", "kernel_dirs", "kernel_names",
                             "initrd_names", "version_hints", "verified", "note"}
                            <= set(it), json.dumps(sorted(it), ensure_ascii=False))
        oe = next(i for i in items if i["id"] == "openeuler")
        self.assertEqual(oe["name"], "openEuler")
        self.assertEqual(oe["family"], "kickstart")
        self.assertTrue(oe["auto_install"])
        self.assertFalse(oe["verified"])
        self.assertIn("24.03", oe["version_hints"])


class OsCatalogEndpointTest(unittest.TestCase):
    """GET /api/it/pxe/os-catalog：前端三个下拉的唯一来源要真的挂上路由。"""

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import pxe as pxe_api
        from app.core.auth import get_current_user

        app = FastAPI()
        app.include_router(pxe_api.router, prefix="/api/it/pxe")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "role": "admin"}
        return TestClient(app)

    def test_get_os_catalog_route(self):
        client = self._client()
        with client:
            r = client.get("/api/it/pxe/os-catalog")
        self.assertEqual(r.status_code, 200, r.text)
        items = r.json()["items"]
        ids = {i["id"] for i in items}
        self.assertTrue(REQUIRED_13 <= ids, REQUIRED_13 - ids)
        oe = next(i for i in items if i["id"] == "openeuler")
        self.assertEqual(oe["name"], "openEuler")
        self.assertEqual(oe["kernel_dirs"], ["images/pxeboot"])

    def test_get_os_catalog_requires_login(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import pxe as pxe_api

        app = FastAPI()
        app.include_router(pxe_api.router, prefix="/api/it/pxe")
        client = TestClient(app)
        with client:
            r = client.get("/api/it/pxe/os-catalog")
        self.assertEqual(r.status_code, 401, r.status_code)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
