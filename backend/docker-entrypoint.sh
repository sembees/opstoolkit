#!/bin/bash
set -e

echo "=== OpsToolkit 启动 ==="

# 创建必要目录
mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/iso /srv/opstk/mnt /app/backend/data

# 修复 SELinux (如果在 SELinux 环境中)
if command -v getenforce >/dev/null 2>&1 && [ "" = "Enforcing" ]; then
    semanage fcontext -a -t tftpdir_t "/srv/tftp(/.*)?" 2>/dev/null || true
    semanage fcontext -a -t tftpdir_t "/srv/opstk/pxe-web(/.*)?" 2>/dev/null || true
    restorecon -R /srv/tftp /srv/opstk/pxe-web 2>/dev/null || true
    echo "SELinux 上下文已修复"
fi

# 如果已有 dnsmasq 配置, 启动 dnsmasq
if [ -f /etc/dnsmasq.d/opstk-pxe.conf ]; then
    echo "发现已有 dnsmasq 配置, 启动 dnsmasq..."
    pkill -x dnsmasq 2>/dev/null || true
    sleep 0.5
    dnsmasq --conf-file=/etc/dnsmasq.d/opstk-pxe.conf 2>/dev/null || \
        dnsmasq --conf-file=/etc/dnsmasq.d/opstk-pxe.conf --keep-in-foreground &
    sleep 1
    echo "dnsmasq 已启动"
fi

echo "=== 启动 OpsToolkit Web 服务 (端口 8000) ==="
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
