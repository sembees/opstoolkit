# -*- coding: utf-8 -*-
"""R4 / RUNBOOK §5.50：ZTP 部署链路的三重缺陷回归。

真机实测（2026-09，10.128.118.113）暴露的三件事，这里逐条钉死：
  1. **假成功**：ZTP 写的是 opstk-ztp.conf，而宿主机重载单元只监视 opstk-pxe.conf
     ⇒ 那份配置永远不会被加载；而 dhcp_control 在容器里返回 ok=True，
     接口于是报成功。现在必须等 **ZTP 槽位** 的握手（sha + 新鲜度）才算成功。
  2. **骨干网地雷**：生成的配置里 `interface=` 是猜的 —— 容器里猜到 ens18
     （承企业网 10.128.118.113）且 standalone 还带 dhcp-range ⇒ 一旦加载就在
     骨干网段上开 DHCP 池。现在落盘前必须被 `check_dhcp_conf_safety` 拦下。
  3. **非原子落盘 / 假 ok**：设备随时可能在下载配置，半截文件会被设备当成有效配置；
     配置写失败也曾被吞掉。现在原子写 + 失败即 ok=False。
"""
import importlib
import os
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

dhcp = importlib.import_module("app.core.dhcp")

# 单位：真机上的事实 —— ens18 承载默认路由（企业网 10.128.118.113/24），
# ens19 是专用装机网（192.168.199.1/24）。
# `iface_v4_map` 让"地址池必须落在网卡自己的网段里"这条检查在 Windows 上也能确定性地测
# （Windows 上读不到网卡地址，不注入的话就只剩真实 Linux 能跑）。
HOST_FACTS = {"iface_name": "ens18", "iface_v4": ("10.128.118.113", 24),
              "iface_v4_map": {"ens18": ("10.128.118.113", 24),
                               "ens19": ("192.168.199.1", 24)}}
GOOD = ("interface=ens19\nbind-interfaces\n"
        "dhcp-range=192.168.199.100,192.168.199.200,12h\n")


