#!/usr/bin/env python3
"""OpsToolkit NetConfig 完整集成测试。
覆盖: 3种格式 x 全部类型 x 边界情况 + meta API + 前端页面
用法: TEST_BASE=http://127.0.0.1:8000 python scripts/test_netconfig_full.py
"""
import urllib.request, json, sys, os, traceback

BASE = os.environ.get("TEST_BASE", "http://127.0.0.1:8000")
API = f"{BASE}/api/it/netconfig/generate"
META = f"{BASE}/api/it/netconfig/meta"
AUTH_URL = f"{BASE}/api/auth/login"
USER = os.environ.get("TEST_USER", "admin")
PASS = os.environ.get("TEST_PASS", "admin@123")

PASS_CNT = 0
FAIL_CNT = 0

def check(name, ok, detail=""):
    global PASS_CNT, FAIL_CNT
    if ok:
        PASS_CNT += 1
        print(f"  [PASS] {name}")
    else:
        FAIL_CNT += 1
        print(f"  [FAIL] {name}  --  {detail}")

def login():
    data = json.dumps({"username": USER, "password": PASS}).encode()
    req = urllib.request.Request(AUTH_URL, data, {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]

TOKEN = login()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

def gen(payload):
    req = urllib.request.Request(API, json.dumps(payload).encode(), HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

def http_get(path):
    req = urllib.request.Request(f"{BASE}{path}", headers={"Authorization": f"Bearer {TOKEN}"})
    resp = urllib.request.urlopen(req, timeout=10)
    return resp.status, resp.read().decode()

# ===== 1. Meta API =====
print("\n=== 1. Meta API ===")
try:
    meta = json.loads(urllib.request.urlopen(urllib.request.Request(META, headers=HEADERS), timeout=10).read())
    check("meta/bond_modes count=7", len(meta.get("bond_modes", [])) == 7, f"got {len(meta.get('bond_modes',[]))}")
    check("meta/formats has ifcfg", any(f["id"] == "ifcfg" for f in meta.get("formats", [])), str([f["id"] for f in meta.get("formats",[])]))
    check("meta/formats has nmcli", any(f["id"] == "nmcli" for f in meta.get("formats", [])), "")
    check("meta/formats has netplan", any(f["id"] == "netplan" for f in meta.get("formats", [])), "")
    check("meta/os_options", len(meta.get("os_options", [])) >= 2, str(meta.get("os_options", [])))
except Exception as e:
    check("meta API", False, str(e)[:100])

# ===== 2. SPA Pages =====
print("\n=== 2. SPA Pages ===")
pages = ["", "dashboard", "netconfig", "pxe", "inspection", "login", "help"]
for p in pages:
    path = f"/{p}" if p else "/"
    try:
        code, html = http_get(path)
        has_vue = 'id="app"' in html
        check(f"page {path}", code == 200 and has_vue, f"code={code} vue={has_vue}")
    except Exception as e:
        check(f"page {path}", False, str(e)[:80])

# ===== 3. netplan all types =====
print("\n=== 3. netplan ===")
NP = {"os": "ubuntu", "format": "netplan"}

# DHCP
s = gen({**NP, "interfaces": [{"name": "eth0", "mode": "dhcp"}]})["script"]
check("np/dhcp", "dhcp4: true" in s and "renderer: networkd" in s)

# Static + DNS + gateway
s = gen({**NP, "hostname": "srv", "interfaces": [{"name": "ens192", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1", "dns": ["8.8.8.8", "114.114.114.114"]}]})["script"]
check("np/static+dns+gw", all(x in s for x in ["10.0.0.10/24", "via: 10.0.0.1", "8.8.8.8", "114.114.114.114"]))

# Multi NIC
s = gen({**NP, "interfaces": [{"name": "mgmt", "mode": "static", "ip": "10.0.0.10", "cidr": 24}, {"name": "eth0", "mode": "dhcp"}, {"name": "eth1", "mode": "dhcp"}]})["script"]
check("np/multi-nic", "mgmt:" in s and s.count("dhcp4: true") >= 2)

# Bond active-backup + primary
s = gen({**NP, "interfaces": [{"name": "eno1", "mode": "dhcp"}, {"name": "eno2", "mode": "dhcp"}], "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eno1", "eno2"], "ip": "10.10.0.10", "cidr": 24, "gateway": "10.10.0.1", "primary": "eno1", "miimon": 100}]})["script"]
check("np/bond-ab+primary", "active-backup" in s and "primary: eno1" in s and "miimon: 100" in s)
check("np/bond-slaves-disabled", "eno1:" in s.split("bonds:")[0] if "bonds:" in s else True)

# Bond LACP 4-slave
s = gen({**NP, "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1", "eth2", "eth3"], "ip": "10.20.0.10", "cidr": 24, "miimon": 50}]})["script"]
check("np/bond-lacp-4slave", "802.3ad" in s and "miimon: 50" in s)

