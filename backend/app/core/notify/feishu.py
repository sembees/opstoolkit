"""飞书通知发送（自建应用：tenant_access_token + im/v1/messages，不是自定义机器人 webhook）。

职责边界：只做"把一条文本发出去"这一层。
  · 不决定"要不要发、何时发"——那是告警集成单元的事（去重窗口 settings.notify_dedup_window
    也留给它用，本模块只定义配置不使用）；
  · 不做"按手机号反查 open_id"——应用权限缺 contact:user.id:readonly，本层不做这件事；
  · 失败**绝不抛异常**：一律返回 FeishuResult(ok=False, detail=中文原因)。通知是旁路功能，
    不能因为它把巡检主流程拖垮。

实测背景（生产机 10.128.118.113，2026-10）：
  · POST /open-apis/auth/v3/tenant_access_token/internal 用自建应用凭据可换取
    tenant_access_token（code=0，expire≈2020 秒）；
  · 机器人尚未被拉进任何群（GET /open-apis/im/v1/chats 列表为空），因此联调前要先在
    飞书后台把应用机器人加进目标群——最常见的发送失败就是 code=230002（见下方错误表）。
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

import httpx

from app.config import settings

_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
_MESSAGE_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

# token 还没到期就提前刷新，避免"拿到手已临界过期"的边界情况（提前 300 秒）。
_TOKEN_REFRESH_MARGIN = 300.0
# 兜底：飞书返回的 expire 小到连刷新余量都不够时，至少保留 60 秒可用期。
_TOKEN_MIN_TTL = 60.0

# 最多尝试 3 次（1 次首发 + 2 次重试），指数退避。只对网络异常 / HTTP 5xx / 飞书频控重试；
# 权限、参数、机器人不在群这类业务错误重试也不会好，直接失败并给运维人话原因。
_MAX_ATTEMPTS = 3
_RETRY_DELAYS = (0.5, 1.0, 2.0)
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}
# 频控码：开放平台《频控策略》——HTTP 429（旧接口 400）+ {"code": 99991400,
# "msg": "request trigger frequency limit"}。
_RETRYABLE_CODES = {99991400}

_VALID_RECEIVE_ID_TYPES = {"open_id", "user_id", "union_id", "email", "chat_id"}

# 常见业务码 → 运维能看懂的中文提示（原始 msg 仍会附在 detail 末尾，避免提示词失真）。
# 2300xx 系列出自 im/v1 文档的错误码表；9999 系列是开放平台通用错误码。
_CODE_HINTS = {
    99991663: "访问令牌无效（tenant token invalid）；若发生在取令牌阶段，请核对 app_id / app_secret",
    99991668: "应用未被授权调用该接口（需在飞书开放平台给应用开通对应权限并发布版本）",
    99991672: "应用缺少所需权限（Access denied：One of the following scopes is required …）—— "
              "请在飞书开放平台给该应用申请并发布对应权限。★ 2026-10-05 实测：调 contact/v3/users/"
              "batch_get_id 缺 contact:user.id:readonly 时返回的正是此码，**与令牌无关**"
              "（令牌类问题是 99991663）；原来这里写成「访问令牌无效」，会把运维引向错误方向。",
    99991400: "触发飞书频控（request trigger frequency limit），请降低发送频率",
    230001: "请求参数不合法（invalid request parameter），请核对 receive_id / receive_id_type / 消息内容",
    230002: "机器人不在目标群里（The bot can not be outside the group）——请先在飞书把应用机器人拉进群，"
            "再核对 receive_id",
    230006: "应用未启用机器人能力（请在开发者后台「应用功能-机器人」开启并发布上线）",
    230027: "应用缺少必要权限（Lack of necessary permissions），请按接口文档补齐并发布版本",
}

# 项目告警规则 AlertRule.operator 的词汇（见 app/core/models.py）与现成比较符都接受。
_OP_SYMBOL = {
    "gt": ">", "gte": "≥", "ge": "≥",
    "lt": "<", "lte": "≤", "le": "≤",
    "eq": "=", "ne": "≠", "neq": "≠",
}

_SEVERITY_CN = {
    "critical": "严重", "fatal": "严重", "p1": "严重",
    "emergency": "紧急", "emerg": "紧急",
    "error": "错误", "high": "高",
    "warning": "警告", "warn": "警告", "medium": "一般", "notice": "注意",
    "info": "提示", "low": "提示",
}


@dataclass
class FeishuResult:
    """一次发送的结果。detail 失败时是可直接给运维看的中文原因；成功固定写 "sent"。"""

    ok: bool
    detail: str
    attempts: int = 0


# ---- 进程内 token 缓存（模块级；跨事件循环只存字符串/时间戳，故不用 asyncio.Lock）----
# 并发下最坏情况是两个协程同时发现过期、各取一次 token——取令牌是幂等操作，无害。
_token: str = ""
_token_expire_at: float = 0.0


def _reset_token_cache() -> None:
    """清空 token 缓存（测试用；运行期更换了应用凭据后也可手工调用强制重取）。"""
    global _token, _token_expire_at
    _token = ""
    _token_expire_at = 0.0


class _DefinitiveFailure(Exception):
    """不值得重试的失败（凭据/权限/参数类）。detail 为可直接给运维看的中文。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


