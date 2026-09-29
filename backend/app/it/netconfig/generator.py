"""服务器网络配置脚本生成器。

三种输出格式，与 os 严格配套（非法组合在 schemas 与 generate_netconfig 两层拦下）：
  · netplan —— 仅 Ubuntu 22.04+（/etc/netplan/99-opstk.yaml）
  · ifcfg   —— 仅 RHEL 家族（/etc/sysconfig/network-scripts/ifcfg-*）
  · nmcli   —— 通用（RHEL 8+ / Ubuntu 22.04+ 上的 NetworkManager）
"""
from __future__ import annotations

import json
import re
import shlex

# 安全标量：原样输出；否则做引用。shlex.quote 对 [A-Za-z0-9_@%+=:,./-] 是恒等变换，
# 因此对正常输入（eth0 / 10.0.0.1 / bond0.100 / mode=active-backup,miimon=100）
# 产出与修复前逐字节一致，不改变既有输出；仅对含特殊字符的值加引用。
_YAML_SAFE = re.compile(r"^[A-Za-z0-9._/-]+$")


_LACP_RATES = ("slow", "fast")
_XMIT_POLICIES = ("layer2", "layer2+3", "layer3+4", "encap2+3", "encap3+4", "vlan+srcmac")


def _bond_opt_value(value, field, kind="enum"):
    """bond 选项值白名单（外部审查 U4-F5）。

    这些值会进**逗号/空格分隔**的 `bond.options` / `BONDING_OPTS`：
    值里带一个 `,` 或空格就能追加任意 bonding 参数（`primary="eth0,mode=broadcast"`），
    或者塞非法值让 `nmcli connection add` 失败 —— 而脚本是 `set -e`，
    中途失败会留下改了一半的网络。所以只放行"确定合法"的写法。
    """
    v = str(value).strip()
    if not v or any(ch in v for ch in (",", "=", '"', "'")) or any(ch.isspace() for ch in v):
        raise ValueError("%s 不能含逗号/等号/引号/空白（会往 bond 选项里追加参数）：%r"
                         % (field, v[:40]))
    if kind == "ifname":
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,32}", v):
            raise ValueError("%s 不是合法的接口名：%r" % (field, v[:40]))
        return v
    if field.endswith("lacp_rate"):
        if v not in _LACP_RATES:
            raise ValueError("%s 只允许 %s：%r" % (field, "/".join(_LACP_RATES), v[:40]))
        return v
    if v not in _XMIT_POLICIES:
        raise ValueError("%s 只允许 %s：%r"
                         % (field, "/".join(_XMIT_POLICIES), v[:40]))
    return v


def _safe_line(v, field="字段") -> str:
    """第 3 层兜底：值会被原样拼进 shell 脚本 / netplan YAML / ifcfg 文件，
    而三者都以**换行分隔**行 —— 值里出现换行或任何控制字符（含 TAB/CR）就等于注入一整行：

      os="ubuntu\\nid > /tmp/pwned #"     → apply-network.sh 的注释行变成一条会被真正执行的
        命令（运维正是被指引去执行这个脚本的）；
      netplan_renderer="networkd\\nid #"  → 99-opstk.yaml 里多出一个任意 YAML 键。

    第 2 层（schemas.NetConfigRequest 的 os/format/netplan_renderer 白名单）已经拦一次；
    这里再挡一次，保证绕过 HTTP 层直接调用 generate_netconfig 的路径同样注入不进去。
    """
    s = str(v if v is not None else "")
    for ch in s:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise ValueError(
                f"{field} 不允许包含换行或控制字符（会造成网络配置/命令注入）"
            )
    return s


def _sh(v, field="字段") -> str:
    """落进 shell 的值：POSIX 安全词（先过 _safe_line 的控制字符闸）。"""
    return shlex.quote(_safe_line(v, field))


def _yaml(v, field="字段") -> str:
    """落进 YAML 的值：安全则原样，否则 JSON 双引号标量（合法 YAML）。"""
    s = _safe_line(v, field)
    return s if _YAML_SAFE.match(s) else json.dumps(s)


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

