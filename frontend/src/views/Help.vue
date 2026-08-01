<template>
  <div class="help-page">
    <el-card shadow="never">
      <template #header>
        <div style="display:flex;align-items:center;gap:8px">
          <el-icon size="20"><Reading /></el-icon>
          <span style="font-weight:600;font-size:16px">使用帮助</span>
        </div>
      </template>

      <el-tabs v-model="activeTab" tab-position="left" style="min-height:520px">
                <!-- ===== 小白手册 ===== -->
        <el-tab-pane label="小白手册" name="beginner">
          <h3>OpsToolkit 小白入门手册</h3>
          <p style="color:#666">本手册面向零基础用户，从“这个工具是什么”开始，一直讲到“能独立完成一次巡检和一次裸机装机”。</p>

          <el-collapse v-model="beginnerActive">

            <el-collapse-item title="这个工具是什么？" name="b0">
              <p>OpsToolkit 是一个网页版的运维工具箱。你打开浏览器就能用，不用安装客户端软件，不用掌握命令行。</p>
              <p>它解决的是运维人员每天重复做的事情：</p>
              <ul style="line-height:2;padding-left:20px">
                <li>检查 20 台交换机的状态（以前要逐台 SSH 登录，现在点一下全部完成）</li>
                <li>新买的服务器要装系统（以前要插 U 盘一台一台装，现在接线开机自动装）</li>
                <li>配置服务器网卡 Bond 聚合（以前要查文档手敲，现在填表自动生成脚本）</li>
                <li>新上架的交换机初始化（以前要控制台手配，现在上电自动拉取配置）</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="代码在哪？服务在哪？" name="b1">
              <p style="font-weight:600;color:#409eff">本地（你的电脑）— 开发和修改代码的地方</p>
              <el-table :data="localPaths" border size="small">
                <el-table-column prop="name" label="名称" width="120" />
                <el-table-column prop="path" label="路径" />
              </el-table>
              <p style="margin-top:8px;color:#999;font-size:13px">本地是 Windows，可运行 Web 界面、生成配置、下载文件，但 PXE 装机需要 Linux，本地不能直接装机。</p>

              <p style="font-weight:600;color:#67c23a;margin-top:12px">远程服务器（真正跑服务的地方）</p>
              <el-table :data="remotePaths" border size="small">
                <el-table-column prop="name" label="名称" width="130" />
                <el-table-column prop="path" label="位置" />
              </el-table>
              <p style="margin-top:8px;color:#999;font-size:13px">远程服务器 IP: <b>10.128.118.113</b>，用户: <b>yang</b>，密码: <b>yang</b>。用 SSH 连接后可管理服务。</p>
            </el-collapse-item>

            <el-collapse-item title="我要巡检设备，从头怎么操作？" name="b2">
              <el-steps direction="vertical" :active="5">
                <el-step title="第 1 步：登录" description="浏览器打开 http://10.128.118.113:8000，输入 admin / admin@123" />
                <el-step title="第 2 步：录入设备" description="点「资产管理」→ 添加设备，填写 IP 地址、选择厂商（H3C/华为/思科）、设备类型（交换机/路由器/防火墙）" />
                <el-step title="第 3 步：录入密码" description="点「凭据管理」→ 添加密码，填写 SSH 账号和密码（自动加密）。回到「资产管理」把这个凭据关联到设备上" />
                <el-step title="第 4 步：开始巡检" description="点「CT 巡检」→ 勾选设备 → 选择巡检模板（系统默认或自定义）→ 点「开始巡检」" />
                <el-step title="第 5 步：查看结果" description="巡检过程中可看到实时命令输出，完成后看解析后的表格化结果（CPU、内存、接口状态等）" />
              </el-steps>
              <el-alert type="info" :closable="false" style="margin-top:12px" title="提示" description="巡检模板可以自己定义！在「CT 巡检」页面点「模板管理」，可以查看系统默认模板的内容，也可以克隆后修改成自己的模板。" />
            </el-collapse-item>

            <el-collapse-item title="我要裸机装系统，从头怎么操作？" name="b3">
              <el-steps direction="vertical" :active="6">
                <el-step title="第 1 步：准备 ISO" description="下载 Ubuntu Server ISO（必须是 live-server 版本），上传到服务器 /srv/opstk/iso/ 目录。可用 scp: scp ubuntu.iso yang@10.128.118.113:/srv/opstk/iso/" />
                <el-step title="第 2 步：提取内核" description="打开 OpsToolkit Web 界面 → 「PXE 装机」 → ISO 面板中点「提取」。系统自动挂载 ISO 并提取 vmlinuz 和 initrd" />
                <el-step title="第 3 步：建装机模板" description="在「PXE 装机」点「新建模板」，填写系统类型、账号密码、磁盘分区等。这些就是装好后的系统配置" />
                <el-step title="第 4 步：一键部署" description="点模板旁的「部署」按钮。系统自动检测网卡、生成配置、启动 dnsmasq。下方会显示部署日志，全部打✓就成功了" />
                <el-step title="第 5 步：设置裸机" description="裸机接网线，开机进 BIOS/UEFI，把 Network Boot 设为 Enabled，启动顺序调到第一位。部分服务器可按 F12 直接选网络启动" />
                <el-step title="第 6 步：等待安装完成" description="裸机重启后自动开始安装，屏幕上会看到进度。安装完成后自动重启，用模板中的账号密码登录即可" />
              </el-steps>
            </el-collapse-item>

            <el-collapse-item title="代码改了之后怎么更新到服务器？" name="b4">
              <pre class="code-block"># ===== 场景 1: 只改了后端 Python 代码 =====
# 在本地 D:-cc-project 改好代码后:

# 1. 打包上传 (Codex 帮你做, 或手动):
scp -r backend/app/ yang@10.128.118.113:/opt/opstk/backend/

# 2. SSH 进去重启:
ssh yang@10.128.118.113
sudo systemctl restart opstk

# ===== 场景 2: 改了前端 =====
# 先本地构建:
cd frontend
npm run build

# 上传 dist:
scp -r dist/ yang@10.128.118.113:/opt/opstk/frontend/

# 刷新浏览器即可, 无需重启服务

# ===== 场景 3: Docker 重建 =====
ssh yang@10.128.118.113
cd /opt/opstk
docker compose up -d --build</pre>
            </el-collapse-item>

            <el-collapse-item title="常见问题" name="b5">
              <el-collapse>
                <el-collapse-item title="“我打不开网页”" name="faq1">
                  <p>检查服务是否在运行：SSH 进服务器，运行 <code>systemctl status opstk</code>。如果 inactive，运行 <code>sudo systemctl restart opstk</code>。</p>
                </el-collapse-item>
                <el-collapse-item title="“巡检报错连接超时”" name="faq2">
                  <p>确认设备 IP 可达、SSH 端口 (22) 未被阻止、凭据密码正确。可在资产管理中修改超时时间。</p>
                </el-collapse-item>
                <el-collapse-item title="“PXE 部署后裸机不引导”" name="faq3">
                  <p>检查：1) dnsmasq 是否运行 (systemctl status dnsmasq)；2) 裸机和服务器是否同一网段；3) 网段内是否有其他 DHCP 服务。</p>
                </el-collapse-item>
                <el-collapse-item title="“忘记密码”" name="faq4">
                  <p>默认 admin / admin@123。如果改过忘了，删除 /opt/opstk/backend/data/ops.db 后重启服务会重置为默认账号（但已录入的资产/凭据会丢失）。</p>
                </el-collapse-item>
              </el-collapse>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

