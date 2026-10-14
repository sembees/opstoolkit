# -*- coding: utf-8 -*-
"""装机网（dnsmasq DHCP 池 / 网关 / DNS）不写死的回归用例。

背景（2026-10-15，用户报「这个 DHCP 应该说支持自定义网段吧，不能说只能固定一个网段」）：
`_dnsmasq` 曾把池硬编码成 `192.168.1.100-192.168.1.200`、网关硬编码成 `192.168.1.1`，
而 `Pxe.vue` 的 buildPayload() **只在 net_mode=static 时才发 net_config**、且从不发
`dhcp_start/dhcp_end/dns_server` ⇒ 界面上选「模式=DHCP」建出来的模板，生成物必然是
192.168.1.x 的池 + `interface=eth0`：池落在本机任何网卡的网段之外，部署会被红线守卫
（`_deploy_redline_check` → `range_inside_binding`）拒掉，而界面上根本没有填池的入口。

本文件钉三件事：
  1. 没显式配置时，池/网关/DNS **跟随 server_ip**（不再 192.168.1.x）；
  2. 显式配置时**以显式值为准**（支持任意自定义网段）；
  3. 推不出来时退回旧默认值（向后兼容），且红线判据**不因推导而放宽**。
"""
import re
import sys

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.it.pxe import server as pxe_server  # noqa: E402
from app.it.pxe.generator import PxeConfig, _dhcp_pool_defaults, generate_all  # noqa: E402

# 口令密文每次盐不同（sha512_crypt）—— 凡是要逐字节比两份产物，必须先掩码，
# 否则必然假红（本项目的老坑，本文件第一版就踩了一次）。
_MASK_RE = re.compile(r"\$6\$[^\s'\"]+")


def _mask(t):
    return _MASK_RE.sub("$6$MASK", t)

BASE = dict(
    os_type="debian", os_version="13.7.0", hostname="h", admin_user="ops",
    admin_password="x", ssh_keys=[], disk_scheme="lvm",
    disk_config={"disk": "sda"}, net_mode="dhcp", net_config={},
    mirror="http://h/repo/", server_ip="192.168.199.1",
    http_root="http://192.168.199.1:8000/pxe/serve/T",
    kernel_path="k", initrd_path="i", deploy_mode="standalone",
    # Ubuntu 家族的 user-data 生成要求"有可成功挂载的安装介质"（casper 需要），
    # 没有它会直接 ValueError —— 本文件要对比三族的应答文件，故给一个占位 ISO。
    iso_url="http://h/iso/x.iso",
)


def dnsmasq(**over):
    kw = dict(BASE)
    kw.update(over)
    return generate_all(PxeConfig(**kw), [])["dnsmasq.conf"]


def line(conf, prefix):
    hits = [l for l in conf.splitlines() if l.startswith(prefix)]
    assert len(hits) == 1, (prefix, hits)
    return hits[0]