class _RetryableFailure(Exception):
    """值得重试的失败（网络/5xx/频控在响应体里的形态）。detail 为中文描述。"""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


async def _sleep(seconds: float) -> None:
    """重试退避。独立成函数是为了让测试能把等待 mock 掉（用例绝不真等）。"""
    await asyncio.sleep(seconds)


def settings_at_open_ids() -> list[str]:
    """把逗号分隔的配置拆成 list（容忍中文逗号与首尾空白）。"""
    raw = (settings.feishu_at_open_ids or "").replace("，", ",")
    return [p.strip() for p in raw.split(",") if p.strip()]


def feishu_configured() -> bool:
    """是否具备发送条件：notify_enabled 且 app_id/secret/receive_id 都非空。

    纯本地判断，**不发任何网络请求**，可直接用于 /health。
    """
    return bool(
        settings.notify_enabled
        and (settings.feishu_app_id or "").strip()
        and (settings.feishu_app_secret or "").strip()
        and (settings.feishu_receive_id or "").strip()
    )


def _at_lines(at_open_ids: list[str] | None, at_all: bool) -> list[str]:
    """渲染 @ 行。条目支持 "ou_xxx|显示名"；没有名字就回退用 id 本身（飞书 text 的惯例）。"""
    lines: list[str] = []
    if at_all:
        lines.append('<at user_id="all">所有人</at>')
    for entry in at_open_ids or []:
        entry = (entry or "").strip()
        if not entry:
            continue
        uid, _, name = entry.partition("|")
        uid = uid.strip()
        if not uid:
            continue
        name = name.strip() or uid
        lines.append(f'<at user_id="{uid}">{name}</at>')
    return lines


def _severity_label(severity: str) -> str:
    """严重级中文标签；认识的词翻成中文，不认识的原样透出。"""
    return _SEVERITY_CN.get((severity or "").strip().lower(), (severity or "").strip())


def _metric_line(m: dict) -> str:
    """一行指标：· 名称：实测值单位（阈值 比较符 阈值单位）。缺项给占位不崩。"""
    name = str(m.get("name") or "未命名指标").strip() or "未命名指标"
    value = m.get("value")
    value = "未知" if value is None else str(value)
    unit = str(m.get("unit") or "").strip()
    threshold = m.get("threshold")
    threshold = "" if threshold is None else str(threshold)
    op_raw = str(m.get("op") or "").strip()
    op_sym = _OP_SYMBOL.get(op_raw.lower(), op_raw)
    cond = " ".join(p for p in (op_sym, threshold + unit) if p)
    line = f"· {name}：{value}{unit}"
    if cond:
        line += f"（阈值 {cond}）"
    return line


