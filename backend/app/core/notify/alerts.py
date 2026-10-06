# -*- coding: utf-8 -*-
"""告警外发集成层：巡检命中告警规则 → 查值班人 → 组文本 → 发飞书 → 落发送记录。

职责边界（本模块决定"要不要发、@ 谁、去重"；"怎么发出去"是上一单元冻结的
app/core/notify/feishu.py，接口不许改）：

@ 目标四级降级（ONCALL-PLATFORM-PLAN.md §3：平台不可用告警不能丢）：
    ① 值班平台查到当班人（app/core/oncall.current_oncall 成功且非空）→ 用平台结果；
    ② 否则 feishu_at_open_ids 配置兜底；
    ③ 否则 feishu_at_all=True → @所有人；
    ④ 都没有 → 不 @，但消息照发。
    凡走了 ②③④（含平台未配置/超时/报错/为空）⇒ degraded=True 并写进发送记录。

去重：event_key = "opstk:{asset_id}:{metric_key}"；notify_dedup_window 秒内已有
**成功**发送记录就不再发第二条（失败不算，允许重试）。窗口 settings.notify_dedup_window
（默认 300 秒）—— 发送层只定义不使用，在本模块落地。

记录：每次调用（无论成败）写一条 models.Notification，用的是**自己的**
app.database.async_session 会话 —— 不依赖调用方（巡检）的会话。

★ 旁路保证：notify_alert 整体 try/except 包住，**绝不抛异常** ——
  通知成不成都不能拖垮巡检主流程。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from app.config import settings
from app.core import models, oncall
from app.core.notify.feishu import build_alert_text, send_text, settings_at_open_ids
from app.core.timeutil import utcnow

# 为什么经模块属性访问 database.async_session，而不是 `from app.database import async_session`：
# 让测试能把会话工厂指到临时库（mock.patch.object(app.database, "async_session", …)）。
# 生产路径两者完全等价 —— async_session 是 app.database 的模块级单例。
from app import database

log = logging.getLogger(__name__)


def _event_key(asset_id, metric_key) -> str:
    """去重键：同资产 + 同指标在窗口内只发一条。"""
    return f"opstk:{asset_id}:{metric_key}"


async def _has_recent_success(event_key: str, window_seconds: int) -> bool:
    """去重窗口内是否已有**成功**的发送记录（失败的记录不算，允许重试）。"""
    since = utcnow() - timedelta(seconds=max(0, int(window_seconds)))
    async with database.async_session() as db:
        row = (await db.execute(
            select(models.Notification.id)
            .where(models.Notification.event_key == event_key)
            .where(models.Notification.ok.is_(True))
            .where(models.Notification.created_at >= since)
            .order_by(models.Notification.created_at.desc())
            .limit(1)
        )).first()
    return row is not None


def _fmt_occurred(occurred_at) -> str:
    """occurred_at 统一成展示字符串；缺省用当前 UTC 时间（naive，与全库口径一致）。"""
    if occurred_at is None:
        return utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(occurred_at, datetime):
        return occurred_at.strftime("%Y-%m-%d %H:%M:%S")
    return str(occurred_at).strip() or utcnow().strftime("%Y-%m-%d %H:%M:%S")


async def _resolve_at_targets() -> tuple[list[str], bool, bool, str]:
    """取 @ 目标（四级降级）。返回 (at_targets, at_all, degraded, oncall_status)。"""
    targets, status = await oncall.current_oncall()
    if targets:
        # ① 平台查到当班人 —— 不降级
        return list(targets), False, False, status
    # 平台未配置 / 超时 / 报错 / 为空 ⇒ 从这里起全部按降级记录
    cfg_ids = settings_at_open_ids()
    if cfg_ids:
        # ② 配置兜底
        return cfg_ids, False, True, status
    if settings.feishu_at_all:
        # ③ @所有人
        return [], True, True, status
    # ④ 都没有：不发 @，但消息仍发
    return [], False, True, status


async def notify_alert(*, rule, metric_key, value, asset_id, asset_name, asset_host,
                       severity: str = "critical", occurred_at=None) -> None:
    """把一条命中告警外发到飞书并落发送记录。★ 绝不抛异常、不返回值。

    notify_enabled=False ⇒ 立即返回：零网络请求、零 DB 写入。
    （巡检接入点在 create_task 前还有一层同样的判断 —— 这里是最后一道闸，
    保证任何调用方都发不出去。）
    """
    try:
        await _notify_alert_inner(
            rule=rule, metric_key=metric_key, value=value, asset_id=asset_id,
            asset_name=asset_name, asset_host=asset_host,
            severity=severity, occurred_at=occurred_at,
        )
    except Exception:  # noqa: BLE001 —— 通知是旁路，任何异常只记日志，绝不上抛
        log.exception("告警通知发送失败（旁路，不影响巡检主流程）event=%s",
                      _event_key(asset_id, metric_key))


async def _notify_alert_inner(*, rule, metric_key, value, asset_id, asset_name, asset_host,
                              severity, occurred_at) -> None:
    # 0) 总开关：未启用直接返回（"零网络请求"由这里保证）。
    if not settings.notify_enabled:
        return

    event_key = _event_key(asset_id, metric_key)

    # 1) 去重：窗口内已有成功发送 ⇒ 直接返回，不发第二条。
    window = int(settings.notify_dedup_window or 0)
    if window > 0 and await _has_recent_success(event_key, window):
        return

    # 2) @ 目标（四级降级，见模块 docstring）
    at_targets, at_all, degraded, oncall_status = await _resolve_at_targets()

    # 3) 组文本 → 发送（send_text 失败不抛异常，返回 FeishuResult）
    rule_name = str(getattr(rule, "name", "") or "")
    operator = str(getattr(rule, "operator", "") or "")
    threshold = getattr(rule, "threshold", None)
    text = build_alert_text(
        title=rule_name or "巡检告警",
        severity=severity,
        asset_name=str(asset_name or ""),
        asset_host=str(asset_host or ""),
        metrics=[{"name": metric_key, "value": value, "unit": "",
                  "threshold": threshold, "op": operator}],
        rule_name=rule_name,
        occurred_at=_fmt_occurred(occurred_at),
        at_open_ids=at_targets,
        at_all=at_all,
        extra_note=("值班平台无可用当班人（%s），已降级" % oncall_status)
                   if degraded else "",
    )
    result = await send_text(text, at_open_ids=at_targets, at_all=at_all)

    # 4) 落发送记录（自己的会话；发送失败也记录 —— 审计需要知道"试过但没发成"）
    record = models.Notification(
        event_key=event_key,
        asset_id=str(asset_id or ""),
        asset_name=str(asset_name or ""),
        asset_host=str(asset_host or ""),
        metric_key=str(metric_key or ""),
        value=None if value is None else str(value),
        operator=operator,
        threshold=threshold,
        rule_id=str(getattr(rule, "id", "") or ""),
        rule_name=rule_name,
        severity=str(severity or ""),
        channel="feishu",
        ok=bool(getattr(result, "ok", False)),
        detail=str(getattr(result, "detail", "") or ""),
        attempts=int(getattr(result, "attempts", 0) or 0),
        at_targets=json.dumps(at_targets, ensure_ascii=False),
        degraded=degraded,
        message_id=None,  # 发送层暂不回传 message_id，字段留给后续链路
    )
    async with database.async_session() as db:
        db.add(record)
        await db.commit()

    if not record.ok:
        log.warning("告警通知发送失败 event=%s degraded=%s detail=%s",
                    event_key, degraded, record.detail)
