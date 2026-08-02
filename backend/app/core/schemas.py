"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class ORMBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- 认证 ----------
class LoginIn(BaseModel):
    username: str
    password: str


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


class PxeInstallOut(ORMBase):
    id: str
    profile_id: str
    hostname: str
    mac: str
    ip: Optional[str] = None
    status: str
    created_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


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


class NetVlanIn(BaseModel):
    parent: str                     # 父接口
    vlan_id: int
    mode: str = "static"
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None


class NetBridgeIn(BaseModel):
    name: str                       # br0
    interfaces: list[str]
    ip: Optional[str] = None
    netmask: Optional[str] = None
    cidr: Optional[int] = None
    gateway: Optional[str] = None


class NetConfigRequest(BaseModel):
    os: str                          # ubuntu / rhel
    hostname: Optional[str] = None
    interfaces: list[NetInterfaceIn] = []
    bonds: list[NetBondIn] = []
    vlans: list[NetVlanIn] = []
    bridges: list[NetBridgeIn] = []
    format: str = "nmcli"           # nmcli / netplan(仅ubuntu)
    netplan_renderer: str = "networkd"  # networkd(服务器推荐) / NetworkManager(无线/动态)


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

