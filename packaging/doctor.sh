#!/usr/bin/env bash
# =============================================================================
# OpsToolkit 环境体检（**只读**：不写任何文件、不重启任何服务、不改任何配置）
#
# 用法：
#   sudo bash doctor.sh                 # 全套体检
#   sudo bash doctor.sh --port 18000    # 指定 Web 端口（默认读安装目录 .env，再退回 8000）
#   bash doctor.sh                      # 非 root 也能跑，但涉及 docker/systemd 的项只能给提示
#
# 结论标记：[正常] / [注意]（建议处理，不阻塞）/ [需处理]（不修就没法正常用）
# 退出码：0 = 没有[需处理]；1 = 存在[需处理]。
# =============================================================================
set -uo pipefail

if [ -t 1 ]; then
    C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[1m'; C_N=$'\e[0m'
else
    C_G=""; C_Y=""; C_R=""; C_B=""; C_N=""
fi
say()   { printf '%s\n' "$*"; }
good()  { printf '  %s[正常]%s %s\n' "$C_G" "$C_N" "$*"; }
warn()  { printf '  %s[注意]%s %s\n' "$C_Y" "$C_N" "$*"; }
bad()   { printf '  %s[需处理]%s %s\n' "$C_R" "$C_N" "$*"; }
section() { printf '\n%s—— %s —-%s\n' "$C_B" "$*" "$C_N"; }

PORT_ARG=""
while [ $# -gt 0 ]; do
    case "$1" in
        --port)   PORT_ARG="${2:-}"; shift 2 ;;
        --port=*) PORT_ARG="${1#*=}"; shift ;;
        -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) say "未知参数：$1（用 -h 看用法）"; exit 2 ;;
    esac
done

ISSUES=0
has_issue() { ISSUES=$((ISSUES+1)); }
IS_ROOT=0
[ "$(id -u)" = "0" ] && IS_ROOT=1

# 安装目录探测：优先当前目录（离线包/安装根），再退默认 /opt/opstk
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 兼容仓库布局（packaging/ 下）：repo 根 = HERE/..
if [ ! -f "$HERE/deploy/docker-compose.yml" ] && [ ! -f "$HERE/docker-compose.yml" ] && [ -f "$HERE/../deploy/docker-compose.yml" ]; then
    HERE="$(cd "$HERE/.." && pwd)"
fi
# 安装脚本路径（离线包根：./install.sh；仓库布局：./packaging/install.sh）
if [ -f "$HERE/install.sh" ]; then INSTALLER="$HERE/install.sh"; else INSTALLER="$HERE/packaging/install.sh"; fi
if [ -f "$HERE/.env" ]; then
    INSTALL_DIR="$HERE"
elif [ -f "$HERE/../.env" ]; then
    INSTALL_DIR="$(cd "$HERE/.." && pwd)"
elif [ -f "/opt/opstk/.env" ]; then
    INSTALL_DIR="/opt/opstk"
else
    INSTALL_DIR=""
fi
COMPOSE_ENV="${INSTALL_DIR:+$INSTALL_DIR/.env}"
# 应用密钥文件：安装根布局优先，退而求其次看默认安装根（开发态宿主目录也认）
APP_ENV=""
for cand in "${INSTALL_DIR:+$INSTALL_DIR/backend/.env}" "/opt/opstk/backend/.env"; do
    [ -n "$cand" ] && [ -f "$cand" ] && { APP_ENV="$cand"; break; }
done

read_env_var() {  # <键> <默认值>
    local k="$1" d="$2" v=""
    if [ -n "${COMPOSE_ENV:-}" ] && [ -f "$COMPOSE_ENV" ]; then
        v="$(grep -E "^${k}=" "$COMPOSE_ENV" | tail -n1 | cut -d= -f2- || true)"
    fi
    printf '%s' "${v:-$d}"
}
PORT="${PORT_ARG:-$(read_env_var OPSTK_PORT 8000)}"
DATA_ROOT="$(read_env_var OPSTK_DATA_ROOT /srv/opstk)"
TFTP_ROOT="$(read_env_var OPSTK_TFTP_ROOT /srv/tftp)"
CNAME="$(read_env_var OPSTK_CONTAINER_NAME opstoolkit)"
OPSTK_IMAGE="$(read_env_var OPSTK_IMAGE opstk:latest)"

say "${C_B}==================== OpsToolkit 环境体检（只读） ====================${C_N}"
say "体检对象：安装目录=${INSTALL_DIR:-（未找到，按默认值检查）}  端口=${PORT}  数据根=${DATA_ROOT}"

