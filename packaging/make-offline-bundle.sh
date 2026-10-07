#!/usr/bin/env bash
# =============================================================================
# OpsToolkit 离线包制作（在【有网 + 有 Docker】的机器上运行）
#
# 干什么：构建自包含镜像 → docker save → 连同部署描述/安装/体检脚本/宿主单元/
#         文档一起打包成 opstk-offline-<版本>-<日期>.tar.zst（+ .sha256），
#         最后打印包大小与目标机三步命令。目标机**全程不需要联网**。
#         ★ 会把源码指纹写进包内 SOURCE-REVISION.txt（git 记 HEAD+脏标记；
#           非 git 记 frontend/src/backend/app 的文件数与最近修改时间并醒目提醒），
#         结尾也打印这行 —— 让人一眼核对"这个包是用哪一版源码打的"。
#
# 用法（在仓库根目录执行；也可用 --repo 指定仓库路径）：
#   bash packaging/make-offline-bundle.sh                          # 构建镜像并打包（需联网）
#   bash packaging/make-offline-bundle.sh --version v1.2.0         # 指定版本号
#   bash packaging/make-offline-bundle.sh --image opstk:v1.2.0     # 不构建，直接打包已有镜像
#   bash packaging/make-offline-bundle.sh --skip-build             # 跳过构建（打已有 opstk:<版本>）
#
# 参数：
#   --version <vX.Y.Z>   版本号（默认 git describe --tags，无标签则用当天日期 vYYYYMMDD）
#   --image <名称:标签>  跳过 docker build，直接打包该镜像（冒烟/复打用）
#   --skip-build         等价 --image opstk:<版本>（镜像须已存在）
#   --out <目录>         产物目录（默认 <仓库>/dist）
#
# 需要联网的只有 docker build 里的 npm / apt / pip 三步；docker save 与 tar 均离线。
# 打包机没有 zstd 时自动退回 .tar.gz（目标机解压命令随之变化，脚本会打印对应命令）。
# =============================================================================
set -euo pipefail

if [ -t 1 ]; then
    C_G=$'\e[32m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[1m'; C_N=$'\e[0m'
else
    C_G=""; C_Y=""; C_R=""; C_B=""; C_N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==> %s%s\n' "$C_B" "$*" "$C_N"; }
ok()   { printf '  %s[完成]%s %s\n' "$C_G" "$C_N" "$*"; }
die()  { printf '\n%s[打包中止]%s %s\n' "$C_R" "$C_N" "$*" >&2; exit 1; }

# ---------- 参数 ----------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
VERSION=""; IMAGE=""; OUT=""
SKIP_BUILD=0
while [ $# -gt 0 ]; do
    case "$1" in
        --version)    VERSION="${2:-}"; shift 2 ;;
        --version=*)  VERSION="${1#*=}"; shift   ;;
        --image)      IMAGE="${2:-}";   shift 2 ;;
        --image=*)    IMAGE="${1#*=}";  shift   ;;
        --skip-build) SKIP_BUILD=1;     shift   ;;
        --out)        OUT="${2:-}";     shift 2 ;;
        --out=*)      OUT="${1#*=}";    shift   ;;
        -h|--help)    sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) die "未知参数：$1（用 -h 看用法）" ;;
    esac
done
[ -f "$REPO/deploy/docker-compose.yml" ] || die "没找到 $REPO/deploy/docker-compose.yml —— 请在仓库根下运行，或用 --repo 指定"
command -v docker >/dev/null 2>&1 || die "打包机需要 docker（构建与 docker save）"

# ---------- 版本与镜像 ----------
if [ -z "$VERSION" ]; then
    VERSION="$(git -C "$REPO" describe --tags --always 2>/dev/null || true)"
    [ -n "$VERSION" ] || VERSION="v$(date +%Y%m%d)"
fi
if [ -z "$IMAGE" ]; then
    IMAGE="opstk:${VERSION}"
fi
IMAGE_TAR_NAME="opstk-image-$(printf '%s' "$IMAGE" | sed 's|.*/||; s/:/-/g').tar"
DATE_STR="$(date +%Y%m%d)"
PKG_DIR_NAME="opstk-offline-${VERSION}-${DATE_STR}"
OUT="${OUT:-$REPO/dist}"
STAGE="$OUT/$PKG_DIR_NAME"

say "仓库：      $REPO"
say "版本：      $VERSION"
say "镜像：      $IMAGE"
say "输出目录：  $OUT"

