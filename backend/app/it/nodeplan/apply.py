# -*- coding: utf-8 -*-
"""M2：把"下发 + 目标机自愈"拆成可离线验证的脚本包。

为什么是这套设计（评审给出的死穴，必须正面解决）：
  · **改 IP 的瞬间 SSH 就断**，放在 /tmp 的回滚脚本永远跑不到 → 不能"平台连着机器等结果"；
  · 正确做法：平台只把三件套 + 一个一次性 systemd timer 布置到目标机，**由目标机本地**
    在 90 秒后自己"应用 → 探测 → 失败回滚"，结果写成文件（并可回传）；
  · 平台 90 秒后按**新 IP**回连取结果；连不上再按旧 IP 取（说明已回滚）。

所有脚本都支持 `OPSTK_ROOT`（默认空 = 真机根）与 `OPSTK_STATE`（默认 /var/lib/opstk/netconfig/<cid>），
因此可以在本机用 **shim 桩**（假的 netplan/nmcli/ip）把"成功 / 失败回滚"两条路径真跑一遍。

本模块**只生成文本**；SSH 布置在 api 层做。
"""
import hashlib
import json
import shlex

STATE_DIR = "/var/lib/opstk/netconfig"
TIMER_DELAY_SEC = 90          # 给网络服务重新配置留的窗口（评审：不要拍脑袋 sleep 30）
PROBE_INTERVAL_SEC = 5
PROBE_TIMEOUT_SEC = 120


