#!/usr/bin/env python3
"""NetConfig 生成器专项测试 — 覆盖所有格式/类型/边界"""
import urllib.request, json, sys, os

BASE = os.environ.get("TEST_BASE", "http://127.0.0.1:8000")
API = f"{BASE}/api/it/netconfig/generate"

def login():
    data = json.dumps({"username":"admin","password":"admin@123"}).encode()
    req = urllib.request.Request(f"{BASE}/api/auth/login", data, {"Content-Type":"application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())["access_token"]

TOKEN = login()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

PASS = FAIL = 0
def check(name, ok, detail=""):
    global PASS, FAIL
    if ok: PASS += 1; print(f"  [PASS] {name}")
    else: FAIL += 1; print(f"  [FAIL] {name}  --  {detail}")

def gen(payload):
    req = urllib.request.Request(API, json.dumps(payload).encode(), HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())

# ===== 1. netplan (Ubuntu) =====
print("\n=== netplan ===")
NP = {"os":"ubuntu","format":"netplan"}

# 1a) DHCP
s = gen({**NP, "interfaces":[{"name":"eth0","mode":"dhcp"}]})["script"]
check("dhcp", "dhcp4: true" in s and "ethernets:" in s)

# 1b) static + DNS + gateway
s = gen({**NP, "hostname":"web01", "interfaces":[{"name":"ens192","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1","dns":["8.8.8.8","114.114.114.114"]}]})["script"]
check("static+dns+gw", all(x in s for x in ["addresses:","routes:","via:","nameservers:","8.8.8.8"]))

# 1c) multi NIC
s = gen({**NP, "interfaces":[{"name":"mgmt","mode":"static","ip":"10.0.0.10","cidr":24},{"name":"eth0","mode":"dhcp"},{"name":"eth1","mode":"dhcp"}]})["script"]
check("multi-NIC", s.count("dhcp4:") >= 3)

# 1d) bond active-backup + primary
s = gen({**NP, "interfaces":[{"name":"eno1","mode":"dhcp"},{"name":"eno2","mode":"dhcp"}], "bonds":[{"name":"bond0","mode":1,"interfaces":["eno1","eno2"],"ip":"10.10.0.10","cidr":24,"gateway":"10.10.0.1","primary":"eno1","miimon":100}]})["script"]
check("bond active-backup+primary", "active-backup" in s and "primary: eno1" in s)
check("bond slaves disabled", "eno1:" in s.split("bonds:")[0] if "bonds:" in s else True)

# 1e) bond LACP 4-slave
s = gen({**NP, "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1","eth2","eth3"],"ip":"10.20.0.10","cidr":24,"miimon":50}]})["script"]
check("bond LACP 4-slave", "802.3ad" in s and "miimon: 50" in s)

# 1f) dual bond
s = gen({**NP, "interfaces":[{"name":"mgmt","mode":"dhcp"},{"name":"eth0","mode":"dhcp"},{"name":"eth1","mode":"dhcp"},{"name":"eth2","mode":"dhcp"},{"name":"eth3","mode":"dhcp"}], "bonds":[{"name":"bond0","mode":1,"interfaces":["eth0","eth1"],"ip":"10.10.0.10","cidr":24,"gateway":"10.10.0.1","primary":"eth0"},{"name":"bond1","mode":4,"interfaces":["eth2","eth3"],"ip":"10.20.0.10","cidr":24,"miimon":100}]})["script"]
check("dual-bond", s.count("parameters:") == 2)

# 1g) VLAN on physical + bond
s = gen({**NP, "interfaces":[{"name":"mgmt","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1"}], "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"]}], "vlans":[{"parent":"mgmt","vlan_id":50,"ip":"10.50.0.10","cidr":24},{"parent":"bond0","vlan_id":100,"ip":"172.16.100.10","cidr":24,"gateway":"172.16.100.1"},{"parent":"bond0","vlan_id":200,"ip":"172.16.200.10","cidr":24}]})["script"]
check("VLAN on bond", all(x in s for x in ["bond0.100","bond0.200","mgmt.50","link: bond0"]))

# 1h) bridge on bond
s = gen({**NP, "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"]}], "bridges":[{"name":"br0","interfaces":["bond0"],"ip":"192.168.100.1","cidr":24}]})["script"]
check("bridge on bond", "br0:" in s and "bond0" in s)

# 1i) full combo: 2 bonds + VLAN + bridge
s = gen({**NP, "hostname":"full","interfaces":[{"name":"mgmt","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1"}], "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"]},{"name":"bond1","mode":1,"interfaces":["eth2","eth3"],"ip":"10.20.0.10","cidr":24,"gateway":"10.20.0.1"}], "vlans":[{"parent":"bond0","vlan_id":10,"ip":"10.10.10.10","cidr":24}], "bridges":[{"name":"br0","interfaces":["bond1"],"ip":"192.168.200.1","cidr":24}]})["script"]
check("full combo netplan", all(x in s for x in ["mgmt:","bond0:","bond1:","bond0.10","br0:"]))

# ===== 2. nmcli (RHEL) =====
print("\n=== nmcli ===")
NM = {"os":"rhel","format":"nmcli"}

def nmcli_check(s): return "command -v nmcli" in s

# 2a) static
s = gen({**NM, "hostname":"rhel01","interfaces":[{"name":"eth0","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1","dns":["8.8.8.8"]}]})["script"]
check("nmcli static", nmcli_check(s) and "ipv4.addresses 10.0.0.10/24" in s)