# Dual bond
s = gen({**NP, "interfaces": [{"name": "mgmt", "mode": "dhcp"}, {"name": "eth0", "mode": "dhcp"}, {"name": "eth1", "mode": "dhcp"}, {"name": "eth2", "mode": "dhcp"}, {"name": "eth3", "mode": "dhcp"}], "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"], "ip": "10.10.0.10", "cidr": 24, "gateway": "10.10.0.1", "primary": "eth0"}, {"name": "bond1", "mode": 4, "interfaces": ["eth2", "eth3"], "ip": "10.20.0.10", "cidr": 24, "miimon": 100}]})["script"]
check("np/dual-bond", s.count("parameters:") == 2)

# VLAN on physical + bond
s = gen({**NP, "interfaces": [{"name": "mgmt", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1"}], "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"]}], "vlans": [{"parent": "mgmt", "vlan_id": 50, "ip": "10.50.0.10", "cidr": 24}, {"parent": "bond0", "vlan_id": 100, "ip": "172.16.100.10", "cidr": 24, "gateway": "172.16.100.1"}, {"parent": "bond0", "vlan_id": 200, "ip": "172.16.200.10", "cidr": 24}]})["script"]
check("np/vlan-on-bond", all(x in s for x in ["bond0.100", "bond0.200", "mgmt.50", "link: bond0"]))
check("np/vlan-gateway", "172.16.100.1" in s)

# Bridge on bond
s = gen({**NP, "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"]}], "bridges": [{"name": "br0", "interfaces": ["bond0"], "ip": "192.168.100.1", "cidr": 24}]})["script"]
check("np/bridge-on-bond", "br0:" in s and "bond0" in s)

# Full combo
s = gen({**NP, "hostname": "full", "interfaces": [{"name": "mgmt", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1"}], "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"]}, {"name": "bond1", "mode": 1, "interfaces": ["eth2", "eth3"], "ip": "10.20.0.10", "cidr": 24, "gateway": "10.20.0.1"}], "vlans": [{"parent": "bond0", "vlan_id": 10, "ip": "10.10.10.10", "cidr": 24}], "bridges": [{"name": "br0", "interfaces": ["bond1"], "ip": "192.168.200.1", "cidr": 24}]})["script"]
check("np/full-combo", all(x in s for x in ["mgmt:", "bond0:", "bond1:", "bond0.10:", "br0:"]))

# Explicit renderer
s = gen({**NP, "netplan_renderer": "networkd", "interfaces": [{"name": "eth0", "mode": "dhcp"}]})["script"]
check("np/explicit-renderer", "renderer: networkd" in s)

# ===== 4. nmcli all types =====
print("\n=== 4. nmcli ===")
NM = {"os": "rhel", "format": "nmcli"}

def nmcli_ok(s): return "command -v nmcli" in s

# Static + DNS
s = gen({**NM, "hostname": "rhel01", "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1", "dns": ["8.8.8.8"]}]})["script"]
check("nm/static+dns", nmcli_ok(s) and "ipv4.addresses 10.0.0.10/24" in s and "ipv4.dns 8.8.8.8" in s and "hostnamectl" in s)

# Bond active-backup + primary
s = gen({**NM, "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eth0", "eth1"], "ip": "10.10.0.10", "cidr": 24, "gateway": "10.10.0.1", "primary": "eth0"}]})["script"]
check("nm/bond-ab+primary", nmcli_ok(s) and "active-backup" in s and "primary=eth0" in s and "bond0-slave-eth1" in s)