def build_expected(os_type: str, ifaces: list[dict]) -> dict:
    """把规划翻译成"期望状态"。

    只收能机械判定的项：地址、默认网关、DNS。bond/vlan 细节留给平台侧 diff
    （输出格式各发行版不一致，本地粗判只负责"该不该回滚"）。
    """
    addrs, dns, gw, mtu, bond, vlans = [], [], "", [], [], []
    for it in ifaces:
        spec0 = it.get("spec") or {}
        if it.get("kind") == "interface" and spec0.get("mtu"):
            try:
                mtu.append({"iface": str(spec0.get("name") or ""), "mtu": int(spec0["mtu"])})
            except (TypeError, ValueError):
                pass
        if it.get("kind") == "bond" and spec0.get("mode"):
            bond.append({"iface": str(spec0.get("name") or ""), "mode": str(spec0["mode"])})
        if it.get("kind") == "vlan" and spec0.get("id"):
            try:
                vlans.append(int(spec0["id"]))
            except (TypeError, ValueError):
                pass
    for it in ifaces:
        if it.get("kind") != "interface":
            continue
        spec = it.get("spec") or {}
        ip = str(spec.get("ip") or "").strip()
        cidr = spec.get("cidr")
        if ip and cidr:
            addrs.append("%s/%s" % (ip, cidr))
        for d in (spec.get("dns") or []):
            d = str(d).strip()
            if d and d not in dns:
                dns.append(d)
        if not gw and spec.get("gateway"):
            gw = str(spec["gateway"]).strip()
    return {"addresses": addrs, "default_gateway": gw, "dns": dns, "os_type": os_type,
            "mtu": mtu, "bond_mode": bond, "vlan_ids": vlans}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_TEMPLATES = {
    "probe": """#!/bin/bash
# 采集"实际状态"（原样落文件，供平台侧逐字段 diff；目标机本地只做粗判）
set -u
ROOT="${OPSTK_ROOT:-}"
STATE="${OPSTK_STATE:-{{STATE}}/{{CID}}}"
mkdir -p "$STATE"
{
  echo "== ip_addr =="
  ip -o addr show 2>/dev/null || true
  echo "== ip_link =="
  ip -o link show 2>/dev/null || true
  echo "== ip_route =="
  ip -o route show 2>/dev/null || true
  echo "== resolv =="
  # 注意：Ubuntu 22.04 默认 systemd-resolved，/etc/resolv.conf 是桩（127.0.0.53）。
  # 真实上游 DNS 必须从 resolvectl 取，否则"DNS 对不上"会是假告警（真机踩过）。
  cat "$ROOT/etc/resolv.conf" 2>/dev/null || true
  echo "== resolvectl =="
  resolvectl dns 2>/dev/null || true
  echo "== bonding =="
  for b in "$ROOT"/proc/net/bonding/*; do [ -e "$b" ] && { echo "--- $b"; cat "$b"; }; done 2>/dev/null || true
  echo "== bridge_vlan =="
  bridge -j vlan show 2>/dev/null || bridge vlan show 2>/dev/null || true
} > "$STATE/probe.txt" 2>&1
echo "probe written: $STATE/probe.txt"
""",
    "check": """#!/bin/bash
# 粗判（目标机本地）：期望的每个地址都在、默认路由网关对。
# 严格 diff 在平台侧（读 probe.txt 逐字段比对）；本地这层只决定"该不该回滚"。
set -u
STATE="${OPSTK_STATE:-{{STATE}}/{{CID}}}"
PROBE="$STATE/probe.txt"
fail=0
{{ADDR_CHECKS}}
GW="{{GW}}"
if [ -n "$GW" ] && ! grep -q "via $GW" "$PROBE"; then
  echo "MISSING_ROUTE $GW"; fail=1
fi
if [ "$fail" -eq 0 ]; then
  echo "CHECK_OK {{EXPECTED_HASH}}"
  exit 0
fi
echo "CHECK_FAILED"
exit 1
""",
    "apply": """#!/bin/bash
# 应用新配置（目标机本地，由 timer 触发；不依赖任何 SSH 会话）
set -u
ROOT="${OPSTK_ROOT:-}"
STATE="${OPSTK_STATE:-{{STATE}}/{{CID}}}"
mkdir -p "$STATE/backup"
echo "[1/4] 备份当前配置"
cp -a "$ROOT/etc/netplan" "$STATE/backup/netplan" 2>/dev/null || true
cp -a "$ROOT/etc/sysconfig/network-scripts" "$STATE/backup/ifcfg" 2>/dev/null || true
echo "[2/4] 写入新配置"
{{WRITE_CONFIG}}
echo "[3/4] 应用"
{{APPLY_CMD}} || { echo "APPLY_FAILED"; exit 2; }
echo "[4/4] 采集并检查"
bash "$STATE/probe.sh"
bash "$STATE/check.sh" || { echo "CHECK_FAILED_AFTER_APPLY"; exit 3; }
echo "APPLY_OK"
""",
    "rollback": """#!/bin/bash
# 失败回滚：恢复备份并重新应用（同样在目标机本地跑）
set -u
ROOT="${OPSTK_ROOT:-}"
STATE="${OPSTK_STATE:-{{STATE}}/{{CID}}}"
echo "[rollback] 恢复 netplan"
if [ -d "$STATE/backup/netplan" ]; then
  rm -rf "$ROOT/etc/netplan"
  cp -a "$STATE/backup/netplan" "$ROOT/etc/netplan" 2>/dev/null || true
fi
echo "[rollback] 恢复 ifcfg"
if [ -d "$STATE/backup/ifcfg" ]; then
  cp -a "$STATE/backup/ifcfg/." "$ROOT/etc/sysconfig/network-scripts/" 2>/dev/null || true
fi
echo "[rollback] 重新应用"
{{APPLY_CMD}} || true
echo "ROLLBACK_DONE"
""",
    "run": """#!/bin/bash
# 一次性执行：应用 → 检查 → 失败回滚 → 写结果（并尽力回传）
set -u
STATE="${OPSTK_STATE:-{{STATE}}/{{CID}}}"
mkdir -p "$STATE"
if bash "$STATE/apply.sh" > "$STATE/apply.log" 2>&1; then
  RESULT=applied
else
  bash "$STATE/rollback.sh" >> "$STATE/apply.log" 2>&1 || true
  bash "$STATE/probe.sh" >> "$STATE/apply.log" 2>&1 || true
  RESULT=rolled_back
fi
cat > "$STATE/result.json" <<EOF
{"result": "$RESULT", "change_id": "{{CID}}", "expected_hash": "{{EXPECTED_HASH}}", "finished_at": "$(date -Is)"}
EOF
{{CALLBACK}}
echo "RESULT=$RESULT"
""",
}


