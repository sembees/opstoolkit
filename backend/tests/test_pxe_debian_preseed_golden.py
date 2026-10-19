# -*- coding: utf-8 -*-
"""Debian preseed 的字节级 golden 快照（_debian_preseed）。

与 backend/tests/test_pxe.py::PxeDiskRegressionTest::test_legacy_full_files_byte_identical_golden
同一条纪律、同一套掩码约定：sha512_crypt 每次盐不同，先 mask 密文，再与内联期望
**逐字节**比对；另附 sha256 双重锁（同 Ubuntu/RHEL golden 的风格）。生成物任何
一个字节变化（哪怕只是注释行），这里必红。

固定输入（与 backend/tests/test_pxe_debian_preseed.py 的 BASE_KW 同一份）：
static+lvm 必测；dhcp / direct 按参数化补齐，各一份内联期望。

重新基线：用装了 passlib 的解释器跑
    python mimo/draft/debian_docs_golden.py gen --case <case> --src <backend 的上级目录>
逐行核对 diff 后更新 GOLDEN，并沿用本仓 golden 先例的注释惯例记录"哪些行变了、为什么"。
重新基线历史：
  · 2026-10-14（初版）：基线取自 b2863cc 之前的实现（内网代工内嵌的那份已过期 ——
    那时 preseed/late_command 的装完回调还是"目标系统外 curl"）。落地时按**当前**
    实现现算，掩码后 sha256: static_lvm 67f877dd… / dhcp_lvm f911b122… / static_direct 6c8eb940…
  · 2026-10-14（真机回归后 · 第一次）：_debian_preseed 增加
    `d-i netcfg/hostname string <主机名>`（当时以为它才是落盘用的键）。
    行数各 **+1**（68→69 / 62→63 / 65→66）。
  · 2026-10-14（真机回归后 · 第二次）：把主机名的**真正落地点**改到
    `preseed/late_command`（写 /target/etc/hostname 与 /target/etc/hosts）。
    原因：netcfg/hostname 虽然模板自称 "can be preseeded"，但 netcfg 1.197 的
    netcfg.c / netcfg-static.c 主流程**根本不读它**，而 netcfg_write_common() 在
    hostname 为空时直接 return —— 两轮真机（含下发 netcfg/hostname 的那轮）装完
    /etc/hostname 恒为 debian。行数不变（69/63/66），只有 late_command 那行内容变长。

基线生成时间：2026-10-14 23:52
"""
import hashlib
import re

import pytest

from app.it.pxe.generator import PxeConfig, _debian_preseed

# 掩码约定与 backend/tests/test_pxe.py:792-794 的 _mask_pw 逐字同源：
# $6$ 之后到空白/引号为止的整段密文（盐随机）替换成 $6$MASK，其余字节一律参与比对。
_MASK_RE = re.compile(r"\$6\$[^\s'\"]+")


def _mask_pw(text):
    return _MASK_RE.sub("$6$MASK", text)


BASE_KW = dict(
    os_type='debian',
    os_version='13.7.0',
    hostname='pxe140-debian',
    timezone='Asia/Shanghai',
    locale='en_US.UTF-8',
    keyboard='us',
    admin_user='ops',
    admin_password='Debian!Pxe2026#ops',
    root_password='',
    ssh_keys=['ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey e2e@opstk'],
    allow_root=False,
    sudo_nopasswd=True,
    disk_scheme='lvm',
    disk_config={'disk': 'sda'},
    net_mode='static',
    net_config={'ip': '192.168.199.140', 'netmask': '255.255.255.0', 'gateway': '192.168.199.1', 'dns': ['192.168.199.1'], 'interface': 'ens18', 'dns_server': '192.168.199.1', 'dhcp_start': '192.168.199.100', 'dhcp_end': '192.168.199.200'},
    extra_packages=['vim'],
    post_script='',
    mirror='http://192.168.199.1:8000/pxe/serve/T/repo/debian-13.7.0/',
    server_ip='192.168.199.1',
    http_root='http://192.168.199.1:8000/pxe/serve/T',
    kernel_path='debian/13.7.0/vmlinuz',
    initrd_path='debian/13.7.0/initrd',
    done_url='http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz',
)


def _cfg(**over):
    kw = dict(BASE_KW)
    kw.update(over)
    return PxeConfig(**kw)


