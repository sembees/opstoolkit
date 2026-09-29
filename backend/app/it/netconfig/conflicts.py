# -*- coding: utf-8 -*-
"""跨模块一致性检查（H）：netconfig 的**静态 IP** 不能落在 PXE/ZTP 的 **DHCP 地址池**里。

为什么这是真事故：装机网段上的 DHCP 池是"谁先来谁拿走"。如果 netconfig 给某台机器
静态配了池内的某个地址，而 DHCP 又把同一个地址发给另一台正在装机的机器，
就会出现**两台机器同 IP** —— 表现为"时好时坏、偶发断网"，查起来非常费时。
两个模块各自看都没问题，**只有交叉看才发现**，所以这条检查放在这里。

刻意做成**警告**而不是硬失败：静态 IP 与 DHCP 池可能在**不同网段/不同现场**
（例如 netconfig 用在办公网、DHCP 池在单独的装机网），硬拦会把正常用法打死。
所以只把冲突如实报出来，由运维判断。
"""
from __future__ import annotations

import ipaddress


def collect_static_addresses(req) -> list:
    """从 netconfig 请求里收集 (标签, IPv4) 列表（只取 mode=static 且填了 ip 的）。

    用 getattr 泛化取字段：interfaces/bonds/vlans/bridges 四类对象的形状略有差别，
    但都带 `name` 与 `ip`。
    """
    out = []
    groups = (("接口", getattr(req, "interfaces", None)),
              ("bond", getattr(req, "bonds", None)),
              ("vlan", getattr(req, "vlans", None)),
              ("bridge", getattr(req, "bridges", None)))
    for kind, items in groups:
        for o in (items or []):
            ip = (getattr(o, "ip", "") or "").strip()
            if not ip:
                continue
            # ★ 外部审查 U4-F2（这条是我自己写出来的 bug）：bond 的 `mode` 是
            #   **聚合模式(0-6 的整数)**，对它调 `.lower()` 会抛 AttributeError，
            #   而被调用方的 `except Exception: return []` 吞掉 ⇒ **整份冲突提示都没了**。
            #   只有物理接口才有 static/dhcp 之分：bond/vlan/bridge 有 ip 就是静态。
            #   （另外一律用 str() 包一层，避免再被非字符串字段坑到。）
            if kind == "接口" and str(getattr(o, "mode", "static") or "static").lower() != "static":
                continue
            name = (getattr(o, "name", "") or "").strip() or "?"
            try:
                ipaddress.ip_address(ip)
            except ValueError:
                continue
            out.append(("%s %s" % (kind, name), ip))
    return out


def pool_conflicts(addrs, pools, max_warnings=20) -> list:
    """返回人类可读的中文警告列表（没有冲突就是空列表）。

    addrs: [(标签, IPv4)]；pools: [{"source": 来源说明, "start": ..., "end": ...}]。
    start/end 解析不了的池子**跳过**（宁可不报，也不误报）。

    **按池段去重**：真机上实测同时存在 20+ 个模板共用同一个地址池（192.168.199.100-200），
    逐个报会把提示刷成一屏废话。同一个 [start,end] 只报一次，把来源个数带上。
    """
    norm = {}
    for p in (pools or []):
        try:
            lo = ipaddress.ip_address(str(p.get("start", "")).strip())
            hi = ipaddress.ip_address(str(p.get("end", "")).strip())
        except ValueError:
            continue
        if int(hi) < int(lo):
            lo, hi = hi, lo
        key = (int(lo), int(hi))
        item = norm.setdefault(key, {"lo": lo, "hi": hi, "sources": []})
        src = str(p.get("source") or "DHCP 池")
        if src not in item["sources"]:
            item["sources"].append(src)

    warns = []
    for label, ip in (addrs or []):
        try:
            a = ipaddress.ip_address(ip)
        except ValueError:
            continue
        for (_lo, _hi), item in sorted(norm.items()):
            if int(item["lo"]) <= int(a) <= int(item["hi"]):
                srcs = item["sources"]
                shown = "、".join(srcs[:3]) + ("、等 %d 个模板" % len(srcs) if len(srcs) > 3 else "")
                warns.append(
                    "静态地址 %s（%s）落在 %s 的 DHCP 地址池 %s-%s 内："
                    "DHCP 可能把这个地址发给另一台正在装机的机器，造成两台机器同 IP。"
                    "请把该静态地址挪到池外，或缩小地址池。"
                    % (ip, label, shown, item["lo"], item["hi"])
                )
    return warns[:max_warnings]


def pools_from_pxe_profiles(profiles) -> list:
    """从 PXE 模板取地址池（池在 `net_config` 里；relay 模式没有池）。"""
    out = []
    for p in (profiles or []):
        nc = getattr(p, "net_config", None) or {}
        if str(nc.get("deploy_mode", "standalone")) == "relay":
            continue
        if nc.get("dhcp_start") and nc.get("dhcp_end"):
            out.append({"source": "PXE 模板 %s" % getattr(p, "name", "?"),
                        "start": nc.get("dhcp_start"), "end": nc.get("dhcp_end")})
    return out


def pools_from_ztp_templates(templates) -> list:
    """从 ZTP 模板取地址池（relay 模式同样没有池）。"""
    out = []
    for t in (templates or []):
        if str(getattr(t, "deploy_mode", "standalone")) == "relay":
            continue
        s, e = getattr(t, "dhcp_start", ""), getattr(t, "dhcp_end", "")
        if s and e:
            out.append({"source": "ZTP 模板 %s" % getattr(t, "name", "?"),
                        "start": s, "end": e})
    return out
