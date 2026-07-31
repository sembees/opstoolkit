"""ORM 模型：用户、资产、凭据、巡检任务、巡检结果。"""
import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def _uuid() -> str:
    return uuid.uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="admin")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Credential(Base):
    """设备登录凭据（加密存储）。"""
    __tablename__ = "credentials"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    username: Mapped[str] = mapped_column(String(128))
    encrypted_password: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ssh_key_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    enable_secret_encrypted: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    device_type: Mapped[str] = mapped_column(String(64), default="")
    port: Mapped[int] = mapped_column(Integer, default=22)
    remark: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    assets: Mapped[list["Asset"]] = relationship(back_populates="credential")


class Asset(Base):
    """资产。category: ct(网络/安全设备) / it(服务器)。"""
    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(128), index=True)
    category: Mapped[str] = mapped_column(String(8), index=True)
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

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    task: Mapped[InspectionTask] = relationship(back_populates="results")
