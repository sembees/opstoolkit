# -*- coding: utf-8 -*-
"""netconfig「快照对拍」golden 回归：把 generate_netconfig 的生成物**逐字符钉死**。

为什么做这个：test_netconfig*.py 的既有用例都是**断言片段**（assertIn 关键行）——
改生成器时只要没碰坏那几行，其它任何输出漂移（注释改了、行序换了、缩进变了、
末尾少了换行、引号加了/去了）都会**静默**溜进产物。这里把每个 case 的完整产物
快照到 backend/tests/data/netconfig-golden/<case_id>.txt，生成物任何一个字节变了
这里必红，且报错里给出**首个差异行号 + 双方该行内容**（不是一句干巴巴的"不相等"）。

用法：
  · 日常回归（验收命令，在 backend 目录执行）：
      .venv/Scripts/python.exe -m pytest tests/test_netconfig_golden.py -q
  · 有意改动生成器、逐行 diff 审阅确认新产物正确后重新基线（写盘 utf-8、换行统一 \n）：
      OPSTK_UPDATE_GOLDEN=1 python -m pytest tests/test_netconfig_golden.py -q
    跑完必须看 golden 的 diff（git diff tests/data/netconfig-golden/），
    在本文件「基线历史」记一笔改了什么、为什么 —— 不要无脑重写期望值。

基线历史：
  · 2026-10-18（初版）：9 个 case，基线由当前实现在本机实跑生成
    （Python 3.13.3 / pytest 9.1.1 / pyyaml 6.0.3，D:\\07-cc\\01-project\\.venv）。

用例清单（CASES 覆盖矩阵，生成器未动一个字节，仅钉现状）：
  ubuntu + netplan：static / dhcp / bond(active-backup,含 primary 与从接口隐式补齐) /
                    vlan(父接口隐式补齐) / bridge(端口隐式补齐) / 纯 IPv6 / 双栈 IPv4+IPv6
                    （双栈另带一条 metric=50 的静态路由，锁住 default 与额外路由合成
                     同一个 routes: 键的行为）
  rhel  + nmcli   ：static 双网关（metric 100/200）+ 额外路由 + 主机名
  rhel  + ifcfg   ：static 双网关（METRIC 100/200）+ 静态 IPv6
                    （IPV6INIT/IPV6ADDR/IPV6_DEFAULTGW）

防呆：test_tamper_detection_canary 把 netplan-static 的产物改 1 个字符走比对函数，
断言比对**会红** —— 证明这套对拍不是"永远绿"。比对逻辑抽在 _diff / assert_same，
可被单独调用。
"""
import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, __file__.rsplit("tests", 1)[0])

from app.core.schemas import NetConfigRequest          # noqa: E402
from app.it.netconfig.generator import generate_netconfig   # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parent / "data" / "netconfig-golden"
UPDATE_ENV = "OPSTK_UPDATE_GOLDEN"

