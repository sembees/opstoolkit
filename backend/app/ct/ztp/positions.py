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


# 表头别名（小写比较；顺序即优先级 —— "mgmt_ip"/"管理ip" 必须排在 "ip" 前）
_FIELD_SYNONYMS = (
    ("position", ("position", "落位", "机位", "位置")),
    ("mgmt_ip", ("mgmt_ip", "management_ip", "管理ip", "管理地址", "规划ip", "ip")),
    ("hostname", ("hostname", "主机名", "主机")),
    ("serial", ("serial", "序列号", "sn")),
    ("mac", ("mac",)),
    ("remark", ("remark", "备注", "说明")),
)
# 无表头时的固定列顺序：落位, 管理IP, 主机名, 序列号, MAC, 备注
_FIELDS = ("position", "mgmt_ip", "hostname", "serial", "mac", "remark")

# 表头判定用的"落位"线索：**必须与 position 的别名同一套**。
# 改前只认 "position"/"落位"，于是表头写成「机位,…」或「位置,…」时不认表头 ⇒
# 整表按固定列序解析（列序一变就错位），且表头行本身被当成数据、报一条误导性的
# 「管理 IP 非法：管理IP」。
_HEADER_HINTS = ("position", "落位", "机位", "位置")

# 带外管理/其它协议的列名：里面恰好含 `ip` / `sn` 这类**短**别名，
# 但绝不能当成管理 IP / 序列号（外部审查 U3-2nd-F10 举的例子：`IPMI地址`、`SNMP社区`）。
_HEADER_DECOYS = ("ipmi", "bmc", "ilo", "idrac", "snmp", "带外", "console", "控制台", "串口")


def _norm_cell(cell: str) -> str:
    """表头单元格归一：去首尾空白、去空格/下划线/连字符/全角空格，转小写。"""
    s = (cell or "").strip().lower()
    for ch in (" ", "_", "-", "　", "\t"):
        s = s.replace(ch, "")
    return s


def _is_ascii_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _header_key(cell_lower: str):
    """表头单元格 → 字段名；认不出返回 None（外部审查 U3-2nd-F10）。

    匹配顺序：
      1. **归一后精确相等**（`管理ip` / `管理 IP` / `管理_ip` 都算）；
      2. 否则子串匹配，但要过两道闸：
         · 单元格不能是带外/其它协议的列（见 `_HEADER_DECOYS`）；
         · 别名长度 ≥ 4 直接认；**长度 ≤ 3 的短别名**（`ip`/`sn`/`mac`）必须被
           非字母数字边界围住 —— `IP地址` 认、`IPMI地址` 不认、`SN号` 认、`SNMP社区` 不认。
      3. 多个别名命中时取**最长**的那个（`管理ip` 优于 `ip`）。
    改前只做"按别名表顺序、任意子串"匹配，于是 `IPMI地址` 命中 `ip` 变成管理 IP、
    `SNMP社区` 命中 `sn` 变成序列号，而真正的「管理IP」列因为 key 已被占用被丢弃 ——
    导入一批张冠李戴的数据，接口还报成功。
    """
    cell = (cell_lower or "").strip()
    if not cell:
        return None
    norm = _norm_cell(cell)
    if not norm:
        return None
    for key, names in _FIELD_SYNONYMS:
        for name in names:
            if _norm_cell(name) == norm:
                return key
    if any(d in cell for d in _HEADER_DECOYS):
        return None
    best = None   # (别名长度, key)
    for key, names in _FIELD_SYNONYMS:
        for name in names:
            alias = _norm_cell(name)
            if not alias or alias not in norm:
                continue
            if len(alias) <= 3:
                pos = norm.index(alias)
                before = norm[pos - 1] if pos > 0 else ""
                after = norm[pos + len(alias)] if pos + len(alias) < len(norm) else ""
                if _is_ascii_alnum(after) or (before and _is_ascii_alnum(before)):
                    continue      # 短别名必须整词出现（前后都不是字母数字）
            if best is None or len(alias) > best[0]:
                best = (len(alias), key)
    return best[1] if best else None


def parse_positions_csv(csv_text) -> tuple:
    """解析落位 CSV 文本，返回 (行列表, 错误列表)。

    · 表头可有可无：任一个格子带 `position`/`落位`/`机位`/`位置` 线索，或这一行有 ≥2 个
      格子能认出字段名（中英文别名都认，大小写/空格/下划线不敏感）时，第一行当表头（跳过），
      按表头名映射列（缺列补空）；没有表头时列顺序固定为 落位, 管理IP, 主机名, 序列号, MAC, 备注；
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
            keys = [_header_key(c) for c in lowered]
            # 表头判定（外部审查 U3-2nd-F10）：
            #   · 任一个格子带"落位"线索（position/落位/机位/位置）→ 是表头；
            #   · 或者这一行有 ≥2 个格子能认出字段名（例如只有 管理IP/主机名/MAC 三列、
            #     没有落位列的表头也能认出来）。
            # 数据行不会误判：落位编码（A01-03-U12）、IP、主机名、序列号、MAC 里
            # 都认不出字段名（`SN012` 的 `sn` 后面紧跟数字、`10.0.0.12` 里没有整词的 `ip`）。
            if (any(h in c for c in lowered for h in _HEADER_HINTS)
                    or sum(1 for k in keys if k) >= 2):
                colmap = {}
                for i, key in enumerate(keys):
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
