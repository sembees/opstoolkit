# OpsToolkit 运维工具合集

一体化运维工具平台。后端 Python/FastAPI，前端 Vue 3 + Element Plus，数据库 SQLite。
覆盖 CT（网络设备/安全设备）和 IT（Linux 服务器）两大运维场景，全部通过浏览器操作。

---

## 功能一览

| 模块 | 功能 | 厂商/系统 | 说明 |
|------|------|-----------|------|
| CT 巡检 | 批量巡检 + 模板管理 | H3C / 华为 / 思科 | WebSocket 实时输出，支持默认模板和自定义命令 |
| CT ZTP | 零配置开局 + dnsmasq 投递 | H3C / 华为 / 思科 | 按 MAC 自动生成配置文件 |
| IT PXE | 完整 PXE 服务器装机 | Ubuntu 22.04 / RHEL 9 | 本机即 DHCP+TFTP+HTTP+iPXE，三种部署模式 |
| IT 网络配置 | nmcli / netplan 配置生成器 | Ubuntu / RHEL | 单口 / Bond / VLAN / Bridge，一键下载脚本 |
| 通用 | 资产 + 凭据管理 | — | Fernet 加密存储，JWT 认证 |
| 通用 | ZIP 打包下载 | — | 巡检报告、配置脚本一键导出 |

### 技术栈

- 后端：Python 3.9+ / FastAPI / SQLAlchemy / netmiko / paramiko
- 前端：Vue 3 / Element Plus / Vite
- 数据库：SQLite（零配置，文件级，随项目走）
- PXE 服务：dnsmasq + iPXE
- 部署：systemd 服务 或 Docker

---

## 代码在哪？服务在哪？

本项目有**两个部署位置**，用途不同：

### 本地（开发机）— Windows