class DhcpConfSafetyTest(unittest.TestCase):
    """红线检查：六条全部 fail-closed。"""

    def setUp(self):
        # /sys/class/net 在 Windows 上不存在 —— 不 mock 的话"网卡是否存在"这条会被跳过，
        # 测试也就测不到它。这里显式给出一个"真实存在"的网卡集合。
        self.real = {"lo": None, "ens18": None, "ens19": None}
        self._orig = os.listdir
        # 配置目录也要隔离：这条检查会去读同目录下**别的** .conf（找不可重复关键字），
        # 而容器里的 /etc/dnsmasq.d 是宿主机的真实配置 —— 用例不能随它漂移。
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.confdir = self._tmp.name
        self.addCleanup(self._tmp.cleanup)
        self._orig_conf_dir = dhcp.CONF_DIR
        dhcp.CONF_DIR = self.confdir
        self.addCleanup(lambda: setattr(dhcp, "CONF_DIR", self._orig_conf_dir))

        def fake(path):
            if path == "/sys/class/net":
                return list(self.real)
            return self._orig(path)

        dhcp.os.listdir = fake
        self.addCleanup(lambda: setattr(dhcp.os, "listdir", self._orig))

    def test_good_config_passes(self):
        ok, why = dhcp.check_dhcp_conf_safety(GOOD, HOST_FACTS)
        self.assertTrue(ok, why)

    def test_placeholder_iface_is_rejected(self):
        ok, why = dhcp.check_dhcp_conf_safety("interface=eth0\n" + GOOD, HOST_FACTS)
        self.assertFalse(ok)
        self.assertIn("占位值", why)

    def test_missing_iface_is_rejected(self):
        """网卡不存在时 dnsmasq 配了 bind-interfaces 会**起不来** —— 而它同时服务 PXE。"""
        ok, why = dhcp.check_dhcp_conf_safety("interface=ens99\n" + GOOD, HOST_FACTS)
        self.assertFalse(ok)
        self.assertIn("不存在网卡", why)

    def test_backbone_iface_is_rejected(self):
        ok, why = dhcp.check_dhcp_conf_safety(
            "interface=ens18\nbind-interfaces\n"
            "dhcp-range=10.0.0.100,10.0.0.200,12h\n", HOST_FACTS)
        self.assertFalse(ok)
        self.assertIn("默认路由", why)
        self.assertIn("10.128.118.0/24", why)

    def test_pool_inside_backbone_subnet_is_rejected(self):
        """网卡名写对了也不行：池落在骨干网段里同样会把骨干地址分出去。"""
        ok, why = dhcp.check_dhcp_conf_safety(
            "interface=ens19\nbind-interfaces\n"
            "dhcp-range=10.128.118.150,10.128.118.160,12h\n", HOST_FACTS)
        self.assertFalse(ok)
        self.assertIn("骨干网段", why)

    def test_range_partially_inside_backbone_is_rejected(self):
        ok, why = dhcp.check_dhcp_conf_safety(
            "interface=ens19\nbind-interfaces\n"
            "dhcp-range=10.128.117.200,10.128.118.10,12h\n", HOST_FACTS)
        self.assertFalse(ok)

    def test_cannot_determine_backbone_is_rejected(self):
        """读不到骨干网事实时**拒绝**（fail-closed），而不是放行。"""
        ok, why = dhcp.check_dhcp_conf_safety(GOOD, {"iface_name": "", "iface_v4": None})
        self.assertFalse(ok)
        self.assertIn("拒绝部署", why)

    def test_proxy_range_has_no_pool(self):
        """proxy 模式只写 `<ip>,proxy`，不构成地址池 —— 不该被当成越界。"""
        ok, why = dhcp.check_dhcp_conf_safety(
            "interface=ens19\nbind-interfaces\ndhcp-range=192.168.199.1,proxy\n", HOST_FACTS)
        self.assertTrue(ok, why)

    def test_pool_outside_iface_subnet_is_rejected(self):
        """第 6 条：地址池不在网卡自己的网段里 ⇒ 拒绝。

        dnsmasq 是按网卡地址推掩码/广播地址的；池子跨网段时客户端会拿到不可用的地址，
        而且这通常意味着"池开到了别的网段上"（真机上这个错很难查：配置看着全对）。
        """
        ok, why = dhcp.check_dhcp_conf_safety(
            "interface=ens19\nbind-interfaces\ndhcp-range=10.99.99.100,10.99.99.200,12h\n",
            HOST_FACTS)
        self.assertFalse(ok)
        self.assertIn("不落在", why)
        self.assertIn("192.168.199.0/24", why)

    def test_iface_without_address_is_rejected(self):
        """读不到网卡地址/掩码时也拒绝（fail-closed），不能靠猜。"""
        facts = {"iface_name": "ens18", "iface_v4": ("10.128.118.113", 24),
                 "iface_v4_map": {"ens18": ("10.128.118.113", 24)}}
        ok, why = dhcp.check_dhcp_conf_safety(
            "interface=ens19\nbind-interfaces\ndhcp-range=192.168.199.100,192.168.199.200,12h\n",
            facts)
        self.assertFalse(ok)
        self.assertIn("读不到 DHCP 网卡", why)

    def test_no_interface_line_is_rejected(self):
        ok, why = dhcp.check_dhcp_conf_safety("port=0\nenable-tftp\n", HOST_FACTS)
        self.assertFalse(ok)
        self.assertIn("interface=", why)

    def test_singleton_keyword_conflict_is_rejected(self):
        """真机实测：两份配置都写 `port=0` ⇒ dnsmasq 报 illegal repeated keyword，
        拒绝**整份**配置 ⇒ 守护进程起不来（含开机）⇒ 装机网段没有 DHCP/TFTP。
        所以这类冲突必须在落盘前被拦下，而且要说清楚是哪个文件冲突。"""
        import pathlib
        other = pathlib.Path(self.confdir) / "opstk-pxe.conf"
        other.write_text("port=0\ninterface=ens19\n", encoding="utf-8")
        mine = pathlib.Path(self.confdir) / "opstk-ztp.conf"
        ok, why = dhcp.check_dhcp_conf_safety(
            "port=0\n" + GOOD, HOST_FACTS, conf_dir=self.confdir, own_path=str(mine))
        self.assertFalse(ok)
        self.assertIn("port", why)
        self.assertIn("opstk-pxe.conf", why)

    def test_singleton_keyword_own_file_is_not_a_conflict(self):
        """自己的文件不算冲突（否则 PXE 每次部署都会自己拦自己）。"""
        import pathlib
        mine = pathlib.Path(self.confdir) / "opstk-pxe.conf"
        mine.write_text("port=0\n", encoding="utf-8")
        ok, why = dhcp.check_dhcp_conf_safety(
            "port=0\n" + GOOD, HOST_FACTS, conf_dir=self.confdir, own_path=str(mine))
        self.assertTrue(ok, why)

    def test_repeatable_keywords_are_not_treated_as_conflict(self):
        """`interface=`/`bind-interfaces`/`enable-tftp`/`tftp-root` 实测可以重复，
        不许把它们误判成冲突（真机逐个删掉验证过）。"""
        import pathlib
        (pathlib.Path(self.confdir) / "opstk-pxe.conf").write_text(
            "port=0\ninterface=ens19\nbind-interfaces\nenable-tftp\ntftp-root=/srv/tftp\n",
            encoding="utf-8")
        ok, why = dhcp.check_dhcp_conf_safety(
            "bind-interfaces\nenable-tftp\ntftp-root=/srv/tftp\n" + GOOD, HOST_FACTS,
            conf_dir=self.confdir, own_path=str(pathlib.Path(self.confdir) / "opstk-ztp.conf"))
        self.assertTrue(ok, why)


