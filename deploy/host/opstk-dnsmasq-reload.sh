#!/bin/bash
# OpsToolkit PXE —— 宿主机 dnsmasq 重载器
#
# 由 opstk-dnsmasq-reload.path 触发（监视 /etc/dnsmasq.d/opstk-pxe.conf）。
# 容器内的应用**无法**命令宿主机的 systemd（既没挂 /run/dbus，也没有 pid: host），
# 所以"配置写好了 → 让正在跑的 dnsmasq 加载它"这一步只能在宿主机上做。
#
# 顺序是刻意的：**先用 --test 校验，通过才 restart**。
# 绝不能反过来 —— 把一份语法错误的配置重启进去，会让 dnsmasq 起不来，
# 整段装机网络立刻失效（而应用侧还会显示"已重载"）。
#
# 完成后把 "OK <配置sha> <时间>" 写进状态文件，供应用侧核对
# "跑起来的确实是它刚写的那份"。任何一步失败都写 FAIL 且以非 0 退出。
set -u

CONF=/etc/dnsmasq.d/opstk-pxe.conf
# 必须落在 compose 挂进容器的路径上，否则应用侧读不到（踩过：放在 /srv/opstk/
# 下，而 compose 只挂了它的子目录 pxe-web/iso/mnt → 容器里永远 ENOENT）。
STATE_DIR=/srv/opstk/state
STATE=$STATE_DIR/.dnsmasq-reload.state
ts=$(date +%s)

mkdir -p "$STATE_DIR"

fail() {
    # $1 = 原因（不含空格，便于应用侧解析）
    echo "FAIL $1 $ts" > "$STATE"
    echo "opstk-dnsmasq-reload: FAIL $1"
    exit 1
}

[ -f "$CONF" ] || fail missing-conf

if ! out=$(dnsmasq --test 2>&1); then
    echo "$out" >&2
    fail test-failed
fi

systemctl restart dnsmasq || fail restart-failed

systemctl is-active --quiet dnsmasq || fail not-active

sha=$(sha256sum "$CONF" | awk '{print $1}')
echo "OK $sha $ts" > "$STATE"
echo "opstk-dnsmasq-reload: OK (sha=${sha:0:16})"