<!-- ===== 快速开始 ===== -->
        <el-tab-pane label="工具使用手册" name="quick">
          <h3>OpsToolkit 工具使用手册</h3>
          <p>一体化运维工具平台。后端 Python/FastAPI，前端 Vue 3 + Element Plus，数据库 SQLite。支持 CT 设备巡检/ZTP 开局，IT 服务器 PXE 装机/网络配置生成。</p>

          <el-collapse v-model="quickActive" style="margin-top:12px">

            <el-collapse-item title="代码结构说明" name="q0">
              <pre class="code-block">01-project/
├── backend/                    # 后端 (Python FastAPI)
│   ├── app/
│   │   ├── main.py              # 入口: FastAPI 应用 + 静态文件挂载
│   │   ├── config.py           # 配置: 数据库路径/密钥/超时参数
│   │   ├── database.py         # 异步 SQLite 引擎
│   │   ├── api/                # API 路由
│   │   │   ├── auth.py         #   登录/JWT
│   │   │   ├── assets.py       #   资产/凭据 CRUD
│   │   │   ├── inspection.py   #   CT 设备巡检
│   │   │   ├── netconfig.py    #   IT 网络配置生成
│   │   │   ├── pxe.py          #   PXE 装机
│   │   │   └── ztp.py           #   ZTP 开局
│   │   ├── core/               # 核心逻辑
│   │   │   ├── models.py       #   数据模型 (SQLAlchemy)
│   │   │   ├── schemas.py      #   Pydantic 模型
│   │   │   ├── crypto.py       #   Fernet 加密
│   │   │   ├── auth.py         #   JWT + bcrypt
│   │   │   └── crud.py         #   通用 CRUD
│   │   ├── ct/                 # CT 模块 (网络设备)
│   │   │   ├── drivers/        #   SSH 驱动 H3C/华为/思科
│   │   │   ├── inspection/     #   巡检解析 + TextFSM
│   │   │   └── ztp/            #   ZTP 配置生成器
│   │   └── it/                 # IT 模块 (服务器)
│   │       ├── netconfig/      #   nmcli/netplan 配置生成
│   │       └── pxe/            #   PXE 配置生成 + 本机服务管控
│   ├── requirements.txt        # Python 依赖
│   ├── Dockerfile             # 容器构建
│   └── docker-entrypoint.sh    # 容器启动脚本
├── frontend/                  # 前端 (Vue 3 + Element Plus)
│   ├── src/views/            # 10 个页面组件
│   ├── src/api/              # axios HTTP 封装
│   ├── src/router/           # Vue Router
│   └── src/layouts/          # 侧边栏布局
├── docker-compose.yml         # 容器编排
└── README.md</pre>
            </el-collapse-item>

            <el-collapse-item title="本地开发环境运行（Windows）" name="q1">
              <p style="font-weight:600">后端启动</p>
              <pre class="code-block"># 1. 安装依赖
cd backend
pip install -r requirements.txt
# 额外补装: pip install pydantic-settings eval_type_backport

# 2. 设置环境变量
 = "backend"

# 3. 启动
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 访问: http://localhost:8000
# 默认账号: admin / admin@123</pre>
              <p style="font-weight:600;margin-top:12px">前端开发服务</p>
              <pre class="code-block">cd frontend
npm install
npm run dev    # 开发服务, 热更新, 访问 http://localhost:5173

# 生产构建 (写入 dist/ 目录, 后端自动挂载)
npm run build</pre>
              <el-alert type="warning" :closable="false" style="margin:8px 0">
                <template #title><b>Windows 限制</b></template>
                <p style="margin:4px 0">· PXE 本机部署不可用 (DHCP/TFTP 需 Linux)</p>
                <p style="margin:4px 0">· ZTP 本机部署不可用 (同上)</p>
                <p style="margin:4px 0">· 其他功能完全可用: 巡检、生成配置、下载 ZIP、资产管理</p>
              </el-alert>
            </el-collapse-item>

            <el-collapse-item title="远程服务器完整部署（从零开始）" name="q2">
              <p style="color:#999;font-size:13px">以 Rocky Linux 9 为例, 其他 RHEL 系或 Ubuntu 类似。详细命令见「部署指南」标签页。</p>
              <pre class="code-block"># ===== 第 1 步: 安装系统依赖 =====
dnf install -y dnsmasq ipxe util-linux python3 python3-pip

# ===== 第 2 步: 创建虚拟环境 =====
python3 -m venv /opt/opstk/venv

# ===== 第 3 步: 上传代码 =====
# 用 scp 或其他方式上传项目到 /opt/opstk/
# 最终目录结构:
#   /opt/opstk/backend/app/main.py
#   /opt/opstk/frontend/dist/index.html

# ===== 第 4 步: 安装 Python 依赖 =====
cd /opt/opstk/backend
/opt/opstk/venv/bin/pip install -r requirements.txt
/opt/opstk/venv/bin/pip install pydantic-settings eval_type_backport

# ===== 第 5 步: 配置 sudo 免密 =====
echo 'yang ALL=(root) NOPASSWD: /usr/bin/systemctl * dnsmasq, /usr/sbin/systemctl * dnsmasq, /usr/bin/tee /etc/dnsmasq.d/*, /usr/bin/chown, /usr/bin/mount, /usr/bin/umount, /usr/sbin/restorecon, /usr/sbin/semanage' | sudo tee /etc/sudoers.d/opstk
sudo chmod 440 /etc/sudoers.d/opstk

# ===== 第 6 步: 创建目录 + SELinux =====
mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/iso /srv/opstk/mnt
sudo chown -R yang\codexsandboxoffline /srv/tftp /srv/opstk
sudo semanage fcontext -a -t tftpdir_t '/srv/tftp(/.*)?'
sudo restorecon -R /srv/tftp

# ===== 第 7 步: 启动 =====
cd /opt/opstk/backend
PYTHONPATH=/opt/opstk/backend /opt/opstk/venv/bin/uvicorn \
  app.main:app --host 0.0.0.0 --port 8000

# ===== 验证 =====
curl http://localhost:8000/health
# 应返回: {"status":"ok"}
# 浏览器: http://服务器IP:8000</pre>
              <p style="font-weight:600;margin-top:12px">配置开机自启（可选）</p>
              <pre class="code-block">cat > /tmp/opstk.service << 'EOF'
[Unit]
Description=OpsToolkit
After=network.target
[Service]
Type=simple
User=yang\codexsandboxoffline
WorkingDirectory=/opt/opstk/backend
Environment=PYTHONPATH=/opt/opstk/backend
ExecStart=/opt/opstk/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
[Install]
WantedBy=multi-user.target
EOF
sudo cp /tmp/opstk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opstk</pre>
            </el-collapse-item>

            <el-collapse-item title="Docker 容器部署（一键迁移）" name="q3">
              <pre class="code-block"># 在项目根目录执行
docker compose up -d --build

# 镜像包含全套环境: Python + dnsmasq + iPXE + 前端
# 访问: http://宿主机IP:8000

# 迁移到其他主机: 复制项目目录, 重复上述命令
# 或导出镜像: docker save opstk-opstoolkit | gzip > opstk.tar.gz</pre>
              <el-alert type="warning" :closable="false" style="margin:8px 0" title="必须配置" description="docker-compose.yml 中 network_mode: host 和 privileged: true 不能改，否则 PXE DHCP 广播和 ISO 挂载无法工作。" />
            </el-collapse-item>

            <el-collapse-item title="日常使用指南" name="q4">
              <el-table :data="dailyOps" border size="small">
                <el-table-column prop="page" label="页面" width="120" />
                <el-table-column prop="func" label="功能" width="140" />
                <el-table-column prop="how" label="怎么用" />
              </el-table>
            </el-collapse-item>

            <el-collapse-item title="数据存储与备份" name="q5">
              <el-table :data="dataPaths" border size="small">
                <el-table-column prop="path" label="路径" width="250" />
                <el-table-column prop="content" label="内容" />
                <el-table-column prop="backup" label="备份方式" width="160" />
              </el-table>
              <p style="margin-top:12px;font-weight:600">备份命令</p>
              <pre class="code-block"># 完整备份 (包含数据库 + PXE 配置 + 内核文件)
