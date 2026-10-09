"""Debian preseed 家族用例（2026-10-09）。

锁住四件事：
1. `_debian_preseed(c)` 的内容（关键 d-i 指令、静态网络、分区两态、回调、crypted 口令）；
2. 家族分发：debian → `preseed.cfg`（不再走 kickstart/ubuntu 的产物名）；
3. iPXE 内核参数：`auto=true priority=critical interface=… netcfg/disable_autoconfig=true url=…`；
4. `os_catalog` 的 debian 已打开 `auto_install`（不再命中"不支持自动装机"的拒绝分支）。

另外**安全断言**：生成物里不得出现明文管理员口令（只允许 crypted 形态）。
"""
import sys

import pytest

sys.path.insert(0, __file__.rsplit("tests", 1)[0]) if False else None

from app.it.pxe import os_catalog
from app.it.pxe.generator import PxeConfig, _debian_preseed, generate_all

PLAIN_PW = "Debian!Pxe2026#ops"
DONE = "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz"
BASE_KW = dict(
    os_type="debian", os_version="13.7.0", hostname="pxe140-debian",
    timezone="Asia/Shanghai", locale="en_US.UTF-8", keyboard="us",
    admin_user="ops", admin_password=PLAIN_PW, root_password="",
    ssh_keys=["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey e2e@opstk"],
    allow_root=False, sudo_nopasswd=True, disk_scheme="lvm", disk_config={"disk": "sda"},
    net_mode="static",
    net_config={"ip": "192.168.199.140", "netmask": "255.255.255.0", "gateway": "192.168.199.1",
                "dns": ["192.168.199.1"], "interface": "ens18", "dns_server": "192.168.199.1",
                "dhcp_start": "192.168.199.100", "dhcp_end": "192.168.199.200"},
    extra_packages=["vim"], post_script="",
    mirror="http://192.168.199.1:8000/pxe/serve/T/repo/debian-13.7.0/",
    server_ip="192.168.199.1", http_root="http://192.168.199.1:8000/pxe/serve/T",
    kernel_path="debian/13.7.0/vmlinuz", initrd_path="debian/13.7.0/initrd",
    done_url=DONE,
)


def _cfg(**over):
    kw = dict(BASE_KW)
    kw.update(over)
    return PxeConfig(**kw)


class TestDebianPreseedContent:
    def test_key_directives(self):
        ps = _debian_preseed(_cfg())
        for d in ("d-i debian-installer/locale string en_US.UTF-8",
                  "d-i keyboard-configuration/xkb-keymap select us",
                  "d-i time/zone string Asia/Shanghai",
                  "d-i netcfg/choose_interface select ens18",
                  "d-i netcfg/disable_autoconfig boolean true",
                  "d-i netcfg/get_ipaddress string 192.168.199.140",
                  "d-i netcfg/get_gateway string 192.168.199.1",
                  "d-i netcfg/get_nameservers string 192.168.199.1",
                  "d-i mirror/http/hostname string 192.168.199.1:8000",
                  "d-i mirror/http/directory string /pxe/serve/T/repo/debian-13.7.0/",
                  "d-i passwd/username string ops",
                  "d-i partman-auto/disk string /dev/sda",
                  "popularity-contest popularity-contest/participate boolean false",
                  "d-i pkgsel/include string vim openssh-server sudo",
                  "tasksel tasksel/first multiselect standard, ssh-server"):
            assert d in ps, "缺指令: " + d
        assert ps.startswith("#_preseed_V1"), "preseed 首行必须是版本标记"

    def test_disk_scheme_two_states(self):
        assert "d-i partman-auto/method string lvm" in _debian_preseed(_cfg(disk_scheme="lvm"))
        assert "d-i partman-auto/method string regular" in _debian_preseed(_cfg(disk_scheme="direct"))

    def test_done_callback_and_sudo_and_keys(self):
        ps = _debian_preseed(_cfg())
        assert "preseed/late_command" in ps
        assert DONE in ps, "装完回调 URL 必须进 late_command"
        assert "90-opstk-ops" in ps and "NOPASSWD:ALL" in ps
        assert "authorized_keys" in ps and "AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey" in ps

    def test_no_plaintext_password(self):
        ps = _debian_preseed(_cfg())
        assert PLAIN_PW not in ps, "生成物里绝不能出现明文口令"
        assert "passwd/user-password-crypted password $6$" in ps


class TestDebianWiring:
    INST = [{"id": "abc", "mac": "bc-24-11-66-8b-ea", "ip": "192.168.199.140",
             "hostname": "pxe140-debian", "status": "pending"}]

    def test_generate_all_emits_preseed_not_ks(self):
        files = generate_all(_cfg(), self.INST)
        assert "preseed.cfg" in files
        assert "ks.cfg" not in files and "user-data" not in files

    def test_ipxe_kernel_args(self):
        boot = generate_all(_cfg(), self.INST)["boot.ipxe"]
        kl = [l for l in boot.splitlines() if l.startswith("kernel ")]
        assert kl, "boot.ipxe 必须有 kernel 行"
        line = kl[0]
        assert "auto=true" in line and "priority=critical" in line
        assert "interface=ens18" in line and "netcfg/disable_autoconfig=true" in line
        assert "url=http://192.168.199.1:8000/pxe/serve/T/preseed.cfg" in line
        assert "debian/13.7.0/vmlinuz" in line and "initrd=initrd" in line

    def test_catalog_auto_install_enabled(self):
        e = os_catalog.entry("debian")
        assert e is not None and e.auto_install is True
