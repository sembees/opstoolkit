# -*- coding: utf-8 -*-
"""装机规划表（IP 规划表）解析与冲突校验 —— 纯逻辑模块。

【定位】只做"把已经读成行的表格解析成设备清单 + 冲突校验"。CSV/Excel 的读取由调用方
完成，本模块不碰文件、不做网络请求、不依赖任何第三方库（只用标准库
``ipaddress`` / ``re`` / ``unicodedata``）。

【真实表形态】多行合并单元格：同一"设备名称"下多行，IP 只写在第一行::

    序号 | 设备名称    | 设备位置 | 带外管理IP地址 | 带外管理地址网关 | 所属VLAN | 设备型号
    1    | SW-CORE-01 | A17U03  | 192.0.2.1/24  | /              | 10      | H3C S7506X-G
    2    |            | A16U03  |               |                | 10      | H3C S7506X-G

合并单元格被 CSV/Excel 读出后是"首行有值、后续行空单元格"，所以空单元格按
"向上继承最近一个非空值"处理。

【继承规则与理由】
- 继承：设备名称 / IP / 网关 / VLAN。它们在真实表里是"一个逻辑设备跨多行的合并
  单元格"——空行是合并的产物，不是"未填写"。
- 不继承：设备位置、设备型号。每行是一台独立的物理机器（例如 IRF/堆叠的两台盒子
  各占一个 U 位），位置/型号按行独立成立；若向上继承，第二台盒子的位置会被
  第一台的抹掉，装机时就会把两台机器装到同一个 U 位。

【新设备边界】某行显式写了与上方最近一次显式设备名**不同**的名字 = 新设备开始：
该行没填的 IP/网关/VLAN 不再继承上一台设备的值（那是"新设备忘了填"，不是"合并
单元格"），会被解析为"缺少 IP"，而不是制造"两台设备共用一个 IP"的假冲突。

【"一台设备名两行" = 两台物理机器、一条逻辑记录】
规划表的记录单位是"带外管理 IP"：同一个合并的 IP 单元格只属于一个逻辑设备
（两台交换机做 IRF/堆叠后共用一个管理地址）。两行各有一个设备位置，说明是两台
物理机器。所以 3 台设备 6 行应解析出 **3 条设备记录**，每条记录用 ``rows`` /
``locations`` 保留全部物理行的位置，供后续 MAC 自动发现后按物理行配对。若那两行
真是各自独立的机器，表里就不会共用同一个合并的 IP 单元格（一台 BMC 一个 IP）。
同设备名/IP 的连续行聚合为一条记录；同样的名字/IP 在不相邻的行再次出现（中间被
其他设备隔开）则视为重复并报错——合并继承后一条设备只应占一段连续行。

【占用探测（可选，默认关闭）】本模块默认**不做任何网络调用**::

    result = parse_plan(rows)                                        # 纯解析，零网络
    result = parse_plan(rows, occupancy_checker=build_ping_checker())  # 显式开启

``checker(ip) -> True(占用) / False(空闲) / None(未知)``。生产建议接 ``arping``
（二层探测更准，Windows 无自带 arping）或经交换机 CLI 查 ARP/MAC 表；
:func:`build_ping_checker` 只是标准库 subprocess ping 的最小可用实现，
只有显式传入才会执行。
"""
from __future__ import annotations

import ipaddress
import re
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

__all__ = ["parse_plan", "find_occupied_ips", "build_ping_checker", "PlanResult"]

PlanResult = Dict[str, Any]

# --------------------------------------------------------------------------
# 列名识别
# --------------------------------------------------------------------------
# 有序规则：先匹配先得。网关必须在 IP 之前（"带外管理地址网关"同时含"地址"与"网关"），
# VLAN 在 IP 之前（"所属VLAN"），位置/型号在 名称 之前（"设备位置"不能被当成名称）。
_COLUMN_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("gateway", ("网关", "gateway", "gw")),
    ("vlan", ("vlan",)),
    ("ip", ("ip", "管理地址", "管理ip", "设备ip", "ipaddress", "deviceip",
            "bmcip", "mgmtip", "oobip", "bmc", "oob", "address")),
    ("location", ("位置", "机柜", "机架", "u位", "rack", "location", "position", "slot")),
    ("model", ("型号", "model")),
    ("name", ("设备名", "名称", "主机名", "hostname", "name")),
    ("seq", ("序号", "编号", "index", "number", "no")),
]
# 短英文词需要词边界，避免 description 匹配 "ip"、notes 匹配 "no" 这类误报。
_SHORT_WORD_KEYWORDS = {"ip", "no"}

