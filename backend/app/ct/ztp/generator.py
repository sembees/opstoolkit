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
    ntp_server: str = ""
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

    @property
    def mgmt_via_dhcp(self) -> bool:
        """没有指定管理 IP ⇒ 管理口用 **DHCP 取址**，而不是写死一个假地址。

        这条是"ZTP 免登记也能开局"的关键（RUNBOOK §5.54）：
          · 不登记设备时，所有设备都拿 `default.cfg`；如果那份配置里写死一个管理 IP，
            两台以上设备就会**同一个 IP 冲突**；
          · 登记了设备但还不知道它的管理 IP（很常见的顺序：先按 MAC 登记、IP 后分配）
            以前会被写死成 `10.0.0.1` —— 一个谁都不在的网段。
        两者现在都走 DHCP 取址：设备开局后从 ZTP 的地址池拿到地址，
        运维按地址/MAC 找到它，再决定要不要下发各自的静态配置。
        """
        return not (self.mgmt_ip or "").strip()


def _mgmt_label(dev, static_ip, p) -> str:
    """头注释里怎么描述管理地址：DHCP 取址时写 dhcp，别写一个根本没配上去的假 IP。"""
    if dev.mgmt_via_dhcp:
        return "dhcp"
    return "%s/%s" % (static_ip, p.mgmt_netmask)


def _ntp(p) -> str:
    """模板里填的 NTP 服务器（没填就是空串）。

    **不下发默认值**：NTP 服务器每个现场都不一样，而以前的默认值
    `10.0.0.254` 在任何现场都不可达 —— 设备会反复重试、时间永远不同步，
    而且日志里看不出原因。留空 = 生成的设备配置里**不出现** NTP 行，
    由运维按现场填（界面/文档都写清了）。
    """
    return (getattr(p, "ntp_server", "") or "").strip()


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


def _require_ztp_password(p) -> str:
    """设备开局配置里的管理员口令**不允许代填默认值**。

    原实现是 `p.admin_password or "ChangeMe@123"`：这个常量就写在**公开**仓库里，
    等于给"没填口令"的模板发一个全网都知道的设备口令。与 PXE 模块同一口径
    （那边也是"必填、绝不代填默认口令"）：宁可显式失败，也不发弱口令。
    调用方（api/ztp.py）会把 ValueError 映射成 4xx + 中文提示。
    """
    pw = p.admin_password
    if not pw:
        raise ValueError(
            "ZTP 开局必须填写设备管理员口令：出于安全考虑不代填任何默认口令，"
            "请在模板的 admin_password 里填写。"
        )
    return pw


# ============ H3C Comware 7 ============
def h3c_config(dev, p) -> str:
    ip = dev.mgmt_ip or "10.0.0.1"
    user = p.admin_user or "admin"
    pw = _require_ztp_password(p)
    L = [
        "# H3C Comware 7 开局配置 (OpsToolkit 生成)",
        f"# host={dev.hostname} mgmt=" + _mgmt_label(dev, ip, p),
        "sysname " + dev.hostname,
        "#",
        "irf mac-address persistent always",
        "irf auto-update enable",
        "#",
    ]
    L += _vlans_block_h3c(p)
    if dev.mgmt_via_dhcp:
        L += [
            "# 管理口用 DHCP 取址（没有指定管理 IP，写死会与其它设备/地址池冲突）",
            f"interface {p.mgmt_interface}",
            " ip address dhcp-alloc",
            "#",
        ]
    else:
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
        # DHCP 取址时默认路由由 DHCP 给（ZTP 的 dnsmasq 配了 option:router），
        # 再写一条静态默认路由只会在"网关字段填错"时把流量打进黑洞
        f"ip route-static 0.0.0.0 0 {p.mgmt_gateway}" if not dev.mgmt_via_dhcp else "",
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
    ]
    if _ntp(p):
        L += [f"ntp-service unicast-peer {_ntp(p)}", "#"]
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
    pw = _require_ztp_password(p)
    vlanif = p.mgmt_interface.replace("Vlan-interface", "Vlanif")
    L = [
        "# Huawei VRP 开局配置 (OpsToolkit 生成)",
        f"# host={dev.hostname} mgmt=" + _mgmt_label(dev, ip, p),
        "sysname " + dev.hostname,
        "#",
    ]
    L += _vlans_block_huawei(p)
    if dev.mgmt_via_dhcp:
        L += [
            "# 管理口用 DHCP 取址（没有指定管理 IP）",
            f"interface {vlanif}",
            " ip address dhcp-alloc",
            "#",
        ]
    else:
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
        # DHCP 取址时默认路由由 DHCP 给，不写静态默认路由（理由同 h3c 分支）
        f"ip route-static 0.0.0.0 0.0.0.0 {p.mgmt_gateway}" if not dev.mgmt_via_dhcp else "",
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
    ]
    if _ntp(p):
        L += [f"ntp-service unicast-server {_ntp(p)}", "#"]
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
    pw = _require_ztp_password(p)
    enable = p.enable_secret or pw
    vlanif = p.mgmt_interface.replace("Vlan-interface", "Vlan")
    L = [
        "! Cisco IOS-XE 开局配置 (OpsToolkit 生成)",
        f"! host={dev.hostname} mgmt=" + _mgmt_label(dev, ip, p),
        "hostname " + dev.hostname,
        "!",
        "no ip domain-lookup",
    ]
    if p.domain_name:
        L += [f"ip domain-name {p.domain_name}", "!"]
    L += _vlans_block_cisco(p)
    if dev.mgmt_via_dhcp:
        L += [
            "! 管理口用 DHCP 取址（没有指定管理 IP）",
            f"interface {vlanif}",
            " ip address dhcp",
            " no shutdown",
            "!",
        ]
    else:
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
        # DHCP 取址时默认路由由 DHCP 给，不写静态默认路由（理由同 h3c 分支）
        f"ip route 0.0.0.0 0.0.0.0 {p.mgmt_gateway}" if not dev.mgmt_via_dhcp else "",
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
    ]
    if _ntp(p):
        L.append(f"ntp server {_ntp(p)}")
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


