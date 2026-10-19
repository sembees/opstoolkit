"""Debian preseed 家族用例（2026-10-09）。

锁住五件事：
1. `_debian_preseed(c)` 的内容（关键 d-i 指令、静态网络、分区两态、回调、crypted 口令）；
2. 家族分发：debian → `preseed.cfg`（不再走 kickstart/ubuntu 的产物名）；
3. iPXE 内核参数：`auto=true priority=critical interface=… [netcfg/*] preseed/url=…`
   —— 静态才带 netcfg/disable_autoconfig 与 netcfg/get_*（遗留 A）；不带裸 `url=`；
4. `disk_config.wipe_disks`：默认只清目标盘（逐字节不变）、显式盘并入 early_command、
   白名单拦注入、其它家族不消费（遗留 B）；
5. `os_catalog` 的 debian 已打开 `auto_install`（不再命中"不支持自动装机"的拒绝分支）。

另外**安全断言**：生成物里不得出现明文管理员口令（只允许 crypted 形态）。
"""
import re
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

    def test_hostname_both_keys_emitted(self):
        """主机名必须**两个键都下**（2026-10-14 真机回归发现的缺口）。

        只写 netcfg/get_hostname（提问模板，Default: debian）时装完 /etc/hostname 恒为
        debian —— VM140(BIOS) 与 VM141(UEFI) 两台实测一致；netcfg 1.197 的
        debian/netcfg-common.templates 里真正落盘的是 netcfg/hostname
        （"Hostname to set for the system; ignores names provided by DHCP or DNS"）。
        这是与 RHEL(`network --hostname=`) / Ubuntu(`identity.hostname`) 的家族等价要求。
        """
        ps = _debian_preseed(_cfg())
        assert "d-i netcfg/get_hostname string pxe140-debian" in ps
        assert "d-i netcfg/hostname string pxe140-debian" in ps, \
            "缺 netcfg/hostname ⇒ 装完主机名会落回 d-i 默认值 debian（真机实证）"
        # 两行同值，且不得出现第三个主机名来源
        hn = [l for l in ps.splitlines() if "netcfg/hostname" in l or "netcfg/get_hostname" in l]
        assert len(hn) == 2, hn

    def test_hostname_sanitized_at_generator_layer(self):
        # 该值落进 d-i 命令串：生成器不信任入参，第 2 层白名单（schema 层是第 1 层）
        ps = _debian_preseed(_cfg(hostname="pxe 140;evil\nx"))
        for l in ps.splitlines():
            if "netcfg/hostname" in l or "netcfg/get_hostname" in l:
                assert l.endswith(" string pxe140evilx"), l
        # 空主机名落回 debian（与 netcfg/get_hostname 的 Default 一致），绝不留空串
        ps2 = _debian_preseed(_cfg(hostname=""))
        assert "d-i netcfg/hostname string debian" in ps2

    def test_hostname_written_in_late_command(self):
        """真正生效的是 late_command 里那次写入（netcfg 侧两个键实测都不生效）。

        依据见 generator._debian_preseed 内"主机名：netcfg 侧两个键都下，但不依赖它们"
        整段注释：netcfg 1.197 主流程从不读 netcfg/hostname，而 netcfg_write_common()
        在 hostname 为空时直接 return —— 两轮真机装完 /etc/hostname 都是 debian。
        """
        lc = [l for l in _debian_preseed(_cfg()).splitlines()
              if l.startswith("d-i preseed/late_command string ")][0]
        assert "> /target/etc/hostname" in lc, "必须在 late_command 里落主机名"
        assert "printf '%s\\n' pxe140-debian > /target/etc/hostname" in lc
        assert "> /target/etc/hosts" in lc
        # 静态 IP 那台：/etc/hosts 的第二行必须是 "静态IP  主机名"
        assert "%s\\t%s\\n\\n" in lc and " 192.168.199.140 pxe140-debian > /target/etc/hosts" in lc
        # 整条 late_command 必须仍是**单逻辑行**（裸换行会截断 preseed 条目）
        assert "\n" not in lc

    def test_hostname_late_command_uses_loopback_when_dhcp(self):
        # 没有静态 IP 时按 d-i 惯例用 127.0.1.1，不写空地址
        lc = [l for l in _debian_preseed(_cfg(net_mode="dhcp")).splitlines()
              if l.startswith("d-i preseed/late_command string ")][0]
        assert " 127.0.1.1 pxe140-debian > /target/etc/hosts" in lc
        assert "\n" not in lc


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
        # 2026-10-19：回调改成 3 次重试后行尾是 `; done; true`（不再是 `|| true`）；
        # 判定意图不变 —— 回调失败绝不能把装好的系统判成装机失败。
        assert re.search(r"(?:\|\| true|done; true)$", lc.rstrip()), "回调容错必须保持在整行末尾"
        assert "for i in 1 2 3" in lc, "回调必须带重试"

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


