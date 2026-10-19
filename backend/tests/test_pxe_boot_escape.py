# -*- coding: utf-8 -*-
"""每机 iPXE 菜单的"逃生口" + 装完回调重试（2026-10-19 真机验收后补的加固）。

来由（真机实测）：装完之后、装完回调落库之前有几秒窗口，此时机器若走网卡引导会
**再进一遍安装器**（VM153 上撞到，被抹了第二次盘）。两处加固：
  1. 每机菜单插一条 `prompt --key 0x6c --timeout 8000 … && goto local`：
     8 秒内按 `l` 就 sanboot/exit 到本地磁盘，不按则照常安装。
     **默认必须是安装** —— 超时默认本地盘会让无人值守装机静默失败（机器起来了、
     系统却是旧的），这种失败比重复装机更难发现。
  2. 装完回调从"单次 curl"改成 3 次重试：回调失败 = 记录永远 pending = 下次
     网卡引导又被重装，属于同一个坑的另一个入口。

这里钉住：菜单形状、默认动作、三族（ubuntu/rhel/debian）都带转义口、回调带重试。
"""
import unittest

import yaml

from app.it.pxe.generator import PxeConfig, _with_boot_escape, generate_all

ISO = "http://10.0.0.1:8000/pxe/iso/test.iso"
DONE = "http://10.0.0.1:8000/api/it/pxe/installs/abc123/done?t=deadbeef"
INSTALLS = [
    {"mac": "00:11:22:33:44:55", "hostname": "web-01", "ip": "10.10.10.100",
     "status": "pending", "done_url": DONE},
]


def _cfg(**kw):
    kw.setdefault("admin_password", "Test@123")
    kw.setdefault("iso_url", ISO)
    kw.setdefault("os_type", "ubuntu")
    kw.setdefault("os_version", "22.04")
    kw.setdefault("admin_user", "ops")
    kw.setdefault("server_ip", "10.10.10.10")
    kw.setdefault("http_root", "http://10.10.10.10:8000/pxe/serve")
    kw.setdefault("deploy_mode", "standalone")
    kw.setdefault("net_mode", "dhcp")
    kw.setdefault("mirror", "http://10.0.0.1:8000/pxe/repo/rocky/9/BaseOS/x86_64/os/")
    return PxeConfig(**kw)


class BootEscapeTest(unittest.TestCase):
    """菜单逃生口：形状、默认动作、插入位置。"""

    def test_inserts_prompt_and_local_branch(self):
        L = ["#!ipxe", "# boot: h (MAC aa:bb)", "kernel http://x/k", "initrd http://x/i", "boot"]
        out = _with_boot_escape(L)
        self.assertEqual(out[0], "#!ipxe")
        self.assertEqual(out[1], "# boot: h (MAC aa:bb)")
        # prompt 必须在**第一条安装命令之前**（否则它等完 8 秒才开始装机，白等）
        self.assertIn("prompt --key 0x6c", out[2])
        self.assertIn("goto local", out[2])
        self.assertIn("kernel http://x/k", out[3])
        # 末尾要有本地引导分支
        self.assertIn(":local", out)
        self.assertTrue(any("sanboot" in x for x in out))
        self.assertTrue(any(x.strip() == "exit" or "|| exit" in x for x in out))

    def test_default_action_is_install_not_local(self):
        """超时必须落到**安装**（fall-through），不能 `|| goto local`。

        写成 `prompt ... || goto local` 的话，无人值守机器不按键 → 直接本地盘 →
        系统永远装不上，而且是静默失败。这条断言就是防这个反向改法。
        """
        out = _with_boot_escape(["#!ipxe", "# boot: h", "kernel k", "boot"])
        prompt = [x for x in out if x.startswith("prompt")][0]
        self.assertIn("goto local", prompt)
        self.assertNotIn("|| goto local", prompt)
        # 安装命令在 prompt 之后，且中间没有 goto（顺序不能反）
        idx_prompt = out.index(prompt)
        idx_kernel = [i for i, x in enumerate(out) if x.startswith("kernel")][0]
        self.assertLess(idx_prompt, idx_kernel)

    def test_unexpected_shape_left_untouched(self):
        """不认识的形状原样返回 —— 宁可没有逃生口，也不能把菜单改坏。"""
        for bad in ([], ["#!ipxe"], ["foo", "bar", "baz"], ["#!grub", "# x", "linux"]):
            self.assertEqual(_with_boot_escape(list(bad)), bad, bad)


class PerMacMenuTest(unittest.TestCase):
    """端到端：三族每机菜单都要带逃生口，且都能从生成结果里看到。"""

    def _menu(self, **kw):
        files = generate_all(_cfg(**kw), INSTALLS)
        key = "boot/00-11-22-33-44-55.ipxe"
        self.assertIn(key, files)
        return files[key]

    def test_ubuntu_menu_has_escape(self):
        m = self._menu()
        self.assertIn("prompt --key 0x6c --timeout 8000", m)
        self.assertIn(":local", m)
        self.assertIn("sanboot", m)
        # 装机链本身不能丢
        self.assertIn("autoinstall", m)
        self.assertIn("cloud-config-url=", m)

    def test_rhel_menu_has_escape(self):
        m = self._menu(os_type="rhel", os_version="9.4")
        self.assertIn("prompt --key 0x6c", m)
        self.assertIn("inst.ks=", m)
        self.assertIn(":local", m)

    def test_debian_menu_has_escape(self):
        m = self._menu(os_type="debian", os_version="12")
        self.assertIn("prompt --key 0x6c", m)
        self.assertIn("preseed/url=", m)
        self.assertIn(":local", m)


class InstallCallbackRetryTest(unittest.TestCase):
    """装完回调必须带重试（单次偶发失败会让记录永远 pending）。"""

    def test_ubuntu_callback_retries(self):
        files = generate_all(_cfg(), INSTALLS)
        ud = files["user-data/00-11-22-33-44-55/user-data"]
        self.assertIn("for i in 1 2 3; do curl", ud)
        self.assertIn("/done?t=deadbeef", ud)
        doc = yaml.safe_load(ud)
        cmds = " ".join(doc["autoinstall"]["late-commands"])
        self.assertIn("for i in 1 2 3", cmds)

    def test_rhel_callback_retries(self):
        files = generate_all(_cfg(os_type="rhel", os_version="9.4"), INSTALLS)
        # ⚠ 回调只随**每机**应答文件下发（模板级 ks.cfg 恒不带，设计如此），
        #   所以这里必须取 `ks/<mac>/ks.cfg`，不能取根上那份。
        key = "ks/00-11-22-33-44-55/ks.cfg"
        self.assertIn(key, files)
        ks = files[key]
        self.assertIn("for i in 1 2 3; do curl", ks)
        self.assertIn("/done?t=deadbeef", ks)
        # 反向：模板级那份不该带回调（否则每台机器都回报，等于没隔离）
        self.assertNotIn("/done?t=", files["ks.cfg"])

    def test_no_callback_when_not_pending(self):
        """已装完的记录（无 done_url）不该再带回调 —— 否则装完的系统会反复回报。"""
        files = generate_all(_cfg(), [{"mac": "00:11:22:33:44:55", "hostname": "web-01",
                                       "ip": "10.10.10.100", "status": "installed"}])
        ud = files["user-data/00-11-22-33-44-55/user-data"]
        self.assertNotIn("/done?t=", ud)


if __name__ == "__main__":
    unittest.main()