# 每个 case 一份固定请求（dict 原样喂 NetConfigRequest，与 HTTP 层同一入口校验）。
# smoke = 关键行双保险：即使有人把生成器和 golden 一起无脑重写，这几行还能单独兜住
# 最要紧的形态；expect_file 把 generate_netconfig 的第二返回值（文件名）也钉住。
CASES = [
    # ---------- ubuntu + netplan ----------
    dict(
        id="netplan-static",
        desc="ubuntu+netplan：单网卡静态 IPv4（cidr）+ 默认路由 + DNS",
        expect_file="99-opstk.yaml",
        smoke=("dhcp4: false", "addresses: [10.0.0.11/24]", "to: default",
               "via: 10.0.0.1", "nameservers:"),
        req=dict(os="ubuntu", format="netplan", hostname="web01",
                 interfaces=[dict(name="ens33", mode="static", ip="10.0.0.11", cidr=24,
                                  gateway="10.0.0.1",
                                  dns=["10.0.0.53", "10.0.0.54"])]),
    ),
    dict(
        id="netplan-dhcp",
        desc="ubuntu+netplan：DHCP 口 + 手工 DNS（dhcp4: true 与 nameservers 并存）",
        expect_file="99-opstk.yaml",
        smoke=("dhcp4: true", "nameservers:", "addresses: [10.0.0.53]"),
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="ens33", mode="dhcp", dns=["10.0.0.53"])]),
    ),
    dict(
        id="netplan-bond-active-backup",
        desc="ubuntu+netplan：bond(active-backup) + primary，从接口未单独声明（隐式补 L2）",
        expect_file="99-opstk.yaml",
        smoke=("bonds:", "interfaces: [eth0, eth1]", "mode: active-backup",
               "mii-monitor-interval: 100", "primary: eth0",
               "addresses: [10.0.0.20/24]", "eth0:", "eth1:"),
        req=dict(os="ubuntu", format="netplan",
                 bonds=[dict(name="bond0", mode=1, interfaces=["eth0", "eth1"],
                             ip="10.0.0.20", cidr=24, gateway="10.0.0.1",
                             miimon=100, primary="eth0", dns=["10.0.0.53"])]),
    ),
    dict(
        id="netplan-vlan",
        desc="ubuntu+netplan：VLAN 子接口（id/link），父接口未单独声明（隐式补 L2）",
        expect_file="99-opstk.yaml",
        smoke=("vlans:", "id: 100", "link: eth0", "addresses: [192.168.100.5/24]",
               "eth0:"),
        req=dict(os="ubuntu", format="netplan",
                 vlans=[dict(parent="eth0", vlan_id=100, mode="static",
                             ip="192.168.100.5", cidr=24, gateway="192.168.100.1",
                             dns=["192.168.100.53"])]),
    ),
    dict(
        id="netplan-bridge",
        desc="ubuntu+netplan：网桥 br0，端口未单独声明（隐式补 L2）",
        expect_file="99-opstk.yaml",
        smoke=("bridges:", "interfaces: [eth1, eth2]", "addresses: [10.0.1.10/24]",
               "eth1:", "eth2:"),
        req=dict(os="ubuntu", format="netplan",
                 bridges=[dict(name="br0", interfaces=["eth1", "eth2"], mode="static",
                               ip="10.0.1.10", cidr=24, gateway="10.0.1.1",
                               dns=["10.0.1.53"])]),
    ),
    dict(
        id="netplan-ipv6-only",
        # ★ 2026-10-18 真 netplan 纠错：netplan 没有 addresses6 键（`netplan generate` 直接报
        #   unknown key）。IPv6 地址进同一个 addresses 列表；IPv6 默认路由写 routes: to: "::/0"。
        desc="ubuntu+netplan：纯 IPv6（addresses 里带 v6 + routes to ::/0）",
        expect_file="99-opstk.yaml",
        smoke=('addresses: ["2001:db8::5/64"]', 'to: "::/0"', 'via: "2001:db8::1"',
               "dhcp4: true"),   # 现状：纯 IPv6 口的 IPv4 侧仍是 dhcp4: true（基线即如此）
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="eth0", mode="static",
                                  ip6="2001:db8::5/64", gateway6="2001:db8::1")]),
    ),
    dict(
        id="netplan-dual-stack",
        desc="ubuntu+netplan：双栈 IPv4+IPv6 + 静态路由（default 与额外路由合成一个 routes: 键）",
        expect_file="99-opstk.yaml",
        smoke=("addresses: [10.10.10.5/24, \"2001:db8::5/64\"]", 'to: "::/0"',
               'via: "2001:db8::1"', "to: default", "to: 10.20.0.0/16",
               "via: 10.10.10.254", "metric: 50"),
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="eth0", mode="static", ip="10.10.10.5", cidr=24,
                                  gateway="10.10.10.1",
                                  ip6="2001:db8::5/64", gateway6="2001:db8::1",
                                  dns=["10.10.10.53"],
                                  routes=[dict(to="10.20.0.0/16", via="10.10.10.254",
                                               metric=50)])]),
    ),
    # ---------- 2026-10-18 新增能力：DHCPv6 / SLAAC / 多地址 / IPv6 路由 ----------
    # 放进 CASES 是一石二鸟：既被快照钉死，又会被 test_netconfig_netplan_real.py
    # 自动拿去喂**真 netplan**（那个用例只挑 format=netplan 的 case）。
    dict(
        id="netplan-dhcp6",
        desc="ubuntu+netplan：DHCPv6（dhcp6: true，不写静态 IPv6 地址）",
        expect_file="99-opstk.yaml",
        smoke=("dhcp6: true", "addresses: [10.10.10.5/24]"),
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="eth0", mode="static", ip="10.10.10.5", cidr=24,
                                  gateway="10.10.10.1", ip6_mode="dhcp6")]),
    ),
    dict(
        id="netplan-slaac",
        desc="ubuntu+netplan：SLAAC（accept-ra: true + dhcp6: false）",
        expect_file="99-opstk.yaml",
        smoke=("accept-ra: true", "dhcp6: false"),
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="eth0", mode="static", ip="10.10.10.5", cidr=24,
                                  ip6_mode="slaac")]),
    ),
    dict(
        id="netplan-multi-ipv6",
        desc="ubuntu+netplan：一个接口两个 IPv6 地址（都在 addresses 里）+ v6 默认路由",
        expect_file="99-opstk.yaml",
        smoke=('addresses: [10.10.10.5/24, "2001:db8::5/64", "2001:db8::6/64"]',
               'to: "::/0"', 'via: "2001:db8::1"'),
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="eth0", mode="static", ip="10.10.10.5", cidr=24,
                                  gateway="10.10.10.1",
                                  ip6="2001:db8::5/64", ip6_addrs=["2001:db8::6/64"],
                                  gateway6="2001:db8::1")]),
    ),
    dict(
        id="netplan-ipv6-route",
        desc="ubuntu+netplan：IPv6 的**额外路由**（2001:db8:1::/64 via v6）",
        expect_file="99-opstk.yaml",
        smoke=('to: "2001:db8:1::/64"', 'via: "2001:db8::1"', "metric: 20"),
        req=dict(os="ubuntu", format="netplan",
                 interfaces=[dict(name="eth0", mode="static", ip="10.10.10.5", cidr=24,
                                  ip6="2001:db8::5/64",
                                  routes=[dict(to="2001:db8:1::/64", via="2001:db8::1",
                                               metric=20)])]),
    ),
    # ---------- rhel + nmcli ----------
    dict(
        id="nmcli-static-dual-gateway",
        desc="rhel+nmcli：两个静态网卡各带默认网关（route-metric 100/200）+ 额外路由 + 主机名",
        expect_file="apply-network.sh",
        smoke=("hostnamectl set-hostname app01", "ipv4.gateway 10.0.0.1",
               "ipv4.route-metric 100", "ipv4.gateway 10.0.1.1",
               "ipv4.route-metric 200", "+ipv4.routes '172.16.0.0/16 10.0.0.254'",
               "ipv4.dns '10.0.0.53 114.114.114.114'"),
        req=dict(os="rhel", format="nmcli", hostname="app01",
                 interfaces=[
                     dict(name="eth0", mode="static", ip="10.0.0.5", cidr=24,
                          gateway="10.0.0.1", dns=["10.0.0.53", "114.114.114.114"],
                          routes=[dict(to="172.16.0.0/16", via="10.0.0.254")]),
                     dict(name="eth1", mode="static", ip="10.0.1.5", cidr=24,
                          gateway="10.0.1.1"),
                 ]),
    ),
    # ---------- rhel + ifcfg ----------
    dict(
        id="ifcfg-static-dual-gateway-ipv6",
        desc="rhel+ifcfg：双网关（METRIC 100/200）+ 静态 IPv6（IPV6INIT/IPV6ADDR/IPV6_DEFAULTGW）",
        expect_file="ifcfg-files.txt",
        smoke=("ifcfg-eth0", "IPADDR=10.0.0.5", "PREFIX=24", "GATEWAY=10.0.0.1",
               "METRIC=100", "IPV6INIT=yes", "IPV6ADDR=2001:db8::5/64",
               "IPV6_DEFAULTGW=2001:db8::1", "ifcfg-eth1", "GATEWAY=10.0.1.1",
               "METRIC=200"),
        req=dict(os="rhel", format="ifcfg",
                 interfaces=[
                     dict(name="eth0", mode="static", ip="10.0.0.5", cidr=24,
                          gateway="10.0.0.1", ip6="2001:db8::5/64",
                          gateway6="2001:db8::1", dns=["10.0.0.53"]),
                     dict(name="eth1", mode="static", ip="10.0.1.5", cidr=24,
                          gateway="10.0.1.1", dns=["10.0.1.53"]),
                 ]),
    ),
]


