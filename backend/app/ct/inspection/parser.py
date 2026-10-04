"""命令输出解析：命令拒绝识别 → Comware 真机格式精准解析 → ntc-templates
→ 自定义 TextFSM → 关键指标正则回退 → 原文摘要。

status 取值：ok / warning / critical / unknown / unsupported。
★ unsupported（2026-10-05 新增）：设备对这条命令报"不认识"（Unrecognized /
  Too many parameters / Invalid input …），即该机型没有这个指标特性 ——
  明确标注为"该机型不支持此指标"，而不是含糊的"未解析/unknown"。
"""
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


# ── "设备不认这条命令"识别（候选命令回退与 unsupported 标注的基础） ──────────────
# 各厂商 CLI 对无法识别命令的报错特征（大小写不敏感、子串匹配）：
#   Comware: % Unrecognized command found at '^' position. / % Too many parameters found at ...
#   VRP    : Error: Unrecognized command found at '^' position.
#   IOS    : % Invalid input detected at '^' marker. / % Ambiguous command / % Incomplete command.
_REJECT_SIGNATURES = (
    "unrecognized command",
    "too many parameters",
    "wrong parameter",
    "ambiguous command",
    "incomplete command",
    "% invalid input",
    "error: unrecognized",
    "% unrecognized",
)
# 真报错整段就 2~3 行（回显 + '^' + 报错行）。正常命令输出几乎总是更长 ——
# 用"非空行数 ≤ 6 且命中特征"双条件把误判概率压到极低（正常日志里出现这些词
# 通常出现在长输出中，不会同时满足短输出条件）。
_REJECT_MAX_LINES = 6
# syslog 行（如 "%Oct  4 13:16:33:843 2026 HOST SHELL/4/...: ..."）里可能复述报错字样，
# 但那是历史日志不是当前命令的报错 —— 命中时间戳形态的行一律跳过。
_SYSLOG_LINE_RE = re.compile(r"^%\s*[A-Za-z]{3}\s+\d{1,2}\s+\d")


def is_command_rejected(output) -> bool:
    """判断一段回显是否是"设备不认识这条命令"的报错。

    判据（三条同时满足才判，宁缺勿滥）：
      1) 非空行数 ≤ _REJECT_MAX_LINES（真报错就 2-3 行，长输出一律不算）；
      2) 至少一行命中 _REJECT_SIGNATURES 特征串（大小写不敏感）；
      3) 命中行不是 syslog 时间戳行（日志回显复述报错字样时不算）。
    空输出不算被拒（设备可能只是没回显）。
    """
    if output is None:
        return False
    text = str(output)
    if not text.strip():
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) > _REJECT_MAX_LINES:
        return False
    for ln in lines:
        if _SYSLOG_LINE_RE.match(ln):
            continue
        low = ln.lower()
        if any(sig in low for sig in _REJECT_SIGNATURES):
            return True
    return False


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

    # 0) 设备不认这条命令（≤6 行短输出 + 报错特征）⇒ unsupported，解析没有意义
    if is_command_rejected(output):
        result["summary"] = ("该机型不支持此指标（设备拒绝命令：%s）" % command) if command \
            else "该机型不支持此指标（设备拒绝该命令）"
        result["status"] = "unsupported"
        return result

    # 1) Comware 真机基线格式（CMW7）精准解析：ntc-templates 与自定义 TextFSM 都不
    #    覆盖 `display cpu-usage` / `display memory` / `display environment` 的真机格式，
    #    先按真机基线格式解析，避免落到泛化回退产生 0%/2% 这类错值；
    #    格式不匹配时返回 unknown → 继续走原链路，不影响其它厂商/其它格式。
    comware = _COMWARE_PARSERS.get(key)
    if comware:
        try:
            parsed = comware(output)
            if parsed and parsed.get("status") != "unknown":
                result["parsed"] = parsed
                result["summary"] = parsed.get("summary", "")
                result["status"] = parsed.get("status", "ok")
                return result
        except Exception:  # noqa: BLE001
            pass

    # 2) ntc-templates（成熟库，优先）
    rows = _ntc_parse(device_type, command, output)
    if rows:
        result["parsed"] = rows
        result["summary"] = _summarize_rows(rows)
        result["status"] = "ok"
        return result

    # 3) 自定义 TextFSM
    tmpl = load_textfsm(textfsm_name)
    if tmpl is not None:
        try:
            # ★ 模板对象按名字缓存、被同进程反复使用，而 textfsm 1.1.3 的 ParseText
            #   **不清空**上一次的结果（往对象内 _result 追加）——不 Reset 的话，
            #   同一进程里解析的第二台设备会带上第一台设备的行（多台巡检必踩的真 bug）。
            if hasattr(tmpl, "Reset"):
                tmpl.Reset()
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

    # 4) 关键指标正则兜底
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

    # 5) 原文摘要
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