tar czf opstk-backup-.tar.gz \
  /opt/opstk/backend/data/ \
  /srv/opstk/pxe-web/ \
  /srv/opstk/iso/ \
  /etc/dnsmasq.d/opstk-pxe.conf

# 恢复: 解压到原路径, 重启服务</pre>
              <el-alert type="warning" :closable="false" style="margin:8px 0" title="重要提示" description="backend/.env 中的 credential_key 是凭据加密密钥。如果删除了数据库但保留 .env，旧凭据仍可解密。但如果 .env 丢失，所有加密凭据将无法解密。" />
            </el-collapse-item>

            <el-collapse-item title="版本更新与重启" name="q6">
              <pre class="code-block"># 更新代码后只需重启 (Python 代码无需重编译)
sudo systemctl restart opstk

# 前端更新: 重新构建后上传 dist/
cd frontend && npm run build
# 上传 /opt/opstk/frontend/dist/ 后刷新浏览器即可

# Docker 更新
docker compose up -d --build

# 数据库迁移 (SQLite 是单文件, 直接拷贝即可)
cp /opt/opstk/backend/data/ops.db /backup/</pre>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== CT 巡检 ===== -->
        <el-tab-pane label="CT 巡检" name="inspect">
          <h3>CT 设备巡检</h3>
          <p>支持 H3C、华为、思科 三类设备的实时巡检。</p>
          <el-alert type="info" :closable="false" style="margin-bottom:16px">
            <strong>两种巡检方式：</strong>默认模板（包含 CPU/内存/电源/接口/告警 等关键指标）和自定义命令（手动输入原生 CLI）。
          </el-alert>
          <ol>
            <li>在「资产管理」添加网络设备（category = ct），填写 IP、厂商、device_type。</li>
            <li>在「凭据管理」录入设备的 SSH/Telnet 账号密码，并在资产里关联。</li>
            <li>进入「CT 巡检」，选择设备 + 巡检模板（或填写自定义命令）。</li>
            <li>点击开始巡检，WebSocket 实时输出命令结果，关键指标自动解析为结构化数据。</li>
            <li>巡检模板均可查看内容；系统默认模板只读，可「克隆」成自定义后编辑。</li>
          </ol>
          <el-alert type="warning" :closable="false" title="连接要求" description="设备需开启 SSH 或 Telnet，本工具服务器能达设备管理 IP。device_type 推荐：h3c → hp_comware，华为 → huawei，思科 → cisco_ios。" style="margin-top:12px" />
        </el-tab-pane>

        <!-- ===== 网络配置 ===== -->
        <el-tab-pane label="网络配置生成" name="netconfig">
          <h3>服务器网络配置生成器</h3>
          <p style="color:#666">统一表格编辑器 + 实时预览面板，支持任意数量的接口/多Bond/VLAN/Bridge自由组合。三种输出格式：netplan / nmcli / ifcfg。</p>

          <el-table :data="netconfigRows" border size="small" style="margin:12px 0">
            <el-table-column prop="item" label="配置项" width="140" />
            <el-table-column prop="desc" label="说明" />
          </el-table>

          <el-alert type="success" :closable="false" style="margin:12px 0">
            <template #title><b>三种格式适配</b></template>
            <p style="margin:4px 0"><b>netplan</b> — Ubuntu 22.04+，生成 99-opstk.yaml，默认 renderer=networkd</p>
            <p style="margin:4px 0"><b>nmcli</b> — RHEL 8+，生成 apply-network.sh，需 NetworkManager</p>
            <p style="margin:4px 0"><b>ifcfg</b> — RHEL/CentOS 7+，生成 ifcfg-files.txt，无需 NetworkManager，传统 network.service</p>
          </el-alert>

          <p style="font-weight:600;margin-top:12px">操作步骤</p>
          <ol>
            <li>选择系统（Ubuntu / RHEL）和格式（netplan / nmcli / ifcfg）</li>
            <li>点击“物理接口”“Bond”“VLAN”“Bridge”按钮自由添加行，每行独立配置类型/名称/IP/网关/DNS</li>
            <li>Bond 行在“从接口”列用逗号填写从接口（如 eth0,eth1），VLAN 填写父接口</li>
            <li>右侧实时预览，确认配置无误后点“下载”</li>
            <li>拷贝到目标服务器执行（netplan: cp + netplan apply，nmcli: bash apply-network.sh，ifcfg: 复制到 /etc/sysconfig/network-scripts/ 后 systemctl restart network）</li>
          </ol>

          <p style="font-weight:600;margin-top:12px">常见场景示例</p>
          <pre class="code-block"># 场景1: 管理口 + 业务Bond + Bond上的VLAN
物理接口 mgmt: 10.0.0.10/24, 网关 10.0.0.1
Bond bond0: mode=4(LACP), 从接口 eth0,eth1, IP 10.10.0.10/24
VLAN bond0.100: 父接口 bond0, IP 172.16.100.10/24
VLAN bond0.200: 父接口 bond0, IP 172.16.200.10/24, 网关 172.16.200.1

# 场景2: KVM 虚拟化主机
物理接口 mgmt: 10.0.0.10/24
Bond bond0: mode=4, 从接口 eth0,eth1 (不配IP)
Bridge br0: 从接口 bond0, IP 192.168.122.1/24</pre>
        </el-tab-pane>

        <!-- ===== PXE ===== -->
        <el-tab-pane label="PXE 装机" name="pxe">
          <h3>PXE 装机手册</h3>
          <p>全自动网络安装 Ubuntu / RHEL 系统。裸机接上网线，开机即可自动安装操作系统，全程无人值守。OpsToolkit 本机可直接作为完整 PXE 服务器。</p>

          <el-collapse v-model="pxeActive" style="margin-top:12px">

            <el-collapse-item title="工作原理：PXE 引导全链路" name="p1">
              <p>一台裸机从接电到装好系统，经过以下环节：</p>
              <pre class="code-block">裸机上电 → BIOS/UEFI 设为 PXE 启动
  ↓
(1) DHCP 请求  —  裸机广播请求 IP
  ↓                OpsToolkit 的 dnsmasq 响应:
                     · 分配一个临时 IP
                     · 告诉它去哪里取引导文件 (next-server + filename)
  ↓
(2) TFTP 下载   —  裸机去 TFTP 服务器下载 iPXE 固件
                     · UEFI 机器 → ipxe.efi
                     · BIOS 机器 → undionly.kpxe
  ↓
(3) iPXE 启动   —  iPXE 固件运行，去 HTTP 下载引导脚本 boot.ipxe
  ↓
(4) 加载内核   —  按 boot.ipxe 指引，从 HTTP 下载:
                     · vmlinuz (Linux 内核)
                     · initrd (初始内存盘)
                     · squashfs (完整文件系统)
  ↓
(5) 自动安装   —  Ubuntu: autoinstall (cloud-init)
                     RHEL:  Kickstart (应答文件 ks.cfg)
                     按模板配置: 磁盘分区、账号密码、网络、软件包
  ↓
