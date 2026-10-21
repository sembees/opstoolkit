# -*- coding: utf-8 -*-
"""M4b 设备下发的纯逻辑用例（假 shell，不碰真设备）。

要钉住的正是设备下发最容易"看起来成功"的三件事：
  · 厂商只回一行 `% Unrecognized` 时，必须判失败（不能以为推成功了）；
  · 失败时**不许保存**（`return` 丢弃未保存配置 = 设备侧的"回滚"）；
  · 回读对账要逐条比对，缺一条就是不一致。
"""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.it.nodeplan.device import (ShellError, expected_lines_from_config, push_config,  # noqa: E402
                                    save_config, verify_device)

CFG = """system-view
vlan 10
 name MGMT
quit
interface Vlan-interface10
 ip address 10.0.0.21 255.255.255.0
quit
return
"""


class FakeShell:
    """按行返回预置输出；记录发出去的每一行。"""

    def __init__(self, outputs=None, default=""):
        self.sent = []
        self.outputs = outputs or {}
        self.default = default

    def send(self, line):
        self.sent.append(line)
        for pat, out in self.outputs.items():
            if pat in line:
                return out
        return self.default


def test_push_success_reads_running_config():
    sh = FakeShell(default="<SW1>", outputs={
        "display current-configuration": "vlan 10\n ip address 10.0.0.21 255.255.255.0\n",
    })
    r = push_config(sh, "h3c", CFG, persist=True)
    assert r["ok"] is True and r["errors"] == [] and r["saved"] is False
    assert "system-view" in sh.sent
    assert any("display current-configuration" in x for x in sh.sent)
    assert "vlan 10" in r["running_config"]
    # 注释与视图切换命令不应推给设备
    assert "return" not in [s.strip() for s in sh.sent if s.strip().lower() == "return"][:1]


def test_unrecognized_command_is_failure_and_does_not_save():
    sh = FakeShell(default="<SW1>", outputs={
        "ip address 10.0.0.21": "% Unrecognized command found at '^' position.\n",
    })
    r = push_config(sh, "h3c", CFG, persist=True)
    assert r["ok"] is False and r["saved"] is False
    assert r["errors"] and r["errors"][0]["line"].strip().startswith("ip address")
    assert "return" in sh.sent            # 退出配置视图 = 丢弃未保存配置
    assert not any("save force" in x for x in sh.sent)


def test_cisco_uses_configure_terminal_and_write_memory():
    sh = FakeShell(default="SW1#")
    r = push_config(sh, "cisco", "vlan 10\n", persist=True)
    assert r["ok"] is True
    assert "configure terminal" in sh.sent
    assert any("show running-config" in x for x in sh.sent)

    sh2 = FakeShell(default="SW1#", outputs={"write memory": "Building configuration...\n[OK]\n"})
    s = save_config(sh2, "cisco")
    assert s["saved"] is True


def test_save_answers_y_prompt():
    sh = FakeShell(default="", outputs={
        "save": "The current configuration will be written to the device. Continue? [Y/N]:",
    })
    s = save_config(sh, "huawei")
    assert "y" in [x.strip().lower() for x in sh.sent]
    assert s["saved"] is True


def test_save_failure_is_reported():
    sh = FakeShell(default="", outputs={"save": "Error: write failed\n"})
    assert save_config(sh, "h3c")["saved"] is False


def test_verify_device_reports_missing_line():
    running = "vlan 10\n interface Vlan-interface10\n  ip address 10.0.0.21 255.255.255.0\n"
    v = verify_device({"lines": ["vlan 10", "ip address 10.0.0.21 255.255.255.0"]}, running)
    assert v["ok"] is True

    v2 = verify_device({"lines": ["vlan 10", "vlan 99"]}, running)
    assert v2["ok"] is False
    assert any(i["expected"] == "vlan 99" and not i["ok"] for i in v2["items"])


def test_expected_lines_skip_view_commands_and_comments():
    e = expected_lines_from_config("# 注释\nsystem-view\nvlan 10\n ! 分隔\nreturn\n")
    assert e["lines"] == ["vlan 10"]


def test_unknown_vendor_rejected():
    with pytest.raises(ValueError):
        push_config(FakeShell(), "juniper", "x")
