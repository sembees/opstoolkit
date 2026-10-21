# -*- coding: utf-8 -*-
"""CSV 批量导入规划（几十台时逐条点太慢）。

列名同时接受中英文（现场表格八成是中文表头）：
  serial/SN、rack/机架、hostname/主机名、mgmt_ip/管理IP、os_type/系统、fmt/格式、
  install_id/装机记录、mac、serial_trusted/SN可信、iface/网卡、ip、cidr/掩码、
  gateway/网关、dns（分号或逗号分隔）

语义：
  · 一行 = 一台设备 + 一块网卡（足够覆盖"给机器配管理 IP"的主场景；bond/VLAN 仍可在界面补）；
  · 逐行隔离：某行错了只跳过该行并返回原因，不整批失败；
  · 与逐条创建**同一套校验**（SN 主键唯一、SN 不可信退 MAC、install_id 唯一），
    所以导入不会绕开任何闸门。
"""
import csv
import io

ALIASES = {
    "serial": ("serial", "sn", "机箱sn", "序列号", "sn号"),
    "rack": ("rack", "机架", "机架位置", "位置"),
    "hostname": ("hostname", "主机名", "规划主机名"),
    "mgmt_ip": ("mgmt_ip", "管理ip", "管理地址"),
    "os_type": ("os_type", "系统", "os"),
    "fmt": ("fmt", "格式", "后端"),
    "install_id": ("install_id", "装机记录", "装机记录id"),
    "mac": ("mac", "mac地址"),
    "serial_trusted": ("serial_trusted", "sn可信", "sntrusted"),
    "iface": ("iface", "网卡", "网卡名", "接口名"),
    "ip": ("ip", "ipv4", "地址"),
    "cidr": ("cidr", "掩码", "prefix"),
    "gateway": ("gateway", "网关"),
    "dns": ("dns", "dns服务器"),
    "kind": ("kind", "类型", "网卡类型"),
    "routes": ("routes", "路由", "路由表", "明细路由"),
}

_TRUE = ("1", "true", "yes", "y", "是", "可信", "")


def parse_csv(text: str) -> list[dict]:
    """把 CSV 解析成"规范化的行"（未知列忽略；表头大小写/空格不敏感）。"""
    if not (text or "").strip():
        raise ValueError("CSV 内容为空")
    # 兼容 BOM 与 Excel 导出的 \r\n
    raw = text.lstrip("\ufeff")
    reader = csv.DictReader(io.StringIO(raw))
    if not reader.fieldnames:
        raise ValueError("CSV 没有表头")
    mapping = {}
    for col in reader.fieldnames:
        norm = (col or "").strip().lower().replace(" ", "").replace("_", "")
        for field, names in ALIASES.items():
            if any(norm == n.replace("_", "") for n in names):
                mapping[col] = field
                break
    if "serial" not in mapping.values() and "mac" not in mapping.values():
        raise ValueError("CSV 至少要有一列 SN（serial/SN）或 MAC —— 否则无法确定业务键")
    if "ip" not in mapping.values():
        raise ValueError("CSV 至少要有一列 IP（ip/地址）—— 否则没有可下发的配置")

    rows = []
    for raw_row in reader:
        row = {}
        for col, val in raw_row.items():
            field = mapping.get(col)
            if field:
                row[field] = (val or "").strip()
        if any(row.values()):
            rows.append(row)
    if not rows:
        raise ValueError("CSV 里没有数据行")
    return rows


def row_to_body(row: dict) -> dict:
    """把一行 CSV 转成 create_plan 的入参（复用同一套校验）。"""
    trusted = (row.get("serial_trusted") or "").strip().lower()
    serial_trusted = trusted in _TRUE or trusted == ""
    dns = [d.strip() for d in (row.get("dns") or "").replace(";", ",").split(",") if d.strip()]
    routes = []
    for part in (row.get("routes") or "").replace("；", ";").split(";"):
        part = part.strip()
        if not part:
            continue
        if ">" in part:
            to, via = part.split(">", 1)
        elif "," in part:
            to, via = part.split(",", 1)
        else:
            to, via = part, ""
        routes.append({"to": to.strip(), "via": (via.strip() or None)})
    kind = (row.get("kind") or "interface").strip().lower() or "interface"
    if kind not in ("interface", "bond", "vlan", "bridge"):
        kind = "interface"
    iface = {
        "kind": kind,
        "name": (row.get("iface") or "eth0").strip() or "eth0",
        "spec": {
            "name": (row.get("iface") or "eth0").strip() or "eth0",
            "mode": "static",
            "ip": (row.get("ip") or "").strip(),
            "cidr": int(row["cidr"]) if (row.get("cidr") or "").strip().isdigit() else 24,
            "gateway": (row.get("gateway") or "").strip(),
            "dns": dns,
            "routes": routes,
        },
    }
    body = {
        "install_id": (row.get("install_id") or "").strip() or None,
        "serial": (row.get("serial") or "").strip(),
        "serial_trusted": serial_trusted,
        "key_source": "serial" if serial_trusted else "mac",
        "mac": (row.get("mac") or "").strip(),
        "rack": (row.get("rack") or "").strip(),
        "planned_hostname": (row.get("hostname") or "").strip(),
        "mgmt_ip": (row.get("mgmt_ip") or "").strip(),
        "os_type": ((row.get("os_type") or "ubuntu").strip().lower() or "ubuntu"),
        "fmt": (row.get("fmt") or "").strip(),
        "interfaces": [iface],
    }
    # 说明：CSV 导入允许"既没有 install_id 也没有 ztp_device_id"——现场常常是先按
    # SN + 规划 IP 登记，等装机/开局记录出来再关联；apply 用 mgmt_ip 作目标即可。
    return body