安装完成，重启进入新系统✔</pre>
              <p style="margin-top:8px;color:#999;font-size:13px">其中 (1)(2) 由 dnsmasq 完成，(3)(4)(5) 由 OpsToolkit 的 HTTP 文件服务提供支持。整个过程不需要人工干预。</p>
            </el-collapse-item>

            <el-collapse-item title="三种部署模式怎么选" name="p2">
              <el-table :data="pxeModes" border size="small" style="margin:8px 0">
                <el-table-column prop="mode" label="模式" width="130" />
                <el-table-column prop="dhcp" label="DHCP 行为" width="220" />
                <el-table-column prop="scene" label="适用场景" />
              </el-table>
              <el-alert type="success" :closable="false" style="margin:8px 0">
                <template #title><b>选择建议</b></template>
                <p style="margin:4px 0">· <b>专用装机网段</b>（如装机 VLAN、维护机柜）→ 选 <b>standalone</b></p>
                <p style="margin:4px 0">· <b>办公网/生产网里临时装机</b>，不想改现有 DHCP → 选 <b>proxy</b></p>
                <p style="margin:4px 0">· <b>大规模集中式部署</b>，交换机做中继 → 选 <b>relay</b></p>
              </el-alert>
              <p style="font-weight:600;margin-top:8px">standalone（独立 DHCP）</p>
              <p>本工具自己就是 DHCP 服务器，裸机插上线就装。前提是这段网络里不能有其他 DHCP，否则会冲突抢答。</p>
              <p style="font-weight:600;margin-top:8px">proxy（ProxyDHCP 代理）</p>
              <p>不动 IP 分配（现有 DHCP 照常工作），只额外广播一条 PXE 引导信息。机器同时收到两份回应：从原 DHCP 拿到 IP，从本工具拿到引导地址。适合接入现有网络即装，不破坏现有 DHCP。</p>
              <p style="font-weight:600;margin-top:8px">relay（中继模式）</p>
              <p>本工具完全不跑 DHCP，只提供 TFTP + HTTP 文件服务，靠交换机的 ip helper-address 把 DHCP 请求中继过来。适合大规模、集中式 PXE。</p>
            </el-collapse-item>

            <el-collapse-item title="操作步骤：从 ISO 到裸机装好" name="p3">
              <p style="font-weight:600">第 1 步：获取并放置 ISO 镜像</p>
              <p>下载官方安装镜像（不是 minimal，必须是 live-server），上传到服务器：</p>
              <pre class="code-block"># Ubuntu 22.04 (推荐 USTC 镜像)
wget http://mirrors.ustc.edu.cn/ubuntu-releases/22.04/\
  ubuntu-22.04.5-live-server-amd64.iso -P /srv/opstk/iso/

# RHEL/Rocky 9
wget http://mirror.rockylinux.org/pub/rocky/9.5/isos/x86_64/\
  Rocky-9.5-x86_64-minimal.iso -P /srv/opstk/iso/

# 或用 scp 从本地上传:
scp ubuntu-22.04.iso yang@服务器IP:/srv/opstk/iso/</pre>

              <p style="font-weight:600;margin-top:12px">第 2 步：提取内核文件</p>
              <p>进入「PXE 装机」页面，找到「ISO 镜像管理」面板：</p>
              <ol style="margin:4px 0 4px 20px">
                <li>列表中会显示已放置的 ISO 文件名称和大小</li>
                <li>选择对应的 OS 类型（Ubuntu / RHEL）和版本号</li>
                <li>点「提取」按钮，系统自动挂载 ISO 并提取引导文件</li>
                <li>期待结果：vmlinuz (~12MB) + initrd (~108MB) + squashfs (~499MB)</li>
              </ol>
              <p style="margin-top:4px;color:#999;font-size:13px">提取后的文件会放到 /srv/opstk/pxe-web/ubuntu/22.04/ 目录，前端可在 PXE 服务器面板的 HTTP 文件列表中看到。</p>

              <p style="font-weight:600;margin-top:12px">第 3 步：创建装机模板</p>
              <p>点「新建模板」，填写以下信息（字段详解见下方「模板字段说明」）：</p>
              <pre class="code-block">名称:     ubuntu-web-prod    (自定义，方便区分)
系统:     Ubuntu 22.04
管理账号: ops
管理密码: Ops@2024
时区:     Asia/Shanghai
磁盘:     lvm (推荐) / direct
网络:     dhcp / static
镜像源:  http://mirrors.aliyun.com/ubuntu (加速安装)</pre>

              <p style="font-weight:600;margin-top:12px">第 4 步：一键部署</p>
              <p>点模板旁的「部署」按钮，系统会自动完成：</p>
              <pre class="code-block">✓ 检测本机网卡名和 IP        (如 ens18, 10.128.118.113)
✓ 计算 DHCP 分配范围            (网段后 1/4)
✓ 生成 dnsmasq 配置               (写入 /etc/dnsmasq.d/opstk-pxe.conf)
✓ 落地应答文件                 (user-data, meta-data, boot.ipxe)
✓ 复制 iPXE 固件                  (ipxe.efi, undionly.kpxe)
✓ 修复 SELinux 上下文              (RHEL/Rocky)
✓ 重启 dnsmasq                  (服务生效)</pre>
              <p style="margin-top:4px">部署日志实时显示在下方「部署日志」区域，全部显示✓即成功。</p>

              <p style="font-weight:600;margin-top:12px">第 5 步：裸机 BIOS/UEFI 设置</p>
              <p>将裸机接上与服务器同一网段的网线，开机进入 BIOS/UEFI 设置：</p>
              <pre class="code-block"># UEFI 机器 (现代服务器多为此模式)
Boot → Network Boot/PXE Boot → Enabled
Boot Order → 将 Network 排在第一位
保存重启 → 自动进入 PXE 引导

# BIOS 机器 (老款服务器)
Advanced → PXE Option ROMs → Enabled
Boot → 选择 PXE 网卡启动</pre>
              <p style="margin-top:8px;color:#999;font-size:13px">部分服务器可按 F12 临时选择网络启动，无需改 BIOS。</p>

              <p style="font-weight:600;margin-top:12px">第 6 步：验证装机</p>
              <p>裸机重启后应自动开始安装，可通过以下方式确认：</p>
              <ol style="margin:4px 0 4px 20px">
                <li>裸机屏幕出现 iPXE 引导画面 → 正在下载内核</li>
                <li>出现 Ubuntu/RHEL 安装画面 → 正在格式化磁盘</li>
                <li>安装完成后自动重启 → 进入登录界面</li>
                <li>用模板中配置的账号密码登录验证</li>
              </ol>
            </el-collapse-item>

            <el-collapse-item title="模板字段说明" name="p4">
              <el-table :data="pxeFields" border size="small">
                <el-table-column prop="field" label="字段" width="130" />
                <el-table-column prop="required" label="必填" width="60" />
                <el-table-column prop="desc" label="说明" />
                <el-table-column prop="example" label="示例" width="160" />
              </el-table>
            </el-collapse-item>

            <el-collapse-item title="生成的配置文件说明" name="p5">
              <el-table :data="pxeFiles" border size="small">
                <el-table-column prop="file" label="文件" width="160" />
                <el-table-column prop="role" label="作用" width="120" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== ZTP ===== -->
        <el-tab-pane label="ZTP 开局" name="ztp">
          <h3>ZTP 配置开局手册</h3>
          <p>网络/安全设备（H3C、华为、思科）首次上电时空配置启动，会自动通过 DHCP 获取 TFTP 地址并下载配置文件。OpsToolkit 可生成全套开局配置并通过 dnsmasq 投递。</p>

          <el-collapse v-model="ztpActive" style="margin-top:12px">

            <el-collapse-item title="ZTP 工作原理" name="z1">
              <pre class="code-block">设备首次上电 (空配置)
  ↓
