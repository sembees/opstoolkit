#!/usr/bin/env bash
# =============================================================================
# OpsToolkit 一键安装（目标机上运行；**全程不需要联网**）
#
# 用法（在解压出来的离线包目录里）：
#   sudo bash install.sh                       # 默认安装：/opt/opstk + /srv/opstk + /srv/tftp + 8000 端口
#   sudo bash install.sh --data-root /data2/opstk --tftp-root /data2/tftp   # 换盘
#   sudo bash install.sh --port 18000          # 端口被占时换端口
#   sudo bash install.sh --no-start            # 只准备目录/.env/密钥，不启动（演练用）
#   sudo bash install.sh --upgrade             # 升级：换镜像，保留全部数据与密钥
#
# 可选参数：
#   --data-root <目录>    业务数据根（默认 /srv/opstk；自动建 iso/pxe-web/ztp-web/state/mnt）
#   --tftp-root <目录>    TFTP 根（默认：给了 --data-root 就跟到 <data-root>/tftp，否则 /srv/tftp）
#   --port <n>            Web 端口（默认 8000）
#   --install-dir <目录>  安装根（compose 项目根；默认 /opt/opstk）
#   --name <名字>         容器名（默认 opstoolkit；并行演练时改掉）
#   --image <镜像:标签>   跳过 docker load，直接用本机已有镜像（演练用）
#   --no-start / --upgrade / -h
#
# 幂等：重复执行安全 —— 目录已存在就跳过，.env 与密钥已存在就保留，绝不覆盖。
# =============================================================================
set -euo pipefail

# ---------- 提示输出（全部中文） ----------
if [ -t 1 ]; then
    C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[1m'; C_N=$'\e[0m'
else
    C_G=""; C_Y=""; C_R=""; C_B=""; C_N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_N"; }
ok()   { printf '  %s[完成]%s %s\n' "$C_G" "$C_N" "$*"; }
warn() { printf '  %s[注意]%s %s\n' "$C_Y" "$C_N" "$*"; }
install_die() { printf '\n%s[安装中止]%s %s\n' "$C_R" "$C_N" "$*" >&2; exit 1; }

usage() {
    cat <<'EOF'
OpsToolkit 一键安装（目标机离线运行）

用法: sudo bash install.sh [选项]

选项:
  --data-root <目录>    业务数据根（默认 /srv/opstk，自动建 iso/pxe-web/ztp-web/state/mnt）
  --tftp-root <目录>    TFTP 根（默认跟随 --data-root：<data-root>/tftp；都不给则 /srv/tftp）
  --port <n>            Web 端口（默认 8000）
  --install-dir <目录>  安装根/compose 项目根（默认 /opt/opstk）
  --name <名字>         容器名（默认 opstoolkit）
  --image <镜像:标签>   跳过 docker load，用本机已有镜像（演练用）
  --upgrade             升级模式：只换镜像，数据目录与 .env 密钥一律不动
  --no-start            只准备目录/.env/密钥，不启动容器（演练用）
  -h, --help            显示本帮助
EOF
}

# 本机全局 IPv4（排除 docker/网桥/虚拟接口），用于打印访问地址
detect_ips() {
    local ips=""
    if command -v ip >/dev/null 2>&1; then
        ips="$(ip -4 -o addr show scope global 2>/dev/null \
               | awk '{print $2, $4}' | cut -d/ -f1 \
               | grep -vE '^(docker|br-|veth|virbr|lo)' | awk '{print $2}' || true)"
    fi
    if [ -z "$ips" ] && command -v hostname >/dev/null 2>&1; then
        ips="$(hostname -I 2>/dev/null || true)"
    fi
    printf '%s\n' $ips | grep -v '^$' | sort -u
}

# 从已有 .env 读某变量的值（没有则给默认值）——升级/重跑时"已配置优先"
read_env_var() {  # read_env_var <文件> <键> <默认值>
    local f="$1" k="$2" d="$3" v=""
    if [ -f "$f" ]; then v="$(grep -E "^${k}=" "$f" | tail -n1 | cut -d= -f2- || true)"; fi
    printf '%s' "${v:-$d}"
}

# ---------- 参数解析（先于一切使用） ----------
DATA_ROOT_ARG=""; TFTP_ROOT_ARG=""; PORT_ARG=""; IMAGE_ARG=""; CNAME_ARG=""
INSTALL_DIR="/opt/opstk"; UPGRADE=0; NO_START=0
while [ $# -gt 0 ]; do
    case "$1" in
        --data-root)     DATA_ROOT_ARG="${2:-}";  shift 2 ;;
        --data-root=*)   DATA_ROOT_ARG="${1#*=}"; shift   ;;
        --tftp-root)     TFTP_ROOT_ARG="${2:-}";  shift 2 ;;
        --tftp-root=*)   TFTP_ROOT_ARG="${1#*=}"; shift   ;;
        --port)          PORT_ARG="${2:-}";       shift 2 ;;
        --port=*)        PORT_ARG="${1#*=}";      shift   ;;
        --image)         IMAGE_ARG="${2:-}";      shift 2 ;;
        --image=*)       IMAGE_ARG="${1#*=}";     shift   ;;
        --install-dir)   INSTALL_DIR="${2:-}";    shift 2 ;;
        --install-dir=*) INSTALL_DIR="${1#*=}";   shift   ;;
        --name)          CNAME_ARG="${2:-}";      shift 2 ;;
        --name=*)        CNAME_ARG="${1#*=}";     shift   ;;
        --upgrade)       UPGRADE=1; shift ;;
        --no-start)      NO_START=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *)               install_die "未知参数：$1（用 -h 看用法）" ;;
    esac
