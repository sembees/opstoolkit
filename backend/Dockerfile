# ===== 前端构建 =====
FROM node:20-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ===== 运行时: Ubuntu 22.04 + PXE 全套组件 =====
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive

# 安装: dnsmasq (DHCP+TFTP) + ipxe (固件) + mount 工具 + Python
RUN apt-get update && apt-get install -y --no-install-recommends \
    dnsmasq ipxe ipxe-qemu util-linux \
    python3 python3-pip python3-venv python-is-python3 \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend

# Python 依赖
COPY backend/requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt pydantic-settings eval_type_backport

# 应用代码
COPY backend/ ./
COPY --from=frontend /build/dist /app/frontend/dist

# 入口脚本
COPY backend/docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# 目录
RUN mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/iso /srv/opstk/mnt /app/backend/data

ENV PYTHONPATH=/app/backend

EXPOSE 8000 67/udp 69/udp

ENTRYPOINT ["/docker-entrypoint.sh"]