(1) 发起 DHCP 请求  —  设备以自己的 MAC 地址发起请求
  ↓                   dnsmasq 响应:
                        · 分配临时 IP
                        · 通过 DHCP Option 告诉设备去哪里取配置
  ↓
(2) 下载配置     —  设备去 TFTP 下载开局文件
                        · H3C:   直接下载 .cfg 配置文件
                        · 华为:   下载中间文件，再按其指引下载 .cfg
                        · 思科:   下载 Python 脚本，脚本拉取 .cfg
  ↓
(3) 加载配置       —  设备自动加载并生效配置
                        · 管理 IP/VLAN/SNMP/SSH 等
  ↓
开局完成，设备可远程管理✔</pre>
            </el-collapse-item>

            <el-collapse-item title="三厂商差异" name="z2">
              <el-table :data="ztpOptions" border size="small" style="margin:8px 0">
                <el-table-column prop="vendor" label="厂商" width="90" />
                <el-table-column prop="opt" label="DHCP Option" width="180" />
                <el-table-column prop="mech" label="工作机制" />
              </el-table>
              <p style="font-weight:600;margin-top:12px">H3C（推荐）</p>
              <p>使用 auto-config 机制。DHCP 通过 Option 66 告诉 TFTP 服务器地址，Option 67 告诉配置文件名。设备直接下载 .cfg 文件并加载。最简单直接。</p>
              <p style="font-weight:600;margin-top:8px">华为（推荐）</p>
              <p>使用 ZTP 机制。DHCP 通过 Option 66 告诉 TFTP 地址，Option 67 告诉中间文件名。设备先下载中间文件（描述需要下载哪些文件），再按指引下载实际配置。</p>
              <p style="font-weight:600;margin-top:8px">思科</p>
              <p>使用 IOS-XE ZTP。DHCP 通过 Option 150 告诉 TFTP 地址，Option 67 告诉脚本名。设备下载 Python 脚本，脚本再去 HTTP/TFTP 拉取配置文件。机制最复杂但灵活。</p>
            </el-collapse-item>

            <el-collapse-item title="操作步骤：从创建到设备开局" name="z3">
              <p style="font-weight:600">第 1 步：创建 ZTP 模板</p>
              <p>进入「ZTP 开局」页面，点「新建模板」，填写：</p>
              <pre class="code-block">名称:     h3c-core-sw      (自定义)
厂商:     H3C / 华为 / 思科
管理 VLAN: 100               (设备管理网段)
管理 IP:  192.168.100.1/24   (设备管理地址)
网关:     192.168.100.254
管理账号: admin
管理密码: Admin@123
SNMP 社区: public
SSH 版本: 2</pre>

              <p style="font-weight:600;margin-top:12px">第 2 步：添加设备清单</p>
              <p>每台设备需要登记其 MAC 地址，系统会按 MAC 生成单独的配置文件：</p>
              <pre class="code-block"># 获取 MAC 方式:
# 1. 设备贴纸上的 MAC 标签
# 2. 打开设备控制台: display device manuinfo (H3C/华为)
# 3. 打开设备控制台: show inventory (Cisco)

# 在设备清单中填写:
MAC:      3c8c-4012-abcd  (H3C/华为 格式) 或 aabb.ccdd.eeff (思科)
主机名: core-sw-floor3
IP:       192.168.100.10  (可选，不填则用模板默认)</pre>

              <p style="font-weight:600;margin-top:12px">第 3 步：生成配置 + 部署</p>
              <p>点「生成配置」，选择投递模式（同样支持 standalone/proxy/relay）。然后：</p>
              <ol style="margin:4px 0 4px 20px">
                <li>点「下载 ZIP」获取全套文件</li>
                <li>将 .cfg 配置文件放入 TFTP 的 ztp/ 目录</li>
                <li>部署 dnsmasq 配置（含 Option 66/67/150）</li>
                <li>重启 dnsmasq</li>
              </ol>

              <p style="font-weight:600;margin-top:12px">第 4 步：设备上电</p>
              <ol style="margin:4px 0 4px 20px">
                <li>设备接入与服务器同网段的端口（或 Trunk 口）</li>
                <li>确保设备为出厂默认配置（空配置）</li>
                <li>上电后设备自动发起 DHCP 并下载配置</li>
                <li>完成后可用模板中的管理 IP 和账号登录</li>
              </ol>
            </el-collapse-item>

            <el-collapse-item title="三种投递模式" name="z4">
              <el-table :data="pxeModes" border size="small" style="margin:8px 0">
                <el-table-column prop="mode" label="模式" width="130" />
                <el-table-column prop="dhcp" label="DHCP 行为" width="220" />
                <el-table-column prop="scene" label="适用场景" />
              </el-table>
              <p style="margin-top:8px">ZTP 的三种模与 PXE 完全一致，区别在于投递的是设备配置文件而非 OS 内核。</p>
              <p style="font-weight:600;margin-top:8px">standalone</p>
              <p>OpsToolkit 自己作为 DHCP + TFTP 服务器。适合专用的设备初始化网络，如维护 VLAN。</p>
              <p style="font-weight:600;margin-top:8px">proxy</p>
              <p>不分配 IP，只额外广播 ZTP 引导信息。适合在现有网络里临时开局设备，不影响现有 DHCP。</p>
              <p style="font-weight:600;margin-top:8px">relay</p>
              <p>不跑 DHCP，交换机 ip helper-address 中继。适合大规模集中开局。</p>
            </el-collapse-item>

            <el-collapse-item title="配置文件结构说明" name="z5">
              <el-table :data="ztpFiles" border size="small">
                <el-table-column prop="file" label="文件" width="160" />
                <el-table-column prop="vendor" label="厂商" width="80" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== 概念 ===== -->
        <!-- ===== 部署指南 ===== -->
        <el-tab-pane label="部署指南" name="deploy">
          <h3>部署指南</h3>

          <el-alert type="info" :closable="false" title="两种部署方式" description="方式一: 直接部署在 Linux 服务器上（推荐，性能最好）。方式二: 用 Docker 容器运行（方便迁移）。两种方式的 PXE 功能完全相同。" style="margin:12px 0" />

          <el-collapse v-model="deployActive" style="margin-top:12px">

            <el-collapse-item title="前置条件：环境要求" name="d0">
              <el-table :data="deployReqs" border size="small">
                <el-table-column prop="item" label="项目" width="140" />
                <el-table-column prop="req" label="要求" />
                <el-table-column prop="note" label="说明" width="200" />
              </el-table>
              <p style="margin-top:8px;color:#999;font-size:13px">注: Windows/macOS 可运行 Web 界面和生成配置、下载 ZIP，但无法直接运行 PXE 服务（DHCP/TFTP 需 Linux 内核）。</p>
            </el-collapse-item>

            <el-collapse-item title="方式一：直接部署在 Linux 服务器（推荐）" name="d1">
              <p style="font-weight:600;color:#409eff;margin-bottom:8px">适用于 Rocky/RHEL/CentOS 9+ 或 Ubuntu 22.04+</p>
              <p style="font-weight:600">第 1 步：安装依赖包</p>
              <pre class="code-block"># RHEL / Rocky / CentOS 9
dnf install -y dnsmasq ipxe util-linux python3 python3-pip