done
if [ -n "$PORT_ARG" ] && ! printf '%s' "$PORT_ARG" | grep -qE '^[0-9]+$'; then
    install_die "--port 必须是数字，收到：$PORT_ARG"
fi
if [ ! -d "$(dirname "$INSTALL_DIR")" ]; then
    install_die "--install-dir 的父目录不存在：$(dirname "$INSTALL_DIR")"
fi
# 只给了 --data-root 时，TFTP 根默认跟到数据根下面（换盘通常一起换；
# 想分开就显式给 --tftp-root）。这样沙箱/演练数据根可以完全自包含。
if [ -n "$DATA_ROOT_ARG" ] && [ -z "$TFTP_ROOT_ARG" ]; then
    TFTP_ROOT_ARG="$DATA_ROOT_ARG/tftp"
fi

# ---------- 0. 基础检查 ----------
step "第 0 步 / 基础检查"
[ "$(id -u)" = "0" ] || install_die "请用 root 运行：sudo bash install.sh"
command -v docker >/dev/null 2>&1 || install_die "没找到 docker。请先在目标机安装 Docker（本离线包不含 Docker 本体）"
if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
else
    install_die "没找到 docker compose 插件。请先安装 docker-compose-plugin（离线安装可向交付方索取）"
fi
ok "root ✓；$(docker --version | sed 's/, version/ /;s/,.*//')；compose ✓"

BUNDLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 兼容两种摆放：离线包根（install.sh 与 deploy/ 同级）、仓库根（packaging/ 与 deploy/ 同级）
if [ ! -f "$BUNDLE/deploy/docker-compose.yml" ] && [ -f "$BUNDLE/../deploy/docker-compose.yml" ]; then
    BUNDLE="$(cd "$BUNDLE/.." && pwd)"
fi
[ -f "$BUNDLE/deploy/docker-compose.yml" ]       || install_die "离线包不完整：缺 $BUNDLE/deploy/docker-compose.yml"
[ -f "$BUNDLE/deploy/container-entrypoint.sh" ]  || install_die "离线包不完整：缺 $BUNDLE/deploy/container-entrypoint.sh"

