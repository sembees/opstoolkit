#!/bin/bash
# OpsToolkit —— 宿主机 dnsmasq 重载器
#
# 由 opstk-dnsmasq-reload.path 触发（监视 /etc/dnsmasq.d/opstk-pxe.conf 与
# opstk-ztp.conf —— PXE 与 ZTP 各一份配置）。
# 容器内的应用**无法**命令宿主机的 systemd（既没挂 /run/dbus，也没有 pid: host），
# 所以"配置写好了 → 让正在跑的 dnsmasq 加载它"这一步只能在宿主机上做。
#
# 完成后把 "OK <配置sha> <完成时刻>" 写进**对应槽位**的状态文件，供应用侧核对
# "跑起来的确实是它刚写的那份"：
#   PXE 槽位 /srv/opstk/state/.dnsmasq-reload.state
#   ZTP 槽位 /srv/opstk/state/.dnsmasq-reload.state.ztp
# 任何一步失败都把 FAIL 写进**两个**槽位且以非 0 退出
# （dnsmasq --test 校验的是整个配置目录，所以任一槽位不合法都意味着"没重启"）。
#
# 不变式 1：标记里的 sha 必须等于 dnsmasq 真正读进内存的那一份配置。
# 不变式 2：**两个槽位一起判定**。"哪一份变了"不重要 —— 重启 dnsmasq 会把
#           /etc/dnsmasq.d 下的配置**全部**重新读一遍，所以只要有一份与
#           "上次成功加载的"不一致就必须重启；两份都没变才允许跳过。
set -euo pipefail

CONF_PXE=/etc/dnsmasq.d/opstk-pxe.conf
CONF_ZTP=/etc/dnsmasq.d/opstk-ztp.conf
STATE_DIR=/srv/opstk/state
STATE_PXE=$STATE_DIR/.dnsmasq-reload.state
STATE_ZTP=$STATE_DIR/.dnsmasq-reload.state.ztp
# 本脚本自己记的"上一次成功加载的两个配置 sha"（第 1 行 PXE、第 2 行 ZTP）。
# 为什么不复用状态文件：应用侧每次部署前会**删掉**自己那个槽位的标记
# （防止陈旧标记让同内容的二次部署假成功），所以状态文件不能用来判断
# "是不是已经加载过了"。
APPLIED=$STATE_DIR/.dnsmasq-applied
# 应用侧部署前的预检要读的"版本标记"。
# 它同时证明"单元被触发过、脚本跑得起来"和"装的不是旧版脚本"。
# 改脚本协议（或需要强制运维重跑 install）时把它 +1，并在应用侧同步
# HOST_RELOAD_MIN_SCRIPT_VERSION —— 这样旧安装会在**落盘之前**被明确指出来，
# 而不是每次部署都等到超时、留下不一致的磁盘状态（实测踩过两次）。
# v3：同时监视/重载 PXE 与 ZTP 两份配置，两个槽位各写各的状态文件。
SCRIPT_VERSION=3
LINK=$STATE_DIR/.dnsmasq-reload.link

mkdir -p "$STATE_DIR"

# 状态文件一律**原子写**（tmp + mv）：容器侧每 0.4s 读一次，
# "截断再写"会让它读到空文件或半行。
write_state() {   # $1=内容 $2=目标文件
    printf '%s\n' "$1" > "$2.tmp.$$"
    mv -f "$2.tmp.$$" "$2"
}

fail() {
    # $1 = 原因（不含空格，便于应用侧解析）
    trap - ERR
    now=$(date +%s)
    write_state "FAIL $1 $now" "$STATE_PXE"
    write_state "FAIL $1 $now" "$STATE_ZTP"
    echo "opstk-dnsmasq-reload: FAIL $1" >&2
    exit 1
}

# 兜底：任何**意料之外**的错误也必须在状态文件里留下 FAIL。
# 为什么需要：状态文件"保持上一次的 OK"是最坏的情况 —— 应用侧会拿它当"这份配置已生效"。
# 实测踩过：`set -o pipefail` 下 `reason=$(... | head -1 ...)` 因 head 提前关管道
# 让 printf 收到 SIGPIPE、整条管道非零，`set -e` 在写 FAIL 之前就退出，留下旧的 OK。
trap 'fail "script-error-line${LINENO}"' ERR

# 版本标记：写在**任何可能失败的动作之前**（包括 flock 和配置存在性检查）——
# 一台还没部署过、连 conf 文件都还不存在的宿主机，也必须能通过应用侧预检的静态部分。
printf 'RUN %s %s\n' "$SCRIPT_VERSION" "$(date +%s)" > "$LINK.tmp.$$" && mv -f "$LINK.tmp.$$" "$LINK"