# Ubuntu / Debian
apt update && apt install -y dnsmasq ipxe util-linux python3 python3-pip</pre>
              <p style="font-weight:600;margin-top:12px">第 2 步：创建 Python 虚拟环境</p>
              <pre class="code-block">python3 -m venv /opt/opstk/venv
/opt/opstk/venv/bin/pip install -r requirements.txt</pre>
              <p style="margin-top:4px;color:#999;font-size:13px">或手动安装: pip install fastapi uvicorn sqlalchemy pydantic pydantic-settings python-jose passlib cryptography netmiko textfsm jinja2 aiosqlite bcrypt paramiko eval_type_backport</p>
              <p style="font-weight:600;margin-top:12px">第 3 步：上传代码</p>
              <pre class="code-block">将项目的 backend/ 和 frontend/dist/ 上传到 /opt/opstk/

最终目录结构:
/opt/opstk/
  backend/
    app/          # FastAPI 后端
    data/         # SQLite 数据库 (自动创建)
    requirements.txt
  frontend/
    dist/         # 前端构建产物</pre>
              <p style="font-weight:600;margin-top:12px">第 4 步：配置 sudo 免密（必要）</p>
              <pre class="code-block">\# OpsToolkit 需要管控 dnsmasq 服务和挂载 ISO
echo 'yang ALL=(root) NOPASSWD: /usr/bin/systemctl * dnsmasq, /usr/sbin/systemctl * dnsmasq, /usr/bin/tee /etc/dnsmasq.d/*, /usr/bin/chown, /bin/chown, /usr/bin/mount, /bin/mount, /usr/bin/umount, /bin/umount, /usr/sbin/restorecon, /sbin/restorecon, /usr/sbin/semanage, /sbin/semanage, /usr/bin/true, /bin/true' > /etc/sudoers.d/opstk
chmod 440 /etc/sudoers.d/opstk
visudo -cf /etc/sudoers.d/opstk  # 验证语法</pre>
              <p style="font-weight:600;margin-top:12px">第 5 步：创建目录 + 修复 SELinux</p>
              <pre class="code-block">mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/iso /srv/opstk/mnt
chown -R yang\codexsandboxoffline /srv/tftp /srv/opstk

# RHEL/Rocky 需要修复 SELinux (Ubuntu 跳过此步)
semanage fcontext -a -t tftpdir_t '/srv/tftp(/.*)?'
semanage fcontext -a -t tftpdir_t '/srv/opstk/pxe-web(/.*)?'
restorecon -R /srv/tftp /srv/opstk/pxe-web</pre>
              <p style="font-weight:600;margin-top:12px">第 6 步：启动服务</p>
              <pre class="code-block">cd /opt/opstk/backend
PYTHONPATH=/opt/opstk/backend /opt/opstk/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000</pre>
              <p style="margin-top:4px;color:#999;font-size:13px">推荐配置 systemd 实现开机自启，见下方「开机自启配置」。</p>
              <p style="font-weight:600;margin-top:12px">开机自启配置（可选）</p>
              <pre class="code-block">cat > /tmp/opstk.service << 'EOF'
[Unit]
Description=OpsToolkit Ops Platform
After=network.target
[Service]
Type=simple
User=yang
WorkingDirectory=/opt/opstk/backend
Environment=PYTHONPATH=/opt/opstk/backend
ExecStart=/opt/opstk/venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF
sudo cp /tmp/opstk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now opstk</pre>
            </el-collapse-item>

            <el-collapse-item title="方式二：Docker 容器部署（方便迁移）" name="d2">
              <p style="font-weight:600;color:#409eff;margin-bottom:8px">一条命令构建镜像，包含 OpsToolkit + dnsmasq + iPXE + Python 全套环境</p>
              <p style="font-weight:600">构建并启动</p>
              <pre class="code-block">cd /opt/opstk
docker compose up -d --build</pre>
              <p style="font-weight:600;margin-top:12px">docker-compose.yml 关键配置说明</p>
              <el-table :data="composeConfig" border size="small">
                <el-table-column prop="key" label="配置项" width="200" />
                <el-table-column prop="why" label="为什么需要" />
              </el-table>
              <p style="font-weight:600;margin-top:12px">导出镜像文件（用于离线迁移）</p>
              <pre class="code-block">docker save opstk-opstoolkit:latest | gzip > opstk-image.tar.gz
# 拷到 U 盘或 scp 到新服务器</pre>
              <p style="font-weight:600;margin-top:12px">在新主机上导入并启动</p>
              <pre class="code-block">\# 导入镜像
docker load < opstk-image.tar.gz

\# 上传 docker-compose.yml 和项目代码，然后:
docker compose up -d

\# 或者直接带源码重建:
docker compose up -d --build</pre>
              <p style="font-weight:600;margin-top:12px">数据卷说明</p>
              <el-table :data="volumeConfig" border size="small">
                <el-table-column prop="vol" label="卷名称" width="130" />
                <el-table-column prop="path" label="容器内路径" width="200" />
                <el-table-column prop="desc" label="内容" />
              </el-table>
            </el-collapse-item>

            <el-collapse-item title="首次使用流程：从部署到裸机装机" name="d3">
              <ol>
                <li><b>登录</b> — 浏览器打开 http://服务器IP:8000，账号 admin / admin@123</li>
                <li><b>上传 ISO</b> — 将 OS 安装镜像 (.iso) 放到服务器 /srv/opstk/iso/ 目录</li>
                <li><b>提取内核</b> — PXE 页面 ISO 面板，选择 OS 类型和版本，点「提取」，自动挂载 ISO 并提取 vmlinuz/initrd/squashfs</li>
                <li><b>创建模板</b> — 填写系统类型、磁盘分区、管理账号、密码、网络等</li>
                <li><b>一键部署</b> — 点「部署」按钮，系统自动完成全部配置</li>
                <li><b>裸机装机</b> — 裸机接线，BIOS/UEFI 设 PXE 启动，全自动安装</li>
              </ol>
              <el-alert type="success" :closable="false" title="PXE 引导全链路" description="裸机 → DHCP 分配 IP → TFTP 下载 iPXE 固件 → HTTP 下载内核(vmlinuz+initrd) → 加载 squashfs → autoinstall/Kickstart 自动安装。全程无人值守。" style="margin-top:12px" />
            </el-collapse-item>

            <el-collapse-item title="排错指南：常见问题" name="d4">
              <el-collapse v-model="troubleActive">
                <el-collapse-item title="dnsmasq 启动失败：Permission denied / SELinux" name="t1">
                  <p>RHEL/Rocky 上 SELinux 会阻止 dnsmasq 访问 TFTP 目录。运行：</p>
                  <pre class="code-block">semanage fcontext -a -t tftpdir_t '/srv/tftp(/.*)?'
restorecon -R /srv/tftp</pre>
                  <p>部署时系统会自动执行此操作，手动部署时需手动执行。</p>
                </el-collapse-item>
                <el-collapse-item title="dnsmasq 启动失败：not configured to listen" name="t2">
                  <p>检查网卡名是否正确。部署后查看 /etc/dnsmasq.d/opstk-pxe.conf 中 interface= 是否匹配实际网卡：</p>
                  <pre class="code-block">ip route show default  # 看 dev 后面是什么
