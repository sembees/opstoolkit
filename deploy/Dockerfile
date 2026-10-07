# =============================================================================
# OpsToolkit 自包含应用镜像（应用态）—— 一条命令构建出一个镜像：
#
#   docker build -f deploy/Dockerfile \
#     -t opstk:<版本> --build-arg OPSTK_VERSION=<版本> .
#
# （必须在**仓库根目录**执行：构建上下文需要同时看到 backend/ 与 frontend/。
#   ★ 本构建需要联网：node 阶段 npm ci、运行阶段 apt-get / pip3 install。
#     离线目标机不需要联网 —— 打包请在"有网 + 有 Docker"的机器上跑
#     packaging/make-offline-bundle.sh，目标机只 docker load。）
#
# 与 backend/Dockerfile（开发态构建）的三点差别：
#   1. 入口脚本用 deploy/container-entrypoint.sh（uvicorn 端口可由 OPSTK_PORT 覆盖，
#      默认仍 8000；backend/docker-entrypoint.sh 被正在跑的容器 bind-mount，不能动）；
#   2. 用 deploy/Dockerfile.dockerignore 精确裁剪构建上下文：
#      backend/.env（真实密钥！）与 backend/data（真实数据）绝不进镜像，
#      前端 node_modules 不进上下文（在 node 阶段重新 npm ci）；
#   3. 打入版本 LABEL（org.opencontainers.image.version），离线包可追溯。
# =============================================================================
# ===== 阶段一：前端构建 =====
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ===== 阶段二：运行时: Ubuntu 22.04 + PXE 全套组件 =====
FROM ubuntu:22.04
ARG OPSTK_VERSION=dev
ENV DEBIAN_FRONTEND=noninteractive

LABEL org.opencontainers.image.title="OpsToolkit" \
      org.opencontainers.image.version="${OPSTK_VERSION}" \
      org.opencontainers.image.description="OpsToolkit 应用态自包含镜像（前后端 + dnsmasq + python3）"

# 安装: dnsmasq (DHCP+TFTP) + ipxe (固件) + mount 工具 + Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsmasq ipxe ipxe-qemu util-linux iproute2 \
    python3 python3-pip python3-venv python-is-python3 \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# Python 依赖
COPY backend/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt pydantic-settings eval_type_backport

# 测试依赖：只为了让运维/落地验证能在容器内直接跑 backend/tests
#   docker exec <容器> python3 -m pytest tests -q
RUN pip3 install --no-cache-dir pytest

# 应用代码（.env / data / node_modules 已被 deploy/Dockerfile.dockerignore 排除）
COPY backend/ ./
# 前端构建产物拷进最终镜像，由后端直接服务（无需 nginx）
COPY --from=frontend /build/dist /app/frontend/dist

# 应用态入口脚本（与 backend/docker-entrypoint.sh 同逻辑，端口读 OPSTK_PORT）
COPY deploy/container-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# 目录（运行时真正的数据来自 compose 挂载，这里只是保证容器内路径存在）
RUN mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/ztp-web /srv/opstk/iso /srv/opstk/mnt /app/backend/data

ENV PYTHONPATH=/app/backend \
    OPSTK_PORT=8000

EXPOSE 8000 67/udp 69/udp
ENTRYPOINT ["/docker-entrypoint.sh"]
