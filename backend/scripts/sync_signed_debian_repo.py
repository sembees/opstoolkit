#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sync_signed_debian_repo.py —— 离线准备「带官方签名」的 Debian 本地仓库子集。

背景（装机机在隔离网内，只能用本地 HTTP 仓库）：
Debian 官方 DVD 的 dists/ 不带 Release.gpg/InRelease，d-i 镜像校验会失败并停在
<Change mirror>。因此索引与签名必须从带 Debian 官方签名的镜像站（默认清华 TUNA）
【原样】搬回本地，本地路径与 Release/Filename 完全一致，签名与哈希才自洽。

同步内容（顺序即依赖，编号对应统计输出）：
  1. dists/<suite>{,-updates}/{Release,Release.gpg,InRelease}（原样，不重签不重排）
  2. 各组件 binary-amd64 与 i18n 的运行期索引：
     Packages, Packages.gz, Packages.xz, Release, Translation-en, Translation-en.xz
  3. 各组件 debian-installer/binary-amd64 的安装器索引（同 Packages 文件集）
  4. 安装器 Packages 索引里 Filename: 列出的全部 .udeb（--no-udeb 关闭）
  5. <suite>-updates 同 1-3（并入 1-3 的套系循环）；不同步 -security
     （Debian 主归档里没有该 suite，真机/镜像实测 404）

约定：
  * 落盘路径 = <dest>/<Filename 经 URL 解码>（%2B -> +，避免字面 %2B 导致 404 的坑）；
  * 幂等：本地已有该文件即跳过，只下缺的；
  * 并发 curl 数上限 8；单文件失败重试（共 3 次尝试，1s/2s 退避）；
  * HTTP 404 视为「镜像上没有该文件」（缺失），单独计数、不判失败——
    Debian 主归档本就没有全部未压缩变体/子目录；但第 1 类签名三件套任一 404
    即判失败（那正是 DVD 仓库的致命缺口）；
  * 默认不下载 .deb（数 GB）；显式 --with-debs 才按运行期索引同步 .deb；
  * 本脚本不做签名/哈希校验：签名文件原样落盘，校验交给 apt / d-i。
  * 仅依赖 Python 标准库 + curl 子进程。
