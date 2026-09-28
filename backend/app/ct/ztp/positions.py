"""ZTP 落位登记 + MAC 认领：纯函数工具（只依赖标准库）。

背景（决定这里的形状）：设备到货时运维手里只有「落位（机架/机柜/U位）+
规划管理 IP + 规划主机名」，**没有 MAC**（设备还没上电）。设备第一次上电向
DHCP 请求地址时，dnsmasq 的租约文件里就记录了它的 MAC。本模块负责：

  · norm_mac          —— 把各种写法的 MAC 归一成 aa:bb:cc:dd:ee:ff（认领/匹配的唯一键）；
  · parse_leases      —— 解析 dnsmasq 租约内容，"学"到上电设备的 MAC；
  · leases_candidates —— 候选租约文件路径（按优先级）；
  · read_leases       —— 读第一个可读的租约文件；**任何异常都吞掉并转成说明文字**，
                          这个接口给前端「待认领设备」用，租约读不到是常态不是故障，
                          绝不能 500；
  · parse_positions_csv —— 批量导入落位表（运维从 Excel/表格里整段粘贴）。
"""
from __future__ import annotations

import csv
import io
import ipaddress
import os


def norm_mac(mac) -> str:
    """把 aabb.ccdd.eeff / AA:BB:CC:DD:EE:FF / aabbccddeeff（以及 aa-bb-cc-dd-ee-ff）
    统一成小写冒号分隔 aa:bb:cc:dd:ee:ff；解析不了返回 ""。

    落位认领与租约匹配都以归一后的形状为唯一键 —— 归一只在这里做一次。
    """
    s = str(mac or "").strip().lower()
    if not s:
        return ""
    # 思科点分 aabb.ccdd.eeff：三组、每组 4 个十六进制位，先拼成连续 12 位
    if "." in s:
        parts = s.split(".")
        if len(parts) != 3 or any(len(x) != 4 for x in parts):
            return ""
        s = "".join(parts)
    cleaned = s.replace(":", "").replace("-", "")
    if len(cleaned) != 12:
        return ""
    if any(ch not in "0123456789abcdef" for ch in cleaned):
        return ""
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


def parse_leases(text) -> list:
    """解析 dnsmasq 租约文件内容。

    每行格式（dnsmasq 2.85 实测一致）：
        `<expiry_epoch> <mac> <ip> <hostname> [<client-id>]`
    hostname 为 `*` 表示没有主机名；`#` 开头是注释；第二列不是 MAC 的行
    （例如 DHCPv6 条目 / duid 行）norm_mac 解析不了 → 跳过。

    返回 [{"mac","ip","hostname","client_id","expires"}]，mac 已归一，
    expires 保留原始字符串。
    """
    out = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        mac = norm_mac(fields[1])
        if not mac:
            continue
        hostname = fields[3]
        out.append({
            "mac": mac,
            "ip": fields[2],
            "hostname": "" if hostname == "*" else hostname,
            "client_id": fields[4] if len(fields) > 4 else "",
            "expires": fields[0],
        })
    return out


def leases_candidates() -> list:
    """候选租约文件路径（按优先级，取第一个可读的）。

    1) OPS_DNSMASQ_LEASES 环境变量（容器部署时把宿主机租约文件挂进来后指到它）；
    2) /var/lib/dnsmasq/dnsmasq.leases —— 实测（dnsmasq 2.85，/proc/<pid>/fd 取证）
       Debian 系真实存在的路径；
    3) /var/lib/misc/dnsmasq.leases（老版本/其它发行版常见）；
    4) /var/lib/misc/dnsmasq/dnsmasq.leases（再兜一层）。
    """
    candidates = []
    env = (os.environ.get("OPS_DNSMASQ_LEASES") or "").strip()
    if env:
        candidates.append(env)
    candidates += [
        "/var/lib/dnsmasq/dnsmasq.leases",
        "/var/lib/misc/dnsmasq.leases",
        "/var/lib/misc/dnsmasq/dnsmasq.leases",
    ]
    return candidates