# ── H3C Comware 真机基线格式（CMW7）精准解析 ─────────────────────────────────
# 依据真机基线（mimo/out/captures/h3c_{vfw,vsr,switch}_capture.json）实测回显：
#   · vFW/vSR: `display cpu-usage` → "Unit CPU usage:" + "24% in last 5 seconds"
#   · S10508X: "Slot 1 CPU 0 CPU usage:" + "0% in last 5 seconds"
#   · memory  : "Mem: 8062308 4318124 3744184 0 10528 387284 47.0%"（Total/Used/Free/
#               Shared/Buffers/Cached/FreeRatio），使用率 = 100 − FreeRatio
#   · environment（交换机）: "System temperature information (degree centigrade):" 温度表
# 此前这两个指标的错误值（cpu=unknown、内存 0%/2%/5%）就来自泛化正则逮住了
# FreeRatio 的小数尾巴 —— 现在按真机格式精准解析，格式不匹配返回 unknown 放行走原链。

_CPU_USAGE_HEADER_RE = re.compile(r"^(?P<block>.+?)\s+CPU\s+usage\s*:\s*$", re.IGNORECASE)
_CPU_5SEC_RE = re.compile(r"(?P<val>\d+)%\s+in\s+last\s+5\s+seconds", re.IGNORECASE)
_MEM_ROW_RE = re.compile(
    r"^\s*Mem\s*:\s*(?P<total>\d+)\s+(?P<used>\d+)\s+(?:\d+\s+){4}(?P<free_ratio>\d+(?:\.\d+)?)\s*%",
    re.MULTILINE,
)
_ENV_ROW_RE = re.compile(
    r"^\s*(?P<slot>\d+(?:/\d+){0,2})\s+"
    r"(?P<sensor>\S+(?:\s+\d+)?)\s+"
    r"(?P<temp>-?\d+(?:\.\d+)?)\s+"
    r"(?P<lower>-?\d+(?:\.\d+)?)\s+"
    r"(?P<warn>-?\d+(?:\.\d+)?)\s+"
    r"(?P<alarm>-?\d+(?:\.\d+)?)\s*$"
)


def _num(text):
    """"47.0" → 47.0；"30" → 30（整数值用 int，避免界面上出现 30.0）。"""
    v = float(text)
    return int(v) if v.is_integer() else v


def _parse_cpu_comware(text):
    """H3C Comware `display cpu-usage`（CMW7 真机基线格式）：

        Unit CPU usage:                 ← vFW / vSR
              24% in last 5 seconds
        Slot 1 CPU 0 CPU usage:         ← S10508X 交换机（多槽位时每个槽一段）
               0% in last 5 seconds

    取每个槽位 "last 5 seconds" 的值；多槽位取最大值，摘要写清是哪几个槽。
    """
    slots = []  # [(槽位描述, last-5-seconds 利用率)]
    current = None
    for raw in str(text).splitlines():
        ln = raw.strip()
        if not ln:
            continue
        m = _CPU_USAGE_HEADER_RE.match(ln)
        if m:
            current = m.group("block").strip()
            continue
        m = _CPU_5SEC_RE.search(ln)
        if m and current is not None:
            slots.append((current, int(m.group("val"))))
    if not slots:
        # 个别版本没有 "XXX CPU usage:" 头，直接输出 "N% in last 5 seconds"
        for m in _CPU_5SEC_RE.finditer(str(text)):
            slots.append(("", int(m.group("val"))))
    if not slots:
        return {"summary": "cpu 未解析", "status": "unknown"}
    peak = max(v for _, v in slots)
    where = list(dict.fromkeys(name for name, v in slots if v == peak))
    status = "ok" if peak < 70 else ("warning" if peak < 90 else "critical")
    loc = ""
    if where and where != [""]:
        loc = "（%s%s）" % ("、".join(where), "，多槽位取最大" if len(slots) > 1 else "")
    summary = "cpu " + str(peak) + "%" + loc
    return {"cpu": peak, "slots": [{"slot": name, "cpu": v} for name, v in slots],
            "summary": summary, "status": status}