| 名称 | 路径 |
|------|------|
| 项目根目录 | `D:\07-cc\01-project` |
| 后端代码 | `D:\07-cc\01-project\backend\app\` |
| 前端代码 | `D:\07-cc\01-project\frontend\src\` |
| 前端构建产物 | `D:\07-cc\01-project\frontend\dist\` |
| 数据库 | `D:\07-cc\01-project\backend\data\ops.db` |
| 加密密钥 | `D:\07-cc\01-project\backend\.env` |
| Python | `C:\Users\11943\miniconda3\python.exe`（3.12）|
| npm | `C:\Program Files\nodejs\npm.cmd` |
| 访问地址 | http://localhost:8000 |

本地是 Windows，可以运行 Web 界面、生成配置、下载文件、巡检网络设备。
但 **PXE 装机和 ZTP 开局需要 Linux 环境**（dnsmasq），本地无法直接使用这些功能。

### 远程服务器（生产环境）— Rocky Linux 9.7

| 名称 | 位置 |
|------|------|
| IP 地址 | `10.128.118.113`（网卡 ens18，/24 网段）|
| 登录账号 | `yang / yang`（sudo 免密，wheel 组）|
| 项目目录 | `/opt/opstk/` |
| 后端代码 | `/opt/opstk/backend/app/` |
| 前端文件 | `/opt/opstk/frontend/dist/` |
| 数据库 | `/opt/opstk/backend/data/ops.db` |
| 加密密钥 | `/opt/opstk/backend/.env` |
| Python venv | `/opt/opstk/venv/` |
| 服务管理 | `sudo systemctl restart opstk`（systemd，开机自启）|
| PXE TFTP 根 | `/srv/tftp/`（ipxe.efi / undionly.kpxe）|
| PXE HTTP/应答 | `/srv/opstk/pxe-web/`（user-data / ks.cfg / vmlinuz / initrd）|
| ISO 存储 | `/srv/opstk/iso/` |
| Docker 镜像 | `opstk-opstoolkit:latest`（约 360MB）|
| 访问地址 | http://10.128.118.113:8000 |

远程服务器是**完整的 PXE 服务器**，裸机接上网线即可自动装机。

---

## 目录结构

```text
01-project/
├── backend/                    # 后端 (Python FastAPI)
│   ├── app/
│   │   ├── main.py             # FastAPI 入口 + 静态文件挂载
│   │   ├── config.py           # 配置 (数据库路径/密钥/超时)
│   │   ├── database.py         # 异步 SQLite 引擎
│   │   ├── server.py           # PXE/ZTP 服务管理 (dnsmasq/systemd)
│   │   ├── api/                # API 路由 (6 个模块)
│   │   │   ├── auth.py         #   登录 / JWT
│   │   │   ├── assets.py       #   资产 / 凭据 CRUD
│   │   │   ├── inspection.py   #   CT 设备巡检
│   │   │   ├── netconfig.py    #   IT 网络配置生成
│   │   │   ├── pxe.py          #   IT PXE 装机
│   │   │   └── ztp.py          #   CT ZTP 开局
│   │   ├── core/               # 核心逻辑
│   │   │   ├── models.py       #   数据模型 (SQLAlchemy)
│   │   │   ├── schemas.py      #   API 请求/响应模型
│   │   │   ├── crypto.py       #   Fernet 对称加密
│   │   │   ├── auth.py         #   JWT + bcrypt
│   │   │   └── crud.py         #   通用 CRUD
│   │   ├── ct/                 # CT 模块 (网络设备)
│   │   │   ├── drivers/        #   SSH 驱动 (H3C/华为/思科)
│   │   │   ├── inspection/     #   巡检解析 + TextFSM 模板
│   │   │   └── ztp/            #   ZTP 配置生成器
│   │   └── it/                 # IT 模块 (服务器)
│   │       ├── pxe/            #   PXE 装机 (ISO提取/应答/dnsmasq)
│   │       └── netconfig/      #   网络配置生成 (nmcli/netplan)
│   ├── data/                   # SQLite 数据库 + 运行数据
│   └── requirements.txt        # Python 依赖
├── frontend/                   # 前端 (Vue 3 + Element Plus)
│   ├── src/
│   │   ├── views/              # 页面组件
│   │   │   ├── Login.vue       #   登录
│   │   │   ├── Dashboard.vue   #   仪表盘
│   │   │   ├── Assets.vue      #   资产管理
│   │   │   ├── Credentials.vue #   凭据管理
│   │   │   ├── Inspection.vue  #   CT 巡检
│   │   │   ├── NetConfig.vue   #   IT 网络配置
│   │   │   ├── PXE.vue         #   IT PXE 装机
│   │   │   ├── ZTP.vue         #   CT ZTP 开局
│   │   │   └── Help.vue        #   使用帮助
│   │   ├── router.js           # 路由
│   │   └── App.vue             # 根组件
│   ├── dist/                   # 构建产物 (后端直接部署)
│   └── package.json
├── docker/                     # 容器化文件
│   ├── Dockerfile              # 镜像构建
│   ├── entrypoint.sh           # 入口脚本
│   └── docker-compose.yml      # 编排
├── README.md                   # 本文件
└── docs/                       # 补充文档
```

---

## 本地运行（开发）

### 前提条件

- Python 3.9+（推荐 3.12）
- Node.js 18+

### 启动后端

```bash
# 进入项目目录
cd D:\07-cc\01-project

# 安装依赖（首次）
C:\Users\11943\miniconda3\python.exe -m pip install -r backend\requirements.txt

# 设置 PYTHONPATH 并启动
set PYTHONPATH=backend
C:\Users\11943\miniconda3\python.exe -m uvicorn app.main:app --reload --port 8000
```

### 构建前端（修改前端代码后）

```bash
cd D:\07-cc\01-project\frontend

# 安装依赖（首次）
"C:\Program Files\nodejs\npm.cmd" install

# 构建
"C:\Program Files\nodejs\npm.cmd" run build
```

构建后产物在 `frontend/dist/`，后端会自动从该目录提供静态文件。
打开 http://localhost:8000 即可访问。

默认账号：`admin / admin@123`

---

## 远程服务器部署（生产）

### 方式一：裸机部署（当前已部署）

远程服务器已通过 systemd 服务 `opstk` 部署，开机自启。

```bash
# SSH 连接
ssh yang@10.128.118.113
# 密码: yang

# 管理服务
sudo systemctl status opstk     # 查看状态
sudo systemctl restart opstk    # 重启
sudo systemctl stop opstk       # 停止
sudo journalctl -u opstk -f     # 查看实时日志

# PXE 相关服务
sudo systemctl status dnsmasq   # PXE DHCP+TFTP
```

**更新代码到远程：**

```bash
# 只改了后端：
scp -r backend/app/ yang@10.128.118.113:/opt/opstk/backend/
ssh yang@10.128.118.113 "sudo systemctl restart opstk"