# ---------- 1. root ----------
section "1/9 当前用户"
if [ "$IS_ROOT" = "1" ]; then
    good "当前是 root，可以做全部检查"
else
    warn "当前不是 root。docker/systemd 相关项可能看不全；建议：sudo bash doctor.sh"
fi

# ---------- 2. docker / compose ----------
section "2 / Docker 与 compose"
if command -v docker >/dev/null 2>&1; then
    good "docker 已安装：$(docker --version 2>/dev/null | sed 's/, version/ /;s/,.*//')"
    if docker info >/dev/null 2>&1; then
        good "docker 守护进程在运行，且当前用户有权访问"
    else
        bad "docker 已安装但连不上守护进程（服务没起或无权限）"
        say "        怎么修：sudo systemctl start docker && sudo systemctl enable docker"
        has_issue
    fi
else
    bad "没找到 docker。OpsToolkit 以 Docker 方式运行，必须先装 Docker。"
    say "        怎么修：在能上网的机器上安装 Docker 后拷贝到本机，或使用包含 Docker 的系统镜像；"
    say "                离线安装 Docker 可参考 Docker 官方 static binary / rpm|deb 离线包方式。"
    has_issue
fi
COMPOSE_OK=0
if docker compose version >/dev/null 2>&1; then
    COMPOSE_OK=1
    good "docker compose（插件）可用：$(docker compose version 2>/dev/null | awk '{print $NF}')"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE_OK=1
    good "docker-compose（独立命令）可用：$(docker-compose --version 2>/dev/null)"
else
    bad "没找到 docker compose（插件或独立命令都没有），无法编排容器"
    say "        怎么修：安装 docker-compose-plugin（yum install docker-compose-plugin 或 apt 安装同名包）"
    has_issue
fi

# ---------- 3. 端口占用 ----------
section "3 / 端口占用（53=DNS 67=DHCP 69=TFTP ${PORT}=OpsToolkit 网页）"
port_users() {  # $1=端口 → 输出 "协议 地址:端口 进程" 行
    if command -v ss >/dev/null 2>&1; then
        ss -H -lntup 2>/dev/null | awk -v P=":${1}\$" '
            ($1=="tcp" || $1=="udp") && $5 ~ P {
                proc=""; if (match($0, /users:\(\(.*\)\)/)) proc=substr($0, RSTART, RLENGTH);
                print $1, $5, proc
            }'
    elif command -v netstat >/dev/null 2>&1; then
        netstat -lntup 2>/dev/null | awk -v P=":${1}\$" 'NR>2 && $4 ~ P {print $1, $4, $NF}'
    else
        echo ""
    fi
}
describe_port() {  # $1=端口 $2=用途说明 $3=算正常的进程名(正则,如 'dnsmasq|python')
    local p="$1" label="$2" friendly_re="$3" users
    users="$(port_users "$p")"
    if [ -z "$users" ]; then
        good "端口 ${p} 空闲（${label}可用）"
        return
    fi
    local resolved_flag=0 friendly_line=""
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        if printf '%s' "$line" | grep -Eq 'systemd-resolv'; then
            resolved_flag=1
        fi
        if printf '%s' "$line" | grep -Eq "$friendly_re"; then
            friendly_line="$line"
        fi
    done <<< "$users"
    if [ "$resolved_flag" = "1" ]; then
        bad "端口 ${p} 被 systemd-resolved 占用（${label}会被它挡住）"
        say "        怎么修（本机不做 DNS 解析服务时，标准做法是关掉它）："
        say "          sudo systemctl disable --now systemd-resolved"
        say "          sudo rm -f /etc/resolv.conf && echo 'nameserver 223.5.5.5' | sudo tee /etc/resolv.conf"
        say "          （223.5.5.5 可换成你们内网 DNS；若 /etc/resolv.conf 原是软链接，rm 后重建普通文件即可）"
        has_issue
        return
    fi
    if [ -n "$friendly_line" ]; then
        good "端口 ${p} 由 OpsToolkit 自己的服务提供（${label}正常）：${friendly_line}"
        return
    fi
    warn "端口 ${p} 被占用：$(printf '%s' "$users" | head -n1)"
    say "        是谁：用 lsof -i :${p} 或 systemctl status <上面括号里的进程名> 确认；"
    say "        若不是 OpsToolkit 自己的进程，需要停掉/迁移该服务后才能用${label}。"
    [ "$p" = "$PORT" ] && has_issue
    return 0
}
describe_port 53  "DNS 服务"        'dnsmasq'
describe_port 67  "DHCP 服务"       'dnsmasq'
describe_port 69  "TFTP 服务"       'dnsmasq'
describe_port "$PORT" "OpsToolkit 网页" 'python|uvicorn|dnsmasq'