def build_alert_text(*, title: str, severity: str, asset_name: str, asset_host: str,
                     metrics: list[dict], rule_name: str, occurred_at: str,
                     source: str = "OpsToolkit 巡检",
                     at_open_ids: list[str] | None = None, at_all: bool = False,
                     extra_note: str = "") -> str:
    """渲染告警文本（飞书 text 消息正文，换行用真实 "\\n"）。

    · metrics 每项形如 {"name","value","unit","threshold","op"}，逐行渲染，缺项给占位；
      op 接受项目告警规则的词汇（gt/gte/lt/lte/eq/ne，见 AlertRule.operator），
      也接受现成比较符（">" 等，原样透出）。
    · @ 条目支持 "ou_xxx|张三"（带显示名）或 "ou_xxx"（显示名回退为 id 本身）；
      @ 统一放**行首**、位于正文之前（飞书 text 的惯例）。
    · at_all=True 时使用 <at user_id="all">所有人</at>。
    """
    lines: list[str] = _at_lines(at_open_ids, at_all)
    sev = _severity_label(severity)
    lines.append(f"【{sev}告警】{(title or '未命名告警').strip()}")
    lines.append("来源：%s" % ((source or "OpsToolkit 巡检").strip()))
    lines.append("严重级：%s" % (sev or "-"))
    lines.append("设备：%s（%s）" % ((asset_name or "未命名设备").strip(),
                                    (asset_host or "未知IP").strip()))
    lines.append("规则：%s" % ((rule_name or "-").strip()))
    lines.append("时间：%s" % ((occurred_at or "-").strip()))
    if metrics:
        lines.append("指标：")
        lines.extend(_metric_line(m) for m in metrics)
    if (extra_note or "").strip():
        lines.append("备注：%s" % extra_note.strip())
    return "\n".join(lines)


async def _post_json(url: str, payload: dict, *, headers: dict | None = None) -> tuple[int, dict]:
    """单次 HTTP POST（JSON 请求体）。返回 (HTTP 状态码, 响应 JSON)。

    网络异常原样向上抛（由 send_text 统一按"可重试"处理）；
    响应不是合法 JSON 时返回空 dict（交由上层按 HTTP 状态码描述）。
    """
    timeout = max(1.0, float(settings.feishu_timeout or 8.0))
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=payload, headers=headers or None)
    try:
        body = resp.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    return resp.status_code, body


def _describe_http(action: str, status: int, body: dict) -> str:
    """把一次 HTTP 失败翻成运维能直接看懂的中文（保留飞书原始 msg 以防提示词失真）。"""
    code = body.get("code")
    msg = str(body.get("msg") or "").strip()
    if status == 401:
        base = f"{action}失败：飞书返回 401，访问令牌无效或已过期"
    elif status == 403:
        base = f"{action}失败：飞书返回 403，应用没有调用该接口的权限" \
               "（请在开放平台开通 im 消息权限并发布版本）"
    elif isinstance(code, int) and code in _CODE_HINTS:
        base = f"{action}失败：飞书错误 code={code}：{_CODE_HINTS[code]}"
    elif isinstance(code, int) and code != 0:
        base = f"{action}失败：飞书错误 code={code}：{msg or '（无描述）'}"
    else:
        base = f"{action}失败：HTTP {status}"
    if msg and msg not in base:
        base += f"（飞书原始信息：{msg}）"
    return base


async def _fetch_token() -> str:
    """取 tenant_access_token，带进程内缓存（过期前 300 秒刷新）。

    失败抛 _DefinitiveFailure / _RetryableFailure；网络异常原样抛，由 send_text 统一处理。
    """
    global _token, _token_expire_at
    now = time.time()
    if _token and now < _token_expire_at:
        return _token
    status, body = await _post_json(_TOKEN_URL, {
        "app_id": settings.feishu_app_id,
        "app_secret": settings.feishu_app_secret,
    })
    if status != 200:
        detail = _describe_http("获取访问令牌", status, body)
        if status in _RETRYABLE_STATUS or body.get("code") in _RETRYABLE_CODES:
            raise _RetryableFailure(detail)
        raise _DefinitiveFailure(detail)
    code = body.get("code")
    token = str(body.get("tenant_access_token") or "").strip()
    if code != 0 or not token:
        detail = _describe_http("获取访问令牌", status, body)
        if code in _RETRYABLE_CODES:
            raise _RetryableFailure(detail)
        raise _DefinitiveFailure(detail)
    try:
        expire = float(body.get("expire") or 0)
    except (TypeError, ValueError):
        expire = 0.0
    # 缓存期 = 有效期 - 300 秒（提前刷新）；算出来太短就退到 min(60, expire)，
    # expire 缺失/为 0 时干脆不缓存（expire_at=now，下一条消息重新取令牌）。
    ttl = expire - _TOKEN_REFRESH_MARGIN
    if ttl < _TOKEN_MIN_TTL:
        ttl = min(_TOKEN_MIN_TTL, expire) if expire > 0 else 0.0
    _token = token
    _token_expire_at = now + ttl if ttl > 0 else now
    return _token


