"""时间工具：统一返回无时区的 UTC 时间，与 SQLite naive datetime 保持一致。"""
from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)
