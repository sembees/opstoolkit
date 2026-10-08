"""ORM 模型：用户、资产、凭据、巡检任务、巡检结果。"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
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

    # ondelete="SET NULL" + passive_deletes（外部审查 U7-F12）：列上原来没有 ondelete，
    # 而 SQLite 默认**不**开启外键校验，于是"凭据被删、资产还指着它"会留下悬挂引用，
    # 之后巡检时取不到解密材料，报出来却是一句莫名其妙的认证失败。
    # 接口层本来就会先校验引用并 409，这里是数据库层的兜底（新库直接生效；
    # 已有库的列定义改不了，故另外在巡检入口把这种情况说清楚）。
    credential_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True)
    credential: Mapped[Optional[Credential]] = relationship(
        back_populates="assets", passive_deletes=True)

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

    # 装完能 SSH（本单元）：root 直登开关（默认否）与 sudo 免密开关（默认是）。
    # 存量库由 app/database._ensure_additive_columns 幂等补列（create_all 不给已存在
    # 的表加列），列定义与 _ADDITIVE_COLUMNS 里的 DDL 对齐：NOT NULL + 默认值。
    allow_root: Mapped[bool] = mapped_column(default=False)
    sudo_nopasswd: Mapped[bool] = mapped_column(default=True)

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
    __table_args__ = (
        # 外部审查 U7-F9：同一模板下同一 MAC 只能有一条装机记录（空 MAC = 还没登记，
        # 用 partial 唯一索引排除，与 ZtpPosition 同口径）。
        # 为什么必须唯一：生成器按 MAC 展开文件 key（`boot/<mac_tag>.ipxe`、user-data/<tag>/）
        # 与 dnsmasq 的 `dhcp-host=` 行 —— 同一个 MAC 两条记录时后一条**静默覆盖**前一条，
        # 运维看到两条记录却只有一台的配置生效（最坑的是"谁生效取决于遍历顺序"）。
        Index("uq_pxe_install_profile_mac", "profile_id", "mac", unique=True,
              sqlite_where=text("mac <> ''")),
    )

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
    # NTP 各现场不同：留空串表示"不下发 NTP"，不再给一个谁都连不上的假默认值
    ntp_server: Mapped[str] = mapped_column(String(64), default="")
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
    __table_args__ = (
        # 同 PxeInstall（外部审查 U7-F9）：同一模板内非空 MAC 唯一。
        # 生成器按 MAC 展开 `dhcp-host=`/`option:bootfile-name`（见 ct/ztp/generator.dnsmasq），
        # 同一 MAC 两条记录会写出两条互相覆盖的条目，设备拿到哪份取决于遍历顺序。
        # 空串 = 还没登记 MAC，必须排除在唯一性之外。
        Index("uq_ztp_dev_template_mac", "template_id", "mac", unique=True,
              sqlite_where=text("mac <> ''")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(ForeignKey("ztp_templates.id", ondelete="CASCADE"), index=True)
    hostname: Mapped[str] = mapped_column(String(128), default="")
    mac: Mapped[str] = mapped_column(String(32), index=True)
    serial: Mapped[str] = mapped_column(String(128), default="")
    mgmt_ip: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)  # ZTP 完成后回填的管理 IP
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ZtpPosition(Base):
    """ZTP 落位登记表（落位 + 认领）。

    真实流程：设备到货时运维手里只有「落位（机架/机柜/U位）+ 规划好的管理 IP +
    规划主机名」，**没有 MAC**（设备还没上电）。这里先按落位登记；设备第一次上电
    向 DHCP 请求地址时，dnsmasq 的租约文件里就记录了它的 MAC —— 系统从租约里
    自动学到 MAC，运维只需在界面上做一步「认领」（把学到的设备指到某个落位），
    之后即可按 MAC 下发该落位规划好的配置。全程不需要手抄 MAC。

    mac 为空 / claimed_at 为 NULL = 待认领；认领后 source="claim"，
    手工直接填 MAC 时 source="manual"。
    """
    __tablename__ = "ztp_positions"
    __table_args__ = (
        UniqueConstraint("template_id", "position", name="uq_ztp_pos_position"),
        UniqueConstraint("template_id", "mgmt_ip", name="uq_ztp_pos_mgmt_ip"),
        # 同一模板内非空 MAC 唯一：partial 唯一索引（SQLite 支持 sqlite_where；
        # 其它后端会忽略该子句 —— 本项目只用 SQLite）。
        # 空串表示"还没认领"，必须排除在外，否则所有待认领落位互相冲突。
        Index("uq_ztp_pos_mac", "template_id", "mac", unique=True,
              sqlite_where=text("mac <> ''")),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    template_id: Mapped[str] = mapped_column(ForeignKey("ztp_templates.id", ondelete="CASCADE"), index=True)
    position: Mapped[str] = mapped_column(String(128))                    # 落位编码，例如 A01-03-U12
    hostname: Mapped[str] = mapped_column(String(128), default="")        # 规划主机名
    mgmt_ip: Mapped[str] = mapped_column(String(64))                      # 规划管理 IP（核心字段）
    serial: Mapped[str] = mapped_column(String(128), default="")          # 序列号（可选，来自入库信息）
    mac: Mapped[str] = mapped_column(String(32), default="", index=True)  # 认领后回填；空 = 还没认领
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    source: Mapped[str] = mapped_column(String(16), default="manual")     # manual / claim
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class AppMeta(Base):
    """极小的应用级键值表（安装引导向导等"系统状态"的落点）。

    为什么不用文件/环境变量：向导的"是否已完成 / 管理员是否改过初始口令"必须**随库走**
    （换容器、换机器时跟着数据目录迁移），而且要在接口里原子读写。键唯一 + updated_at
    便于排查"什么时候完成/改的"。只允许应用自己写，没有对外批量写接口。
    """
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Notification(Base):
    """告警通知发送记录（一次外发尝试 = 一行）。

    谁写入：app/core/notify/alerts.notify_alert（巡检命中告警 → 发飞书 → 落库）。
    用途：
      ① 去重 —— 同 event_key 在 settings.notify_dedup_window 秒内已有**成功**记录
         （ok=True）就不再发第二条（失败的记录不算，允许下条告警重试）；窗口内的
         重复触发把首条成功记录的 merged_count 原地 +1（不丢计数，下条消息带
         "上一次同类告警之后又触发 N 次，已合并"）；
      ② 审计 —— 发给谁 / @ 了谁 / 是否降级 / 平台查询是否失败 / 失败原因，运维直接查表就能回答
         "为什么没 @ 到人"。

    注意与 ONCALL-PLATFORM-PLAN.md §6 里"平台侧"的 Notification 是两张表：
    本表在 OpsToolkit 自己的库里，记录"发送侧"的事实（模式 A：OpsToolkit 自己发）。
    """
    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    # 去重键："opstk:{metric_key}:{asset_host}"（asset_host 为空回退 asset_id，见 alerts._event_key）
    event_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    asset_id: Mapped[str] = mapped_column(String(32), default="", index=True)
    asset_name: Mapped[str] = mapped_column(String(128), default="")
    asset_host: Mapped[str] = mapped_column(String(255), default="")
    metric_key: Mapped[str] = mapped_column(String(64), default="")
    # value 存文本（str(value)）：巡检指标值可能是 int/float，审计场景字符串最稳；
    # 不用数值列，避免个别指标给非数值时写库失败把通知旁路炸掉。
    value: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    operator: Mapped[str] = mapped_column(String(8), default="")
    threshold: Mapped[Optional[float]] = mapped_column(nullable=True)
    rule_id: Mapped[str] = mapped_column(String(32), default="")
    rule_name: Mapped[str] = mapped_column(String(128), default="")
    severity: Mapped[str] = mapped_column(String(16), default="critical")
    channel: Mapped[str] = mapped_column(String(16), default="feishu")
    ok: Mapped[bool] = mapped_column(default=False)
    detail: Mapped[str] = mapped_column(Text, default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # JSON 文本：当时实际 @ 的目标列表（"ou_xxx|显示名"），没有 @ 时为 "[]"。
    at_targets: Mapped[Optional[str]] = mapped_column(Text, default="[]")
    # True = 走了降级路径（值班平台未配置/超时/报错/为空，回退到配置 @ / @all / 不 @）
    degraded: Mapped[bool] = mapped_column(default=False)
    # True = 值班平台查询失败/无人无排班（no_shift / 401/403 / 404 / 400 / 5xx/超时 /
    # 未配置）：当次告警没有 @ 到平台给的当班人。与 degraded 的分工：
    #   degraded        —— "未走平台路径"（@ 路径降级了这个事实）；
    #   oncall_lookup_failed —— 专指"值班平台这一环没成"（平台 ok 时恒为 False，
    #                        即便未来重新启用配置 open_id 兜底，两字段也会各表其义）。
    # 存量库由 app/database._ensure_additive_columns 幂等补列（create_all 不给已存在的表加列）。
    oncall_lookup_failed: Mapped[bool] = mapped_column(default=False)
    # 去重合并计数：同 event_key 在去重窗口内的第 2..N 次触发不再发消息，而是把首条
    # 成功记录的 merged_count 原子 +1（UPDATE 自增，不读-改-写）。新记录恒为 0。
    merged_count: Mapped[int] = mapped_column(Integer, default=0)
    # 预留：发送层 FeishuResult 目前不回传飞书 message_id，恒为 NULL
    message_id: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