# 改了前端（先在本地构建）：
scp -r frontend/dist/ yang@10.128.118.113:/opt/opstk/frontend/
# 前端是静态文件，刷新浏览器即可，无需重启服务
```

### 方式二：Docker 部署

远程服务器上已构建了 Docker 镜像 `opstk-opstoolkit:latest`。

```bash
ssh yang@10.128.118.113
cd /opt/opstk

# 启动容器（host 网络模式，PXE 需要）
docker compose up -d --build

# 查看状态
docker compose ps
docker compose logs -f

# 停止 / 重启
docker compose down
docker compose restart
```

Docker 镜像包含完整的 PXE 服务（dnsmasq + iPXE），可作为独立 PXE 服务器迁移到其他主机。

**导出镜像迁移到其他机器：**

```bash
# 导出
docker save opstk-opstoolkit:latest | gzip > /tmp/opstk.tar.gz

# 传到新机器
scp /tmp/opstk.tar.gz user@new-host:/tmp/

# 新机器导入
docker load < /tmp/opstk.tar.gz
```

---

## API 接口

| 模块 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 认证 | POST | `/api/auth/login` | 登录获取 JWT |
| 资产 | GET/POST | `/api/assets/` | 设备列表/添加 |
| 资产 | POST | `/api/assets/import` | 批量导入 |
| 凭据 | GET/POST | `/api/credentials/` | 凭据列表/添加 |
| 巡检 | POST | `/api/inspection/run` | 发起巡检 |
| 巡检 | WS | `/api/inspection/ws` | WebSocket 实时输出 |
| 巡检 | GET/POST | `/api/inspection/templates` | 巡检模板 |
| 网络配置 | POST | `/api/netconfig/generate` | 生成配置脚本 |
| 网络配置 | GET | `/api/netconfig/download/{id}` | 下载脚本 |
| PXE | POST | `/api/pxe/iso/upload` | 上传 ISO |
| PXE | POST | `/api/pxe/iso/extract` | 提取内核 |
| PXE | POST | `/api/pxe/templates` | 创建装机模板 |
| PXE | POST | `/api/pxe/deploy/{id}` | 部署 PXE |
| ZTP | POST | `/api/ztp/templates` | 创建 ZTP 模板 |
| ZTP | POST | `/api/ztp/deploy/{id}` | 部署 ZTP |

完整 API 文档：启动后访问 http://localhost:8000/docs（Swagger UI）

---

## 数据安全

| 文件 | 内容 | 备份建议 |
|------|------|----------|
| `backend/data/ops.db` | SQLite 数据库（资产/凭据/模板）| 直接拷贝备份 |
| `backend/.env` | 加密密钥 + JWT 密钥 | **必须一并备份**，否则旧凭据无法解密 |
| `/etc/dnsmasq.d/opstk-pxe.conf` | PXE/ZTP dnsmasq 配置 | 备份后可快速恢复 |
| `/srv/tftp/` | iPXE 固件 | 可从系统包重装 |
| `/srv/opstk/pxe-web/` | 应答文件 + 内核文件 | 重要，tar 打包备份 |

**重要提示：** 删除数据库后密钥会重新生成，已录入的加密凭据将无法解密。

### 数据备份脚本

```bash
# 在远程服务器上执行
ssh yang@10.128.118.113
sudo tar czf /tmp/opstk-backup-$(date +%Y%m%d).tar.gz \
  /opt/opstk/backend/data/ops.db \
  /opt/opstk/backend/.env \
  /etc/dnsmasq.d/opstk-pxe.conf \
  /srv/opstk/pxe-web/
