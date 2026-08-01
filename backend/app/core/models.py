"""ORM 模型：用户、资产、凭据、巡检任务、巡检结果。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.timeutil import utcnow
from app.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    """系统用户，支持 admin 和 operator 角色。"""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="admin")  # admin / operator
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Credential(Base):
    """设备登录凭据（加密存储）。"""
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)      # 凭据显示名称
    username: Mapped[str] = mapped_column(String(128))                 # 登录用户名
    encrypted_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # 加密存储的密码
    ssh_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)      # 加密的 SSH 私钥
    enable_secret_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)   # 加密的 enable 密码
    device_type: Mapped[str] = mapped_column(String(64), default="")
    port: Mapped[int] = mapped_column(Integer, default=22)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    assets: Mapped[list["Asset"]] = relationship(back_populates="credential")


class Asset(Base):
    """资产。

    category: ct(网络/安全设备) / it(服务器)。
    vendor: h3c / huawei / cisco / dell / hp / generic
    """
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str] = mapped_column(String(8), index=True)    # ct / it
    vendor: Mapped[str] = mapped_column(String(32), default="")
    device_role: Mapped[str] = mapped_column(String(32), default="")
    host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer, default=22)
    device_type: Mapped[str] = mapped_column(String(64), default="")
    serial: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    mac: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    location: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    tags: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)
    remark: Mapped[str] = mapped_column(Text, default="")

    credential_id: Mapped[Optional[str]] = mapped_column(ForeignKey("credentials.id"), nullable=True)
    credential: Mapped[Optional[Credential]] = relationship(back_populates="assets")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class PxeProfile(Base):
    """PXE 装机模板。os_type: ubuntu/rhel。"""
    __tablename__ = "pxe_profiles"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    os_type: Mapped[str] = mapped_column(String(16))          # ubuntu / rhel
    os_version: Mapped[str] = mapped_column(String(32), default="")  # 22.04 / 9.3
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    locale: Mapped[str] = mapped_column(String(64), default="en_US.UTF-8")
    keyboard: Mapped[str] = mapped_column(String(32), default="us")

    admin_user: Mapped[str] = mapped_column(String(64), default="ops")
    admin_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ssh_keys: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    root_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    disk_scheme: Mapped[str] = mapped_column(String(16), default="lvm")  # lvm / direct
    disk_config: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    net_mode: Mapped[str] = mapped_column(String(16), default="dhcp")     # dhcp / static
    net_config: Mapped[Optional[dict]] = mapped_column(JSON, default=dict)

    mirror: Mapped[str] = mapped_column(String(255), default="")
    extra_packages: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    post_script: Mapped[str] = mapped_column(Text, default="")
    remark: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    installs: Mapped[list["PxeInstall"]] = relationship(back_populates="profile", cascade="all, delete-orphan")


class PxeInstall(Base):
    """单台主机装机记录，通过 MAC 关联。"""
    __tablename__ = "pxe_installs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    profile_id: Mapped[str] = mapped_column(ForeignKey("pxe_profiles.id", ondelete="CASCADE"), index=True)
    hostname: Mapped[str] = mapped_column(String(128), default="")
    mac: Mapped[str] = mapped_column(String(32), index=True)
    ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/booting/installing/done/failed
    log: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    profile: Mapped[PxeProfile] = relationship(back_populates="installs")


class InspectionTemplate(Base):
    """巡检模板。

    is_system=True 时不可编辑/删除，但可克隆后自定义。
    items 每项包含: {key, label, command, textfsm, unit}。
    """
    __tablename__ = "inspection_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    vendor: Mapped[str] = mapped_column(String(32), default="")    # h3c / huawei / cisco / generic
    is_system: Mapped[bool] = mapped_column(default=False)            # True=系统默认只读
    items: Mapped[list] = mapped_column(JSON, default=list)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class InspectionTask(Base):
    """一次巡检任务（可对多台设备）。"""
    __tablename__ = "inspection_tasks"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="default")
    template: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    commands: Mapped[Optional[list]] = mapped_column(JSON, nullable=True)
    asset_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    created_by: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # 巡检过程中的实时输出日志，用于任务回放
    output_log: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    results: Mapped[list["InspectionResult"]] = relationship(
        back_populates="task", cascade="all, delete-orphan"
    )


class InspectionResult(Base):
    """单台设备在某个任务下的巡检产出。"""
    __tablename__ = "inspection_results"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("inspection_tasks.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    asset_name: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    error: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    raw: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    task: Mapped[InspectionTask] = relationship(back_populates="results")


class AlertRule(Base):
    """告警规则。metric_key: cpu/memory/temperature 等巡检指标。"""
    __tablename__ = "alert_rules"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128))
    metric_key: Mapped[str] = mapped_column(String(64), index=True)  # 对应巡检指标 key
    operator: Mapped[str] = mapped_column(String(8), default="gt")   # gt / lt / gte / lte
    threshold: Mapped[float] = mapped_column(default=0.0)             # 告警阈值
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ZtpTemplate(Base):
    """ZTP ?????vendor: h3c / huawei / cisco?"""
    __tablename__ = "ztp_templates"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    vendor: Mapped[str] = mapped_column(String(16))                     # h3c / huawei / cisco
    mgmt_vlan: Mapped[int] = mapped_column(Integer, default=10)
    mgmt_interface: Mapped[str] = mapped_column(String(64), default="Vlan-interface10")
    mgmt_netmask: Mapped[str] = mapped_column(String(32), default="255.255.255.0")
    mgmt_gateway: Mapped[str] = mapped_column(String(64), default="10.0.0.254")
    dns_servers: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    ntp_server: Mapped[str] = mapped_column(String(64), default="10.0.0.254")
    snmp_community: Mapped[str] = mapped_column(String(64), default="public")
    domain_name: Mapped[str] = mapped_column(String(128), default="")
    vlans: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    admin_user: Mapped[str] = mapped_column(String(64), default="admin")
    admin_password_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enable_secret_enc: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ssh_keys: Mapped[Optional[list]] = mapped_column(JSON, default=list)

    uplink_port: Mapped[str] = mapped_column(String(128), default="")
    access_ports: Mapped[Optional[list]] = mapped_column(JSON, default=list)
    extra_config: Mapped[str] = mapped_column(Text, default="")

    server_ip: Mapped[str] = mapped_column(String(64), default="10.0.0.250")
    tftp_root: Mapped[str] = mapped_column(String(255), default="/srv/tftp")
    http_root: Mapped[str] = mapped_column(String(255), default="http://10.0.0.250:8000/ztp")
    deploy_mode: Mapped[str] = mapped_column(String(16), default="standalone")  # standalone / proxy / relay，与 PXE 一致
    dhcp_iface: Mapped[str] = mapped_column(String(32), default="eth0")
    dhcp_start: Mapped[str] = mapped_column(String(64), default="10.0.0.100")
    dhcp_end: Mapped[str] = mapped_column(String(64), default="10.0.0.200")

    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ZtpDevice(Base):
    """ZTP ??????? MAC/??????????"""
    __tablename__ = "ztp_devices"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(ForeignKey("ztp_templates.id", ondelete="CASCADE"), index=True)
    hostname: Mapped[str] = mapped_column(String(128), default="")
    mac: Mapped[str] = mapped_column(String(32), index=True)
    serial: Mapped[str] = mapped_column(String(128), default="")
    mgmt_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # ZTP 完成后回填的管理 IP
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

