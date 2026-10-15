#!/bin/bash
# install-opstk-repo-sync.sh —— 在**宿主机**上安装"离线签名仓库同步"的 path/service 单元。
#
# 与 install-opstk-dnsmasq-reload.sh 同一套路（幂等、可重复执行）：
#   1. 拷脚本到 /usr/local/sbin（单元里写的是绝对路径）
#   2. 拷单元到 /etc/systemd/system
#   3. daemon-reload + enable --now
#   4. 打印自检结果
#
# 用法（在宿主机上、用 root）：bash install-opstk-repo-sync.sh
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
STATE=/srv/opstk/state/repo-sync

echo "== 1) 安装脚本 =="
install -m 0755 "$SRC_DIR/opstk-repo-sync.sh" /usr/local/sbin/opstk-repo-sync.sh
echo "   /usr/local/sbin/opstk-repo-sync.sh"

echo "== 2) 安装单元 =="
install -m 0644 "$SRC_DIR/opstk-repo-sync.path" /etc/systemd/system/opstk-repo-sync.path
install -m 0644 "$SRC_DIR/opstk-repo-sync.service" /etc/systemd/system/opstk-repo-sync.service
echo "   /etc/systemd/system/opstk-repo-sync.{path,service}"

echo "== 3) 准备状态目录 =="
mkdir -p "$STATE"
chmod 0755 "$STATE"
echo "   $STATE"

echo "== 4) 启用并启动 path 单元 =="
systemctl daemon-reload
systemctl enable --now opstk-repo-sync.path

echo "== 5) 自检 =="
systemctl is-enabled opstk-repo-sync.path || true
systemctl is-active opstk-repo-sync.path || true
systemctl status opstk-repo-sync.path --no-pager -n 5 || true

echo
echo "完成。触发方式：平台写入 $STATE/request.json（见 deploy/host/README.md 的契约）。"
