#!/bin/bash
# OpsToolkit PXE —— 宿主机 dnsmasq 重载器
#
# 由 opstk-dnsmasq-reload.path 触发（监视 /etc/dnsmasq.d/opstk-pxe.conf）。
# 容器内的应用**无法**命令宿主机的 systemd（既没挂 /run/dbus，也没有 pid: host），
# 所以"配置写好了 → 让正在跑的 dnsmasq 加载它"这一步只能在宿主机上做。
#
# 完成后把 "OK <配置sha> <完成时刻>" 写进状态文件，供应用侧核对
# "跑起来的确实是它刚写的那份"。任何一步失败都写 FAIL 且以非 0 退出。
#
# 不变式：**标记里的 sha 必须等于 dnsmasq 真正读进内存的那一份配置**。
set -euo pipefail

CONF=/etc/dnsmasq.d/opstk-pxe.conf
STATE_DIR=/srv/opstk/state
STATE=$STATE_DIR/.dnsmasq-reload.state
# 本脚本自己记的"上一次成功加载的配置 sha"。
# 为什么不复用 STATE：应用侧每次部署前会**删掉** STATE（防止陈旧标记让同内容的
# 二次部署假成功），所以 STATE 不能用来判断"是不是已经加载过了"。
APPLIED=$STATE_DIR/.dnsmasq-applied

mkdir -p "$STATE_DIR"

# 状态文件一律**原子写**（tmp + mv）：容器侧每 0.4s 读一次，
# "截断再写"会让它读到空文件或半行。
write_state() {
    printf '%s\n' "$1" > "$STATE.tmp.$$"
    mv -f "$STATE.tmp.$$" "$STATE"
}

fail() {
    # $1 = 原因（不含空格，便于应用侧解析）
    trap - ERR
    write_state "FAIL $1 $(date +%s)"
    echo "opstk-dnsmasq-reload: FAIL $1" >&2
    exit 1
}

# 兜底：任何**意料之外**的错误也必须在状态文件里留下 FAIL。
# 为什么需要：状态文件"保持上一次的 OK"是最坏的情况 —— 应用侧会拿它当"这份配置已生效"。
# 实测踩过：`set -o pipefail` 下 `reason=$(... | head -1 ...)` 因 head 提前关管道
# 让 printf 收到 SIGPIPE、整条管道非零，`set -e` 在写 FAIL 之前就退出，留下旧的 OK。
trap 'fail "script-error-line${LINENO}"' ERR

# 串行化：path 单元可能在应用部署的同时触发，我们自己也常被手动再调一次。
# 两次 `systemctl restart dnsmasq` 交叠会让其中一次拿到 systemd 事务冲突而假失败。
exec 9>"$STATE_DIR/.reload.lock"
flock -w 60 9 || fail lock-timeout

# 拿到锁之后重新确认文件存在：等锁期间它可能已经被改过/删过
[ -f "$CONF" ] || fail missing-conf

# sha 必须在 restart **之前**采样，并在 restart 之后确认没再被改过。
# 为什么（MiMo R1 的高危发现）：若在 restart 之后才采样，而另一个部署恰好在
# "restart 完成"与"采样"之间写入新内容，标记里记的就是**新内容的 sha**，
# 而 dnsmasq 加载的是**旧内容** —— 调用方一比对就命中，**假成功**。
sha_before=$(sha256sum "$CONF" | awk '{print $1}')
[ -n "$sha_before" ] || fail empty-sha

if ! out=$(dnsmasq --test 2>&1); then
    echo "$out" >&2
    # 只用 bash 参数展开提取原因 —— **不要**用 `| head -1`：pipefail 下 head 提前关管道
    # 会让 printf 收到 SIGPIPE、整条管道非零，set -e 会在写 FAIL 之前就把脚本干掉（踩过）。
    # 注意 dnsmasq 的输出以换行开头，按"首个换行"切会切出空串，
    # 所以直接抹掉所有空白（状态文件的 reason 本来就不允许含空格）。
    reason="${out//[[:space:]]/}"
    fail "test-failed:${reason:0:60}"
fi

# 内容与**上次成功加载的**一致 ⇒ 不必重启。
# 两个理由：
#   1) 正确性：dnsmasq 里已经是这份配置，重启是多余动作；
#   2) 可用性：systemd 对同一 unit 有启动限流（默认 10 秒内 5 次），
#      连续部署同一个模板会密集调用 restart，撞上限流后 `systemctl restart` 直接失败，
#      状态被写成 FAIL restart-failed —— 实测踩过。跳过无变化的重启是根治。
applied=""
[ -f "$APPLIED" ] && applied=$(cat "$APPLIED" 2>/dev/null || true)
if [ "$applied" = "$sha_before" ] && systemctl is-active --quiet dnsmasq; then
    write_state "OK $sha_before $(date +%s)"
    echo "opstk-dnsmasq-reload: OK (unchanged, sha=${sha_before:0:16})"
    exit 0
fi

systemctl restart dnsmasq || fail restart-failed

# dnsmasq.service 若是 Type=simple，restart 返回后状态可能还是 activating，
# 直接 is-active 会误报 not-active（假失败）→ 重试几次。
active=0
for _ in $(seq 1 25); do
    if systemctl is-active --quiet dnsmasq; then active=1; break; fi
    sleep 0.2
done
[ "$active" = "1" ] || fail not-active

sha_after=$(sha256sum "$CONF" | awk '{print $1}')
# 期间被并发改写 ⇒ 我们加载的那份已经不是当前这份，报 FAIL 让下一次触发重跑
# （调用方会继续等它自己那个 sha，等到或超时，都不会假成功）。
[ "$sha_after" = "$sha_before" ] || fail config-changed

# 时刻在最后取：语义是"这份配置生效的完成时刻"，应用侧用它做新鲜度判定。
write_state "OK $sha_before $(date +%s)"
printf '%s\n' "$sha_before" > "$APPLIED.tmp.$$"
mv -f "$APPLIED.tmp.$$" "$APPLIED"
echo "opstk-dnsmasq-reload: OK (sha=${sha_before:0:16})"