# ══ 遗留 A/B 用例（2026-10-14 落地）═════════════════════════════════════════
# 遗留 A：_ipxe_menu 的 debian 分支此前**恒发** netcfg/disable_autoconfig=true 与
#   netcfg/get_*，DHCP 模板被拉回"静态但无值"（d-i 停在静态网络提问，无人应答的死局）。
#   修复 = 与三族应答文件侧同一谓词 (c.net_mode == "static")：只有字面 static 走静态。
# 遗留 B：disk_config.wipe_disks —— 显式声明"本次装机要一并清理的盘"（不含目标盘），
#   用于清掉别盘残留的 LVM 卷组元数据（真机实证：d-i 停在
#   "Volume group name already in use"）。默认 [] = 只清目标盘，绝不默认全抹。
import re as _re

from app.it.pxe.generator import _disk_config_active, _rhel_ks, _ubuntu_user_data

_MASK_RE = _re.compile(r"\$6\$[^\s'\"]+")


def _mask(t: str) -> str:
    return _MASK_RE.sub("$6$MASK", t)


class TestDebianIpxeNetModeBranch:
    """遗留 A：iPXE debian 分支按 net_mode 分岔。

    注意两层优先级（generate_all 第 3108-3110 行）：装机记录**自带 ip** 时，
    每机菜单会被强制按静态生成（`replace(ic, net_mode="static", net_config=mnet)`）
    —— 这是每机静态 IP 的正道，与模板 net_mode 无关。所以要看模板级 net_mode 分岔，
    装机记录必须**不带 ip**（INST_NOIP），否则量到的是"该机自己的静态优先"。
    """

    TAG = "bc-24-11-66-8b-ea"
    # 与 TestDebianWiring.INST 同形，但**不带 ip** ⇒ 不触发每机静态覆盖
    INST_NOIP = [{"id": "abc", "mac": "BC:24:11:66:8B:EA",
                  "hostname": "pxe140-debian", "status": "pending"}]

    def _kernel_line(self, insts=None, **over):
        files = generate_all(_cfg(**over), self.INST_NOIP if insts is None else insts)
        boot = files["boot/" + self.TAG + ".ipxe"]
        return [l for l in boot.splitlines() if l.startswith("kernel ")][0]

    def test_static_keeps_netcfg_args(self):
        line = self._kernel_line()          # BASE_KW 就是 static
        assert "netcfg/disable_autoconfig=true" in line
        assert "netcfg/get_ipaddress=192.168.199.140" in line
        assert "netcfg/get_netmask=255.255.255.0" in line
        assert "netcfg/get_gateway=192.168.199.1" in line
        assert "netcfg/get_nameservers=192.168.199.1" in line
        assert "netcfg/confirm_static=true" in line

    def test_dhcp_drops_all_netcfg_args(self):
        # net_config 里仍带着静态值（前端可能只翻了 net_mode）—— 也必须一个 netcfg/ 都不发
        line = self._kernel_line(net_mode="dhcp")
        assert "netcfg/" not in line, "DHCP 模板不得再带任何 netcfg/ 参数（否则被拉回静态死局）"
        # 装机网卡仍要钉住（多网卡），preseed 取用路径不变
        assert "interface=ens18" in line
        assert "auto=true" in line and "priority=critical" in line
        assert "preseed/url=http://192.168.199.1:8000/pxe/serve/T/preseed/" + self.TAG + "/preseed.cfg" in line

    def test_unknown_net_mode_falls_back_to_dhcp(self):
        # 与四族同一口径：只有字面 "static" 走静态，其余（含非法/缺失）一律 DHCP
        for mode in ("", "DHCP", "bogus", None):
            line = self._kernel_line(net_mode=mode)
            assert "netcfg/" not in line, "net_mode=%r 必须落 DHCP" % (mode,)

    def test_registered_machine_ip_still_wins_over_template_dhcp(self):
        # 反向锁定：装机记录自带 ip 时，即便模板是 dhcp，每机菜单仍按该机静态生成
        # （否则"每机静态 IP"能力会被模板的 net_mode 吃掉）
        line = self._kernel_line(insts=TestDebianWiring.INST, net_mode="dhcp")
        assert "netcfg/get_ipaddress=192.168.199.140" in line
        assert "netcfg/disable_autoconfig=true" in line
        assert "netcfg/confirm_static=true" in line