# ---------- 1. 镜像（构建或复用） ----------
step "第 1 步 / 准备镜像"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    ok "镜像已存在：$IMAGE（复用，不再构建）"
else
    if [ "$SKIP_BUILD" = "1" ]; then
        die "指定了 --skip-build 但镜像 $IMAGE 不存在。先 docker build 或改用 --image <已有镜像>"
    fi
    # ★ 真正构建需要联网（npm ci / apt-get / pip3）。打包机必须能访问外网或内网镜像源。
    ok "开始构建镜像（需要联网：node 阶段 npm ci、运行阶段 apt/pip）……"
    docker build -f "$REPO/deploy/Dockerfile" \
        --build-arg OPSTK_VERSION="$VERSION" \
        -t "$IMAGE" "$REPO"
    ok "镜像构建完成：$IMAGE"
fi
IMAGE_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}' 2>/dev/null || true)"
say "镜像 ID：   ${IMAGE_ID:-未知}"

# ---------- 2. 组装离线包内容 ----------
step "第 2 步 / 收集离线包内容"
rm -rf "$OUT/$PKG_DIR_NAME"
mkdir -p "$OUT/$PKG_DIR_NAME/deploy/host"
STAGE="$OUT/$PKG_DIR_NAME"
printf '%s\n' "$IMAGE" > "$STAGE/IMAGE_REF"   # install.sh 优先读它拿准确镜像名:标签