# Bond LACP 4-slave
s = gen({**NM, "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1", "eth2", "eth3"], "ip": "10.20.0.10", "cidr": 24, "miimon": 50}]})["script"]
check("nm/bond-lacp-4slave", "802.3ad" in s and "bond0-slave-eth3" in s)

# VLAN on bond
s = gen({**NM, "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"]}], "vlans": [{"parent": "bond0", "vlan_id": 100, "ip": "172.16.100.10", "cidr": 24, "gateway": "172.16.100.1"}]})["script"]
check("nm/vlan-on-bond", "dev bond0 id 100" in s and "ipv4.gateway 172.16.100.1" in s)

# Bridge
s = gen({**NM, "bridges": [{"name": "br0", "interfaces": ["eth0"], "ip": "10.50.0.10", "cidr": 24, "gateway": "10.50.0.1"}]})["script"]
check("nm/bridge", "type bridge" in s and "br0-port-eth0" in s)

# Full combo
s = gen({**NM, "hostname": "rhel-all", "interfaces": [{"name": "mgmt", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1"}], "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"]}, {"name": "bond1", "mode": 1, "interfaces": ["eth2", "eth3"], "ip": "10.20.0.10", "cidr": 24}], "vlans": [{"parent": "bond0", "vlan_id": 10, "ip": "10.10.10.10", "cidr": 24}], "bridges": [{"name": "br0", "interfaces": ["bond1"], "ip": "192.168.200.1", "cidr": 24}]})["script"]
check("nm/full-combo", all(x in s for x in ["mgmt", "bond0", "bond1", "bond0.10", "br0"]))

# ===== 5. ifcfg all types =====
print("\n=== 5. ifcfg ===")
IF = {"os": "rhel", "format": "ifcfg"}

def ifcfg_ok(s): return "ONBOOT=yes" in s and "network-scripts" in s

# Static + dual DNS
s = gen({**IF, "hostname": "if01", "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1", "dns": ["8.8.8.8", "114.114.114.114"]}]})["script"]
check("if/static+dual-dns", ifcfg_ok(s) and "DNS1=8.8.8.8" in s and "DNS2=114.114.114.114" in s and "GATEWAY=10.0.0.1" in s)

# Bond with primary
s = gen({**IF, "interfaces": [{"name": "eno1", "mode": "dhcp"}, {"name": "eno2", "mode": "dhcp"}], "bonds": [{"name": "bond0", "mode": 1, "interfaces": ["eno1", "eno2"], "ip": "10.10.0.10", "cidr": 24, "gateway": "10.10.0.1", "primary": "eno1"}]})["script"]
check("if/bond+primary", "BONDING_MASTER" in s and "SLAVE=yes" in s and "primary=eno1" in s and "GATEWAY=10.10.0.1" in s)

# Dual bond + VLAN with gateway
s = gen({**IF, "interfaces": [{"name": "mgmt", "mode": "static", "ip": "10.0.0.10", "cidr": 24, "gateway": "10.0.0.1"}], "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"], "ip": "10.10.0.10", "cidr": 24}, {"name": "bond1", "mode": 1, "interfaces": ["eth2", "eth3"], "ip": "10.20.0.10", "cidr": 24}], "vlans": [{"parent": "bond0", "vlan_id": 100, "ip": "172.16.100.10", "cidr": 24, "gateway": "172.16.100.1"}, {"parent": "bond0", "vlan_id": 200, "ip": "172.16.200.10", "cidr": 24}]})["script"]
check("if/dual-bond+vlan", s.count("BONDING_MASTER") == 2 and "GATEWAY=172.16.100.1" in s and "PHYSDEV=bond0" in s)

