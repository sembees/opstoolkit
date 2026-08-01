"""PXE 装机配置生成器。

生成四类文件：
  1. Ubuntu autoinstall (user-data, cloud-init subiquity)
  2. RHEL Kickstart (ks.cfg)
  3. iPXE 启动菜单脚本
  4. dnsmasq 配置 (DHCP + TFTP + PXE)
"""
from __future__ import annotations

import json
from passlib.hash import sha512_crypt
from dataclasses import dataclass, replace


@dataclass
class PxeConfig:
    os_type: str = "ubuntu"
    os_version: str = "22.04"
    hostname: str = "server01"
    timezone: str = "Asia/Shanghai"
    locale: str = "en_US.UTF-8"
    keyboard: str = "us"
    admin_user: str = "ops"
    admin_password: str = ""
    root_password: str = ""
    ssh_keys: list = None
    disk_scheme: str = "lvm"
    disk_config: dict = None
    net_mode: str = "dhcp"
    net_config: dict = None
    mirror: str = ""
    extra_packages: list = None
    post_script: str = ""
    server_ip: str = "192.168.1.100"
    http_root: str = "http://192.168.1.100:8000/pxe"
    kernel_path: str = "ubuntu/22.04/vmlinuz"
    initrd_path: str = "ubuntu/22.04/initrd"
    squashfs_path: str = "ubuntu/22.04/installer.squashfs"
    deploy_mode: str = "standalone"  # standalone(独立DHCP) / proxy(ProxyDHCP) / relay(中继模式)


def _hash_pw(plaintext):
    if not plaintext:
        plaintext = "opstk-default"
    return sha512_crypt.using(rounds=5000).hash(plaintext)


# ===== Ubuntu autoinstall =====
def _ubuntu_user_data(c):
    dc = c.disk_config or {}
    nc = c.net_config or {}
    disk = dc.get("disk", "sda")

    if c.disk_scheme == "direct":
        storage = json.dumps([
            {"type": "format", "fstype": "ext4", "volume": "/dev/" + disk},
            {"type": "mount", "path": "/", "device": "/dev/" + disk},
        ])
    else:
        storage = json.dumps([
            {"type": "disk", "id": "disk0", "device": "/dev/" + disk, "wipe": "superblock"},
            {"type": "lvm_volgroup", "name": "vg0", "devices": ["disk0"]},
            {"type": "lvm_partition", "name": "root", "volgroup": "vg0", "size": "-8G"},
            {"type": "lvm_partition", "name": "swap", "volgroup": "vg0", "size": "8G"},
            {"type": "format", "fstype": "ext4", "volume": "lvm_volgroup-vg0/root"},
            {"type": "format", "fstype": "swap", "volume": "lvm_volgroup-vg0/swap"},
            {"type": "mount", "path": "/", "device": "lvm_volgroup-vg0/root"},
        ])

    if c.net_mode == "static":
        iface = nc.get("interface", "ens33")
        addr = nc.get("ip", "10.0.0.10") + "/" + str(nc.get("cidr", 24))
        eth = {iface: {
            "addresses": [addr],
            "routes": [{"to": "default", "via": nc.get("gateway", "10.0.0.1")}],
            "nameservers": {"addresses": nc.get("dns", ["8.8.8.8"])}},
        }
        network = json.dumps({"version": 2, "renderer": "networkd", "ethernets": eth})
    else:
        network = json.dumps({"version": 2, "renderer": "networkd"})

    lines = [
        "#cloud-config",
        "# Ubuntu Server autoinstall - subiquity",
        "autoinstall:",
        "  version: 1",
        "  locale: " + c.locale,
        "  keyboard: {layout: " + c.keyboard + "}",
        "  timezone: " + c.timezone,
        "  identity:",
        "    realname: '" + c.admin_user + "'",
        "    username: " + c.admin_user,
        "    hostname: " + c.hostname,
        "    password: '" + _hash_pw(c.admin_password) + "'",
        "  storage: " + storage,
        "  network: " + network,
    ]
    if c.ssh_keys:
        lines.append("  ssh:")
        lines.append("    authorized-keys: " + json.dumps(c.ssh_keys))
    if c.mirror:
        lines.append("  apt:")
        lines.append("    primary:")
        lines.append("      - arches: [amd64]")
        lines.append("        uri: " + c.mirror)
    if c.extra_packages:
        lines.append("  packages: " + json.dumps(c.extra_packages))
    lines.append("  late_commands:")
    if c.post_script:
        escaped = c.post_script.replace(chr(39), chr(92) + chr(39))
        lines.append("    - curtin in-target --target=/target -- bash -c '" + escaped + "'")
    lines.append("    - curtin in-target --target=/target -- systemctl enable ssh")
    lines.append("    - reboot")
    return "\n".join(lines) + "\n"


