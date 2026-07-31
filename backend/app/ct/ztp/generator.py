"""ZTP 配置开局生成器。

为 H3C / 华为 / 思科 设备生成：
  1. 每台设备的开局配置文件 (设备 CLI 语法)
  2. dnsmasq 投递配置 (按厂商下发 DHCP option 66/67/150/141)
  3. 厂商中间文件 (华为 midfile / 思科 python 脚本 / H3C 脚本)
  4. 部署说明 README
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class ZtpProfile:
    vendor: str = "h3c"            # h3c / huawei / cisco
    # 管理网段
    mgmt_vlan: int = 10
    mgmt_interface: str = "Vlan-interface10"
    mgmt_netmask: str = "255.255.255.0"
    mgmt_gateway: str = "10.0.0.254"
    dns_servers: list = field(default_factory=lambda: ["114.114.114.114"])
    ntp_server: str = "10.0.0.254"
    snmp_community: str = "public"
    domain_name: str = ""
    vlans: list = field(default_factory=list)   # [{"id":10,"name":"MGMT"}]
    # 管理
    admin_user: str = "admin"
    admin_password: str = ""
    enable_secret: str = ""
    ssh_keys: list = field(default_factory=list)
    # 上联/接入口 (可选)
    uplink_port: str = ""
    access_ports: list = field(default_factory=list)
    extra_config: str = ""
    # 投递
    server_ip: str = "10.0.0.250"
    tftp_root: str = "/srv/tftp"
    http_root: str = "http://10.0.0.250:8000/ztp"
    deploy_mode: str = "standalone"   # standalone / proxy / relay
    dhcp_iface: str = "eth0"
    dhcp_start: str = "10.0.0.100"
    dhcp_end: str = "10.0.0.200"


@dataclass
class ZtpDevice:
    hostname: str = "SW01"
    mac: str = ""               # 用于 DHCP 映射
    serial: str = ""            # 用于文件命名 (可选)
    mgmt_ip: str = ""           # 该设备管理 IP (可选覆盖)


def _file_stem(dev) -> str:
    """配置文件名主干：优先 serial，其次 hostname。"""
    return (dev.serial or dev.hostname or "device").strip()


# ============ 通用片段 ============
def _vlans_block_h3c(p) -> list:
    lines = []
    for v in p.vlans:
        vid = v.get("id", v.get("vlan"))
        name = v.get("name", "")
        lines.append(f"vlan {vid}")
        if name:
            lines.append(f" description {name}")
        lines.append("#")
    return lines


def _vlans_block_huawei(p) -> list:
    ids = [str(v.get("id", v.get("vlan"))) for v in p.vlans]
    if not ids:
        return []
    return [f"vlan batch {','.join(ids)}", "#"] + [
        (f"vlan {v.get('id', v.get('vlan'))}\n description {v.get('name','')}\n#" if v.get("name") else "")
        for v in p.vlans if v.get("name")
    ]


def _vlans_block_cisco(p) -> list:
    lines = []
    for v in p.vlans:
        vid = v.get("id", v.get("vlan"))
        name = v.get("name", "")
        lines.append(f"vlan {vid}")
        if name:
            lines.append(f" name {name}")
        lines.append("!")
    return lines


# ============ H3C Comware 7 ============
def h3c_config(dev, p) -> str:
    ip = dev.mgmt_ip or "10.0.0.1"
    user = p.admin_user or "admin"
    pw = p.admin_password or "ChangeMe@123"
    L = [
        "# H3C Comware 7 开局配置 (OpsToolkit 生成)",
        f"# host={dev.hostname} mgmt={ip}/{p.mgmt_netmask}",
        "sysname " + dev.hostname,
        "#",
        "irf mac-address persistent always",
        "irf auto-update enable",
        "#",
    ]
    L += _vlans_block_h3c(p)
    L += [
        f"interface {p.mgmt_interface}",
        f" ip address {ip} {p.mgmt_netmask}",
        "#",
    ]
    if p.access_ports:
        L.append("# 接入端口划入管理 VLAN")
        for port in p.access_ports:
            L += [f"interface {port}", " port link-mode bridge",
                  f" port access vlan {p.mgmt_vlan}", "#"]
    if p.uplink_port:
        L += [f"interface {p.uplink_port}", " port link-mode bridge",
              f" port access vlan {p.mgmt_vlan}", "#"]
    dns = " ".join(p.dns_servers) if p.dns_servers else ""
    L += [
        f"ip route-static 0.0.0.0 0 {p.mgmt_gateway}",
        "#",
        f"dns server {p.dns_servers[0]}" if p.dns_servers else "",
        "#",
        "# ---- 本地管理账号 ----",
        f"local-user {user} class manage",
        " service-type ssh",
        f" password simple {pw}",
        " authorization-attribute user-role network-admin",
        "#",
        "line vty 0 63",
        " authentication-mode scheme",
        " protocol inbound ssh",
        "#",
        "ssh server enable",
        "stelnet server enable",
        "#",
        "snmp-agent",
        f" snmp-agent community read {p.snmp_community}",
        " snmp-agent sys-info version v2c",
        "#",
        f"ntp-service unicast-peer {p.ntp_server}",
        "#",
    ]
    if p.domain_name:
        L.append(f"domain {p.domain_name}")
    if p.extra_config:
        L += ["# ---- 自定义配置 ----", p.extra_config]
    L += ["return", ""]
    return "\n".join(x for x in L if x != "")


# ============ 华为 VRP ============
def huawei_config(dev, p) -> str:
    ip = dev.mgmt_ip or "10.0.0.1"
    user = p.admin_user or "admin"
    pw = p.admin_password or "ChangeMe@123"
    vlanif = p.mgmt_interface.replace("Vlan-interface", "Vlanif")
    L = [
        "# Huawei VRP 开局配置 (OpsToolkit 生成)",
        f"# host={dev.hostname} mgmt={ip}/{p.mgmt_netmask}",
        "sysname " + dev.hostname,
        "#",
    ]
    L += _vlans_block_huawei(p)
    L += [
        f"interface {vlanif}",
        f" ip address {ip} {p.mgmt_netmask}",
        "#",
    ]
    if p.access_ports:
        L.append("# 接入端口划入管理 VLAN")
        for port in p.access_ports:
            L += [f"interface {port}", " port link-type access",
                  f" port default vlan {p.mgmt_vlan}", "#"]
    if p.uplink_port:
        L += [f"interface {p.uplink_port}", " port link-type access",
              f" port default vlan {p.mgmt_vlan}", "#"]
    L += [
        f"ip route-static 0.0.0.0 0.0.0.0 {p.mgmt_gateway}",
        "#",
        "# ---- AAA 本地账号 ----",
        "aaa",
        f" local-user {user} password cipher {pw}",
        f" local-user {user} privilege level 15",
        f" local-user {user} service-type ssh",
        "#",
        "user-interface vty 0 4",
        " authentication-mode aaa",
        " protocol inbound ssh",
        "#",
        "stelnet server enable",
        f"ssh user {user}",
        f"ssh user {user} authentication-type password",
        f"ssh user {user} service-type stelnet",
        "#",
        "snmp-agent",
        f" snmp-agent community read {p.snmp_community}",
        " snmp-agent sys-info version v2c",
        "#",
        f"ntp-service unicast-server {p.ntp_server}",
        "#",
    ]
    if p.dns_servers:
        L.append(f"dns server {p.dns_servers[0]}")
    if p.domain_name:
        L.append(f"domain {p.domain_name}")
    if p.extra_config:
        L += ["# ---- 自定义配置 ----", p.extra_config]
    L += ["return", ""]
    return "\n".join(x for x in L if x != "")


# ============ Cisco IOS-XE ============
def cisco_config(dev, p) -> str:
    ip = dev.mgmt_ip or "10.0.0.1"
    user = p.admin_user or "admin"
    pw = p.admin_password or "ChangeMe@123"
    enable = p.enable_secret or pw
    vlanif = p.mgmt_interface.replace("Vlan-interface", "Vlan")
    L = [
        "! Cisco IOS-XE 开局配置 (OpsToolkit 生成)",
        f"! host={dev.hostname} mgmt={ip}/{p.mgmt_netmask}",
        "hostname " + dev.hostname,
        "!",
        "no ip domain-lookup",
    ]
    if p.domain_name:
        L += [f"ip domain-name {p.domain_name}", "!"]
    L += _vlans_block_cisco(p)
    L += [
        f"interface {vlanif}",
        f" ip address {ip} {p.mgmt_netmask}",
        " no shutdown",
        "!",
    ]
    if p.access_ports:
        L.append("! 接入端口划入管理 VLAN")
        for port in p.access_ports:
            L += [f"interface {port}", " switchport mode access",
                  f" switchport access vlan {p.mgmt_vlan}", " no shutdown", "!"]
    if p.uplink_port:
        L += [f"interface {p.uplink_port}", " switchport mode access",
              f" switchport access vlan {p.mgmt_vlan}", " no shutdown", "!"]
    L += [
        f"ip route 0.0.0.0 0.0.0.0 {p.mgmt_gateway}",
        "!",
        "no service password-encryption",
        f"enable secret {enable}",
        f"username {user} privilege 15 secret {pw}",
        "!",
        "line vty 0 15",
        " login local",
        " transport input ssh",
        "!",
        "ip ssh version 2",
        "crypto key generate rsa modulus 2048",
        "!",
        f"snmp-server community {p.snmp_community} RO",
        f"ntp server {p.ntp_server}",
    ]
    if p.dns_servers:
        L.append("ip name-server " + " ".join(p.dns_servers))
    if p.extra_config:
        L += ["! ---- 自定义配置 ----", p.extra_config]
    L += ["end", ""]
    return "\n".join(x for x in L if x != "")


VENDOR_CONFIG = {
    "h3c": h3c_config,
    "huawei": huawei_config,
    "cisco": cisco_config,
}


def _ext(vendor) -> str:
    return {"h3c": "cfg", "huawei": "cfg", "cisco": "cfg"}.get(vendor, "cfg")


# ============ dnsmasq 投递配置 ============
def _mode_label(mode) -> str:
    return {
        "standalone": "standalone - 独立 DHCP (专用开局网络)",
        "proxy": "proxy - ProxyDHCP (与现有 DHCP 并存)",
        "relay": "relay - 中继模式 (仅 TFTP, 依赖交换机中继)",
    }.get(mode, mode)


def dnsmasq(p, devices) -> str:
    """按厂商下发 DHCP option，把每台设备指向自己的配置文件。

    H3C/华为: option 66 = TFTP server, option 67 = 配置文件名
    思科:     option 150 = TFTP server, option 67 = 配置文件名
    """
    vendor = p.vendor or "h3c"
    srv = p.server_ip or "10.0.0.250"
    L = [
        "# dnsmasq ZTP 投递配置 (OpsToolkit 生成)",
        f"# 厂商: {vendor}  部署模式: {_mode_label(p.deploy_mode)}",
        "port=0",
        f"interface={p.dhcp_iface}",
        "bind-interfaces",
        "",
    ]
    if p.deploy_mode == "relay":
        L.append("# 中继模式: 不开 DHCP, 仅 TFTP; 交换机 ip-helper 指向本机")
        L.append("no-dhcp-interface=")
    elif p.deploy_mode == "proxy":
        L.append("# ProxyDHCP: 不分配 IP, 仅下发 PXE/ZTP 引导, 与现有 DHCP 并存")
        L.append(f"dhcp-range={srv},proxy")
    else:
        L.append("# 独立 DHCP: 分配 IP + 下发 ZTP 配置文件名")
        L.append(f"dhcp-range={p.dhcp_start},{p.dhcp_end},12h")
        L.append(f"dhcp-option=option:router,{p.mgmt_gateway}")
        dns = p.dns_servers[0] if p.dns_servers else p.mgmt_gateway
        L.append(f"dhcp-option=option:dns-server,{dns}")
    L += [
        "",
        "enable-tftp",
        f"tftp-root={p.tftp_root}",
        "",
        "# ---- 全局 TFTP 服务器与引导文件 ----",
    ]
    if vendor == "cisco":
        L.append(f"dhcp-option=150,{srv}")
    else:
        L.append(f"dhcp-option=option:tftp-server,{srv}")
        L.append(f"dhcp-option=66,{srv}")
    # 默认引导文件 (未登记 MAC 的设备)
    L.append(f'dhcp-option=option:bootfile-name,"ztp/default.cfg"')
    L.append("")
    L.append("# ---- 按 MAC/序列号映射到各自配置文件 ----")
    for d in devices:
        stem = _file_stem(d)
        fname = f"ztp/{stem}.{_ext(vendor)}"
        if d.mac:
            tag = "tag:set_" + d.mac.replace(":", "").lower()
            L.append(f"dhcp-host={d.mac},set:set_{d.mac.replace(':', '').lower()}")
            L.append(f'dhcp-option={tag},option:bootfile-name,"{fname}"')
        else:
            L.append(f'# {d.hostname}: 缺少 MAC, 使用 default.cfg')
    L.append("")
    return "\n".join(L) + "\n"


# ============ 厂商中间文件 ============
def _huawei_midfile(p, devices) -> str:
    """华为 ZTP 中间文件: 指定系统软件/配置/补丁下载源。"""
    srv = p.server_ip or "10.0.0.250"
    lines = [
        "# Huawei ZTP intermediate file",
        "BOM",
        f'"ZTP file server" : "tftp://{srv}"',  # noqa
        f'"HTTP file server" : "{p.http_root}"',
        '"ZTP version" : "1.0"',
        '"File info" : {',
    ]
    for d in devices:
        stem = _file_stem(d)
        lines.append(f'  "{stem}.cfg" : "ztp/{stem}.cfg"')
    lines += ['}', 'EOF', '']
    return "\n".join(lines)


def _cisco_script(p, devices) -> str:
    """思科 IOS-XE ZTP Python 脚本: 拉取配置并 apply。"""
    srv = p.server_ip or "10.0.0.250"
    return (
        "#! /usr/bin/env python3\n"
        "# Cisco IOS-XE ZTP bootstrap (OpsToolkit)\n"
        "import cli, json\n"
        "cfg = cli.execute('show version | inc Serial')\n"
        f'server = "{p.http_root}"\n'
        "# 按 hostname/serial 下载对应 .cfg 并应用\n"
        "for host in " + json.dumps([_file_stem(d) for d in devices]) + ":\n"
        "    cli.configurep(['file tftp://{}/{}/{}.cfg'.format('" + srv + "', 'ztp', host)])\n"
        "    break\n"
    )


def _h3c_script(p, devices) -> str:
    """H3C ZTP 脚本占位: auto-config 默认即按 DHCP option 取配置。"""
    return (
        "# H3C Comware auto-config 由 DHCP option 66/67 自动获取配置,\n"
        "# 无需额外脚本。本文件仅作说明占位。\n"
        "# 设备首次启动空配置时, 会向 DHCP 请求并下载 default.cfg 或本机命名 .cfg\n"
    )


def intermediate(p, devices):
    v = p.vendor or "h3c"
    if v == "huawei":
        return _huawei_midfile(p, devices), "ztp_intermediate.txt"
    if v == "cisco":
        return _cisco_script(p, devices), "ztp_bootstrap.py"
    return _h3c_script(p, devices), "ztp_note.txt"


def _readme(p, devices) -> str:
    v = p.vendor or "h3c"
    return (
        "OpsToolkit ZTP 开局部署说明\n"
        "==========================\n\n"
        f"厂商: {v}\n"
        f"ZTP 服务器: {p.server_ip}\n"
        f"投递模式: {p.deploy_mode}\n\n"
        "步骤:\n"
        "1. 安装 dnsmasq, 用生成的 dnsmasq.conf 替换 /etc/dnsmasq.conf\n"
        f"   systemctl restart dnsmasq\n\n"
        "2. 建立 TFTP 目录结构:\n"
        f"   {p.tftp_root}/ztp/  放入各设备 .cfg 与 default.cfg\n\n"
        "3. (可选) HTTP 服务器镜像 {p.http_root} 提供大文件下载\n\n"
        "4. 新设备空配置上电, 接入开局网络, 自动获取配置\n\n"
        "厂商要点:\n"
        "  H3C   : auto-config, DHCP option 66(TFTP) + 67(文件名)\n"
        "  华为  : ZTP, DHCP option 66(TFTP) + 67(中间文件) + 中间文件描述下载项\n"
        "  思科  : IOS-XE ZTP, DHCP option 150(TFTP) + 67(脚本/配置)\n\n"
        f"本批次设备: {len(devices)} 台\n"
    )


def generate_all(p, devices=None):
    devices = devices or []
    files = {}
    gen = VENDOR_CONFIG.get(p.vendor, h3c_config)
    for d in devices:
        stem = _file_stem(d)
        files[f"ztp/{stem}.{_ext(p.vendor)}"] = gen(d, p)
    # 未登记设备的兜底配置
    if devices:
        files["ztp/default.cfg"] = gen(ZtpDevice(hostname="default", mgmt_ip=p.dhcp_start), p)
    files["dnsmasq.conf"] = dnsmasq(p, devices)
    inter, inter_name = intermediate(p, devices)
    files[f"ztp/{inter_name}"] = inter
    files["README.txt"] = _readme(p, devices)
    return files
