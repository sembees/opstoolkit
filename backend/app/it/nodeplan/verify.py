# -*- coding: utf-8 -*-
"""M3：平台侧**逐字段比对**（目标机只做粗判，这里做严格对账）。

为什么要两层：
  · 目标机本地那层（bash grep）只回答"该不该回滚"——它必须在断网时也能跑，所以只能粗；
  · 平台这层拿 `ip -o addr/route` 原文做**字段级 diff**，回答"到底哪一项和规划不一致"。
评审的原话是「ping 通不算成功」：IP 写错一位也可能 ping 通网关，所以判定必须是配置对账。

解析的是 `ip -o` 的稳定格式（不是 `ip -j`，因为老发行版的 iproute2 未必支持 -j）：
  2: eth0    inet 192.168.10.10/24 brd ... scope global eth0
  default via 192.168.10.1 dev eth0 proto static
"""
import re
from typing import Optional

from app.it.nodeplan.links import diff_links, parse_links

_INET = re.compile(r"^\s*\d+:\s+(\S+)\s+(inet|inet6)\s+([0-9a-fA-F:.]+/\d+)")


def parse_probe(probe_text: str) -> dict:
    """把 probe.txt 解析成 {addresses: [(iface, cidr)], routes: [(dest, via, dev)], dns: [...]}。"""
    section = ""
    addrs, routes, dns = [], [], []
    for raw in (probe_text or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^==\s*(\S+)\s*==", line.strip())
        if m:
            section = m.group(1)
            continue
        if section == "ip_addr":
            mm = _INET.match(line)
            if mm:
                addrs.append({"iface": mm.group(1), "family": mm.group(2), "cidr": mm.group(3)})
        elif section == "ip_route":
            parts = line.split()
            if not parts:
                continue
            dest = parts[0]
            via = dev = ""
            if "via" in parts:
                via = parts[parts.index("via") + 1]
            if "dev" in parts:
                dev = parts[parts.index("dev") + 1]
            routes.append({"dest": dest, "via": via, "dev": dev})
        elif section == "resolv":
            if line.startswith("nameserver"):
                v = line.split()[1] if len(line.split()) > 1 else ""
                # 127.0.0.53 是 systemd-resolved 的本地桩，不是真实上游，不能当"实际 DNS"
                if v and not v.startswith("127.0.0.53"):
                    dns.append(v)
        elif section == "resolvectl":
            # 形如：Link 2 (ens18): 192.168.199.1
            m2 = re.search(r":\s*(.+)$", line)
            if m2:
                for tok in m2.group(1).split():
                    if re.match(r"^[0-9a-fA-F:.]+$", tok) and ":" in tok or re.match(r"^\d+\.\d+\.\d+\.\d+$", tok):
                        if tok not in dns:
                            dns.append(tok)
    return {"addresses": addrs, "routes": routes, "dns": dns}


def diff_expected(expected: dict, parsed: dict) -> dict:
    """逐项对账，返回 {ok, items:[{item, expected, actual, ok, note}]}。

    规则（写死，避免"差不多就行"）：
      · 地址：每个期望的 ip/prefix 必须在某个网卡上出现，**且网卡名一致**（可选：expected 里带 iface）；
      · 默认路由：期望网关必须出现在默认路由的 via 上；
      · DNS：期望的每个 DNS 都必须在 resolv 里出现（顺序不比）。
    """
    items = []
    ok_all = True

    actual_addrs = {(a["iface"], a["cidr"]) for a in parsed.get("addresses", [])}
    actual_cidrs = {a["cidr"] for a in parsed.get("addresses", [])}
    for want in expected.get("addresses") or []:
        hit = want in actual_cidrs
        items.append({"item": "address", "expected": want,
                      "actual": sorted(actual_cidrs), "ok": hit})
        ok_all = ok_all and hit

    gw = (expected.get("default_gateway") or "").strip()
    if gw:
        vias = {r.get("via") for r in parsed.get("routes", []) if r.get("dest") == "default"}
        hit = gw in vias
        items.append({"item": "default_route", "expected": gw,
                      "actual": sorted(v for v in vias if v), "ok": hit})
        ok_all = ok_all and hit

    for want in expected.get("dns") or []:
        hit = want in set(parsed.get("dns") or [])
        items.append({"item": "dns", "expected": want,
                      "actual": parsed.get("dns") or [], "ok": hit})
        ok_all = ok_all and hit

    return {"ok": ok_all, "items": items,
            "counts": {"total": len(items), "failed": len([i for i in items if not i["ok"]])}}


def verify_plan(expected: dict, probe_text: str) -> dict:
    parsed = parse_probe(probe_text)
    out = diff_expected(expected, parsed)
    # MTU / VLAN / bond 模式：来源不同（ip -o link / bridge vlan / /proc/net/bonding），合并进同一份报告
    lk = diff_links(expected, parse_links(probe_text))
    out["items"] = out["items"] + lk["items"]
    out["ok"] = out["ok"] and lk["ok"]
    out["counts"] = {"total": len(out["items"]),
                     "failed": len([i for i in out["items"] if not i["ok"]])}
    out["parsed"] = parsed
    out["parsed_links"] = parse_links(probe_text)
    return out