# 2b) bond
s = gen({**NM, "bonds":[{"name":"bond0","mode":1,"interfaces":["eth0","eth1"],"ip":"10.10.0.10","cidr":24,"gateway":"10.10.0.1","primary":"eth0"}]})["script"]
check("nmcli bond AB", nmcli_check(s) and "active-backup" in s and "primary=eth0" in s)

# 2c) bond LACP
s = gen({**NM, "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1","eth2","eth3"],"ip":"10.20.0.10","cidr":24,"miimon":50}]})["script"]
check("nmcli bond LACP 4slave", "bond0-slave-eth3" in s and "802.3ad" in s)

# 2d) VLAN on bond
s = gen({**NM, "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"]}], "vlans":[{"parent":"bond0","vlan_id":100,"ip":"172.16.100.10","cidr":24,"gateway":"172.16.100.1"}]})["script"]
check("nmcli VLAN on bond", "dev bond0 id 100" in s and "ipv4.gateway 172.16.100.1" in s)

# 2e) bridge
s = gen({**NM, "bridges":[{"name":"br0","interfaces":["eth0"],"ip":"10.50.0.10","cidr":24,"gateway":"10.50.0.1"}]})["script"]
check("nmcli bridge", "type bridge" in s and "br0-port-eth0" in s)

# 2f) full combo
s = gen({**NM, "hostname":"rhel-all","interfaces":[{"name":"mgmt","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1"}], "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"]},{"name":"bond1","mode":1,"interfaces":["eth2","eth3"],"ip":"10.20.0.10","cidr":24}], "vlans":[{"parent":"bond0","vlan_id":10,"ip":"10.10.10.10","cidr":24}], "bridges":[{"name":"br0","interfaces":["bond1"],"ip":"192.168.200.1","cidr":24}]})["script"]
check("nmcli full combo", all(x in s for x in ["mgmt","bond0","bond1","bond0.10","br0"]))

# ===== 3. ifcfg (RHEL legacy) =====
print("\n=== ifcfg ===")
IF = {"os":"rhel","format":"ifcfg"}

def ifcfg_check(s): return "ONBOOT=yes" in s and "network-scripts" in s

# 3a) static
s = gen({**IF, "hostname":"if01","interfaces":[{"name":"eth0","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1","dns":["8.8.8.8","114.114.114.114"]}]})["script"]
check("ifcfg static+dns", ifcfg_check(s) and "DNS1=8.8.8.8" in s and "DNS2=114.114.114.114" in s)

# 3b) bond
s = gen({**IF, "interfaces":[{"name":"eno1","mode":"dhcp"},{"name":"eno2","mode":"dhcp"}], "bonds":[{"name":"bond0","mode":1,"interfaces":["eno1","eno2"],"ip":"10.10.0.10","cidr":24,"gateway":"10.10.0.1","primary":"eno1"}]})["script"]
check("ifcfg bond", "BONDING_MASTER" in s and "SLAVE=yes" in s and "primary=eno1" in s)

# 3c) dual bond + VLAN
s = gen({**IF, "interfaces":[{"name":"mgmt","mode":"static","ip":"10.0.0.10","cidr":24,"gateway":"10.0.0.1"}], "bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"],"ip":"10.10.0.10","cidr":24},{"name":"bond1","mode":1,"interfaces":["eth2","eth3"],"ip":"10.20.0.10","cidr":24}], "vlans":[{"parent":"bond0","vlan_id":100,"ip":"172.16.100.10","cidr":24,"gateway":"172.16.100.1"},{"parent":"bond0","vlan_id":200,"ip":"172.16.200.10","cidr":24}]})["script"]
check("ifcfg dual bond+VLAN", s.count("BONDING_MASTER") == 2 and "GATEWAY=172.16.100.1" in s and "PHYSDEV=bond0" in s)

# 3d) bridge
s = gen({**IF, "bridges":[{"name":"br0","interfaces":["eth0"],"ip":"10.50.0.10","cidr":24,"gateway":"10.50.0.1"}]})["script"]
check("ifcfg bridge", "TYPE=Bridge" in s and "BRIDGE=br0" in s)

# ===== 4. Edge cases =====
print("\n=== Edge cases ===")
try:
    s = gen({"os":"ubuntu","format":"netplan","interfaces":[]})["script"]
    check("empty interfaces", len(s) > 30)
except Exception as e: check("empty interfaces", False, str(e)[:80])

try:
    s = gen({"os":"rhel","format":"nmcli","hostname":"bare"})["script"]
    check("hostname only", "hostnamectl" in s)
except Exception as e: check("hostname only", False, str(e)[:80])

try:
    s = gen({"os":"rhel","format":"nmcli","interfaces":[{"name":"eth0","mode":"dhcp"}],"bonds":[{"name":"bond0","mode":4,"interfaces":["eth0","eth1"]}]})["script"]
    check("bond slave in ifaces", "bond0-slave-eth0" in s)
except Exception as e: check("bond slave in ifaces", False, str(e)[:80])

try:
    s = gen({"os":"ubuntu","format":"netplan","netplan_renderer":"networkd","interfaces":[{"name":"eth0","mode":"dhcp"}]})["script"]
    check("explicit renderer", "renderer: networkd" in s)
except Exception as e: check("explicit renderer", False, str(e)[:80])

print(f"\n{'='*50}")
print(f"  TOTAL: {PASS} passed, {FAIL} failed")
print(f"{'='*50}")
sys.exit(0 if FAIL == 0 else 1)