# 默认路由 metric：只有当一次请求里出现**多个**默认网关时才写出来。
#
# 缺陷：改前两个网卡各带一个网关会生成两条完全同 metric 的 `to: default`/ipv4.gateway，
# 实际生效的那条取决于内核选路与接口 up 顺序 —— 症状是时通时不通、流量走错网卡。
# 处理方式（已与 schemas.NetConfigRequest 的分工写死）：不拒绝这种拓扑（管理口 + bond
# 各一个网关是合法且常见的），而是按**声明顺序**给每个默认路由一个确定的 metric：
# 第一个 100、第二个 200、第三个 300…… 数字越小优先级越高，内核优先走 metric 最小的
# 那条，它不可用时才回退到下一条，因而既是确定的、又天然带主备语义。
#
# 顺序与三份生成器的输出顺序一致：物理口（跳过 bond/bridge 从接口）→ bond → vlan → bridge。
# 只保留一个默认网关时返回空表 —— 单网关的输出与改前逐字节一致（既有配置零变化）。
DEFAULT_ROUTE_METRIC_BASE = 100
DEFAULT_ROUTE_METRIC_STEP = 100


def _has_default_route(o) -> bool:
    """该设备是否真的会产生一条默认路由。

    三个生成器都只在「有 ip」时才写 gateway（见 _ipv4_lines / _netplan_addr_block /
    _ip_lines），mode=dhcp 的设备根本不会走到那段。用同一个条件取设备，metric 编号才不会错位。
    """
    if getattr(o, "mode", "static") == "dhcp":
        return False
    return bool(getattr(o, "ip", None)) and bool(getattr(o, "gateway", None))


def _route_metrics(req) -> dict:
    """{(kind, index): metric}；只有多个默认网关时才非空。"""
    slaves = set()
    for b in req.bonds:
        slaves.update(b.interfaces)
    for br in req.bridges:
        slaves.update(br.interfaces)
    order = []
    for i, o in enumerate(req.interfaces):
        if o.name not in slaves and _has_default_route(o):
            order.append(("iface", i))
    for i, o in enumerate(req.bonds):
        if _has_default_route(o):
            order.append(("bond", i))
    for i, o in enumerate(req.vlans):
        if _has_default_route(o):
            order.append(("vlan", i))
    for i, o in enumerate(req.bridges):
        if _has_default_route(o):
            order.append(("bridge", i))
    if len(order) <= 1:
        return {}
    return {key: DEFAULT_ROUTE_METRIC_BASE + i * DEFAULT_ROUTE_METRIC_STEP for i, key in enumerate(order)}


def _netmask_to_cidr(netmask: str) -> int:
    """点分十进制掩码 → 前缀长度。**必须校验**（外部审查 U4-F8）。

    旧实现只数 1 的个数：`255.255.0.255`（非连续掩码）会算出 24，`255.255`（两段）
    或乱码也静默变成 24 —— 于是"校验说网关同子网、实际生成的地址却在另一网段"，
    机器静默落到错误的网段。这里：必须 4 段、每段 0-255、且 32 位是**连续的 1**。
    """
    parts = str(netmask or "").split(".")
    if len(parts) != 4:
        raise ValueError("netmask 必须是 4 段点分十进制（例如 255.255.255.0）：%r" % netmask)
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError("netmask 含非数字段：%r" % netmask)
    if any(n < 0 or n > 255 for n in nums):
        raise ValueError("netmask 每段必须在 0-255：%r" % netmask)
    bits = "".join(format(n, "08b") for n in nums)
    ones = bits.count("1")
    if "1" in bits[ones:] or "0" in bits[:ones]:
        raise ValueError("netmask 的 1 必须连续（例如 255.255.254.0），收到：%r" % netmask)
    return ones


