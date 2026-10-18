# -*- coding: utf-8 -*-
"""离线装机仓库（要求**带 Debian 官方签名**）的**完整性核查** —— 只读，不下载任何东西。

## 为什么需要它（B1）

隔离网里装机必须用本地仓库，而 Debian 官方 DVD 的 `dists/` **不带** `Release.gpg`/`InRelease`，
d-i 的镜像校验会失败并停在 `<Change mirror>`。所以索引与签名必须从带官方签名的镜像站
（默认清华 TUNA）原样搬回本地 —— 搬运脚本是 `backend/scripts/sync_signed_debian_repo.py`。

但"搬回来了"不等于"搬对了"：索引与 `.deb`/`.udeb` 之间靠 `Release` 里的 **SHA256 + Size**
自洽，任何一个文件搬坏/搬漏，装机就会在下载阶段失败（而且现场是隔离网，很难查）。
本模块把这件事变成**一条可复算的结论**：逐 suite 核对
  1. 签名文件在不在（`InRelease` / `Release.gpg` + `Release`）；
  2. `Release` 里声明的 `Packages*` / `Translation-*` 的 **SHA256 与大小**是否与磁盘上的**实际**一致；
  3. 能不能真的用 Debian 归档密钥验签（宿主上有 `gpgv` 与密钥环时才做，否则**如实报告"未校验"**，
     绝不把"文件在"说成"签名有效"）。

## 为什么不做签名校验的替代品

拿不到密钥环时**不猜**：只报 `signature.checked=False` 与原因。理由和本项目其它地方一致 ——
把"看起来没问题"包装成"已验证"比不验更危险。
"""
from __future__ import annotations

import hashlib
import os
import re
import subprocess

REPO_ROOT = "/srv/opstk/pxe-web/repo"
# 宿主 Debian 归档密钥环的常见位置（本环境实测**都不存在** —— 见 signature.checked=False 的原因）
KEYRING_CANDIDATES = (
    "/usr/share/keyrings/debian-archive-keyring.gpg",
    "/usr/share/keyrings/debian-archive-removed-keys.gpg",
    "/etc/apt/trusted.gpg.d/debian-archive-keyring.gpg",
)
# 我们关心的索引文件名（与同步脚本搬的东西一致；比对范围收窄到这些，避免把整个 pool 扫一遍）
_INDEX_NAMES = ("Packages", "Packages.gz", "Packages.xz", "Release",
                "Translation-en", "Translation-en.gz", "Translation-en.xz")

# ★ 2026-10-18（B1 口径闭环）：同一个逻辑索引有**多种压缩变体**，Release 会为每个变体各写一条
#   哈希（`main/binary-amd64/Packages`、`...Packages.gz`、`...Packages.xz` 各一行）。
#   本工具同步的是**子集**：线上 debian-13.7.0 只有 `.gz`，于是"Release 声明了未压缩的
#   `Packages`、磁盘上没有"被报成 missing ⇒ 4 条**误报**，整仓永远 warn，等于没有校验。
#   正确语义：按"逻辑索引"聚合变体，**任一变体在且哈希对**就算该索引齐；都没有才 missing。
_COMPRESS_SUFFIXES = ("", ".gz", ".xz", ".bz2")


def _logical_index(relpath):
    """把 `main/binary-amd64/Packages.gz` 归一成 `main/binary-amd64/Packages`。

    非 Packages/Translation 类的（比如 `Release`）原样返回，不做聚合。
    """
    for base in ("Packages", "Translation-en"):
        for suf in _COMPRESS_SUFFIXES:
            if relpath.endswith(base + suf) and (
                    relpath == base + suf or relpath.endswith("/" + base + suf)):
                return relpath[: len(relpath) - len(suf)]
    return relpath


def _sha256(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _dir_bytes(path, cap=None):
    """目录大小（字节）。cap 给上限时超过就停（用于大目录只要个数量级）。"""
    total = 0
    for dirpath, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(dirpath, n))
            except OSError:
                pass
        if cap and total > cap:
            return total
    return total


