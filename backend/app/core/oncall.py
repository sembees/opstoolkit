# -*- coding: utf-8 -*-
"""值班人查询客户端（值班平台集成「模式 A：被查询方」）。

接口契约（已冻结）：ONCALL-PLATFORM-PLAN.md §4.1 ——
    GET {base}/api/oncall/current?team=…      # Authorization: Bearer <token>
    200 {
      "team": …,
      "primary": {"name": "张三", "feishu_open_id": "ou_xxx", …},
      "backup":  {"name": "李四", "feishu_open_id": "ou_yyy", …},
      "empty_reason": null | "no_shift|team_not_found",
      …
    }

职责边界：只做"现在该 @ 谁"这一件事。要不要 @、发什么消息、去重，都是
notify/alerts.py（告警集成层）的事。

可靠性设计（§3 的"失败降级必须写进 P0"）：
  · **任何失败都不抛异常**：超时 / 断网 / 非 200 / 响应不是 JSON 对象 / 字段缺失，
    一律返回 `([], 状态码)`，由调用方走降级（配置 @ → @all → 不 @）——
    值班平台挂了，告警外发不能跟着挂；
  · 查询超时 2 秒：平台不可用时要快速失败，不能让一条告警卡在外发前置查询上；
  · 30 秒内存缓存（契约允许缓存 30–60 秒；同 team 同结果），
    **只缓存成功结果** —— 失败不缓存，下一条告警还能再试一次平台；
  · 缓存是进程内的：重启即清；值班变化最多迟到 30 秒，可接受（换班不靠它做强一致）。

配置（app/config.py，默认全空 = 未配置 ⇒ 一个请求都不发）：
    oncall_base_url  平台根地址，如 http://oncall.internal:8000
    oncall_token     Bearer token（只进 .env / 环境变量，不进代码、不进仓库）
    oncall_team      默认团队；调用方也可用参数覆盖（参数优先）
"""
from __future__ import annotations

import time

import httpx

from app.config import settings

_TIMEOUT = 2.0    # 契约：2 秒拿不到结果就按"平台不可用"处理（不阻塞告警外发）
_CACHE_TTL = 30.0  # 成功结果缓存 30 秒（§4.1：幂等、无副作用、可缓存 30–60 秒）

# 进程内缓存：{team: (expire_monotonic, [targets])}。跨请求共享、重启即清。
_cache: dict[str, tuple[float, list[str]]] = {}


def _reset_cache() -> None:
    """清空缓存（测试用；运行期换了平台/想强制重查也可手工调用）。"""
    _cache.clear()


def oncall_configured() -> bool:
    """是否具备查询条件：oncall_base_url 非空。纯本地判断，不发网络请求。"""
    return bool((settings.oncall_base_url or "").strip())


def _member_target(member) -> str | None:
    """平台成员对象 → "ou_xxx|显示名"（feishu.send_text 的 at_open_ids 条目格式）。

    · 缺 feishu_open_id 的成员不可用（没法 @）→ None；
    · 缺 name 时回退用 open_id 本身（与 feishu._at_lines 的占位规则一致）。
    """
    if not isinstance(member, dict):
        return None
    oid = str(member.get("feishu_open_id") or "").strip()
    if not oid:
        return None
    name = str(member.get("name") or "").strip()
    return f"{oid}|{name}" if name else oid


async def _get_json(url: str, *, params=None, headers=None) -> tuple[int, object]:
    """单次 HTTP GET。返回 (HTTP 状态码, 解析后的 JSON 值（可能不是 dict）/ None)。

    独立成函数是为了让测试能把它整个 mock 掉 ⇒ 用例零联网。
    网络异常原样向上抛（由 current_oncall 统一按 timeout/error 分类，绝不外泄）。
    """
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(url, params=params, headers=headers or None)
    try:
        body = resp.json()
    except ValueError:  # 含 json.JSONDecodeError；非 JSON 响应体按 None 交上层判"error"
        body = None
    return resp.status_code, body


async def current_oncall(team: str = "") -> tuple[list[str], str]:
    """查"现在正在值班的人"。返回 `(["ou_xxx|显示名", …], 状态码)`。

    状态码：
      "ok"             查到当班人（targets 非空）
      "not_configured" 没配 oncall_base_url（默认态；一个请求都不发）
      "timeout"        2 秒内平台没回（含读超时/连接超时）
      "error"          断网、其它 HTTP 异常、非 200、响应不是 JSON 对象
      "empty"          平台正常应答但拿不到可用成员（无班次 / 成员缺 feishu_open_id）

    成功结果缓存 30 秒（同 team）；失败不缓存。★ 绝不抛异常。
    """
    team = (team or settings.oncall_team or "").strip()
    base = (settings.oncall_base_url or "").strip().rstrip("/")
    if not base:
        return [], "not_configured"

    now = time.monotonic()
    cached = _cache.get(team)
    if cached is not None and now < cached[0]:
        return list(cached[1]), "ok"

    url = f"{base}/api/oncall/current"
    token = (settings.oncall_token or "").strip()
    headers = {"Authorization": f"Bearer {token}"} if token else None
    params = {"team": team} if team else None
    try:
        status, body = await _get_json(url, params=params, headers=headers)
    except httpx.TimeoutException:
        return [], "timeout"
    except (httpx.HTTPError, OSError):
        return [], "error"
    except Exception:  # noqa: BLE001 —— 兜底：任何意外异常都不能外泄（通知是旁路）
        return [], "error"

    if status != 200 or not isinstance(body, dict):
        return [], "error"

    targets = [t for t in (_member_target(body.get("primary")),
                           _member_target(body.get("backup"))) if t]
    if not targets:
        # 平台活着、应答合法，但当前没有可 @ 的当班人（无班次/成员缺 open_id）
        return [], "empty"

    _cache[team] = (now + _CACHE_TTL, list(targets))
    return list(targets), "ok"