def _norm_vendor(vendor) -> str:
    """将厂商统一为小写，未知厂商回落 H3C。"""
    v = (vendor or "").strip().lower()
    return v if v in ("h3c", "huawei", "cisco") else "h3c"


def _ext(vendor) -> str:
    return {"h3c": "cfg", "huawei": "cfg", "cisco": "cfg"}.get(_norm_vendor(vendor), "cfg")


# ============ dnsmasq 投递配置 ============
def _mode_label(mode) -> str:
    return {
        "standalone": "standalone - 独立 DHCP (专用开局网络)",
        "proxy": "proxy - ProxyDHCP (与现有 DHCP 并存)",
        "relay": "relay - 中继模式 (仅 TFTP, 依赖交换机中继)",
    }.get(mode, mode)


def _detect_iface():
    """检测物理网络接口名，容器友好，不依赖 ip 命令。
    优先 ens/eth/enp 开头的物理网卡，跳过 lo/docker/veth/br-。

    **不要拿它去填 dnsmasq 的 interface=**（R4 / RUNBOOK §5.50）：容器里
    os.listdir 的顺序不保证，实测它返回的是 **ens18 —— 承载企业网 10.128.118.113
    的那张卡**。用猜出来的网卡配 standalone 的 dhcp-range，等于在骨干网段上开
    DHCP 池、抢答企业 DHCP。现在只在"模板里明确要求自动探测"时才用，
    并且 `dhcp.check_dhcp_conf_safety` 会在落盘前把这类配置拦下来。
    """
    import os
    try:
        preferred = []
        fallback = []
        for name in sorted(os.listdir("/sys/class/net")):
            if name == "lo" or name.startswith(("docker", "veth", "br-", "virbr")):
                continue
            # 仅选择已 UP 的接口
            try:
                if not open(f"/sys/class/net/{name}/operstate").read().strip() == "up":
                    continue
            except Exception:
                pass
            if name.startswith(("ens", "eth", "enp", "eno")):
                preferred.append(name)
            else:
                fallback.append(name)
        if preferred:
            return preferred[0]
        if fallback:
            return fallback[0]
    except Exception:
        pass
    return "eth0"


def _iface_or_placeholder(p) -> str:
    """配置里该写哪个网卡：**只认模板里显式填的**，没填就写占位值。

    绝不替运维猜网卡：猜错的两个后果都很重 ——
      · 猜成骨干网卡 ⇒ 在骨干网段开 DHCP 池（抢答企业 DHCP）；
      · 猜成不存在的网卡 ⇒ dnsmasq 配了 bind-interfaces 会**起不来**，
        而它同时服务着 PXE，整个装机网段的 DHCP/TFTP 一起没了。
    占位值会在**部署**时被 dhcp.check_dhcp_conf_safety 拒绝（下载 ZIP 不受影响，
    但 README 会提醒必须改）。
    """
    v = (p.dhcp_iface or "").strip()
    if v and v.lower() not in ("auto", "detect"):
        return v
    return "eth0"