def read_leases() -> tuple:
    """读第一个**可读**的租约文件，返回 (路径, 说明, 记录列表)。

    说明里写清读了哪个路径；一个都读不到时返回
    ("", "读取不到 dnsmasq 租约文件（容器里需要把宿主机租约文件挂进来，
    或用 OPS_DNSMASQ_LEASES 指定路径）：<尝试过的路径>", [])。
    **任何异常都吞掉并转成说明文字**，这个接口绝不能 500。
    """
    tried = []
    for path in leases_candidates():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read()
        except Exception as e:  # noqa: BLE001
            # 权限不足 / 不存在 / 编码问题……统一转成说明文字，不让前端撞 500
            tried.append("%s（%s）" % (path, type(e).__name__))
            continue
        records = parse_leases(content)
        return path, "读取 dnsmasq 租约文件：%s（%d 条）" % (path, len(records)), records
    return "", (
        "读取不到 dnsmasq 租约文件（容器里需要把宿主机租约文件挂进来，"
        "或用 OPS_DNSMASQ_LEASES 指定路径）：" + "、".join(tried or leases_candidates())
    ), []


# 表头别名（小写比较；包含匹配，顺序即优先级 —— "mgmt_ip"/"管理ip" 必须排在 "ip" 前）
_FIELD_SYNONYMS = (
    ("position", ("position", "落位", "机位", "位置")),
    ("mgmt_ip", ("mgmt_ip", "管理ip", "管理地址", "规划ip", "ip")),
    ("hostname", ("hostname", "主机名", "主机")),
    ("serial", ("serial", "序列号", "sn")),
    ("mac", ("mac",)),
    ("remark", ("remark", "备注", "说明")),
)
# 无表头时的固定列顺序：落位, 管理IP, 主机名, 序列号, MAC, 备注
_FIELDS = ("position", "mgmt_ip", "hostname", "serial", "mac", "remark")


def _header_key(cell_lower: str):
    for key, names in _FIELD_SYNONYMS:
        for name in names:
            if name and name in cell_lower:
                return key
    return None


def parse_positions_csv(csv_text) -> tuple:
    """解析落位 CSV 文本，返回 (行列表, 错误列表)。

    · 表头可有可无：小写化后包含 `position` 或 `落位` 的第一行当表头（跳过），
      按表头名映射列（中英文别名都认，缺列补空）；没有表头时列顺序固定为
      落位, 管理IP, 主机名, 序列号, MAC, 备注；
    · 用 csv 标准库解析（支持引号/逗号转义）；忽略空行；每格 strip 首尾空白；
    · 校验：落位为空 → 报错跳过；管理 IP 用 ipaddress.ip_address() 校验并保留
      原字符串 → 非法则报错跳过；
    · 错误信息格式 `第N行: <原因>`，N 从 1 开始、含表头行号（按物理行计）。
    行列表里的每条记录带 `_line`（来源行号），供导入接口报"重复"类错误时定位。
    """
    rows, errors = [], []
    colmap = None  # None = 还没遇到表头（首条非空行决定用表头映射还是固定列序）
    for lineno, raw in enumerate(csv.reader(io.StringIO(str(csv_text or ""))), start=1):
        cells = [c.strip() for c in raw]
        if not any(cells):
            continue  # 忽略空行
        if colmap is None:
            lowered = [c.lower() for c in cells]
            if any(("position" in c) or ("落位" in c) for c in lowered):
                colmap = {}
                for i, c in enumerate(lowered):
                    key = _header_key(c)
                    if key and key not in colmap:
                        colmap[key] = i
                continue
            colmap = {k: i for i, k in enumerate(_FIELDS)}
        values = {k: cells[i] for k, i in colmap.items() if i < len(cells)}
        record = {k: (values.get(k) or "").strip() for k in _FIELDS}
        record["_line"] = lineno
        if not record["position"]:
            errors.append("第%d行: 落位为空" % lineno)
            continue
        try:
            ipaddress.ip_address(record["mgmt_ip"])
        except ValueError:
            errors.append("第%d行: 管理 IP 非法：%s" % (lineno, record["mgmt_ip"]))
            continue
        rows.append(record)
    return rows, errors