def _fill(text: str, change_id: str, extra: dict) -> str:
    out = text.replace("{{STATE}}", STATE_DIR).replace("{{CID}}", change_id)
    for k, v in extra.items():
        out = out.replace("{{%s}}" % k, v)
    return out


def render_bundle(*, change_id: str, os_type: str, fmt: str, config_name: str,
                  config_text: str, expected: dict, callback_url: str = "") -> dict:
    """生成目标机上的整套文件（纯文本，便于审计与离线测试）。"""
    ex = json.dumps(expected, ensure_ascii=False, sort_keys=True)
    ex_hash = _sha(ex)

    # 粗判的地址检查（每个期望地址一条 grep）
    checks = []
    for a in expected.get("addresses") or []:
        checks.append('if ! grep -q "%s" "$PROBE"; then echo "MISSING_ADDR %s"; fail=1; fi'
                      % (a, a))
    addr_checks = "\n".join(checks) or ":"

    # 配置落点与应用命令（按后端）
    if fmt == "netplan":
        write_cfg = 'mkdir -p "$ROOT/etc/netplan" && cp "$STATE/config.yaml" "$ROOT/etc/netplan/99-opstk.yaml"'
        apply_cmd = "netplan apply"
    elif fmt == "ifcfg":
        write_cfg = ('mkdir -p "$ROOT/etc/sysconfig/network-scripts" && '
                     'cp "$STATE/ifcfg.txt" "$ROOT/etc/sysconfig/network-scripts/ifcfg-$(head -1 "$STATE/ifcfg.txt" | sed "s/^#//")"')
        apply_cmd = "nmcli connection reload && nmcli connection up \"$(nmcli -t -f NAME,DEVICE connection show | head -1 | cut -d: -f1)\""
    else:                                  # nmcli：直接执行生成的脚本
        write_cfg = "true"
        apply_cmd = 'bash "$STATE/config.sh"'

    extra = {
        "EXPECTED_HASH": ex_hash, "ADDR_CHECKS": addr_checks,
        "GW": expected.get("default_gateway") or "",
        "WRITE_CONFIG": write_cfg, "APPLY_CMD": apply_cmd,
        "CALLBACK": ("curl -s -m 10 -X POST " + shlex.quote(callback_url)
                     + " -H 'Content-Type: application/json' --data @\"$STATE/result.json\" "
                       ">/dev/null 2>&1 || true") if callback_url else "true",
    }

    cfg_key = {"netplan": "config.yaml", "nmcli": "config.sh", "ifcfg": "ifcfg.txt"}.get(fmt, "config.txt")
    files = {name + ".sh": _fill(tpl, change_id, extra) for name, tpl in _TEMPLATES.items()}
    files[cfg_key] = config_text

    unit = ("[Unit]\nDescription=OpsToolkit netconfig apply (%(cid)s)\nAfter=network.target\n\n"
            "[Service]\nType=oneshot\nEnvironment=OPSTK_STATE=%(state)s/%(cid)s\n"
            "ExecStart=/bin/bash %(state)s/%(cid)s/run.sh\n"
            % {"cid": change_id, "state": STATE_DIR})
    timer = ("[Unit]\nDescription=OpsToolkit netconfig apply timer (%(cid)s)\n\n"
             "[Timer]\nOnActiveSec=%(delay)d\nAccuracySec=1s\n"
             "Unit=opstk-netconfig-apply@%(cid)s.service\n\n[Install]\nWantedBy=timers.target\n"
             % {"cid": change_id, "delay": TIMER_DELAY_SEC})
    files["opstk-netconfig-apply@%s.service" % change_id] = unit
    files["opstk-netconfig-apply@%s.timer" % change_id] = timer

    manifest = {"change_id": change_id, "expected": expected, "expected_hash": ex_hash,
                "config_name": cfg_key, "format": fmt, "os_type": os_type,
                "timer_delay_sec": TIMER_DELAY_SEC, "files": sorted(files)}
    files["manifest.json"] = json.dumps(manifest, ensure_ascii=False, indent=2)
    return {"change_id": change_id, "expected_hash": ex_hash, "files": files, "manifest": manifest}