def dnsmasq(p, devices) -> str:
    """按厂商下发 DHCP option，把每台设备指向自己的配置文件。

    H3C/华为: option 66 = TFTP server, option 67 = 配置文件名
    思科:     option 150 = TFTP server, option 67 = 配置文件名
    """
    vendor = _norm_vendor(p.vendor)
    srv = p.server_ip or "10.0.0.250"
    # 网卡只用模板里**显式填的**：没填就是占位 eth0（部署时会被红线检查拒绝，
    # 下载 ZIP 仍然可用）。绝不在这里猜 —— 猜错就是骨干网上开 DHCP 池，
    # 或者让 dnsmasq 因为 bind-interfaces + 不存在的网卡而直接起不来（连带打死 PXE）。
    iface = _iface_or_placeholder(p)
    L = [
        "# dnsmasq ZTP 投递配置 (OpsToolkit 生成)",
        f"# 厂商: {vendor}  部署模式: {_mode_label(p.deploy_mode)}",
        # **绝不写 `port=0`**（真机实测，RUNBOOK §5.50）：dnsmasq 的 `port` 是
        # **不可重复**的关键字 —— /etc/dnsmasq.d 下只要有两个文件都写了它，
        # dnsmasq 就会以 `illegal repeated keyword` 拒绝加载**整份**配置，
        # 也就是 `dnsmasq --test` 失败 ⇒ 守护进程起不来（开机也起不来）⇒
        # 整个装机网段没有 DHCP/TFTP。PXE 的配置里已经有 `port=0`，
        # 关 DNS 属于**守护进程级**设置，不属于这份"投递配置"。
        # 只跑 ZTP、不跑 PXE 的宿主机若也想关掉 DNS，请在主配置里加一次 `port=0`。
        f"interface={iface}",
        "bind-interfaces",
        "",
    ]
    if iface == "eth0":
        L += [
            "# ⚠⚠ 未指定 DHCP 网卡：上面的 interface=eth0 是**占位值**，",
            "#    本文件不能直接部署（部署接口会拒绝，见 dhcp.check_dhcp_conf_safety）。",
            "#    请在模板里把「DHCP网卡」填成宿主机上真实存在、且**不承载默认路由**的",
            "#    那张卡（例如专用于开局/装机的 ens19），再重新生成。",
            "#    自动探测不可靠：容器里实测会探测到承载企业网的那张卡，",
            "#    而 standalone 模式会在这张卡上开 DHCP 池、抢答企业 DHCP。",
            "",
        ]
    if p.deploy_mode == "relay":
        L.append("# 中继模式: 不开 DHCP, 仅 TFTP; 交换机 ip-helper 指向本机")
        L.append(f"no-dhcp-interface={iface}")
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
    v = _norm_vendor(p.vendor)
    if v == "huawei":
        return _huawei_midfile(p, devices), "ztp_intermediate.txt"
    if v == "cisco":
        return _cisco_script(p, devices), "ztp_bootstrap.py"
    return _h3c_script(p, devices), "ztp_note.txt"