def list_repos(root=REPO_ROOT):
    """列出仓库根下的每个仓库及其 suite（来自 dists/ 子目录）。只读。"""
    out = []
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if not os.path.isdir(p):
            continue
        dists = os.path.join(p, "dists")
        suites = sorted(d for d in (os.listdir(dists) if os.path.isdir(dists) else [])
                        if os.path.isdir(os.path.join(dists, d)))
        out.append({
            "name": name, "path": p, "suites": suites,
            "size_bytes": _dir_bytes(p),
            "has_dists": os.path.isdir(dists),
        })
    return out


def parse_release_sha256(text):
    """解析 `Release` 的 SHA256 段 → {相对路径: (sha256, size)}。

    只认标准的三个字段行（hash size filename），遇到下一个段的标题就停 —— 不猜格式。
    """
    out = {}
    in_sha = False
    for line in text.split("\n"):
        if line.startswith("SHA256:"):
            in_sha = True
            continue
        if in_sha:
            if not line.startswith(" "):
                break                      # 段结束
            m = re.match(r"^\s+([0-9a-fA-F]{64})\s+(\d+)\s+(\S+)\s*$", line)
            if m:
                out[m.group(3)] = (m.group(1).lower(), int(m.group(2)))
    return out


def _pick_keyring():
    for k in KEYRING_CANDIDATES:
        if os.path.exists(k):
            return k
    return None


