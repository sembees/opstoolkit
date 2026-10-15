# -*- coding: utf-8 -*-
"""离线签名仓库的**同步**：平台侧只做"写请求 + 读状态"，真正的下载在**宿主机**上跑。

## 为什么不在这里下载（B1，2026-10-15 探明）

- 容器里**没有 curl / xz**（宿主才有），而同步脚本就是靠它们；
- 容器里**没有 systemctl/systemd-run，也没有 /run/systemd** ⇒ 平台**无法**命令宿主机的 systemd。

所以走的是本项目已有的那条路（与 dnsmasq 重载同一个解法）：
**平台写一个请求文件 → 宿主机上的 path 单元接住 → 跑同步脚本 → 把结果写回文件 → 平台读**。

```
/srv/opstk/state/repo-sync/request.json   平台写（创建即触发）
/srv/opstk/state/repo-sync/status.json    宿主写（running / done / failed）
/srv/opstk/state/repo-sync/result.json    宿主写（rc + 日志尾部）
/srv/opstk/state/repo-sync/sync.log       宿主写（完整日志）
```

宿主侧实现见 `deploy/host/opstk-repo-sync.{path,service,sh}` 与 `install-opstk-repo-sync.sh`；
**两边的字段名是一份契约，改一边必须改另一边**。

## 参数校验为什么必须在这里做

这些值会进宿主上的一个 shell 命令（脚本里用单引号包住、并剥掉单引号，已有一层防护）。
但入口仍要白名单化：`dest` 必须落在仓库根下（否则一次调用就能把同步写到任意目录，
把宿主的盘写满或覆盖别的东西），`suite`/`components`/`mirror` 只放行安全字符。
"""
from __future__ import annotations

import ipaddress  # noqa: F401  (保持与同目录其它模块一致的导入风格)
import json
import os
import re
import time
import uuid

STATE_DIR = "/srv/opstk/state/repo-sync"
REQUEST = os.path.join(STATE_DIR, "request.json")
STATUS = os.path.join(STATE_DIR, "status.json")
RESULT = os.path.join(STATE_DIR, "result.json")
LOG = os.path.join(STATE_DIR, "sync.log")

REPO_ROOT = "/srv/opstk/pxe-web/repo"
DEFAULT_MIRROR = "https://mirrors.tuna.tsinghua.edu.cn/debian"
DEFAULT_COMPONENTS = "main,contrib,non-free,non-free-firmware"

_RE_SUITE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_RE_COMPONENTS = re.compile(r"^[A-Za-z0-9][A-Za-z0-9,._+-]{0,255}$")
_RE_MIRROR = re.compile(r"^https?://[A-Za-z0-9._:/?&=%~+-]{1,255}$")
_RE_DEST = re.compile(r"^/srv/opstk/pxe-web/repo/[A-Za-z0-9._-]{1,64}$")


def validate(params: dict) -> dict:
    """把入参归一化并白名单校验；不合法就抛 ValueError（API 层转 422）。"""
    p = params or {}
    suite = str(p.get("suite") or "trixie").strip()
    if not _RE_SUITE.match(suite):
        raise ValueError("suite 不合法：只允许字母数字与 . _ -（收到 %r）" % suite)
    comps = str(p.get("components") or DEFAULT_COMPONENTS).strip()
    if not _RE_COMPONENTS.match(comps):
        raise ValueError("components 不合法：只允许字母数字与 , . _ + -（收到 %r）" % comps)
    mirror = str(p.get("mirror") or DEFAULT_MIRROR).strip()
    if not _RE_MIRROR.match(mirror):
        raise ValueError("mirror 必须是 http(s) URL（收到 %r）" % mirror)
    dest = str(p.get("dest") or "").strip()
    if not dest:
        dest = REPO_ROOT + "/debian-" + suite
    if not _RE_DEST.match(dest):
        # ★ 为什么必须落在仓库根下：这个值会决定宿主机把几个 GB 写到哪个目录，
        #   放开就等于给了一个"写任意路径"的入口。
        raise ValueError("dest 必须形如 %s/<名字>（只允许字母数字与 . _ -）：收到 %r"
                         % (REPO_ROOT, dest))
    # ★ 只有 None / "" 才算"没填"；`jobs=0` 这种是**给了非法值**，必须报错而不是静默回退成 8
    #   （本项目反复踩过"静默回退"，运维看到的是"我设了 0 却按 8 在跑"）。
    raw_jobs = p.get("jobs")
    if raw_jobs in (None, ""):
        jobs = 8
    else:
        try:
            jobs = int(raw_jobs)
        except (TypeError, ValueError):
            raise ValueError("jobs 必须是整数（收到 %r）" % (raw_jobs,))
        if not (1 <= jobs <= 16):
            raise ValueError("jobs 必须在 1..16 之间（同步脚本硬约束并发 <= 8，这里放宽到 16）")
    return {"id": uuid.uuid4().hex, "suite": suite, "components": comps, "mirror": mirror,
            "dest": dest, "jobs": jobs,
            "no_udeb": bool(p.get("no_udeb")), "with_debs": bool(p.get("with_debs")),
            "dry_run": bool(p.get("dry_run")), "created_at": int(time.time())}


def start_sync(params: dict) -> dict:
    """写请求文件（**创建即触发**宿主的 path 单元）。返回请求本身。"""
    req = validate(params)
    os.makedirs(STATE_DIR, exist_ok=True)
    # 先清掉上一条状态，避免"刚点同步就看到上一次的结果"（运维会以为这次已经跑完了）
    for f in (STATUS, RESULT):
        try:
            os.remove(f)
        except OSError:
            pass
    with open(REQUEST, "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False, indent=1)
    return req


def _read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def sync_status(tail_lines=20) -> dict:
    """读宿主侧写的状态/结果 + 日志尾部。没有请求也没有结果 ⇒ state=idle。"""
    req = _read_json(REQUEST)
    st = _read_json(STATUS)
    res = _read_json(RESULT)
    tail = []
    try:
        with open(LOG, encoding="utf-8", errors="replace") as f:
            tail = [ln.rstrip("\n") for ln in f.readlines()][-tail_lines:]
    except OSError:
        pass
    state = (st or {}).get("state") or ("queued" if req else "idle")
    return {"state": state,
            "pending_request": req,          # 非空 = 已请求但宿主还没处理完
            "status": st, "result": res, "log_tail": tail,
            "state_dir": STATE_DIR,
            "hint": ("平台只负责写请求；真正的下载在宿主机上由 opstk-repo-sync.path 触发。"
                     "若一直停在 queued，说明宿主机没装/没启用该单元："
                     "在宿主机执行 bash /opt/opstk/deploy/host/install-opstk-repo-sync.sh")}
