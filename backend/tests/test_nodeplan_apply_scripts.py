# -*- coding: utf-8 -*-
"""M2 脚本包的**离线真跑**用例：用 shim 桩把 apply/check/rollback 两条路径跑一遍。

为什么不在 Windows 上只断言字符串：
  "改 IP 失联 → 回滚跑不到"这类问题**只会在脚本逻辑里暴露**，字符串断言看不出来。
  这里用 Git Bash + 假的 ip/netplan/nmcli（shim），在没有真机的情况下验证：
    · 期望状态满足 → result=applied；
    · 期望状态不满足 → 自动走 rollback，且 result=rolled_back、备份被恢复。
  真机验收（zfs153）另做，这一层是回归网。

跳过条件：没有 bash 的环境（例如 CI 的纯 Windows runner 没装 Git）——用 pytest.skip，
不掩盖失败，只是标记"本机没有 bash 无法跑这一层"。
"""
import json
import pathlib
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.nodeplan.apply import build_expected, render_bundle  # noqa: E402

BASH_CANDIDATES = [
    r"C:\Program Files\Git\bin\bash.exe",
    r"C:\Program Files (x86)\Git\bin\bash.exe",
    "bash",
]


def _bash():
    for p in BASH_CANDIDATES:
        found = shutil.which(p) if not p.startswith("C:") else (p if pathlib.Path(p).exists() else None)
        if found:
            return found
    return None


BASH = _bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="本机没有 bash，无法跑脚本逻辑这一层")


def _posix(p: pathlib.Path) -> str:
    """Windows 路径 → Git Bash 的 POSIX 路径（D:\\a\\b → /d/a/b）。"""
    s = str(p).replace("\\", "/")
    if len(s) > 1 and s[1] == ":":
        return "/" + s[0].lower() + s[2:]
    return s


IFACES = [{"kind": "interface",
           "spec": {"name": "eth0", "mode": "static", "ip": "192.168.30.10",
                    "cidr": 24, "gateway": "192.168.30.1", "dns": ["192.168.30.1"]}}]


def _prepare(tmp_path, ok: bool):
    """布置一套"目标机"：root/etc/netplan（旧配置）+ state（脚本包）+ shims。"""
    root = tmp_path / "root"
    (root / "etc" / "netplan").mkdir(parents=True)
    (root / "etc" / "netplan" / "01-old.yaml").write_text("network: {version: 2}\n", encoding="utf-8")
    (root / "etc").mkdir(exist_ok=True)
    (root / "etc" / "resolv.conf").write_text("nameserver 192.168.30.1\n", encoding="utf-8")

    cid = "cid123"
    state = tmp_path / "state" / cid
    state.mkdir(parents=True)

    expected = build_expected("ubuntu", IFACES)
    bundle = render_bundle(change_id=cid, os_type="ubuntu", fmt="netplan",
                           config_name="99-opstk.yaml",
                           config_text="network:\n  version: 2\n  ethernets:\n    eth0:\n      "
                                       "addresses: [192.168.30.10/24]\n",
                           expected=expected)
    for name, content in bundle["files"].items():
        (state / name).write_text(content, encoding="utf-8", newline="\n")

    # shims：ip 输出（ok=True 时包含期望地址与默认路由）
    shims = tmp_path / "shims"
    shims.mkdir()
    addr_line = ("2: eth0    inet 192.168.30.10/24 brd 192.168.30.255 scope global eth0\\\n"
                 if ok else "2: eth0    inet 10.9.9.9/24 brd 10.9.9.255 scope global eth0\\\n")
    route_line = ("default via 192.168.30.1 dev eth0 proto static\\\n" if ok
                  else "default via 10.9.9.1 dev eth0 proto static\\\n")
    (shims / "ip").write_text(
        '#!/bin/bash\nif [ "$1" = "-o" ] && [ "$2" = "addr" ]; then printf "%s"; exit 0; fi\n'
        'if [ "$1" = "-o" ] && [ "$2" = "route" ]; then printf "%s"; exit 0; fi\nexit 0\n'
        % (addr_line, route_line), encoding="utf-8", newline="\n")
    for name in ("netplan", "nmcli", "bridge"):
        (shims / name).write_text("#!/bin/bash\nexit 0\n", encoding="utf-8", newline="\n")
    for p in shims.iterdir():
        p.chmod(0o755)
    return root, state, shims


