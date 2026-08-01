"""服务器网络配置脚本生成器。统一用 nmcli，RHEL 8+ 与 Ubuntu 22.04+ 通用。"""
from __future__ import annotations


BOND_MODES = {
    0: "balance-rr",
    1: "active-backup",
    2: "balance-xor",
    3: "broadcast",
    4: "802.3ad",
    5: "balance-tlb",
    6: "balance-alb",
}

BOND_MODE_NAMES = {
    0: "balance-rr (轮询)",
    1: "active-backup (主备, 推荐)",
    2: "balance-xor (源目MAC哈希)",
    3: "broadcast (广播)",
    4: "802.3ad (LACP, 需交换机配置)",
    5: "balance-tlb (自适应发送负载)",
    6: "balance-alb (自适应负载)",
}


def _netmask_to_cidr(netmask: str) -> int:
    try:
        parts = [int(p) for p in netmask.split(".")]
        bits = sum(bin(p).count("1") for p in parts)
        return bits if 0 <= bits <= 32 else 24
    except Exception:
        return 24


def _cidr_to_netmask(cidr: int) -> str:
    cidr = int(cidr)
    bits = (0xFFFFFFFF << (32 - cidr)) & 0xFFFFFFFF
    return ".".join(str((bits >> (8 * i)) & 0xFF) for i in range(3, -1, -1))


def _prefix(obj) -> int:
    if obj.get("cidr"):
        return int(obj["cidr"])
    if obj.get("netmask"):
        return _netmask_to_cidr(obj["netmask"])
    return 24


def _ipv4_lines(obj) -> list:
    lines = []
    if not obj.get("ip"):
        lines.append("ipv4.method auto")
        return lines
    lines.append(f"ipv4.addresses {obj['ip']}/{_prefix(obj)}")
    lines.append("ipv4.method manual")
    if obj.get("gateway"):
        lines.append(f"ipv4.gateway {obj['gateway']}")
    if obj.get("dns"):
        lines.append("ipv4.dns " + ";".join(obj["dns"]))
    return lines


def _mod(cmds, name, obj):
    lines = _ipv4_lines(obj)
    if lines:
        cmds.append("nmcli connection modify " + name + " " + " ".join(lines))


def _iface(cmds, obj):
    name = obj["name"]
    cmds.append("# 接口 " + name)
    cmds.append(f"nmcli connection add type ethernet ifname {name} con-name {name}")
    _mod(cmds, name, obj)
    cmds.append(f"nmcli connection up {name}")
    cmds.append("")


def _bond(cmds, obj):
    name = obj["name"]
    mode = int(obj.get("mode", 1))
    mode_str = BOND_MODES.get(mode, "active-backup")
    cmds.append(f"# 聚合 {name} 模式={mode}:{mode_str}")
    opts = f"mode={mode_str},miimon={obj.get('miimon', 100)}"
    if obj.get("primary"):
        opts += f",primary={obj['primary']}"
    cmds.append(f"nmcli connection add type bond ifname {name} con-name {name} bond.options {opts}")
    _mod(cmds, name, obj)
    for i in obj.get("interfaces", []):
        sub = f"{name}-slave-{i}"
        cmds.append(f"nmcli connection add type ethernet ifname {i} con-name {sub} master {name}")
        cmds.append(f"nmcli connection up {sub}")
    cmds.append(f"nmcli connection up {name}")
    cmds.append("")


def _vlan(cmds, obj):
    name = f"{obj['parent']}.{obj['vlan_id']}"
    cmds.append(f"# VLAN {name}")
    cmds.append(f"nmcli connection add type vlan ifname {name} con-name {name} dev {obj['parent']} id {obj['vlan_id']}")
    _mod(cmds, name, obj)
    cmds.append(f"nmcli connection up {name}")
    cmds.append("")


def _bridge(cmds, obj):
    name = obj["name"]
    cmds.append(f"# 网桥 {name}")
    cmds.append(f"nmcli connection add type bridge ifname {name} con-name {name}")
    _mod(cmds, name, obj)
    for i in obj.get("interfaces", []):
        sub = f"{name}-port-{i}"
        cmds.append(f"nmcli connection add type ethernet ifname {i} con-name {sub} master {name}")
        cmds.append(f"nmcli connection up {sub}")
    cmds.append(f"nmcli connection up {name}")
    cmds.append("")


