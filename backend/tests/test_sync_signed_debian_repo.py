# -*- coding: utf-8 -*-
"""backend/scripts/sync_signed_debian_repo.py 的落地用例。

脚本用途（详见脚本 docstring）：为隔离网内的 Debian 全自动装机准备一份
**带官方签名**的本地仓库子集——DVD 直拷的 dists/ 没有 Release.gpg/InRelease，
d-i 校验签名失败会停在 <Change mirror>，所以签名索引必须从带官方签名的镜像
（默认清华 TUNA）原样搬回本地。

本用例钉三条：
  · 脚本可执行（--help 出用法；--dry-run 全程不联网也能跑）；
  · 真同步的统计行含「取得」字样，且签名三件套 Release/Release.gpg/InRelease 落盘
    （这正是 DVD 直拷仓库缺、d-i 会因此停在 <Change mirror> 的东西）；
  · 幂等：同一 dest 跑两遍，第二遍「跳过」总数 == 第一遍「取得」总数
    （镜像 404 的缺失项两侧同数、不参与）。

联网口径：真同步路径**先探测镜像**，不可达就 pytest.skip 并写明原因，
绝不把网络失败当成功。镜像可用环境变量 SYNC_SIGNED_DEBIAN_MIRROR 覆盖
（隔离网内指向内网带签名的镜像即可复用本用例）。
组件刻意收窄为 --components main：索引文件只有 ~26 个 / ~20 MiB，
避免用例每次拖全量 4 组件（54 文件 / 29.07 MiB 那条是人工复算跑过的口径）。
"""
import os
import pathlib
import re
import shutil
import subprocess
import sys

import pytest

SCRIPT = (pathlib.Path(__file__).resolve().parents[1]
          / "scripts" / "sync_signed_debian_repo.py")
MIRROR = os.environ.get(
    "SYNC_SIGNED_DEBIAN_MIRROR",
    "https://mirrors.tuna.tsinghua.edu.cn/debian").rstrip("/")
# 与脚本的每文件集统计行同构：「取得 N  跳过 N  缺失(镜像404) N  失败 N」
# （dry-run 时第一个词是「拟取得」）。
STAT_RE = re.compile(
    r"^\s*(?:拟取得|取得)\s+(\d+)\s+跳过\s+(\d+)\s+缺失\(镜像404\)\s+(\d+)\s+失败\s+(\d+)\s*$",
    re.MULTILINE)


def _run(args, timeout=900):
    return subprocess.run(
        [sys.executable, str(SCRIPT)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout)


def _stat_totals(out):
    rows = STAT_RE.findall(out)
    assert rows, "没有解析到任何统计行，原始输出：\n" + out
    return (sum(int(r[0]) for r in rows),   # 取得/拟取得
            sum(int(r[1]) for r in rows),   # 跳过
            sum(int(r[2]) for r in rows),   # 缺失(镜像404)
            sum(int(r[3]) for r in rows))   # 失败


def _probe_mirror(tmp_path):
    """联网探测镜像可达性；不可达时返回 skip 原因（可达返回 None）。"""
    if shutil.which("curl") is None:
        return ("curl 不在 PATH：脚本本体只依赖标准库 + curl，"
                "本机无法真同步，跳过联网用例")
    probe = tmp_path / "probe-Release"
    r = subprocess.run(
        ["curl", "-f", "-sS", "-L", "--connect-timeout", "10",
         "--max-time", "30", "-o", str(probe), MIRROR + "/dists/trixie/Release"],
        capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        return ("探测镜像不可达（GET %s/dists/trixie/Release，curl exit %d：%s）"
                "——无法真同步，跳过联网用例（不把网络失败当成功）"
                % (MIRROR, r.returncode, (r.stderr or "").strip()))
    return None


def test_script_entrypoint_is_runnable():
    """①脚本可执行：文件在、--help 出用法、缺必选 --dest 被拒（exit 2）。"""
    assert SCRIPT.is_file(), SCRIPT
    r = _run(["--help"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "--dest" in r.stdout and "--dry-run" in r.stdout
    r2 = _run([])
    assert r2.returncode == 2, r2.stdout + r2.stderr
    assert "--dest" in (r2.stderr or "")


def test_dry_run_enumerates_without_downloading(tmp_path):
    """--dry-run 只枚举统计（不联网）：统计行含「取得」（以「拟取得」出现），
    dest 目录一个都不建。计划数 = 2 套系 × (3 签名 + main 6 运行期 + 4 安装器)
    = 26 个文件，逐文件集行合计应与之相等。"""
    dest = tmp_path / "repo"
    r = _run(["--dest", str(dest), "--components", "main",
              "--no-udeb", "--dry-run"])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "本次拟取得" in r.stdout
    assert "取得" in r.stdout                       # ②统计行含「取得」字样
    fetched, skipped, missing, failed = _stat_totals(r.stdout)
    assert fetched == 26 and failed == 0
    assert not dest.exists(), "dry-run 不应建目录/落盘"


def test_real_sync_idempotent(tmp_path):
    """真同步（联网才跑，取不到就 skip）+ 幂等：第二遍「跳过」总数 ==
    第一遍「取得」总数，且第二遍不再取得任何文件。"""
    reason = _probe_mirror(tmp_path)
    if reason:
        pytest.skip(reason)

    dest = tmp_path / "repo"
    args = ["--dest", str(dest), "--components", "main", "--no-udeb"]
    r1 = _run(args)
    assert r1.returncode == 0, "第一遍真同步失败：\n%s\n%s" % (r1.stdout, r1.stderr)
    assert "本次取得" in r1.stdout                  # ②统计行含「取得」字样
    f1, _s1, m1, fail1 = _stat_totals(r1.stdout)
    assert f1 > 0 and fail1 == 0
    # 签名三件套必须原样落盘（这正是 DVD 仓库的致命缺口）
    for name in ("Release", "Release.gpg", "InRelease"):
        p = dest / "dists" / "trixie" / name
        assert p.is_file() and p.stat().st_size > 0, p

    r2 = _run(args)
    assert r2.returncode == 0, "第二遍失败：\n%s\n%s" % (r2.stdout, r2.stderr)
    f2, s2, m2, fail2 = _stat_totals(r2.stdout)
    assert f2 == 0, "第二遍不该再取得任何文件（幂等）：\n" + r2.stdout
    assert s2 == f1, ("第二遍「跳过」%d != 第一遍「取得」%d（幂等被破坏）"
                      % (s2, f1))
    assert fail2 == 0 and m2 == m1
