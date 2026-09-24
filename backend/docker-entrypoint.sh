#!/bin/bash
set -e

echo "=== OpsToolkit 启动 ==="

# 创建必要目录
mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/iso /srv/opstk/mnt /app/backend/data

# 修复 SELinux (如果在 SELinux 环境中)
if command -v getenforce >/dev/null 2>&1 && [ "$(getenforce 2>/dev/null)" = "Enforcing" ]; then
    semanage fcontext -a -t tftpdir_t "/srv/tftp(/.*)?" 2>/dev/null || true
    semanage fcontext -a -t tftpdir_t "/srv/opstk/pxe-web(/.*)?" 2>/dev/null || true
    restorecon -R /srv/tftp /srv/opstk/pxe-web 2>/dev/null || true
    echo "SELinux 上下文已修复"
fi

# dnsmasq 归属：宿主机（systemd）优先，容器只在宿主机没有时才兜底。
#
# 为什么必须这样：本容器是 host 网络模式（compose 里 network_mode: host）。
# 若容器里再起一个 dnsmasq，它会和宿主机的那个抢同一个 ens18:67 —— 两个实例
# 各自维护独立租约库、各自从同一地址池分配，可能把同一个 IP 分给两个客户端，
# 或让同一客户端两次拿到不同 IP。对 PXE 装机是致命的（也就没有可信的测试基线）。
#
# 探测手段：容器与宿主机**共享网络命名空间**，所以 /proc/net/udp 里能看到宿主机的
# UDP socket（0x43 = 67、0x45 = 69）。不能靠 /proc 找进程 —— 容器有自己的 PID
# 命名空间，看不到宿主机上的 dnsmasq 进程。
if [ -f /etc/dnsmasq.d/opstk-pxe.conf ]; then
    # 先清掉【本容器 PID 命名空间内】可能残留的 dnsmasq。
    # 容器有独立 PID ns，所以这条 pkill 不会碰到宿主机那个（已实测确认）。
    pkill -x dnsmasq 2>/dev/null || true
    sleep 0.5
    if grep -qE '^[[:space:]]*[0-9]+:[[:space:]]+[0-9A-Fa-f]{8}:0043[[:space:]]' /proc/net/udp 2>/dev/null; then
        echo "宿主机已有 dnsmasq 在监听 :67，容器不再另起（配置由宿主机加载/重载）"
    else
        echo "宿主机未监听 :67，容器内兜底启动 dnsmasq..."
        dnsmasq --conf-file=/etc/dnsmasq.d/opstk-pxe.conf 2>/dev/null || \
            dnsmasq --conf-file=/etc/dnsmasq.d/opstk-pxe.conf --keep-in-foreground &
        sleep 1
        echo "dnsmasq 已启动"
    fi
fi

echo "=== 启动 OpsToolkit Web 服务 (端口 8000) ==="
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