# ---------- 1. 镜像 ----------
step "第 1 步 / 加载镜像（docker load）"
if [ -n "$IMAGE_ARG" ]; then
    OPSTK_IMAGE_VALUE="$IMAGE_ARG"
    warn "按参数指定，跳过 docker load：使用本机已有镜像 $OPSTK_IMAGE_VALUE"
else
    IMG_TAR="$(ls "$BUNDLE"/opstk-image-*.tar 2>/dev/null | head -n1 || true)"
    [ -n "$IMG_TAR" ] || install_die "离线包不完整：没找到 opstk-image-*.tar（镜像 tar 应与 install.sh 同目录）"
    ok "找到镜像文件：$(basename "$IMG_TAR")（$(du -h "$IMG_TAR" | awk '{print $1}')，加载约需几十秒）"
    docker load -i "$IMG_TAR"
    ok "docker load 完成"
    # 打包脚本会写入 IMAGE_REF（准确的 镜像:标签），优先采用；没有才从 tar 文件名推断
    if [ -f "$BUNDLE/IMAGE_REF" ]; then
        OPSTK_IMAGE_VALUE="$(head -n1 "$BUNDLE/IMAGE_REF" | tr -d '[:space:]')"
    else
        OPSTK_IMAGE_VALUE="opstk:$(basename "$IMG_TAR" | sed -E 's/^opstk-image-(.+)\.tar$/\1/')"
    fi
fi
docker image inspect "$OPSTK_IMAGE_VALUE" >/dev/null 2>&1 \
    || warn "镜像 $OPSTK_IMAGE_VALUE 在本机不存在（load 可能失败），继续执行，稍后 compose 会报错提示"

# ---------- 2. 目录与权限 ----------
step "第 2 步 / 创建数据目录并设置权限"
COMPOSE_ENV="$INSTALL_DIR/.env"
DATA_ROOT="${DATA_ROOT_ARG:-$(read_env_var "$COMPOSE_ENV" OPSTK_DATA_ROOT /srv/opstk)}"
TFTP_ROOT="${TFTP_ROOT_ARG:-$(read_env_var "$COMPOSE_ENV" OPSTK_TFTP_ROOT /srv/tftp)}"
PORT="${PORT_ARG:-$(read_env_var "$COMPOSE_ENV" OPSTK_PORT 8000)}"

mkdir -p "$DATA_ROOT/iso" "$DATA_ROOT/pxe-web" "$DATA_ROOT/ztp-web" "$DATA_ROOT/state" "$DATA_ROOT/mnt" "$TFTP_ROOT" "$TFTP_ROOT/boot"
chmod 755 "$DATA_ROOT" "$DATA_ROOT/iso" "$DATA_ROOT/pxe-web" "$DATA_ROOT/ztp-web" "$DATA_ROOT/state" "$DATA_ROOT/mnt" "$TFTP_ROOT"
ok "数据根就绪：$DATA_ROOT（iso / pxe-web / ztp-web / state / mnt）"
ok "TFTP 根就绪：$TFTP_ROOT（含 boot/）"

# ---------- 3. 应用密钥（已存在则绝不覆盖） ----------
step "第 3 步 / 生成应用密钥（credential_key / secret_key / pxe_serve_token / ztp_serve_token）"
gen_hex32() {
    local h=""
    if command -v openssl >/dev/null 2>&1; then
        h="$(openssl rand -hex 32 2>/dev/null || true)"
    fi
    if ! printf '%s' "$h" | grep -qE '^[0-9a-f]{64}$'; then
        # 无 openssl 时退回 /dev/urandom：32 字节 → 64 个 hex 字符
        h="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    fi
    printf '%s' "$h" | grep -qE '^[0-9a-f]{64}$' || install_die "密钥生成失败（openssl 与 /dev/urandom 都不可用？）"
    printf '%s\n' "$h"
}
APP_ENV="$INSTALL_DIR/backend/.env"
mkdir -p "$INSTALL_DIR/backend"
touch "$APP_ENV"
for KEY in credential_key secret_key pxe_serve_token ztp_serve_token; do
    if grep -qE "^${KEY}=.+" "$APP_ENV" 2>/dev/null; then
        ok "  ${KEY}：已存在，保留不动"
    else
        printf '%s=%s\n' "$KEY" "$(gen_hex32)" >> "$APP_ENV"
        ok "  ${KEY}：已生成（64 位随机 hex）"
    fi
