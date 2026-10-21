# -*- coding: utf-8 -*-
"""对账扩展到 MTU / VLAN / bond（M3 收尾）。

解析来源：
  · MTU      —— `ip -o link show`（形如 `2: eth0: <...> mtu 1500 qdisc ...`）
  · VLAN     —— `bridge vlan show`（`eth0  10 PVID Egress Untagged`）或
               `ip -d link`（`vlan protocol 802.1Q id 10`）
  · bond 模式 —— `/proc/net/bonding/bond0` 的 `Bonding Mode: ...` 行

判定口径与地址/路由一致：**规划里写了什么就必须在实际上出现什么**，否则算不一致。
规划里没写（空）的项不参与对账 —— 不然会给"没配 MTU 的机器"造出假告警。
"""
import re


def parse_links(probe_text: str) -> dict:
    """从 probe.txt 的 ip_link / bonding / bridge_vlan 段解析 mtu、bond、vlan。"""
    section = ""
    mtu, bond_mode, vlan_ids = {}, {}, set()
    for raw in (probe_text or "").splitlines():
        line = raw.rstrip()
        m = re.match(r"^==\s*(\S+)\s*==", line.strip())
        if m:
            section = m.group(1)
            continue
        if section == "ip_link":
            mm = re.match(r"^\s*\d+:\s+([^:@\s]+)[^:]*:.*\bmtu\s+(\d+)", line)
            if mm:
                mtu[mm.group(1)] = int(mm.group(2))
            mv = re.search(r"\bvlan protocol \S+ id (\d+)", line)
            if mv:
                vlan_ids.add(int(mv.group(1)))
        elif section == "bonding":
            if line.strip().startswith("---"):
                cur = line.strip().split("/")[-1]
                bond_mode.setdefault(cur, "")
            elif line.startswith("Bonding Mode:"):
                for k in list(bond_mode):
                    if not bond_mode[k]:
                        bond_mode[k] = line.split(":", 1)[1].strip()
        elif section == "bridge_vlan":
            for mm in re.finditer(r"\bvlan\s+(\d+)", line):
                vlan_ids.add(int(mm.group(1)))
    return {"mtu": mtu, "bond_mode": bond_mode, "vlan_ids": sorted(vlan_ids)}


def diff_links(expected: dict, parsed: dict) -> dict:
    items, ok = [], True

    for want in (expected or {}).get("mtu") or []:
        iface, val = want.get("iface"), want.get("mtu")
        actual = (parsed.get("mtu") or {}).get(iface)
        hit = actual == val
        items.append({"item": "mtu", "expected": "%s mtu %s" % (iface, val),
                      "actual": ("%s mtu %s" % (iface, actual)) if actual else "未采集到",
                      "ok": hit})
        ok = ok and hit

    for want in (expected or {}).get("bond_mode") or []:
        iface, val = want.get("iface"), (want.get("mode") or "").lower()
        actual = ((parsed.get("bond_mode") or {}).get(iface) or "").lower()
        # 设备/内核的措辞差异大（IEEE 802.3ad Dynamic link aggregation vs 802.3ad），做包含匹配
        hit = bool(actual) and (val in actual or actual in val)
        items.append({"item": "bond_mode", "expected": "%s %s" % (iface, val),
                      "actual": actual or "未采集到", "ok": hit})
        ok = ok and hit

    for want in (expected or {}).get("vlan_ids") or []:
        hit = want in (parsed.get("vlan_ids") or [])
        items.append({"item": "vlan", "expected": want,
                      "actual": parsed.get("vlan_ids") or [], "ok": hit})
        ok = ok and hit

    return {"ok": ok, "items": items,
            "counts": {"total": len(items), "failed": len([i for i in items if not i["ok"]])}}
