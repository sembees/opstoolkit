# -*- coding: utf-8 -*-
"""告警外发集成层：巡检命中告警规则 → 查值班人 → 组文本 → 发飞书 → 落发送记录。

职责边界（本模块决定"要不要发、@ 谁、去重"；"怎么发出去"是上一单元冻结的
app/core/notify/feishu.py，接口不许改）：

@ 目标（值班平台是唯一事实源；ONCALL-PLATFORM-PLAN.md §3：平台不可用告警不能丢）：
    ① 值班平台查到当班人（current_oncall 返回 "ok"）→ 用平台结果；
    ② 非 ok（no_shift / unauthorized / team_not_found / bad_request / platform_error /
       empty / not_configured）→ **一律走 @all**（feishu_at_all=True 才真正 @所有人；
       连 @all 都没开则不 @，但消息照发 —— 告警事实不能丢）。
    配置里的固定 open_id 列表（feishu_at_open_ids）保留为**可选覆盖**（优先级低于平台、
    高于 @all），本次迭代**不启用** —— 代码段保留在 _config_at_open_ids()，暂不接线。

记录字段语义（models.Notification）：
    degraded             —— "未走平台路径"：平台 ok 为 False，其余（含未配置）为 True；
    oncall_lookup_failed —— 专指"平台查询失败/无人无排班"。所有非 ok 状态（含
                            not_configured）都置 True：从当次告警的效果看，"平台没配"
                            与"平台查失败"同样 @ 不到值班人，运维查表时两者都必须被标出
                            （漏标未配置 = 集成没接上却没人知道）。两者的**区分**写在
                            detail 里：未配置写"值班平台未配置（oncall_base_url 为空）"，
                            查询失败写具体原因（401/403、404、400、5xx/超时、no_shift…）。

404 = 团队 code 不存在 ⇒ **配置错误，不许静默**：除降级 @all 外，还要
    ① Notification.detail 写明团队配置错误（含团队名）；
    ② log.warning；
    ③ 发出去的消息末尾加一行「（值班团队配置错误：团队 {ONCALL_TEAM} 不存在，已 @all）」。

去重：event_key = "opstk:{metric_key}:{asset_host}"（asset_host 为空回退 asset_id，
两者都空用 "unknown"，保证不退化成空键）；notify_dedup_window 秒内已有**成功**发送记录
就不再发第二条（失败不算，允许重试），但要把首条成功记录的 merged_count **原子 +1**
（UPDATE 自增，不读-改-写）。下一次真正发送时若上一条同类记录 merged_count>0，
消息末尾加一行「（上一次同类告警之后又触发 N 次，已合并）」。

记录：每次调用（无论成败）写一条 models.Notification，用的是**自己的**
app.database.async_session 会话 —— 不依赖调用方（巡检）的会话。

★ 旁路保证：notify_alert 整体 try/except 包住，**绝不抛异常** ——
  通知成不成都不能拖垮巡检主流程。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import select, update

from app.config import settings
from app.core import models, oncall
from app.core.notify.feishu import build_alert_text, send_text, settings_at_open_ids
from app.core.timeutil import utcnow

# 为什么经模块属性访问 database.async_session，而不是 `from app.database import async_session`：
# 让测试能把会话工厂指到临时库（mock.patch.object(app.database, "async_session", …)）。
# 生产路径两者完全等价 —— async_session 是 app.database 的模块级单例。
from app import database

log = logging.getLogger(__name__)

# 非 ok 状态 → Notification.detail 里的原因说明。
# "平台未配置"与"平台查询失败"靠这里的文案在记录里可区分（两态都置
# oncall_lookup_failed=True，但 detail 一眼能看出是没配平台还是平台出了问题）。
_ONCALL_STATUS_DETAIL = {
    "not_configured": "值班平台未配置（oncall_base_url 为空），未查询值班人",
    "no_shift": "值班平台应答正常但当前时段无排班（no_shift），无当班人",
    "unauthorized": "值班平台查询失败：认证被拒（HTTP 401/403，token 无效或无权限）",
    "bad_request": "值班平台查询失败：请求参数被拒（HTTP 400，消费侧参数问题）",
    "platform_error": "值班平台查询失败：平台不可用（HTTP 5xx/超时/网络异常）",
    "empty": "值班平台查询失败：应答里没有可用的当班成员（缺 feishu_open_id）",
}

# 非 ok ⇒ @all 降级时，消息备注（extra_note）里的原因；都带"已降级"字样。
_ONCALL_STATUS_NOTE = {
    "not_configured": "值班平台未配置，未查询当班人，已降级",
    "no_shift": "值班平台当前时段无排班（no_shift），已降级",
    "unauthorized": "值班平台认证失败（401/403），已降级",
    "team_not_found": "值班平台无此团队（404），已降级",
    "bad_request": "值班平台拒绝请求参数（400），已降级",
    "platform_error": "值班平台不可用（5xx/超时），已降级",
    "empty": "值班平台无可 @ 的当班成员，已降级",
}


def _oncall_team() -> str:
    """本次查询用的团队名（与 oncall.current_oncall 的默认参数口径一致）。"""
    return (settings.oncall_team or "").strip()


def _config_at_open_ids() -> list[str]:
    """【保留但本次不启用】配置固定 open_id 覆盖。

    优先级介于平台与 @all 之间：平台 ok 用平台；平台失败时本可回退这份配置再 @all。
    本次迭代按验收要求"非 ok 一律 @all"，此路径不接 —— 函数与说明保留，
    后续要启用时在 _resolve_at_targets 的非 ok 分支接回一行即可。
    """
    return settings_at_open_ids()


def _event_key(asset_id, metric_key, asset_host="") -> str:
    """去重键：同指标 + 同设备在窗口内只发一条。

    键 = "opstk:{metric_key}:{asset_host}"；asset_host 为空回退 asset_id，
    两者都空用 "unknown" —— 保证不会退化成 "opstk:{metric_key}:" 这样的空尾键。
    """
    host = str(asset_host or "").strip()
    ident = host or str(asset_id or "").strip() or "unknown"
    return f"opstk:{metric_key}:{ident}"


async def _suppress_and_merge(event_key: str, window_seconds: int) -> bool:
    """去重窗口内已有**成功**发送记录 ⇒ 不再发第二条，把那条记录 merged_count 原子 +1。

    返回 True = 已合并（调用方直接返回，本次不发消息）；False = 窗口内没有成功记录。
    只认 ok=True 的记录 —— 失败的记录不占窗口（下条告警允许重试）。
    计数用 UPDATE ... SET merged_count = merged_count + 1 原地自增，
    **不读-改-写**（并发触发也不丢计数）。
    """
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
        if row is None:
            return False
        await db.execute(
            update(models.Notification)
            .where(models.Notification.id == row.id)
            .values(merged_count=models.Notification.merged_count + 1)
        )
        await db.commit()
    log.info("告警去重：窗口内已发过 event=%s，首条成功记录 merged_count+1", event_key)
    return True


async def _previous_merged_count(event_key: str) -> int:
    """上一条同类（同 event_key）**成功**记录的 merged_count；没有则 0。

    能走到"真正发送"说明窗口内没有成功记录，所以查到的必是**窗口外**的上一条 ——
    它的 merged_count>0 就代表"上次这条告警之后又默默触发过 N 次"，新消息要带出来。
    """
    async with database.async_session() as db:
        row = (await db.execute(
            select(models.Notification.merged_count)
            .where(models.Notification.event_key == event_key)
            .where(models.Notification.ok.is_(True))
            .order_by(models.Notification.created_at.desc())
            .limit(1)
        )).first()
    try:
        return int(row[0]) if row and row[0] else 0
    except (TypeError, ValueError):
        return 0


def _fmt_occurred(occurred_at) -> str:
    """occurred_at 统一成展示字符串；缺省用当前 UTC 时间（naive，与全库口径一致）。"""
    if occurred_at is None:
        return utcnow().strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(occurred_at, datetime):
        return occurred_at.strftime("%Y-%m-%d %H:%M:%S")
    return str(occurred_at).strip() or utcnow().strftime("%Y-%m-%d %H:%M:%S")


async def _resolve_at_targets() -> tuple[list[str], bool, bool, bool, str]:
    """取 @ 目标。返回 (at_targets, at_all, degraded, oncall_lookup_failed, oncall_status)。

    ① 平台 "ok" 且有成员 → 平台结果（不降级、oncall_lookup_failed=False）；
    ② 非 ok（任何状态）→ **一律 @all**：feishu_at_all=True 则 @所有人，否则不 @。
       两档都算降级：degraded=True、oncall_lookup_failed=True。
       配置固定 open_id 的可选覆盖（_config_at_open_ids）保留但本次不启用。
    """
    targets, status = await oncall.current_oncall()
    if status == "ok" and targets:
        # ① 平台查到当班人 —— 不降级
        return list(targets), False, False, False, status
    # ② 非 ok ⇒ 一律 @all 降级（配置 open_id 覆盖保留未启用，见 _config_at_open_ids）
    if settings.feishu_at_all:
        # ③ @所有人
        return [], True, True, True, status
    # ④ @all 也没开：不发 @，但消息仍发
    return [], False, True, True, status


async def notify_alert(*, rule, metric_key, value, asset_id, asset_name, asset_host,
                       severity: str = "critical", occurred_at=None) -> None:
    """把一条命中告警外发到飞书并落发送记录。★ 绝不抛异常、不返回值。

    notify_enabled=False ⇒ 立即返回：零网络请求（含值班平台查询）、零 DB 写入。
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
                      _event_key(asset_id, metric_key, asset_host))