done
chmod 600 "$APP_ENV"
ok "应用密钥文件：$APP_ENV（权限 600）"

# ---------- 4. 写安装目录的 .env 与部署文件 ----------
step "第 4 步 / 写安装目录的 .env 与部署文件"
# 容器名/项目名：默认与今天完全一致（opstoolkit / opstk）；
# 装到非默认目录（演练/并行）时**两者都**自动派生，避免和已有实例冲突。
# ★ 真实事故教训（2026-10-07）：这里原先只派生容器名、项目名恒为 "opstk"，
#   于是在 /tmp 演练时与生产**共用同一个 compose 项目** ⇒ `up -d` 把生产容器
#   当成自己的服务重建掉了（表现为生产容器凭空消失、站点中断）。项目名必须跟着目录派生。
PROJ_VALUE="opstk"
if [ "$INSTALL_DIR" != "/opt/opstk" ]; then
    _slug="$(printf '%s' "$(basename "$INSTALL_DIR")" | tr -c 'a-zA-Z0-9_.-' '-' | sed 's/^-*//;s/-*$//')"
    [ -n "$_slug" ] && PROJ_VALUE="opstk-$_slug"
fi
if [ -n "$CNAME_ARG" ]; then
    CNAME_VALUE="$CNAME_ARG"
elif [ "$INSTALL_DIR" != "/opt/opstk" ]; then
    CNAME_VALUE="$(printf '%s' "$(basename "$INSTALL_DIR")" | tr -c 'a-zA-Z0-9_.-' '-' | sed 's/^-*//;s/-*$//')-opstoolkit"
else
    CNAME_VALUE="opstoolkit"
fi
write_compose_env() {  # 全新写入（首次安装）
    cat > "$COMPOSE_ENV" <<EOF
# OpsToolkit 安装目录 .env（install.sh 生成；换盘/换端口改这里，然后重建容器）
OPSTK_PORT=${PORT}
OPSTK_DATA_ROOT=${DATA_ROOT}
OPSTK_TFTP_ROOT=${TFTP_ROOT}
OPSTK_IMAGE=${OPSTK_IMAGE_VALUE}
OPSTK_CONTAINER_NAME=${CNAME_VALUE}
OPSTK_PROJECT=${PROJ_VALUE}
EOF
    chmod 600 "$COMPOSE_ENV"
}
upsert_compose_env() {  # 已存在：只更新受影响的项，其余保留（幂等；--upgrade 不动用户配置）
    local k v
    for pair in "OPSTK_PORT|${PORT}" "OPSTK_DATA_ROOT|${DATA_ROOT}" "OPSTK_TFTP_ROOT|${TFTP_ROOT}" \
                "OPSTK_IMAGE|${OPSTK_IMAGE_VALUE}" "OPSTK_CONTAINER_NAME|${CNAME_VALUE}" "OPSTK_PROJECT|${PROJ_VALUE}"; do
        k="${pair%%|*}"; v="${pair#*|}"
        if grep -qE "^${k}=" "$COMPOSE_ENV"; then
            sed -i "s|^${k}=.*|${k}=${v}|" "$COMPOSE_ENV"
        else
            printf '%s=%s\n' "$k" "$v" >> "$COMPOSE_ENV"
        fi
    done
    chmod 600 "$COMPOSE_ENV"
}
if [ -f "$COMPOSE_ENV" ]; then
    upsert_compose_env
    ok ".env 已存在：仅更新部署需要的项（未指定的配置原样保留）"
else
    write_compose_env
    ok ".env 已生成：$COMPOSE_ENV"
