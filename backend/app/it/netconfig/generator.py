"""服务器网络配置脚本生成器。统一用 nmcli，RHEL 8+ 与 Ubuntu 22.04+ 通用。"""
from __future__ import annotations

from app.core.schemas import NetConfigRequest

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


def _build_nmcli(req):
    cmds = []
    cmds.append("#!/bin/bash")
    cmds.append("# 由 OpsToolkit 网络配置生成器自动生成")
    cmds.append(f"# 目标系统: {req.os}  后端: NetworkManager (nmcli)")
    cmds.append("set -e")
    cmds.append("")
    if req.hostname:
        cmds.append(f"hostnamectl set-hostname {req.hostname}")
        cmds.append("")
    for o in req.interfaces:
        _iface(cmds, o.model_dump())
    for o in req.bonds:
        _bond(cmds, o.model_dump())
    for o in req.vlans:
        _vlan(cmds, o.model_dump())
    for o in req.bridges:
        _bridge(cmds, o.model_dump())
    cmds.append("# 应用完成。可执行 nmcli connection show 复核。")
    return "\n".join(cmds) + "\n"


def _build_netplan(req):
    out = []
    out.append("# /etc/netplan/99-opstk.yaml")
    out.append("network:")
    out.append("  version: 2")
    out.append("  renderer: NetworkManager")
    out.append("  ethernets:")
    ind = "    "
    for o in req.interfaces:
        out.append(f"{ind}{o.name}:")
        if o.mode == "dhcp":
            out.append(f"{ind}  dhcp4: true")
        else:
            p = _prefix(o.model_dump())
            out.append(f"{ind}  dhcp4: false")
            out.append(f"{ind}  addresses: [{o.ip}/{p}]")
            if o.gateway:
                out.append(f"{ind}  routes:")
                out.append(f"{ind}    - to: default")
                out.append(f"{ind}      via: {o.gateway}")
        if o.dns:
            out.append(f"{ind}  nameservers:")
            out.append(f"{ind}    addresses: [{', '.join(o.dns)}]")
    if req.bonds:
        out.append("  bonds:")
        for o in req.bonds:
            ms = BOND_MODES.get(int(o.mode), "active-backup")
            out.append(f"{ind}{o.name}:")
            out.append(f"{ind}  interfaces: [{', '.join(o.interfaces)}]")
            out.append(f"{ind}  parameters:")
            out.append(f"{ind}    mode: {ms}")
            out.append(f"{ind}    miimon: {o.miimon}")
            if o.ip:
                p = _prefix(o.model_dump())
                out.append(f"{ind}  addresses: [{o.ip}/{p}]")
                if o.gateway:
                    out.append(f"{ind}  routes:")
                    out.append(f"{ind}    - to: default")
                    out.append(f"{ind}      via: {o.gateway}")
    return "\n".join(out) + "\n"


def generate_netconfig(req):
    if req.format == "netplan" and req.os == "ubuntu":
        return _build_netplan(req), "99-opstk.yaml"
    return _build_nmcli(req), "apply-network.sh"