# 表格里的这些记号 = "无值"（用户用 "/" 表示网关为空，其余列同样按记号处理）。
_JUNK_TOKENS = {"/", "-", "—", "–", "无", "n/a", "na", "none", "null", "nil"}

# 表头/单元格里的分隔符（归一化时删除）：空格、全角空格、常见标点、BOM。
_SEP_RE = re.compile(r"[\s\u3000_\-—–/\\（）()\[\]【】：:，,。.·\"'“”‘’\ufeff]+")

_INHERIT_FIELDS = ("name", "ip", "gateway", "vlan")   # 空单元格向上继承
_ALL_FIELDS = ("seq",) + _INHERIT_FIELDS + ("location", "model")


def _cell_to_text(value: Any) -> str:
    """单元格 → 规范文本：None→""；Excel 数值(含 10.0)→整数字符串；NFKC 折叠全角。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    if isinstance(value, int):
        return str(value)
    return unicodedata.normalize("NFKC", str(value)).strip()


def _norm_header(value: Any) -> str:
    """表头归一化：NFKC + casefold + 去分隔符，让"设 备 名 称"/“设备名称”都能匹配。"""
    text = unicodedata.normalize("NFKC", _cell_to_text(value)).casefold()
    return _SEP_RE.sub("", text)


def _match_field(norm: str) -> Optional[str]:
    if not norm:
        return None
    for field, keywords in _COLUMN_RULES:
        for keyword in keywords:
            if keyword in _SHORT_WORD_KEYWORDS:
                # 短词用词边界，避免 description→ip、notes→no 的误匹配
                if re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", norm):
                    return field
            elif keyword in norm:
                return field
    return None


def _is_junk(text: str) -> bool:
    return text.strip().casefold() in _JUNK_TOKENS


def _header_display(value: Any) -> str:
    """表头展示文本：保留用户原文（只去首尾空白），不做 NFKC 折叠。"""
    if value is None:
        return ""
    return str(value).strip()


# --------------------------------------------------------------------------
# IP / 网关 / VLAN 解析
# --------------------------------------------------------------------------
def _parse_ip_cell(text: str) -> Dict[str, Any]:
    """解析 "10.10.10.1/24" → ``{ok, ip, prefix, netmask, network, bare, error}``。

    同时支持：``10.10.10.1/24``、``10.10.10.1 24``、``10.10.10.1 255.255.255.0``、
    ``10.10.10.1/255.255.255.0``、以及不带掩码的裸 IP（``bare=True``）。
    """
    raw = unicodedata.normalize("NFKC", (text or "")).strip()
    if not raw:
        return {"ok": False, "empty": True, "error": "IP 为空"}
    parts = raw.split()
    if len(parts) == 1:
        if "/" in raw:
            addr, _, mask = raw.partition("/")
            addr, mask = addr.strip(), mask.strip()
        else:
            addr, mask = raw, None
    elif len(parts) == 2:
        addr, mask = parts[0], parts[1]
        if "/" in addr:
            addr = addr.split("/", 1)[0]
    else:
        return {"ok": False, "empty": False, "error": f"IP“{raw}”格式无法识别（应为 x.x.x.x/掩码）"}
    try:
        ip_obj = ipaddress.ip_address(addr)
    except ValueError:
        return {"ok": False, "empty": False, "error": f"“{addr}”不是合法的 IP 地址"}
    if mask is None:
        return {"ok": True, "empty": False, "bare": True, "ip": str(ip_obj),
                "prefix": None, "netmask": None, "network": None, "error": None}
    max_prefix = 32 if ip_obj.version == 4 else 128
    if re.fullmatch(r"\d{1,3}", mask):
        prefix = int(mask)
        if not 0 <= prefix <= max_prefix:
            return {"ok": False, "empty": False,
                    "error": f"掩码 /{mask} 非法（应在 0-{max_prefix}）"}
        mask_text = str(prefix)  # 去掉可能的前导零（ipaddress 不接受 "/024"）
    else:
        mask_text = mask
    try:
        net = ipaddress.ip_network(f"{addr}/{mask_text}", strict=False)
    except ValueError:
        return {"ok": False, "empty": False, "error": f"掩码“{mask}”非法（应为 /0-{max_prefix} 或点分十进制）"}
    return {"ok": True, "empty": False, "bare": False, "ip": str(ip_obj),
            "prefix": net.prefixlen, "netmask": str(net.netmask),
            "network": str(net), "error": None}


def _parse_gateway_value(text: str) -> Dict[str, Any]:
    """网关单元格取值：兼容误写的 "10.0.0.254/24"（取主机部分，忽略掩码）。"""
    raw = unicodedata.normalize("NFKC", (text or "")).strip()
    host = raw.split("/", 1)[0].strip()
    try:
        gw_obj = ipaddress.ip_address(host)
    except ValueError:
        return {"state": "invalid", "raw": raw}
    if str(gw_obj) == "0.0.0.0":
        return {"state": "zero", "raw": raw}
    return {"state": "value", "gw": str(gw_obj), "raw": raw}


# --------------------------------------------------------------------------
# 输入行规整
# --------------------------------------------------------------------------
def _detect_columns(header_cells: Sequence[Tuple[Any, Any]]):
    """``header_cells`` 为 (位置或键, 原始表头) 序列 → (字段→键, 字段→表头, 未识别表头, 冲突警告)。"""
    detected: Dict[str, Any] = {}
    detected_headers: Dict[str, str] = {}
    ignored: List[str] = []
    warnings: List[Dict[str, Any]] = []
    for key, raw in header_cells:
        norm = _norm_header(raw)
        if not norm:
            continue
        field = _match_field(norm)
        if field is None:
            ignored.append(_header_display(raw))
        elif field in detected:
            warnings.append({"row": 0, "field": "header",
                             "message": f"表头“{detected_headers[field]}”与“{_header_display(raw)}”"
                                        f"都识别为“{field}”列，保留靠左的“{detected_headers[field]}”，"
                                        f"忽略靠右的“{_header_display(raw)}”"})
        else:
            detected[field] = key
            detected_headers[field] = _header_display(raw)
    return detected, detected_headers, ignored, warnings


def _make_cell_reader(detected: Dict[str, Any], detected_headers: Dict[str, str]):
    """返回 reader(raw_row, field) -> 规范文本，兼容 dict 行 / list 行混排。"""

    def reader(raw_row: Any, field: str) -> str:
        if field not in detected:
            return ""
        key = detected[field]
        if isinstance(raw_row, dict):
            if isinstance(key, str) and key in raw_row:
                return _cell_to_text(raw_row[key])
            header_text = detected_headers.get(field)
            if field in raw_row:
                return _cell_to_text(raw_row[field])
            if header_text is not None and header_text in raw_row:
                return _cell_to_text(raw_row[header_text])
            return ""
        if isinstance(key, int) and key < len(raw_row):
            return _cell_to_text(raw_row[key])
        return ""

    return reader


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
def parse_plan(rows: Sequence[Any],
               occupancy_checker: Optional[Callable[[str], Optional[bool]]] = None) -> PlanResult:
    """解析装机规划表并做冲突校验。

    :param rows: 已读成行的表格。两种形态：
        - ``list[list]``：**第一行是表头行**，其余为数据行；
        - ``list[dict]``：每行一个字典，键为表头（表头隐含在键里）。
    :param occupancy_checker: 可选占用探测函数 ``checker(ip) -> True/False/None``。
        默认 ``None``——**绝不发起任何网络调用**；显式传入（如
        ``build_ping_checker()``）后才逐个唯一 IP 探测，占用者计入 ``warnings``。
    :returns: ``{"devices": [...], "errors": [...], "warnings": [...], "stats": {...}}``

    ``devices`` 每条为
    ``{name, location, ip, prefix, netmask, network, gateway, vlan, model, row,
       rows, locations, seq, physical_units}``；
    其中 ``row`` 是该逻辑设备首行（数据行行号，从 1 起、不含表头行），``rows`` 是
    全部物理行，``locations`` 为 ``[{"row": 行号, "location": 位置或 None}]``。
    ``errors``/``warnings`` 每条为 ``{"row": 行号(0=表级), "field": 字段, "message": 中文原因}``。
    """
    if rows is None:
        raise TypeError("rows 不能为 None：应传入 list[dict] 或 list[list]（含表头行）")
    if isinstance(rows, dict):
        raise TypeError("rows 应为行的列表（list[dict] 或 list[list]），不是单个 dict")
    if not isinstance(rows, (list, tuple)):
        raise TypeError("rows 应为行的列表：list[dict] 或 list[list]（含表头行）")

    rows = list(rows)
    stats: Dict[str, Any] = {"input_rows": len(rows), "data_rows": 0, "skipped_empty_rows": 0,
                             "device_count": 0, "physical_unit_count": 0, "multi_row_devices": 0,
                             "error_count": 0, "warning_count": 0, "columns": {"detected": {}, "ignored": []}}
    errors: List[Dict[str, Any]] = []
    warnings: List[Dict[str, Any]] = []

    def finish(devices: List[Dict[str, Any]]) -> PlanResult:
        errors.sort(key=lambda e: e["row"])
        warnings.sort(key=lambda w: w["row"])
        stats["device_count"] = len(devices)
        stats["physical_unit_count"] = sum(len(d["rows"]) for d in devices)
        stats["multi_row_devices"] = sum(1 for d in devices if len(d["rows"]) > 1)
        stats["error_count"] = len(errors)
        stats["warning_count"] = len(warnings)
        return {"devices": devices, "errors": errors, "warnings": warnings, "stats": stats}

    # ---------- 0. 空输入 ----------
    if not rows:
        warnings.append({"row": 0, "field": "table", "message": "输入为空：没有传入任何行，未解析到设备"})
        return finish([])

    # ---------- 1. 区分输入形态 ----------
    dict_mode = isinstance(rows[0], dict)
    list_mode = isinstance(rows[0], (list, tuple))
    if not dict_mode and not list_mode:
        raise TypeError("rows 的每个元素应为 dict（数据行）或 list（第一行为表头行）")
    if list_mode:
        header_cells = [(i, cell) for i, cell in enumerate(rows[0])]
        data_rows = rows[1:]
    else:
        seen_keys: Dict[Any, None] = {}
        for row in rows:
            if isinstance(row, dict):
                for k in row.keys():
                    seen_keys.setdefault(k, None)
        header_cells = [(k, k) for k in seen_keys]
        data_rows = rows

    # ---------- 2. 列名识别 ----------
    detected, detected_headers, ignored, dup_warnings = _detect_columns(header_cells)
    warnings.extend(dup_warnings)
    stats["columns"] = {"detected": detected_headers, "ignored": ignored}

    if not detected:
        errors.append({"row": 0, "field": "header",
                       "message": "无法识别表头：未找到任何已知列。支持的表头：序号/设备名称/设备位置/"
                                  "带外管理IP地址/带外管理地址网关/所属VLAN/设备型号，"
                                  "或英文 name/location/ip/gateway/vlan/model"})
        return finish([])
    if "ip" not in detected:
        headers_seen = "、".join(h for _, h in header_cells if _header_display(h))
        errors.append({"row": 0, "field": "header",
                       "message": f"缺少 IP 列：表头中未识别到“带外管理IP地址/IP”列"
                                  f"（现有表头：{headers_seen}），无法解析设备 IP"})
        return finish([])
    if "name" not in detected:
        warnings.append({"row": 0, "field": "header",
                         "message": "未识别到“设备名称”列，设备名称将全部为空，同设备名去重将失效"})

    reader = _make_cell_reader(detected, detected_headers)

    # ---------- 3. 逐行提取 + 合并单元格继承 ----------
    # 继承字段：name/ip/gateway/vlan（真实表里是跨行合并单元格，空=合并的产物）。
    # 不继承：location/model（每行一台物理机器，位置/型号按行独立）。
    # gateway 的 "/" 一族记号是"显式无网关"，要粘住（后续空行继承"无网关"，而不是
    # 继承到上一台设备的真实网关）；name/ip/vlan 的记号按"空"处理（向上继承）。
    records: List[Dict[str, Any]] = []
    carry: Dict[str, str] = {f: "" for f in _INHERIT_FIELDS}

    for offset, raw_row in enumerate(data_rows):
        row_no = offset + 1
        if not isinstance(raw_row, (dict, list, tuple)):
            errors.append({"row": row_no, "field": "table",
                           "message": f"第{row_no}行数据不是字典/列表，已跳过"})
            continue
        if dict_mode and not isinstance(raw_row, dict):
            errors.append({"row": row_no, "field": "table",
                           "message": f"第{row_no}行是列表，但输入为字典模式（每行应为 dict，键=表头），已跳过"})
            continue
        raw_texts = {field: reader(raw_row, field) for field in _ALL_FIELDS}
        # 整行都是空/"记号"（如整行都是 "/"）→ 空行，跳过且不参与继承
        if all(text == "" or _is_junk(text) for text in raw_texts.values()):
            stats["skipped_empty_rows"] += 1
            continue
        normalized: Dict[str, str] = {}
        for field, text in raw_texts.items():
            if text and _is_junk(text):
                # 网关列：记号 = "显式无网关"，要粘住（后续空行继承"无网关"，而不是继承到
                # 上一台设备的真实网关）；其余字段：记号当空处理（向上继承）。
                normalized[field] = "/" if field == "gateway" else ""
            else:
                normalized[field] = text

        rec: Dict[str, Any] = {
            "row": row_no,
            "seq": normalized["seq"] or None,
            "location": normalized["location"] or None,
            "model": normalized["model"] or None,
        }
        # 新设备边界：这一行显式写了与上方最近一次显式设备名不同的名字 = 新设备开始。
        # 该行没填的 IP/网关/VLAN 不再继承上一台设备的值（那是"新设备忘了填"，
        # 不是"合并单元格"），避免两台设备被解析成共用同一个 IP 的假冲突。
        if normalized["name"] and normalized["name"] != carry["name"]:
            carry["ip"] = carry["gateway"] = carry["vlan"] = ""
        for field in ("name", "ip", "vlan"):
            raw = normalized[field]
            if raw:
                rec[field] = raw
                rec[field + "_inherited"] = False
                carry[field] = raw
            else:
                rec[field] = carry[field]
                rec[field + "_inherited"] = bool(carry[field])
        gw = normalized["gateway"]
        if gw:
            rec["gateway"] = gw
            rec["gateway_inherited"] = False
            carry["gateway"] = gw
        else:
            rec["gateway"] = carry["gateway"]
            rec["gateway_inherited"] = bool(rec["gateway"])
        records.append(rec)

    stats["data_rows"] = len(records)

    if not records:
        warnings.append({"row": 0, "field": "table",
                         "message": "表头之后没有数据行（或所有数据行都是空行），未解析到设备"})

    # ---------- 3. 逐行校验"显式填写"的值（继承值已在来源行校验过，不重复报错） ----------
    for rec in records:
        n = rec["row"]
        if rec["ip"] and not rec["ip_inherited"]:
            parse = _parse_ip_cell(rec["ip"])
            rec["ip_parse"] = parse
            if not parse["ok"]:
                errors.append({"row": n, "field": "ip",
                               "message": f"第{n}行 IP“{rec['ip']}”无法解析：{parse['error']}"
                                          f"（应为 x.x.x.x/掩码，如 10.10.10.1/24）"})
            elif parse["bare"]:
                warnings.append({"row": n, "field": "ip",
                                 "message": f"第{n}行 IP {parse['ip']} 未携带掩码，无法计算网段，"
                                            f"网关同网段校验将跳过"})
        if rec["gateway"] and rec["gateway"] != "/" and not rec["gateway_inherited"]:
            state = _parse_gateway_value(rec["gateway"])
            if state["state"] == "invalid":
                errors.append({"row": n, "field": "gateway",
                               "message": f"第{n}行 网关“{rec['gateway']}”不是合法的 IP 地址"})
                rec["gateway_ip"] = None
            elif state["state"] == "zero":
                warnings.append({"row": n, "field": "gateway",
                                 "message": f"第{n}行 网关写为 0.0.0.0，按“无网关”处理"})
                rec["gateway"] = "/"
                rec["gateway_ip"] = None
            else:
                rec["gateway_ip"] = state["gw"]
        if rec["vlan"] and not rec["vlan_inherited"]:
            if not re.fullmatch(r"\d+", rec["vlan"]):
                errors.append({"row": n, "field": "vlan",
                               "message": f"第{n}行 VLAN“{rec['vlan']}”不是纯数字"})
            else:
                rec["vlan_int"] = int(rec["vlan"])
                if not 1 <= rec["vlan_int"] <= 4094:
                    warnings.append({"row": n, "field": "vlan",
                                     "message": f"第{n}行 VLAN {rec['vlan_int']} 超出常规范围 1-4094，请核对"})

    # ---------- 4. 连续行按 (设备名, IP) 聚合成逻辑设备 ----------
    groups: List[Dict[str, Any]] = []
    for rec in records:
        key = (rec["name"], rec["ip"])
        if groups and groups[-1]["key"] == key:
            groups[-1]["recs"].append(rec)
        else:
            groups.append({"key": key, "recs": [rec]})

    devices: List[Dict[str, Any]] = []
    for group in groups:
        recs = group["recs"]
        first = recs[0]
        name = first["name"] or None
        first_row = first["row"]
        rows_list = [r["row"] for r in recs]
        rows_str = "、".join(str(r) for r in rows_list)

        parse = _parse_ip_cell(first["ip"])
        if parse["ok"]:
            ip_val, prefix, netmask, network = parse["ip"], parse["prefix"], parse["netmask"], parse["network"]
        else:
            ip_val, prefix, netmask, network = (first["ip"] or None), None, None, None

        if not first["ip"]:
            errors.append({"row": first_row, "field": "ip",
                           "message": f"设备“{name or '（未命名）'}”（第{rows_str}行）缺少 IP 地址"})

        locations = [{"row": r["row"], "location": r["location"]} for r in recs]
        location = next((e["location"] for e in locations if e["location"]), None)
        model = next((r["model"] for r in recs if r["model"]), None)
        seq = next((r["seq"] for r in recs if r["seq"]), None)

        # 网关：取组内第一个非空取值（含继承）；"/" 一族 → None（无网关）。
        # 组级重新解析一次，兼容"网关从上一台设备继承过来"的取值（逐行校验只校验显式行）。
        gw_row, gateway, gateway_ip = None, None, None
        for r in recs:
            if r["gateway"]:
                gw_row = r["row"]
                text = r["gateway"]
                if text == "/" or _is_junk(text):  # "/" 一族 = 显式无网关
                    gateway = gateway_ip = None
                    break
                gw_state = _parse_gateway_value(text)
                if gw_state["state"] == "value":
                    gateway = gateway_ip = gw_state["gw"]
                elif gw_state["state"] == "zero":
                    gateway = gateway_ip = None
                else:  # invalid：显式行已报过格式错误，这里保底记原始文本
                    gateway, gateway_ip = text, None
                break

        # VLAN：取组内第一个非空取值（有效数字时转 int）
        vlan_rec = next((r for r in recs if r["vlan"]), None)
        vlan_val = None
        if vlan_rec is not None:
            if "vlan_int" in vlan_rec:
                vlan_val = vlan_rec["vlan_int"]
            elif vlan_rec["vlan"].isdigit():
                vlan_val = int(vlan_rec["vlan"])
            else:
                vlan_val = vlan_rec["vlan"]

        # —— 组内提醒/冲突（同一逻辑设备的多行）——
        for extra in recs[1:]:
            explicit_name = extra["name"] and not extra["name_inherited"]
            explicit_ip = extra["ip"] and not extra["ip_inherited"]
            if (explicit_name and extra["name"] == first["name"]) or \
               (explicit_ip and extra["ip"] == first["ip"]):
                warnings.append({"row": extra["row"], "field": "name",
                                 "message": f"第{extra['row']}行与第{first_row}行名称/IP 相同且均为显式填写，"
                                            f"已合并为同一设备“{name or '（未命名）'}”；"
                                            f"若实为两台独立设备，请为它们分别规划名称与 IP"})
                break
        if vlan_rec is not None:
            for r in recs:
                if r["vlan"] and r["row"] != vlan_rec["row"] and r["vlan"] != vlan_rec["vlan"]:
                    warnings.append({"row": r["row"], "field": "vlan",
                                     "message": f"同一设备“{name or '（未命名）'}”第{vlan_rec['row']}行与"
                                                f"第{r['row']}行 VLAN 不一致（{vlan_rec['vlan']} / {r['vlan']}），"
                                                f"以第{vlan_rec['row']}行为准"})
                    break
        gw_values = [(r["row"], r["gateway"]) for r in recs if r["gateway"]]
        if len({v for _, v in gw_values}) > 1:
            warnings.append({"row": gw_values[1][0], "field": "gateway",
                             "message": f"同一设备“{name or '（未命名）'}”第{gw_values[0][0]}行与"
                                        f"第{gw_values[1][0]}行网关不一致（“{gw_values[0][1]}” / "
                                        f"“{gw_values[1][1]}”），以第{gw_values[0][0]}行为准"})
        prefixes = [(r["row"], r["ip_parse"]["prefix"]) for r in recs
                    if r.get("ip_parse", {}).get("ok") and not r["ip_parse"]["bare"]]
        if len({p for _, p in prefixes}) > 1:
            warnings.append({"row": prefixes[1][0], "field": "ip",
                             "message": f"同一设备“{name or '（未命名）'}”第{prefixes[0][0]}行与"
                                        f"第{prefixes[1][0]}行掩码不一致（/{prefixes[0][1]} 与 /{prefixes[1][1]}），"
                                        f"以第{prefixes[0][0]}行为准"})
        models = [(r["row"], r["model"]) for r in recs if r["model"]]
        if len({m for _, m in models}) > 1:
            warnings.append({"row": models[1][0], "field": "model",
                             "message": f"同一设备“{name or '（未命名）'}”第{models[0][0]}行与"
                                        f"第{models[1][0]}行型号不一致（“{models[0][1]}” / "
                                        f"“{models[1][1]}”），同一逻辑设备的多行请核对"})

        # —— 网关同网段校验（"/" 或空 = 无网关，跳过）——
        if gateway_ip:
            try:
                gw_obj = ipaddress.ip_address(gateway_ip)
                if prefix is not None and network:
                    net = ipaddress.ip_network(network)
                    if gw_obj not in net:
                        errors.append({"row": gw_row or first_row, "field": "gateway",
                                       "message": f"网关 {gateway} 不在设备“{name or '（未命名）'}”所在网段"
                                                  f"（IP {ip_val}/{prefix}，网段 {network}，设备见第{first_row}行，"
                                                  f"网关取自第{gw_row}行）"})
                if ip_val and str(gw_obj) == str(ip_val):
                    warnings.append({"row": first_row, "field": "gateway",
                                     "message": f"第{first_row}行 设备“{name or '（未命名）'}”的网关与自身 IP "
                                                f"({ip_val}) 相同，请核对"})
            except ValueError:
                pass

        devices.append({
            "name": name, "location": location, "ip": ip_val, "prefix": prefix,
            "netmask": netmask, "network": network, "gateway": gateway,
            "vlan": vlan_val, "model": model, "row": first_row,
            "rows": rows_list, "locations": locations, "seq": seq,
            "physical_units": len(recs),
        })

    # ---------- 5. 跨设备冲突：IP 重复 / 设备名重复 ----------
    ip_first: Dict[str, int] = {}
    name_first: Dict[str, int] = {}
    combined_reported: set = set()
    for gi, dev in enumerate(devices):
        ip_norm = dev["ip"] if dev["prefix"] is not None else None
        name = dev["name"]
        if ip_norm:
            if ip_norm in ip_first:
                prev = devices[ip_first[ip_norm]]
                if name and name == prev["name"]:
                    errors.append({"row": dev["row"], "field": "name",
                                   "message": f"设备“{name}”（IP {ip_norm}）在第{prev['row']}行与"
                                              f"第{dev['row']}行重复出现；合并单元格继承后一条设备只应占一段"
                                              f"连续行，请检查是否重名或重复粘贴"})
                    combined_reported.add(gi)
                else:
                    errors.append({"row": dev["row"], "field": "ip",
                                   "message": f"IP {ip_norm} 重复：第{prev['row']}行"
                                              f"“{prev['name'] or '（未命名）'}”与第{dev['row']}行"
                                              f"“{name or '（未命名）'}”分配了同一个 IP"})
            else:
                ip_first[ip_norm] = gi
        if name and gi not in combined_reported:
            if name in name_first:
                prev = devices[name_first[name]]
                if prev["ip"] != dev["ip"] or prev["prefix"] is None or dev["prefix"] is None:
                    errors.append({"row": dev["row"], "field": "name",
                                   "message": f"设备名称“{name}”重复：同时出现在第{prev['row']}行与"
                                              f"第{dev['row']}行"})
            else:
                name_first[name] = gi

    # ---------- 6. 可选占用探测（默认关闭：occupancy_checker=None 时零网络） ----------
    if occupancy_checker is not None:
        findings = find_occupied_ips({"devices": devices}, occupancy_checker)
        stats["occupancy_probed"] = len(findings)
        stats["occupancy_occupied"] = sum(1 for f in findings if f["occupied"] is True)
        warnings.extend({"row": f["row"], "field": "ip", "message": f["message"]}
                        for f in findings if f["occupied"] is True)

    return finish(devices)


# --------------------------------------------------------------------------
# 可选：占用探测接口（默认关闭，本模块任何地方都不会主动调用）
# --------------------------------------------------------------------------
def find_occupied_ips(plan: PlanResult,
                      checker: Optional[Callable[[str], Optional[bool]]]) -> List[Dict[str, Any]]:
    """对 plan 里每个唯一 IP 调用 ``checker(ip)`` 做占用探测。

    :param checker: ``ip -> True(占用) / False(空闲) / None(未知)``。
        **传入 None 时直接返回空列表，绝不发起任何网络调用。**
    :return: 每个探测过的 IP 一条
        ``{"row", "field", "ip", "name", "occupied", "message"}``；
        接线方按 ``occupied is True`` 取"已被占用"的告警。
    """
    findings: List[Dict[str, Any]] = []
    if checker is None:
        return findings
    seen: set = set()
    for dev in plan.get("devices", []):
        ip = dev.get("ip")
        if not ip or ip in seen:
            continue
        seen.add(ip)
        row = dev.get("row")
        name = dev.get("name") or "（未命名）"
        base = {"row": row, "field": "ip", "ip": ip, "name": dev.get("name")}
        try:
            occupied = checker(ip)
        except Exception as exc:  # 探测器自身抛错不能影响解析结果
            findings.append({**base, "occupied": None,
                             "message": f"IP {ip}（第{row}行“{name}”）占用探测失败：{exc}"})
            continue
        if occupied is True:
            findings.append({**base, "occupied": True,
                             "message": f"IP {ip} 探测到占用（第{row}行“{name}”），"
                                        f"该地址可能已被使用，请勿重复分配"})
        elif occupied is False:
            findings.append({**base, "occupied": False,
                             "message": f"IP {ip} 探测空闲（第{row}行“{name}”）"})
        else:
            findings.append({**base, "occupied": None,
                             "message": f"IP {ip} 探测无结果，跳过占用判定（第{row}行“{name}”）"})
    return findings


def build_ping_checker(count: int = 1, timeout_ms: int = 1000) -> Callable[[str], Optional[bool]]:
    """生成标准库 subprocess ping 占用探测函数（**只有显式传给 parse_plan 才会执行**）。

    用法::

        result = parse_plan(rows, occupancy_checker=build_ping_checker())

    生产建议：二层探测更准——Linux 下可换成 ``arping -c1 -w1 <ip>``（需要 iputils；
    Windows 无自带 arping），或经交换机 CLI 查 ARP/MAC 表判断占用；
    也可以先探测再 arping 交叉确认。本函数不会被 parse_plan 默认调用。
    """
    import subprocess
    import sys

    def _check(ip: str) -> Optional[bool]:
        try:
            if sys.platform.startswith("win"):
                cmd = ["ping", "-n", str(max(1, count)), "-w", str(max(1, timeout_ms)), ip]
            else:
                cmd = ["ping", "-c", str(max(1, count)), "-W", str(max(1, timeout_ms // 1000)), ip]
            return subprocess.call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) == 0
        except OSError:
            return None

    return _check