# RHEL 8+ 使用 NetworkManager (nmcli) 生成配置脚本
def _build_nmcli(req):
    cmds = []
    cmds.append("#!/bin/bash")
    cmds.append("# 由 OpsToolkit 网络配置生成器自动生成")
    cmds.append(f"# 目标系统: {req.os}  后端: NetworkManager (nmcli)")
    cmds.append("set -e")
    cmds.append("")
    cmds.append("# 检查 nmcli 是否可用")
    cmds.append("if ! command -v nmcli >/dev/null 2>&1; then")
    cmds.append('  echo "错误: 未找到 nmcli，请先安装 NetworkManager: dnf install NetworkManager"')
    cmds.append("  exit 1")
    cmds.append("fi")
    cmds.append("")
    if req.hostname:
        cmds.append(f"hostnamectl set-hostname {req.hostname}")
        cmds.append("")
    # 收集 bond/bridge 从接口，跳过独立配置
    nm_bond_slaves = set()
    for b in req.bonds:
        nm_bond_slaves.update(b.interfaces)
    nm_bridge_slaves = set()
    for br in req.bridges:
        nm_bridge_slaves.update(br.interfaces)

    for o in req.interfaces:
        if o.name in nm_bond_slaves or o.name in nm_bridge_slaves:
            continue
        _iface(cmds, o.model_dump())
    for o in req.bonds:
        _bond(cmds, o.model_dump())
    for o in req.vlans:
        _vlan(cmds, o.model_dump())
    for o in req.bridges:
        _bridge(cmds, o.model_dump())
    cmds.append("# 应用完成。可执行 nmcli connection show 复核。")
    return "\n".join(cmds) + "\n"


def _netplan_addr_block(out, indent, obj, dhcp_default=False):
    """输出 netplan 地址/routes/nameservers 块。indent 为属性缩进 (6 空格)。"""
    if obj.get("mode") == "dhcp":
        out.append(f"{indent}dhcp4: true")
        return
    if not obj.get("ip"):
        if dhcp_default:
            out.append(f"{indent}dhcp4: true")
        return
    p = _prefix(obj)
    out.append(f"{indent}dhcp4: false")
    out.append(f"{indent}addresses: [{obj['ip']}/{p}]")
    if obj.get("gateway"):
        out.append(f"{indent}routes:")
        out.append(f"{indent}  - to: default")
        out.append(f"{indent}    via: {obj['gateway']}")
    if obj.get("dns"):
        out.append(f"{indent}nameservers:")
        out.append(f"{indent}  addresses: [{', '.join(obj['dns'])}]")


# Ubuntu 22.04+ 使用 netplan 生成 YAML 配置 ，默认 renderer=networkd
def _build_netplan(req):
    out = []
    out.append("# /etc/netplan/99-opstk.yaml")
    out.append("network:")
    out.append("  version: 2")
    renderer = getattr(req, "netplan_renderer", "networkd") or "networkd"
    out.append(f"  renderer: {renderer}")
    ind = "    "
    # 收集被 bond/bridge 引用的从接口，避免重复配置 IP
    bond_slaves = set()
    for b in req.bonds:
        bond_slaves.update(b.interfaces)
    bridge_slaves = set()
    for br in req.bridges:
        bridge_slaves.update(br.interfaces)

    if req.interfaces:
        out.append("  ethernets:")
        for o in req.interfaces:
            # 如果该接口被 bond 或 bridge 引用，仅设为禁用状态
            if o.name in bond_slaves or o.name in bridge_slaves:
                out.append(f"{ind}{o.name}:")
                out.append(f"{ind}  dhcp4: false")
                continue
            out.append(f"{ind}{o.name}:")
            _netplan_addr_block(out, ind + "  ", o.model_dump(), dhcp_default=True)
    # 网卡聚合 (bond)：支持 active-backup/802.3ad 等模式
    if req.bonds:
        out.append("  bonds:")
        for o in req.bonds:
            ms = BOND_MODES.get(int(o.mode), "active-backup")
            out.append(f"{ind}{o.name}:")
            out.append(f"{ind}  interfaces: [{', '.join(o.interfaces)}]")
            out.append(f"{ind}  parameters:")
            out.append(f"{ind}    mode: {ms}")
            out.append(f"{ind}    miimon: {o.miimon}")
            if o.primary:
                out.append(f"{ind}    primary: {o.primary}")
            _netplan_addr_block(out, ind + "  ", o.model_dump())
    # VLAN 子接口：从父接口创建 tagged sub-interface
    if req.vlans:
        out.append("  vlans:")
        for o in req.vlans:
            vname = f"{o.parent}.{o.vlan_id}"
            out.append(f"{ind}{vname}:")
            out.append(f"{ind}  id: {o.vlan_id}")
            out.append(f"{ind}  link: {o.parent}")
            _netplan_addr_block(out, ind + "  ", o.model_dump())
    # 网桥 (bridge)：将多个接口归入同一二层广播域
    if req.bridges:
        out.append("  bridges:")
        for o in req.bridges:
            out.append(f"{ind}{o.name}:")
            out.append(f"{ind}  interfaces: [{', '.join(o.interfaces)}]")
            _netplan_addr_block(out, ind + "  ", o.model_dump())
    return "\n".join(out) + "\n"