CASE_OVERRIDES = {
    "static_lvm": {},
    "dhcp_lvm": {"net_mode": "dhcp"},
    "static_direct": {"disk_scheme": "direct"},
}

# 每个用例：lines = 掩码后的期望 preseed（逐行；最后 append 空串保证以 \n 结尾），
# sha256 = sha256(掩码文本)，n_lines = len(text.split("\n"))。三重一致才算过。
GOLDEN = {
    "static_lvm": {
        "lines": (
            '#_preseed_V1',
            '# 由 OpsToolkit 生成（Debian d-i preseed）',
            '',
            '### 本地化与时钟',
            'd-i debian-installer/locale string en_US.UTF-8',
            'd-i keyboard-configuration/xkb-keymap select us',
            'd-i time/zone string Asia/Shanghai',
            'd-i clock-setup/utc boolean true',
            '',
            '### 网络（静态；iPXE 侧必须同时给 interface=ens18）',
            'd-i netcfg/choose_interface select ens18',
            'd-i netcfg/disable_autoconfig boolean true',
            'd-i netcfg/get_ipaddress string 192.168.199.140',
            'd-i netcfg/get_netmask string 255.255.255.0',
            'd-i netcfg/get_gateway string 192.168.199.1',
            'd-i netcfg/get_nameservers string 192.168.199.1',
            'd-i netcfg/confirm_static boolean true',
            'd-i netcfg/get_hostname string pxe140-debian',
            'd-i netcfg/hostname string pxe140-debian',
            'd-i netcfg/get_domain string localdomain',
            '',
            '### 包源（本地 HTTP 仓库，离线装机用）',
            'd-i mirror/country string manual',
            'd-i mirror/protocol string http',
            'd-i mirror/http/hostname string 192.168.199.1:8000',
            'd-i mirror/http/directory string /pxe/serve/T/repo/debian-13.7.0/',
            'd-i mirror/http/proxy string',
            'd-i mirror/suite string trixie',
            'd-i mirror/udeb/suite string trixie',
            'd-i apt-setup/services-select multiselect updates',
            '',
            '### 账户',
            'd-i passwd/root-login boolean false',
            'd-i passwd/make-user boolean true',
            'd-i passwd/user-fullname string ops',
            'd-i passwd/username string ops',
            'd-i passwd/user-password-crypted password $6$MASK',
            'd-i passwd/user-default-groups string audio cdrom video sudo',
            '',
            '### 分区',
            'd-i partman-auto/method string lvm',
            'd-i partman-lvm/device_remove_lvm boolean true',
            'd-i partman-lvm/confirm boolean true',
            'd-i partman-lvm/confirm_nooverwrite boolean true',
            'd-i partman-md/device_remove_md boolean true',
            'd-i partman-md/confirm boolean true',
            'd-i partman-auto/disk string /dev/sda',
            'd-i partman-auto-lvm/guided_size string max',
            'd-i partman/early_command string wipefs -a /dev/sda 2>/dev/null || true; dd if=/dev/zero of=/dev/sda bs=1M count=200 conv=notrunc; _SZ=$(blockdev --getsz /dev/sda 2>/dev/null || echo 0); [ "$_SZ" -gt 409600 ] && dd if=/dev/zero of=/dev/sda bs=512 seek=$((_SZ-204800)) count=204800 conv=notrunc; true',
            'd-i partman-auto/choose_recipe select atomic',
            'd-i partman/choose_partition select finish',
            'd-i partman/confirm boolean true',
            'd-i partman/confirm_nooverwrite boolean true',
            'd-i partman-partitioning/confirm_write_new_label boolean true',
            '',
            '### 引导',
            'd-i grub-installer/only_debian boolean true',
            'd-i grub-installer/bootdev string /dev/sda',
            '',
            '### 软件选择',
            'tasksel tasksel/first multiselect standard, ssh-server',
            'd-i pkgsel/include string vim openssh-server sudo',
            'd-i pkgsel/upgrade select none',
            'popularity-contest popularity-contest/participate boolean false',
            '',
            '### 收尾',
            'd-i finish-install/reboot_in_progress note',
            'd-i preseed/late_command string printf \'%s\\n\' pxe140-debian > /target/etc/hostname && printf \'127.0.0.1\\tlocalhost\\n%s\\t%s\\n\\n# The following lines are desirable for IPv6 capable hosts\\n::1     localhost ip6-localhost ip6-loopback\\nff02::1 ip6-allnodes\\nff02::2 ip6-allrouters\\n\' 192.168.199.140 pxe140-debian > /target/etc/hosts; mkdir -p /target/home/ops/.ssh && chmod 700 /target/home/ops/.ssh && printf \'%s\\n\' \'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey e2e@opstk\' >> /target/home/ops/.ssh/authorized_keys && chmod 600 /target/home/ops/.ssh/authorized_keys && chown -R 1000:1000 /target/home/ops/.ssh; echo \'ops ALL=(ALL) NOPASSWD:ALL\' > /target/etc/sudoers.d/90-opstk-ops && chmod 440 /target/etc/sudoers.d/90-opstk-ops; in-target sh -c \'command -v curl >/dev/null 2>&1 || apt-get -y -q install --no-install-recommends curl >/dev/null 2>&1 || true; for i in 1 2 3; do curl -s -m 10 "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; sleep 3; done; true\'; for i in 1 2 3; do wget -q -O- "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; curl -s -m 10 "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; sleep 3; done; true',
            '',
        ),
        "sha256": "3aae7ca3694c4716843dfa3bf61fbc3f6ec6b0e3d3040f17f3982e2b7fb5ddad",
        "n_lines": 69,
    },
    "dhcp_lvm": {
        "lines": (
            '#_preseed_V1',
            '# 由 OpsToolkit 生成（Debian d-i preseed）',
            '',
            '### 本地化与时钟',
            'd-i debian-installer/locale string en_US.UTF-8',
            'd-i keyboard-configuration/xkb-keymap select us',
            'd-i time/zone string Asia/Shanghai',
            'd-i clock-setup/utc boolean true',
            '',
            '### 网络（DHCP；iPXE 侧必须同时给 interface=ens18）',
            'd-i netcfg/choose_interface select ens18',
            'd-i netcfg/get_hostname string pxe140-debian',
            'd-i netcfg/hostname string pxe140-debian',
            'd-i netcfg/get_domain string localdomain',
            '',
            '### 包源（本地 HTTP 仓库，离线装机用）',
            'd-i mirror/country string manual',
            'd-i mirror/protocol string http',
            'd-i mirror/http/hostname string 192.168.199.1:8000',
            'd-i mirror/http/directory string /pxe/serve/T/repo/debian-13.7.0/',
            'd-i mirror/http/proxy string',
            'd-i mirror/suite string trixie',
            'd-i mirror/udeb/suite string trixie',
            'd-i apt-setup/services-select multiselect updates',
            '',
            '### 账户',
            'd-i passwd/root-login boolean false',
            'd-i passwd/make-user boolean true',
            'd-i passwd/user-fullname string ops',
            'd-i passwd/username string ops',
            'd-i passwd/user-password-crypted password $6$MASK',
            'd-i passwd/user-default-groups string audio cdrom video sudo',
            '',
            '### 分区',
            'd-i partman-auto/method string lvm',
            'd-i partman-lvm/device_remove_lvm boolean true',
            'd-i partman-lvm/confirm boolean true',
            'd-i partman-lvm/confirm_nooverwrite boolean true',
            'd-i partman-md/device_remove_md boolean true',
            'd-i partman-md/confirm boolean true',
            'd-i partman-auto/disk string /dev/sda',
            'd-i partman-auto-lvm/guided_size string max',
            'd-i partman/early_command string wipefs -a /dev/sda 2>/dev/null || true; dd if=/dev/zero of=/dev/sda bs=1M count=200 conv=notrunc; _SZ=$(blockdev --getsz /dev/sda 2>/dev/null || echo 0); [ "$_SZ" -gt 409600 ] && dd if=/dev/zero of=/dev/sda bs=512 seek=$((_SZ-204800)) count=204800 conv=notrunc; true',
            'd-i partman-auto/choose_recipe select atomic',
            'd-i partman/choose_partition select finish',
            'd-i partman/confirm boolean true',
            'd-i partman/confirm_nooverwrite boolean true',
            'd-i partman-partitioning/confirm_write_new_label boolean true',
            '',
            '### 引导',
            'd-i grub-installer/only_debian boolean true',
            'd-i grub-installer/bootdev string /dev/sda',
            '',
            '### 软件选择',
            'tasksel tasksel/first multiselect standard, ssh-server',
            'd-i pkgsel/include string vim openssh-server sudo',
            'd-i pkgsel/upgrade select none',
            'popularity-contest popularity-contest/participate boolean false',
            '',
            '### 收尾',
            'd-i finish-install/reboot_in_progress note',
            'd-i preseed/late_command string printf \'%s\\n\' pxe140-debian > /target/etc/hostname && printf \'127.0.0.1\\tlocalhost\\n%s\\t%s\\n\\n# The following lines are desirable for IPv6 capable hosts\\n::1     localhost ip6-localhost ip6-loopback\\nff02::1 ip6-allnodes\\nff02::2 ip6-allrouters\\n\' 127.0.1.1 pxe140-debian > /target/etc/hosts; mkdir -p /target/home/ops/.ssh && chmod 700 /target/home/ops/.ssh && printf \'%s\\n\' \'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey e2e@opstk\' >> /target/home/ops/.ssh/authorized_keys && chmod 600 /target/home/ops/.ssh/authorized_keys && chown -R 1000:1000 /target/home/ops/.ssh; echo \'ops ALL=(ALL) NOPASSWD:ALL\' > /target/etc/sudoers.d/90-opstk-ops && chmod 440 /target/etc/sudoers.d/90-opstk-ops; in-target sh -c \'command -v curl >/dev/null 2>&1 || apt-get -y -q install --no-install-recommends curl >/dev/null 2>&1 || true; for i in 1 2 3; do curl -s -m 10 "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; sleep 3; done; true\'; for i in 1 2 3; do wget -q -O- "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; curl -s -m 10 "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; sleep 3; done; true',
            '',
        ),
        "sha256": "da3fdbb6bee60b5eb5c2a397069ec8f9ca21ec3e46d033d8d21c0cfc7b274ef9",
        "n_lines": 63,
    },
    "static_direct": {
        "lines": (
            '#_preseed_V1',
            '# 由 OpsToolkit 生成（Debian d-i preseed）',
            '',
            '### 本地化与时钟',
            'd-i debian-installer/locale string en_US.UTF-8',
            'd-i keyboard-configuration/xkb-keymap select us',
            'd-i time/zone string Asia/Shanghai',
            'd-i clock-setup/utc boolean true',
            '',
            '### 网络（静态；iPXE 侧必须同时给 interface=ens18）',
            'd-i netcfg/choose_interface select ens18',
            'd-i netcfg/disable_autoconfig boolean true',
            'd-i netcfg/get_ipaddress string 192.168.199.140',
            'd-i netcfg/get_netmask string 255.255.255.0',
            'd-i netcfg/get_gateway string 192.168.199.1',
            'd-i netcfg/get_nameservers string 192.168.199.1',
            'd-i netcfg/confirm_static boolean true',
            'd-i netcfg/get_hostname string pxe140-debian',
            'd-i netcfg/hostname string pxe140-debian',
            'd-i netcfg/get_domain string localdomain',
            '',
            '### 包源（本地 HTTP 仓库，离线装机用）',
            'd-i mirror/country string manual',
            'd-i mirror/protocol string http',
            'd-i mirror/http/hostname string 192.168.199.1:8000',
            'd-i mirror/http/directory string /pxe/serve/T/repo/debian-13.7.0/',
            'd-i mirror/http/proxy string',
            'd-i mirror/suite string trixie',
            'd-i mirror/udeb/suite string trixie',
            'd-i apt-setup/services-select multiselect updates',
            '',
            '### 账户',
            'd-i passwd/root-login boolean false',
            'd-i passwd/make-user boolean true',
            'd-i passwd/user-fullname string ops',
            'd-i passwd/username string ops',
            'd-i passwd/user-password-crypted password $6$MASK',
            'd-i passwd/user-default-groups string audio cdrom video sudo',
            '',
            '### 分区',
            'd-i partman-auto/method string regular',
            'd-i partman-md/device_remove_md boolean true',
            'd-i partman-md/confirm boolean true',
            'd-i partman-auto/disk string /dev/sda',
            'd-i partman-auto-lvm/guided_size string max',
            'd-i partman/early_command string wipefs -a /dev/sda 2>/dev/null || true; dd if=/dev/zero of=/dev/sda bs=1M count=200 conv=notrunc; _SZ=$(blockdev --getsz /dev/sda 2>/dev/null || echo 0); [ "$_SZ" -gt 409600 ] && dd if=/dev/zero of=/dev/sda bs=512 seek=$((_SZ-204800)) count=204800 conv=notrunc; true',
            'd-i partman-auto/choose_recipe select atomic',
            'd-i partman/choose_partition select finish',
            'd-i partman/confirm boolean true',
            'd-i partman/confirm_nooverwrite boolean true',
            'd-i partman-partitioning/confirm_write_new_label boolean true',
            '',
            '### 引导',
            'd-i grub-installer/only_debian boolean true',
            'd-i grub-installer/bootdev string /dev/sda',
            '',
            '### 软件选择',
            'tasksel tasksel/first multiselect standard, ssh-server',
            'd-i pkgsel/include string vim openssh-server sudo',
            'd-i pkgsel/upgrade select none',
            'popularity-contest popularity-contest/participate boolean false',
            '',
            '### 收尾',
            'd-i finish-install/reboot_in_progress note',
            'd-i preseed/late_command string printf \'%s\\n\' pxe140-debian > /target/etc/hostname && printf \'127.0.0.1\\tlocalhost\\n%s\\t%s\\n\\n# The following lines are desirable for IPv6 capable hosts\\n::1     localhost ip6-localhost ip6-loopback\\nff02::1 ip6-allnodes\\nff02::2 ip6-allrouters\\n\' 192.168.199.140 pxe140-debian > /target/etc/hosts; mkdir -p /target/home/ops/.ssh && chmod 700 /target/home/ops/.ssh && printf \'%s\\n\' \'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleKey e2e@opstk\' >> /target/home/ops/.ssh/authorized_keys && chmod 600 /target/home/ops/.ssh/authorized_keys && chown -R 1000:1000 /target/home/ops/.ssh; echo \'ops ALL=(ALL) NOPASSWD:ALL\' > /target/etc/sudoers.d/90-opstk-ops && chmod 440 /target/etc/sudoers.d/90-opstk-ops; in-target sh -c \'command -v curl >/dev/null 2>&1 || apt-get -y -q install --no-install-recommends curl >/dev/null 2>&1 || true; for i in 1 2 3; do curl -s -m 10 "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; sleep 3; done; true\'; for i in 1 2 3; do wget -q -O- "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; curl -s -m 10 "http://192.168.199.1:8000/api/it/pxe/installs/abc/done?t=xyz" >/dev/null 2>&1 && break; sleep 3; done; true',
            '',
        ),
        "sha256": "70ae70ecd5b1ef0607ff585c097d815ab629f7548c3e9a2fd27b28e3300260f2",
        "n_lines": 66,
    },
}