# ---------- 比对逻辑（可单独调用；防呆用例直接调它） ----------
def _first_char_col(a: str, b: str) -> int:
    """两行里首个不同字符的 1-based 位置（长度不同也按差异报）。"""
    for j, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return j + 1
    return min(len(a), len(b)) + 1


def _diff(expected: str, actual: str):
    """逐字符钉死用的差异定位。完全一致返回 None；否则返回
    (首个差异行号(1-based), 期望侧该行, 实际侧该行)。

    按 split("\\n") 找首个不同的行：两串文本相等 ⇔ 行序列相等，所以
    返回 None 就等于逐字节一致（含末尾换行的有无 —— 缺行会以"<xx侧已无此行>"报出）。
    """
    exp_lines = expected.split("\n")
    act_lines = actual.split("\n")
    for i, (e, a) in enumerate(zip(exp_lines, act_lines)):
        if e != a:
            return i + 1, e, a
    if len(exp_lines) != len(act_lines):
        i = min(len(exp_lines), len(act_lines))
        e = exp_lines[i] if i < len(exp_lines) else "<期望侧已无此行>"
        a = act_lines[i] if i < len(act_lines) else "<实际侧已无此行>"
        return i + 1, e, a
    return None


def assert_same(expected: str, actual: str, *, case_id: str = "-") -> None:
    """不一致就 AssertionError，报错带**首个差异行号 + 双方该行内容**。

    行内容用 repr 输出，行尾空格 / \\r / 不可见字符不会被吞掉。
    """
    d = _diff(expected, actual)
    if d is None:
        return
    lineno, e_line, a_line = d
    col = _first_char_col(e_line, a_line)
    raise AssertionError(
        "netconfig 生成物与 golden 不一致（case=%s）：首个差异在第 %d 行第 %d 个字符\n"
        "  golden  第 %d 行: %r\n"
        "  生成物  第 %d 行: %r\n"
        "若本次生成器改动是有意的：先逐行 diff 审阅，再 OPSTK_UPDATE_GOLDEN=1 重新基线，"
        "不要无脑重写期望值。" % (case_id, lineno, col, lineno, e_line, lineno, a_line)
    )