class TestDebianWipeDisks:
    """遗留 B：disk_config.wipe_disks 的生成器消费与闭集边界。"""

    PS_A = "http://192.168.199.1:8000/pxe/serve/T/preseed/bc-24-11-66-8b-ea/preseed.cfg"

    @staticmethod
    def _early(ps):
        return [l for l in ps.splitlines()
                if l.startswith("d-i partman/early_command string ")][0]

    def test_default_absent_is_byte_identical(self):
        # 不声明 wipe_disks ⇒ 产物与"只有目标盘"逐字节相同（存量模板零变化）。
        # 口令密文盐随机会导致明文比较假红，按既有 golden 惯例掩码后比。
        assert _mask(_debian_preseed(_cfg())) == _mask(_debian_preseed(_cfg(disk_config={"disk": "sda"})))

    def test_wipe_disks_added_to_early_command(self):
        ps = _debian_preseed(_cfg(disk_config={"disk": "sda", "wipe_disks": ["sdb", "/dev/nvme0n1"]}))
        e = self._early(ps)
        for d in ("/dev/sda", "/dev/sdb", "/dev/nvme0n1"):
            assert ("wipefs -a " + d + " ") in e, "每个盘都要 wipefs: " + d
            assert ("of=" + d + " bs=1M count=200") in e, "每个盘都要抹首 200MB: " + d
        assert e.rstrip().endswith("; true"), "整行必须以 ; true 收尾（与目标盘同形）"

    def test_wipe_disks_dedup_and_target_skip(self):
        # 与目标盘同名 / 列表内重复 ⇒ 幂等忽略，不对同一盘重复 dd
        ps = _debian_preseed(_cfg(disk_config={"disk": "sda",
                                               "wipe_disks": ["sda", "sdb", "sdb", "/dev/sdb"]}))
        e = self._early(ps)
        assert e.count("wipefs -a /dev/sda ") == 1
        assert e.count("wipefs -a /dev/sdb ") == 1

    def test_illegal_wipe_disk_name_rejected(self):
        # 盘名会拼进 d-i 用 sh 执行的命令串 ⇒ 白名单外一律 ValueError（不生成产物）
        for bad in ("sdb; rm -rf /", "sd*", "sdb$(reboot)", "sdb x", 'sdb"x'):
            with pytest.raises(ValueError):
                _debian_preseed(_cfg(disk_config={"disk": "sda", "wipe_disks": [bad]}))

    def test_empty_wipe_disk_entry_skipped_not_raised(self):
        # 空串/纯空白是"没填"而不是"填错了"：生成器跳过（与 api 层 _normalize_extra_repos
        # 对空 url 同规则）。致命面由 schema 保存期 422 兜住（见 test_pxe_wipe_disks_schema）。
        for empty in ("", "   ", "/dev/"):
            ps = _debian_preseed(_cfg(disk_config={"disk": "sda", "wipe_disks": [empty]}))
            e = self._early(ps)
            assert e.count("wipefs -a ") == 1, "空条目不得产出额外清理盘: %r" % (empty,)

    def test_only_debian_consumes_wipe_disks(self):
        # 闭集边界：RHEL/Ubuntu 家族不消费该键（v1 只接 preseed）。同一份 disk_config
        # 加不加 wipe_disks，两族产物掩码后逐字节相同 —— 且守卫不让它误切结构化路径。
        dc_plain = {"disk": "sda"}
        dc_wipe = {"disk": "sda", "wipe_disks": ["sdb"]}
        assert _disk_config_active(dc_plain) is False
        assert _disk_config_active(dc_wipe) is False, \
            "wipe_disks 不是结构化键，不得把存量模板切进结构化路径"
        assert _disk_config_active({"disk": "sda", "target": {"mode": "name", "name": "sda"}}) is True
        assert _mask(_rhel_ks(_cfg(os_type="rocky", disk_config=dc_plain))) == \
            _mask(_rhel_ks(_cfg(os_type="rocky", disk_config=dc_wipe)))
        assert _mask(_ubuntu_user_data(_cfg(os_type="ubuntu", disk_config=dc_plain))) == \
            _mask(_ubuntu_user_data(_cfg(os_type="ubuntu", disk_config=dc_wipe)))