def _cidr_to_netmask(cidr: int) -> str:
    cidr = int(cidr)
    bits = (0xFFFFFFFF << (32 - cidr)) & 0xFFFFFFFF
    return ".".join(str((bits >> (8 * i)) & 0xFF) for i in range(3, -1, -1))


def _prefix(obj) -> int:
    # TODO(IPv6): 只处理 IPv4 —— netmask 是点分十进制 IPv4 掩码，缺省按 /24。
    # 需要 IPv6 时这里要整体改成按地址族取前缀（netplan 还要写 addresses6/dhcp6）。
    # 第 2 层（schemas.NetConfigRequest）已用同一个默认值 /24 做网关同子网检查，
    # 两处默认值必须保持一致，否则会出现"校验通过但生成的默认路由不可达"。
    if obj.get("cidr"):
        # ★ 外部审查 U4-F8：原来直接 int() 不校验范围，cidr=64 会生成 `x/64` /
        #   `PREFIX=64`，nmcli/ifcfg 中途报错，而脚本是 set -e ⇒ 留下改了一半的网络。
        cidr = int(obj["cidr"])
        if not 0 <= cidr <= 32:
            raise ValueError("cidr 前缀长度必须在 0-32：%r" % obj["cidr"])
        return cidr
    if obj.get("netmask"):
        return _netmask_to_cidr(obj["netmask"])
    return 24


def _ipv4_lines(obj, metric=None) -> list:
    lines = []
    if not obj.get("ip"):
        lines.append("ipv4.method auto")
        if obj.get("dns"):
            # DHCP 口 + 手填 DNS 是合法且常见的（覆盖 DHCP 下发的 DNS）。
            # 改前这里直接 return，DNS 静默丢失。
            lines.append("ipv4.dns " + _sh(" ".join(str(d) for d in obj["dns"])))
        return lines
    lines.append(f"ipv4.addresses {_sh(obj['ip'])}/{_prefix(obj)}")
    lines.append("ipv4.method manual")
    if obj.get("gateway"):
        lines.append(f"ipv4.gateway {_sh(obj['gateway'])}")
        if metric is not None:
            # 有多个默认网关时写死 metric，避免"谁生效取决于接口 up 顺序"
            lines.append(f"ipv4.route-metric {int(metric)}")
    if obj.get("dns"):
        # 修 D1：原来是 ";".join(...) —— 未加引号的 ; 会被 bash 当命令分隔符，
        # set -e 下脚本在此中止，后面的 nmcli connection up 永不执行。
        # nmcli 接受「一个参数内空格分隔」的 DNS，故整体引用成单个 shell 词。
        lines.append("ipv4.dns " + _sh(" ".join(str(d) for d in obj["dns"])))
    return lines


def _mod(cmds, name, obj, metric=None):
    lines = _ipv4_lines(obj, metric)
    if lines:
        cmds.append("nmcli connection modify " + _sh(name) + " " + " ".join(lines))


def _iface(cmds, obj, metric=None):
    name = obj["name"]
    cmds.append("# 接口 " + _safe_line(name, "interfaces[].name"))
    cmds.append(f"nmcli connection add type ethernet ifname {_sh(name)} con-name {_sh(name)}")
    _mod(cmds, name, obj, metric)
    cmds.append(f"nmcli connection up {_sh(name)}")
    cmds.append("")