systemctl status dnsmasq -l  # 看报错详情</pre>
                </el-collapse-item>
                <el-collapse-item title="sudo: a password is required" name="t3">
                  <p>sudo 免密未配置成功。重新配置 /etc/sudoers.d/opstk，确保路径正确（systemctl 在 /usr/sbin/ 而非 /usr/bin/）。</p>
                  <pre class="code-block">sudo -n true  # 测试免密是否生效</pre>
                </el-collapse-item>
                <el-collapse-item title="裸机 PXE 引导卡住，无法下载内核" name="t4">
                  <p>检查内核文件是否存在：在 PXE 页面查看 HTTP 文件列表是否包含 ubuntu/22.04/ 目录。如果没有，说明未提取 ISO。</p>
                </el-collapse-item>
                <el-collapse-item title="容器部署: port 67 already in use" name="t5">
                  <p>宿主机上有其他 DHCP 服务。停掉宿主机的 DHCP，或者在 OpsToolkit 中改用 proxy 模式。</p>
                  <pre class="code-block">systemctl stop dnsmasq dhcpd 2>/dev/null  # 停掉宿主机 DHCP</pre>
                </el-collapse-item>
              </el-collapse>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        
<!-- ===== 网络配置生成器使用指南 ===== -->
        <el-tab-pane label="网络配置生成" name="netconfig">
          <h3>网络配置生成器使用指南</h3>
          <p style="color:#666">生成 Ubuntu netplan 或 RHEL nmcli 配置脚本，支持网卡、Bond、VLAN、Bridge。</p>

          <el-collapse v-model="netconfigActive" style="margin-top:12px">

            <el-collapse-item title="操作步骤" name="nc1">
              <el-steps direction="vertical" :active="4">
                <el-step title="第1步：选择系统" description="在页面顶部切换 OS：Ubuntu 生成 .yaml (netplan)，RHEL 生成 .sh 脚本 (nmcli)。对于静态 IP 服务器，推荐 renderer=networkd。" />
                <el-step title="第2步：添加接口" description="可添加多个品种：物理网卡、网卡聚合(Bond)、VLAN子接口、网桥(Bridge)。每个接口独立配置 IP。" />
                <el-step title="第3步：配置参数" description="每个接口可选 dhcp 或手动填写 IP/网关/DNS。Bond 支持 mode 0-6，可指定主/从接口。" />
                <el-step title="第4步：下载使用" description="点击“下载”按钮。得到的文件直接在目标机器上执行（RHEL: bash apply-network.sh，Ubuntu: sudo cp 99-opstk.yaml /etc/netplan/ → sudo netplan apply）。" />
              </el-steps>
            </el-collapse-item>

            <el-collapse-item title="配置类型说明" name="nc2">
              <el-table :data="netconfigRows" border size="small">
                <el-table-column prop="item" label="类型" width="150" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
            </el-collapse-item>

            <el-collapse-item title="下载后怎么用" name="nc3">
              <p style="font-weight:600;color:#409eff">Ubuntu 22.04+ (netplan)</p>
              <pre class="code-block"># 1. 复制配置文件
sudo cp 99-opstk.yaml /etc/netplan/
# 2. 应用配置
sudo netplan apply
# 3. 验证
ip addr show</pre>

              <p style="font-weight:600;color:#67c23a;margin-top:12px">RHEL 8+ (nmcli)</p>
              <pre class="code-block"># 1. 加可执行权限
chmod +x apply-network.sh
# 2. 执行配置脚本
sudo bash apply-network.sh
# 3. 验证
nmcli device status</pre>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>


        <el-tab-pane label="常见概念" name="concept">
          <h3>常见概念解释</h3>
          <el-collapse v-model="conceptActive">
            <el-collapse-item title="ProxyDHCP（代理 DHCP）" name="c1">
              <p>不分配 IP，只额外广播 PXE/ZTP 引导信息。机器同时收到两份回应：从原 DHCP 拿到 IP，从本工具拿到引导地址。适合「接入现有网络即装/开局」。</p>
            </el-collapse-item>
            <el-collapse-item title="relay 中继模式" name="c2">
              <p>工具完全不跑 DHCP，只提供 TFTP+HTTP 文件服务，靠交换机的 ip helper-address 把请求中继过来。适合大规模、集中式部署。</p>
            </el-collapse-item>
            <el-collapse-item title="netplan renderer：networkd vs NetworkManager" name="c3">
              <p>物理网卡（eth0/ens*）推荐 networkd（稳定）；无线网卡、WWAN、USB 共享网络必须 NetworkManager（支持动态认证和信号扫描）。静态 IP 服务器首选 networkd。</p>
            </el-collapse-item>
            <el-collapse-item title="bond 聚合模式" name="c4">
              <p>mode 1 (active-backup) 主备，最常用、无需交换机配置；mode 4 (802.3ad/LACP) 需交换机两端同步配置，提供真正负载均衡。</p>
            </el-collapse-item>
            <el-collapse-item title="凭据加密" name="c5">
              <p>所有设备密码以 Fernet 对称加密存储于数据库，密钥首次启动时自动生成于 backend/.env。注意：删除数据库后密钥会重生，旧凭据将无法解密。</p>
            </el-collapse-item>
          </el-collapse>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from "vue"

const activeTab = ref("beginner")
const conceptActive = ref("c1")

const deployActive = ref("d1")

const beginnerActive = ref("b1")
const localPaths = [
  { name: "项目根目录", path: "D:\\07-cc\\01-project" },
  { name: "后端代码", path: "D:\\07-cc\\01-project\\backend\\app\\" },
  { name: "前端代码", path: "D:\\07-cc\\01-project\\frontend\\src\\" },
  { name: "前端构建物", path: "D:\\07-cc\\01-project\\frontend\\dist\\" },
  { name: "数据库", path: "D:\\07-cc\\01-project\\backend\\data\\ops.db" },
  { name: "加密密钥", path: "D:\\07-cc\\01-project\\backend\\.env" },
  { name: "Python", path: "C:\\Users\\11943\\miniconda3\\python.exe" },
  { name: "访问地址", path: "http://localhost:8000" },
]
const remotePaths = [
  { name: "IP 地址", path: "10.128.118.113" },
  { name: "登录账号", path: "yang / yang (sudo 免密)" },
  { name: "项目目录", path: "/opt/opstk/" },
  { name: "后端代码", path: "/opt/opstk/backend/app/" },
  { name: "前端文件", path: "/opt/opstk/frontend/dist/" },
  { name: "数据库", path: "/opt/opstk/backend/data/ops.db" },
  { name: "Python venv", path: "/opt/opstk/venv/" },
  { name: "服务管理", path: "sudo systemctl restart opstk" },
  { name: "PXE TFTP", path: "/srv/tftp/" },
  { name: "PXE HTTP", path: "/srv/opstk/pxe-web/" },
  { name: "ISO 存储", path: "/srv/opstk/iso/" },
  { name: "Docker 镜像", path: "opstk-opstoolkit:latest" },
  { name: "访问地址", path: "http://10.128.118.113:8000" },
]