"""

import argparse
import gzip
import lzma
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote, unquote

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/debian"
DEFAULT_SUITE = "trixie"
DEFAULT_COMPONENTS = "main,contrib,non-free,non-free-firmware"
ARCH = "amd64"                      # 硬约束：装机机 amd64
MAX_JOBS = 8                        # 硬约束：并发 <= 8
ATTEMPTS = 3                        # 单文件最多尝试次数
CURL_CONNECT_TIMEOUT = "20"
CURL_MAX_TIME = "300"

RELEASE_FILES = ("Release", "Release.gpg", "InRelease")
PKG_INDEX_FILES = ("Packages", "Packages.gz", "Packages.xz", "Release")
I18N_INDEX_FILES = ("Translation-en", "Translation-en.xz")


class SetStat(object):
    """一个文件集的统计。"""

    def __init__(self):
        self.fetched = 0      # 本次取得（dry-run 时 = 拟取得）
        self.skipped = 0      # 本地已有，跳过
        self.missing = 0      # 镜像上没有（HTTP 404），非致命
        self.failed = []      # [(相对路径, 错误信息)]
        self.bytes = 0        # 本次取得字节数


def human(n):
    x = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if x < 1024.0 or unit == "TiB":
            return ("%.0f %s" if unit == "B" else "%.2f %s") % (x, unit)
        x /= 1024.0
    return "%d B" % n


def curl_fetch(url, dst, attempts=ATTEMPTS):
    """用 curl 下载 url 到 dst，返回 (状态, 错误)。

    状态: "ok" / "http404" / "fail"。先写 .part，成功后原子改名，
    失败不留半截文件冒充已同步。
    """
    part = dst + ".part"
    last_err = ""
    was_404 = False
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            time.sleep(attempt - 1)          # 1s, 2s 退避
        try:
            r = subprocess.run(
                ["curl", "-f", "-sS", "-L",
                 "--connect-timeout", CURL_CONNECT_TIMEOUT,
                 "--max-time", CURL_MAX_TIME,
                 "-o", part, url],
                capture_output=True, text=True, errors="replace")
        except OSError as e:
            return "fail", "无法启动 curl: %s" % e
        if r.returncode == 0:
            try:
                os.replace(part, dst)
                return "ok", ""
            except OSError as e:
                return "fail", "落盘失败: %s" % e
        err = (r.stderr or "").strip().replace("\n", " ")
        if not err:
            err = "curl exit %d" % r.returncode
        last_err = err
        was_404 = (r.returncode == 22 and "404" in err)
        try:
            if os.path.exists(part):
                os.remove(part)
        except OSError:
            pass
    return ("http404" if was_404 else "fail"), last_err


def build_plan(suites, comps):
    """返回 [(文件集名, [仓库相对路径...])]，顺序即依赖顺序（1→2→3）。"""
    release, runtime, installer = [], [], []
    for s in suites:
        for f in RELEASE_FILES:
            release.append("dists/%s/%s" % (s, f))
        for c in comps:
            for f in PKG_INDEX_FILES:
                runtime.append("dists/%s/%s/binary-%s/%s" % (s, c, ARCH, f))
                installer.append("dists/%s/%s/debian-installer/binary-%s/%s"
                                 % (s, c, ARCH, f))
            for f in I18N_INDEX_FILES:
                runtime.append("dists/%s/%s/i18n/%s" % (s, c, f))
    return [
        ("1:签名索引 Release/Release.gpg/InRelease", release),
        ("2:运行期索引 binary-%s+i18n" % ARCH, runtime),
        ("3:安装器索引 debian-installer/binary-%s" % ARCH, installer),
    ]


def find_local_packages_index(dest, base):
    """在本地找 base 下的 Packages 索引，优先 .xz > .gz > 未压缩。"""
    for name in ("Packages.xz", "Packages.gz", "Packages"):
        p = os.path.join(dest, base, name)
        if os.path.exists(p):
            return "%s/%s" % (base, name)
    return None


def parse_filenames(dest, rel):
    """从本地 Packages(.gz/.xz) 索引抽出全部 Filename: 值。"""
    opener = (lzma.open if rel.endswith(".xz")
              else gzip.open if rel.endswith(".gz")
              else open)
    out = []
    with opener(os.path.join(dest, rel), "rt",
                encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith("Filename: "):
                out.append(line.split(":", 1)[1].strip())
    return out


def run_sets(sets, mirror, dest, jobs, dry, verbose, stats):
    """按依赖顺序逐文件集处理：跳过本地已有，其余并发下载。"""
    for setname, rels in sets:
        st = stats.setdefault(setname, SetStat())
        todo = []
        for rel in rels:
            dst = os.path.join(dest, unquote(rel))   # URL 解码后再落盘
            if os.path.exists(dst):
                st.skipped += 1
                if verbose:
                    print("    = 跳过(已有) %s" % rel)
            else:
                todo.append((rel, dst))
        if todo and not dry:
            for _rel, _dst in todo:                  # 先建父目录，curl 不自己建
                _d = os.path.dirname(_dst)
                if _d and not os.path.isdir(_d):
                    os.makedirs(_d, exist_ok=True)

            def fetch(t):
                return curl_fetch(mirror + "/" + quote(t[0], safe="/"), t[1])

            with ThreadPoolExecutor(max_workers=jobs) as ex:
                for (rel, _dst), (status, err) in zip(todo, ex.map(fetch, todo)):
                    if status == "ok":
                        st.fetched += 1
                        try:
                            st.bytes += os.path.getsize(_dst)
                        except OSError:
                            pass
                        if verbose:
                            print("    + 取得 %s" % rel)
                    elif status == "http404" and not setname.startswith("1:"):
                        st.missing += 1               # 镜像本来就没有，不算失败
                        if verbose:
                            print("    - 缺失(镜像404) %s" % rel)
                    else:
                        if status == "http404":
                            err = "签名文件在镜像上不存在(HTTP 404)"
                        st.failed.append((rel, err))
                        print("    ! 失败 %s\n        %s" % (rel, err))
        elif todo and dry:
            st.fetched += len(todo)
            if verbose:
                for rel, _dst in todo:
                    print("    + 拟取得 %s" % rel)
        print("  [%s]\n      %s %d  跳过 %d  缺失(镜像404) %d  失败 %d"
              % (setname, "拟取得" if dry else "取得",
                 st.fetched, st.skipped, st.missing, len(st.failed)))


def collect_pool_names(dest, suites, comps, kind):
    """从本地 Packages 索引收集 pool 文件名；kind: "udeb" | "deb"。

    udeb 只看 <suite>/main/debian-installer；deb 看全部套系×组件运行期索引。
    返回 (文件集名, [相对路径...])。
    """
    seen, names = set(), []
    if kind == "udeb":
        bases = ["dists/%s/main/debian-installer/binary-%s" % (suites[0], ARCH)]
    else:
        bases = ["dists/%s/%s/binary-%s" % (s, c, ARCH)
                 for s in suites for c in comps]
    for base in bases:
        rel = find_local_packages_index(dest, base)
        if rel is None:
            continue
        for fn in parse_filenames(dest, rel):
            if fn and fn not in seen:
                seen.add(fn)
                names.append(fn)
    return ("4:安装器 .udeb" if kind == "udeb" else "5:.deb(--with-debs)"), names


def summary(stats, dest, dry):
    print("")
    print("==== 统计 ====")
    fails = [(s, r, e) for s, st in stats.items() for (r, e) in st.failed]
    if fails:
        print("失败清单 (%d):" % len(fails))
        for s, r, e in fails:
            print("  - [%s] %s\n      %s" % (s, r, e))
    total = nfiles = 0
    if os.path.isdir(dest):
        for root, _dirs, files in os.walk(dest):
            for f in files:
                nfiles += 1
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    got_n = sum(st.fetched for st in stats.values())
    got_b = sum(st.bytes for st in stats.values())
    if dry:
        print("本次拟取得: %d 文件（dry-run 不下载不建目录）" % got_n)
    else:
        print("本次取得: %d 文件 / %s；dest 总体积: %s（%d 个文件）"
              % (got_n, human(got_b), human(total), nfiles))
    if fails:
        print("结果: FAILED（%d 个文件失败，见失败清单）" % len(fails))
        return 1
    print("结果: OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="sync_signed_debian_repo.py",
        description="离线准备带官方签名的 Debian 本地仓库子集（d-i 可用）。",
        epilog="示例:\n"
               "  python sync_signed_debian_repo.py --dest /srv/repo/debian --no-udeb\n"
               "  python sync_signed_debian_repo.py --dest /srv/repo/debian --with-debs\n"
               "  python sync_signed_debian_repo.py --dest /tmp/x --no-udeb --dry-run")
    ap.add_argument("--mirror", default=DEFAULT_MIRROR,
                    help="带官方签名的镜像站（默认清华 TUNA）")
    ap.add_argument("--suite", default=DEFAULT_SUITE,
                    help="套系名，不带 -updates/-security（默认 trixie）")
    ap.add_argument("--dest", required=True,
                    help="本地仓库根目录（落盘路径与 Filename: 完全一致）")
    ap.add_argument("--components", default=DEFAULT_COMPONENTS,
                    help="逗号分隔组件（默认 main,contrib,non-free,non-free-firmware）")
    ap.add_argument("--jobs", type=int, default=8,
                    help="并发 curl 数，上限 8（默认 8）")
    ap.add_argument("--no-udeb", action="store_true",
                    help="不同步安装器 .udeb")
    ap.add_argument("--with-debs", action="store_true",
                    help="同时按运行期索引同步全部 .deb（数 GB，默认关）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只枚举与统计，不下载、不建目录")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="逐文件打印")
    args = ap.parse_args(argv)

    mirror = args.mirror.rstrip("/")
    dest = os.path.abspath(args.dest)
    suites = [args.suite, args.suite + "-updates"]
    comps = [c.strip() for c in args.components.split(",") if c.strip()]
    if not comps:
        print("错误: --components 为空", file=sys.stderr)
        return 2
    jobs = max(1, min(args.jobs, MAX_JOBS))

    print("== 同步带签名 Debian 仓库 ==")
    print("镜像 : %s" % mirror)
    print("套系 : %s, %s   组件: %s" % (suites[0], suites[1], ",".join(comps)))
    print("目标 : %s" % dest)
    print("并发 : %d（上限 %d）  udeb: %s  deb: %s  dry-run: %s"
          % (jobs, MAX_JOBS, "否" if args.no_udeb else "是",
             "是" if args.with_debs else "否", "是" if args.dry_run else "否"))

    plan = build_plan(suites, comps)
    nfiles = sum(len(rels) for _name, rels in plan)
    print("索引文件集 %d 个 / 文件 %d 个" % (len(plan), nfiles))
    if not args.dry_run:
        os.makedirs(dest, exist_ok=True)

    stats = {}
    run_sets(plan, mirror, dest, jobs, args.dry_run, args.verbose, stats)

    if not args.no_udeb:
        label, names = collect_pool_names(dest, suites, comps, "udeb")
        if names:
            print("安装器索引列出 .udeb %d 个，同步 pool ..." % len(names))
            run_sets([(label, names)], mirror, dest, jobs,
                     args.dry_run, args.verbose, stats)
        else:
            st = stats.setdefault(label, SetStat())
            st.failed.append((
                "dists/%s/main/debian-installer/binary-%s/Packages*"
                % (args.suite, ARCH),
                "无法枚举 .udeb：本地没有安装器 Packages 索引（第 3 类未同步成功？）"))
            print("  [4:安装器 .udeb] 无法枚举：本地没有安装器 Packages 索引")

    if args.with_debs:
        label, names = collect_pool_names(dest, suites, comps, "deb")
        if names:
            print("运行期索引列出 .deb %d 个（预计数 GB），同步 pool ..." % len(names))
            run_sets([(label, names)], mirror, dest, jobs,
                     args.dry_run, args.verbose, stats)
        else:
            st = stats.setdefault(label, SetStat())
            st.failed.append(("dists/*/binary-%s/Packages*" % ARCH,
                              "无法枚举 .deb：本地没有运行期 Packages 索引"))
            print("  [5:.deb] 无法枚举：本地没有运行期 Packages 索引")

    return summary(stats, dest, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