def _bond(cmds, obj, metric=None):
    name = obj["name"]
    mode = int(obj.get("mode", 1))
    mode_str = BOND_MODES.get(mode, "active-backup")
    cmds.append(f"# 聚合 {_safe_line(name, 'bonds[].name')} 模式={mode}:{mode_str}")
    opts = f"mode={mode_str},miimon={int(obj.get('miimon', 100))}"
    # ★ 外部审查 U4-F5：这三个值直接进**逗号分隔**的 bond.options，值里的 `,`/`=`
    #   可以追加任意 bonding 参数（例如 primary="eth0,mode=broadcast"）。白名单化。
    if obj.get("primary"):
        opts += f",primary={_bond_opt_value(obj['primary'], 'bonds[].primary', 'ifname')}"
    if obj.get("lacp_rate"):
        opts += f",lacp_rate={_bond_opt_value(obj['lacp_rate'], 'bonds[].lacp_rate', 'enum')}"
    if obj.get("xmit_hash_policy"):
        opts += (f",xmit_hash_policy="
                 + _bond_opt_value(obj['xmit_hash_policy'], 'bonds[].xmit_hash_policy', 'enum'))
    cmds.append(f"nmcli connection add type bond ifname {_sh(name)} con-name {_sh(name)} bond.options {_sh(opts)}")
    _mod(cmds, name, obj, metric)
    for i in obj.get("interfaces", []):
        sub = f"{name}-slave-{i}"
        cmds.append(f"nmcli connection add type ethernet ifname {_sh(i)} con-name {_sh(sub)} master {_sh(name)}")
        cmds.append(f"nmcli connection up {_sh(sub)}")
    cmds.append(f"nmcli connection up {_sh(name)}")
    cmds.append("")


def _vlan(cmds, obj, metric=None):
    name = f"{obj['parent']}.{obj['vlan_id']}"
    cmds.append(f"# VLAN {_safe_line(name, 'vlans[].name')}")
    cmds.append(f"nmcli connection add type vlan ifname {_sh(name)} con-name {_sh(name)} "
                f"dev {_sh(obj['parent'])} id {int(obj['vlan_id'])}")
    _mod(cmds, name, obj, metric)
    cmds.append(f"nmcli connection up {_sh(name)}")
    cmds.append("")


def _bridge(cmds, obj, metric=None):
    name = obj["name"]
    cmds.append(f"# 网桥 {_safe_line(name, 'bridges[].name')}")
    cmds.append(f"nmcli connection add type bridge ifname {_sh(name)} con-name {_sh(name)}")
    _mod(cmds, name, obj, metric)
    for i in obj.get("interfaces", []):
        sub = f"{name}-port-{i}"
        cmds.append(f"nmcli connection add type ethernet ifname {_sh(i)} con-name {_sh(sub)} master {_sh(name)}")
        cmds.append(f"nmcli connection up {_sh(sub)}")
    cmds.append(f"nmcli connection up {_sh(name)}")
    cmds.append("")


# RHEL 8+ 使用 NetworkManager (nmcli) 生成配置脚本
def _build_nmcli(req):
    cmds = []
    cmds.append("#!/bin/bash")
    cmds.append("# 由 OpsToolkit 网络配置生成器自动生成")
    # os 是本行唯一的插值：改前它未校验，os="ubuntu\nid > /tmp/pwned #" 会让下面多出一整行
    # 可执行命令（HTTP 层已在 NetConfigRequest 白名单拦下，这里是绕过接口时的兜底）。
    cmds.append(f"# 目标系统: {_safe_line(req.os, 'os')}  后端: NetworkManager (nmcli)")
    cmds.append("set -e")
    cmds.append("")
    cmds.append("# 检查 nmcli 是否可用")
    cmds.append("if ! command -v nmcli >/dev/null 2>&1; then")
    cmds.append('  echo "错误: 未找到 nmcli，请先安装 NetworkManager: dnf install NetworkManager"')
    cmds.append("  exit 1")
    cmds.append("fi")
    cmds.append("")
    if req.hostname:
        cmds.append(f"hostnamectl set-hostname {_sh(req.hostname)}")
        cmds.append("")
    # 收集 bond/bridge 从接口，跳过独立配置
    nm_bond_slaves = set()
    for b in req.bonds:
        nm_bond_slaves.update(b.interfaces)
    nm_bridge_slaves = set()
    for br in req.bridges:
        nm_bridge_slaves.update(br.interfaces)

    # 多个默认网关时按声明顺序写死 metric（详见 _route_metrics 上方注释）
    metrics = _route_metrics(req)
    for i, o in enumerate(req.interfaces):
        if o.name in nm_bond_slaves or o.name in nm_bridge_slaves:
            continue
        _iface(cmds, o.model_dump(), metrics.get(("iface", i)))
    for i, o in enumerate(req.bonds):
        _bond(cmds, o.model_dump(), metrics.get(("bond", i)))
    for i, o in enumerate(req.vlans):
        _vlan(cmds, o.model_dump(), metrics.get(("vlan", i)))
    for i, o in enumerate(req.bridges):
        _bridge(cmds, o.model_dump(), metrics.get(("bridge", i)))
    cmds.append("# 应用完成。可执行 nmcli connection show 复核。")
    return "\n".join(cmds) + "\n"