# ===== RHEL Kickstart =====
def _rhel_ks(c):
    dc = c.disk_config or {}
    nc = c.net_config or {}
    disk = dc.get("disk", "sda")

    if c.disk_scheme == "direct":
        parts = (
            "clearpart --drives=" + disk + " --all --initlabel\n"
            "part /boot/efi --fstype=efi --size=512\n"
            "part / --fstype=ext4 --ondisk=" + disk + " --grow\n"
            "part swap --size=8192\n"
        )
    else:
        parts = (
            "clearpart --drives=" + disk + " --all --initlabel\n"
            "part /boot/efi --fstype=efi --size=512\n"
            "part /boot --fstype=ext4 --size=1024\n"
            "part pv.01 --size=1 --grow\n"
            "volgroup vg0 pv.01\n"
            "logvol / --vgname=vg0 --name=root --size=20480 --fstype=ext4\n"
            "logvol swap --vgname=vg0 --name=swap --size=8192\n"
            "logvol /home --vgname=vg0 --name=home --size=10240 --fstype=ext4\n"
        )

    if c.net_mode == "static":
        net = ("network --bootproto=static --device=" + nc.get("interface", "ens33") +
               " --ip=" + nc.get("ip", "10.0.0.10") +
               " --netmask=" + nc.get("netmask", "255.255.255.0") +
               " --gateway=" + nc.get("gateway", "10.0.0.1") +
               " --nameserver=" + ",".join(nc.get("dns", ["8.8.8.8"])) +
               " --hostname=" + c.hostname + " --activate")
    else:
        net = "network --bootproto=dhcp --hostname=" + c.hostname + " --activate"

    repo = ("url --url=" + chr(34) + c.mirror + chr(34) + "\n"
            if c.mirror else "# url --url=" + chr(34) + "http://mirror/rocky/9/BaseOS/x86_64/os/" + chr(34) + "\n")

    pkgs = c.extra_packages or ["vim", "net-tools", "bash-completion", "tar", "wget", "curl"]

    L = [
        "# RHEL / Rocky / Alma Linux Kickstart",
        "lang " + c.locale,
        "keyboard --vckeymap=" + c.keyboard,
        "timezone " + c.timezone,
        "",
        repo.rstrip(),
        "",
        net,
        "",
        "auth --enableshadow --passalgo=sha512",
        "rootpw --iscrypted " + _hash_pw(c.root_password or c.admin_password),
        "user --name=" + c.admin_user + " --password=" + _hash_pw(c.admin_password) + " --gecos=" + chr(34) + c.admin_user + chr(34) + " --groups=wheel",
        "",
        parts.rstrip(),
        "bootloader --location=mbr --boot-drive=" + disk,
        "selinux --permissive",
        "firewall --enabled --ssh",
        "services --enabled=sshd,NetworkManager",
        "firstboot --disable",
        "eula --agreed",
        "reboot",
        "",
        "%packages --ignoremissing",
    ]
    L.extend(pkgs)
    L.append("%end")
    if c.ssh_keys or c.post_script:
        L.append("")
        L.append("%post --interpreter=/bin/bash")
        for k in c.ssh_keys or []:
            L.append("mkdir -p /home/" + c.admin_user + "/.ssh && echo " + chr(39) + k + chr(39) + " >> /home/" + c.admin_user + "/.ssh/authorized_keys")
        L.append("chown -R " + c.admin_user + ":" + c.admin_user + " /home/" + c.admin_user + "/.ssh")
        if c.post_script:
            L.append(c.post_script)
        L.append("%end")
    return "\n".join(L) + "\n"


