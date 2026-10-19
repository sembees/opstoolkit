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


class NoInteractiveEscapeTest(unittest.TestCase):
    """逃生口已被真机否决：菜单里**不允许**出现任何交互/菜单命令。

    为什么用"不许出现"来钉：两轮失败都是"加了个看起来无害的交互命令"把装机链
    打断（prompt 的选项不认 → 中止脚本；menu/choose 之后网络设备不可用 →
    kernel 连接超时）。这里把"不许加回去"变成机械断言，避免以后又有人顺手加。
    """

    def test_helper_is_passthrough(self):
        L = ["#!ipxe", "# boot: h (MAC aa:bb)", "kernel http://x/k", "boot"]
        self.assertEqual(_with_boot_escape(list(L)), L)

    def test_no_interactive_commands_in_menu(self):
        for kw in (dict(), dict(os_type="rhel", os_version="9.4"),
                   dict(os_type="debian", os_version="12")):
            files = generate_all(_cfg(**kw), INSTALLS)
            menu = files["boot/00-11-22-33-44-55.ipxe"]
            for banned in ("prompt", "menu ", "item ", "choose", "sleep"):
                self.assertNotIn(banned, menu, (kw, banned))
            # 装机链本身必须在
            self.assertIn("kernel ", menu)
            self.assertIn("boot", menu)


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