def _netplan_dns(out, indent, dns):
    """netplan 的 nameservers 块。dhcp4: true 与 nameservers 并存是合法的。"""
    if dns:
        out.append(f"{indent}nameservers:")
        out.append(f"{indent}  addresses: [{', '.join(_yaml(d) for d in dns)}]")


def _netplan_addr_block(out, indent, obj, dhcp_default=False, metric=None):
    """输出 netplan 地址/routes/nameservers 块。indent 为属性缩进 (6 空格)。"""
    dns = obj.get("dns") or []
    if obj.get("mode") == "dhcp":
        out.append(f"{indent}dhcp4: true")
        # 改前 dhcp 分支直接 return，静默丢掉 DNS（netplan 里 dhcp4+nameservers 是合法的）
        _netplan_dns(out, indent, dns)
        return
    if not obj.get("ip"):
        if dhcp_default:
            out.append(f"{indent}dhcp4: true")
            _netplan_dns(out, indent, dns)
        # 无地址、也不走 DHCP（bond/bridge/vlan 的 L2 场景）时不写任何地址块：
        # 这里不写 nameservers —— 单独的 nameservers 没有地址承载，语义不清。
        return
    p = _prefix(obj)
    out.append(f"{indent}dhcp4: false")
    out.append(f"{indent}addresses: [{_yaml(str(obj['ip']) + '/' + str(p))}]")
    if obj.get("gateway"):
        out.append(f"{indent}routes:")
        out.append(f"{indent}  - to: default")
        out.append(f"{indent}    via: {_yaml(obj['gateway'])}")
        if metric is not None:
            out.append(f"{indent}    metric: {int(metric)}")
    _netplan_dns(out, indent, dns)