def _first_diff(a, b):
    for i in range(min(len(a), len(b))):
        if a[i] != b[i]:
            return i
    return min(len(a), len(b)) if len(a) != len(b) else -1


@pytest.mark.parametrize("case", list(GOLDEN))
def test_debian_preseed_byte_identical_golden(case):
    """固定输入 → 掩码后与内联期望逐字节相等 + sha256 相等 + 行数相等。"""
    ps = _debian_preseed(_cfg(**CASE_OVERRIDES[case]))
    got = _mask_pw(ps)
    # 行列表末元素是空串 ⇒ join 结果本身就以单个 \n 结尾（与生成器一致），不再追加。
    exp = "\n".join(GOLDEN[case]["lines"])
    if got != exp:
        i = _first_diff(got, exp)
        ln = exp.count("\n", 0, i) + 1
        ctx = (exp.split("\n")[ln - 1] if 0 < ln <= exp.count("\n") + 1 else "")[:120]
        raise AssertionError(
            "preseed 与 golden 逐字节不一致（case=%s）：第 %d 个字节、期望第 %d 行\n"
            "  期望行: %r\n  改动了 _debian_preseed 就按本文件头说明重新基线；"
            "不要无脑改期望值。" % (case, i, ln, ctx)
        )
    assert hashlib.sha256(got.encode()).hexdigest() == GOLDEN[case]["sha256"], case
    assert len(got.split("\n")) == GOLDEN[case]["n_lines"], case