# ===== iPXE 菜单 =====
def _ipxe_menu(c, mac="", answer_url=""):
    kernel = c.http_root + "/" + c.kernel_path
    initrd = c.http_root + "/" + c.initrd_path
    if c.os_type == "ubuntu":
        seed = answer_url or (c.http_root + "/")
        cmdline = ("autoinstall ds=nocloud-net;s=" + seed + " "
                   "ip=dhcp --- " + c.squashfs_path)
        L = ["#!ipxe", "# boot: " + c.hostname + " (MAC " + (mac or "auto") + ")",
             "kernel " + kernel + " root=/dev/ram0 " + cmdline,
             "initrd " + initrd, "boot"]
    else:
        answer = answer_url or (c.http_root + "/ks.cfg")
        repo = c.mirror or (c.http_root + "/os")
        L = ["#!ipxe", "# boot: " + c.hostname + " (MAC " + (mac or "auto") + ")",
             "kernel " + kernel + " inst.ks=" + answer + " inst.repo=" + repo + " ip=dhcp",
             "initrd " + initrd, "boot"]
    return "\n".join(L) + "\n"


# ===== dnsmasq 配置 =====
def _dnsmasq(c, installs=None):
    """三种部署模式：
    standalone - 独立 DHCP（专用装机网络，裸机接入即装）
    proxy      - ProxyDHCP（与现有 DHCP 并存，只提供 PXE 引导信息）
    relay      - 中继模式（仅 TFTP+HTTP，依赖交换机 DHCP 中继）
    """
    installs = installs or []
    nc = c.net_config or {}
    iface = nc.get("interface", "eth0")
    gateway = nc.get("gateway", "192.168.1.1")
    mode = c.deploy_mode or "standalone"

    L = [
        "# dnsmasq PXE 配置 (OpsToolkit 生成)",
        "# 部署模式: " + _mode_label(mode),
        "port=0",
        "interface=" + iface,
        "bind-interfaces",
        "",
    ]

    if mode == "relay":
        # 中继模式：不开 DHCP，只做 TFTP + HTTP 文件服务
        L.append("# 仅 TFTP，DHCP 由网络中继转发，确保交换机 IP Helper 指向本机")
        L.append("no-dhcp-interface=")
        L.append("")
        L.append("enable-tftp")
        L.append("tftp-root=/srv/tftp")
        L.append("")
        L.append("# PXE 引导文件: BIOS -> undionly.kpxe, UEFI -> ipxe.efi")
        L.append("dhcp-match=set:efi-x86_64,option:client-arch,7")
        L.append("dhcp-match=set:efi-x86_64,option:client-arch,9")
        L.append("dhcp-boot=tag:efi-x86_64,ipxe.efi")
        L.append("dhcp-boot=tag:!efi-x86_64,undionly.kpxe")
    elif mode == "proxy":
        # ProxyDHCP：不分配 IP，只提供 PXE 引导信息，与现有 DHCP 并存
        L.append("# ProxyDHCP 模式: 不分配 IP，仅提供 PXE 引导，与现有 DHCP 服务器并存")
        L.append("# 必须设置 pxeserver 本机 IP")
        pxeserver = c.server_ip or "192.168.1.100"
        L.append('dhcp-range=' + pxeserver + ',proxy')
        L.append('dhcp-option=option:server-ip-address,' + pxeserver)
        L.append('pxe-service=tag:!efi-x86_64,x86PC,"PXE Boot",undionly.kpxe')
        L.append('pxe-service=tag:efi-x86_64,X86-64_EFI,"PXE Boot",ipxe.efi')
        L.append("")
        L.append("enable-tftp")
        L.append("tftp-root=/srv/tftp")
    else:
        # standalone：独立 DHCP + TFTP，完整分配 IP
        L.append("# 独立 DHCP 模式: 完整分配 IP + PXE 引导（确保网段内无其他 DHCP）")
        L.append("dhcp-range=" + nc.get("dhcp_start", "192.168.1.100") + "," + nc.get("dhcp_end", "192.168.1.200") + ",12h")
        L.append("dhcp-option=option:router," + gateway)
        L.append("dhcp-option=option:dns-server," + nc.get("dns_server", gateway))
        L.append("")
        L.append("enable-tftp")
        L.append("tftp-root=/srv/tftp")
        L.append("")
        L.append("# PXE 引导: BIOS -> undionly.kpxe, UEFI -> ipxe.efi")
        L.append("dhcp-match=set:efi-x86_64,option:client-arch,7")
        L.append("dhcp-match=set:efi-x86_64,option:client-arch,9")
        L.append("dhcp-boot=tag:efi-x86_64,ipxe.efi")
        L.append("dhcp-boot=tag:!efi-x86_64,undionly.kpxe")

    L.append("")
    L.append("# 按 MAC 指定 iPXE 菜单 (tag 方式, 避免 URL 被当作 hostname)")
    for inst in installs:
        mac = (inst.get("mac") or "").strip()
        if mac:
            tag = mac.lower().replace(":", "-")
            menu = c.http_root + "/boot/" + tag + ".ipxe"
            L.append("dhcp-host=" + mac + ",set:pxe_" + tag)
            L.append("dhcp-boot=tag:pxe_" + tag + "," + menu)
    L.append("")
    return "\n".join(L) + "\n"