# Ubuntu 22.04+ 使用 netplan 生成 YAML 配置 ，默认 renderer=networkd
def _build_netplan(req):
    out = []
    out.append("# /etc/netplan/99-opstk.yaml")
    out.append("network:")
    out.append("  version: 2")
    # ★ 外部审查 U7-F7 的实证根因：netplan **要求** bond/bridge 的从接口与 vlan 的
    #   `link:` 都在同一份配置里被定义过，否则整份文件被拒：
    #     bond0: interface 'ens35' is not defined      （netplan 0.107.1 @ ubuntu 22.04）
    #     ens99.100: interface 'ens99' is not defined
    #   而改前只有当请求里显式写了 interfaces[] 行时才输出 ethernets:，
    #   于是「加一个 bond、从接口填 ens35,ens36、不另加物理接口行」这种最常见的用法
    #   （前端 bond 行默认就是 eth0,eth1）生成出来的 YAML 在真机上是**加载不了**的 ——
    #   既有用例 test_netplan_full 正是这个形状，只断言了字符串所以没暴露。
    #   ifcfg 分支早就有对应处理（从接口没声明就单独生成一份 ifcfg-<slave>），
    #   这里补齐 netplan 侧的同一件事：把"被引用但没声明"的口子补成 L2 态 ethernet。
    #   已经声明过的设备（含 bond/bridge/vlan 子接口）不重复输出 —— 同名重复键会让
    #   netplan 报 "changes device type" 或静默丢掉一份定义。
    # ★ 外部审查 U4-F11：renderer 是 YAML 的标量，`|` / `>` / `&a` / `*a` 这类结构字符
    #   虽然不含控制字符，却会把后面的整份缩进块吞掉（ethernets/bonds 在 netplan 里"消失"，
    #   apply 之后机器静默断网）。所以这里改成**白名单**，与文件开头"两层都拦"的说法对齐。
    renderer = str(getattr(req, "netplan_renderer", "networkd") or "networkd").strip()
    if renderer not in ("networkd", "NetworkManager"):
        raise ValueError(
            "netplan_renderer 只允许 networkd 或 NetworkManager，收到：%r" % renderer[:40])
    out.append(f"  renderer: {renderer}")
    ind = "    "
    # 收集被 bond/bridge 引用的从接口，避免重复配置 IP
    bond_slaves = set()
    for b in req.bonds:
        bond_slaves.update(b.interfaces)
    bridge_slaves = set()
    for br in req.bridges:
        bridge_slaves.update(br.interfaces)
    metrics = _route_metrics(req)

    # 被引用（bond 从接口 / 网桥端口 / vlan 父接口）但没有自己的定义的名字 → 补成 L2 ethernet。
    # 顺序固定（排序）以保证同一请求每次生成逐字节一致。
    virtual_names = {o.name for o in req.bonds}
    virtual_names.update(o.name for o in req.bridges)
    virtual_names.update(f"{o.parent}.{o.vlan_id}" for o in req.vlans)
    referenced = set(bond_slaves) | set(bridge_slaves) | {o.parent for o in req.vlans}
    declared_ifaces = {o.name for o in req.interfaces}
    implicit_ifaces = sorted(referenced - declared_ifaces - virtual_names)

    if req.interfaces or implicit_ifaces:
        out.append("  ethernets:")
        for i, o in enumerate(req.interfaces):
            # 如果该接口被 bond 或 bridge 引用，仅设为禁用状态
            if o.name in bond_slaves or o.name in bridge_slaves:
                out.append(f"{ind}{_yaml(o.name)}:")
                out.append(f"{ind}  dhcp4: false")
                continue
            out.append(f"{ind}{_yaml(o.name)}:")
            _netplan_addr_block(
                out, ind + "  ", o.model_dump(), dhcp_default=True,
                metric=metrics.get(("iface", i)),
            )
        for name in implicit_ifaces:
            # DHCP/地址都没有：netplan 里留空即"不上电配置"，与 ifcfg 侧
            # `DEVICE=x / MASTER=bond0 / SLAVE=yes` 那份文件语义一致。
            out.append(f"{ind}{_yaml(name, '从接口/父接口名')}:")
            out.append(f"{ind}  dhcp4: false")
    # 网卡聚合 (bond)：支持 active-backup/802.3ad 等模式
    if req.bonds:
        out.append("  bonds:")
        for i, o in enumerate(req.bonds):
            ms = BOND_MODES.get(int(o.mode), "active-backup")
            out.append(f"{ind}{_yaml(o.name)}:")
            out.append(f"{ind}  interfaces: [{', '.join(_yaml(s) for s in o.interfaces)}]")
            out.append(f"{ind}  parameters:")
            out.append(f"{ind}    mode: {ms}")
            # ★ 外部审查 U4-F3：netplan 的 bonding 参数是**内核名的 kebab-case**，
            #   没有 `miimon` 这个键（同段的 lacp-rate / transmit-hash-policy 都对，
            #   唯独这里漏了）⇒ 写 `mii-monitor-interval`。
            out.append(f"{ind}    mii-monitor-interval: {int(o.miimon)}")
            if o.primary:
                out.append(f"{ind}    primary: {_yaml(o.primary)}")
            if o.lacp_rate:
                out.append(f"{ind}    lacp-rate: {_yaml(o.lacp_rate)}")
            if o.xmit_hash_policy:
                out.append(f"{ind}    transmit-hash-policy: {_yaml(o.xmit_hash_policy)}")
            _netplan_addr_block(out, ind + "  ", o.model_dump(), metric=metrics.get(("bond", i)))
    # VLAN 子接口：从父接口创建 tagged sub-interface
    if req.vlans:
        out.append("  vlans:")
        for i, o in enumerate(req.vlans):
            vname = f"{o.parent}.{o.vlan_id}"
            out.append(f"{ind}{_yaml(vname)}:")
            out.append(f"{ind}  id: {int(o.vlan_id)}")
            out.append(f"{ind}  link: {_yaml(o.parent)}")
            _netplan_addr_block(out, ind + "  ", o.model_dump(), metric=metrics.get(("vlan", i)))
    # 网桥 (bridge)：将多个接口归入同一二层广播域
    if req.bridges:
        out.append("  bridges:")
        for i, o in enumerate(req.bridges):
            out.append(f"{ind}{_yaml(o.name)}:")
            out.append(f"{ind}  interfaces: [{', '.join(_yaml(s) for s in o.interfaces)}]")
            _netplan_addr_block(out, ind + "  ", o.model_dump(), metric=metrics.get(("bridge", i)))
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

    def _ip_lines(obj, metric=None):
        lines = []
        dns = obj.get("dns") or []
        if obj.get("mode") == "dhcp" or not obj.get("ip"):
            lines.append("BOOTPROTO=dhcp")
        else:
            lines.append("BOOTPROTO=static")
            lines.append("IPADDR=" + _sh(obj["ip"]))
            lines.append("PREFIX=" + str(_prefix(obj)))
            gw = obj.get("gateway")
            if gw:
                lines.append("GATEWAY=" + _sh(gw))
                if metric is not None:
                    # 多个默认网关时写死 METRIC（initscripts 与 NM 的 ifcfg-rh 都认）
                    lines.append("METRIC=" + str(int(metric)))
        # DHCP 分支也要写 DNS：改前整段只在 static 分支里，配了 DNS 的 DHCP 口会静默丢 DNS
        for i, d in enumerate(dns, 1):
            lines.append("DNS" + str(i) + "=" + _sh(d))
        lines.append("ONBOOT=yes")
        return lines

    metrics = _route_metrics(req)

    # Interfaces
    for i, o in enumerate(req.interfaces):
        name = o.name
        lines = ["DEVICE=" + _sh(name), "TYPE=Ethernet"]
        if name in bond_slaves:
            master = next((b.name for b in req.bonds if name in b.interfaces), "")
            lines += ["MASTER=" + _sh(master), "SLAVE=yes", "ONBOOT=yes"]
        elif name in bridge_ports:
            master = next((br.name for br in req.bridges if name in br.interfaces), "")
            lines += ["BRIDGE=" + _sh(master), "ONBOOT=yes"]
        else:
            lines += _ip_lines(o.model_dump(), metrics.get(("iface", i)))
        files["ifcfg-" + _sh(name)] = "\n".join(lines) + "\n"

    # ? bond/bridge ??? interfaces ????????????
    all_ifaces = {o.name for o in req.interfaces}
    for b in req.bonds:
        for ifname in b.interfaces:
            if ifname not in all_ifaces:
                files["ifcfg-" + _sh(ifname)] = ("DEVICE=" + _sh(ifname) + "\nTYPE=Ethernet\nMASTER=" + _sh(b.name) + "\nSLAVE=yes\nONBOOT=yes\n")
    for br in req.bridges:
        for ifname in br.interfaces:
            if ifname not in all_ifaces:
                files["ifcfg-" + _sh(ifname)] = ("DEVICE=" + _sh(ifname) + "\nTYPE=Ethernet\nBRIDGE=" + _sh(br.name) + "\nONBOOT=yes\n")

    # Bonds
    for i, o in enumerate(req.bonds):
        name = o.name
        mode = int(o.mode or 1)
        mode_str = BOND_MODES.get(mode, "active-backup")
        opts = "mode=" + mode_str + " miimon=" + str(o.miimon or 100)
        # 同 nmcli 分支（外部审查 U4-F5）：这几个值不能把参数"追加"进 BONDING_OPTS。
        # ifcfg 里是空格分隔，且整个串包在双引号里，所以这里用校验后的裸值（不再 _sh，
        # 原来 _sh 会把引号变成**字面量**写进 BONDING_OPTS，本身就不是合法参数）。
        if o.primary:
            opts += " primary=" + _bond_opt_value(o.primary, "bonds[].primary", "ifname")
        if o.lacp_rate:
            opts += " lacp_rate=" + _bond_opt_value(o.lacp_rate, "bonds[].lacp_rate", "enum")
        if o.xmit_hash_policy:
            opts += (" xmit_hash_policy="
                     + _bond_opt_value(o.xmit_hash_policy, "bonds[].xmit_hash_policy", "enum"))
        lines = ["DEVICE=" + _sh(name), "TYPE=Bond", "BONDING_MASTER=yes", "BONDING_OPTS=\"" + opts + "\""]
        lines += _ip_lines(
            {"ip": o.ip, "cidr": o.cidr, "gateway": o.gateway, "dns": o.dns, "netmask": o.netmask},
            metrics.get(("bond", i)),
        )
        files["ifcfg-" + _sh(name)] = "\n".join(lines) + "\n"

    # VLANs
    for i, o in enumerate(req.vlans):
        name = o.parent + "." + str(o.vlan_id)
        lines = ["DEVICE=" + _sh(name), "TYPE=Vlan", "VLAN=yes", "PHYSDEV=" + _sh(o.parent)]
        # mode/dns 要一起传：改前手工拼的 dict 漏了 dns，前端填的 VLAN DNS 会静默消失
        lines += _ip_lines(
            {"ip": o.ip, "cidr": o.cidr, "gateway": o.gateway, "netmask": o.netmask,
             "mode": o.mode, "dns": o.dns},
            metrics.get(("vlan", i)),
        )
        files["ifcfg-" + _sh(name)] = "\n".join(lines) + "\n"

    # Bridges
    for i, o in enumerate(req.bridges):
        name = o.name
        lines = ["DEVICE=" + _sh(name), "TYPE=Bridge"]
        lines += _ip_lines(
            {"ip": o.ip, "cidr": o.cidr, "gateway": o.gateway, "netmask": o.netmask,
             "mode": o.mode, "dns": o.dns},
            metrics.get(("bridge", i)),
        )
        files["ifcfg-" + _sh(name)] = "\n".join(lines) + "\n"

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
    """根据请求参数分发到 netplan、nmcli 或 ifcfg 生成器。

    第 3 层兜底（第 2 层是 schemas.NetConfigRequest）：HTTP 入口已经拦下
    os×format 的非法组合与未知 format，这里再拦一次，防止绕过校验直接调用时
    「要 netplan 却拿到 nmcli 脚本」（RHEL 上根本没有 /etc/netplan）。

    默认网关：一次请求里出现多个默认网关时不拒绝（管理口 + bond 各一个网关是合法拓扑），
    而是由 _route_metrics 按声明顺序写出确定的 metric（100/200/300…），
    避免改前那种「多条同 metric 的 to: default，谁生效看接口 up 顺序」。
    """
    if req.format == "netplan":
        if req.os != "ubuntu":
            raise ValueError(
                f"format='netplan' 仅支持 os='ubuntu'（当前 os={req.os!r}）："
                f"RHEL 上没有 /etc/netplan"
            )
        return _build_netplan(req), "99-opstk.yaml"
    if req.format == "ifcfg":
        if req.os != "rhel":
            raise ValueError(
                f"format='ifcfg' 仅支持 os='rhel'（当前 os={req.os!r}）："
                f"Ubuntu 上没有 /etc/sysconfig/network-scripts"
            )
        return _build_ifcfg(req), "ifcfg-files.txt"
    if req.format != "nmcli":
        raise ValueError(
            f"unsupported format {req.format!r}: must be one of nmcli/netplan/ifcfg"
        )
    return _build_nmcli(req), "apply-network.sh"