# ---------- 4. 数据目录 ----------
section "4 / 数据目录（存在性 / 可写性）"
check_dir() {  # $1=目录 $2=用途
    local d="$1" label="$2"
    if [ ! -d "$d" ]; then
        bad "${label}不存在：$d"
        say "        怎么修：sudo mkdir -p $d   （或重跑 install.sh，它会自动建）"
        has_issue
        return
    fi
    if [ -w "$d" ]; then
        good "${label}存在且可写：$d"
    else
        if [ "$IS_ROOT" = "1" ]; then
            warn "${label}存在，但当前用户不可写（root 可绕过；确认目录属主）：$d"
            say "        怎么修：sudo chown -R root:root $d && sudo chmod -R u+rwX $d"
        else
            bad "${label}存在但不可写（容器以 root 运行不受影响，但体检以 root 为准）：$d"
            say "        怎么修：用 sudo 重新体检；或 sudo chown -R root:root $d"
            has_issue
        fi
    fi
}
check_dir "$DATA_ROOT/iso"     "ISO 目录"
check_dir "$DATA_ROOT/pxe-web" "PXE HTTP 目录"
check_dir "$DATA_ROOT/ztp-web" "ZTP HTTP 目录"
check_dir "$DATA_ROOT/state"   "dnsmasq 重载状态目录"
check_dir "$DATA_ROOT/mnt"     "ISO 挂载点"
check_dir "$TFTP_ROOT"         "TFTP 目录"
check_dir "/etc/dnsmasq.d"     "dnsmasq 配置目录"
check_dir "/var/lib/dnsmasq"   "dnsmasq 租约目录（ZTP 落位认领用）"

# ---------- 5. 磁盘余量 ----------
section "5 / 磁盘余量（数据根所在分区）"
disk_free_mb() { df -Pm "$1" 2>/dev/null | awk 'NR==2{print $4}'; }
free_mb="$(disk_free_mb "$DATA_ROOT")"
if [ -n "$free_mb" ]; then
    free_gb=$((free_mb/1024))
    if [ "$free_gb" -ge 20 ]; then
        good "数据根分区可用 ${free_gb} GB（>=20 GB，够放 ISO 镜像）"
    elif [ "$free_gb" -ge 5 ]; then
        warn "数据根分区可用 ${free_gb} GB（不足 20 GB）—— 加 ISO 镜像可能放不下"
        say "        怎么修：清磁盘或换大盘（改 .env 的 OPSTK_DATA_ROOT / OPSTK_TFTP_ROOT，见 README）"
    else
        bad "数据根分区可用 ${free_gb} GB（<5 GB）—— 很快会写满，必须先清理/扩容"
        has_issue
    fi
else
    warn "取不到 $DATA_ROOT 的磁盘信息（目录不存在？跳过）"
fi
DOCKER_ROOT="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
if [ -n "$DOCKER_ROOT" ]; then
    dfree="$(disk_free_mb "$DOCKER_ROOT")"
    if [ -n "$dfree" ] && [ $((dfree/1024)) -lt 5 ]; then
        warn "Docker 数据目录 $DOCKER_ROOT 余量 $((dfree/1024)) GB，低于 5 GB（load 镜像可能失败）"
        say "        怎么修：docker system df 看占用；docker image prune 清理悬空镜像"
    else
        good "Docker 数据目录 $DOCKER_ROOT 余量 $((dfree/1024)) GB"
    fi
fi

# ---------- 6. ISO 镜像 ----------
section "6 / ISO 目录（${DATA_ROOT}/iso）"
if [ -d "$DATA_ROOT/iso" ]; then
    iso_count="$(find "$DATA_ROOT/iso" -maxdepth 1 -type f \( -iname '*.iso' -o -iname '*.img' \) 2>/dev/null | wc -l)"
    if [ "${iso_count:-0}" -gt 0 ]; then
        good "已有 ${iso_count} 个 ISO 镜像（前 3 个）："
        find "$DATA_ROOT/iso" -maxdepth 1 -type f \( -iname '*.iso' -o -iname '*.img' \) 2>/dev/null | head -3 | while read -r f; do
            printf '          · %s（%s）\n' "$(basename "$f")" "$(du -h "$f" | awk '{print $1}')"
        done
    else
        warn "还没有 ISO 镜像 —— 首次打开网页后按引导「③ 添加 ISO 镜像」上传/导入"
    fi
