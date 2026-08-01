"""命令输出解析：ntc-templates 优先，回退自定义 TextFSM，再回退关键指标正则，最后原文摘要。"""
from __future__ import annotations

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
    return str(len(rows)) + " 行; " + ", ".join(parts)


def _percent_helper(text, patterns, label):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = int(m.group(1))
            except ValueError:
                continue
            warn, crit = (70, 90) if label == "cpu" else (80, 92)
            status = "ok" if val < warn else ("warning" if val < crit else "critical")
            return {label: val, "summary": label + " " + str(val) + "%", "status": status}
    return {"summary": label + " 未解析", "status": "unknown"}


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
        return {"memory": val, "summary": "内存 " + str(val) + "%", "status": status}
    return {"summary": "内存未解析", "status": "unknown"}


def _parse_version(text):
    patterns = [
        r"H3C Comware Software, Version\s+([^,\s]+)",
        r"VRP[^,]*,?\s*Version\s+([^,\s]+)",
        r"Cisco IOS(?:-XE)? Software[^,]*,?\s*Version\s+([^,\s]+)",
        r"Software, Version\s+([^,\s]+)",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            ver = m.group(1).strip()
            return {"version": ver, "summary": "版本 " + ver, "status": "ok"}
    return {"summary": "版本未解析", "status": "unknown"}


def _parse_interface(text):
    up = len(re.findall(r"\b(?:up|administratively up)\b", text, re.IGNORECASE))
    down = len(re.findall(r"\bdown\b", text, re.IGNORECASE))
    total = up + down
    status = "ok" if down == 0 else "warning"
    return {
        "up": up, "down": down, "total": total,
        "summary": "接口 up=" + str(up) + " down=" + str(down),
        "status": status,
    }


def _parse_temperature(text):
    m = re.search(r"(?:temperature|temp)[^0-9]{0,40}(\d+(?:\.\d+)?)\s*(?:c|℃)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:degrees c|℃)", text, re.IGNORECASE)
    if m:
        try:
            val = float(m.group(1))
            status = "ok" if val < 60 else ("warning" if val < 75 else "critical")
            return {"temperature": val, "summary": "温度 " + str(val) + "C", "status": status}
        except ValueError:
            pass
    return {"summary": "温度未解析", "status": "unknown"}


def _parse_status_rows(text, label):
    ok_words = ["normal", "ok", "present"]
    bad_words = ["abnormal", "fail", "fault", "absent", "down"]
    ok = sum(len(re.findall(r"\b" + w + r"\b", text, re.IGNORECASE)) for w in ok_words)
    bad = sum(len(re.findall(r"\b" + w + r"\b", text, re.IGNORECASE)) for w in bad_words)
    status = "ok" if bad == 0 else "critical"
    return {label: {"ok": ok, "bad": bad}, "summary": label + " ok=" + str(ok) + " bad=" + str(bad), "status": status}


def _parse_device(text):
    return _parse_status_rows(text, "device")


def _parse_power(text):
    return _parse_status_rows(text, "power")


def _parse_fan(text):
    return _parse_status_rows(text, "fan")


def _parse_environment(text):
    temp = _parse_temperature(text)
    statuses = [_parse_power(text), _parse_fan(text), _parse_device(text)]
    bad = sum(s["status"] == "critical" for s in statuses)
    summary = temp["summary"]
    if bad:
        status = "critical"
        summary += " ，有故障"
    else:
        status = temp["status"] if temp["status"] != "unknown" else "ok"
    return {
        "temperature": temp.get("temperature"),
        "power": statuses[0].get("power", {}),
        "fan": statuses[1].get("fan", {}),
        "device": statuses[2].get("device", {}),
        "summary": summary,
        "status": status,
    }


def _parse_inventory(text):
    rows = [ln.strip() for ln in text.splitlines() if re.search(r"pid:|device name|product", ln, re.IGNORECASE)]
    return {"count": len(rows), "summary": "硬件条目 " + str(len(rows)), "status": "ok" if rows else "unknown"}


_KEY_METRIC_PARSERS = {
    "cpu": _parse_cpu,
    "memory": _parse_memory,
    "version": _parse_version,
    "interface": _parse_interface,
    "temperature": _parse_temperature,
    "device": _parse_device,
    "power": _parse_power,
    "fan": _parse_fan,
    "environment": _parse_environment,
    "inventory": _parse_inventory,
}
