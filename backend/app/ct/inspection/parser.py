"""命令输出解析：ntc-templates 优先，回退自定义 TextFSM，再回退关键指标正则，最后原文摘要。"""
import re
from pathlib import Path

try:
    import textfsm
except ImportError:  # noqa: BLE001
    textfsm = None

try:
    from ntc_templates import parse as ntc_parse
    _HAS_NTC = True
except Exception:  # noqa: BLE001
    _HAS_NTC = False

from app.config import TEXTFSM_DIR

_PARSER_CACHE = {}

# netmiko device_type -> ntc-templates platform
_PLATFORM_MAP = {
    "huawei": "huawei_vrp",
    "huawei_vrpv8": "huawei_vrp",
    "hp_comware": "hp_comware",
    "cisco_ios": "cisco_ios",
    "cisco_xe": "cisco_xe",
    "cisco_asa": "cisco_asa",
    "cisco_nxos": "cisco_nxos",
    "cisco_xr": "cisco_xr",
}


def load_textfsm(name):
    """加载自定义 TextFSM 模板。"""
    if not textfsm or not name:
        return None
    if name in _PARSER_CACHE:
        return _PARSER_CACHE[name]
    path = Path(TEXTFSM_DIR) / name
    if not path.exists():
        _PARSER_CACHE[name] = None
        return None
    with path.open(encoding="utf-8") as fh:
        tmpl = textfsm.TextFSM(fh)
    _PARSER_CACHE[name] = tmpl
    return tmpl


def _ntc_parse(device_type, command, output):
    """用 ntc-templates 解析；返回 list[dict] 或 None。"""
    if not _HAS_NTC or not device_type or not command:
        return None
    platform = _PLATFORM_MAP.get(device_type.lower(), device_type.lower())
    try:
        rows = ntc_parse.parse_output(platform=platform, command=command, data=output)
        return rows if rows else None
    except Exception:  # noqa: BLE001  无对应模板或解析失败
        return None


def parse_output(key, output, textfsm_name="", device_type="", command=""):
    """返回 {parsed, summary, status}。"""
    result = {"parsed": None, "summary": "", "status": "unknown"}

    # 1) ntc-templates（成熟库，优先）
    rows = _ntc_parse(device_type, command, output)
    if rows:
        result["parsed"] = rows
        result["summary"] = _summarize_rows(rows)
        result["status"] = "ok"
        return result

    # 2) 自定义 TextFSM
    tmpl = load_textfsm(textfsm_name)
    if tmpl is not None:
        try:
            parsed_rows = tmpl.ParseText(output)
            header = tmpl.header
            parsed = [dict(zip(header, row)) for row in parsed_rows]
            if parsed:
                result["parsed"] = parsed
                result["summary"] = _summarize_rows(parsed)
                result["status"] = "ok"
                return result
        except Exception:  # noqa: BLE001
            pass

    # 3) 关键指标正则兜底
    metric = _KEY_METRIC_PARSERS.get(key)
    if metric:
        try:
            parsed = metric(output)
            result["parsed"] = parsed
            result["summary"] = parsed.get("summary", "")
            result["status"] = parsed.get("status", "ok")
            return result
        except Exception:  # noqa: BLE001
            pass

    # 4) 原文摘要
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    result["summary"] = " | ".join(lines[:3]) if lines else "(空)"
    result["status"] = "unknown"
    return result


def _summarize_rows(rows):
    if not rows:
        return "(无数据)"
    first = rows[0]
    parts = [f"{k}={v}" for k, v in list(first.items())[:4]]
    return f"{len(rows)} 行; " + ", ".join(parts)


def _percent_helper(text, patterns, label):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            status = "ok" if val < 70 else ("warning" if val < 90 else "critical")
            return {label: val, "summary": f"{label} {val}%", "status": status}
    return {"summary": f"{label} 未解析", "status": "unknown"}


def _parse_cpu(text):
    return _percent_helper(text, [
        r"Five seconds?: *(\d+)%",
        r"five seconds: *(\d+)",
        r"(\d+)%.*one minute",
        r"CPU utilization.*?(\d+)%",
        r"(\d+)%\s*cpu",
    ], "cpu")


def _parse_memory(text):
    m = re.search(r"(\d+)%", text)
    if m:
        val = int(m.group(1))
        status = "ok" if val < 80 else ("warning" if val < 92 else "critical")
        return {"memory": val, "summary": f"内存 {val}%", "status": status}
    return {"summary": "内存未解析", "status": "unknown"}


_KEY_METRIC_PARSERS = {
    "cpu": _parse_cpu,
    "memory": _parse_memory,
}