# 串行化：path 单元可能在应用部署的同时触发，我们自己也常被手动再调一次。
# 两次 `systemctl restart dnsmasq` 交叠会让其中一次拿到 systemd 事务冲突而假失败。
exec 9>"$STATE_DIR/.reload.lock"
flock -w 60 9 || fail lock-timeout

# 配置的 sha：文件不存在记 "-"（表示"这一槽位本来就没有配置"，是一个**稳定值**，
# 不是"读不到"）；存在但 sha 算不出来则返回空串，由下面判空报 empty-sha。
sha_of() {
    [ -e "$1" ] || { printf '%s' "-"; return 0; }
    sha256sum "$1" 2>/dev/null | awk '{print $1}'
}

# sha 必须在 restart **之前**采样，并在 restart 之后确认没再被改过。
# 为什么（MiMo R1 的高危发现）：若在 restart 之后才采样，而另一个部署恰好在
# "restart 完成"与"采样"之间写入新内容，标记里记的就是**新内容的 sha**，
# 而 dnsmasq 加载的是**旧内容** —— 调用方一比对就命中，**假成功**。
sha_pxe=$(sha_of "$CONF_PXE")
sha_ztp=$(sha_of "$CONF_ZTP")
[ -n "$sha_pxe" ] || fail empty-sha
[ -n "$sha_ztp" ] || fail empty-sha

# 两个槽位都没有配置 ⇒ 没有可加载的东西。旧版只看 PXE 那一份，于是"只部署 ZTP"
# 的宿主机上会一直 fail —— 这里改成"至少有一份"。
if [ "$sha_pxe" = "-" ] && [ "$sha_ztp" = "-" ]; then
    fail missing-conf
fi

if ! out=$(dnsmasq --test 2>&1); then
    echo "$out" >&2
    # 只用 bash 参数展开提取原因 —— **不要**用 `| head -1`：pipefail 下 head 提前关管道
    # 会让 printf 收到 SIGPIPE、整条管道非零，set -e 会在写 FAIL 之前就把脚本干掉（踩过）。
    # 注意 dnsmasq 的输出以换行开头，按"首个换行"切会切出空串，
    # 所以直接抹掉所有空白（状态文件的 reason 本来就不允许含空格）。
    reason="${out//[[:space:]]/}"
    fail "test-failed:${reason:0:60}"
fi

# 两份配置都与**上次成功加载的**一致 ⇒ 不必重启。
# 两个理由：
#   1) 正确性：dnsmasq 里已经是这两份配置，重启是多余动作；
#   2) 可用性：systemd 对同一 unit 有启动限流（默认 10 秒内 5 次），
#      连续部署会密集调用 restart，撞上限流后 `systemctl restart` 直接失败，
#      状态被写成 FAIL restart-failed —— 实测踩过。跳过无变化的重启是根治。
#      （另：opstk-dnsmasq-reload.service 自己也关了启动限流，见该单元里的说明。）
applied_pxe=""
applied_ztp=""
if [ -f "$APPLIED" ]; then
    applied_pxe=$(sed -n 1p "$APPLIED" 2>/dev/null || true)
    applied_ztp=$(sed -n 2p "$APPLIED" 2>/dev/null || true)
fi
if [ "$applied_pxe" = "$sha_pxe" ] && [ "$applied_ztp" = "$sha_ztp" ] \
   && systemctl is-active --quiet dnsmasq; then
    now=$(date +%s)
    write_state "OK $sha_pxe $now" "$STATE_PXE"
    write_state "OK $sha_ztp $now" "$STATE_ZTP"
    echo "opstk-dnsmasq-reload: OK (unchanged, pxe=${sha_pxe:0:16} ztp=${sha_ztp:0:16})"
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

# 期间被并发改写 ⇒ 我们加载的那份已经不是当前这份，报 FAIL 让下一次触发重跑
# （调用方会继续等它自己那个 sha，等到或超时，都不会假成功）。
[ "$(sha_of "$CONF_PXE")" = "$sha_pxe" ] || fail config-changed
[ "$(sha_of "$CONF_ZTP")" = "$sha_ztp" ] || fail config-changed

# 时刻在最后取：语义是"这两份配置生效的完成时刻"，应用侧用它做新鲜度判定。
now=$(date +%s)
write_state "OK $sha_pxe $now" "$STATE_PXE"
write_state "OK $sha_ztp $now" "$STATE_ZTP"
printf '%s\n%s\n' "$sha_pxe" "$sha_ztp" > "$APPLIED.tmp.$$"
mv -f "$APPLIED.tmp.$$" "$APPLIED"
echo "opstk-dnsmasq-reload: OK (pxe=${sha_pxe:0:16} ztp=${sha_ztp:0:16})"