fi
mkdir -p "$INSTALL_DIR/deploy"
cp -f "$BUNDLE/deploy/docker-compose.yml"      "$INSTALL_DIR/deploy/docker-compose.yml"
cp -f "$BUNDLE/deploy/container-entrypoint.sh" "$INSTALL_DIR/deploy/container-entrypoint.sh"
if [ -f "$BUNDLE/deploy/.env.example" ]; then
    cp -f "$BUNDLE/deploy/.env.example" "$INSTALL_DIR/deploy/.env.example"
fi
chmod 644 "$INSTALL_DIR/deploy/docker-compose.yml" "$INSTALL_DIR/deploy/container-entrypoint.sh"
ok "部署文件就绪：$INSTALL_DIR/deploy/"

# 同名容器归属检查：避免误伤别的 compose 项目里已在跑的同名容器
if docker ps -a --format '{{.Names}}' | grep -qx "$CNAME_VALUE"; then
    OWNER_PROJ="$(docker inspect "$CNAME_VALUE" --format '{{index .Config.Labels "com.docker.compose.project"}}' 2>/dev/null || true)"
    if [ -n "$OWNER_PROJ" ] && [ "$OWNER_PROJ" != "$PROJ_VALUE" ]; then
        install_die "本机已有同名容器 $CNAME_VALUE（属于 compose 项目 $OWNER_PROJ）。
  请换容器名重跑：sudo bash install.sh --name <新名字>
  或确认旧容器可下线后：docker rm -f $CNAME_VALUE"
    fi
fi

# ★ 项目级归属检查（2026-10-07 事故后补，失败即中止）：
#   同名 compose 项目若已指向**别的部署目录**，继续 up -d 会把那个实例的容器当成
#   本项目的服务重建掉（正是这次把生产容器搞没的机制）。这里直接拒绝，绝不冒险。
#   ★ 判据用"**最终生效**的项目名"：compose 读的是 .env 里的 OPSTK_PROJECT
#   （见 docker-compose.yml 的 `name: ${OPSTK_PROJECT:-opstk}`），它可能被用户手改过，
#   所以必须以文件里的值为准，不能只看脚本派生的默认值。
EFF_PROJ="$PROJ_VALUE"
if [ -f "$COMPOSE_ENV" ]; then
    _p="$(sed -n 's/^OPSTK_PROJECT=//p' "$COMPOSE_ENV" | tail -1)"
    [ -n "$_p" ] && EFF_PROJ="$_p"
fi
_existing_cfg="$(docker ps -a --filter "label=com.docker.compose.project=$EFF_PROJ" \
    --format '{{.Label "com.docker.compose.project.config_files"}}' 2>/dev/null | sort -u | grep -v '^$' | head -1 || true)"
if [ -n "$_existing_cfg" ]; then
    case "$_existing_cfg" in
        *"$INSTALL_DIR/deploy/docker-compose.yml"*) : ;;   # 就是本目录 ⇒ 幂等重跑，放行
        *) install_die "compose 项目名 $EFF_PROJ 已被另一个部署占用，拒绝继续：
    已在用的配置文件： $_existing_cfg
    本次将使用的配置： $INSTALL_DIR/deploy/docker-compose.yml
  继续执行会把那个实例的容器当成自己的服务重建掉（会误伤生产）。请任选其一：
    · 换安装目录（项目名会自动跟着目录派生，互不干扰）
    · 或把安装目录下 .env 里的 OPSTK_PROJECT 改成没被占用的名字再装"
           ;;
    esac
fi

# ---------- 5/6. 启动与体检 ----------
if [ "$NO_START" = "1" ]; then
    step "第 5 步 / 按要求跳过启动（--no-start）"
    ok "目录、.env、密钥已就绪，未创建容器。"
    MANUAL_UP="${COMPOSE[*]} --project-directory $INSTALL_DIR -f $INSTALL_DIR/deploy/docker-compose.yml up -d"