# Bridge
s = gen({**IF, "bridges": [{"name": "br0", "interfaces": ["eth0"], "ip": "10.50.0.10", "cidr": 24, "gateway": "10.50.0.1"}]})["script"]
check("if/bridge", "TYPE=Bridge" in s and "BRIDGE=br0" in s)

# DHCP
s = gen({**IF, "interfaces": [{"name": "eth0", "mode": "dhcp"}]})["script"]
check("if/dhcp", "BOOTPROTO=dhcp" in s)

# ===== 6. Edge Cases =====
print("\n=== 6. Edge Cases ===")

try:
    s = gen({"os": "ubuntu", "format": "netplan", "interfaces": []})["script"]
    check("edge/empty-interfaces", len(s) > 30 and "network:" in s, f"len={len(s)}")
except Exception as e:
    check("edge/empty-interfaces", False, str(e)[:80])

try:
    s = gen({"os": "rhel", "format": "nmcli", "hostname": "bare"})["script"]
    check("edge/hostname-only", "hostnamectl" in s and "set -e" in s, f"len={len(s)}")
except Exception as e:
    check("edge/hostname-only", False, str(e)[:80])

try:
    s = gen({"os": "rhel", "format": "nmcli", "interfaces": [{"name": "eth0", "mode": "dhcp"}], "bonds": [{"name": "bond0", "mode": 4, "interfaces": ["eth0", "eth1"]}]})["script"]
    check("edge/bond-slave-in-ifaces", "bond0-slave-eth0" in s and "bond0-slave-eth1" in s)
except Exception as e:
    check("edge/bond-slave-in-ifaces", False, str(e)[:80])

try:
    s = gen({"os": "rhel", "format": "nmcli", "interfaces": [{"name": "eth0", "mode": "static", "ip": "10.0.0.10", "netmask": "255.255.255.0", "gateway": "10.0.0.1"}]})["script"]
    check("edge/netmask-instead-of-cidr", "10.0.0.10/24" in s, f"output: {s[:100]}")
except Exception as e:
    check("edge/netmask-instead-of-cidr", False, str(e)[:80])

try:
    for mode in range(7):
        s = gen({**NM, "bonds": [{"name": "bond0", "mode": mode, "interfaces": ["eth0", "eth1"], "ip": "10.10.0.10", "cidr": 24}]})["script"]
        check(f"edge/bond-mode-{mode}", len(s) > 100, f"len={len(s)}")
except Exception as e:
    check("edge/bond-all-modes", False, str(e)[:80])

try:
    s = gen({"os": "rhel", "format": "ifcfg", "interfaces": [], "hostname": "empty"})["script"]
    check("edge/ifcfg-empty", "ONBOOT=yes" in s or "network-scripts" in s, f"len={len(s)}")
except Exception as e:
    check("edge/ifcfg-empty", False, str(e)[:80])

# ===== 7. Response Format =====
print("\n=== 7. Response Format ===")
try:
    resp = gen({**NM, "interfaces": [{"name": "eth0", "mode": "dhcp"}]})
    check("resp/has-script", isinstance(resp.get("script"), str) and len(resp["script"]) > 50)
    check("resp/has-filename", isinstance(resp.get("filename"), str) and len(resp["filename"]) > 0)
    check("resp/has-format", resp.get("format") == "nmcli")
except Exception as e:
    check("resp/format", False, str(e)[:80])

# ===== SUMMARY =====
print(f"\n{'='*50}")
print(f"  TOTAL: {PASS_CNT} passed, {FAIL_CNT} failed")
print(f"{'='*50}")
sys.exit(0 if FAIL_CNT == 0 else 1)