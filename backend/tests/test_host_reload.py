"""宿主机 dnsmasq 重载握手（RUNBOOK-STATE §5.41）的回归测试。

要钉死的核心行为：**配置"写下去了"不等于配置"生效了"**。
实测过一次真实的假成功 —— /deploy 返回 ok=true、界面显示"部署完成"，
但正在跑的 dnsmasq 还是旧配置，把全网机器指向一份"拒绝安装"菜单，
装机全部卡在 PXE 循环里。所以：
  · 只认 sha 相等的标记（不能只看"标记变新了"，否则连续两次部署会误判）；
  · 标记缺失/不匹配时必须报失败，绝不能默默当成功。
"""
import importlib
import os
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


def test_fail_line_timestamp_is_parsed(state_file):
    """真机实测的缺陷：FAIL 行不解析时刻 ⇒ 预检无法判断"这个 FAIL 是不是我这次探测
    引起的"，只能等满超时，把"配置本身非法"报成"重载单元没反应"。

    宿主脚本写的是 `FAIL <原因> <时刻>`，原因不含空格（脚本用 bash 参数展开抹掉了空白）。
    """
    state_file.write_text(
        "FAIL test-failed:dnsmasq:badoptionatline1 1790582574\n", encoding="utf-8")
    st = dhcp.host_reload_status()
    assert st["state"] == "FAIL"
    assert st["reason"] == "test-failed:dnsmasq:badoptionatline1"
    assert st["ts"] == "1790582574"
    # 格式意外时不能把尾部乱码当时刻（宁可空着）
    state_file.write_text("FAIL some-reason not-a-time\n", encoding="utf-8")
    st = dhcp.host_reload_status()
    assert st["ts"] == "" and st["reason"] == "some-reason not-a-time"


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


def test_write_conf_is_in_place_so_the_host_unit_fires(tmp_path, monkeypatch):
    """R4 真机实测的硬约束：写配置必须**原地写**（inode 不变）。

    systemd 的 PathChanged/PathModified 不吃 IN_ATTRIB（`touch`/`os.utime` 不触发），
    对 rename 替换也不可靠（watch 挂在被换掉的 inode 上，部署会偶发卡到超时）。
    只有原地写入的 IN_CLOSE_WRITE 稳定触发。曾经为了"原子"改成 tmp+rename，
    结果是**部署偶发不生效** —— 比半截文件更糟，所以这里钉死 inode 不变。
    """
    import hashlib
    monkeypatch.setattr(dhcp, "CONF_DIR", str(tmp_path))
    assert dhcp.write_conf("opstk-pxe.conf", "OLD\n") is True
    ino = os.stat(str(tmp_path / "opstk-pxe.conf")).st_ino
    content = "interface=ens19\ndhcp-range=192.168.199.100,192.168.199.200,12h\n"
    assert dhcp.write_conf("opstk-pxe.conf", content) is True
    p = tmp_path / "opstk-pxe.conf"
    if os.name != "nt":
        assert p.stat().st_ino == ino, "inode 变了 = 用了 rename = 宿主机可能收不到事件"
    assert dhcp.conf_sha(content) == hashlib.sha256(p.read_bytes()).hexdigest()
    # 不许留下临时文件垃圾
    assert sorted(x.name for x in tmp_path.iterdir()) == ["opstk-pxe.conf"]


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


# ── R3：部署前的预检（dhcp.reload_preflight）────────────────────────────────
# 为什么要有它：原来的顺序是"先落盘、再核对重载"，链路坏掉时调用方要等到超时才
# 失败，而**文件已经改完**（磁盘上一半新一半旧，网络处于不一致状态）。
# 预检把这类失败挪到落盘之前：状态目录没挂、单元没装/是旧版、单元不响应，
# 都能在写任何文件之前判定，失败即"什么都没动"。


@pytest.fixture()
def preflight_env(tmp_path, monkeypatch):
    """把预检会碰的三样东西全部指向 tmp：状态目录、标记文件、被监视的配置。

    必须这样做：预检的活体探测会去碰 /etc/dnsmasq.d/opstk-pxe.conf 的 mtime，
    测试绝不能在真实的宿主机配置上做这件事。
    """
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(dhcp, "HOST_RELOAD_STATE_DIR", str(state))
    monkeypatch.setattr(dhcp, "HOST_RELOAD_STATE", str(state / ".dnsmasq-reload.state"))
    monkeypatch.setattr(dhcp, "HOST_RELOAD_LINK", str(state / ".dnsmasq-reload.link"))
    monkeypatch.setattr(dhcp, "_in_container", lambda: True)
    return {
        "dir": state,
        "conf": tmp_path / "opstk-pxe.conf",
        "state": state / ".dnsmasq-reload.state",
        "link": state / ".dnsmasq-reload.link",
    }


