#!/bin/bash
# 在**宿主机**上安装 OpsToolkit 的 dnsmasq 重载单元。
#
#   sudo bash deploy/host/install-opstk-dnsmasq-reload.sh
#
# 为什么必须在宿主机跑：容器里跑的应用无法命令宿主机的 systemd
# （compose 是 network_mode: host + privileged，但没挂 /run/dbus，也没有 pid: host）。
# 不装这套单元的直接后果是：/deploy 把配置写下去了，但正在跑的 dnsmasq 还是旧配置，
# 磁盘上的文件与守护进程不一致 —— 而应用侧现在会因此**明确报错**，不再假报成功。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SBIN=/usr/local/sbin/opstk-dnsmasq-reload.sh
UNIT_DIR=/etc/systemd/system

[ "$(id -u)" = "0" ] || { echo "请用 root 运行（sudo）" >&2; exit 1; }
command -v dnsmasq >/dev/null || { echo "没找到 dnsmasq，先装 dnsmasq" >&2; exit 1; }
[ -d "$UNIT_DIR" ] || { echo "没有 $UNIT_DIR（宿主机没有 systemd？）" >&2; exit 1; }

install -m 0755 "$HERE/opstk-dnsmasq-reload.sh" "$SBIN"
install -m 0644 "$HERE/opstk-dnsmasq-reload.path" "$UNIT_DIR/opstk-dnsmasq-reload.path"
install -m 0644 "$HERE/opstk-dnsmasq-reload.service" "$UNIT_DIR/opstk-dnsmasq-reload.service"

# 状态目录必须存在，且必须与 docker-compose.yml 里挂进容器的路径一致 ——
# 否则应用侧读不到标记，会把"重载成功"误判成失败。
mkdir -p /srv/opstk/state

# 两个被监视的配置文件**必须先存在**：systemd 的 path 单元对"还不存在的路径"
# 监视的是父目录，而 PXE/ZTP 的第一次部署正是"创建这个文件"的时刻 ——
# 让路径从一开始就存在，触发才可靠（空配置对 dnsmasq 完全无害，--test 也过）。
touch /etc/dnsmasq.d/opstk-pxe.conf /etc/dnsmasq.d/opstk-ztp.conf
chmod 0644 /etc/dnsmasq.d/opstk-pxe.conf /etc/dnsmasq.d/opstk-ztp.conf

systemctl daemon-reload
systemctl enable --now opstk-dnsmasq-reload.path

# 立刻跑一次：既验证链路可用，也让状态文件先就位（否则应用侧第一次部署
# 会在等一个永远不出现的标记）。
systemctl start opstk-dnsmasq-reload.service || true

echo
echo "已安装并启用："
systemctl is-enabled opstk-dnsmasq-reload.path
systemctl is-active opstk-dnsmasq-reload.path
echo "状态文件："
cat /srv/opstk/state/.dnsmasq-reload.state 2>/dev/null || echo "  （还没有，说明上一步没跑成功，看 journalctl -u opstk-dnsmasq-reload）"
echo "版本标记（应用侧部署前的预检要读它，见 backend/app/core/dhcp.py 的 HOST_RELOAD_LINK）："
cat /srv/opstk/state/.dnsmasq-reload.link 2>/dev/null || echo "  （还没有 —— 应用侧会拒绝部署，并且会明确告诉你重跑本脚本）"
echo
echo "自检：改一下 /etc/dnsmasq.d/opstk-pxe.conf，几秒内该文件应变为 OK <sha> <时间>。"
echo "注意：容器要能看到这个标记，docker-compose.yml 必须挂载 /srv/opstk/state。"
echo "注意：**每次改过 deploy/host/ 下的脚本或单元后都必须重跑本脚本** —— 应用侧会在"
echo "      部署前核对版本标记（SCRIPT_VERSION），装的是旧版就直接拒绝部署（不落任何文件）。"