def _read_golden(path: Path) -> str:
    """读基线：utf-8，并把 CRLF 归一成 LF。

    ★ 2026-10-18 实测：Windows 上 `core.autocrlf=true` 会把 checkout 出来的基线转成 CRLF，
    于是 9 条快照 + 1 条防呆全红（`首个差异在第 1 行：'# /etc/netplan/99-opstk.yaml\\r'`）。
    治本用 `.gitattributes` 把基线目录标成 `-text`（不转换）；这里再加一道归一化，
    让"换了台机器/换了编辑器"不会把 CLI 换行差异误报成生成器漂移。
    注意：**只归一 CRLF**，其它差异（空格、行序、值）仍然逐字节比对。
    """
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def _write_golden(path: Path, text: str) -> None:
    # 写盘：utf-8、换行统一 \n（基线在 Windows/Linux 上逐字节一致）。
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


# ---------- 快照对拍 ----------
@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_netconfig_golden_snapshot(case):
    script, filename = generate_netconfig(NetConfigRequest(**case["req"]))
    golden = GOLDEN_DIR / (case["id"] + ".txt")

    if os.environ.get(UPDATE_ENV) == "1":
        _write_golden(golden, script)
        print("[update-golden] %s <- %d 字节 / %d 行"
              % (golden.name, len(script.encode("utf-8")), script.count("\n")))
        pytest.skip("OPSTK_UPDATE_GOLDEN=1：已重写基线，跳过比对")

    assert golden.exists(), (
        "缺 golden 基线：%s\n首次基线：在 backend 目录跑 "
        "OPSTK_UPDATE_GOLDEN=1 python -m pytest tests/test_netconfig_golden.py -q" % golden
    )
    expected = _read_golden(golden)
    # 主断言：逐字符一致（不一致时报首个差异行号 + 双方该行内容）
    assert_same(expected, script, case_id=case["id"])

    # 双保险 1：关键行仍在（防"生成器与 golden 一起被无脑重写"）
    for frag in case["smoke"]:
        assert frag in script, "关键行丢失（case=%s）：%r" % (case["id"], frag)
    # 双保险 2：第二返回值（产物文件名）也钉住
    assert filename == case["expect_file"], (
        "filename 漂移（case=%s）：期望 %r，实际 %r" % (case["id"], case["expect_file"], filename)
    )
    # 双保险 3：netplan 产物必须是合法 YAML（解析失败这里直接红）
    if case["req"]["format"] == "netplan":
        doc = yaml.safe_load(script)
        assert doc["network"]["version"] == 2