def _write_state_later(path, delay=0.15, line=None):
    """模拟宿主机脚本：过一会儿写出状态文件（活体探测要等的就是它）。"""
    import threading
    import time as _time

    def run():
        _time.sleep(delay)
        pathlib.Path(path).write_text(
            line if line is not None else ("OK deadbeef %d\n" % int(_time.time())),
            encoding="utf-8")

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def _preflight(env, timeout=0.6, make_conf=True):
    if make_conf and not env["conf"].exists():
        env["conf"].write_text("interface=ens19\n", encoding="utf-8")
    return dhcp.reload_preflight(str(env["conf"]), timeout=timeout)


def test_link_status_parsing(preflight_env):
    link = preflight_env["link"]
    assert dhcp.host_reload_link_status()["version"] == 0        # 不存在
    link.write_text("RUN 2 1699999999\n", encoding="utf-8")
    st = dhcp.host_reload_link_status()
    assert st["version"] == 2 and st["ts"] == "1699999999"
    link.write_text("garbage\n", encoding="utf-8")
    assert dhcp.host_reload_link_status()["version"] < 0


def test_preflight_skipped_outside_container(preflight_env, monkeypatch):
    monkeypatch.setattr(dhcp, "_in_container", lambda: False)
    res = _preflight(preflight_env)
    assert res["ok"] is True and res["skipped"] is True
    assert res["reason"] == "not-delegated"


def test_preflight_reports_missing_state_dir(preflight_env, monkeypatch, tmp_path):
    monkeypatch.setattr(dhcp, "HOST_RELOAD_STATE_DIR", str(tmp_path / "nope"))
    res = _preflight(preflight_env)
    assert res["ok"] is False and res["reason"] == "state-dir-missing"
    assert "compose" in res["hint"]          # 提示要能照做


def test_preflight_requires_link_marker(preflight_env):
    """没装单元 / 装的是不写标记的旧脚本 —— 都必须在**落盘之前**挡住。"""
    res = _preflight(preflight_env)
    assert res["ok"] is False and res["reason"] == "unit-not-installed"
    assert "install-opstk-dnsmasq-reload.sh" in res["hint"]


def test_preflight_rejects_old_script_version(preflight_env):
    preflight_env["link"].write_text("RUN 1 1699999999\n", encoding="utf-8")
    res = _preflight(preflight_env)
    assert res["ok"] is False and res["reason"] == "script-outdated"
    assert "旧版" in res["hint"]


def test_preflight_unreadable_link_is_not_reported_as_missing(preflight_env):
    """读不了 ≠ 没装：把排查引到"去装单元"会完全走错方向。"""
    preflight_env["link"].mkdir()
    res = _preflight(preflight_env)
    assert res["ok"] is False and res["reason"] == "link-unreadable"


def test_preflight_probe_succeeds_on_fresh_state(preflight_env):
    # 用应用侧要求的最低版本写标记（而不是写死数字）：抬版本时这些用例不该集体变红
    preflight_env["link"].write_text(
        "RUN %d 1699999999\n" % dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION, encoding="utf-8")
    _write_state_later(preflight_env["state"])
    res = _preflight(preflight_env)
    assert res["ok"] is True and res["probed"] is True and res["reason"] == ""


def test_preflight_probe_does_not_change_conf_content(preflight_env):
    """探针只能"写回同样的字节"，绝不能改内容（那会被宿主机当成一次真部署）。

    而且**必须真写**：真机实测 systemd 的 PathChanged/PathModified 不含 IN_ATTRIB，
    `touch`/`os.utime` 根本不触发 path 单元 → 每个部署都会"预检超时"。
    """
    # 用应用侧要求的最低版本写标记（而不是写死数字）：抬版本时这些用例不该集体变红
    preflight_env["link"].write_text(
        "RUN %d 1699999999\n" % dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION, encoding="utf-8")
    conf = preflight_env["conf"]
    conf.write_text("interface=ens19\ndhcp-range=192.168.199.100,192.168.199.200,12h\n",
                    encoding="utf-8")
    before = conf.read_bytes()
    inode_before = os.stat(str(conf)).st_ino
    _write_state_later(preflight_env["state"])
    assert _preflight(preflight_env, make_conf=False)["ok"] is True
    assert conf.read_bytes() == before
    # 探针必须是**原地写**（inode 不变）—— rename 替换会让宿主机偶发收不到事件
    if os.name != "nt":
        assert os.stat(str(conf)).st_ino == inode_before