class TestPoolDefaults:
    def test_pool_follows_server_ip_when_unset(self):
        """界面选 DHCP 且什么都没填（本次报障的形状）：池必须落在 server_ip 的网段里。"""
        conf = dnsmasq()
        assert line(conf, "dhcp-range=") == "dhcp-range=192.168.199.100,192.168.199.200,12h"
        assert line(conf, "dhcp-option=option:router,") == "dhcp-option=option:router,192.168.199.1"
        assert line(conf, "dhcp-option=option:dns-server,") == \
            "dhcp-option=option:dns-server,192.168.199.1"

    def test_derived_pool_passes_the_redline_binding_check(self):
        """推导出来的默认池必须真的能过红线判据（按 192.168.199.0/24 那张网卡）。"""
        conf = dnsmasq()
        rng = line(conf, "dhcp-range=").split("=", 1)[1].split(",")
        bind = {"interface": "ens19", "ip": "192.168.199.1",
                "netmask": "255.255.255.0", "prefixlen": 24}
        assert pxe_server.range_inside_binding(rng[0], rng[1], bind) is True

    def test_explicit_values_win_any_segment(self):
        """运维显式给任意网段（这里 10.99.0.0/24）⇒ 以显式值为准。"""
        conf = dnsmasq(server_ip="10.99.0.1", net_config={
            "dhcp_start": "10.99.0.50", "dhcp_end": "10.99.0.150",
            "dns_server": "10.99.0.53"})
        assert line(conf, "dhcp-range=") == "dhcp-range=10.99.0.50,10.99.0.150,12h"
        assert line(conf, "dhcp-option=option:router,") == "dhcp-option=option:router,10.99.0.1"
        assert line(conf, "dhcp-option=option:dns-server,") == \
            "dhcp-option=option:dns-server,10.99.0.53"

    def test_explicit_gateway_and_partial_pool(self):
        """只给一半也能用：给了起止之一，另一头仍按 server_ip 推；网关可单独覆盖。"""
        conf = dnsmasq(net_config={"dhcp_start": "192.168.199.30", "gateway": "192.168.199.254"})
        assert line(conf, "dhcp-range=") == "dhcp-range=192.168.199.30,192.168.199.200,12h"
        assert line(conf, "dhcp-option=option:router,") == \
            "dhcp-option=option:router,192.168.199.254"
        # DNS 没给 ⇒ 跟随（被覆盖后的）网关
        assert line(conf, "dhcp-option=option:dns-server,") == \
            "dhcp-option=option:dns-server,192.168.199.254"

    def test_empty_strings_fall_back_not_emitted_blank(self):
        """空串按"没填"处理（旧实现用 dict.get(k, default)，给了空串就会发出空值）。"""
        conf = dnsmasq(net_config={"dhcp_start": "", "dhcp_end": "", "dns_server": ""})
        assert line(conf, "dhcp-range=") == "dhcp-range=192.168.199.100,192.168.199.200,12h"
        assert line(conf, "dhcp-option=option:dns-server,") == \
            "dhcp-option=option:dns-server,192.168.199.1"

    def test_missing_or_invalid_server_ip_keeps_legacy_defaults(self):
        """推不出来时退回旧默认值（向后兼容），且不抛异常。"""
        for bad in ("", "   ", "not-an-ip", "192.168.199", "999.1.1.1", "fe80::1"):
            conf = dnsmasq(server_ip=bad)
            assert line(conf, "dhcp-range=") == "dhcp-range=192.168.1.100,192.168.1.200,12h", bad
            assert line(conf, "dhcp-option=option:router,") == \
                "dhcp-option=option:router,192.168.1.1", bad

    def test_helper_contract(self):
        assert _dhcp_pool_defaults("192.168.199.1") == ("192.168.199.100", "192.168.199.200")
        assert _dhcp_pool_defaults("10.0.0.1") == ("10.0.0.100", "10.0.0.200")
        assert _dhcp_pool_defaults("") == ("192.168.1.100", "192.168.1.200")
        assert _dhcp_pool_defaults(None) == ("192.168.1.100", "192.168.1.200")


class TestAnswerFilesUnaffected:
    """DHCP 模式下也下发装机网字段，绝不能改变"被装机器"的应答文件。"""

    def test_dhcp_mode_answer_files_ignore_server_side_keys(self):
        plain = dict(BASE, net_config={})
        extra = dict(BASE, net_config={
            "dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200",
            "dns_server": "192.168.199.1", "gateway": "192.168.199.1"})
        for os_type, key in (("rocky", "ks.cfg"), ("ubuntu", "user-data"), ("debian", "preseed.cfg")):
            a = generate_all(PxeConfig(**dict(plain, os_type=os_type)), {})[key]
            b = generate_all(PxeConfig(**dict(extra, os_type=os_type)), {})[key]
            assert _mask(a) == _mask(b), "%s 的 %s 被装机网字段影响了" % (os_type, key)

    def test_static_mode_still_uses_client_fields(self):
        """static 模式下客户端字段照旧进 preseed（回归红线：不能把两组字段搅在一起）。"""
        conf = generate_all(PxeConfig(**dict(
            BASE, os_type="debian", net_mode="static",
            net_config={"interface": "ens18", "ip": "192.168.199.140",
                        "netmask": "255.255.255.0", "gateway": "192.168.199.1",
                        "dns": ["192.168.199.1"],
                        "dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200"})), {})
        ps = conf["preseed.cfg"]
        assert "d-i netcfg/get_ipaddress string 192.168.199.140" in ps
        assert "d-i netcfg/choose_interface select ens18" in ps
        assert "dhcp-range=192.168.199.100,192.168.199.200,12h" in conf["dnsmasq.conf"]