def _parse_memory_comware(text):
    r"""H3C Comware `display memory`（CMW7 真机基线格式）：

        Mem: 8062308   4318124   3744184     0   10528   387284   47.0%
             Total      Used      Free    Shared Buffers  Cached  FreeRatio

    ★ 使用率 = 100 − FreeRatio（统一公式，多槽位取最大值）。
      旧实现 `(\d+)%` 只逮到小数尾巴（47.0%→0%、69.2%→2%、81.5%→5%）——已修。
    """
    rows = []
    for m in _MEM_ROW_RE.finditer(str(text)):
        free_ratio = float(m.group("free_ratio"))
        rows.append({
            "total_kb": int(m.group("total")),
            "used_kb": int(m.group("used")),
            "free_ratio": free_ratio,
            "usage": round(max(0.0, min(100.0, 100.0 - free_ratio)), 1),
        })
    if not rows:
        return {"summary": "内存未解析", "status": "unknown"}
    peak = max(r["usage"] for r in rows)
    status = "ok" if peak < 80 else ("warning" if peak < 92 else "critical")
    summary = "内存 " + ("%g" % peak) + "%" + ("（多槽位取最大）" if len(rows) > 1 else "")
    return {"memory": peak, "rows": rows, "summary": summary, "status": status}


def _parse_environment_comware(text):
    """H3C Comware `display environment`（CMW7 交换机真机基线格式）—— 温度表：

        System temperature information (degree centigrade):
        -------------------------------------
        Slot  Sensor    Temperature LowerLimit WarningLimit AlarmLimit
         0/1  Inflow  1   30           -5          66          76
         0/1  Hotspot 3   44           -5          66          76

    解析每个传感器的温度与告警限；主温度取第一个传感器（Inflow 进风口，最稳定
    的代表读数），摘要列出全部传感器并标注最高温；状态按 Warning/AlarmLimit 判定。
    （vFW/vSR 没有 `display environment` 命令，上游已判 unsupported，不会走到这里。）
    """
    sensors = []
    for raw in str(text).splitlines():
        m = _ENV_ROW_RE.match(raw)
        if m:
            d = m.groupdict()
            sensors.append({
                "slot": d["slot"],
                "sensor": d["sensor"],
                "temperature": _num(d["temp"]),
                "lower_limit": _num(d["lower"]),
                "warning_limit": _num(d["warn"]),
                "alarm_limit": _num(d["alarm"]),
            })
    if not sensors:
        return {"summary": "温度未解析", "status": "unknown"}
    status = "ok"
    for s in sensors:
        if s["alarm_limit"] and s["temperature"] >= s["alarm_limit"]:
            status = "critical"
        elif s["warning_limit"] and s["temperature"] >= s["warning_limit"] and status != "critical":
            status = "warning"
    first = sensors[0]
    hottest = max(sensors, key=lambda s: s["temperature"])
    summary = "温度 %gC（%s %s）" % (first["temperature"], first["slot"], first["sensor"])
    if len(sensors) > 1:
        summary += "；共 %d 个传感器，最高 %gC（%s %s）" % (
            len(sensors), hottest["temperature"], hottest["slot"], hottest["sensor"])
    return {"temperature": first["temperature"], "max_temperature": hottest["temperature"],
            "sensors": sensors, "summary": summary, "status": status}


_COMWARE_PARSERS = {
    "cpu": _parse_cpu_comware,
    "memory": _parse_memory_comware,
    "environment": _parse_environment_comware,
}