async def send_text(text: str, *, at_open_ids=None, at_all: bool = False) -> FeishuResult:
    """发一条纯文本消息（msg_type=text）到 settings.feishu_receive_id。

    · @：text 里已经带 <at …> 就**不再重复添加**；否则若调用方给了 at_open_ids/at_all
      （未给时回落到 settings.feishu_at_open_ids / feishu_at_all），按行首惯例补在正文前。
    · 失败不抛异常，返回 ok=False + 中文原因；网络/5xx/频控按指数退避重试，
      最多尝试 3 次（1 次首发 + 2 次重试）；单次请求超时 settings.feishu_timeout 秒。
    · 未启用/配置不全（feishu_configured()==False）时**一个请求都不发**，直接返回失败。
    · attempts = 本次实际尝试的轮数（成功通常为 1；未发请求为 0）。
    """
    if not feishu_configured():
        return FeishuResult(
            ok=False,
            detail="通知未启用或飞书配置不全（需要 notify_enabled=True，且 app_id / "
                   "app_secret / receive_id 均非空），本次不发送",
            attempts=0,
        )
    receive_id_type = (settings.feishu_receive_id_type or "chat_id").strip() or "chat_id"
    if receive_id_type not in _VALID_RECEIVE_ID_TYPES:
        return FeishuResult(
            ok=False,
            detail=f"feishu_receive_id_type 不合法：{receive_id_type!r}"
                   f"（可选：{'/'.join(sorted(_VALID_RECEIVE_ID_TYPES))}）",
            attempts=0,
        )

    if "<at" not in text:
        ids = at_open_ids if at_open_ids is not None else settings_at_open_ids()
        want_all = bool(at_all) or bool(settings.feishu_at_all)
        at_prefix = _at_lines(ids, want_all)
        if at_prefix:
            text = "\n".join(at_prefix + [text])

    url = f"{_MESSAGE_URL}?receive_id_type={receive_id_type}"
    payload = {
        "receive_id": settings.feishu_receive_id,
        "msg_type": "text",
        # 飞书要求 content 是"JSON 序列化后的字符串"；ensure_ascii=False 保留中文原样。
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    headers: dict = {}
    last_detail = ""
    attempts = 0
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        attempts = attempt
        try:
            token = await _fetch_token()
            headers["Authorization"] = f"Bearer {token}"
            status, body = await _post_json(url, payload, headers=headers)
            if status == 200 and body.get("code") == 0:
                return FeishuResult(ok=True, detail="sent", attempts=attempts)
            detail = _describe_http("发送消息", status, body)
            if status in _RETRYABLE_STATUS or body.get("code") in _RETRYABLE_CODES:
                last_detail = detail
            else:
                raise _DefinitiveFailure(detail)
        except _RetryableFailure as exc:
            last_detail = exc.detail
        except _DefinitiveFailure as exc:
            return FeishuResult(ok=False, detail=exc.detail, attempts=attempts)
        except (httpx.HTTPError, OSError) as exc:
            last_detail = (f"网络异常，无法连接飞书（第 {attempt} 次尝试）："
                           f"{type(exc).__name__}: {exc}")
        if attempt < _MAX_ATTEMPTS:
            await _sleep(_RETRY_DELAYS[min(attempt - 1, len(_RETRY_DELAYS) - 1)])
    return FeishuResult(
        ok=False,
        detail=last_detail or f"发送失败：已尝试 {attempts} 次仍未成功（未知原因）",
        attempts=attempts,
    )
