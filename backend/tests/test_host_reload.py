"""宿主机 dnsmasq 重载握手（RUNBOOK-STATE §5.41）的回归测试。

要钉死的核心行为：**配置"写下去了"不等于配置"生效了"**。
实测过一次真实的假成功 —— /deploy 返回 ok=true、界面显示"部署完成"，
但正在跑的 dnsmasq 还是旧配置，把全网机器指向一份"拒绝安装"菜单，
装机全部卡在 PXE 循环里。所以：
  · 只认 sha 相等的标记（不能只看"标记变新了"，否则连续两次部署会误判）；
  · 标记缺失/不匹配时必须报失败，绝不能默默当成功。
"""
import importlib
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

dhcp = importlib.import_module("app.core.dhcp")


@pytest.fixture()
def state_file(tmp_path, monkeypatch):
    p = tmp_path / ".opstk-dnsmasq-reload.state"
    monkeypatch.setattr(dhcp, "HOST_RELOAD_STATE", str(p))
    return p


def test_conf_sha_matches_sha256sum_format():
    # 必须与宿主机脚本 `sha256sum <conf> | awk '{print $1}'` 同值，
    # 否则握手永远核不上。
    import hashlib
    content = "interface=ens19\n"
    assert dhcp.conf_sha(content) == hashlib.sha256(content.encode()).hexdigest()
    assert dhcp.conf_sha(content).isascii()


def test_status_none_when_marker_missing(state_file):
    assert dhcp.host_reload_status() is None


def test_status_parses_ok_and_fail(state_file):
    state_file.write_text("OK abc123 1699999999\n", encoding="utf-8")
    st = dhcp.host_reload_status()
    assert st["state"] == "OK" and st["sha"] == "abc123" and st["ts"] == "1699999999"

    state_file.write_text("FAIL test-failed 1699999999\n", encoding="utf-8")
    st = dhcp.host_reload_status()
    assert st["state"] == "FAIL" and "test-failed" in st["reason"]


def test_wait_ok_on_matching_sha(state_file):
    state_file.write_text("OK " + dhcp.conf_sha("x") + " 1\n", encoding="utf-8")
    got = dhcp.wait_host_reload(dhcp.conf_sha("x"), timeout=1.0)
    assert got["ok"] is True


def test_wait_fails_on_stale_sha(state_file):
    """旧的 OK 标记（上一次部署留下的）不能算数 —— 这正是"连续两次部署"的坑。"""
    state_file.write_text("OK " + dhcp.conf_sha("old") + " 1\n", encoding="utf-8")
    got = dhcp.wait_host_reload(dhcp.conf_sha("new"), timeout=0.6)
    assert got["ok"] is False
    assert got["state"]["sha"] == dhcp.conf_sha("old")


def test_wait_fails_when_host_reported_failure(state_file):
    state_file.write_text("FAIL restart-failed 1\n", encoding="utf-8")
    got = dhcp.wait_host_reload("whatever", timeout=0.6)
    assert got["ok"] is False and got["state"]["state"] == "FAIL"


def test_wait_fails_when_no_marker_at_all(state_file):
    """宿主机没装重载单元 → 不能当成功。"""
    got = dhcp.wait_host_reload("whatever", timeout=0.6)
    assert got["ok"] is False and got["state"] is None


def test_container_control_does_not_claim_config_is_live(monkeypatch):
    """容器分支只能声明"我不拥有这个守护进程"，不能暗示配置已生效。"""
    monkeypatch.setattr(dhcp, "_in_container", lambda: True)
    res = dhcp.dhcp_control("restart")
    assert res["managed"] is False
    assert res["reload_delegated"] is True
    assert "重载" in res["msg"]


def test_hint_is_actionable():
    assert "install-opstk-dnsmasq-reload.sh" in dhcp.HOST_RELOAD_HINT
    assert dhcp.HOST_RELOAD_UNIT in dhcp.HOST_RELOAD_HINT