# RHEL/CentOS 7+ 传统 ifcfg 文件格式（无需 NetworkManager）
def _build_ifcfg(req):
    """生成 /etc/sysconfig/network-scripts/ifcfg-* 文件。"""
    files = {}
    bond_slaves = set()
    for b in req.bonds:
        bond_slaves.update(b.interfaces)
    bridge_ports = set()
    for br in req.bridges:
        bridge_ports.update(br.interfaces)

    def _ip_lines(obj):
        lines = []
        if obj.get("mode") == "dhcp" or not obj.get("ip"):
            lines.append("BOOTPROTO=dhcp")
        else:
            lines.append("BOOTPROTO=static")
            lines.append("IPADDR=" + str(obj["ip"]))
            lines.append("PREFIX=" + str(_prefix(obj)))
            gw = obj.get("gateway")
            if gw:
                lines.append("GATEWAY=" + str(gw))
            dns = obj.get("dns") or []
            for i, d in enumerate(dns, 1):
                lines.append("DNS" + str(i) + "=" + str(d))
        lines.append("ONBOOT=yes")
        return lines

    # Interfaces
    for o in req.interfaces:
        name = o.name
        lines = ["DEVICE=" + name, "TYPE=Ethernet"]
        if name in bond_slaves:
            master = next((b.name for b in req.bonds if name in b.interfaces), "")
            lines += ["MASTER=" + master, "SLAVE=yes", "ONBOOT=yes"]
        elif name in bridge_ports:
            master = next((br.name for br in req.bridges if name in br.interfaces), "")
            lines += ["BRIDGE=" + master, "ONBOOT=yes"]
        else:
            lines += _ip_lines(o.model_dump())
        files["ifcfg-" + name] = "\n".join(lines) + "\n"

    # ? bond/bridge ??? interfaces ????????????
    all_ifaces = {o.name for o in req.interfaces}
    for b in req.bonds:
        for ifname in b.interfaces:
            if ifname not in all_ifaces:
                files["ifcfg-" + ifname] = "DEVICE=" + ifname + "\nTYPE=Ethernet\nMASTER=" + b.name + "\nSLAVE=yes\nONBOOT=yes\n"
    for br in req.bridges:
        for ifname in br.interfaces:
            if ifname not in all_ifaces:
                files["ifcfg-" + ifname] = "DEVICE=" + ifname + "\nTYPE=Ethernet\nBRIDGE=" + br.name + "\nONBOOT=yes\n"

    # Bonds
    for o in req.bonds:
        name = o.name
        mode = int(o.mode or 1)
        mode_str = BOND_MODES.get(mode, "active-backup")
        opts = "mode=" + mode_str + " miimon=" + str(o.miimon or 100)
        if o.primary:
            opts += " primary=" + o.primary
        lines = ["DEVICE=" + name, "TYPE=Bond", "BONDING_MASTER=yes", "BONDING_OPTS=\"" + opts + "\""]
        lines += _ip_lines({"ip": o.ip, "cidr": o.cidr, "gateway": o.gateway, "dns": o.dns, "netmask": o.netmask})
        files["ifcfg-" + name] = "\n".join(lines) + "\n"

    # VLANs
    for o in req.vlans:
        name = o.parent + "." + str(o.vlan_id)
        lines = ["DEVICE=" + name, "TYPE=Vlan", "VLAN=yes", "PHYSDEV=" + o.parent]
        lines += _ip_lines({"ip": o.ip, "cidr": o.cidr, "gateway": o.gateway, "netmask": o.netmask})
        files["ifcfg-" + name] = "\n".join(lines) + "\n"

    # Bridges
    for o in req.bridges:
        name = o.name
        lines = ["DEVICE=" + name, "TYPE=Bridge"]
        lines += _ip_lines({"ip": o.ip, "cidr": o.cidr, "gateway": o.gateway, "netmask": o.netmask})
        files["ifcfg-" + name] = "\n".join(lines) + "\n"

    # Combine output
    result = ["# ===== 网络配置文件 (ifcfg格式) ====="]
    result.append("# 存放位置: /etc/sysconfig/network-scripts/")
    result.append("# 生成后执行: systemctl restart network")
    result.append("")
    for fname in sorted(files.keys()):
        result.append("# ===== " + fname + " =====")
        result.append(files[fname])
        result.append("")
    return "\n".join(result)


def generate_netconfig(req):
    """根据请求参数分发到 netplan、nmcli 或 ifcfg 生成器。"""
    if req.format == "netplan" and req.os == "ubuntu":
        return _build_netplan(req), "99-opstk.yaml"
    if req.format == "ifcfg":
        return _build_ifcfg(req), "ifcfg-files.txt"
    return _build_nmcli(req), "apply-network.sh"