def verify_signature(suite_dir, keyring=None):
    """用 gpgv 验 `InRelease`（或 `Release.gpg`+`Release`）。做不到就如实说做不到。"""
    kr = keyring or _pick_keyring()
    if not kr:
        return {"checked": False,
                "reason": ("宿主/容器上没有 Debian 归档密钥环（" +
                           "、".join(KEYRING_CANDIDATES) + " 均不存在），"
                           "因此**未做**签名校验 —— 只有文件存在性被核对过")}
    if not os.path.exists("/usr/bin/gpgv") and not _which("gpgv"):
        return {"checked": False, "reason": "没有 gpgv 可执行文件"}
    inrel = os.path.join(suite_dir, "InRelease")
    rel = os.path.join(suite_dir, "Release")
    sig = os.path.join(suite_dir, "Release.gpg")
    if os.path.exists(inrel):
        cmd = ["gpgv", "--keyring", kr, inrel]
    elif os.path.exists(rel) and os.path.exists(sig):
        cmd = ["gpgv", "--keyring", kr, sig, rel]
    else:
        return {"checked": False, "reason": "没有 InRelease，也没有 Release+Release.gpg"}
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:  # noqa: BLE001
        return {"checked": False, "reason": "gpgv 执行失败：%s" % e}
    tail = (p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace")).strip()
    return {"checked": True, "ok": p.returncode == 0, "keyring": kr,
            "output": tail.split("\n")[-1][:200] if tail else ""}


def _which(name):
    for d in os.environ.get("PATH", "").split(os.pathsep):
        p = os.path.join(d, name)
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def verify_suite(repo_path, suite, keyring=None, limit_bad=10):
    """核对一个 suite。返回结构化结论（全部字段都可由磁盘事实复算）。"""
    sd = os.path.join(repo_path, "dists", suite)
    res = {"suite": suite, "dir": sd,
           "files": {"Release": os.path.exists(os.path.join(sd, "Release")),
                     "Release.gpg": os.path.exists(os.path.join(sd, "Release.gpg")),
                     "InRelease": os.path.exists(os.path.join(sd, "InRelease"))}}
    if not res["files"]["Release"]:
        # 只有目录、没有任何索引内容 ⇒ 这个 suite **根本没同步**（线上 testing/unstable 就是，
        # debian-tuna 是从完整镜像站 rsync 出来的一棵树，里面留着很多没搬的 suite 目录）。
        # 这种情况报 fail 是"狼来了"：它本来就不该有。真正危险的是"有内容却没有 Release"。
        # 判"有没有内容"要看**文件**，不能只看目录：线上 `trixie-security` 就是 0 个文件、
        # 只有一层层空目录（一次中止的同步留下的），按目录判会误判成"有内容却缺 Release"⇒ fail。
        has_content = any(
            os.path.isfile(os.path.join(sd, comp, "binary-amd64", n))
            for comp in ("main", "contrib", "non-free", "non-free-firmware")
            for n in ("Packages", "Packages.gz", "Packages.xz", "Release")
        )
        res["verdict"] = "fail" if has_content else "warn"
        res["reason"] = ("有内容但缺少 Release（索引声明文件）：d-i 无法校验镜像"
                         if has_content else
                         "本仓库没有同步这个 suite（只有目录、没有索引）—— 若装机指向它会失败")
        res["signature"] = {"checked": False, "reason": "没有 Release，无从验签"}
        # ★ 提前返回**也必须**带上 index_check：调用方（API / 前端 / 验收脚本）按固定形状读它，
        #   少一个键就是 500。本轮线上实测：debian-tuna 下的 testing/unstable 这些**只有目录、
        #   没有 Release** 的 suite 把 /repo/status 直接打成 500 —— 是我漏写了这个键，
        #   不是数据问题（用例只覆盖了"有 Release"的分支，所以没抓住，本轮补了用例）。
        res["index_check"] = {"checked": 0, "ok": 0, "bad": [], "bad_count": 0,
                             "missing": [], "missing_count": 0,
                             "compressed_used": [], "compressed_count": 0}
        return res
    with open(os.path.join(sd, "Release"), encoding="utf-8", errors="replace") as f:
        declared = parse_release_sha256(f.read())
    # 只核对"我们关心的、且在 Release 里声明了"的索引文件
    checked = ok = 0
    bad, missing = [], []
    # 先按**逻辑索引**聚合变体：{逻辑路径: [(变体路径, want_sha, want_size), ...]}
    groups = {}
    for relpath, (want_sha, want_size) in sorted(declared.items()):
        if os.path.basename(relpath) not in _INDEX_NAMES:
            continue
        groups.setdefault(_logical_index(relpath), []).append((relpath, want_sha, want_size))

    compressed_used = []
    for logical, variants in sorted(groups.items()):
        present = [(rp, ws, wz) for rp, ws, wz in variants
                   if os.path.exists(os.path.join(sd, rp))]
        if not present:
            # 一个变体都没有才算"没同步"。把试过的变体写进 reason，运维知道要补什么。
            tried = ",".join(os.path.basename(rp) for rp, _s, _z in variants)
            missing.append(logical + "（试过变体：" + tried + "）")
            continue
        # 有变体在：逐个校验哈希/大小（同族多个变体都在时，任一对就算过）
        fam_ok = False
        for relpath, want_sha, want_size in present:
            checked += 1
            fp = os.path.join(sd, relpath)
            got_sha = _sha256(fp)
            got_size = os.path.getsize(fp)
            if got_sha == want_sha and got_size == want_size:
                fam_ok = True
                if relpath != logical:
                    compressed_used.append(relpath)
            else:
                bad.append({"path": relpath, "want_sha256": want_sha[:16],
                            "got_sha256": got_sha[:16],
                            "want_size": want_size, "got_size": got_size})
        if fam_ok:
            ok += 1
    res["index_check"] = {"checked": checked, "ok": ok,
                          "bad": bad[:limit_bad], "bad_count": len(bad),
                          "missing": missing[:limit_bad], "missing_count": len(missing),
                          # 用压缩变体是**正常**的（apt 优先 .xz/.gz）；列出来只为可读
                          "compressed_used": compressed_used[:limit_bad],
                          "compressed_count": len(compressed_used)}
    res["signature"] = verify_signature(sd, keyring)
    # ★ 语义修正（2026-10-15，线上真数据逼出来的）：
    #   `bad`（文件在、但内容与 Release 声明不符）= **fail**，这是真正的损坏；
    #   `missing`（Release 声明了、但本地没搬）= **warn**，因为本工具同步的是**子集**
    #   （只搬 amd64 的若干组件），未同步的索引属于预期。第一版把 missing 也算 fail，
    #   结果 deliberately-partial 的仓库永远红灯 —— 那等于没有校验（"狼来了"）。
    #   但仍要**报出来**：装机若正好需要其中某个未同步的索引/包，就会失败。
    if bad:
        res["verdict"] = "fail"
        res["reason"] = ("有 %d 个索引文件与 Release 声明不符（内容被改坏/搬错）—— "
                         "装机取包会失败" % len(bad))
    elif missing:
        res["verdict"] = "warn"
        res["reason"] = ("索引自洽，但有 %d 个 Release 声明的索引**未同步**（同一索引的"
                         "Packages/Packages.gz/Packages.xz 一个都没有；子集同步属预期）；"
                         "装机若需要其中某个会失败，需要时补同步" % len(missing))
        if not (res["files"]["InRelease"]
                or (res["files"]["Release.gpg"] and res["files"]["Release"])):
            res["reason"] += "；另外**未签名**（缺 InRelease/Release.gpg），无法校验索引完整性"
    elif not (res["files"]["InRelease"] or
              (res["files"]["Release.gpg"] and res["files"]["Release"])):
        res["verdict"] = "warn"
        res["reason"] = ("索引齐全且自洽，但**未签名**（InRelease / Release.gpg 都没有）—— "
                         "能装机，但无法证明索引没被改过；需要完整性保证请同步签名文件")
    elif res["signature"].get("checked") and not res["signature"].get("ok"):
        res["verdict"] = "fail"
        res["reason"] = "签名校验失败：" + str(res["signature"].get("output"))
    elif not res["signature"].get("checked"):
        res["verdict"] = "warn"
        res["reason"] = ("索引自洽且签名文件齐备，但**签名未校验**：" +
                         str(res["signature"].get("reason")))
    else:
        res["verdict"] = "ok"
        res["reason"] = "索引与 Release 声明逐项一致，且签名校验通过"
    return res


def verify_repo(repo_path, keyring=None):
    r = {"path": repo_path, "name": os.path.basename(repo_path), "suites": []}
    dists = os.path.join(repo_path, "dists")
    if not os.path.isdir(dists):
        # 不是 Debian 风格的仓库树。**不要报 fail**：centos-7 / openeuler / rocky 的本地仓库
        # 是 RHEL 系的 repodata 结构，本来就没有 dists/ —— 把它们一律标红同样是"狼来了"。
        r["verdict"] = "n/a"
        r["reason"] = "不是 Debian 风格的仓库树（没有 dists/）：本核查不适用（RHEL 系 repodata 仓库属正常）"
        # ★ 提前返回也必须带 `suites`（哪怕是空列表）：调用方按固定形状读它。
        #   这里和 verify_suite 的 index_check 是**同一个错**（我先修了后者，没顺手检查前者）——
        #   centos-7 / openeuler-24.03 / rocky-9.4 这些没有 dists/ 的仓库于是又把 /repo/status
        #   打成 500。教训：写"提前返回"时，把**正常分支返回的全部键**列一遍再返回。
        r["suites"] = []
        return r
    for s in sorted(os.listdir(dists)):
        if os.path.isdir(os.path.join(dists, s)):
            r["suites"].append(verify_suite(repo_path, s, keyring))
    verdicts = {s["verdict"] for s in r["suites"]}
    r["verdict"] = ("fail" if "fail" in verdicts else
                    "warn" if "warn" in verdicts else
                    "ok" if "ok" in verdicts else "n/a")
    # ★ 2026-10-18：仓库级 `reason` 以前是空的 —— 前端把 verdict 显示成黄色却一句话不说，
    #   运维不知道要做什么。这里按最差套系汇总一句可执行的话（套系级仍是完整细节）。
    if r["verdict"] == "fail":
        bad = [s["suite"] for s in r["suites"] if s["verdict"] == "fail"]
        r["reason"] = ("%d 个套系有**索引损坏**（与 Release 声明不符）：%s —— "
                       "装机取包会失败，先修/重同步这些套系"
                       % (len(bad), "、".join(bad[:5]) + ("…" if len(bad) > 5 else "")))
    elif r["verdict"] == "warn":
        warns = [s for s in r["suites"] if s["verdict"] == "warn"]
        unsigned = [s["suite"] for s in warns
                    if "未签名" in (s.get("reason") or "")]
        missing = [s["suite"] for s in warns
                   if (s.get("index_check") or {}).get("missing_count")]
        bits = []
        if unsigned:
            bits.append("未签名（%d 个套系：%s）—— 能装机但无法证明索引没被改过"
                        % (len(unsigned), "、".join(unsigned[:4])))
        if missing:
            bits.append("%d 个套系有未同步的索引（子集同步属预期，缺到时会装不上：%s）"
                        % (len(missing), "、".join(missing[:4])))
        r["reason"] = "；".join(bits) if bits else "有 %d 个套系被判 warn（详见套系明细）" % len(warns)
    elif r["verdict"] == "ok":
        r["reason"] = "全部 %d 个套系索引与签名都通过" % len(r["suites"])
    else:
        r["reason"] = "没有可核查的套系"
    return r
