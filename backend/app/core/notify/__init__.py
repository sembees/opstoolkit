"""通知发送模块（目前只有飞书，见 feishu.py）。

对外接口（告警集成单元按此接入，签名不要改）：
    from app.core.notify import FeishuResult, feishu_configured, build_alert_text, send_text

职责边界：本包只负责"把一条文本消息发出去"；要不要发、何时发、去重窗口
（settings.notify_dedup_window）由告警集成单元决定。
"""
from app.core.notify.feishu import FeishuResult, build_alert_text, feishu_configured, send_text

__all__ = ["FeishuResult", "build_alert_text", "feishu_configured", "send_text"]
