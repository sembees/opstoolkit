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
                  "d-i mirror/suite string trixie",
                  "d-i mirror/udeb/suite string trixie",
                  "d-i apt-setup/services-select multiselect updates",
                  "d-i passwd/username string ops",
                  "d-i partman-auto/disk string /dev/sda",
                  "d-i partman-auto-lvm/guided_size string max",
                  "d-i partman/early_command string wipefs -a /dev/sda 2>/dev/null || true;",
                  "d-i partman-partitioning/confirm_write_new_label boolean true",
                  "d-i partman-md/confirm boolean true",
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
    # MAC 必须用生成器认的冒号形态：_MAC_RE = ^[0-9a-f]{2}(:[0-9a-f]{2}){5}$ 只认冒号
    # （_safe_mac 先 lower 再 fullmatch，大小写均可）；连字符 MAC 会被 generate_all 的
    # `if not mac: continue` 跳过 —— per-MAC 分支此前从未被本用例走到过。
    # <tag> = _mac_tag(_safe_mac(mac))：小写 MAC 把冒号换成连字符（generator._mac_tag），
    # 每机应答文件键即 files["preseed/<tag>/preseed.cfg"]（generator.generate_all）。
    INST = [{"id": "abc", "mac": "BC:24:11:66:8B:EA", "ip": "192.168.199.140",
             "hostname": "pxe140-debian", "status": "pending"}]
    TAG = "bc-24-11-66-8b-ea"  # = _mac_tag(_safe_mac("BC:24:11:66:8B:EA"))

    def test_generate_all_emits_preseed_not_ks(self):
        files = generate_all(_cfg(), self.INST)
        assert "preseed.cfg" in files
        assert "ks.cfg" not in files and "user-data" not in files
        assert "preseed/" + self.TAG + "/preseed.cfg" in files, \
            "合法 MAC 必须走到 per-MAC 分支：files 里要有 preseed/<tag>/preseed.cfg"

    def test_ipxe_kernel_args(self):
        # INST 里有合法 MAC ⇒ generate_all 的模板级 boot.ipxe 按 has_registered 分支
        # 切换成"未登记机器拒绝装机"安全菜单（无 kernel 行，generator 有意为之）；
        # 真正带 netcfg 内核参数的是每机菜单 boot/<tag>.ipxe —— 在这里断言它：
        files = generate_all(_cfg(), self.INST)
        boot = files["boot/" + self.TAG + ".ipxe"]
        kl = [l for l in boot.splitlines() if l.startswith("kernel ")]
        assert kl, "每机 boot/<tag>.ipxe 必须有 kernel 行"
        line = kl[0]
        assert "auto=true" in line and "priority=critical" in line
        assert "interface=ens18" in line and "netcfg/disable_autoconfig=true" in line
        # 静态网络必须同时在内核命令行（否则 preseed 取不到 -> 网络死锁，真机实证 2026-10-09）
        assert "netcfg/get_ipaddress=192.168.199.140" in line
        assert "netcfg/get_netmask=255.255.255.0" in line
        assert "netcfg/get_gateway=192.168.199.1" in line
        assert "netcfg/get_nameservers=192.168.199.1" in line
        assert "netcfg/confirm_static=true" in line
        # 不带裸 url=（真机两次实证：带了它连 preseed 都不取）；只允许 preseed/url=
        assert " url=" not in line
        assert "preseed/url=http://" in line
        assert ("preseed/url=http://192.168.199.1:8000/pxe/serve/T/preseed/"
                + self.TAG + "/preseed.cfg") in line, "必须是 preseed/url=（url= 会被 d-i 当成介质位置，真机实证 2026-10-09）"
        assert "debian/13.7.0/vmlinuz" in line and "initrd=initrd" in line

    def test_catalog_auto_install_enabled(self):
        e = os_catalog.entry("debian")
        assert e is not None and e.auto_install is True