fi

# ---------- 7. 容器 / 镜像 / 服务 ----------
section "7 / OpsToolkit 容器与镜像"
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    if docker image inspect "$OPSTK_IMAGE" >/dev/null 2>&1; then
        good "镜像存在：$OPSTK_IMAGE"
    else
        warn "镜像 $OPSTK_IMAGE 不存在（离线包 install.sh 会 docker load；或改 .env 的 OPSTK_IMAGE）"
    fi
    CT_STATUS="$(docker ps -a --filter "name=^/${CNAME}$" --format '{{.Status}}' 2>/dev/null | head -n1)"
    if [ -z "$CT_STATUS" ]; then
        warn "容器 $CNAME 不存在（还没安装？在离线包目录执行 sudo bash install.sh）"
    elif printf '%s' "$CT_STATUS" | grep -q '^Up'; then
        good "容器在运行：$CNAME（$CT_STATUS）"
    else
        bad "容器存在但没在运行：$CNAME（$CT_STATUS）"
        say "        怎么修：docker start $CNAME ；看失败原因：docker logs $CNAME --tail 50"
        has_issue
    fi
    if [ "$CT_STATUS" ] && printf '%s' "$CT_STATUS" | grep -q '^Up'; then
        if command -v curl >/dev/null 2>&1; then
            CODE="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)"
            if [ "$CODE" = "200" ]; then
                good "本机访问 http://127.0.0.1:${PORT}/health 正常（200）"
            else
                bad "容器在跑但 /health 不可达（HTTP=${CODE:-无}）"
                say "        怎么修：docker logs $CNAME --tail 50 看报错；常见是 .env 缺密钥或端口被占"
                has_issue
            fi
        else
            warn "本机没有 curl，跳过 /health 探测（可改用浏览器访问 http://<本机IP>:${PORT}）"
        fi
    fi
fi

# ---------- 8. 密钥 ----------
section "8 / 应用密钥（backend/.env，只检查存在性，**不显示内容**）"
if [ -n "$APP_ENV" ] && [ -f "$APP_ENV" ]; then
    for KEY in credential_key secret_key pxe_serve_token ztp_serve_token; do
        if grep -qE "^${KEY}=.+" "$APP_ENV"; then
            good "${KEY}：已设置"
        else
            bad "${KEY}：缺失或为空"
            say "        怎么修：重跑安装脚本（幂等，只会补缺失的项）：sudo bash $INSTALLER"
            has_issue
        fi
    done
else
    bad "没找到应用密钥文件 $APP_ENV"
    say "        怎么修：在离线包目录执行 sudo bash $INSTALLER（会自动生成密钥）"
    has_issue
fi

# ---------- 9. 宿主机 dnsmasq 重载单元 ----------
section "9 / 宿主机 dnsmasq 重载单元（用 PXE/ZTP 前必须装）"
if [ "$IS_ROOT" = "1" ] && command -v systemctl >/dev/null 2>&1; then
    if [ "$(systemctl is-active opstk-dnsmasq-reload.path 2>/dev/null || true)" = "active" ]; then
        good "opstk-dnsmasq-reload.path 在运行（部署配置后 dnsmasq 会自动重载）"
    else
        warn "opstk-dnsmasq-reload.path 未安装/未运行 —— PXE/ZTP 部署时配置不会自动生效"
        say "        怎么修：sudo bash $HERE/deploy/host/install-opstk-dnsmasq-reload.sh"
    fi
    if systemctl is-active dnsmasq >/dev/null 2>&1; then
        good "本机 dnsmasq 服务在运行（DHCP/TFTP 由它提供）"
    elif [ -f /etc/dnsmasq.d/opstk-pxe.conf ]; then
        warn "发现 opstk-pxe.conf 但 dnsmasq 服务没在运行 —— PXE 装机会失败"
        say "        怎么修：sudo systemctl enable --now dnsmasq"
        has_issue
    else
        good "尚未配置 PXE（没有 opstk-pxe.conf），dnsmasq 未运行属正常"
    fi
else
    warn "非 root 或无 systemctl，跳过 systemd 检查；用 sudo 重跑可查"
fi

# ---------- 总结 ----------
say ""
if [ "$ISSUES" -eq 0 ]; then
    say "${C_G}体检结论：全部通过，或仅有不需要立即处理的[注意]项。${C_N}"
    exit 0
else
    say "${C_R}体检发现 ${ISSUES} 个[需处理]问题，请按上面各项的「怎么修」处理。${C_N}"
    exit 1
fi
