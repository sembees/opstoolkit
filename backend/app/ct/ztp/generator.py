"""ZTP 配置开局生成器。

为 H3C / 华为 / 思科 设备生成：
  1. 每台设备的开局配置文件 (设备 CLI 语法)
  2. dnsmasq 投递配置 (按厂商下发 DHCP option 66/67/150/141)
  3. 厂商中间文件 (华为 midfile / 思科 python 脚本 / H3C 脚本)
  4. 部署说明 README
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from app.ct.ztp.positions import norm_mac as _norm_mac_text


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
    position: str = ""          # 落位编码（来自落位登记；仅用于注释/兜底主机名）

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


def _require_clean(value, field, allow_space=True):
    """要拼进 **dnsmasq 配置** 或 **设备命令行** 的"单值"字段必须是干净的。

    为什么 fail-closed 而不是转义（外部审查 F3/F7 指出、我复核确认）：
      · `option:bootfile-name,"ztp/<stem>.cfg"` 里的引号会截断字符串，**换行**会直接
        多出**一条新的 dnsmasq 指令**；
      · `sysname/domain/snmp-agent community` 同理，换行等于向设备开局配置里注入 CLI。
    最坏情况是注入一条"能过 `dnsmasq --test`、又不落在 6 条红线里"的指令
    （例如 `dhcp-script=`），而这份配置会随 dnsmasq 生效，影响**整个装机网段**。
    所以这里直接拒绝：引号、换行、制表、控制字符（口令另加：不允许空格，
    因为空格会把 CLI 命令拆成两个参数）。
    """
    s = "" if value is None else str(value)
    bad = [ch for ch in s if ch in ('"', "\n", "\r", "\t") or ord(ch) < 32]
    if not allow_space and any(ch.isspace() for ch in s):
        bad.append(" ")
    if bad:
        raise ValueError(
            "%s 含不允许的字符（引号/换行/制表/控制字符%s）：%r。"
            "这些值会被拼进 dnsmasq 配置与设备命令行，换行可以注入额外指令，"
            "所以不允许出现 —— 请改成单行的普通文本。"
            % (field, "或空格" if not allow_space else "", s[:60])
        )
    return s


def _check_profile_values(p):
    """模板里的"单值"字段统一过一遍（domain/SNMP/NTP/用户名/口令/管理接口）。

    返回值是把管理接口归一/推导后的名字，并写回 `p`（空值按厂商补 SVI 形式）。
    """
    for name, val, allow_space in (("domain_name", p.domain_name, True),
                                   ("snmp_community", p.snmp_community, True),
                                   ("ntp_server", p.ntp_server, True),
                                   ("admin_user", p.admin_user, False),
                                   ("admin_password", p.admin_password, False)):
        if val:
            _require_clean(val, "模板字段 " + name, allow_space=allow_space)
    p.mgmt_interface = _require_vlan_mgmt_interface(p)
    return p.mgmt_interface


# 管理接口可以是 **VLAN 接口**（SVI：`Vlan-interface10` / `Vlanif10` / `Vlan10`），
# 也可以是**物理口**（U3-2nd-F6：真机验证完成后放开，见 RUNBOOK §5.74）。
_VLAN_IFACE_PREFIXES = ("vlan-interface", "vlanif", "vlan")


def _is_vlan_interface(iface: str) -> bool:
    return (iface or "").strip().lower().startswith(_VLAN_IFACE_PREFIXES)


# 物理口形式（真机见过 + 各厂商文档常见）：GigabitEthernet1/0/24、GE1/0/24、WGE1/0/4、
# Ten-GigabitEthernet1/0/1、10GE1/0/1、25GE1/0/4、HundredGigE1/0/25、M-GigabitEthernet0/0/0、
# MEth0/0/0、Ethernet1/0/1，以及聚合口 Eth-Trunk1 / Port-channel1 / Bridge-Aggregation1。
# 判定不写死型号：**名字里必须带「数字(/数字)+」**（这样 400GE1/0/1 这类新型号不会被误拦），
# 而 `eth0`、`uplink` 这类"不像交换机接口名"的写法仍然挡在外面。
# ★ 写这个正则时我自己踩了一次：第一版写成 `(?:\d+/\d+)+` —— 那只能匹配"数字/数字"**成对**的
#   形式，`1/0/24` 是三段，于是**所有**物理口都被判成非法（用例一跑就露）。机器规则也必须被
#   用例验证，不能"看着对"就上线。
_PHYS_IFACE_RE = re.compile(
    r"^(?:(?:\d+)?[A-Za-z][A-Za-z0-9.\-]*\d+(?:/\d+)+"
    r"|(?:Eth-Trunk|Port-channel|Bridge-Aggregation|Route-Aggregation)\d+)"
    r"(?::\d+)?$")

# 物理口做管理口时，**先把二层口切成三层口**的命令 —— 全部来自厂商语法，其中
# H3C / 华为 VRP8 两条是**真机回读验证过**的（RUNBOOK §5.74 有原文），另外两条验不了：
#   · h3c（Comware 7，S6850 7.1.070，✅ 真机验证）：
#       二层口配 `ip address` 被拒 —— 真机原文 `% Unrecognized command found at '^' position.`，
#       且回读配置里确实没有那行；`port link-mode route` 之后才配得上（有回读原文）。
#       另外真机看到：S6850 的 25G 口**默认就是 route 模式**（没碰过的 WGE1/0/5 回读也是
#       `port link-mode route`），所以这一行在这类机型上是幂等的；但切模式会弹
#       `... will be restored to the default. Continue? [Y/N]` —— 会清掉该口原有配置，
#       这句话必须让运维看见（见下面产物里的注释）。
#   · huawei-ce（VRP8 / CE6800 V200R005，✅ 真机验证）：二层口配 `ip address` 被拒
#       （`Error: Unrecognized command found at '^' position.`），`undo portswitch` + `commit`
#       之后配得上（有回读原文）。
#   · huawei（VRP5，❌ 本环境验不了：EVE 里只有 VRP8 镜像）⇒ 按文档写法生成并标注未验证。
#   · cisco（IOS-XE，❌ 本环境验不了：EVE 里只有 IOS-XR 镜像）⇒ 同上。
_PHYS_L3_CMD = {
    "h3c": ["port link-mode route"],
    "huawei": ["undo portswitch"],
    "huawei-ce": ["undo portswitch", "commit"],
    "cisco": ["no switchport"],
}
_PHYS_L3_UNVERIFIED = ("huawei", "cisco")


def _phys_l3_switch_lines(p, vendor: str, comment: str = "#") -> list:
    """管理口是**物理口**时，插在 `interface <管理口>` 之后、`ip address` 之前的切模式命令。

    VLAN 接口（SVI）返回 `[]` —— 老路径的输出因此**逐字不变**。
    """
    if _is_vlan_interface(_iface_or_derived(p)):
        return []
    cmds = _PHYS_L3_CMD.get(vendor) or []
    out = [comment + " ⚠ 物理口做管理口：先切成三层口再配地址；切模式会把该口的"
                     "原有配置恢复成默认（真机验证见 RUNBOOK §5.74）"]
    out += [" " + c for c in cmds]
    if vendor in _PHYS_L3_UNVERIFIED:
        out.append(comment + " ⚠ 上面这条切三层命令**本环境没有真机可验**（EVE 里没有该平台镜像）"
                             "—— 按厂商文档生成，首次上机请先手工敲一条确认")
    return out


def _iface_or_derived(p) -> str:
    """模板里填的管理接口；没填就按厂商 + 管理 VLAN 推导 SVI（与最终产物同一规则）。"""
    iface = (getattr(p, "mgmt_interface", "") or "").strip()
    if iface:
        return iface
    vendor = _norm_vendor(getattr(p, "vendor", "h3c"))
    vid = int(getattr(p, "mgmt_vlan", 0) or 1)
    tmpl = {"huawei": "Vlanif%d", "huawei-ce": "Vlanif%d", "cisco": "Vlan%d"}.get(
        vendor, "Vlan-interface%d")
    return tmpl % vid


def _require_vlan_mgmt_interface(p) -> str:
    """管理接口名（名字是历史遗留，现在**两种形态都支持**）：空 → 推导 SVI；VLAN 口/物理口 → 放行。

    为什么要区分这两种形态（U3-2nd-F6 的真根源）：改前是**从接口名尾部抠数字当 VLAN 号**
    （`GigabitEthernet1/0/24` → VLAN 24），于是模板里明明写的是"把管理 IP 配在这个物理口上"，
    生成出来的却是「凭空新建 `vlan 24` + 把上联/接入口划进 VLAN 24 + 在 GE1/0/24 上配 IP」——
    端口会因此被挪出它原本的 VLAN（上联口可能就是 trunk），而 Comware 上物理口默认是二层口，
    `ip address` 根本不生效（要先把端口切三层）。那是"看着对、其实把网络改坏"的配置。

    现在物理口这条路已经**真机验证过**（RUNBOOK §5.74：H3C 与华为 VRP8 都有回读原文），
    所以放开，但按平台补切三层的命令、且**不再**从接口名里抠 VLAN 号
    （物理口做三层口不进任何 VLAN；端口 VLAN 一律用模板里的「管理 VLAN 号」）。
    """
    iface = (getattr(p, "mgmt_interface", "") or "").strip()
    if iface:
        # 第 3 层兜底：接口名会原样拼进设备命令行 —— 换行即注入一整条命令
        iface = _require_clean(iface, "管理接口")
    if not iface:
        return _iface_or_derived(p)
    if _is_vlan_interface(iface) or _PHYS_IFACE_RE.match(iface):
        return iface
    raise ValueError(
        "模板的「管理接口」既不像 VLAN 接口（Vlan-interface10 / Vlanif10 / Vlan10），"
        "也不像物理口（GigabitEthernet1/0/24 / GE1/0/24 / WGE1/0/4 / 10GE1/0/1 …）：%r。"
        "这个值会被原样拼进设备命令行，所以只接受上面两种形态；"
        "留空则由生成器按厂商 + 管理 VLAN 推导成 Vlan-interface<N>/Vlanif<N>/Vlan<N>。" % iface
    )


def _file_stem(dev) -> str:
    """配置文件名主干：优先 serial，其次 hostname。

    **必须过 _require_clean**：这个值会进文件名，而文件名会进 dnsmasq 的
    `option:bootfile-name,"ztp/<stem>.cfg"` —— 带引号或换行就能注入配置。
    """
    stem = (dev.serial or dev.hostname or "device").strip()
    return _require_clean(stem, "设备文件名主干(serial/hostname)", allow_space=False)


def _position_device(pos) -> ZtpDevice:
    """把一条落位记录（dict 或 ORM 对象）转成生成器设备。

    主机名为空时用**落位编码**兜底 —— 生成的配置里 sysname 不能是空；
    落位编码本身记进 ZtpDevice.position，dnsmasq 注释/文件名规则都可引用。
    落位记录的 mac 统一过 norm_mac：解析不了就当"待认领"处理（绝不把
    乱码 MAC 写进 dnsmasq）。
    """

    def _get(key) -> str:
        if isinstance(pos, dict):
            v = pos.get(key, "")
        else:
            v = getattr(pos, key, "")
        return (v or "").strip() if isinstance(v, str) else ""

    position = _get("position")
    return ZtpDevice(
        hostname=_get("hostname") or position,
        mac=_norm_mac_text(_get("mac")),
        serial=_get("serial"),
        mgmt_ip=_get("mgmt_ip"),
        position=position,
    )


# ============ 通用片段 ============
def _vlan_id_of_interface(iface, fallback):
    """从 `Vlan-interface10` / `Vlanif10` 里解析出 VLAN 号；解析不出就用 fallback。

    为什么需要它：真机上管理 VLAN **必须先存在**，SVI（Vlan-interfaceX）才配得上、
    `ip address dhcp-alloc` 才是合法命令（H3C Comware 7 实测）。而模板里
    "管理 VLAN 号"和"管理接口名"是两个字段，运维很容易只改其中一个 ——
    如果只按 `mgmt_vlan` 建 VLAN、却按接口名配 SVI，就会出现"建了 VLAN 10、
    却在配 Vlan-interface100"，设备直接报错。这里以**接口名为准**，
    并在两者不一致时往配置里写一行醒目注释。

    ★ 但**物理口做管理口**时**不解析**（U3-2nd-F6 的真根源）：`GigabitEthernet1/0/24`
      用 `(\\d+)$` 抠出来的是 **24**，于是"凭空新建 vlan 24、还把上联/接入口划进去"。
      物理口做三层口根本不进任何 VLAN —— 端口 VLAN 一律用模板里的「管理 VLAN 号」。
      真机依据（RUNBOOK §5.74）：WGE1/0/4 切成 bridge 后 `ip address` 被拒
      （`% Unrecognized command found at '^' position.`），切回 route 才配得上。
    """
    if not _is_vlan_interface(iface):
        return int(fallback or 1), False
    m = re.search(r"(\d+)\s*$", str(iface or ""))
    if m:
        return int(m.group(1)), True
    return int(fallback or 1), False


def _ports_vlan_id(p) -> int:
    """接入/上联端口该划进哪个 VLAN —— 必须与 SVI 用**同一个**解析结果。

    外部审查 F4（我复核确认）：`_ensure_vlan_lines_h3c` 按接口名推出 VLAN 100 并建了它，
    而端口却按 `p.mgmt_vlan`(10) 划 —— 端口与管理口落在不同 VLAN，
    管理 IP 从接入端口根本不可达，而配置里还有一行"以接口名为准"的注释自相矛盾。
    """
    return _vlan_id_of_interface(p.mgmt_interface, p.mgmt_vlan)[0]


def _ensure_vlan_lines_h3c(p) -> list:
    """保证管理 VLAN 存在（H3C）。返回要插到 SVI 之前的行。"""
    vid, parsed = _vlan_id_of_interface(p.mgmt_interface, p.mgmt_vlan)
    lines = []
    if parsed and int(p.mgmt_vlan or 0) != vid:
        lines.append("# ⚠ 模板里「管理 VLAN 号=%s」与「管理接口=%s」不一致："
                     "本配置以接口名为准，按 VLAN %d 生成（请回模板统一，否则接入端口会划错 VLAN）"
                     % (p.mgmt_vlan, p.mgmt_interface, vid))
    existing = set()
    for v in (p.vlans or []):
        try:
            existing.add(int(v.get("id", v.get("vlan"))))
        except (TypeError, ValueError):
            continue
    if vid not in existing:
        lines += [f"vlan {vid}", " description MGMT", "#"]
    return lines


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
    """华为的 VLAN 创建：**管理 VLAN 必须先进 batch**。

    VRP8（CE/NE 系列）实测：Vlanif 之前没有 `vlan <id>`，接口配不上。
    另外模板里"管理 VLAN 号"与"管理接口名"不一致时（运维只改了一个），
    以**接口名**为准并在文件里留一行醒目注释。
    """
    vid, parsed = _vlan_id_of_interface(p.mgmt_interface, p.mgmt_vlan)
    warn = []
    if parsed and int(p.mgmt_vlan or 0) != vid:
        warn = ["# ⚠ 模板里「管理 VLAN 号=%s」与「管理接口=%s」不一致："
                "本配置以接口名为准，按 VLAN %d 生成（请回模板统一）"
                % (p.mgmt_vlan, p.mgmt_interface, vid)]
    ids, existing = [], set()
    for v in (p.vlans or []):
        try:
            n = int(v.get("id", v.get("vlan")))
        except (TypeError, ValueError):
            continue
        if n not in existing:
            existing.add(n)
            ids.append(str(n))
    if vid not in existing:
        ids.insert(0, str(vid))
    lines = warn + [f"vlan batch {','.join(ids)}", "#"]
    for v in (p.vlans or []):
        if v.get("name"):
            lines += [f"vlan {v.get('id', v.get('vlan'))}",
                      f" description {v.get('name')}", "#"]
    return lines


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


def _ensure_vlan_lines_cisco(p) -> list:
    """思科同样要**先建 VLAN**：IOS-XE 上 SVI 对应的 VLAN 不存在时，
    接口会一直是 down（这个坑很隐蔽：配置看着全对，就是不通）。"""
    vid, parsed = _vlan_id_of_interface(p.mgmt_interface, p.mgmt_vlan)
    lines = []
    if parsed and int(p.mgmt_vlan or 0) != vid:
        lines.append("! ⚠ 模板里「管理 VLAN 号=%s」与「管理接口=%s」不一致："
                     "本配置以接口名为准，按 VLAN %d 生成"
                     % (p.mgmt_vlan, p.mgmt_interface, vid))
    existing = set()
    for v in (p.vlans or []):
        try:
            existing.add(int(v.get("id", v.get("vlan"))))
        except (TypeError, ValueError):
            continue
    if vid not in existing:
        lines += [f"vlan {vid}", "!"]
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
    _check_profile_values(p)
    host = _require_clean(dev.hostname, "主机名")
    L = [
        "# H3C Comware 7 开局配置 (OpsToolkit 生成)",
        f"# host={host} mgmt=" + _mgmt_label(dev, ip, p),
        "sysname " + host,
        "#",
        "irf mac-address persistent always",
        "irf auto-update enable",
        "#",
    ]
    L += _vlans_block_h3c(p)
    # 管理 VLAN 必须先存在，SVI 才配得上（真机实测；这里保证它一定被创建）
    L += _ensure_vlan_lines_h3c(p)
    if dev.mgmt_via_dhcp:
        L += [
            "# 管理口用 DHCP 取址（没有指定管理 IP，写死会与其它设备/地址池冲突）",
            f"interface {p.mgmt_interface}",
        ] + _phys_l3_switch_lines(p, "h3c") + [
            " ip address dhcp-alloc",
            "#",
        ]
    else:
        L += [
            f"interface {p.mgmt_interface}",
        ] + _phys_l3_switch_lines(p, "h3c") + [
            f" ip address {ip} {p.mgmt_netmask}",
            "#",
        ]
    if p.access_ports:
        L.append("# 接入端口划入管理 VLAN")
        for port in p.access_ports:
            L += [f"interface {port}", " port link-mode bridge",
                  f" port access vlan {_ports_vlan_id(p)}", "#"]
    if p.uplink_port:
        L += [f"interface {p.uplink_port}", " port link-mode bridge",
              f" port access vlan {_ports_vlan_id(p)}", "#"]
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
        # Comware 7 上**没有** `stelnet server enable` 这条命令（旧版本/别的产品线才有），
        # 真机上敲下去是报错的；开 SSH 服务就是 `ssh server enable`。
        "ssh server enable",
        "#",
        "# ---- 关掉不用的明文/网页管理面（真机验证这几条在 Comware 7 上可用）----",
        "undo telnet server enable",
        "undo ip http enable",
        "undo ip https enable",
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
def _require_huawei_community(p) -> str:
    """华为的 SNMP 团体名：**VRP8/CE 系列要求 8-32 个字符**（真机实测）。

    VRP5 上 1-32 都能用，但为了"一份模板在两种平台上都不出错"，这里统一按 8-32 卡。
    默认值 `public`（6 位）在 CE 上会被设备**拒绝这一行**，于是 SNMP 悄悄用不了 ——
    与其发一份设备不认的配置，不如在这里显式失败（与本项目"口令绝不代填"同一口径）。
    """
    comm = (p.snmp_community or "").strip()
    if not comm:
        raise ValueError(
            "华为模板必须填写 SNMP 团体名（snmp_community）：ZTP 基线里 SNMP 是必配项，"
            "留空会生成一条设备拒绝的命令。"
        )
    if len(comm) < 8 or len(comm) > 32:
        raise ValueError(
            "华为设备的 SNMP 团体名必须是 8-32 个字符（VRP8/CE 系列实测要求），"
            "当前是 %r（%d 个字符）。请改成至少 8 位，例如 Opstk@2026。"
            % (comm, len(comm))
        )
    return comm


def _require_vrp8_username(p) -> str:
    """VRP8（CE/NE）本地用户名**至少 6 个字符**（真机实测）。

    证据（CE6800，V200R005）：`local-user ?` 的帮助是
        `STRING<6-253>  User name, ... If the user already exists, do not check
         the minimum length of username`
    而模板默认的 `admin_user` 就是 `admin`（5 位）——在 CE 上 `local-user admin …`
    会直接 `Error: Wrong parameter`，**整段建账号的命令全部失败**（SSH 登不上，
    而配置看着"下发成功"）。这类失败必须在生成期就拦住。
    """
    user = (p.admin_user or "").strip()
    if not user:
        raise ValueError("模板必须填写设备管理员用户名（admin_user）。")
    if len(user) < 6:
        raise ValueError(
            "华为 VRP8（CE/NE）要求本地用户名**至少 6 个字符**（真机实测："
            "`local-user ?` 帮助为 STRING<6-253>）。当前是 %r（%d 位），"
            "在 CE 上设备会拒绝、账号建不出来，请改成 6 位以上（例如 opstkadm）。"
            % (user, len(user))
        )
    return user


def huawei_config(dev, p, vrp8=False) -> str:
    """华为 VRP 开局配置。vrp8=True 时按 **CE/NE（VRP8）** 的语法生成。

    两者在真机上验证到的差异（都别凭印象改）：
      · 本地账号：VRP5 用 `privilege level 15`；**VRP8 没有这条**（Unrecognized），
        管理员权限靠内置用户组 `user-group manage-ug`；密码用 `irreversible-cipher`
        （`cipher` 期望的是**密文**，传明文会被拒）。
      · 用户名：VRP8 要求 ≥6 位（见 _require_vrp8_username）。
      · NTP：VRP5 是 `ntp-service unicast-server`；**VRP8 是 `ntp unicast-server`**
        （`ntp-service …` 在 CE 上是 Unrecognized）。
      · 结尾：VRP8 有配置提交语义，`return` 会弹 `[Y/N/C]` 交互确认 ⇒ 用 `commit` 收尾。
    """
    ip = dev.mgmt_ip or "10.0.0.1"
    user = _require_vrp8_username(p) if vrp8 else (p.admin_user or "admin")
    pw = _require_ztp_password(p)
    community = _require_huawei_community(p)
    _check_profile_values(p)
    host = _require_clean(dev.hostname, "主机名")
    vlanif = p.mgmt_interface.replace("Vlan-interface", "Vlanif")
    L = [
        "# Huawei VRP 开局配置 (OpsToolkit 生成)",
        f"# host={host} mgmt=" + _mgmt_label(dev, ip, p),
        "sysname " + host,
        "#",
    ]
    L += _vlans_block_huawei(p)
    if dev.mgmt_via_dhcp:
        L += [
            "# 管理口用 DHCP 取址（没有指定管理 IP）",
            f"interface {vlanif}",
        ] + _phys_l3_switch_lines(p, "huawei-ce" if vrp8 else "huawei") + [
            " ip address dhcp-alloc",
            "#",
        ]
    else:
        L += [
            f"interface {vlanif}",
        ] + _phys_l3_switch_lines(p, "huawei-ce" if vrp8 else "huawei") + [
            f" ip address {ip} {p.mgmt_netmask}",
            "#",
        ]
    if p.access_ports:
        L.append("# 接入端口划入管理 VLAN")
        for port in p.access_ports:
            L += [f"interface {port}", " port link-type access",
                  f" port default vlan {_ports_vlan_id(p)}", "#"]
    if p.uplink_port:
        L += [f"interface {p.uplink_port}", " port link-type access",
              f" port default vlan {_ports_vlan_id(p)}", "#"]
    L += [
        # DHCP 取址时默认路由由 DHCP 给，不写静态默认路由（理由同 h3c 分支）
        f"ip route-static 0.0.0.0 0.0.0.0 {p.mgmt_gateway}" if not dev.mgmt_via_dhcp else "",
        "#",
    ]
    if vrp8:
        L += [
            "# ---- AAA 本地账号（VRP8/CE 语法，真机逐条验过）----",
            "aaa",
            # VRP8 用 irreversible-cipher 收**明文**；`cipher` 收的是密文，传明文会被拒
            f" local-user {user} password irreversible-cipher {pw}",
            f" local-user {user} service-type ssh",
            # VRP8 没有 `privilege level`（Unrecognized），管理员权限用内置用户组
            f" local-user {user} user-group manage-ug",
            "#",
        ]
    else:
        L += [
            "# ---- AAA 本地账号 ----",
            "aaa",
            f" local-user {user} password cipher {pw}",
            f" local-user {user} privilege level 15",
            f" local-user {user} service-type ssh",
            "#",
        ]
    L += [
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
        f" snmp-agent community read {community}",
        " snmp-agent sys-info version v2c",
        "#",
    ]
    if _ntp(p):
        # VRP8（CE）上 `ntp-service …` 是 Unrecognized，只有 `ntp unicast-server`
        L += [(f"ntp unicast-server {_ntp(p)}" if vrp8
               else f"ntp-service unicast-server {_ntp(p)}"), "#"]
    if p.dns_servers:
        L.append(f"dns server {p.dns_servers[0]}")
    if p.domain_name:
        # 华为的域名命令是 `dns domain`（VRP5/VRP8 实测都是这条），不是裸 `domain`
        L.append(f"dns domain {p.domain_name}")
    if p.extra_config:
        L += ["# ---- 自定义配置 ----", p.extra_config]
    # VRP8 有提交语义：`return` 会弹 [Y/N/C] 交互确认（脚本里会卡住/需要应答），
    # 用 `commit` 显式提交收尾；VRP5 用惯例的 `return`。
    L += ["commit", ""] if vrp8 else ["return", ""]
    return "\n".join(x for x in L if x != "")


# ============ Cisco IOS-XE ============
def cisco_config(dev, p) -> str:
    ip = dev.mgmt_ip or "10.0.0.1"
    user = p.admin_user or "admin"
    pw = _require_ztp_password(p)
    _check_profile_values(p)
    host = _require_clean(dev.hostname, "主机名")
    enable = p.enable_secret or pw
    vlanif = p.mgmt_interface.replace("Vlan-interface", "Vlan")
    L = [
        "! Cisco IOS-XE 开局配置 (OpsToolkit 生成)",
        f"! host={host} mgmt=" + _mgmt_label(dev, ip, p),
        "hostname " + host,
        "!",
        "no ip domain-lookup",
    ]
    if p.domain_name:
        L += [f"ip domain-name {p.domain_name}", "!"]
    L += _vlans_block_cisco(p)
    L += _ensure_vlan_lines_cisco(p)
    if dev.mgmt_via_dhcp:
        L += [
            "! 管理口用 DHCP 取址（没有指定管理 IP）",
            f"interface {vlanif}",
        ] + _phys_l3_switch_lines(p, "cisco", comment="!") + [
            " ip address dhcp",
            " no shutdown",
            "!",
        ]
    else:
        L += [
            f"interface {vlanif}",
        ] + _phys_l3_switch_lines(p, "cisco", comment="!") + [
            f" ip address {ip} {p.mgmt_netmask}",
            " no shutdown",
            "!",
        ]
    if p.access_ports:
        L.append("! 接入端口划入管理 VLAN")
        for port in p.access_ports:
            L += [f"interface {port}", " switchport mode access",
                  f" switchport access vlan {_ports_vlan_id(p)}", " no shutdown", "!"]
    if p.uplink_port:
        L += [f"interface {p.uplink_port}", " switchport mode access",
              f" switchport access vlan {_ports_vlan_id(p)}", " no shutdown", "!"]
    L += [
        # DHCP 取址时默认路由由 DHCP 给，不写静态默认路由（理由同 h3c 分支）
        f"ip route 0.0.0.0 0.0.0.0 {p.mgmt_gateway}" if not dev.mgmt_via_dhcp else "",
        "!",
        # 这里是 `service password-encryption`（**开**），不是 `no`：
        # 老代码写的是 `no service password-encryption`，那等于把 line password 之类的
        # 弱口令以**明文**存进 running-config —— 开局基线里主动降低设备安全等级，是缺陷。
        # （enable secret / username secret 本身是哈希存储，不受这一行影响。）
        "service password-encryption",
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
    # 华为 CE/NE（VRP8）与 VRP5 的命令差异是**真机实测**出来的（见 huawei_config 的说明），
    # 所以单列一个厂商值，而不是在同一份配置里赌哪条命令通用。
    "huawei-ce": lambda dev, p: huawei_config(dev, p, vrp8=True),
    "cisco": cisco_config,
}

# 厂商别名 → 规范值（运维在模板里怎么写都能落到正确的分支）
_VENDOR_ALIASES = {
    "h3c": "h3c", "comware": "h3c", "hp": "h3c", "hpe": "h3c",
    "huawei": "huawei", "vrp5": "huawei", "vrp": "huawei",
    "huawei-ce": "huawei-ce", "huawei-vrp8": "huawei-ce", "huawei_vrp8": "huawei-ce",
    "ce": "huawei-ce", "vrp8": "huawei-ce", "cloudengine": "huawei-ce",
    "cisco": "cisco", "ios": "cisco", "ios-xe": "cisco", "iosxe": "cisco",
}


def _norm_vendor(vendor) -> str:
    """将厂商统一为小写规范值（含别名），未知厂商回落 H3C。"""
    v = (vendor or "").strip().lower()
    return _VENDOR_ALIASES.get(v, "h3c")


def _ext(vendor) -> str:
    return {"h3c": "cfg", "huawei": "cfg", "huawei-ce": "cfg",
            "cisco": "cfg"}.get(_norm_vendor(vendor), "cfg")


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


def build_dnsmasq_lines(positions, vendor="h3c") -> list:
    """落位记录 → dnsmasq 行（落位登记 + 认领的投递侧）。

    有 MAC 的落位（已认领）：
        dhcp-host=<mac>,set:pos_<mac去冒号小写>
        dhcp-option=tag:pos_<mac去冒号小写>,option:bootfile-name,"ztp/<stem>.<ext>"
    stem 用 _file_stem 同一套规则，保证与 generate_all 写出的配置文件名一致。
    没有 MAC 的落位（待认领）：只输出一行注释占位 —— 设备上电后从 DHCP 租约里
    学到 MAC，在界面上认领后重新生成即可。

    （备注：若接入交换机插 option 82，也可以改用
    `--dhcp-circuitid=set:<tag>,<circuit-id>` 按「接入交换机端口」精确匹配选配置，
    原理与按 MAC 的 tag 相同。）
    """
    lines = []
    if not (positions or []):
        return lines
    lines.append("# ---- 落位登记（认领后按 MAC 下发各自配置） ----")
    for pos in positions:
        dev = _position_device(pos)
        if not dev.mac:
            pos_name = dev.position or dev.hostname or "未命名落位"
            lines.append(
                "# %s: 待认领（设备上电后从 DHCP 租约里学到 MAC，再在界面上认领）" % pos_name
            )
            continue
        tag = "pos_" + dev.mac.replace(":", "")
        fname = "ztp/%s.%s" % (_file_stem(dev), _ext(vendor))
        lines.append("dhcp-host=%s,set:%s" % (dev.mac, tag))
        lines.append('dhcp-option=tag:%s,option:bootfile-name,"%s"' % (tag, fname))
    return lines


def dnsmasq(p, devices, positions=None) -> str:
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
            "#    `eth0` 是「没填」的**哨兵值**：若你的宿主机上真实网卡就叫 eth0，",
            "#    请先把它改名（netplan/udev，例：ens19）再生成 —— 本工具区分不了这两种情况。",
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
    # ★ 外部审查 U6-F2：设备清单与落位登记**撞同一个文件名**时，落位规划覆盖手工登记
    #   （既定的兼容行为，见 generate_all 的说明）—— 但那个 MAC 的 bootfile 仍然指向这份
    #   被覆盖的文件，也就是"这台机器会拿到落位的规划 IP"。这是有意为之，但必须在生成物里
    #   看得见，否则运维核对 dnsmasq 时只会看到一条正常的映射。
    pos_stems = {_file_stem(_position_device(x)) for x in (positions or [])}
    for d in devices:
        stem = _file_stem(d)
        fname = f"ztp/{stem}.{_ext(vendor)}"
        mac = _norm_mac_text(d.mac)
        if d.mac and not mac:
            # 设备清单里的 MAC 写得不对：**不能静默放过**——原样拼进 dhcp-host=
            # 要么让 dnsmasq 配置非法（部署/重载失败），要么与租约里的归一 MAC 对不上，
            # 于是设备静默地只拿到 default.cfg 而不是自己的配置（外部审查 F5）。
            raise ValueError(
                "设备 %s 的 MAC %r 不是合法 MAC：请写成 aa:bb:cc:dd:ee:ff "
                "（也接受 AABB.CCDD.EEFF 与 AA-BB-CC-DD-EE-FF 写法）。"
                % (getattr(d, "hostname", "?"), d.mac)
            )
        if mac:
            tag = "tag:set_" + mac.replace(":", "")
            L.append(f"dhcp-host={mac},set:set_{mac.replace(':', '')}")
            L.append(f'dhcp-option={tag},option:bootfile-name,"{fname}"')
            if stem in pos_stems:
                L.append(
                    "# ⚠ 上面这台（MAC %s）与**落位登记**撞了同一个文件名 %s："
                    "该文件的内容由落位规划决定（落位覆盖手工登记），"
                    "也就是这台机器会拿到那份规划。若两台是不同设备，请改序列号/主机名。" % (mac, fname)
                )
        else:
            L.append(f'# {_require_clean(d.hostname, "主机名")}: 缺少 MAC, 使用 default.cfg')
    # 落位登记（认领后按 MAC 下发各自配置）：设备清单靠手抄 MAC，落位登记不需要 ——
    # MAC 是设备上电后从 DHCP 租约里自动学来的。
    L += build_dnsmasq_lines(positions, vendor=vendor)
    # ★ 外部审查 U6-F8：落位的**规划管理 IP** 落在本模板 DHCP 池内。
    #   设备首次上电先拿池里的临时地址，若配置里的静态地址同时也在池内，dnsmasq 可能把
    #   同一个地址再租给另一台设备 ⇒ 两台设备同 IP（这正是 §5.62 / test_netconfig_conflicts
    #   描述的事故形状，只是发生在 ZTP 这条线上）。
    #   只**告警不拦**：池常常是为首次引导临时开的，各现场规划口径不同；但必须让运维在
    #   生成物（以及预览）里看得见。
    if (p.deploy_mode or "standalone") == "standalone":
        for warn in _pool_overlap_warnings(p, positions):
            L.append("# ⚠ " + warn)
    L.append("")
    return "\n".join(L) + "\n"


def _pool_overlap_warnings(p, positions) -> list:
    """落位规划管理 IP 落在本模板 DHCP 池内的告警（U6-F8）。

    非法地址/池一律跳过（生成器不做二次校验，校验在 schema 层），绝不在这里抛异常 ——
    一条提示性检查不该把整份配置的生成打断。
    """
    import ipaddress

    def _net_of(start, end):
        try:
            a, b = ipaddress.IPv4Address(str(start).strip()), ipaddress.IPv4Address(str(end).strip())
        except Exception:  # noqa: BLE001
            return None
        lo, hi = sorted((int(a), int(b)))
        return lo, hi

    span = _net_of(p.dhcp_start, p.dhcp_end)
    if not span:
        return []
    lo, hi = span
    out = []
    for pos in positions or []:
        dev = _position_device(pos)
        ip = (getattr(dev, "mgmt_ip", "") or "").strip()
        if not ip:
            continue
        try:
            n = int(ipaddress.IPv4Address(ip))
        except Exception:  # noqa: BLE001
            continue
        if lo <= n <= hi:
            label = dev.position or dev.hostname or "未命名落位"
            out.append(
                "落位 %s 的规划管理 IP %s 落在本模板 DHCP 池 %s-%s 内："
                "首次引导时它可能已被租给别的设备 ⇒ 两台同 IP。"
                "请把规划地址挪到池外，或缩小池。" % (label, ip, p.dhcp_start, p.dhcp_end)
            )
    return out


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
    if v in ("huawei", "huawei-ce"):
        return _huawei_midfile(p, devices), "ztp_intermediate.txt"
    if v == "cisco":
        return _cisco_script(p, devices), "ztp_bootstrap.py"
    return _h3c_script(p, devices), "ztp_note.txt"


def _readme(p, devices, positions=None) -> str:
    v = _norm_vendor(p.vendor)
    iface = _iface_or_placeholder(p)
    positions = list(positions or [])
    n_claimed = sum(1 for x in positions if _position_device(x).mac)
    warn = ""
    if iface == "eth0":
        warn = ("!! 警告: 模板里没有填「DHCP网卡」, 生成的 dnsmasq 配置里是占位值 "
                "interface=eth0。\n"
                "   这份配置**不能**直接部署: dnsmasq 配了 bind-interfaces, 网卡不存在会\n"
                "   直接起不来(而它同时服务着 PXE); 若填错成骨干网卡, 则会在骨干网段上\n"
                "   开 DHCP 池、抢答企业 DHCP。请填好网卡后重新生成。\n"
                "   eth0/eth1/ens0 是「没填」的哨兵值: 若宿主机上真实网卡就叫 eth0,\n"
                "   请先改名(netplan/udev, 例 ens19) —— 本工具区分不了这两种情况。\n\n")
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
        "**落位登记 + 认领(推荐的登记方式)**:\n"
        "  设备到货时手里只有落位(机架/机柜/U位)和规划好的管理 IP/主机名, 还没有 MAC ——\n"
        "  而且不需要手抄。先在「落位登记」里按落位录入(支持批量粘贴 CSV:\n"
        "  落位,管理IP,主机名,序列号,MAC,备注), MAC 一栏留空;\n"
        "  设备第一次上电向 DHCP 请求地址时, dnsmasq 的租约文件里就记录了它的 MAC,\n"
        "  到「待认领设备」列表里点「认领到落位」, 把学到的 MAC 指到对应落位即可,\n"
        "  全程不需要到机器上抄 MAC。\n"
        "  认领后**重新生成并部署**, 该设备就会按 MAC 拿到自己落位规划的主机名/管理 IP;\n"
        "  未认领的落位不参与按 MAC 匹配, 对应设备上电后只会拿到 ztp/default.cfg。\n\n"
        f"本批次登记设备: {len(devices)} 台\n"
        f"落位: {len(positions)} 个（其中已认领 {n_claimed} 个）\n"
        "NTP 说明:\n"
        "  NTP 服务器**每个现场都不一样**, 所以这里不代填默认值:\n"
        "  模板里留空 = 设备配置里不出现 NTP 行(设备时间不会同步);\n"
        "  要下发就按**现场真实**的 NTP 地址填进模板, 再重新生成。\n\n"
        "厂商要点:\n"
        "  H3C   : auto-config, DHCP option 66(TFTP) + 67(文件名)\n"
        "  华为  : ZTP, DHCP option 66(TFTP) + 67(中间文件) + 中间文件描述下载项\n"
        "          华为 VRP5 与 **VRP8(CE/NE)** 命令不同: 模板厂商请选对应的那个\n"
        "          (`huawei` = VRP5, `huawei-ce` = VRP8/CloudEngine)。\n"
        "          VRP8 实测差异: 本地账号用 `password irreversible-cipher` + `user-group manage-ug`\n"
        "          (没有 `privilege level`)、用户名**至少 6 位**、NTP 是 `ntp unicast-server`、\n"
        "          结尾用 `commit`(不是 `return`, 后者会弹 [Y/N/C] 交互确认)。\n"
        "  思科  : IOS-XE ZTP, DHCP option 150(TFTP) + 67(脚本/配置)\n\n"
        "!! 真机实测坑（H3C S6850，EVE 里第一手验过）:\n"
        "   自动配置的 DHCP 应答**必须是完整的** —— 至少要有 option 51(租期)。\n"
        "   用只带 66/67 的\"精简\"应答时, 设备会**静默丢弃**这个 OFFER:\n"
        "   不 DECLINE、不报错, 只是每 8~20 秒重发 DISCOVER, 永远不去 TFTP 取文件,\n"
        "   从服务端看就是\"它收得到但不用\"。补齐成标准应答(51/58/59/1/28/3/6/54)后,\n"
        "   DISCOVER→OFFER→REQUEST→ACK→TFTP 一次走通(设备 sysname 也被改成配置里的值)。\n"
        "   ⇒ 本工具用 dnsmasq 投递, 它天然会带全这些选项, 无需额外配置;\n"
        "     但若现场改用交换机自带的 DHCP 服务器或第三方 DHCP, 请确认应答里有 option 51。\n\n"
        "!! 安全提醒:\n"
        "   生成的设备配置里含**管理员口令**, 而 ztp/ 下的文件是通过 TFTP/HTTP\n"
        "   **无认证**提供给设备的 ⇒ 开局网段上的任何主机都能读到它。\n"
        "   请: (1) 把开局网段与生产/办公网段物理或 VLAN 隔离;\n"
        "       (2) 按现场修改模板里的设备管理员口令, 不要用出厂默认值;\n"
        "       (3) 开局完成后及时删掉 TFTP/HTTP 上的配置(或撤掉 ZTP 投递配置)。\n"
        f"本批次登记设备: {len(devices)} 台\n"
        f"落位: {len(positions)} 个（其中已认领 {n_claimed} 个）\n"
    )


def _dup_macs(items) -> None:
    """同一 MAC 只能出现在一个地方（外部审查 U3-2nd-F5）。

    为什么必须挡：dnsmasq 侧每个"有 MAC 的条目"都会写出
        dhcp-host=<mac>,set:<tag>
        dhcp-option=tag:<tag>,option:bootfile-name,"ztp/<文件>"
    —— 同一个 MAC 出现两次就会有两组 tag 指向**不同的文件**，哪一份生效取决于 dnsmasq 的
    合并语义（本机未取证），设备可能拿到另一台的配置；而接口/界面都报成功。
    最典型的来源：设备清单里手抄了 MAC，之后又从租约里把同一台设备认领到某个落位。
    """
    seen = {}
    for d in items:
        mac = _norm_mac_text(getattr(d, "mac", "") or "")
        if not mac:
            continue
        label = getattr(d, "position", None) or getattr(d, "hostname", "") or "未命名"
        if mac in seen:
            raise ValueError(
                "ZTP 同一个 MAC %s 被登记了两次（%r 与 %r）：dnsmasq 会为它生成两组互相冲突的"
                "引导项（指向两份不同的配置），设备最终拿到哪一份不确定。"
                "请二选一：要么保留设备清单里的手抄记录，要么保留落位认领。"
                % (mac, seen[mac], label)
            )
        seen[mac] = label


def generate_all(p, devices=None, positions=None):
    """生成 ZTP 全部部署文件。

    positions：落位登记记录（dict 或 ORM 对象均可）。**向后兼容**：不传
    positions 时行为与旧版完全一致。落位设备排在 devices 之后生成 ——
    同一个文件名（_file_stem 相同）以后者为准，落位规划覆盖手工登记。
    """
    devices = devices or []
    positions = list(positions or [])
    pos_devices = [_position_device(x) for x in positions]
    # 文件名撞名检测（外部审查 F2，我复核确认机制）：
    # 配置文件名主干来自 serial 或 hostname，而这两者**没有唯一性约束**。
    # 撞名的后果不是"少生成一个文件"，而是 dict 覆盖 + dnsmasq 把**多个 MAC 指向同一份文件**
    # ⇒ 两台真机拿到同一份 sysname 与同一个静态管理 IP（网络冲突），而接口报成功。
    # 落位之间的撞名必须拒绝；"落位覆盖手工登记设备"是既定的兼容行为（有测试锁），保持不变。
    def _dup_stems(items, label):
        seen = {}
        for d in items:
            stem = _file_stem(d)
            if stem in seen:
                raise ValueError(
                    "ZTP 配置文件名撞名：%s 里的 %r 与 %r 都落在 ztp/%s.cfg。"
                    "撞名会让它们拿到同一份配置（主机名/管理 IP 都会重复）。"
                    "请给不同的设备填不同的序列号或主机名。"
                    % (label, seen[stem], getattr(d, "position", None) or d.hostname,
                       stem)
                )
            seen[stem] = getattr(d, "position", None) or d.hostname
        return seen

    _dup_stems(list(devices), "设备清单")
    _dup_stems(pos_devices, "落位登记")
    _dup_macs(list(devices) + pos_devices)
    files = {}
    vendor = _norm_vendor(p.vendor)
    gen = VENDOR_CONFIG.get(vendor, h3c_config)
    for d in list(devices) + pos_devices:
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
    files["dnsmasq.conf"] = dnsmasq(p, devices, positions=positions)
    inter, inter_name = intermediate(p, devices)
    files[f"ztp/{inter_name}"] = inter
    files["README.txt"] = _readme(p, devices, positions)
    return files