def test_preflight_probe_rejects_stale_state(preflight_env):
    """宿主机留下的**旧**标记不算"有响应"（否则单元早死了也照样过）。"""
    # 用应用侧要求的最低版本写标记（而不是写死数字）：抬版本时这些用例不该集体变红
    preflight_env["link"].write_text(
        "RUN %d 1699999999\n" % dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION, encoding="utf-8")
    preflight_env["state"].write_text("OK abc 1000\n", encoding="utf-8")
    res = _preflight(preflight_env)
    assert res["ok"] is False and res["reason"] == "no-response"
    assert "journalctl" in res["hint"]


def test_preflight_accepts_a_fresh_fail_as_liveness(preflight_env):
    """链路"活着"不等于"配置合法"：宿主机报 FAIL（--test 不通过）也证明
    单元在触发、脚本在跑 —— 预检必须放行，把"新配置合不合法"留给后面那一步。

    真机实测：不认 FAIL 时，磁盘上一份坏配置会让**每次**部署都卡在预检超时，
    连"重新部署一次即可自愈"这条路都被堵死。
    """
    import time as _time
    # 用应用侧要求的最低版本写标记（而不是写死数字）：抬版本时这些用例不该集体变红
    preflight_env["link"].write_text(
        "RUN %d 1699999999\n" % dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION, encoding="utf-8")
    preflight_env["conf"].write_text("interface=ens19\n", encoding="utf-8")
    # 模拟"宿主机脚本跑到 --test 就失败"：只有 FAIL，没有 OK
    _write_state_later(preflight_env["state"], delay=0.15,
                       line="FAIL test-failed:badoption %d\n" % int(_time.time() + 1))
    res = _preflight(preflight_env, make_conf=False)
    assert res["ok"] is True and res["probed"] is True
    assert res["state"]["state"] == "FAIL"


def test_preflight_probe_timeout_is_a_failure(preflight_env):
    # 用应用侧要求的最低版本写标记（而不是写死数字）：抬版本时这些用例不该集体变红
    preflight_env["link"].write_text(
        "RUN %d 1699999999\n" % dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION, encoding="utf-8")
    res = _preflight(preflight_env)
    assert res["ok"] is False and res["reason"] == "no-response"


def test_preflight_skips_probe_when_conf_missing(preflight_env):
    """还没部署过 → 没有可碰的监视目标 → 只做静态判断（不为了探测造假配置）。"""
    # 用应用侧要求的最低版本写标记（而不是写死数字）：抬版本时这些用例不该集体变红
    preflight_env["link"].write_text(
        "RUN %d 1699999999\n" % dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION, encoding="utf-8")
    res = _preflight(preflight_env, make_conf=False)
    assert res["ok"] is True and res["probed"] is False
    assert res["reason"] == "no-target-to-probe"
    assert not preflight_env["conf"].exists()


@pytest.mark.parametrize("reason,expected", [
    ("test-failed:badoption", True),
    ("test-failed:bad-address", True),
    ("missing-conf", True),
    ("empty-sha", True),
    # 这些都不能证明"没重启过" —— 回滚会让磁盘与守护进程更不一致
    ("restart-failed", False),
    ("not-active", False),
    ("config-changed", False),
    ("lock-timeout", False),
    ("script-error-line47", False),
    ("", False),
])
def test_only_pre_restart_failures_allow_rollback(reason, expected):
    assert dhcp.reload_failure_is_pre_restart(
        {"state": "FAIL", "reason": reason}) is expected


def test_missing_or_unreadable_state_never_allows_rollback():
    assert dhcp.reload_failure_is_pre_restart(None) is False
    assert dhcp.reload_failure_is_pre_restart({"state": "UNREADABLE", "reason": ""}) is False
    assert dhcp.reload_failure_is_pre_restart(
        {"state": "OK", "sha": "x", "ts": "1"}) is False


def test_retrying_wait_nudges_when_the_edge_was_swallowed(state_file, tmp_path):
    """边沿事件被合并时必须**补触发**（真机实测：部署偶发卡在等重载直到超时）。

    模拟：第一次等待超时（宿主机什么都没写），"补触发"动作把状态写出来，
    第二次等待就成功 —— 并要求补触发恰好发生一次。
    """
    conf = tmp_path / "opstk-pxe.conf"
    conf.write_text("interface=ens19\n", encoding="utf-8")
    sha = dhcp.conf_sha("whatever")
    nudges = []

    def nudge():
        nudges.append(1)
        state_file.write_text("OK %s 9999999999\n" % sha, encoding="utf-8")
        return True

    got, n = dhcp.wait_host_reload_retrying(sha, str(conf), timeout=3.0,
                                            not_before=1, nudge=nudge)
    assert got["ok"] is True
    assert n == 1 and len(nudges) == 1
    # 补触发过程不能改动配置内容
    assert conf.read_text(encoding="utf-8") == "interface=ens19\n"


