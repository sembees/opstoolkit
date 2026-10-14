#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""test_pxe_wipe_disks_schema.py — disk_config.wipe_disks 的 schema 侧行为用例。

被锁的行为（schemas.PxeDiskConfigIn.wipe_disks，2026-10-14 落地）：
合法盘名通过（/dev/ 前缀剥掉归一）/ 重复条目 422 / "sd*" 422 / 空串 422 /
注入与类型错误 422 / 缺省与 null 惰性（只清目标盘）/
wipe_disks 不算结构化键（disk_scheme=custom 不被"必须有 / 分区"误拒）。

环境变量 WIPE_PROBE_MODE（默认 after）：
  after（默认） → 跑补丁后的验收项（本仓库现状）；
  before        → 只跑"打补丁前"的缺口证据用例（需要一份未打补丁的 backend
                  副本在 PYTHONPATH 上），这些用例在 after 模式下全部 skip。
缺口证据的意义：证明"没有 schema 声明时 wipe_disks 会以 extra 键直通保存、
保存期零校验"，即本补丁修的就是它。
"""
import os

import pytest
from pydantic import ValidationError

from app.core.schemas import PxeDiskConfigIn, PxeProfileIn

MODE = os.environ.get("WIPE_PROBE_MODE", "after")

AFTER = pytest.mark.skipif(MODE != "after", reason="仅在已打补丁副本(AFTER)上跑")
BEFORE = pytest.mark.skipif(MODE != "before", reason="仅在未打补丁基线(BEFORE)上跑")


def profile_in(disk_config, os_type="debian", disk_scheme="lvm"):
    return PxeProfileIn(name="t", os_type=os_type, disk_scheme=disk_scheme,
                        disk_config=disk_config)


def first_msg(exc) -> str:
    v = exc.value if hasattr(exc, "value") else exc   # 兼容 ExceptionInfo / ValidationError
    return str(v.errors()[0].get("msg", ""))


# ── 验收 1：合法盘名通过（/dev/ 前缀容忍并剥掉，归一后保存）─────────────────
@AFTER
def test_valid_disk_names_pass():
    dc = PxeDiskConfigIn.model_validate(
        {"disk": "sda", "wipe_disks": ["sdb", "/dev/nvme0n1", "mmcblk0p2"]})
    dump = dc.model_dump(exclude_unset=True)
    assert dump["wipe_disks"] == ["sdb", "nvme0n1", "mmcblk0p2"], dump
    # PxeProfileIn 全链（create/update 的入参模型）：归一化值原样落进 disk_config
    body = profile_in({"disk": "sda", "wipe_disks": ["sdb", "/dev/nvme0n1"]},
                      os_type="debian")
    assert body.disk_config == {"disk": "sda", "wipe_disks": ["sdb", "nvme0n1"]}


# ── 验收 2：重复条目 ⇒ 422（监工拍板：明确报错优于静默去重）────────────────
@AFTER
def test_duplicate_entries_422():
    with pytest.raises(ValidationError) as e:
        PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": ["sdb", "sdb"]})
    assert "重复" in first_msg(e), first_msg(e)
    # /dev/ 前缀剥掉后同名，同样算重复（同一块盘）
    with pytest.raises(ValidationError) as e:
        PxeDiskConfigIn.model_validate(
            {"disk": "sda", "wipe_disks": ["/dev/sdb", "sdb"]})
    assert "重复" in first_msg(e), first_msg(e)


# ── 验收 3："sd*"（通配/白名单外字符）⇒ 422 ────────────────────────────────
@AFTER
def test_glob_sd_star_422():
    with pytest.raises(ValidationError) as e:
        PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": ["sd*"]})
    assert "*" in first_msg(e), first_msg(e)


# ── 验收 4：空串 ⇒ 422（"/dev/" 剥完为空同样拒）────────────────────────────
@AFTER
def test_empty_string_422():
    # 真空串（及 "/dev/" 剥完为空）→ "不能为空"；纯空白 → 白名单按坏字符拦截，同样 422
    for bad in ([""], ["/dev/"]):
        with pytest.raises(ValidationError) as e:
            PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": bad})
        assert "不能为空" in first_msg(e), (bad, first_msg(e))
    for bad in (["   "], ["\t"]):
        with pytest.raises(ValidationError) as e:
            PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": bad})
        assert "含不允许的字符" in first_msg(e), (bad, first_msg(e))


# ── 附加：注入形态 / 类型错误一律 422 ──────────────────────────────────────
@AFTER
def test_injection_and_type_shapes_422():
    bads = ["sdb; rm -rf /", "sdb$(reboot)", 'sdb"x', "sdb y", "sdb\tz"]
    for w in bads:
        with pytest.raises(ValidationError) as e:
            PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": [w]})
        assert "含不允许的字符" in first_msg(e) or "不能为空" in first_msg(e), (w, first_msg(e))
    with pytest.raises(ValidationError):
        PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": [1]})
    with pytest.raises(ValidationError) as e:      # 字符串而非数组
        PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": "sdb"})
    assert "必须是数组" in first_msg(e), first_msg(e)


# ── 附加：缺省/None = 只清目标盘，不改变任何既有行为 ───────────────────────
@AFTER
def test_default_empty_is_inert():
    dc = PxeDiskConfigIn.model_validate({"disk": "sda"})
    assert dc.model_dump(exclude_unset=True) == {"disk": "sda"}      # 不出现新键
    assert profile_in({"disk": "sda"}).disk_config == {"disk": "sda"}
    dc = PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": None})
    assert dc.model_dump(exclude_unset=True)["wipe_disks"] == []     # null 归一为 []


# ── 附加（HUNK_S3）：wipe_disks 不激活布局规则；结构化键的既有规则不放松 ──
@AFTER
def test_wipe_disks_not_structural_custom_scheme():
    # 仅有 {disk, wipe_disks}：scheme=custom 不再被"必须有 / 分区"误拒
    body = profile_in({"disk": "sda", "wipe_disks": ["sdb"]},
                      os_type="debian", disk_scheme="custom")
    assert body.disk_config["wipe_disks"] == ["sdb"]
    # 结构化键照旧拒绝（守卫只对 wipe_disks 放行，不放松既有规则）
    with pytest.raises(ValidationError) as e:
        profile_in({"disk": "sda", "target": {"mode": "name", "name": "sda"}},
                   os_type="debian", disk_scheme="custom")
    assert "/ 分区" in first_msg(e), first_msg(e)


# ── 附加：HTTP 层真实 422（FastAPI TestClient，进程内、离线）───────────────
@AFTER
def test_http_layer_returns_422():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()

    @app.post("/p")
    def p(body: PxeProfileIn):
        return {"disk_config": body.disk_config}

    c = TestClient(app)
    r = c.post("/p", json={"name": "t",
                           "disk_config": {"disk": "sda",
                                           "wipe_disks": ["sdb", "sdb"]}})
    assert r.status_code == 422, (r.status_code, r.text[:200])
    r = c.post("/p", json={"name": "t",
                           "disk_config": {"disk": "sda", "wipe_disks": ["sd*"]}})
    assert r.status_code == 422, (r.status_code, r.text[:200])
    r = c.post("/p", json={"name": "t",
                           "disk_config": {"disk": "sda", "wipe_disks": ["sdb"]}})
    assert r.status_code == 200 and \
        r.json()["disk_config"] == {"disk": "sda", "wipe_disks": ["sdb"]}, r.text[:200]


# ── BEFORE 基线：无 schema 声明时 extra="allow" 让非法值直通保存（缺口证据）──
@BEFORE
def test_before_extra_passthrough_unvalidated():
    for w in (["sd*"], ["sdb", "sdb"], [""], ["sdb; rm -rf /"]):
        dc = PxeDiskConfigIn.model_validate({"disk": "sda", "wipe_disks": w})
        assert dc.model_dump(exclude_unset=True)["wipe_disks"] == w   # 原样入库


@BEFORE
def test_before_default_is_inert_too():
    assert PxeDiskConfigIn.model_validate({"disk": "sda"}).model_dump(
        exclude_unset=True) == {"disk": "sda"}
    assert profile_in({"disk": "sda"}).disk_config == {"disk": "sda"}


@BEFORE
def test_before_custom_scheme_misrejects_wipe_only():
    # HUNK_S3 修的正是这个：{disk, wipe_disks} + scheme=custom 被"必须有 / 分区"误拒
    with pytest.raises(ValidationError) as e:
        profile_in({"disk": "sda", "wipe_disks": ["sdb"]},
                   os_type="debian", disk_scheme="custom")
    assert "/ 分区" in first_msg(e), first_msg(e)