cp -f "$REPO/deploy/docker-compose.yml"        "$STAGE/deploy/docker-compose.yml"
cp -f "$REPO/deploy/container-entrypoint.sh"   "$STAGE/deploy/container-entrypoint.sh"
cp -f "$REPO/deploy/.env.example"              "$STAGE/deploy/.env.example"
cp -f "$REPO/deploy/Dockerfile"                "$STAGE/deploy/Dockerfile"
[ -f "$REPO/deploy/Dockerfile.dockerignore" ] && cp -f "$REPO/deploy/Dockerfile.dockerignore" "$STAGE/deploy/" || true
[ -f "$REPO/deploy/docker-compose.dev.yml" ] && cp -f "$REPO/deploy/docker-compose.dev.yml" "$STAGE/deploy/" || true
cp -f "$REPO/deploy/README-离线部署.md"        "$STAGE/deploy/README-离线部署.md"
cp -f "$REPO/deploy/README-离线部署.md"        "$STAGE/README-离线部署.md"
# 宿主机 systemd 单元（dnsmasq 重载）与安装脚本 —— 原样随包
mkdir -p "$STAGE/deploy/host"
cp -f "$REPO"/deploy/host/* "$STAGE/deploy/host/"
# 安装与体检脚本
cp -f "$REPO/packaging/install.sh" "$STAGE/install.sh"
cp -f "$REPO/packaging/doctor.sh"  "$STAGE/doctor.sh"
chmod 755 "$STAGE/install.sh" "$STAGE/doctor.sh" "$STAGE/deploy/container-entrypoint.sh" \
          "$STAGE/deploy/host/install-opstk-dnsmasq-reload.sh" "$STAGE/deploy/host/opstk-dnsmasq-reload.sh"
printf '%s\n' "$VERSION" > "$STAGE/VERSION"
ok "部署描述/安装/体检/文档/宿主单元 已就位"

# ---------- 2b. 源码指纹（★ 实测缺口④：证明这个包是用哪一版源码打的） ----------
# 事故背景：镜像曾从应用机上的【旧前端源码】构建 ⇒ 包里前端没有向导、后端却是新的（混版）。
# 这里把源码指纹写进包内 SOURCE-REVISION.txt，让"包是哪一版源码打的"可一眼核对：
#   git 仓库 → git rev-parse HEAD + git status --porcelain（脏标记）；
#   非 git   → frontend/src 与 backend/app 的文件数与最近修改时间（并醒目提醒打包方）。
step "第 2b 步 / 写源码指纹（SOURCE-REVISION.txt，让人一眼看到打了哪版）"
SRC_REV_FILE="$STAGE/SOURCE-REVISION.txt"
SRC_REV_SUMMARY="（未生成）"
SRC_REV_IS_GIT=0
if git -C "$REPO" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    SRC_REV_IS_GIT=1
    GIT_HEAD="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
    GIT_SHORT="$(git -C "$REPO" log -1 --format='%h' 2>/dev/null || true)"
    GIT_SUBJ="$(git -C "$REPO" log -1 --format='%s (%ci)' 2>/dev/null || true)"
    GIT_DESC="$(git -C "$REPO" describe --tags --always 2>/dev/null || true)"
    GIT_DIRTY="$(git -C "$REPO" status --porcelain 2>/dev/null || true)"
    if [ -n "$GIT_DIRTY" ]; then
        DIRTY_N="$(printf '%s\n' "$GIT_DIRTY" | grep -c . || true)"
        DIRTY_LABEL="有未提交改动（${DIRTY_N} 个路径）⇒ 镜像内容未必等于该 commit，发布前请先提交"
        SRC_REV_SUMMARY="git ${GIT_SHORT}（脏：${DIRTY_N} 项未提交）"
    else
        DIRTY_LABEL="干净（工作区无未提交改动，镜像可追溯到该 commit）"
        SRC_REV_SUMMARY="git ${GIT_SHORT}（干净）"
    fi
    {
        echo "OpsToolkit 离线包 源码指纹"
        echo "来源:         git 仓库（$REPO）"
        echo "HEAD:         ${GIT_HEAD:-未知}"
        echo "HEAD 摘要:    ${GIT_SHORT:-未知} ${GIT_SUBJ}"
        [ -n "$GIT_DESC" ] && echo "git describe: $GIT_DESC"
        echo "脏标记:       $DIRTY_LABEL"
        if [ -n "$GIT_DIRTY" ]; then
            echo "未提交改动（git status --porcelain，最多列 30 行）:"
            printf '%s\n' "$GIT_DIRTY" | head -30 | sed 's/^/  /'
        fi
        echo "打包时间:     $(date '+%Y-%m-%d %H:%M:%S %Z')"
        echo "说明:         对照 SOURCE-REVISION 与 install.sh 结尾打印，可确认包里镜像由哪一版源码构建。"
    } > "$SRC_REV_FILE"
    ok "源码指纹：${SRC_REV_SUMMARY}（已写入包内 SOURCE-REVISION.txt）"
else
    FE_FILES="$( (find "$REPO/frontend/src" -type f 2>/dev/null || true) | wc -l | tr -d '[:space:]')"
    BE_FILES="$( (find "$REPO/backend/app"  -type f 2>/dev/null || true) | wc -l | tr -d '[:space:]')"
    FE_MTIME="$( (find "$REPO/frontend/src" -type f -printf '%TY-%Tm-%Td %TH:%TM\n' 2>/dev/null || true) | sort | tail -1)"
    BE_MTIME="$( (find "$REPO/backend/app"  -type f -printf '%TY-%Tm-%Td %TH:%TM\n' 2>/dev/null || true) | sort | tail -1)"
    [ -n "$FE_MTIME" ] || FE_MTIME="未知"
    [ -n "$BE_MTIME" ] || BE_MTIME="未知"
    {
        echo "OpsToolkit 离线包 源码指纹"
        echo "来源:          非 git 仓库（无法用 commit 证明源码版本）"
        echo "frontend/src:  ${FE_FILES} 个文件，最近修改 ${FE_MTIME}"
        echo "backend/app:   ${BE_FILES} 个文件，最近修改 ${BE_MTIME}"
        echo "说明:          镜像曾实测从旧前端源码构建造成「前端没向导、后端是新的」混版镜像；"
        echo "               请与打包方核对 frontend/src 是完整最新源码（含 Setup.vue 等向导源码）。"
        echo "打包时间:      $(date '+%Y-%m-%d %H:%M:%S %Z')"
    } > "$SRC_REV_FILE"
    SRC_REV_SUMMARY="非 git 仓库：frontend/src ${FE_FILES} 文件 / backend/app ${BE_FILES} 文件（最近修改 ${FE_MTIME%% *}）"
    ok "源码指纹：${SRC_REV_SUMMARY}（已写入包内 SOURCE-REVISION.txt）"
    say ""
    say "${C_Y}  ⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠${C_N}"
    say "${C_Y}  ⚠ 本次打包目录不是 git 仓库，无法证明源码版本。${C_N}"
    say "${C_Y}  ⚠ 正式发布请在 git 检出目录打包（指纹才能指到具体 commit）。${C_N}"
    say "${C_Y}  ⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠⚠${C_N}"
fi

# ---------- 3. docker save ----------
step "第 3 步 / 导出镜像（docker save，约 400MB，需一两分钟）"
docker save "$IMAGE" -o "$STAGE/$IMAGE_TAR_NAME"
ok "镜像已导出：$IMAGE_TAR_NAME（$(du -h "$STAGE/$IMAGE_TAR_NAME" | awk '{print $1}')）"

# ---------- 3b. 清单与自校验 ----------
cat > "$STAGE/MANIFEST.txt" <<EOF
OpsToolkit 离线部署包
版本:     $VERSION
镜像:     $IMAGE ($IMAGE_ID)
源码指纹: $SRC_REV_SUMMARY   （详见包内 SOURCE-REVISION.txt）
打包时间: $(date '+%Y-%m-%d %H:%M:%S %Z')
打包主机: $(hostname 2>/dev/null || echo unknown)
内容:
  opstk-image-*.tar      应用镜像（docker load 导入）
  IMAGE_REF              镜像名:标签（install.sh 读取）
  SOURCE-REVISION.txt    源码指纹（git commit+脏标记；非 git 则为目录统计）
  install.sh             目标机一键安装（目标机不需要联网）
  doctor.sh              只读环境体检
  docker-compose.yml     应用态部署描述（在 deploy/ 下）
  container-entrypoint.sh 容器入口（应用态，端口可配）
  .env.example           部署变量说明（含默认值，均非密钥）
  deploy/host/           宿主机 dnsmasq 重载 systemd 单元 + 安装脚本 + README
  README-离线部署.md     小白向离线部署文档
目标机前提: Docker 已安装（真 Docker，不能是 Podman 伪装的 docker）；root；建议磁盘富余 >= 20 GB；**全程不需要联网**。
EOF
ok "清单已生成：MANIFEST.txt"

# ---------- 4. 压缩 + sha256 ----------
step "第 4 步 / 压缩打包"
ARCHIVE=""
if command -v zstd >/dev/null 2>&1; then
    ARCHIVE="$OUT/${PKG_DIR_NAME}.tar.zst"
    tar -C "$OUT" -cf - "$PKG_DIR_NAME" | zstd -19 -T0 -o "$ARCHIVE"
    UNPACK_HINT="tar --zstd -xf ${ARCHIVE##*/} -C /opt/opstk-pkg"