else
    step "第 5 步 / 启动容器（docker compose up -d）"
    "${COMPOSE[@]}" --project-directory "$INSTALL_DIR" -f "$INSTALL_DIR/deploy/docker-compose.yml" up -d
    ok "容器已创建：$CNAME_VALUE"

    step "第 6 步 / 等待服务就绪（GET /health，最多 180 秒）"
    i=0; HEALTHY=0
    while [ "$i" -lt 90 ]; do
        if command -v curl >/dev/null 2>&1; then
            CODE="$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:${PORT}/health" 2>/dev/null || true)"
            [ "$CODE" = "200" ] && { HEALTHY=1; break; }
        else
            if (exec 3<>"/dev/tcp/127.0.0.1/${PORT}") 2>/dev/null; then
                HEALTHY=1; break
            fi
        fi
        i=$((i+1)); sleep 2
    done
    if [ "$HEALTHY" != "1" ]; then
        warn "等待 /health 超时。看日志找原因：docker logs $CNAME_VALUE --tail 50"
        warn "常见原因：端口被占（sudo bash $BUNDLE/packaging/doctor.sh 或包内 doctor.sh 看 53/67/69/${PORT} 被谁占）；磁盘满；.env 格式错误。"
        install_die "服务未就绪（容器还在，排查后直接重跑本脚本即可，幂等）"
    fi
    ok "服务已就绪：/health 返回 200"
    MANUAL_UP="${COMPOSE[*]} --project-directory $INSTALL_DIR -f $INSTALL_DIR/deploy/docker-compose.yml up -d"
fi

# ---------- 7. 收尾：告诉小白下一步干什么 ----------
IP_LIST="$(detect_ips)"
if [ -f "$BUNDLE/doctor.sh" ]; then DOCTOR_PATH="$BUNDLE/doctor.sh"; else DOCTOR_PATH="$BUNDLE/packaging/doctor.sh"; fi
UPGRADE_SUFFIX=""
[ "$UPGRADE" = "1" ] && UPGRADE_SUFFIX="（升级）"
say ""
say "${C_B}==============================================================${C_N}"
say "  OpsToolkit 安装完成${UPGRADE_SUFFIX}"
say "${C_B}==============================================================${C_N}"
say ""
if [ "$NO_START" = "1" ]; then
    say "  （--no-start：未启动。手动启动命令见下方「以后手动启动」。）"
else
    say "  用浏览器打开："
    for ip in $IP_LIST; do
        say "    ${C_G}http://${ip}:${PORT}${C_N}"
    done
fi
say ""
say "  首次打开网页会引导你完成三件事："
say "    ① 改管理员口令   ② 环境体检   ③ 添加 ISO 镜像"
say ""
if [ "$NO_START" = "1" ]; then
    say "  初始管理员口令：启动后用这条命令取（只打印一次）："
    say "    docker logs ${CNAME_VALUE} 2>&1 | grep 初始随机口令"
else
    INIT_PW_LINE="$(docker logs "$CNAME_VALUE" 2>&1 | grep -m1 '初始随机口令' || true)"
    if [ -n "$INIT_PW_LINE" ]; then
        say "  初始管理员口令（只打印这一次，登录后请立即修改）："
        say "    ${INIT_PW_LINE}"
    else
        say "  初始管理员口令：未取到（可能已设过固定口令）。需要时执行："
        say "    docker logs ${CNAME_VALUE} 2>&1 | grep 初始随机口令"
    fi
fi
say ""
say "  以后手动启动（幂等，可随时重跑）："
say "    ${MANUAL_UP}"
say ""
say "  常用命令："
say "    看状态：  docker ps --filter name=${CNAME_VALUE}"
say "    看日志：  docker logs --tail 100 ${CNAME_VALUE}"
say "    体检：    sudo bash $DOCTOR_PATH"
say "    升级：    新离线包解压后 sudo bash install.sh --upgrade（数据与密钥原样保留）"
say "    用 PXE/ZTP 前（宿主机装一次 dnsmasq 重载单元）："
say "              sudo bash $BUNDLE/deploy/host/install-opstk-dnsmasq-reload.sh"
say ""
exit 0