class DefaultRouteParsingTest(unittest.TestCase):
    """从 /proc/net/route 认默认路由（字段是**主机字节序**的十六进制）。"""

    def test_default_route_iface_and_gateway(self):
        import tempfile
        # 真机 /proc/net/route 的形态：Iface Destination Gateway Flags ...
        body = (
            "Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\t\tMTU\tWindow\tIRTT\n"
            "ens18\t00000000\t0100000A\t0003\t0\t0\t100\t00000000\t0\t0\t0\n"
            "ens19\t0000A8C0\t00000000\t0001\t0\t0\t101\t00FFFFFF\t0\t0\t0\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".route", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(body)
            path = fh.name
        try:
            self.assertEqual(dhcp._default_route_iface(path), "ens18")
            # 0100000A 小端还原 = 10.0.0.1
            self.assertEqual(dhcp.gateway_from_route(path), "10.0.0.1")
        finally:
            os.unlink(path)

    def test_missing_file_is_not_an_exception(self):
        self.assertEqual(dhcp._default_route_iface("Z:/nope/route"), "")
        self.assertEqual(dhcp.gateway_from_route("Z:/nope/route"), "")


class ZtpGeneratorIfaceTest(unittest.TestCase):
    """生成层：网卡只认模板里显式填的，绝不自动猜。"""

    def _gen(self, iface):
        gen = importlib.import_module("app.ct.ztp.generator")
        p = gen.ZtpProfile(vendor="h3c", admin_password="Test@123", dhcp_iface=iface,
                           server_ip="192.168.199.1", tftp_root="/srv/tftp",
                           http_root="http://192.168.199.1:8000/ztp")
        return p, gen.generate_all(p, [gen.ZtpDevice(hostname="SW1", mac="00:11:22:33:44:55")])

    def test_placeholder_is_not_replaced_by_guessing(self):
        p, files = self._gen("eth0")
        conf = files["dnsmasq.conf"]
        self.assertIn("interface=eth0", conf)
        # 关键反向断言：绝不能出现猜出来的网卡名（真机上是 ens18 = 企业网卡）
        for guessed in ("ens18", "ens19", "ens160"):
            self.assertNotIn("interface=" + guessed, conf)
        self.assertIn("未指定 DHCP 网卡", conf)
        self.assertIn("警告", files["README.txt"])
        self.assertIn("直接部署", files["README.txt"])

    def test_explicit_iface_is_used_verbatim(self):
        p, files = self._gen("ens19")
        self.assertIn("interface=ens19", files["dnsmasq.conf"])
        self.assertNotIn("未指定 DHCP 网卡", files["dnsmasq.conf"])
        self.assertIn("DHCP 网卡: ens19", files["README.txt"])

    def test_generated_placeholder_conf_is_actually_refused(self):
        """生成层与红线检查必须一致：占位配置真的会被部署侧拒绝。"""
        p, files = self._gen("eth0")
        quick = {"iface_name": "ens18", "iface_v4": ("10.128.118.113", 24)}
        ok, why = dhcp.check_dhcp_conf_safety(files["dnsmasq.conf"], quick)
        self.assertFalse(ok, "占位配置竟然通过了红线检查")
        self.assertIn("占位值", why)

    def test_conf_has_no_singleton_keyword(self):
        """真机实测：`port=0` 在 dnsmasq 里**不可重复**，而 PXE 的配置已经写了它。
        ZTP 的投递配置再写一次 ⇒ `dnsmasq --test` 报 illegal repeated keyword ⇒
        dnsmasq 起不来（含开机）⇒ 装机网段没有 DHCP/TFTP。"""
        for iface in ("eth0", "ens19"):
            p, files = self._gen(iface)
            lines = [ln.strip() for ln in files["dnsmasq.conf"].splitlines()
                     if not ln.strip().startswith("#")]
            self.assertNotIn("port=0", lines, files["dnsmasq.conf"])
            self.assertFalse([ln for ln in lines if ln.startswith("port=")],
                             "ZTP 投递配置里不该出现 port=")
        # 只有跑 ZTP、不跑 PXE 的宿主机需要关 DNS，README 要把这件事说清楚
        p, files = self._gen("ens19")
        self.assertIn("port=0", files["README.txt"])


class ZtpDeployServerTest(unittest.TestCase):
    """部署层：红线 → 预检 → 原子落盘 → 握手 → 回滚。"""

    GOOD_CONF = ("interface=ens19\nbind-interfaces\n"
                 "dhcp-range=192.168.199.100,192.168.199.200,12h\n")

    def setUp(self):
        import tempfile
        from unittest import mock

        self.mock = mock
        self.ztp = importlib.import_module("app.ct.ztp.server")
        self._tmp = tempfile.TemporaryDirectory()
        t = self._tmp.name
        self.tftp = os.path.join(t, "tftp")
        self.web = os.path.join(t, "ztp-web")
        self.confdir = os.path.join(t, "dnsmasq.d")
        self.statedir = os.path.join(t, "state")
        for d in (self.tftp, self.web, self.confdir, self.statedir):
            os.makedirs(d)
        self.conf = os.path.join(self.confdir, "opstk-ztp.conf")
        self.dhcp_control = mock.Mock(
            return_value={"ok": True, "action": "restart", "msg": "ok", "running": True})
        import ipaddress
        for target, attr, value in (
            (self.ztp, "TFTP_ROOT", self.tftp),
            (self.ztp, "WEB_ROOT", self.web),
            (dhcp, "CONF_DIR", self.confdir),
            (dhcp, "ZTP_CONF", self.conf),
            (dhcp, "HOST_RELOAD_STATE", os.path.join(self.statedir, ".dnsmasq-reload.state")),
            (dhcp, "HOST_RELOAD_STATE_ZTP",
             os.path.join(self.statedir, ".dnsmasq-reload.state.ztp")),
            (dhcp, "HOST_RELOAD_STATE_DIR", self.statedir),
            (dhcp, "is_linux", lambda: True),
            (dhcp, "sudo_ok", lambda: True),
            (dhcp, "ensure_dirs", lambda dirs=None: []),
            (dhcp, "dhcp_control", self.dhcp_control),
            (dhcp, "_in_container", lambda: False),
            # 真机事实：ens18 承载默认路由（企业网 10.128.118.0/24）
            (dhcp, "protected_networks",
             lambda providers=None: ("ens18", [ipaddress.ip_network("10.128.118.0/24")], "")),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    def _files(self, conf=None, tag="SW1"):
        return {"dnsmasq.conf": conf if conf is not None else self.GOOD_CONF,
                "ztp/%s.cfg" % tag: "sysname %s\n" % tag,
                "README.txt": "readme\n"}

    def _tree(self):
        out = {}
        for root in (self.tftp, self.web):
            for r, _d, names in os.walk(root):
                for n in names:
                    p = os.path.join(r, n)
                    with open(p, "rb") as fh:
                        out[os.path.relpath(p, root) + "@" + os.path.basename(root)] = fh.read()
        if os.path.exists(self.conf):
            with open(self.conf, "rb") as fh:
                out["<conf>"] = fh.read()
        return out

    def _delegate(self, wait):
        self.mock.patch.object(dhcp, "_in_container", lambda: True).start()
        self.mock.patch.object(
            dhcp, "reload_preflight",
            lambda *a, **k: {"ok": True, "skipped": False, "probed": True, "reason": "",
                             "hint": "", "state": None, "log": ["预检：测试桩"]}).start()
        self.addCleanup(self.mock.patch.stopall)
        self.dhcp_control.return_value = {
            "ok": True, "action": "restart", "managed": False, "reload_delegated": True,
            "msg": "容器内，等待宿主机重载", "running": True}
        m = self.mock.Mock(side_effect=list(wait))
        self.mock.patch.object(dhcp, "wait_host_reload", m).start()
        # 生产代码走带重试的版本；测试里转调上面这个桩（不产生补触发）
        self.mock.patch.object(dhcp, "wait_host_reload_retrying",
                               lambda *a, **k: (m(*a, **k), 0)).start()
        self.addCleanup(self.mock.patch.stopall)
        return m

    WAIT_OK = {"ok": True, "state": {"state": "OK", "sha": "x", "ts": "1", "reason": "", "err": ""}}
    # FAIL 的 ts 必须新鲜：U2-F8 之后只认"本次部署之后写下的 FAIL"（陈旧 FAIL 不算数）
    _FRESH_TS = "9999999999"
    WAIT_STALE_TEST_FAILED = {"ok": False, "state": {"state": "FAIL", "sha": "", "ts": "1",
                                                     "reason": "test-failed:badoption", "err": ""}}
    WAIT_TEST_FAILED = {"ok": False, "state": {"state": "FAIL", "sha": "", "ts": _FRESH_TS,
                                              "reason": "test-failed:badoption", "err": ""}}
    WAIT_RESTART_FAILED = {"ok": False, "state": {"state": "FAIL", "sha": "", "ts": _FRESH_TS,
                                                  "reason": "restart-failed", "err": ""}}

    # ── 红线 ──

    def test_backbone_config_is_refused_with_zero_writes(self):
        """生成器把网卡猜成企业网那张卡时，必须在落盘前被拦下。"""
        bad = ("interface=ens18\nbind-interfaces\ndhcp-range=10.0.0.100,10.0.0.200,12h\n")
        res = self.ztp.deploy_files(self._files(bad), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertTrue(res["preflight_failed"])
        self.assertEqual(res["preflight_reason"], "dhcp-safety")
        self.assertEqual(res["files_written"], [])
        self.assertIn("本次未写入任何文件", res["errors"][0])
        self.assertFalse(os.path.exists(self.conf))
        self.assertEqual(self._tree(), {})

    def test_protected_subnet_pool_is_refused(self):
        bad = ("interface=ens19\nbind-interfaces\n"
               "dhcp-range=10.128.118.150,10.128.118.160,12h\n")
        res = self.ztp.deploy_files(self._files(bad), "")
        self.assertFalse(res["ok"])
        self.assertEqual(res["preflight_reason"], "dhcp-safety")
        self.assertFalse(os.path.exists(self.conf))

    def test_singleton_conflict_is_refused_at_deploy_time(self):
        """落盘前就撞上 port=0 冲突：必须零落盘、且原因里点出冲突文件。"""
        with open(os.path.join(self.confdir, "opstk-pxe.conf"), "w", encoding="utf-8") as fh:
            fh.write("port=0\ninterface=ens19\n")
        res = self.ztp.deploy_files(
            self._files("port=0\n" + self.GOOD_CONF), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertEqual(res["preflight_reason"], "dhcp-safety")
        self.assertIn("opstk-pxe.conf", res["errors"][0])
        self.assertEqual(res["files_written"], [])
        self.assertEqual(self._tree(), {})

    # ── 预检 ──

    def test_preflight_failure_writes_nothing(self):
        self.mock.patch.object(dhcp, "_in_container", lambda: True).start()
        self.mock.patch.object(
            dhcp, "reload_preflight",
            lambda *a, **k: {"ok": False, "skipped": False, "probed": False,
                             "reason": "unit-not-installed", "hint": "测试桩：没装单元",
                             "state": None, "log": ["预检失败：测试桩"]}).start()
        self.addCleanup(self.mock.patch.stopall)
        res = self.ztp.deploy_files(self._files(), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertEqual(res["preflight_reason"], "unit-not-installed")
        self.assertEqual(res["files_written"], [])
        self.assertEqual(self._tree(), {})

    # ── 成功路径：必须等 ZTP 槽位 ──

    def test_success_waits_on_the_ztp_slot(self):
        m = self._delegate([self.WAIT_OK])
        res = self.ztp.deploy_files(self._files(), "t" * 32)
        self.assertTrue(res["ok"], res)
        self.assertEqual(sorted(res["files_written"]),
                         ["tftp/README.txt", "tftp/ztp/SW1.cfg", "web/README.txt", "web/ztp/SW1.cfg"])
        with open(self.conf, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), self.GOOD_CONF)
        self.assertEqual(len(m.call_args_list), 1)
        self.assertEqual(m.call_args_list[0].kwargs.get("state_path"),
                         dhcp.HOST_RELOAD_STATE_ZTP)
        # 不许留下 .tmp 垃圾
        for root in (self.tftp, self.web, self.confdir):
            self.assertEqual([f for f in os.listdir(root) if ".tmp." in f], [])

    # ── 回滚 ──

    def test_rollback_on_pre_restart_failure_restores_bytes(self):
        self._delegate([self.WAIT_OK])
        self.assertTrue(self.ztp.deploy_files(self._files(), "t" * 32)["ok"])
        before = self._tree()

        self._delegate([self.WAIT_TEST_FAILED, self.WAIT_OK])
        res = self.ztp.deploy_files(self._files(tag="SW2"), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(res["files_written"], [])
        self.assertTrue(any("已回滚" in e for e in res["errors"]), res["errors"])
        self.assertEqual(self._tree(), before)

    def test_no_rollback_when_restart_may_have_happened(self):
        self._delegate([self.WAIT_OK])
        self.assertTrue(self.ztp.deploy_files(self._files(), "t" * 32)["ok"])
        before = self._tree()

        self._delegate([self.WAIT_RESTART_FAILED])
        res = self.ztp.deploy_files(self._files(tag="SW2"), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("rolled_back"))
        self.assertTrue(any("未回滚" in e for e in res["errors"]), res["errors"])
        self.assertNotEqual(self._tree(), before)

    def test_stale_fail_does_not_authorize_rollback(self):
        """★ 外部审查 U2-F8：状态槽里**陈旧**的 FAIL 不能证明"这次没重启过"。

        宿主机可能早就因为别的事件重启过 dnsmasq —— 拿一个上次运行留下的 FAIL 去授权
        回滚磁盘，正好会制造那个判据想避免的"磁盘 vs 守护进程更不一致"。
        """
        self._delegate([self.WAIT_OK])
        self.assertTrue(self.ztp.deploy_files(self._files(), "t" * 32)["ok"])
        before = self._tree()

        self._delegate([self.WAIT_STALE_TEST_FAILED])
        res = self.ztp.deploy_files(self._files(tag="SW2"), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("rolled_back"), res)
        self.assertTrue(any("未回滚" in e for e in res["errors"]), res["errors"])
        self.assertNotEqual(self._tree(), before)   # 磁盘保持本次写入的内容（已如实报告）

    def test_config_write_failure_rolls_back_device_files(self):
        """配置写不下去 ⇒ 连设备配置也不该留在盘上（旧代码只 log 一句，还报成功）。"""
        self._delegate([self.WAIT_OK])
        self.mock.patch.object(dhcp, "write_conf", self.mock.Mock(return_value=False)).start()
        self.addCleanup(self.mock.patch.stopall)
        res = self.ztp.deploy_files(self._files(), "t" * 32)
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(self._tree(), {})

    def test_illegal_path_fails_and_rolls_back(self):
        self._delegate([self.WAIT_OK])
        files = self._files()
        files["../../etc/passwd"] = "x\n"
        res = self.ztp.deploy_files(files, "")
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(self._tree(), {})

    def test_without_dnsmasq_conf_no_handshake_needed(self):
        """只有设备配置文件时不该去等重载（没有配置变更）。"""
        pre = self.mock.Mock(return_value={"ok": True, "log": [], "reason": ""})
        self.mock.patch.object(dhcp, "reload_preflight", pre).start()
        self.addCleanup(self.mock.patch.stopall)
        res = self.ztp.deploy_files({"ztp/SW1.cfg": "sysname SW1\n"}, "")
        self.assertFalse(pre.called)
        self.assertTrue(res["ok"])
        self.assertFalse(os.path.exists(self.conf))


class ZtpNtpOptionalTest(unittest.TestCase):
    """NTP 服务器**每个现场都不一样**，所以：模板里没填就**不下发** NTP 行，
    绝不代填一个谁都连不上的默认值（旧默认 10.0.0.254 在任何现场都不可达 ——
    设备反复重试、时间永远不同步，而日志里看不出原因）。
    """

    def _dev_cfg(self, vendor, ntp):
        gen = importlib.import_module("app.ct.ztp.generator")
        p = gen.ZtpProfile(vendor=vendor, admin_password="Test@123", ntp_server=ntp,
                           dhcp_iface="ens19", server_ip="192.168.199.1",
                           snmp_community="Opstk@2026")   # 华为要求 8-32 位（见 test_ztp_baseline）
        files = gen.generate_all(p, [gen.ZtpDevice(hostname="SW1", mac="00:11:22:33:44:55")])
        return files["ztp/SW1.cfg"], files["README.txt"]

    def test_blank_ntp_emits_no_ntp_line(self):
        for vendor, keyword in (("h3c", "ntp-service"), ("huawei", "ntp-service"),
                                ("cisco", "ntp server")):
            cfg, readme = self._dev_cfg(vendor, "")
            self.assertNotIn(keyword, cfg, "%s 在留空时仍下发了 NTP" % vendor)
            self.assertIn("（未配置：生成的设备配置里不下发 NTP）", readme)
            self.assertIn("NTP 说明", readme)

    def test_filled_ntp_is_emitted_verbatim(self):
        for vendor, keyword, line in (
                ("h3c", "ntp-service unicast-peer", "ntp-service unicast-peer 10.9.9.9"),
                ("huawei", "ntp-service unicast-server",
                 "ntp-service unicast-server 10.9.9.9"),
                ("cisco", "ntp server", "ntp server 10.9.9.9")):
            cfg, readme = self._dev_cfg(vendor, "10.9.9.9")
            self.assertIn(line, cfg, "%s 没有下发填好的 NTP" % vendor)
            self.assertIn("10.9.9.9", readme)

    def test_profile_default_is_blank_not_a_fake_address(self):
        gen = importlib.import_module("app.ct.ztp.generator")
        self.assertEqual(gen.ZtpProfile().ntp_server, "")
        schemas = importlib.import_module("app.core.schemas")
        self.assertEqual(schemas.ZtpTemplateIn(name="t").ntp_server, "")


class ZtpZeroTouchTest(unittest.TestCase):
    """**ZTP 免登记也能开局**（RUNBOOK §5.54）。

    实测暴露的两个缺陷：
      1. `generate_all()` 里 `default.cfg` 写在 `if devices:` 里 —— 一台设备都没登记时
         **根本不生成这个文件**，而 dnsmasq 的全局 option 67 一直指向 `ztp/default.cfg`
         ⇒ 设备去取一个不存在的文件，ZTP 完全不可用（部署却报成功）。
      2. 那份 `default.cfg` 还把管理 IP 写死成 `p.dhcp_start` ⇒ 两台以上未登记设备
         **配成同一个地址**（还要和地址池里的租约撞车）。
    正确做法：default.cfg **永远生成**，管理口**走 DHCP 取址**；要"每台各自的管理 IP"
    才需要按 MAC 登记（DHCP 是按 MAC 匹配的）。
    """

    def _gen(self, devices, vendor="h3c", **kw):
        gen = importlib.import_module("app.ct.ztp.generator")
        kw.setdefault("vendor", vendor)
        kw.setdefault("admin_password", "Test@123")
        kw.setdefault("dhcp_iface", "ens19")
        kw.setdefault("server_ip", "192.168.199.1")
        kw.setdefault("dhcp_start", "192.168.199.210")
        kw.setdefault("dhcp_end", "192.168.199.240")
        kw.setdefault("mgmt_gateway", "192.168.199.1")
        # 华为的 SNMP 团体名必须 8-32 位（VRP8/CE 真机要求），默认的 public 只有 6 位、
        # 生成器会 fail-closed 拦下。这里不是测 SNMP，所以给一个合法值。
        kw.setdefault("snmp_community", "Opstk@2026")
        p = gen.ZtpProfile(**kw)
        return gen, gen.generate_all(p, devices)

    def test_default_cfg_always_generated_even_with_zero_devices(self):
        gen, files = self._gen([])
        self.assertIn("ztp/default.cfg", files)
        conf = files["dnsmasq.conf"]
        # option 67 指向它，文件就必须存在（这就是原来那个缺陷）
        self.assertIn('option:bootfile-name,"ztp/default.cfg"', conf)

    def test_zero_touch_default_uses_dhcp_not_a_shared_static_ip(self):
        gen, files = self._gen([])
        cfg = files["ztp/default.cfg"]
        self.assertIn("ip address dhcp-alloc", cfg)
        # 绝不能把地址池的起点写死成管理 IP（多台设备会撞同一个地址）
        self.assertNotIn("192.168.199.210", cfg)
        self.assertNotIn("10.0.0.1", cfg)
        self.assertIn("mgmt=dhcp", cfg)
        # DHCP 取址时不该再写静态默认路由（路由由 DHCP 给）
        self.assertNotIn("ip route-static", cfg)

    def test_each_vendor_supports_dhcp_mgmt(self):
        expect = {"h3c": "ip address dhcp-alloc", "huawei": "ip address dhcp-alloc",
                  "cisco": "ip address dhcp"}
        for vendor, line in expect.items():
            gen, files = self._gen([], vendor=vendor)
            self.assertIn(line, files["ztp/default.cfg"],
                          "%s 的免登记默认配置没有走 DHCP" % vendor)

    def test_registered_device_without_mgmt_ip_also_uses_dhcp(self):
        """先按 MAC 登记、管理 IP 后分配 —— 很常见的顺序，以前会被写死成 10.0.0.1。"""
        g = importlib.import_module("app.ct.ztp.generator")
        _, files = self._gen([g.ZtpDevice(hostname="SW1", mac="00:11:22:33:44:55")])
        cfg = files["ztp/SW1.cfg"]
        self.assertIn("ip address dhcp-alloc", cfg)
        self.assertNotIn("10.0.0.1", cfg)
        self.assertIn('dhcp-host=00:11:22:33:44:55', files["dnsmasq.conf"])

    def test_registered_device_with_mgmt_ip_keeps_static(self):
        g = importlib.import_module("app.ct.ztp.generator")
        _, files = self._gen([g.ZtpDevice(hostname="SW1", mac="00:11:22:33:44:55",
                                          mgmt_ip="192.168.199.30")])
        cfg = files["ztp/SW1.cfg"]
        self.assertIn("ip address 192.168.199.30 255.255.255.0", cfg)
        self.assertIn("ip route-static 0.0.0.0 0 192.168.199.1", cfg)
        self.assertNotIn("dhcp-alloc", cfg)
        self.assertIn("mgmt=192.168.199.30/255.255.255.0", cfg)

    def test_readme_documents_both_delivery_modes(self):
        gen, files = self._gen([])
        r = files["README.txt"]
        self.assertIn("免登记", r)
        self.assertIn("按设备差异化", r)
        self.assertIn("DHCP 是按 **MAC** 匹配的", r)


if __name__ == "__main__":
    unittest.main()
