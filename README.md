# OpsToolkit 运维工具合集

一体化运维工具平台，覆盖网络设备巡检与服务器网络配置生成。

## 功能概览

### CT 板块 (网络设备 / 安全设备)
- **设备巡检**：支持 H3C / 华为 / 思科，内置关键指标默认巡检模板
  - CPU、内存、接口状态、电源/风扇、温度、告警、日志
  - 支持手动输入自定义命令批量下发
  - WebSocket 实时回显执行输出
  - 命令输出自动结构化解析 (ntc-templates + TextFSM)
- **ZTP 配置开局**：(规划中)

### IT 板块 (Linux 服务器)
- **PXE 装机**：(规划中)
- **网络配置生成器**：
  - 统一 nmcli 脚本生成，RHEL 8+ / Ubuntu 22.04+ 通用
  - 支持：单口(static/dhcp)、Bond 聚合(7 种模式)、VLAN、网桥
  - Ubuntu 可选 netplan 格式
  - 表单填写 → 脚本预览 → 一键下载

## 技术栈
- 后端：Python + FastAPI + SQLAlchemy + netmiko + ntc-templates
- 前端：Vue 3 + Element Plus + Vite
- 数据库：SQLite (可迁移 PostgreSQL)
- 部署：Docker Compose

## 快速开始

### 方式一：Docker 部署 (推荐)
```bash
cp .env.example .env
docker compose up -d --build
# 访问 http://localhost:8000
```

### 方式二：本地开发
```bash
# 后端
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# 前端 (另开终端)
cd frontend
npm install
npm run dev
# 访问 http://localhost:5173
```

默认账号：admin / admin@123

## 项目结构
```
opstoolkit/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 入口
│   │   ├── config.py            # 配置
│   │   ├── database.py          # 数据库
│   │   ├── core/                # 模型/认证/加密/CRUD
│   │   ├── ct/                  # CT 板块
│   │   │   ├── drivers/         # H3C/华为/思科 驱动 (命令集)
│   │   │   └── inspection/      # 巡检服务 + 解析器 + TextFSM
│   │   ├── it/                  # IT 板块
│   │   │   └── netconfig/       # 网络配置生成器
│   │   └── api/                 # API 路由
│   ├── requirements.txt
│   └── Dockerfile
├── frontend/                    # Vue 3 + Element Plus
├── docker-compose.yml
└── .env.example
```

## Bond 聚合模式说明
| 模式 | 名称 | 说明 |
|------|------|------|
| 0 | balance-rr | 轮询 |
| 1 | active-backup | 主备 (推荐) |
| 2 | balance-xor | 源目MAC哈希 |
| 3 | broadcast | 广播 |
| 4 | 802.3ad | LACP (需交换机配置) |
| 5 | balance-tlb | 自适应发送负载 |
| 6 | balance-alb | 自适应负载 |

## 支持的巡检命令 (默认模板)
- **H3C**: display version, display cpu-usage, display memory, display device,
  display environment, display power, display fan, display interface brief,
  display alarm, display logbuffer reverse
- **华为**: display version, display cpu-usage, display memory-usage, display device,
  display temperature, display power, display fan, display interface brief,
  display alarm urgent, display logbuffer
- **思科**: show version, show processes cpu sorted, show memory statistics,
  show environment all, show environment power, show ip interface brief,
  show inventory, show alarms, show logging