else
    ARCHIVE="$OUT/${PKG_DIR_NAME}.tar.gz"
    say "  （本机没有 zstd，退回 gzip 打包为 .tar.gz）"
    tar -C "$OUT" -czf "$ARCHIVE" "$PKG_DIR_NAME"
    UNPACK_HINT="tar -xzf ${ARCHIVE##*/} -C /opt/opstk-pkg"
fi
SHA_FILE="${ARCHIVE}.sha256"
( cd "$OUT" && sha256sum "$(basename "$ARCHIVE")" > "$(basename "$SHA_FILE")" )
ok "离线包：$ARCHIVE"
ok "校验和：$SHA_FILE"
say "  包大小：$(du -h "$ARCHIVE" | awk '{print $1}')"
say "  SHA256：$(awk '{print $1}' "$SHA_FILE")"
say ""
say "${C_B}本包源码指纹（一眼核对打了哪一版，详见包内 SOURCE-REVISION.txt）:${C_N}"
say "  ${C_G}$(printf '%s' "$SRC_REV_SUMMARY")${C_N}"
if [ "${SRC_REV_IS_GIT:-0}" != "1" ]; then
    say "  ${C_Y}提醒：本次打包目录不是 git 仓库，无法证明源码版本；正式发布请在 git 检出目录打包。${C_N}"
fi

# ---------- 5. 打印目标机三步命令 ----------
say ""
say "${C_B}=================== 交付方式：把这个文件拷到目标机 ===================${C_N}"
say "  $(basename "$ARCHIVE")"
say ""
say "${C_B}目标机三步（全程不需要联网，前提：已装 Docker、root 权限、磁盘富余>=20G）：${C_N}"
say "  第 1 步（解压）："
say "    sudo mkdir -p /opt/opstk-pkg && sudo $UNPACK_HINT"
say "  第 2 步（一键安装，自动生成密钥与目录）："
say "    cd /opt/opstk-pkg/${PKG_DIR_NAME} && sudo bash install.sh"
say "    （换盘加 --data-root <目录> --tftp-root <目录>；端口被占加 --port <n>）"
say "  第 3 步（打开网页跟着引导走）："
say "    http://<目标机IP>:8000   —— 首次会引导你：① 改管理员口令 ② 环境体检 ③ 添加 ISO 镜像"
say ""
say "  详细说明见包内 README-离线部署.md；环境问题先跑：sudo bash doctor.sh"
say ""
ok "打包完成。"