```

---

## PXE 装机说明

本机（远程服务器）是**完整的 PXE 服务器**，包含 DHCP + TFTP + HTTP + iPXE 全部组件。

### 三种 DHCP 模式

| 模式 | DHCP 行为 | 适用场景 |
|------|-----------|----------|
| standalone | 完整分配 IP + PXE 引导 | 专用装机 VLAN，网段内无其他 DHCP |
| proxy | 不分配 IP，只广播引导信息 | 接入现有网络即装，不破坏现有 DHCP |
| relay | 不跑 DHCP，仅 TFTP | 大规模集中式，交换机 ip-helper 中继 |

### 快速装机流程

1. 上传 ISO 到 `/srv/opstk/iso/`（或通过 Web 界面上传）
2. Web 界面 → PXE 装机 → 提取内核
3. 创建装机模板（账号/密码/分区等）
4. 点「部署」→ 自动生成配置 + 启动 dnsmasq
5. 裸机接线 → BIOS 设为网络启动 → 开机自动安装

### PXE 文件说明

| 文件 | 路径 | 作用 |
|------|------|------|
| ipxe.efi | /srv/tftp/ | UEFI 引导固件 |
| undionly.kpxe | /srv/tftp/ | BIOS 引导固件 |
| vmlinuz | /srv/opstk/pxe-web/ | Linux 内核（从 ISO 提取）|
| initrd | /srv/opstk/pxe-web/ | 初始内存盘（从 ISO 提取）|
| user-data | /srv/opstk/pxe-web/ | Ubuntu autoinstall 应答 |
| ks.cfg | /srv/opstk/pxe-web/ | RHEL Kickstart 应答 |
| boot.ipxe | /srv/opstk/pxe-web/ | iPXE 菜单脚本 |
| dnsmasq.conf | /etc/dnsmasq.d/ | DHCP + TFTP 配置 |

---

## ZTP 开局说明

### 厂商差异

| 厂商 | DHCP Option | 机制 |
|------|-------------|------|
| H3C | 66 (TFTP) + 67 (文件名) | auto-config，空配置启动时自动拉取 |
| 华为 | 66 (TFTP) + 67 (中间文件) | ZTP，中间文件描述下载项 |
| 思科 | 150 (TFTP) + 67 (脚本) | IOS-XE ZTP，Python 脚本拉取配置 |

### 快速开局流程

1. 创建 ZTP 模板（VLAN/管理 IP/账号等）
2. 添加设备 MAC 地址
3. 点「部署」→ 按 MAC 自动生成配置文件
4. dnsmasq 配置 DHCP Option 指向 TFTP
5. 新设备上电 → 空配置启动 → 自动拉取配置

---

## 网络配置生成器说明

支持 Ubuntu（netplan）和 RHEL（nmcli）两种系统。

### 配置类型

| 类型 | 说明 |
|------|------|
| 单口配置 | 静态 IP 或 DHCP |
| Bond 聚合 | mode 0-6，含 active-backup、LACP（802.3ad）|
| VLAN | 基于父接口划分 VLAN |
| Bridge 网桥 | 多接口二层网桥（虚拟化/KVM）|

### Ubuntu renderer 说明

Ubuntu 系统有两个网络管理器：networkd 和 NetworkManager。

- 物理网卡（eth0/ens*）→ 推荐 networkd（稳定优先）
- 无线/WWAN/USB → 必须 NetworkManager（支持动态认证）

生成器中静态 IP 默认使用 networkd，无线场景可选 NetworkManager。

### 使用方式

1. Web 界面 → IT 网络配置
2. 选择系统类型（Ubuntu / RHEL）
3. 选择配置类型（单口/Bond/VLAN/Bridge）
4. 填写参数 → 生成配置 → 下载脚本
5. 在目标服务器执行脚本即可

---

## 常见问题

**Q: 打不开网页？**
A: SSH 进服务器运行 `sudo systemctl restart opstk`，确认 `systemctl status opstk` 显示 active。

**Q: 巡检报错连接超时？**
A: 确认设备 IP 可达、SSH 端口 22 未被阻止、凭据密码正确。

**Q: PXE 部署后裸机不引导？**
A: 检查 dnsmasq 是否运行、裸机和服务器是否同一网段、网段内是否有其他 DHCP 冲突。

**Q: 忘记密码？**
A: 默认 admin / admin@123。如果改过忘了，删除 ops.db 重启服务会重置（但已录入数据会丢失）。

**Q: 如何迁移到新服务器？**
A: 方式一：scp 整个 /opt/opstk/ 目录 + 安装依赖。方式二：导出 Docker 镜像传到新机器。

---

## 许可证

内部使用，未公开发布。


## ??

| ?? | ?? | ?? |
|------|------|------|
| ???? | `python -m unittest discover -s backend/tests` | 42 ? |
| NetConfig ?? | `PYTHONPATH=backend python backend/scripts/test_netconfig_full.py` | 24 ?? |

??????????????????? `TEST_BASE` ??????