def test_tamper_detection_canary():
    """★ 防呆：证明这套对拍**不是永远绿** —— 改 1 个字符，比对必须红。

    同时钉住比对函数自身的两件事：
      1) `_diff` 对一致输入返回 None（不会"永远报错"）；
      2) 单字符差异被定位到**正确的行号**，assert_same 的报错带行号与双方该行内容。
    改法：netplan 头部 "  version: 2" 的 2 → 3 —— 改完仍是合法 YAML，
    证明抓的是内容漂移而不是把 YAML 弄坏。
    """
    case = next(c for c in CASES if c["id"] == "netplan-static")
    script, _ = generate_netconfig(NetConfigRequest(**case["req"]))
    golden = GOLDEN_DIR / (case["id"] + ".txt")
    # 基线：盘上有 golden 就用 golden（与正式用例同一来源）；没有（首次跑未基线）用未改动的现产物。
    baseline = _read_golden(golden) if golden.exists() else script

    # 绿路径：一致输入必须无差异
    assert _diff(baseline, script) is None, "_diff 对完全一致的输入不该报差异"

    # 改 1 个字符
    marker = "  version: 2"
    pos = script.index(marker) + len(marker) - 1        # 指向字符 '2'
    tampered = script[:pos] + "3" + script[pos + 1:]
    assert len(tampered) == len(script) and tampered != script, "没有按预期只改一个字符"

    d = _diff(baseline, tampered)
    assert d is not None, "改了 1 个字符 _diff 仍返回 None —— 比对函数失效，这套对拍不可信"
    lineno, e_line, a_line = d
    assert lineno == script.count("\n", 0, pos) + 1, "差异定位到的行号不对：%r" % (d,)
    with pytest.raises(AssertionError) as ei:
        assert_same(baseline, tampered, case_id=case["id"])
    msg = str(ei.value)
    assert "第 %d 行" % lineno in msg, "报错里没有行号：%s" % msg
    assert repr(e_line) in msg and repr(a_line) in msg, "报错里没有双方该行内容：%s" % msg