# ══ 三缺口补齐用例（草稿，2026-10）══════════════════════════════════════════
# 对应 generator._debian_preseed 的三处新能力：
#   1) c.root_password：allow_root=True 时发 passwd/root-password-crypted（crypted）
#   2) c.extra_repos：apt-setup/localN/repository|comment|source
#   3) c.post_script：进 preseed/late_command，先落 /target/tmp 再 in-target bash 执行，
#      且与 done_url 回调共存（顺序：公钥/sudo → post_script → 回调）
class TestDebianPreseedGaps:
    ROOT_PW = "RootPw!Deb2026"

    def test_root_password_crypted_only_when_allow_root(self):
        # 默认（allow_root=False）不发 root 口令键：d-i 此时跳过 root 账户口令，发了也不生效
        assert "passwd/root-password-crypted" not in _debian_preseed(_cfg())
        ps = _debian_preseed(_cfg(allow_root=True, root_password=self.ROOT_PW))
        root = [l for l in ps.splitlines()
                if l.startswith("d-i passwd/root-password-crypted password ")]
        assert len(root) == 1, "allow_root=True 必须恰好一条 root 口令指令"
        h = root[0].split("password ", 1)[1]
        assert h.startswith("$6$"), "root 口令必须是 sha512 crypted 形态"
        assert self.ROOT_PW not in ps, "明文 root 口令绝不进生成物"

    def test_root_password_fallback_chain(self):
        # 与 RHEL rootpw / Ubuntu chpasswd -e 同一条回退链：root_password 空 → 用管理员口令做根口令。
        # 注意 _hash_pw 每次调用盐不同，密文互不相等 —— 只能按明文验证（passlib.verify）。
        from passlib.hash import sha512_crypt
        ps = _debian_preseed(_cfg(allow_root=True, root_password=""))
        r = [l for l in ps.splitlines()
             if l.startswith("d-i passwd/root-password-crypted password ")][0]
        h = r.split("password ", 1)[1]
        assert h.startswith("$6$")
        assert sha512_crypt.verify(PLAIN_PW, h), "root 密文必须能被管理员明文验证（回退链）"

    def test_extra_repos_apt_setup_local(self):
        ps = _debian_preseed(_cfg(extra_repos=[
            {"name": "internal", "url": "http://192.168.199.1:8000/apt/internal"}]))
        assert ("d-i apt-setup/local0/repository string "
                "http://192.168.199.1:8000/apt/internal trixie main") in ps
        assert "d-i apt-setup/local0/comment string internal" in ps
        assert "d-i apt-setup/local0/source boolean false" in ps

    def test_extra_repos_string_url_and_multiple(self):
        # 字符串形态（api 层 _normalize_extra_repos 未归一时的兜底）+ 多仓库编号递增
        ps = _debian_preseed(_cfg(extra_repos=[
            "http://192.168.199.1:8000/apt/extra/",
            {"name": "sec", "url": "http://192.168.199.2/apt/sec/"}]))
        assert ("d-i apt-setup/local0/repository string "
                "http://192.168.199.1:8000/apt/extra/ trixie main") in ps
        assert "d-i apt-setup/local0/comment string extra" in ps
        assert ("d-i apt-setup/local1/repository string "
                "http://192.168.199.2/apt/sec/ trixie main") in ps
        assert "d-i apt-setup/local1/comment string sec" in ps

    def test_extra_repos_empty_url_skipped(self):
        ps = _debian_preseed(_cfg(extra_repos=[{"name": "bad", "url": ""}]))
        assert "apt-setup/local0" not in ps

    def test_post_script_in_late_command_before_callback(self):
        ps = _debian_preseed(_cfg(post_script="echo hello-from-post"))
        lc = [l for l in ps.splitlines()
              if l.startswith("d-i preseed/late_command string ")][0]
        assert "in-target bash /tmp/opstk-post.sh" in lc, "post_script 必须在 target 内执行"
        p = lc.find("opstk-post.sh")
        d = lc.find(DONE)
        assert 0 <= p < d, "顺序必须是 公钥/sudo → post_script → done 回调"
        assert lc.rstrip().endswith("|| true"), "回调容错必须保持在整行末尾"

    def test_post_script_multiline_written_verbatim(self):
        ps = _debian_preseed(_cfg(post_script="apt-get update\necho b"))
        lc = [l for l in ps.splitlines()
              if l.startswith("d-i preseed/late_command string ")][0]
        assert "printf '%s\\n' 'apt-get update' 'echo b'" in lc, "多行脚本必须逐行落盘"
        assert "in-target bash /tmp/opstk-post.sh" in lc
        assert "apt-get update && echo b" not in lc, "不允许把两行揉成一行改变语义"

    def test_post_script_coexists_with_keys_sudo_callback(self):
        # 验收点：post_script 与 done_url 回调共存，且四段顺序明确
        ps = _debian_preseed(_cfg(post_script="echo x"))
        lc = [l for l in ps.splitlines()
              if l.startswith("d-i preseed/late_command string ")][0]
        i_keys = lc.find("authorized_keys")
        i_sudo = lc.find("90-opstk-ops")
        i_post = lc.find("opstk-post.sh")
        i_done = lc.find(DONE)
        assert -1 < i_keys < i_sudo < i_post < i_done, (
            "顺序必须是 公钥 → sudo → post_script → done 回调")