def _readme(p, devices) -> str:
    v = _norm_vendor(p.vendor)
    iface = _iface_or_placeholder(p)
    warn = ""
    if iface == "eth0":
        warn = ("!! 警告: 模板里没有填「DHCP网卡」, 生成的 dnsmasq 配置里是占位值 "
                "interface=eth0。\n"
                "   这份配置**不能**直接部署: dnsmasq 配了 bind-interfaces, 网卡不存在会\n"
                "   直接起不来(而它同时服务着 PXE); 若填错成骨干网卡, 则会在骨干网段上\n"
                "   开 DHCP 池、抢答企业 DHCP。请填好网卡后重新生成。\n\n")
    return (
        "OpsToolkit ZTP 开局部署说明\n"
        "==========================\n\n"
        f"厂商: {v}\n"
        f"ZTP 服务器: {p.server_ip}\n"
        f"投递模式: {p.deploy_mode}\n"
        f"DHCP 网卡: {iface}\n"
        f"DHCP 地址池: {p.dhcp_start} - {p.dhcp_end}\n"
        f"NTP 服务器: {_ntp(p) or '（未配置：生成的设备配置里不下发 NTP）'}\n\n"
        + warn +
        "步骤:\n"
        "1. 安装 dnsmasq, 把生成的 dnsmasq.conf 放进 /etc/dnsmasq.d/ (例如\n"
        "   /etc/dnsmasq.d/opstk-ztp.conf), 然后 systemctl restart dnsmasq\n"
        "   注意: 本文件**故意不写** port=0 —— dnsmasq 的 port 关键字不可重复,\n"
        "   配置目录里两份文件都写它会让 dnsmasq 直接起不来。若这台机器只跑 ZTP,\n"
        "   请在主配置里自己加一次 port=0 (缺省时 dnsmasq 会同时做 DNS 转发, 无害)。\n\n"
        "2. 建立 TFTP 目录结构:\n"
        f"   {p.tftp_root}/ztp/  放入各设备 .cfg 与 default.cfg\n\n"
        f"3. (可选) HTTP 服务器镜像 {p.http_root} 提供大文件下载\n\n"
        "4. 新设备空配置上电, 接入开局网络, 自动获取配置\n\n"
        "下发方式(两种, 可同时用):\n"
        "  · **免登记**: 所有设备都拿 ztp/default.cfg — 一份基础配置; 其中管理口用\n"
        "    DHCP 取址(不写死 IP, 否则多台设备会撞同一个地址), 开局后从地址池\n"
        "    " + p.dhcp_start + "-" + p.dhcp_end + " 拿到地址(在 DHCP 服务器上按 MAC 认领)。\n"
        "  · **按设备差异化**: 在模板里登记设备(MAC + 主机名/序列号)后,\n"
        "    每台设备拿到自己的 ztp/<序列号或主机名>.cfg(含各自的管理 IP/主机名)。\n"
        "    DHCP 是按 **MAC** 匹配的 ⇒ 只填序列号、不填 MAC 的设备仍会拿 default.cfg。\n\n"
        f"本批次登记设备: {len(devices)} 台\n"
        "NTP 说明:\n"
        "  NTP 服务器**每个现场都不一样**, 所以这里不代填默认值:\n"
        "  模板里留空 = 设备配置里不出现 NTP 行(设备时间不会同步);\n"
        "  要下发就按**现场真实**的 NTP 地址填进模板, 再重新生成。\n\n"
        "厂商要点:\n"
        "  H3C   : auto-config, DHCP option 66(TFTP) + 67(文件名)\n"
        "  华为  : ZTP, DHCP option 66(TFTP) + 67(中间文件) + 中间文件描述下载项\n"
        "  思科  : IOS-XE ZTP, DHCP option 150(TFTP) + 67(脚本/配置)\n\n"
        "!! 安全提醒:\n"
        "   生成的设备配置里含**管理员口令**, 而 ztp/ 下的文件是通过 TFTP/HTTP\n"
        "   **无认证**提供给设备的 ⇒ 开局网段上的任何主机都能读到它。\n"
        "   请: (1) 把开局网段与生产/办公网段物理或 VLAN 隔离;\n"
        "       (2) 按现场修改模板里的设备管理员口令, 不要用出厂默认值;\n"
        "       (3) 开局完成后及时删掉 TFTP/HTTP 上的配置(或撤掉 ZTP 投递配置)。\n"
        f"本批次登记设备: {len(devices)} 台\n"
    )


def generate_all(p, devices=None):
    devices = devices or []
    files = {}
    vendor = _norm_vendor(p.vendor)
    gen = VENDOR_CONFIG.get(vendor, h3c_config)
    for d in devices:
        stem = _file_stem(d)
        files[f"ztp/{stem}.{_ext(vendor)}"] = gen(d, p)
    # 未登记设备的兜底配置：**无论有没有登记设备都必须生成**。
    # 为什么（RUNBOOK §5.54）：dnsmasq 的全局 option 67 一直是 `ztp/default.cfg`，
    # 而这里原来写成 `if devices:` —— 一个设备都没登记时**根本不生成这个文件**，
    # 于是设备按 option 67 去取一个不存在的文件，ZTP 完全不可用
    # （表现：部署产物只有 ztp_note.txt + README，看起来"少东西"但接口报成功）。
    # 另外这份配置**不再写死管理 IP**：以前填的是 `p.dhcp_start`，
    # 两台以上未登记设备会配成同一个地址（还会和地址池里的租约撞车）；
    # 现在管理口走 DHCP 取址（ZtpDevice.mgmt_via_dhcp）。
    files["ztp/default.cfg"] = gen(ZtpDevice(hostname="default"), p)
    files["dnsmasq.conf"] = dnsmasq(p, devices)
    inter, inter_name = intermediate(p, devices)
    files[f"ztp/{inter_name}"] = inter
    files["README.txt"] = _readme(p, devices)
    return files
