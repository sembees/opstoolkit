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


def test_conf_sha_matches_bytes_actually_written(tmp_path, monkeypatch):
    """R1 的 #5：真正的不变量是"conf_sha(字符串) == 落盘字节的 sha256"。

    原来那条只断言 conf_sha 等于它自己的定义（同义反复）。这里真的过一遍 write_conf，
    再对**磁盘上的字节**取 sha —— 一旦 write_conf 哪天改写内容（补换行/换编码/改换行符），
    宿主机算出的 sha 就永远匹配不上，表现为"每次部署都超时"，而报错却指向"去装重载单元"。
    """
    import hashlib
    monkeypatch.setattr(dhcp, "CONF_DIR", str(tmp_path))
    content = "interface=ens19\ndhcp-range=192.168.199.100,192.168.199.200,12h\n"
    assert dhcp.write_conf("opstk-pxe.conf", content) is True
    on_disk = (tmp_path / "opstk-pxe.conf").read_bytes()
    assert dhcp.conf_sha(content) == hashlib.sha256(on_disk).hexdigest()


def test_wait_rejects_stale_marker_even_with_matching_sha(state_file):
    """R1 的 #2：内容相同的连续两次部署不能命中上一次的陈旧标记。

    只比对 sha 时，第二次会在宿主机还没做任何事之前就"成功" —— 整条校验被跳过。
    所以 wait_host_reload 还要看标记时间戳是否 >= 本次部署开始的时刻。
    """
    sha = dhcp.conf_sha("same")
    state_file.write_text("OK %s 1000\n" % sha, encoding="utf-8")
    # 本次部署开始于 2000：标记（1000）太旧，不算数
    got = dhcp.wait_host_reload(sha, timeout=0.6, not_before=2000)
    assert got["ok"] is False
    # 时间戳够新才算
    state_file.write_text("OK %s 3000\n" % sha, encoding="utf-8")
    got = dhcp.wait_host_reload(sha, timeout=0.6, not_before=2000)
    assert got["ok"] is True


def test_invalidate_removes_stale_marker(state_file):
    state_file.write_text("OK abc 1\n", encoding="utf-8")
    assert dhcp.invalidate_host_reload_state() is True
    assert dhcp.host_reload_status() is None
    # 本来就不存在也算成功（幂等）
    assert dhcp.invalidate_host_reload_state() is True


def test_unreadable_state_is_not_reported_as_missing(tmp_path, monkeypatch):
    """R1 的 #9：权限/SELinux 读失败不能伪装成"没装单元"，那会把排查引向错误方向。"""
    d = tmp_path / "state-as-dir"          # 用目录冒充文件，读到的是 IsADirectoryError
    d.mkdir()
    monkeypatch.setattr(dhcp, "HOST_RELOAD_STATE", str(d))
    st = dhcp.host_reload_status()
    assert st is not None and st["state"] == "UNREADABLE" and st["err"]


def test_malformed_ok_line_is_not_accepted(state_file):
    """宿主机脚本理论上不写空 sha，但真出现时"OK <时间>"会被错位解析成 sha=时间戳。"""
    state_file.write_text("OK 1699999999\n", encoding="utf-8")
    st = dhcp.host_reload_status()
    assert st["state"] == "UNREADABLE"