def _run(root, state, shims):
    script = _posix(state / "run.sh")
    env = ("export OPSTK_ROOT=%s; export OPSTK_STATE=%s; export PATH=%s:$PATH; "
           "bash %s") % (_posix(root), _posix(state), _posix(shims), script)
    return subprocess.run([BASH, "-c", env], capture_output=True, text=True, timeout=120)


@needs_bash
def test_success_applies(tmp_path):
    root, state, shims = _prepare(tmp_path, ok=True)
    r = _run(root, state, shims)
    res = json.loads((state / "result.json").read_text(encoding="utf-8"))
    assert res["result"] == "applied", (r.stdout, r.stderr, res)
    assert res["change_id"] == "cid123" and len(res["expected_hash"]) == 64
    # 新配置落到 netplan 目录
    assert (root / "etc" / "netplan" / "99-opstk.yaml").exists()
    log = (state / "apply.log").read_text(encoding="utf-8")
    assert "APPLY_OK" in log


@needs_bash
def test_failure_rolls_back_and_restores_config(tmp_path):
    """期望地址不存在 → check 失败 → 必须自动回滚，并把旧配置恢复回去。"""
    root, state, shims = _prepare(tmp_path, ok=False)
    r = _run(root, state, shims)
    res = json.loads((state / "result.json").read_text(encoding="utf-8"))
    assert res["result"] == "rolled_back", (r.stdout, r.stderr, res)
    log = (state / "apply.log").read_text(encoding="utf-8")
    assert "CHECK_FAILED_AFTER_APPLY" in log and "ROLLBACK_DONE" in log
    # 回滚把备份的旧配置放回来了，新配置被清掉
    assert (root / "etc" / "netplan" / "01-old.yaml").exists()
    assert not (root / "etc" / "netplan" / "99-opstk.yaml").exists()


def test_bundle_contains_all_pieces():
    """不依赖 bash 的结构断言：脚本包必须齐、timer 必须是一次性的、manifest 要能对账。"""
    expected = build_expected("ubuntu", IFACES)
    b = render_bundle(change_id="c9", os_type="ubuntu", fmt="netplan",
                      config_name="99-opstk.yaml", config_text="x: 1\n", expected=expected,
                      callback_url="http://10.0.0.1:8000/api/it/nodeplan/plans/p/report?token=t")
    f = b["files"]
    for name in ("probe.sh", "check.sh", "apply.sh", "rollback.sh", "run.sh",
                 "config.yaml", "manifest.json",
                 "opstk-netconfig-apply@c9.service", "opstk-netconfig-apply@c9.timer"):
        assert name in f, name
    assert "OnActiveSec=90" in f["opstk-netconfig-apply@c9.timer"]
    assert "192.168.30.10/24" in f["check.sh"]
    assert "via $GW" in f["check.sh"]
    assert "http://10.0.0.1:8000/api/it/nodeplan/plans/p/report?token=t" in f["run.sh"]
    m = json.loads(f["manifest.json"])
    assert m["expected"]["addresses"] == ["192.168.30.10/24"]
    assert m["timer_delay_sec"] == 90


def test_expected_collects_only_interface_kind():
    """bond/vlan 不进本地粗判（输出格式各发行版不同），只取 interface 的地址/网关/DNS。"""
    e = build_expected("rhel", [
        {"kind": "interface", "spec": {"name": "eth0", "ip": "10.1.1.1", "cidr": 24,
                                       "gateway": "10.1.1.254", "dns": ["10.1.1.254", "8.8.8.8"]}},
        {"kind": "bond", "spec": {"name": "bond0"}},
    ])
    assert e["addresses"] == ["10.1.1.1/24"]
    assert e["default_gateway"] == "10.1.1.254"
    assert e["dns"] == ["10.1.1.254", "8.8.8.8"]
