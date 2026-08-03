# OpsToolkit - 运维工具合集

> 最新更新 2026-08-03: PXE 与 ZTP 统一为共享 dnsmasq 实例，两个界面状态实时同步
> 测试状态: 42 单元测试 + 50 集成测试全部通过

## 项目概要
OpsToolkit 是一个 Web 版的运维工具箱，覆盖 CT(网络设备/安全设备)和 IT(Linux 服务器)两大运维场景。打开浏览器就能做巡检、装机、配网、开局。

### 适用场景
| 场景 | 工具 | 效果 |
|------|------|------|
| 巡检 20 台交换机 | CT 巡检 | 一键全检，实时输出 |
| 新服务器装系统 | IT PXE | 裸机接线上电自动装 |
| 配服务器网卡 Bond | IT 网络配置 | 填表生成脚本 |
| 新交换机上电 | CT ZTP | 自动拉取配置 |

## 功能一览
| 模块 | 功能 | 厂商/系统 |
|------|------|-----------|
| CT 巡检 | 批量巡检 | H3C/华为/思科 |
| CT ZTP | 零配置开局 | H3C/华为/思科 |
| IT PXE | 完整 PXE 装机 | Ubuntu 22.04/RHEL 9 |
| IT 网络配置 | netplan/nmcli 生成 | Ubuntu/RHEL |
| 通用 | 资产+凭据管理 | Fernet加密+JWT |

### 技术栈
- 后端: Python 3.9+ / FastAPI / SQLAlchemy / netmiko / paramiko
- 前端: Vue 3 / Element Plus / Vite
- 数据库: SQLite (零配置)
- DHCP: dnsmasq (PXE + ZTP 共享同一实例)
- PXE: iPXE (UEFI + BIOS)
- 部署: Docker Compose (推荐) / systemd

## 代码在哪？服务在哪？
本项目有两个部署位置:

### 本地 Windows 开发机
| 名称 | 路径 |
|------|------|
| 项目根目录 | D:\\07-cc\\01-project |
| 后端代码 | backend\\app\\ |
| 前端代码 | frontend\\src\\ |
| 数据库 | backend\\data\\ops.db |
| 访问地址 | http://localhost:8000 |
本地可运行 Web 界面、生成配置、下载文件、巡检设备。PXE/ZTP 需要 Linux，本地无法直接使用。

### 远程服务器 (生产环境) - Rocky Linux 9.7
| 名称 | 位置 |
|------|------|
| IP | 10.128.118.113 |
| 账号 | yang / <your-password> (wheel 组) |
| 项目目录 | /opt/opstk/ |
| 容器名 | opstoolkit |
| 镜像 | opstk-opstoolkit:latest (~360MB) |
| PXE TFTP | /srv/tftp/ |
| PXE HTTP | /srv/opstk/pxe-web/ |
| ISO 存储 | /srv/opstk/iso/ |
| 访问地址 | http://10.128.118.113:8000 |
| 默认账号 | admin / <见 .env 或管理员分发> |
远程服务器是完整的 PXE 服务器，裸机接上网线即可自动装机。

## 快速开始
### 本地运行 (开发)
```bash
cd D:\\07-cc\\01-project
set PYTHONPATH=backend
pip install -r backend\\requirements.txt
uvicorn app.main:app --reload --port 8000
```
### 远程服务器管理
```bash
ssh yang@10.128.118.113  # 输入密码
cd /opt/opstk
docker compose ps
docker compose restart
docker compose logs -f --tail=100
```
更新代码后重启: docker compose restart

## 各模块详解

### CT 巡检 - 网络设备批量检查
支持 H3C/华为/思科交换机、路由器、防火墙。默认巡检项: 系统信息/CPU/内存/接口/环境/温度。支持自定义命令模板。WebSocket 实时输出结果。
使用: 资产管理 -> 添加设备 -> 凭据管理 -> 关联凭据 -> CT巡检 -> 开始巡检

### IT PXE - 裸机自动装机
本机 = DHCP + TFTP + HTTP + iPXE 四合一的完整 PXE 服务器。
三种部署模式: standalone(独立DHCP)/proxy(与现有DHCP并存)/relay(仅TFTP)
装机流程: 上传ISO -> 提取内核 -> 创建模板 -> 部署 -> 裸机接线上电自动装

### CT ZTP - 零配置开局
新交换机上电自动拉取配置。H3C用auto-config，华为用ZTP Python脚本，思科用IOS-XE ZTP。
ZTP 与 PXE 共享同一 dnsmasq 实例，状态实时同步。

### IT 网络配置 - 服务器网络脚本生成
支持 Ubuntu 22.04+(netplan) 和 RHEL 8+(nmcli)。
配置类型: 单口/Bond(0-6)/VLAN/Bridge。Bond 支持 active-backup/LACP/balance-rr 等。
Ubuntu 静态IP默认用 networkd(稳定)，无线场景可用 NetworkManager。

## 部署方式对比
| 方式 | 适用 | PXE/ZTP | 优点 |
|------|------|----------|------|
| 本地 uvicorn | 开发 | 否 | 开发便捷 |
| 远程 Docker | 生产 | 是 | 迁移方便，隔离安全 |
| 远程 裸金属 | 生产 | 是 | 性能最好 |

## Docker 容器构建
```bash
cd /opt/opstk
docker compose build
docker compose up -d
# 导出镜像迁移
docker save opstk-opstoolkit:latest | gzip > /tmp/opstk.tar.gz
scp /tmp/opstk.tar.gz user@new-host:/tmp/
# 新主机导入
docker load < /tmp/opstk.tar.gz
```

## 常见问题
Q: 打不开网页? A: SSH 进服务器运行 docker compose restart
Q: 巡检连接超时? A: 确认设备IP可达、SSH端口22未阻止
Q: PXE 装机不引导? A: 检查dnsmasq是否运行、裸机是否同网段、有无其他DHCP冲突
Q: 忘记密码? A: 默认见管理员分发，删除 ops.db 重启会重置

## 测试
| 类型 | 命令 | 数量 |
|------|------|------|
| 单元测试 | pytest backend/tests/ | 42 |
| 网络配置集成 | python backend/scripts/test_netconfig_full.py | 50 |

内部使用，未公开发布。