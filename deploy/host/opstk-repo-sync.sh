#!/bin/bash
# opstk-repo-sync.sh —— 在**宿主机**上执行"离线签名仓库同步"（由 opstk-repo-sync.path 触发）。
#
# 为什么要有这个脚本（B1，2026-10-15）：
#   平台容器里**没有** systemctl/systemd-run，也没有 /run/systemd ⇒ 容器无法命令宿主机的
#   systemd；而同步必须用宿主机的 curl/xz（容器里没有）。这与 dnsmasq 重载是**同一个问题、
#   同一个解法**：容器写状态文件，宿主用 path 单元接住（见同目录 opstk-dnsmasq-reload.*）。
#
# 契约（与平台侧 app/it/pxe/repo_sync.py 一一对应，改一边必须改另一边）：
#   · 请求：/srv/opstk/state/repo-sync/request.json
#        {"mirror": "...", "suite": "...", "dest": "...", "components": "...",
#         "jobs": 8, "with_debs": false, "no_udeb": false, "dry_run": true, "id": "<uuid>"}
#   · 状态：/srv/opstk/state/repo-sync/status.json   {"state": "running|done|failed", ...}
#   · 结果：/srv/opstk/state/repo-sync/result.json   {"id":..., "rc":..., "tail": [...]}
#   · 日志：/srv/opstk/state/repo-sync/sync.log（平台只回传尾部若干行）
#   · 处理完**必须删掉 request.json**：path 单元靠"文件出现"触发，不删就不会再触发。
set -u

STATE=/srv/opstk/state/repo-sync
REQ="$STATE/request.json"
LOCK=/srv/opstk/state/.repo-sync.lock
SCRIPT=/opt/opstk/backend/scripts/sync_signed_debian_repo.py
PY=/usr/bin/python3

mkdir -p "$STATE"
# 串行化：同一时刻只跑一个同步（两个并发同步会互相覆盖 dest 下的 .part 文件）
exec 9>"$LOCK"
if ! flock -n 9; then
    echo "another repo-sync is running; skip" >&2
    exit 0
fi

[ -f "$REQ" ] || { echo "no request file"; exit 0; }

# 读请求 → 生成命令行参数（值全部经 python 读出，避免 shell 再解析 JSON）
PARAMS=$("$PY" - "$REQ" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception as e:
    print("PARSE_ERROR", e); raise SystemExit(0)
out = [
    "--dest", str(d.get("dest") or ""),
    "--suite", str(d.get("suite") or "trixie"),
    "--mirror", str(d.get("mirror") or "https://mirrors.tuna.tsinghua.edu.cn/debian"),
    "--components", str(d.get("components") or "main,contrib,non-free,non-free-firmware"),
    "--jobs", str(int(d.get("jobs") or 8)),
]
if d.get("no_udeb"):
    out.append("--no-udeb")
if d.get("with_debs"):
    out.append("--with-debs")
if d.get("dry_run"):
    out.append("--dry-run")
print(" ".join("'" + x.replace("'", "") + "'" for x in out))
print("ID " + str(d.get("id") or ""))
PY
)
case "$PARAMS" in
    PARSE_ERROR*|"") echo "bad request: $PARAMS" >&2; rm -f "$REQ"; exit 1 ;;
esac
ARGS=$(printf '%s\n' "$PARAMS" | head -n 1)
RID=$(printf '%s\n' "$PARAMS" | sed -n 's/^ID //p')

"$PY" - "$STATE" "$RID" <<'PY'
import json, sys, time
state, rid = sys.argv[1], sys.argv[2]
json.dump({"state": "running", "id": rid, "started_at": int(time.time())},
          open(state + "/status.json", "w", encoding="utf-8"), ensure_ascii=False)
PY

: > "$STATE/sync.log"
# shellcheck disable=SC2086
eval "$PY '$SCRIPT' $ARGS" >>"$STATE/sync.log" 2>&1
RC=$?

"$PY" - "$STATE" "$RID" "$RC" <<'PY'
import json, sys, time
state, rid, rc = sys.argv[1], sys.argv[2], int(sys.argv[3])
lines = []
try:
    with open(state + "/sync.log", encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip("\n") for l in f.readlines()][-40:]
except OSError:
    pass
json.dump({"state": "done" if rc == 0 else "failed", "id": rid, "rc": rc,
           "finished_at": int(time.time()), "tail": lines},
          open(state + "/result.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
json.dump({"state": "done" if rc == 0 else "failed", "id": rid, "rc": rc},
          open(state + "/status.json", "w", encoding="utf-8"), ensure_ascii=False)
PY

rm -f "$REQ"          # ★ 必须删：path 单元靠"文件出现"再触发
exit 0