def _mode_label(mode):
    return {
        "standalone": "standalone - 独立 DHCP (专用装机网络)",
        "proxy": "proxy - ProxyDHCP (与现有 DHCP 并存)",
        "relay": "relay - 中继模式 (仅 TFTP, 依赖交换机中继)",
    }.get(mode, mode)





def _readme(c):
    return (
        "OpsToolkit PXE 部署说明\n"
        "========================\n\n"
        "目标系统: " + c.os_type + " " + c.os_version + "\n"
        "PXE 服务端: " + c.server_ip + "\n\n"
        "部署步骤:\n"
        "1. 安装 dnsmasq，将 dnsmasq.conf 放到 /etc/dnsmasq.conf\n"
        "   iPXE 固件(ipxe.efi/undionly.kpxe)放入 /srv/tftp/\n"
        "   systemctl restart dnsmasq\n\n"
        "2. 挂载 ISO，通过 HTTP 提供 vmlinuz/initrd/squashfs:\n"
        "   mount -o loop ubuntu.iso /mnt/iso\n"
        "   拷贝内核文件到 HTTP 目录\n\n"
        "3. user-data(Ubuntu) 或 ks.cfg(RHEL) + boot.ipxe 放到 HTTP 目录\n"
        "   路径需与 http_root 一致\n\n"
        "4. 装机主机 BIOS/UEFI 设为 PXE 网络启动即可\n\n"
        "注意: 已有 DHCP 时在交换机配置 IP Helper 指向本机\n"
    )


def generate_all(c, installs=None):
    files = {}
    if c.os_type == "ubuntu":
        files["user-data"] = _ubuntu_user_data(c)
        files["meta-data"] = "local-hostname: " + c.hostname + "\n"
    else:
        files["ks.cfg"] = _rhel_ks(c)
    files["boot.ipxe"] = _ipxe_menu(c)
    files["dnsmasq.conf"] = _dnsmasq(c, installs)
    files["README.txt"] = _readme(c)
    # 每台装机记录生成独立菜单与应答文件，使 hostname 生效
    for inst in installs or []:
        mac = (inst.get("mac") or "").strip()
        if not mac:
            continue
        tag = mac.lower().replace(":", "-")
        hostname = (inst.get("hostname") or "").strip() or c.hostname
        ic = replace(c, hostname=hostname)
        if c.os_type == "ubuntu":
            seed = c.http_root + "/user-data/" + tag + "/"
            files["user-data/" + tag + "/user-data"] = _ubuntu_user_data(ic)
            files["user-data/" + tag + "/meta-data"] = "local-hostname: " + hostname + "\n"
            answer = seed
        else:
            answer = c.http_root + "/ks/" + tag + "/ks.cfg"
            files["ks/" + tag + "/ks.cfg"] = _rhel_ks(ic)
        files["boot/" + tag + ".ipxe"] = _ipxe_menu(ic, mac=mac, answer_url=answer)
    return files