async def _notify_alert_inner(*, rule, metric_key, value, asset_id, asset_name, asset_host,
                              severity, occurred_at) -> None:
    # 0) 总开关：未启用直接返回（"零网络请求"由这里保证）。
    if not settings.notify_enabled:
        return

    event_key = _event_key(asset_id, metric_key, asset_host)

    # 1) 去重：窗口内已有成功发送 ⇒ 不再发第二条，但 merged_count 原子 +1（合并计数不丢）。
    window = int(settings.notify_dedup_window or 0)
    if window > 0 and await _suppress_and_merge(event_key, window):
        return

    # 2) @ 目标（平台 ok → 平台结果；非 ok → 一律 @all 降级，见模块 docstring）
    at_targets, at_all, degraded, lookup_failed, oncall_status = await _resolve_at_targets()

    # 404 = 团队配置错误：不许静默 —— 先大声记日志（运维要改 ONCALL_TEAM 配置）。
    team = _oncall_team()
    if oncall_status == "team_not_found":
        log.warning(
            "值班团队配置错误：值班平台返回 404，团队 %r 不存在 —— 请检查 ONCALL_TEAM 配置；"
            "本次告警已降级 @all 照常外发（event=%s）", team or "（未配置）", event_key)

    # 3) 组文本 → 发送（send_text 失败不抛异常，返回 FeishuResult）
    #    窗口外重发时，上一条同类记录若合并过 N 次触发，消息末尾要带"已合并 N 次"。
    prev_merged = await _previous_merged_count(event_key)
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
        extra_note=_ONCALL_STATUS_NOTE.get(oncall_status, "") if degraded else "",
    )
    # 消息末尾附加行（在 build_alert_text 的正文之后，保证是"末尾一行"）：
    tail: list[str] = []
    if prev_merged > 0:
        tail.append(f"（上一次同类告警之后又触发 {prev_merged} 次，已合并）")
    if oncall_status == "team_not_found":
        tail.append(f"（值班团队配置错误：团队 {team or '（未配置）'} 不存在，已 @all）")
    if tail:
        text = "\n".join([text] + tail)
    result = await send_text(text, at_open_ids=at_targets, at_all=at_all)

    # 4) 落发送记录（自己的会话；发送失败也记录 —— 审计需要知道"试过但没发成"）
    detail = str(getattr(result, "detail", "") or "")
    if lookup_failed:
        # 非 ok 状态在 detail 里写明原因；404 必须点明"团队配置错误 + 团队名"。
        if oncall_status == "team_not_found":
            note = (f"值班团队配置错误：团队 {team or '（未配置）'} 在值班平台不存在"
                    f"（HTTP 404），已降级 @all")
        else:
            note = _ONCALL_STATUS_DETAIL.get(
                oncall_status, f"值班平台查询失败（{oncall_status}）")
        detail = f"{detail}；{note}" if detail else note
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
        detail=detail,
        attempts=int(getattr(result, "attempts", 0) or 0),
        at_targets=json.dumps(at_targets, ensure_ascii=False),
        degraded=degraded,
        oncall_lookup_failed=lookup_failed,
        merged_count=0,   # 新记录从 0 起；窗口内的重复触发加在首条成功记录上
        message_id=None,  # 发送层暂不回传 message_id，字段留给后续链路
    )
    async with database.async_session() as db:
        db.add(record)
        await db.commit()

    if not record.ok:
        log.warning("告警通知发送失败 event=%s degraded=%s detail=%s",
                    event_key, degraded, record.detail)