const quickActive = ref("q4")
const dailyOps = [
  { page: "登录", func: "JWT 认证", how: "admin / admin@123, 首次登录后建议修改密码" },
  { page: "仪表盘", func: "总览", how: "查看资产数/巡检记录/快捷入口" },
  { page: "资产管理", func: "设备清单", how: "添加设备 IP/厂商/类型, 可批大批量导入" },
  { page: "凭据管理", func: "加密密码", how: "录入 SSH/Telnet 密码, Fernet 加密存储, 与资产关联" },
  { page: "CT 巡检", func: "设备巡检", how: "选择设备 + 模板, WebSocket 实时看命令输出和解析结果" },
  { page: "CT 巡检", func: "模板管理", how: "系统默认模板可查看/克隆; 用户模板可增删改" },
  { page: "IT 网络配置", func: "生成脚本", how: "选 OS 类型, 配置网卡/Bond/VLAN/Bridge, 下载脚本" },
  { page: "PXE 装机", func: "一键装机", how: "上传 ISO → 提取内核 → 创建模板 → 点部署 → 裸机接线" },
  { page: "ZTP 开局", func: "设备初始化", how: "建模板 → 添设设备 MAC → 生成配置 → 部署 → 设备上电" },
  { page: "使用帮助", func: "在线手册", how: "查看每个功能的详细说明和操作步骤" },
]
const dataPaths = [
  { path: "/opt/opstk/backend/data/ops.db", content: "SQLite 数据库 (资产/凭据/模板)", backup: "直接拷贝备份" },
  { path: "/opt/opstk/backend/.env", content: "加密密钥 + JWT 密钥", backup: "必须一并备份" },
  { path: "/etc/dnsmasq.d/opstk-pxe.conf", content: "PXE/ZTP dnsmasq 配置", backup: "备份后可快速恢复" },
  { path: "/srv/tftp/", content: "iPXE 固件 (ipxe.efi/undionly.kpxe)", backup: "可从系统包重装" },
  { path: "/srv/opstk/pxe-web/", content: "应答文件 + 内核文件", backup: "重要, tar 打包" },
  { path: "/srv/opstk/iso/", content: "ISO 镜像文件", backup: "可重新下载" },
]

const netconfigActive = ref("nc1")
const pxeActive = ref("p3")
const ztpActive = ref("z3")
const pxeFields = [
  { field: "名称", required: "是", desc: "自定义模板名，方便区分", example: "ubuntu-web-prod" },
  { field: "系统类型", required: "是", desc: "ubuntu 生成 autoinstall，rhel 生成 Kickstart", example: "ubuntu / rhel" },
  { field: "系统版本", required: "是", desc: "需与 ISO 版本一致，决定内核文件路径", example: "22.04 / 9.3" },
  { field: "管理账号", required: "是", desc: "安装后的管理用户名，已加入 sudo", example: "ops" },
  { field: "管理密码", required: "是", desc: "加密存储，生成 shadow 哈希", example: "Ops@2024" },
  { field: "时区", required: "否", desc: "默认 Asia/Shanghai", example: "Asia/Shanghai" },
  { field: "磁盘分区", required: "否", desc: "lvm (推荐) 或 direct", example: "lvm" },
  { field: "网络模式", required: "否", desc: "dhcp 自动获取 / static 静态", example: "dhcp" },
  { field: "镜像源", required: "否", desc: "apt/yum 源，留空用默认", example: "mirrors.aliyun.com" },
  { field: "SSH 公钥", required: "否", desc: "可添加公钥实现免密登录", example: "ssh-rsa AAA..." },
  { field: "后置脚本", required: "否", desc: "安装后自动执行的 Shell 脚本", example: "systemctl enable docker" },
]
const pxeFiles = [
  { file: "user-data", role: "Ubuntu 应答", desc: "autoinstall: 账号/磁盘/网络/软件包/后置脚本" },
  { file: "ks.cfg", role: "RHEL 应答", desc: "Kickstart 配置，功能同 user-data" },
  { file: "meta-data", role: "cloud-init", desc: "主机名、实例 ID 等元数据" },
  { file: "boot.ipxe", role: "iPXE 菜单", desc: "告诉 iPXE 去哪里下载内核、传什么参数" },
  { file: "dnsmasq.conf", role: "网络服务", desc: "DHCP + TFTP 配置，三种模式各不相同" },
  { file: "vmlinuz", role: "Linux 内核", desc: "从 ISO 提取，通过 HTTP 下载到内存" },
  { file: "initrd", role: "初始内存盘", desc: "从 ISO 提取，含安装器和驱动" },
  { file: "squashfs", role: "文件系统", desc: "从 ISO 提取，压缩的完整根文件系统" },
]
const ztpFiles = [
  { file: "device.cfg", vendor: "H3C", desc: "按 MAC 生成的配置文件，含 VLAN/IP/账号" },
  { file: "device.cfg", vendor: "华为", desc: "同上，华为语法格式" },
  { file: "ztp_script.py", vendor: "思科", desc: "Python 脚本，负责拉取并应用配置" },
  { file: "中间文件", vendor: "华为", desc: "描述需下载的文件列表" },
  { file: "dnsmasq.conf", vendor: "通用", desc: "含 Option 66/67 (H3C/华为) 或 150 (思科)" },
]
const troubleActive = ref("t1")
const deployReqs = [
  { item: "操作系统", req: "Linux (Rocky/RHEL/Ubuntu)", note: "Windows 仅 Web 界面" },
  { item: "Python", req: "3.9+", note: "3.12 最佳" },
  { item: "dnsmasq", req: "已安装", note: "DHCP+TFTP 服务" },
  { item: "iPXE 固件", req: "ipxe 包或已部署", note: "UEFI/BIOS 引导" },
  { item: "sudo 权限", req: "免密 sudo", note: "管控 dnsmasq" },
  { item: "端口", req: "8000 (Web) + 67 (DHCP) + 69 (TFTP)", note: "确保未被占用" },
]
const composeConfig = [
  { key: "network_mode: host", why: "PXE 需要广播 DHCP 数据包，必须用宿主机网络，不肽用 bridge" },
  { key: "privileged: true", why: "挂载 ISO 需要访问 /dev/loop 设备" },
  { key: "restart: unless-stopped", why: "服务崩溃或重启后自动恢复" },
]
const volumeConfig = [
  { vol: "data", path: "/app/backend/data", desc: "SQLite 数据库 (资产/凭据/模板)" },
  { vol: "tftp-root", path: "/srv/tftp", desc: "iPXE 固件 (ipxe.efi/undionly.kpxe)" },
  { vol: "pxe-web", path: "/srv/opstk/pxe-web", desc: "应答文件 + 内核 (vmlinuz/initrd)" },
  { vol: "iso-store", path: "/srv/opstk/iso", desc: "ISO 镜像文件" },
]

const netconfigRows = [
  { item: "物理接口", desc: "单口静态/DHCP配置" },
  { item: "链路聚合 Bond", desc: "mode 0-6，含 active-backup、LACP 等，支持指定从接口和主接口" },
  { item: "VLAN", desc: "基于父接口划分 VLAN，配置 IP" },
  { item: "网桥 Bridge", desc: "多接口打成二层网桥，用于虚拟化/KVM" },
]

const pxeModes = [
  { mode: "standalone", dhcp: "完整分配 IP + PXE 引导", scene: "专用装机 VLAN，网段内无其他 DHCP" },
  { mode: "proxy", dhcp: "不分配 IP，只广播引导信息", scene: "接入现有网络即装，不破坏现有 DHCP" },
  { mode: "relay", dhcp: "不跑 DHCP，仅 TFTP", scene: "大规模集中式，交换机 ip-helper 中继" },
]

const ztpOptions = [
  { vendor: "H3C", opt: "option 66 (TFTP) + 67 (文件名)", mech: "auto-config，空配置启动时自动拉取" },
  { vendor: "华为", opt: "option 66 (TFTP) + 67 (中间文件)", mech: "ZTP，中间文件描述下载项" },
  { vendor: "思科", opt: "option 150 (TFTP) + 67 (脚本)", mech: "IOS-XE ZTP，Python 脚本拉取配置" },
]
</script>

<style scoped>
.help-page h3 { margin: 0 0 16px; }
.help-page p { line-height: 1.8; color: #555; }
.help-page ol { line-height: 2; padding-left: 20px; }
.help-page li { margin-bottom: 4px; }

.help-page pre.code-block {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre;
  font-family: 'Cascadia Code', 'Fira Code', Consolas, monospace;
}
</style>