def test_retrying_wait_gives_up_and_reports_last_state(state_file, tmp_path):
    conf = tmp_path / "opstk-pxe.conf"
    conf.write_text("interface=ens19\n", encoding="utf-8")
    got, n = dhcp.wait_host_reload_retrying("nope", str(conf), timeout=2.0, not_before=1,
                                            nudge=lambda: True)
    assert got["ok"] is False
    assert n >= 1                      # 至少补触发过一次才算尽力


def test_rewrite_conf_unchanged_keeps_bytes_and_inode(tmp_path):
    p = tmp_path / "opstk-pxe.conf"
    p.write_text("interface=ens19\nbind-interfaces\n", encoding="utf-8")
    before = p.read_bytes()
    ino = p.stat().st_ino
    ok, err = dhcp.rewrite_conf_unchanged(str(p))
    assert ok is True and err == ""
    assert p.read_bytes() == before
    if os.name != "nt":
        assert p.stat().st_ino == ino, "必须原地写（rename 会让宿主机收不到事件）"


def _host_file(name: str):
    """读宿主机侧的脚本/单元文件；找不到时返回 None。

    ★ 外部审查 U6-F6：这两条契约用例以前在容器里**静默 skip**（容器只挂了 backend/），
    等于从未被守卫 —— 而它们钉的正是"改宿主脚本时最容易忘"的两件事。
    现在 compose 把 `/opt/opstk/deploy/host` 只读挂进 `/app/deploy/host`，容器里也能真跑。
    另外加了 fail-closed：只要**目录在**（`OPSTK_REPO` 指了完整仓库，或 `/app/deploy/host`
    被挂进来）却找不到那个文件，就直接失败 —— 目录在而文件没了，本身就是契约被破坏。
    """
    here = pathlib.Path(__file__).resolve()
    roots = [here.parents[2], pathlib.Path("/app")]
    if os.environ.get("OPSTK_REPO"):
        roots.insert(0, pathlib.Path(os.environ["OPSTK_REPO"]))
    for r in roots:
        d = r / "deploy" / "host"
        f = d / name
        if f.is_file():
            return f.read_text(encoding="utf-8")
        if d.is_dir():
            raise AssertionError("宿主目录 %s 存在，但缺少 %s（契约被破坏）" % (d, name))
    return None


def test_script_version_contract_is_documented():
    """宿主脚本里写的 SCRIPT_VERSION 必须 >= 应用侧要求的最低版本，
    否则部署会永远卡在 preflight（这条约束很容易在改脚本时忘掉）。

    容器里通过 `/app/deploy/host`（compose 的只读挂载）看到真文件；确实没有仓库的
    环境才 skip。
    """
    import re as _re
    sh_text = _host_file("opstk-dnsmasq-reload.sh")
    if sh_text is None:
        pytest.skip("宿主脚本不在本环境（容器没挂 deploy/host，也没有完整仓库）")
    # 注意行尾：检出到 Windows 时是 CRLF，正则必须容忍 `\r`（踩过：`$` 卡在 \r 前）
    m = _re.search(r"^SCRIPT_VERSION=(\d+)\s*$", sh_text, _re.M)
    assert m, "宿主脚本里没有 SCRIPT_VERSION"
    assert int(m.group(1)) >= dhcp.HOST_RELOAD_MIN_SCRIPT_VERSION
    assert "HOST_RELOAD_LINK" in sh_text or ".dnsmasq-reload.link" in sh_text


def test_reload_service_disables_start_rate_limit():
    """真机实测（2026-09）：systemd 默认"10 秒内最多启动 5 次"，而一次 /deploy 就会
    触发重载服务两次（预检碰 mtime + 真正写 conf），撞上限流后服务**根本不跑** ——
    状态文件没人写，应用读不到 FAIL，于是"无法证明没重启过 → 不能回滚"，
    磁盘被留在不一致状态。这条用例钉死那个开关（很容易被人"清理"掉）。
    """
    here = pathlib.Path(__file__).resolve()
    unit = _host_file("opstk-dnsmasq-reload.service")
    if unit is None:
        pytest.skip("宿主单元不在本环境（容器没挂 deploy/host，也没有完整仓库）")
    assert "StartLimitIntervalSec=0" in unit
    # SuccessExitStatus=0 1 也必须在：脚本"失败"是设计的一部分（写 FAIL 状态），
    # 不能让它因为退出码 1 触发 systemd 的失败处理。
    assert "SuccessExitStatus=0 1" in unit
