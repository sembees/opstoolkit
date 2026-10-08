<template>
  <div class="page help-page">
    <PageHeader title="使用帮助" desc="按主题分类的在线手册，覆盖零基础入门、巡检、网络配置、PXE 装机、ZTP 开局与部署排错。" />
    <CardSection icon="Reading">
      <el-tabs v-model="activeTab" tab-position="left" class="help-tabs">
                <!-- ===== 小白手册 ===== -->
        <el-tab-pane label="小白手册" name="beginner">
          <h3>OpsToolkit 小白入门手册</h3>
          <p>本手册面向零基础用户，从“这个工具是什么”开始，一直讲到“能独立完成一次巡检和一次裸机装机”。</p>

          <el-collapse v-model="beginnerActive">

            <el-collapse-item title="这个工具是什么？" name="b0">
              <p>OpsToolkit 是一个网页版的运维工具箱。你打开浏览器就能用，不用安装客户端软件，不用掌握命令行。</p>
              <p>它解决的是运维人员每天重复做的事情：</p>
              <ul>
                <li>检查 20 台交换机的状态（以前要逐台 SSH 登录，现在勾选设备点一下，批量并发巡检）</li>
                <li>新买的服务器要装系统（以前要插 U 盘一台一台装，现在接线开机自动装）</li>
                <li>配置服务器网卡 Bond 聚合（以前要查文档手敲，现在填表自动生成脚本）</li>
                <li>新上架的交换机初始化（以前要控制台手配，现在上电自动拉取配置）</li>
              </ul>
              <p>左侧菜单共 9 个功能页：仪表盘、CT 巡检、告警管理、网络配置生成、PXE 装机、ZTP 开局、资产管理、凭据管理、使用帮助。</p>
            </el-collapse-item>

            <el-collapse-item title="代码在哪？服务在哪？" name="b1">
              <p class="lead-primary">本地（你的电脑）— 开发和修改代码的地方</p>
              <el-table :data="localPaths" border size="small">
                <el-table-column prop="name" label="名称" width="120" />
                <el-table-column prop="path" label="路径" />
              </el-table>
              <p class="muted-sm mt-2">本地是 Windows，可运行 Web 界面、巡检设备、生成配置、下载文件，但 PXE/ZTP 的本机部署需要 Linux，本地不能直接装机。</p>

              <p class="lead-success mt-3">远程服务器（真正跑服务的地方）</p>
              <el-table :data="remotePaths" border size="small">
                <el-table-column prop="name" label="名称" width="130" />
                <el-table-column prop="path" label="位置" />
              </el-table>
              <p class="muted-sm mt-2">远程服务器是 Rocky Linux 9 + Docker（容器名 opstoolkit），用 SSH 连接后 <code>docker compose restart</code> 即可管理服务。</p>
            </el-collapse-item>

            <el-collapse-item title="我要巡检设备，从头怎么操作？" name="b2">
              <el-steps direction="vertical" :active="5">
                <el-step title="第 1 步：登录" description="浏览器打开 http://10.128.118.113:8000，用管理员账号 admin 登录（初始密码见服务日志）" />
                <el-step title="第 2 步：录入设备" description="点「资产管理」→ 新增资产，类型选 CT，填写 IP 地址、选择厂商（H3C/华为/思科）、角色（交换机/路由器/防火墙）" />
                <el-step title="第 3 步：录入凭据" description="点「凭据管理」→ 新增凭据，填写 SSH 用户名和密码（自动加密存储，也可存 SSH 私钥和 enable 密码）。回到「资产管理」把这个凭据关联到设备上" />
                <el-step title="第 4 步：开始巡检" description="点「CT 巡检」→ 多选设备 → 选「默认巡检」并挑一个模板（或选「自定义命令」每行填一条命令）→ 点「开始巡检」" />
                <el-step title="第 5 步：看结果" description="输出按设备分组实时滚动，顶部显示“已开始 x/N 台 · 完成 y · 失败 z”；每台展开后是指标卡片 + 命令原始输出表格。跑完可点「下载结果」导出报告 txt / 数据 json / 表格 csv" />
              </el-steps>
              <el-alert type="info" :closable="false" class="mt-3" title="提示" description="巡检模板可以自己定义！在「CT 巡检」页面点「模板管理」，系统内置模板只能查看，克隆一份即可编辑；模板里每条命令被设备拒绝时会自动尝试候选命令。" />
            </el-collapse-item>

            <el-collapse-item title="我要裸机装系统，从头怎么操作？" name="b3">
              <el-steps direction="vertical" :active="7">
                <el-step title="第 1 步：放置 ISO" description="把 Ubuntu live-server 或 RHEL/Rocky 的 ISO 放到服务器 /srv/opstk/iso/ 目录（应用内没有上传接口，用 scp/共享目录放进去即可）" />
                <el-step title="第 2 步：提取内核" description="打开「PXE 装机」→ ISO 镜像管理面板，为该 ISO 选择系统类型（下拉可搜索，完整清单来自系统目录，含 openEuler/麒麟/UOS 等）和版本号（常见候选 + 从文件名自动识别），点「提取」。系统自动挂载 ISO 提取 vmlinuz/initrd（Ubuntu 还有 installer.squashfs）" />
                <el-step title="第 3 步：建装机模板" description="点「新建装机模板」，填系统类型/版本、管理员账号密码、磁盘方案与目标磁盘（自动选最大盘 / 按盘名 / 按序列号或型号）。这些就是装好后的系统配置" />
                <el-step title="第 4 步：一键部署" description="点模板旁的「部署」，确认弹窗里选部署模式（默认 ProxyDHCP，与现有 DHCP 并存更安全）。系统生成配置、写盘、重启 dnsmasq，下方显示部署日志，并列出实际写入的文件" />
                <el-step title="第 5 步：设置裸机" description="裸机接上与服务器同网段的网线，开机进 BIOS/UEFI 把 Network Boot 设为第一位。部分服务器可按 F12 临时选网络启动" />
                <el-step title="第 6 步：等待安装完成" description="裸机重启后自动安装，装完自动重启，用模板中的账号密码登录即可。" />
                <el-step title="第 7 步：标记完成（关键，别跳过）" description="装完的机器必须变成「已装完」，否则它下次走网卡引导会被重新装一遍（盘会被再抹一次）。正常情况下机器装完会自己回调服务端完成标记；也可以在「装机记录」里点「标记完成」。标记完成后 dnsmasq 不再给这台机器下发装机菜单，重启走网卡会落到“拒绝自动安装”并继续引导本地磁盘。要重装这台机器：点「重新启用装机」（会抹盘，有二次确认）。" />
              </el-steps>
            </el-collapse-item>

            <el-collapse-item title="代码改了之后怎么更新到服务器？" name="b4">
              <pre class="code-block"># ===== 场景 1: 改了后端 Python 代码（容器部署）=====
# 在本地 D:\07-cc\01-project 改好代码后，上传到 /opt/opstk/，然后:
ssh yang@10.128.118.113
cd /opt/opstk
docker compose restart

# ===== 场景 2: 改了前端 =====
# 先本地构建:
cd frontend
npm run build

# 上传 dist 到 /opt/opstk/frontend/ 后刷新浏览器即可（后端直接挂载 dist，无需重启）

# ===== 场景 3: Docker 重建（改了依赖/镜像内文件时）=====
ssh yang@10.128.118.113
cd /opt/opstk
docker compose up -d --build</pre>
              <p class="muted-sm mt-2">容器里挂载了 backend/app 与 frontend/dist 两个目录，所以“改代码 → 上传 → restart”就生效；只有改依赖（requirements.txt）或 Dockerfile 才需要 --build 重建。</p>
            </el-collapse-item>

            <el-collapse-item title="常见问题" name="b5">
              <el-collapse>
                <el-collapse-item title="“我打不开网页”" name="faq1">
                  <p>容器部署先看容器状态：<code>docker compose ps</code> / <code>docker compose logs -f</code>；服务健康可访问 <code>http://服务器IP:8000/health</code>，返回 JSON 即后端正常。</p>
                </el-collapse-item>
                <el-collapse-item title="“巡检提示设备不可达”" name="faq2">
                  <p>这是连接前的 TCP 预检在保护你：服务器 3 秒内建不起到设备 22 端口的 TCP 连接就会立即报这句话，不会卡住界面。依次检查：设备在线、地址端口正确、本机到设备有路由、中间防火墙放行 22。注意 ping 通不等于 22 端口通。</p>
                </el-collapse-item>
                <el-collapse-item title="“巡检提示 SSH 握手失败（不是密码错误）”" name="faq3">
                  <p>先访问 <code>/health</code> 看 <code>legacy_ssh</code> 字段：true 表示服务支持老设备算法；false 说明 SSH 库丢了 ssh-rsa/SHA-1 支持（把 paramiko 钉回 3.5.1）。设备侧可在设备上生成较新的主机密钥（华三 <code>public-key local create ecdsa secp256r1</code>、华为 <code>ecc local-key-pair create</code>、思科 <code>crypto key generate rsa modulus 2048</code>）。详见「CT 巡检」主题。</p>
                </el-collapse-item>
                <el-collapse-item title="“PXE 部署后裸机不引导”" name="faq4">
                  <p>检查：1) dnsmasq 是否运行（PXE 页面顶部状态灯或 <code>systemctl status dnsmasq</code>）；2) 裸机与服务器是否同一网段（DHCP 广播不过路由）；3) 网段内是否有其他 DHCP（standalone 模式会冲突，换 ProxyDHCP）；4) ISO 是否已提取（PXE 页面 HTTP 文件列表应有对应目录）。</p>
                </el-collapse-item>
                <el-collapse-item title="“装完的机器重启后又被装了一遍（盘又被抹了）？”" name="faq4b">
                  <p>这是 2026-10-08 之前版本的已知缺陷：装机记录一直停在「待装机」，dnsmasq 就一直给这台机器下发自动装机菜单，它每次走网卡引导都会被重装一遍。现在装完的机器会<strong>自动回调</strong>服务端（应答文件里的 <code>%post</code> / <code>late-commands</code> 调用 <code>/api/it/pxe/installs/机器记录ID/done?t=一次性令牌</code>），也可以在「装机记录」里人工点「标记完成」；变成「已装完」之后就不会再被重装（<code>dhcp-host</code> 地址预留仍保留，IP 不会漂）。若确实要重装这台机器：点「重新启用装机」，它会退回「待装机」并自动重新部署一次，机器下次网卡引导即重新安装（会抹盘，有二次确认）。</p>
                </el-collapse-item>
                <el-collapse-item title="“忘记 admin 密码”" name="faq5">
                  <p>管理员密码在首次初始化时随机生成并打印在服务日志里（容器部署是 <code>docker logs opstoolkit</code>，只在创建时打印一次）。若彻底丢失：停服后删除数据库文件 ops.db 再启动，会重建 admin 并打印新的初始密码——但已录入的资产/凭据/模板会一起清空，操作前先备份数据库和 .env。</p>
                </el-collapse-item>
              </el-collapse>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

<!-- ===== 快速开始 ===== -->
        <el-tab-pane label="工具使用手册" name="quick">
          <h3>OpsToolkit 工具使用手册</h3>
          <p>一体化运维工具平台。后端 Python/FastAPI，前端 Vue 3 + Element Plus，数据库 SQLite。支持 CT 设备巡检与告警、ZTP 开局、IT 服务器 PXE 装机、网络配置生成。</p>

          <el-collapse v-model="quickActive" class="mt-3">

            <el-collapse-item title="代码结构说明" name="q0">
              <pre class="code-block">01-project/
├── backend/                    # 后端 (Python FastAPI)
│   ├── app/
│   │   ├── main.py              # 入口: FastAPI + /health + 静态文件挂载
│   │   ├── config.py           # 配置: 密钥/超时/并发/PXE-ZTP token 等
│   │   ├── database.py         # 异步 SQLite 引擎 + 初始管理员/模板
│   │   ├── api/                # API 路由
│   │   │   ├── auth.py         #   登录/JWT
│   │   │   ├── assets.py       #   资产/凭据 CRUD
│   │   │   ├── inspection.py   #   CT 巡检（WS 实时/任务落库/导出）
│   │   │   ├── compare.py      #   巡检结果对比
│   │   │   ├── alerts.py       #   告警规则 + 告警记录
│   │   │   ├── dashboard.py    #   仪表盘汇总
│   │   │   ├── netconfig.py    #   IT 网络配置生成
│   │   │   ├── pxe.py          #   PXE 装机
│   │   │   └── ztp.py          #   ZTP 开局（含落位/认领）
│   │   ├── core/               # 核心逻辑
│   │   │   ├── models.py       #   数据模型 (SQLAlchemy)
│   │   │   ├── schemas.py      #   Pydantic 模型与校验
│   │   │   ├── crypto.py       #   Fernet 加密 + .env 密钥管理
│   │   │   ├── auth.py         #   JWT + bcrypt
│   │   │   ├── dhcp.py         #   dnsmasq 管控 + 配置红线检查
│   │   │   └── serve_token.py  #   /pxe/serve 与 /ztp 的 token 门禁
│   │   ├── ct/                 # CT 模块 (网络设备)
│   │   │   ├── drivers/        #   H3C/华为/思科 命令集（含候选命令）
│   │   │   ├── inspection/     #   连接/并发/解析 + TextFSM
│   │   │   └── ztp/            #   ZTP 配置生成 + 本机部署
│   │   └── it/                 # IT 模块 (服务器)
│   │       ├── netconfig/      #   netplan/nmcli/ifcfg 生成 + DHCP 池冲突提示
│   │       └── pxe/            #   PXE 生成 + 本机部署 + ISO 提取
│   ├── requirements.txt        # Python 依赖（paramiko 钉 3.5.1）
│   ├── Dockerfile             # 容器构建（Ubuntu 22.04 + dnsmasq + iPXE）
│   └── docker-entrypoint.sh    # 容器启动脚本
├── frontend/                  # 前端 (Vue 3 + Element Plus)
│   ├── src/views/            # 页面组件
│   ├── src/api/              # axios HTTP 封装
│   ├── src/router/           # Vue Router + 帮助主题表
│   └── src/layouts/          # 侧边栏布局
├── docker-compose.yml         # 容器编排（host 网络 + 挂载清单）
└── README.md</pre>
            </el-collapse-item>

            <el-collapse-item title="本地开发环境运行（Windows）" name="q1">
              <p class="section-title">后端启动</p>
              <pre class="code-block"># 1. 安装依赖
cd backend
pip install -r requirements.txt

# 2. 设置环境变量
$env:PYTHONPATH = "backend"

# 3. 启动
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

# 访问: http://localhost:8000
# 管理员: admin（初始密码见服务日志）</pre>
              <p class="section-title mt-3">前端开发服务</p>
              <pre class="code-block">cd frontend
npm install
npm run dev    # 开发服务, 热更新, 访问 http://localhost:5173

# 生产构建 (写入 dist/ 目录, 后端自动挂载)
npm run build</pre>
              <el-alert type="warning" :closable="false" class="mv-2">
                <template #title><b>Windows 限制</b></template>
                <p class="tight-p">· PXE / ZTP 的「本机部署」不可用（检测到非 Linux 环境时按钮禁用并提示，可改用「下载 ZIP」手动部署）</p>
                <p class="tight-p">· 其他功能完全可用: 巡检、结果对比、生成配置、下载 ZIP、资产/凭据/告警管理</p>
              </el-alert>
            </el-collapse-item>

            <el-collapse-item title="三种运行方式一览" name="q2">
              <el-table :data="runModes" border size="small">
                <el-table-column prop="mode" label="方式" width="180" />
                <el-table-column prop="for" label="适用" width="160" />
                <el-table-column prop="pxe" label="PXE/ZTP 本机部署" width="170" />
                <el-table-column prop="note" label="说明" />
              </el-table>
              <p class="muted-sm mt-2">具体安装命令见「部署指南」主题；本机部署 PXE/ZTP 的前提条件（dnsmasq、sudo 免密、目录）也在那里。</p>
            </el-collapse-item>

            <el-collapse-item title="Docker 容器部署（一键迁移）" name="q3">
              <pre class="code-block"># 在项目根目录执行
docker compose up -d --build

# 镜像包含全套环境: Python + dnsmasq + iPXE + 前端
# 访问: http://宿主机IP:8000

# 迁移到其他主机: 复制项目目录, 重复上述命令
# 或导出镜像: docker save opstk-opstoolkit | gzip > opstk.tar.gz</pre>
              <el-alert type="warning" :closable="false" class="mv-2" title="必须配置" description="docker-compose.yml 中 network_mode: host 和 privileged: true 不能改，否则 PXE DHCP 广播和 ISO 挂载无法工作。" />
              <p class="section-title mt-3">关键挂载（详见 docker-compose.yml 注释）</p>
              <el-table :data="composeMounts" border size="small">
                <el-table-column prop="vol" label="宿主机路径" min-width="170" />
                <el-table-column prop="why" label="为什么必须挂" />
              </el-table>
            </el-collapse-item>

            <el-collapse-item title="日常使用指南" name="q4">
              <el-table :data="dailyOps" border size="small">
                <el-table-column prop="page" label="页面" min-width="110" />
                <el-table-column prop="func" label="功能" min-width="130" />
                <el-table-column prop="how" label="怎么用" min-width="430" />
              </el-table>
            </el-collapse-item>

            <el-collapse-item title="数据存储与备份" name="q5">
              <el-table :data="dataPaths" border size="small">
                <el-table-column prop="path" label="路径" width="240" />
                <el-table-column prop="content" label="内容" />
                <el-table-column prop="backup" label="备份方式" width="150" />
              </el-table>
              <p class="section-title mt-3">备份命令</p>
              <pre class="code-block"># 完整备份 (包含数据库 + 应答文件 + 内核 + dnsmasq 配置)
tar czf opstk-backup.tar.gz \
  /opt/opstk/backend/data/ \
  /opt/opstk/backend/.env \
  /srv/opstk/pxe-web/ \
  /srv/opstk/ztp-web/ \
  /srv/opstk/iso/ \
  /etc/dnsmasq.d/

# 恢复: 解压到原路径, 重启服务</pre>
              <el-alert type="warning" :closable="false" class="mv-2" title="重要提示" description="backend/.env 里的 credential_key 是凭据加密密钥，secret_key 是 JWT 密钥，pxe_serve_token / ztp_serve_token 是装机/开局文件服务的门禁。密钥与数据库相互独立：只删数据库不会重生密钥（旧凭据仍可解密）；但 .env 丢失则所有已加密凭据永远打不开，必须一并备份。" />
            </el-collapse-item>

            <el-collapse-item title="版本更新与重启" name="q6">
              <pre class="code-block"># 容器部署（推荐）: 改代码后重启即可（app 与 dist 都是挂载）
cd /opt/opstk && docker compose restart

# 改了 requirements.txt / Dockerfile / 入口脚本: 重建
docker compose up -d --build

# 前端更新: 本地 npm run build 后上传 dist/，刷新浏览器即可

# 数据库迁移 (SQLite 是单文件, 停服后直接拷贝)
cp /opt/opstk/backend/data/ops.db /backup/</pre>
              <p class="muted-sm mt-2">服务重启后会把遗留的 running 巡检任务标记为 failed，不会永远卡在“执行中”。</p>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== CT 巡检 ===== -->
        <el-tab-pane label="CT 巡检" name="inspect">
          <h3>CT 设备巡检</h3>
          <p>支持 H3C、华为、思科三类网络/安全设备的批量巡检：多台设备并发执行、输出按设备分组实时滚动、命令结果自动解析为指标，任务落库后可下载报告、回放过程、做历史对比，还能联动告警规则。</p>

          <el-collapse v-model="inspectActive" class="mt-3">

            <el-collapse-item title="一次巡检的完整流程" name="i1">
              <el-steps direction="vertical" :active="5">
                <el-step title="选设备" description="「CT 巡检」页面的设备下拉只列出资产类型为 CT 的设备，支持搜索与多选；未关联凭据的设备会在结果里报“该资产关联的凭据已不存在”" />
                <el-step title="选模式" description="「默认巡检」按模板执行（不选模板时自动匹配厂商默认模板）；「自定义命令」每行填一条 CLI，原样下发" />
                <el-step title="开始巡检" description="点「开始巡检」后建立 WebSocket 连接，后端先创建巡检任务（落库），再逐台并发执行；单台失败不影响其他设备" />
                <el-step title="看输出" description="命令输出按设备分组滚动展示；关键指标（CPU/内存/温度/电源/风扇/接口…）解析成摘要卡片与命令明细表格" />
                <el-step title="用结果" description="任务结束后可下载 txt/json/csv、在仪表盘回放任务过程、在结果区做“巡检结果对比”；启用了告警规则时超标指标会写入告警记录" />
              </el-steps>
              <el-alert type="info" :closable="false" class="mt-3" title="设备侧要求" description="设备需开启 SSH 且管理 IP 可达。netmiko 设备类型自动推断：h3c → hp_comware，huawei → huawei，思科按角色（firewall/asa → cisco_asa，xr → cisco_xr，nexus → cisco_nxos，其余 → cisco_ios）；也可在资产里手动指定 device_type。" />
            </el-collapse-item>

            <el-collapse-item title="实时输出与进度显示" name="i2">
              <p>巡检过程中，下方「巡检结果」区域是事件流：</p>
              <ul>
                <li><b>按设备分组</b>：每台设备一块，块头是设备名；连接建立、模板信息等系统级行归入「系统」块。</li>
                <li><b>事件实时到达</b>：start（开始某台）/ cmd（下发命令，含“（候选命令 2/2）”标注）/ output（命令回显）/ cmd_fallback（“[命令不支持，切换候选] A → B”）/ error / done。</li>
                <li><b>进度行</b>：跑批时显示「已开始 x/N 台 · 完成 y · 失败 z」+ 进度条；失败台数会让进度条变红。</li>
                <li><b>任务事件</b>：连接建立后服务端回推 task_id，右上角随即出现「下载结果」下拉（报告 txt / 数据 json / 表格 csv）；清屏后仍可导出上一次的结果。</li>
              </ul>
              <p class="muted-sm mt-2">巡检在服务端后台执行：中途关掉浏览器页面不影响任务继续跑，结果照常落库、照常可下载。</p>
            </el-collapse-item>

            <el-collapse-item title="任务落库、下载与回放" name="i3">
              <p>每一次巡检都是一个任务（InspectionTask），跑完后逐台结果（InspectionResult）也落库：</p>
              <ul>
                <li><b>仪表盘</b>的「近期巡检任务」列出最近 5 条任务，显示 状态（已完成/执行中/失败）、设备数（结果数/总数）、创建时间，并可点「回放」看当时的输出事件流。</li>
                <li><b>下载结果</b>三种格式：
                  <ul>
                    <li>报告 (txt)：每台设备一段，逐条命令的原始输出原样贴出，附解析摘要 —— 给人看。</li>
                    <li>数据 (json)：任务元信息 + 每台设备的指标与原始结果 —— 给程序用。</li>
                    <li>表格 (csv)：一台设备 × 一条命令一行（摘要级，带 BOM，Excel 打开不乱码）。</li>
                  </ul>
                </li>
                <li><b>结果对比</b>：结果区点「巡检结果对比」，选一台设备可对比最近两次巡检的指标变化（上次/本次/涨跌趋势）；对比需要至少 2 条巡检记录。</li>
                <li>失败的任务也会保留原始报错，方便定位；服务重启后遗留的 running 任务会被标为 failed。</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="批量并发与超时（重要）" name="i4">
              <el-table :data="inspectTimeouts" border size="small" class="mv-2">
                <el-table-column prop="item" label="参数" width="210" />
                <el-table-column prop="value" label="默认值" width="110" />
                <el-table-column prop="note" label="说明" />
              </el-table>
              <ul>
                <li><b>批量并发</b>：多选设备后并发巡检，同时连接的设备数受并发上限约束（默认 10 台），避免压垮网络；这些超时/并发是全局配置，不能按资产单独修改。</li>
                <li><b>连接前 TCP 预检</b>：连 SSH 之前先测一次 TCP 端口（默认 3 秒），不通立刻报「设备不可达：IP:端口 在 N 秒内建不起 TCP 连接…」。以前没有预检时，不可达地址要等操作系统 SYN 重试（Linux 默认约 127 秒），界面就像卡死。</li>
                <li><b>两种超时分开</b>：「连接超时」管 SSH 建连阶段，「命令读取超时」管发命令后等多久回显，互不占用。</li>
                <li><b>失败互不影响</b>：某台设备预检失败、认证失败、命令全部被拒，都只影响它自己的结果，其他设备照常完成。</li>
              </ul>
              <p class="muted-sm mt-2">超时/并发通过环境变量（如 INSPECTION_TIMEOUT）调整，见 backend/app/config.py。</p>
            </el-collapse-item>

            <el-collapse-item title="候选命令回退与“该机型不支持此指标”" name="i5">
              <p>同一指标在不同机型上命令名可能不同（如部分 Comware 机型只有 <code>display alarm</code>、部分 VRP 版本只认 <code>display cpu</code>）。模板里的指标可以带「候选命令」：</p>
              <ul>
                <li>主命令被设备拒绝时，自动依次尝试候选命令；输出里会出现「&gt; display alarm（候选命令 2/2）」和「[命令不支持，切换候选] display alarm urgent → display alarm」两行提示。</li>
                <li>“被拒绝”的判据很严格：非空回显 ≤ 6 行、命中 Unrecognized command / Too many parameters / % Invalid input 等特征、且命中行不是 syslog 日志行 —— 三条同时满足才回退，长输出里出现这些字样不会误判。</li>
                <li>所有候选都被拒绝时，该指标标注为 <b>「该机型不支持此指标（已尝试：命令 A、命令 B）」</b>（状态 unsupported，灰色显示）——这不是“未解析”，而是设备确实没有这个特性。</li>
              </ul>
              <p class="muted-sm mt-2">系统内置模板已按真机实测配置候选命令（华三 alarm、华为 cpu/memory/alarm）；模板编辑弹窗里每条指标包含 指标标识 / 显示名 / 下发命令 / TextFSM 四列（候选命令不在界面上直接编辑）：存量模板里没配候选命令的指标，巡检时会按「指标标识 + 命令」与内置驱动精确匹配自动继承候选命令 —— 所以自定义模板里若改掉了命令本身，就不会有候选回退。</p>
            </el-collapse-item>

            <el-collapse-item title="老设备 SSH 支持（ssh-rsa / SHA-1）" name="i6">
              <p>大量在用的老机型只提供 <code>ssh-rsa</code> 主机密钥与 SHA-1 密钥交换。本服务已把 paramiko 钉在 3.5.1（仍带这些老算法，且含 Terrapin 修复），可以直接巡检这类设备。</p>
              <p>配套的两道保险：</p>
              <ul>
                <li><b>自检</b>：访问 <code>GET /health</code>，看 <code>legacy_ssh</code> 字段 —— true =「SSH 库支持老式算法（ssh-rsa + SHA-1 kex），可巡检老设备」；false 时 <code>legacy_ssh_detail</code> 会说明缺什么（并提示把 paramiko 钉回 3.5.1）。服务启动日志在能力缺失时也会告警。</li>
                <li><b>中文可执行提示</b>：连接握手失败时，报错会翻译成排查步骤（原始英文错误保留在末尾）：
                  <pre class="code-block">SSH 握手失败：设备与巡检服务的算法对不上 —— 不是账号密码问题（换密码没用）。
  ① 看服务是否仍带老算法：GET /health 的 legacy_ssh 字段（或启动日志）
  ② 在设备侧生成较新的主机密钥（推荐，不动服务）：
      华三 Comware： public-key local create ecdsa secp256r1   ← 已真机验证
      华为 VRP    ： ecc local-key-pair create（或 rsa local-key-pair create）
      思科 IOS    ： crypto key generate rsa modulus 2048
  ③ 若必须支持 ssh-dss / SSH v1 这类已淘汰算法，需单独评估（不建议）</pre>
                </li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="巡检模板管理" name="i7">
              <p>「模板管理」抽屉（或侧栏「CT 巡检 · 模板」）里可按厂商筛选，每个模板可执行：</p>
              <ul>
                <li><b>查看</b>：指标名、下发命令、标识、TextFSM 模板一张表看全。</li>
                <li><b>导出 / 导入</b>：JSON 文件，可在实例之间迁移模板。</li>
                <li><b>克隆</b>：把系统内置模板复制成自定义模板（克隆后可编辑）。</li>
                <li><b>编辑 / 删除</b>：仅自定义模板可用；系统内置模板的编辑/删除按钮被禁用，后端也会拒绝（403）。</li>
              </ul>
              <p>编辑弹窗里每条指标包含：指标标识（key，如 cpu）、显示名、下发命令、TextFSM 解析模板（可选）。不填 TextFSM 时按“Comware 专用解析 → ntc-templates → 自定义 TextFSM → 指标正则 → 原文摘要”的链路自动解析。</p>
              <p class="muted-sm mt-2">厂商选「通用」的自定义模板不绑定厂商，可任意设备使用。</p>
            </el-collapse-item>

            <el-collapse-item title="自定义命令巡检" name="i8">
              <p>巡检模式切到「自定义命令」，每行一条 CLI（如 <code>display interface brief</code>），原样下发到所选设备：</p>
              <ul>
                <li>每条命令独立执行、独立解析；没有对应解析模板时给出原文摘要，不会整场失败。</li>
                <li>同样有分页禁用（Comware <code>screen-length disable</code> / VRP <code>screen-length 0 temporary</code> / 思科 <code>terminal length 0</code>），长输出不会被截断。</li>
                <li>自定义命令巡检同样落库，可在仪表盘回放、下载结果。</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="告警联动" name="i9">
              <p>「告警管理」页面维护告警规则（指标 cpu / memory / temperature，条件 &gt;、&lt;、≥、≤，阈值，启用开关）：</p>
              <ul>
                <li>每次巡检结束后，服务端用<b>启用中</b>的规则逐台检查指标数值，命中条件的设备结果状态改为 failed，错误信息带「[告警] 规则名: metric=值 条件 阈值」前缀。</li>
                <li>「告警记录」页面展示最近触发的条目（设备、告警信息、时间），数据来源是巡检结果里带告警/失败标记的记录。</li>
                <li>因此：改了规则要<b>再跑一次巡检</b>才会按新规则判定；告警记录条数 = 失败结果条数（含巡检失败与告警触发）。</li>
              </ul>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== 网络配置 ===== -->
        <el-tab-pane label="网络配置生成" name="netconfig">
          <h3>服务器网络配置生成器</h3>
          <p>统一表格编辑器 + 实时预览面板，支持任意数量的物理口 / Bond / VLAN / 网桥自由组合。三种输出格式严格跟随所选系统，非法组合在保存/生成前就被拦下。</p>

          <el-table :data="netconfigRows" border size="small" class="mv-3">
            <el-table-column prop="item" label="配置项" width="140" />
            <el-table-column prop="desc" label="说明" />
          </el-table>

          <el-alert type="success" :closable="false" class="mv-3">
            <template #title><b>三种格式适配（os × format 严格配套）</b></template>
            <p class="tight-p"><b>netplan</b> — 仅 Ubuntu（生成 99-opstk.yaml，可选 renderer：networkd 服务器推荐 / NetworkManager）</p>
            <p class="tight-p"><b>ifcfg</b> — 仅 RHEL 家族（生成 ifcfg-files.txt，写入 /etc/sysconfig/network-scripts/，无需 NetworkManager）</p>
            <p class="tight-p"><b>nmcli</b> — 通用（RHEL 8+ / Ubuntu 22.04+ 上有 NetworkManager 即可，生成 apply-network.sh）</p>
            <p class="tight-p">组合错误（如 Ubuntu + ifcfg）会得到 422 与一句中文原因，不会静默换成别的格式。</p>
          </el-alert>

          <el-collapse v-model="netOverviewActive" class="mv-3">

            <el-collapse-item title="预览与下载：所见即所得" name="n1">
              <ul>
                <li>参数变化会自动（防抖 400ms）重新预览；校验失败时清空预览并给出逐条错误。</li>
                <li>预览区右上角有状态标签：<b>「下载内容 = 预览内容」</b>（绿色，可直接下载）或<b>「预览已失效（参数已修改）」</b>（黄色，此时下载按钮禁用，重新生成后再下载）。</li>
                <li>下载的就是预览里那份文本（本地生成 Blob，不再发第二个请求），文件名自动带主机名前缀；中文等特殊主机名会被后端替换成安全字符。</li>
                <li>后端校验不通过时红色横幅显示完整原因（含 interfaces[0].gateway 这类字段路径），旧预览同时作废。</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="校验规则（前端提示 + 后端 422 双层）" name="n2">
              <el-table :data="netValidation" border size="small">
                <el-table-column prop="rule" label="规则" min-width="240" />
                <el-table-column prop="note" label="说明" />
              </el-table>
              <p class="muted-sm mt-2">“参数有误”列表只提示不发请求；真正的边界始终在后端（同样的规则再校验一遍，绕过界面直接调 API 也进不来）。</p>
            </el-collapse-item>

            <el-collapse-item title="Bond 参数按聚合模式变化" name="n3">
              <ul>
                <li><b>mode 4 (802.3ad/LACP)</b>：显示「lacp 速率」（slow / fast）与「hash 策略」。</li>
                <li><b>mode 2 (balance-xor)</b>：显示「hash 策略」（layer2 / layer2+3 / layer3+4）。</li>
                <li><b>mode 1 / 5 / 6</b>：显示「主接口」——只允许填本 bond 的从接口，填别的网卡后端直接拒绝（内核要求 primary 是成员端口，否则参数不生效）。</li>
                <li>所有模式共用 miimon（链路检测间隔，默认 100ms）。</li>
                <li>Bond 参数值有白名单：不含逗号/等号/引号/空白，防止往 bond 选项里追加任意参数。</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="多个默认网关怎么办（metric 100/200/300）" name="n4">
              <p>管理口 + Bond 各带一个网关是合法且常见的拓扑，工具不会拒绝，而是按声明顺序给每条默认路由一个确定的 metric：第一个 100、第二个 200、第三个 300……数字越小优先级越高，前一条不可用时才回退下一条（天然带主备语义）。</p>
              <p class="muted-sm">只保留一个默认网关时输出与以前一致，不写 metric。</p>
            </el-collapse-item>

            <el-collapse-item title="netplan 的从接口自动补全" name="n5">
              <p>Bond/网桥引用了某个物理口但没单独为它加一行时，netplan 产物会自动补一段只声明不配地址的 ethernets 条目 —— 否则 networkd 会报「interface 'ens35' is not defined」起不来。同一张网卡同时被 bond 和 bridge 引用会被拒绝（nmcli 下等于先把网卡划进聚合再摘出来建桥，会产生抖动）。</p>
            </el-collapse-item>

            <el-collapse-item title="DNS 与 DHCP 的处理" name="n6">
              <ul>
                <li>DNS 输入框只在“会真的写出 DNS”的行启用：被 Bond/网桥引用的从接口不配 DNS（“从接口不配 DNS”占位提示）；static 未填 IP 的行也禁用（“先填 IP”）。</li>
                <li>DHCP 行同样可以填 DNS：nmcli 产物写 ipv4.dns（auto 模式下也生效），netplan 写 dhcp4 + nameservers，ifcfg 在 BOOTPROTO=dhcp 分支写 DNS1..N。</li>
                <li>mode=dhcp 时 IP/掩码/网关输入框禁用，残值不会提交（后端也会拒绝 DHCP 行带网关）。</li>
                <li>IP 掩码写法二选一：<code>10.0.0.1/24</code> 或 IP + 独立掩码列（255.255.255.0，必须是连续掩码）；只给 IP 不给前缀时按 /24 处理（网关同子网校验也用这个默认值）。</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="跨模块提示：静态 IP 与 DHCP 地址池重叠" name="n7">
              <p>生成时会顺手检查你填的静态 IP 是否落在 PXE 模板或 ZTP 模板的 DHCP 池里 —— 撞上了会给出黄色警告（可能和正在装机的机器拿到同一个地址），但只是提示，不拦截、不影响生成（不同网段/不同现场这是正常用法）。</p>
            </el-collapse-item>

            <el-collapse-item title="常见场景示例" name="n8">
              <pre class="code-block"># 场景1: 管理口 + 业务Bond + Bond上的VLAN
物理接口 mgmt: 10.0.0.10/24, 网关 10.0.0.1
Bond bond0: mode=4(LACP), 从接口 eth0,eth1, IP 10.10.0.10/24
VLAN bond0.100: 父接口 bond0, IP 172.16.100.10/24
VLAN bond0.200: 父接口 bond0, IP 172.16.200.10/24, 网关 172.16.200.1

# 场景2: KVM 虚拟化主机
物理接口 mgmt: 10.0.0.10/24
Bond bond0: mode=4, 从接口 eth0,eth1 (不配IP)
Bridge br0: 从接口 bond0, IP 192.168.122.1/24

# 场景3: 纯二层网桥（有 IP 即静态，无 IP 即纯二层）
Bridge br-lan: 从接口 eth2,eth3, IP 留空</pre>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== PXE ===== -->
        <el-tab-pane label="PXE 装机" name="pxe">
          <h3>PXE 装机手册</h3>
          <p>全自动网络安装 Ubuntu 与 RHEL 系系统（含 RHEL 克隆/近亲：openEuler、银河麒麟、统信 UOS、Anolis、Fedora、Oracle Linux 等，完整清单以「系统目录」为准；debian/openSUSE 目前仅支持识别 ISO 与提取引导介质，生成配置时明确拒绝自动装机）。裸机接上网线，开机即可自动安装操作系统，全程无人值守。OpsToolkit 本机可直接作为完整 PXE 服务器。</p>

          <el-collapse v-model="pxeActive" class="mt-3">

            <el-collapse-item title="工作原理：PXE 引导全链路" name="p1">
              <p>一台裸机从接电到装好系统，经过以下环节：</p>
              <pre class="code-block">裸机上电 → BIOS/UEFI 设为 PXE 启动
  ↓
(1) DHCP 请求  —  裸机广播请求 IP
  ↓                OpsToolkit 的 dnsmasq 响应:
                      · 分配一个临时 IP（standalone 模式）
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
                      · installer.squashfs（Ubuntu 安装器文件系统）
  ↓
(5) 自动安装   —  Ubuntu: autoinstall (subiquity/cloud-init)
                      RHEL:  Kickstart (应答文件 ks.cfg)
                      按模板配置: 目标盘、分区、账号密码、网络、软件包
  ↓
安装完成，重启进入新系统✔</pre>
              <p class="muted-sm mt-2">其中 (1)(2) 由 dnsmasq 完成，(3)(4)(5) 由本服务的 HTTP 文件服务（/pxe/serve）提供。应答文件里的口令是哈希；若在 .env 里启用了 pxe_serve_token，该 HTTP 根会要求 URL 带 token（生成器会自动把 token 接进所有 URL）。</p>
            </el-collapse-item>

            <el-collapse-item title="三种部署模式怎么选" name="p2">
              <el-table :data="pxeModes" border size="small" class="mv-2">
                <el-table-column prop="mode" label="模式" min-width="130" />
                <el-table-column prop="dhcp" label="DHCP 行为" min-width="220" />
                <el-table-column prop="scene" label="适用场景" min-width="280" />
              </el-table>
              <el-alert type="success" :closable="false" class="mv-2">
                <template #title><b>选择建议</b></template>
                <p class="tight-p">· <b>专用装机网段</b>（如装机 VLAN、维护机柜）→ 选 <b>standalone</b></p>
                <p class="tight-p">· <b>办公网/生产网里临时装机</b>，不想动现有 DHCP → 选 <b>proxy</b>（「部署」确认弹窗的默认值，影响面最小）</p>
                <p class="tight-p">· <b>大规模集中式部署</b>，交换机做中继 → 选 <b>relay</b></p>
              </el-alert>
              <p class="section-title mt-2">standalone（独立 DHCP）</p>
              <p>本工具自己就是 DHCP 服务器，裸机插上线就装。前提是这段网络里不能有其他 DHCP，否则会冲突抢答甚至断网 —— 确认弹窗里会红字提醒这一风险。</p>
              <p class="section-title mt-2">proxy（ProxyDHCP 代理）</p>
              <p>不动 IP 分配（现有 DHCP 照常工作），只额外广播一条 PXE 引导信息。机器同时收到两份回应：从原 DHCP 拿到 IP，从本工具拿到引导地址。</p>
              <p class="section-title mt-2">relay（中继模式）</p>
              <p>本工具完全不跑 DHCP，只提供 TFTP + HTTP 文件服务，靠交换机的 ip helper-address 把 DHCP 请求中继过来。适合大规模、集中式 PXE。</p>
            </el-collapse-item>

            <el-collapse-item title="三种添加镜像的方式" name="p2b">
              <p>ISO 镜像有三种方式进入服务器的 /srv/opstk/iso/ 目录，按文件大小与现场网络条件选择：</p>
              <p class="section-title">方式一：页面内「从 URL 拉取」（服务器可直连镜像源时）</p>
              <p>「ISO 镜像管理」卡片右上角点「添加镜像」→「从 URL 拉取」：填<b>镜像 URL</b>（支持 http/https）、<b>保存文件名</b>（可留空，从 URL 末尾推导，必须以 .iso 结尾）、<b>校验 SHA256</b>（可选）。提交后由服务器后台下载（串行执行），进度在「进行中 / 最近传输」列表实时可见，可取消；完成后回到 ISO 列表点「提取」。</p>
              <p class="section-title">方式二：页面内「本机上传」（小镜像、服务器无外网）</p>
              <p>「添加镜像」→「本机上传」：选择本地 ISO 后，前端按后端下发的分块大小（默认 8 MB）用 File.slice 分块顺序上传，界面显示进度条、已传/总量与速度，可随时取消；分块失败自动重试 3 次，仍失败可点「重试当前块」续传。单文件受 max_upload 上限与 ISO 目录剩余空间约束，超限会在提交前禁用按钮并提示改用方式三。</p>
              <p class="section-title">方式三：零代码 —— scp / SFTP 直放宿主目录（大文件推荐）</p>
              <p>大文件也可以用 <b>scp / SFTP 直接放到宿主 /srv/opstk/iso</b>，回到本页点「刷新」即可看到，<b>无需走浏览器上传</b>：</p>
              <pre class="code-block">scp Rocky-9.4-x86_64-dvd.iso root@10.128.118.113:/srv/opstk/iso/</pre>
              <el-alert type="warning" :closable="false" class="mv-2" title="空间提醒" description="根盘可用空间有限：放之前先看页面/弹窗顶部的可用量，传完记得删除不用的镜像。" />
            </el-collapse-item>

            <el-collapse-item title="操作步骤：从 ISO 到裸机装好" name="p3">
              <p class="section-title">第 1 步：获取并放置 ISO 镜像</p>
              <p>把 ISO 放到服务器 /srv/opstk/iso/ 目录（三种放法见上方「三种添加镜像的方式」：页面内 URL 拉取 / 本机分块上传 / scp·SFTP 直放；该目录同时通过 /pxe/iso 提供只读下载，且只放行 .iso 文件）：</p>
              <pre class="code-block"># Ubuntu 22.04 live-server（Ubuntu 必须是 live-server，mini.iso 不含 casper 无法用）
scp ubuntu-22.04.5-live-server-amd64.iso yang@服务器IP:/srv/opstk/iso/

# RHEL/Rocky（DVD 或 Boot 均可，装包走镜像源或本地 repo）
scp Rocky-9.5-x86_64-dvd.iso yang@服务器IP:/srv/opstk/iso/</pre>

              <p class="section-title mt-3">第 2 步：提取引导文件</p>
              <p>进入「PXE 装机」页面的「ISO 镜像管理」面板：</p>
              <ol class="indent-ol">
                <li>列表中会显示已放置的 ISO 文件名称和大小</li>
                <li>为该 ISO 选择 OS 类型和版本号 —— 类型下拉来自系统目录（可搜索），版本有常见候选并支持从文件名自动识别；提取前有确认弹窗，明确目标目录</li>
                <li>点「提取」，系统自动挂载 ISO 并提取引导文件（Ubuntu: vmlinuz/initrd/installer.squashfs；RHEL: vmlinuz/initrd）</li>
              </ol>
              <p class="muted-sm mt-1">提取目录是 /srv/opstk/pxe-web/&lt;系统&gt;/&lt;版本&gt;/，必须与模板里的「系统 + 版本」完全一致，否则生成的内核 URL 是 404，机器端只报 "Could not boot image"。</p>

              <p class="section-title mt-3">第 3 步：创建装机模板</p>
              <p>点「新建装机模板」，填写系统类型/版本、管理员账号密码、磁盘方案与目标磁盘（字段详解见下）、网络模式、镜像源等。新建时管理员密码必填（后端拒绝空口令，也不代填默认口令）。</p>

              <p class="section-title mt-3">第 4 步：生成预览 / 一键部署</p>
              <p>「生成配置」弹窗可调部署模式、PXE 服务 IP、内核路径等并逐文件预览；「部署」确认弹窗（默认 ProxyDHCP）按模板生成并写盘：</p>
              <pre class="code-block">✓ 生成 dnsmasq 配置      (写入 /etc/dnsmasq.d/opstk-pxe.conf，宿主机 dnsmasq 真正重载并核对生效)
✓ 落地应答文件           (profiles/<模板>/ 下的 user-data/ks.cfg、boot.ipxe、meta-data)
✓ 复制 iPXE 固件         (ipxe.efi → UEFI，undionly.kpxe → BIOS)
✓ 修复 SELinux 上下文    (RHEL/Rocky，semanage/restorecon)
✓ 重启 dnsmasq           (失败会明确报错并回滚，不会假装成功)</pre>
              <p class="mt-1">部署日志实时显示在顶部「部署日志」区域，并注明实际写入了哪些文件。注意：「生成配置」弹窗与「部署」用的是两套参数 —— 部署固定按模板生成（内核路径留空由后端按模板系统/版本推导，HTTP 根由后端强制为本机 /pxe/serve），要覆盖路径请用「生成配置」弹窗里的参数再部署。</p>

              <p class="section-title mt-3">第 5 步：裸机 BIOS/UEFI 设置</p>
              <p>将裸机接上与服务器同一网段的网线，开机进入 BIOS/UEFI 设置，把 Network/PXE Boot 打开并调到启动顺序第一位（部分服务器可按 F12 临时选择网络启动）。32 位 UEFI（client-arch 6）的 iPXE 固件发行版不提供，当前不支持。</p>

              <p class="section-title mt-3">第 6 步：验证装机</p>
              <ol class="indent-ol">
                <li>裸机屏幕出现 iPXE 引导画面 → 正在下载内核</li>
                <li>出现 Ubuntu/RHEL 安装画面 → 正在分区安装</li>
                <li>安装完成后自动重启 → 进入登录界面</li>
                <li>用模板中配置的账号密码登录验证</li>
              </ol>
              <p class="muted-sm mt-1">默认内核参数带 <code>console=tty0 console=ttyS0,115200</code>：串口能看到安装器界面与全部日志，装机失败时串口日志是第一手证据（可在模板里改）。</p>
            </el-collapse-item>

            <el-collapse-item title="装机模板字段说明" name="p4">
              <el-table :data="pxeFields" border size="small">
                <el-table-column prop="field" label="字段" width="140" />
                <el-table-column prop="required" label="必填" width="60" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
              <p class="muted-sm mt-2">RHEL 系还有「root 密码」字段；「额外软件」逗号分隔追加包；「安装后脚本」在装完系统后以 root 执行（Ubuntu 走 late-commands，RHEL 走 %post）。</p>
            </el-collapse-item>

            <el-collapse-item title="目标盘选择：三种模式与语义" name="p5">
              <ul>
                <li><b>自动（最大盘）</b>：换硬件不用改模板。RHEL 系在 %pre 里按「非可移动、非光驱、容量达标（可用“最小容量”过滤）、按盘名排序取第一块」现场选盘，再 %include 生成的分区片段 —— NVMe(nvme0n1)/virtio(vda) 都能装。<b>Ubuntu 不支持自动选盘</b>：subiquity 没有“自动挑最大盘”的写法，Ubuntu + 自动选盘会被 422 拒绝，必须按盘名指定。</li>
                <li><b>按盘名指定</b>：直接写设备名（sda / vda / nvme0n1）。注意盘名由内核探测顺序决定，同一台机器两次启动都可能互换；好处是填错会明确报错、不会错装。</li>
                <li><b>按序列号/型号</b>：序列号最稳（如 S3Z1NB0K123456），型号做兜底。Ubuntu 侧 subiquity 取的序列号来自 sysfs，QEMU/虚拟化下常为空（报 matched no disk）——虚拟机请用按盘名。</li>
                <li><b>「清空目标盘」开关</b>：只清空目标盘本身（ignoredisk --only-use 把安装范围钉死在这块盘上），其它盘一律不碰；关掉则保留目标盘既有分区表（仅 Ubuntu 的自定义布局等场景需要）。</li>
              </ul>
              <el-alert type="warning" :closable="false" class="mt-2" title="Ubuntu 自定义分区必须按盘名" description="subiquity 认盘的方式与 RHEL 不同：serial 取 sysfs（虚拟化下为空）、wwn/model 可能缺失或歧义、不认识的键不报错而是退回“匹配第一块盘”——若数据盘排在前面会直接抹掉数据盘，产品已拒绝这种写法。Ubuntu 侧请选「按盘名」填系统盘设备名，并确认它与数据盘不是同一块盘。" />
            </el-collapse-item>

            <el-collapse-item title="数据盘与 RAID：格式化 vs 挂已有文件系统" name="p6">
              <p>「其它数据盘」默认<b>不格式化、不动数据</b>；每行可选：</p>
              <ul>
                <li><b>格式化新建</b>：打开「格式化」开关 + 填挂载点（如 /data），建新分区并格式化后挂载。</li>
                <li><b>挂已有文件系统</b>：在「挂已有文件系统」列填该分区文件系统的 UUID 或卷标 —— 保留数据、不格式化（此时「格式化」开关被自动禁用，后端对“既挂已有文件系统又格式化”直接 422，因为 wipe 会清掉分区表）。</li>
                <li><b>排除保护</b>：即使不挂载，数据盘也要给「容量 / 序列号 / WWID」之一作为稳定识别条件 —— 自动选盘靠它把这块盘排除在系统盘候选之外（盘名会漂移，不能当判据；没有识别条件时提交会被拦下并点名补哪一行）。</li>
                <li><b>挂已有文件系统目前只在 RHEL 系实现</b>：Ubuntu（subiquity）侧没有对应的 preserve 能力，给了会被后端拒绝（422，原因写明“挂载已有文件系统目前只在 RHEL 系实现”）。</li>
              </ul>
              <p>RAID 仅在自定义分区表下支持：设备列表填成员分区序号（如 <code>3,4</code> → part.03/part.04），支持级别 0/1/5/6/10，可挂载点 + 文件系统。</p>
              <p class="muted-sm mt-2">分区方案说明：LVM（推荐）/ 直通分区由安装器自动完成；自定义分区表逐行定义（大小 512M/20G/rest，rest 只能最后一行；挂载点留空 = 只建分区不挂载；填了 VG 与 LV 才是 LVM 逻辑卷），且必须包含一个 / 分区。自定义分区表下才能配 RAID 与数据盘挂载。</p>
            </el-collapse-item>

            <el-collapse-item title="生成的文件与 token 保护" name="p7">
              <el-table :data="pxeFiles" border size="small">
                <el-table-column prop="file" label="文件" width="150" />
                <el-table-column prop="role" label="作用" width="110" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
              <p class="mt-2"><b>token 保护</b>：/pxe/serve 是无认证的 HTTP 根（iPXE、anaconda、cloud-init 这些客户端不支持认证头），ks.cfg / user-data 里含口令哈希。可在 backend/.env 里配置 pxe_serve_token（≥16 位）启用门禁：URL 形如 /pxe/serve/&lt;token&gt;/ks.cfg 或 /pxe/serve/ks.cfg?t=&lt;token&gt;；留空 = 不启用（行为与从前逐字相同），启用/更换后必须重新部署一次。PXE 页面顶部会如实显示“是否启用”。TFTP 一侧（ipxe 固件）与 /pxe/iso 不在门禁内（前者无认证可言，后者只发 .iso）。</p>
            </el-collapse-item>

            <el-collapse-item title="装机记录的状态与「装完不再重装」" name="p9">
              <p><b>状态只有两个：待装机 / 已装完。</b>「已装完」不是摆设 —— 它决定 dnsmasq 还给不给这台机器下发自动装机菜单。改这个状态之前，装完的机器每次走网卡引导都会被<strong>重装一遍（盘会被再抹一次）</strong>。</p>
              <ul>
                <li><b>怎么变成「已装完」</b>：① 机器自己回调 —— 生成应答文件时会把一条回调写进 RHEL 的 <code>%post</code> / Ubuntu 的 <code>late-commands</code>（<code>curl -X POST /api/it/pxe/installs/&lt;记录ID&gt;/done?t=&lt;一次性令牌&gt;</code>，令牌由后端密钥派生、不落库；网络不通只会被 <code>|| true</code> 吞掉，不会把装好的系统判成失败）；② 人工在「装机记录」里点<strong>「标记完成」</strong>。</li>
                <li><b>标记完成之后</b>：该 MAC 的按机装机菜单不再下发（原位置留一行注释），<code>dhcp-host=&lt;MAC&gt;,&lt;IP&gt;</code> 地址预留<strong>保留</strong>（重装/重启 IP 不漂）；它落到「未登记默认菜单」——明确拒绝自动安装然后 <code>exit</code>，固件继续按引导顺序引导本地磁盘。因此<b>推荐把裸机引导顺序设成「先硬盘、后网卡」</b>：空盘装机会自然落到网卡，装好的机器直接起系统。</li>
                <li><b>要重装这台机器</b>：点<strong>「重新启用装机」</strong>（二次确认会提醒抹盘）→ 记录退回「待装机」并自动重新部署一次 → 机器下次网卡引导即重新安装。</li>
                <li><b>标记完成会顺带自动重部署</b>：用的还是上次部署的 server_ip / 部署模式（记在 <code>/srv/opstk/state/last-deploy.json</code>），同一个红线守卫照旧生效；若该模板从未部署过，会提示「未找到上次部署参数，请手动点一次部署」。</li>
                <li>装机记录一直停在「待装机」时页面会保持轮询（用来跟踪进度）；全部标记完成后轮询自动停。</li>
              </ul>
            </el-collapse-item>

            <el-collapse-item title="明确不支持 / 会被拒绝的写法" name="p8">
              <ul>
                <li>Ubuntu + 自动选最大盘（subiquity 无此能力，422 拒绝）。</li>
                <li>Ubuntu 侧挂已有文件系统（preserve 未实现，422 拒绝）。</li>
                <li>非自定义布局下给自定义分区表 / RAID / 数据盘挂载点（这些产物不会生成，直接拒绝并提示改用自定义分区表）。</li>
                <li>自定义分区表缺 / 分区（没有 / 的系统起不来）。</li>
                <li>数据盘只给盘名、没有 size/serial/wwid 任一稳定识别条件（提交被拦，防止自动选盘误清数据盘）。</li>
                <li>「挂已有文件系统」同时开「格式化」（互相矛盾，422）。</li>
                <li>32 位 UEFI 引导（发行版无 i386 iPXE 固件）。</li>
                <li>装机模板管理员口令留空（安全设计：不代填默认口令）。</li>
                <li>ZFS 暂未支持（后端未实现，选项已移除）：分区方案只提供 LVM / 直通分区 / 自定义分区表；API 直连传 disk_scheme=zfs 仍会被 422 拒绝。</li>
              </ul>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== ZTP ===== -->
        <el-tab-pane label="ZTP 开局" name="ztp">
          <h3>ZTP 配置开局手册</h3>
          <p>网络/安全设备（H3C、华为、思科）首次上电时空配置启动，会自动通过 DHCP 获取 TFTP 地址并下载配置文件。OpsToolkit 可生成全套开局文件并通过 dnsmasq 下发，支持“落位登记 + MAC 认领”。</p>

          <el-collapse v-model="ztpActive" class="mt-3">

            <el-collapse-item title="当前能力范围（如实说明）" name="z0">
              <el-alert type="warning" :closable="false" class="mv-2" title="ZTP 目前是「基线开局」" description="生成的设备配置覆盖：主机名、管理 VLAN/SVI（或物理管理口 + 切三层）、管理 IP（静态或 DHCP）、默认路由、DNS、NTP（可选）、VLAN 规划与上联/接入口划分、本地管理员账号（SSH 服务 + vty 认证）、SNMP v2c 团体、域名（可选）、自定义 CLI 追加。业务侧配置（路由协议、ACL、QoS 等）尚未开发 —— 有需要的用「自定义配置」字段手动追加厂商 CLI。" />
              <p class="muted-sm">安全设计：设备管理员口令必填、绝不代填默认口令；模板里的域名/SNMP 团体/NTP 等会拼进设备命令行的字段有字符白名单（引号/换行/控制字符直接拒绝，防止配置注入）；NTP 留空 = 不下发（不再代填一个现场不可达的默认值）。</p>
            </el-collapse-item>

            <el-collapse-item title="工作原理与三厂商差异" name="z1">
              <pre class="code-block">设备首次上电 (空配置)
  ↓
(1) 发起 DHCP 请求  —  设备以自己的 MAC 地址发起请求
  ↓                   dnsmasq 响应: 分配 IP + 告知取配置的位置
  ↓
(2) 下载配置     —  按厂商机制取文件
                      · H3C:  DHCP option 66+67 (TFTP) → ztp/<主机名或序列号>.cfg
                      · 华为: option 66+67 → 中间文件 ztp_intermediate.txt → 按指引下载 .cfg
                      · 思科: option 150 + 67 → ztp_bootstrap.py 脚本拉取 .cfg
  ↓
(3) 加载配置       —  管理 IP/VLAN/账号/SSH/SNMP/NTP 等基线生效
  ↓
开局完成，设备可远程管理✔</pre>
              <el-table :data="ztpOptions" border size="small" class="mv-2">
                <el-table-column prop="vendor" label="厂商" width="170" />
                <el-table-column prop="opt" label="DHCP Option" width="200" />
                <el-table-column prop="mech" label="工作机制" />
              </el-table>
              <p class="section-title mt-3">华为要分清 VRP5 与 VRP8</p>
              <p>模板厂商下拉是四个选项：<b>H3C</b>、<b>华为 VRP5（S 系列交换机/AR 路由器）</b>、<b>华为 VRP8（CE/NE 系列，CloudEngine）</b>、<b>思科</b>。两者命令差异已被真机实测钉住：</p>
              <ul>
                <li>VRP8 本地账号用 <code>password irreversible-cipher</code>（收明文）+ 内置用户组 <code>user-group manage-ug</code>（没有 privilege level）；VRP5 是 privilege level 15。</li>
                <li>VRP8 本地用户名<b>至少 6 个字符</b>（设备实测要求 STRING&lt;6-253&gt;），填 5 位会被拒。</li>
                <li>VRP8 的 NTP 命令是 <code>ntp unicast-server</code>（VRP5 是 ntp-service …）。</li>
                <li>VRP8 有两阶段提交：配置结尾用 <code>commit</code> 收尾（用 return 会弹 [Y/N/C] 交互确认）；VRP5 用惯例的 return。改完记得在设备上 <code>save</code>（save 只能在用户视图执行，系统视图里报 Unrecognized）。</li>
                <li>VRP5 与思科的生成语法按文档编写，产物中会标注「未真机验证」；H3C 与 VRP8（含物理管理口切三层）已真机验证。</li>
              </ul>
              <p class="section-title mt-2">管理口可以填物理口</p>
              <p>「管理SVI」除了 Vlan-interface10/Vlanif10 这类 VLAN 接口，也可以填物理口（GE1/0/24 / WGE1/0/4 等）：生成时会在它上面配管理 IP，并按平台先切三层（Comware <code>port link-mode route</code>、VRP8 <code>undo portswitch</code>、思科 <code>no switchport</code>）。物理口名字里的数字不是 VLAN 号，接入/上联端口仍按「管理VLAN」划分。留空则按厂商 + 管理 VLAN 自动推导 SVI 名。</p>
            </el-collapse-item>

            <el-collapse-item title="操作步骤：从创建到设备开局" name="z2">
              <p class="section-title">第 1 步：创建 ZTP 模板</p>
              <p>进入「ZTP 开局」页面，点「新建开局模板」，填写：模板名称、厂商（H3C / 华为VRP5 / 华为VRP8 / 思科）、域名（可选）、管理 VLAN、管理 SVI（或物理管理口）、掩码、网关、DNS、NTP（留空 = 不下发）、VLAN 规划（每行“VLAN号,名称”）、管理员与密码（新建必填；编辑留空 = 不修改）、SNMP 团体（华为 VRP8/CE 要求 8-32 字符）、上联口/接入口、自定义配置。</p>
              <p class="section-title mt-3">第 2 步：（可选）设备清单或落位登记</p>
              <p>两处都可以不填 —— 没有任何按 MAC 的登记时，所有设备统一拿 <code>ztp/default.cfg</code> 基础配置，管理口自动走 DHCP 取址（不会写死一个假地址造成多台设备 IP 冲突）。要按设备下发各自的主机名/管理 IP，再登记：</p>
              <ul>
                <li><b>设备清单</b>：主机名 + MAC 必填（MAC 用于 DHCP 匹配），序列号仅用于文件命名，管理 IP 可选（不填 = 该设备管理口走 DHCP）。</li>
                <li><b>落位登记（推荐）</b>：设备到货时只有机架位置和规划 IP/主机名，没有 MAC —— 先按落位登记（MAC 留空）；设备上电后，dnsmasq 租约里自动出现它的 MAC，「待认领设备」列表读租约显示出来，点「认领到落位」把它指到落位即可，全程不手抄 MAC。认领后必须<b>重新生成并部署</b>，设备才会拿到自己落位规划的配置。</li>
              </ul>
              <p class="section-title mt-3">第 3 步：生成配置 + 部署</p>
              <p>点「生成配置」逐文件预览（设备配置 / dnsmasq.conf / 中间文件 / README）。注意两点：</p>
              <ol class="indent-ol">
                <li>「DHCP网卡」必填且必须是宿主机真实网卡（如 ens19）：留空或填 eth0/eth1/ens0 这类占位值能保存、能生成 ZIP，但<b>部署时会被红线检查拒绝</b>（dnsmasq 配了 bind-interfaces，网卡不存在会起不来）。</li>
                <li>参数改过而没重新生成时，「下载 ZIP」和「部署到本机」都会禁用，并提示先重新生成 —— 保证“审核看到的 = 实际部署的”。</li>
              </ol>
              <p>然后「部署到本机」（写 TFTP/HTTP + 宿主机 dnsmasq 重载并核对生效）或「下载 ZIP」手动部署。</p>
              <p class="section-title mt-3">第 4 步：设备上电</p>
              <ol class="indent-ol">
                <li>设备接入与服务器同网段的端口（或 Trunk 口）</li>
                <li>确保设备为出厂默认配置（空配置）</li>
                <li>上电后设备自动发起 DHCP 并下载配置</li>
                <li>完成后可用模板中的管理 IP（或 DHCP 地址 + 租约/MAC 认领）与账号登录</li>
              </ol>
            </el-collapse-item>

            <el-collapse-item title="生成的文件说明" name="z3">
              <el-table :data="ztpFiles" border size="small">
                <el-table-column prop="file" label="文件" width="190" />
                <el-table-column prop="vendor" label="厂商" width="90" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
              <p class="muted-sm mt-2">dnsmasq.conf 里的 DHCP 应答是完整的（含租期 option 51），H3C/华为走 option 66+67、思科走 option 150+67；按 MAC 的 tag 匹配把已认领设备指向各自的 .cfg，未认领的落位只生成注释占位，不影响 default.cfg 下发。</p>
            </el-collapse-item>

            <el-collapse-item title="三种部署模式与红线检查" name="z4">
              <p>ZTP 与 PXE 共用同一套部署模式语义：</p>
              <el-table :data="pxeModes" border size="small" class="mv-2">
                <el-table-column prop="mode" label="模式" min-width="130" />
                <el-table-column prop="dhcp" label="DHCP 行为" min-width="220" />
                <el-table-column prop="scene" label="适用场景" min-width="280" />
              </el-table>
              <p>落盘前有一道<b>红线检查</b>：占位网卡名（eth0/eth1/ens0/空）、明显的地址配置错误等会被拒绝，错误信息直接告诉运维差在哪；部署失败会明确报错并回滚已写文件，不会让“配置写下去了但 dnsmasq 还是旧的”被当成成功。</p>
              <p class="mt-2"><b>token 保护</b>：/ztp 静态根上的设备配置里是<b>明文</b>口令（H3C password simple / VRP8 irreversible-cipher），可在 .env 配置 ztp_serve_token 启用门禁（URL 带 token，华为中间文件/思科脚本的下载地址会自动带上）。注意：H3C 的 auto-config 走 TFTP，不经 HTTP，这条开关对 H3C 链路既无保护也无影响。</p>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>

        <!-- ===== 部署指南 ===== -->
        <el-tab-pane label="部署指南" name="deploy">
          <h3>部署指南</h3>

          <el-alert type="info" :closable="false" title="两种部署方式" description="方式一: 直接部署在 Linux 服务器上（性能最好）。方式二: 用 Docker 容器运行（方便迁移，当前生产采用）。两种方式的 PXE/ZTP 功能相同；Windows/macOS 只能跑 Web 界面与生成/下载，本机部署按钮会自动禁用。" class="mv-3" />

          <el-collapse v-model="deployActive" class="mt-3">

            <el-collapse-item title="前置条件：环境要求" name="d0">
              <el-table :data="deployReqs" border size="small">
                <el-table-column prop="item" label="项目" width="150" />
                <el-table-column prop="req" label="要求" />
                <el-table-column prop="note" label="说明" width="200" />
              </el-table>
              <p class="muted-sm mt-2">注: Windows/macOS 可运行 Web 界面、巡检、生成配置、下载 ZIP，但无法直接运行 PXE/ZTP 服务（DHCP/TFTP 需 Linux 内核，页面会提示改用「下载 ZIP」手动部署）。</p>
            </el-collapse-item>

            <el-collapse-item title="方式一：直接部署在 Linux 服务器" name="d1">
              <p class="lead-primary mb-2">适用于 Rocky/RHEL 9 或 Ubuntu 22.04+</p>
              <p class="section-title">第 1 步：安装依赖包</p>
              <pre class="code-block"># RHEL / Rocky
dnf install -y dnsmasq ipxe util-linux python3 python3-pip

# Ubuntu / Debian
apt update && apt install -y dnsmasq ipxe util-linux python3 python3-pip</pre>
              <p class="section-title mt-3">第 2 步：创建虚拟环境并安装依赖</p>
              <pre class="code-block">python3 -m venv /opt/opstk/venv
/opt/opstk/venv/bin/pip install -r requirements.txt</pre>
              <p class="muted-sm mt-1">注意 paramiko 已钉在 3.5.1（支持 ssh-rsa 老设备），不要随意升级到 4/5 —— 升级前先确认 /health 的 legacy_ssh 仍为 true。</p>
              <p class="section-title mt-3">第 3 步：上传代码</p>
              <pre class="code-block">将项目的 backend/ 和 frontend/dist/ 上传到 /opt/opstk/

最终目录结构:
/opt/opstk/
  backend/
    app/          # FastAPI 后端
    data/         # SQLite 数据库 (自动创建)
    .env          # 密钥（首次启动自动生成，务必备份）
    requirements.txt
  frontend/
    dist/         # 前端构建产物</pre>
              <p class="section-title mt-3">第 4 步：配置 sudo 免密（必要）</p>
              <pre class="code-block"># 后端管控 dnsmasq、写配置、挂载 ISO 都要走 sudo -n（免密），
# 涉及的命令以 backend/app/core/dhcp.py 与 it/pxe/server.py 里的调用为准：
echo 'yang ALL=(root) NOPASSWD: /usr/bin/systemctl * dnsmasq, /usr/sbin/systemctl * dnsmasq, /usr/bin/tee /etc/dnsmasq.d/*, /usr/bin/chown, /bin/chown, /usr/bin/rm, /bin/rm, /usr/bin/mount, /bin/mount, /usr/bin/umount, /bin/umount, /usr/sbin/restorecon, /sbin/restorecon, /usr/sbin/semanage, /sbin/semanage, /usr/bin/true, /bin/true' > /etc/sudoers.d/opstk
chmod 440 /etc/sudoers.d/opstk
visudo -cf /etc/sudoers.d/opstk  # 验证语法
sudo -n true                     # 免密生效验证</pre>
              <p class="section-title mt-3">第 5 步：创建目录 + 修复 SELinux</p>
              <pre class="code-block">mkdir -p /srv/tftp/boot /srv/opstk/pxe-web /srv/opstk/ztp-web /srv/opstk/iso /srv/opstk/mnt /srv/opstk/state
chown -R yang /srv/tftp /srv/opstk

# RHEL/Rocky 需要修复 SELinux (Ubuntu 跳过此步)
semanage fcontext -a -t tftpdir_t '/srv/tftp(/.*)?'
semanage fcontext -a -t tftpdir_t '/srv/opstk/pxe-web(/.*)?'
restorecon -R /srv/tftp /srv/opstk/pxe-web</pre>
              <p class="section-title mt-3">第 6 步：启动服务</p>
              <pre class="code-block">cd /opt/opstk/backend
PYTHONPATH=/opt/opstk/backend /opt/opstk/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000

# 首次启动会:
#   1. 生成 backend/.env（JWT 密钥 + 凭据加密密钥，跨进程加锁写）
#   2. 建表 + 写入各厂商默认巡检模板
#   3. 创建 admin 账号并打印一次性随机初始口令（看日志！）
#   4. 自检老设备 SSH 能力（缺失会告警）
# 验证: curl http://localhost:8000/health</pre>
              <p class="section-title mt-3">开机自启配置（可选，systemd 单元由部署侧自建）</p>
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

            <el-collapse-item title="方式二：Docker 容器部署（当前生产方式）" name="d2">
              <p class="lead-primary mb-2">一条命令构建镜像（Ubuntu 22.04 + dnsmasq + iPXE + Python + 前端构建物）</p>
              <p class="section-title">构建并启动</p>
              <pre class="code-block">cd /opt/opstk
docker compose up -d --build

# 日常管理
docker compose ps / restart / logs -f --tail=100

# 导出镜像文件（用于离线迁移）
docker save opstk-opstoolkit:latest | gzip > opstk-image.tar.gz</pre>
              <p class="section-title mt-3">docker-compose.yml 关键配置说明</p>
              <el-table :data="composeConfig" border size="small">
                <el-table-column prop="key" label="配置项" min-width="200" />
                <el-table-column prop="why" label="为什么需要" min-width="320" />
              </el-table>
              <p class="section-title mt-3">数据挂载说明（下表均为宿主机目录/文件的绑定挂载，非具名卷）</p>
              <el-table :data="volumeConfig" border size="small">
                <el-table-column prop="vol" label="宿主机路径" width="170" />
                <el-table-column prop="path" label="容器内路径" width="190" />
                <el-table-column prop="desc" label="内容" />
              </el-table>
              <p class="muted-sm mt-2">关键三条：<b>backend/.env 必须挂</b>（否则重建容器会丢密钥，加密凭据永久打不开）；<b>/srv/opstk/ztp-web 必须挂</b>（不挂则 /ztp 静态服务根本不会注册）；<b>/var/lib/dnsmasq 只读挂</b>（ZTP 落位认领靠它读设备 MAC）。</p>
            </el-collapse-item>

            <el-collapse-item title="首次使用流程与 /health 自检" name="d3">
              <ol>
                <li><b>自检</b> — 浏览器打开 <code>http://服务器IP:8000/health</code>，确认：<code>status: ok</code>；<code>legacy_ssh: true</code>（false 时看 legacy_ssh_detail，老设备将无法巡检）。</li>
                <li><b>登录</b> — admin + 初始随机口令（服务日志里找“初始随机口令”，只在创建时打印一次；也可用环境变量 ADMIN_PASSWORD 固定）。</li>
                <li><b>录入资产/凭据</b> — CT 设备与 IT 服务器统一在「资产管理」，凭据在「凭据管理」。</li>
                <li><b>PXE 装机</b> — 放 ISO → 提取 → 建模板 → 部署 → 裸机接线开机。</li>
                <li><b>ZTP 开局</b> — 建模板 →（落位/认领）→ 生成 → 部署 → 设备上电。</li>
              </ol>
              <el-alert type="success" :closable="false" title="快速验收清单" description="1) /health 返回 legacy_ssh=true；2) 巡检一台设备，进度行出现“已开始 1/1 · 完成 1 · 失败 0”并能下载 txt 报告；3) 网络配置生成能预览并下载，下载内容与预览一致；4) PXE 页面能提取 ISO 并部署成功（部署日志列出写入文件）。" class="mt-3" />
            </el-collapse-item>

            <el-collapse-item title="排错指南：常见问题" name="d4">
              <el-collapse v-model="troubleActive">
                <el-collapse-item title="巡检卡住 / 报“设备不可达”" name="t1">
                  <p>先看报错：3 秒内 TCP 建不起来会立即报「设备不可达：IP:端口 …」，不会久等。排查顺序：</p>
                  <pre class="code-block">ping 设备IP            # ICMP 通不通（通 ≠ 22 端口通）
telnet 设备IP 22        # 22 端口通不通
# 中间有防火墙/隔离网段时，确认放行了服务器的出站 22
# 路由不对（地址被丢给默认网关）是最常见的“卡住”原因，预检后 3 秒即报</pre>
                  <p>若报的是「SSH 握手失败」而不是不可达，看下一节。</p>
                </el-collapse-item>
                <el-collapse-item title="老设备 SSH：Incompatible ssh peer / no acceptable host key" name="t2">
                  <p>这是算法不兼容，<b>不是账号密码错</b>（换密码没用）。</p>
                  <p>① 打开 <code>GET /health</code> 看 <code>legacy_ssh</code>：true = 服务侧没问题，去设备侧处理；false = 把 paramiko 钉回 3.5.1（requirements.txt 里有注释说明原因）。</p>
                  <p>② 设备侧生成较新的主机密钥（推荐）：华三 <code>public-key local create ecdsa secp256r1</code>（真机验证）；华为 <code>ecc local-key-pair create</code>；思科 <code>crypto key generate rsa modulus 2048</code>。</p>
                  <p>③ ssh-dss / SSH v1 这类已淘汰算法需单独评估，不建议开启。</p>
                </el-collapse-item>
                <el-collapse-item title="巡检结果里出现「该机型不支持此指标」" name="t3">
                  <p>这不是故障：设备对所有候选命令都回了 Unrecognized/参数过多，说明该机型没有这个特性（如 vFW/vSR 没有 display environment）。界面上灰色标注，状态为 unsupported。想让某条指标走别的命令，把它克隆进自定义模板并改命令即可。</p>
                </el-collapse-item>
                <el-collapse-item title="华为 VRP8 开局注意（真机实测）" name="t4">
                  <ul>
                    <li>本地用户名至少 6 个字符（5 位的 opstk 会被设备拒绝）。</li>
                    <li>生成的配置以 <code>commit</code> 结尾（两阶段提交，不 commit 接口 IP 不生效）；设备上保存配置用 <code>save</code>，且只能在用户视图执行（系统视图里报 Unrecognized）。</li>
                    <li>VRP8 的 SNMP 团体名要求 8-32 字符。</li>
                    <li>模板厂商务必选对：VRP5 与 VRP8(CE/NE) 命令不同（NTP/账号/提交语法都不同）。</li>
                  </ul>
                </el-collapse-item>
                <el-collapse-item title="隔离网段 / 实验环境注意事项" name="t5">
                  <ul>
                    <li>巡检地址必须与服务器有<b>路由可达的 TCP 22</b>；实测踩过“ICMP 能通、TCP 永远不通”的模拟环境（数据面上送路径问题）—— ping 通连不上时先 telnet 22 验证。</li>
                    <li>PXE/ZTP 的 DHCP 广播不过路由：裸机/设备必须与服务器同广播域，跨网段用 relay 模式 + 交换机 ip helper-address。</li>
                    <li>装机/开局网段建议物理隔离：/pxe/serve 与 /ztp 是无认证 HTTP 根（可再加 token 门禁），网段隔离是第一层防线。</li>
                  </ul>
                </el-collapse-item>
                <el-collapse-item title="dnsmasq 启动失败：Permission denied / SELinux" name="t9">
                  <p>RHEL/Rocky 上 SELinux 会阻止 dnsmasq 访问 TFTP 目录。运行：</p>
                  <pre class="code-block">semanage fcontext -a -t tftpdir_t '/srv/tftp(/.*)?'
restorecon -R /srv/tftp</pre>
                  <p>部署时系统会自动执行此操作，手动部署时需手动执行。</p>
                </el-collapse-item>
                <el-collapse-item title="dnsmasq 启动失败：not configured to listen / illegal repeated keyword" name="t6">
                  <p>网卡名不匹配或配置冲突。部署后查看 /etc/dnsmasq.d/opstk-pxe.conf 中 interface= 是否为宿主机真实网卡：</p>
                  <pre class="code-block">ip -br link          # 看真实网卡名
systemctl status dnsmasq -l   # 看报错详情</pre>
                  <p>另外 dnsmasq 的 <code>port</code> 关键字不可重复：/etc/dnsmasq.d 下两份文件都写 port=0 会让整份配置加载失败（本工具生成的 ZTP 配置故意不写它）。</p>
                </el-collapse-item>
                <el-collapse-item title="sudo: a password is required" name="t7">
                  <p>sudo 免密未配置成功。重新配置 /etc/sudoers.d/opstk（命令清单见「方式一」第 4 步），确保路径正确（systemctl 在 /usr/sbin/ 或 /usr/bin/）。</p>
                  <pre class="code-block">sudo -n true  # 测试免密是否生效</pre>
                </el-collapse-item>
                <el-collapse-item title="裸机 PXE 引导卡住，无法下载内核" name="t7b">
                  <p>检查内核文件是否存在：在 PXE 页面查看 HTTP 文件列表是否包含对应顶层目录（该列表仅显示顶层条目，不展开子目录，因此不会显示 ubuntu/22.04/ 这类子路径）。没有就是 ISO 未提取，或提取时选错了系统/版本（提取目录必须与模板的系统+版本一致）。</p>
                </el-collapse-item>
                <el-collapse-item title="容器部署: port 67 already in use" name="t8">
                  <p>宿主机上有其他 DHCP 服务。停掉宿主机的 DHCP，或者改用 ProxyDHCP 模式。</p>
                  <pre class="code-block">systemctl stop dnsmasq dhcpd 2>/dev/null  # 停掉宿主机 DHCP</pre>
                </el-collapse-item>
              </el-collapse>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>


<!-- ===== 网络配置生成器使用指南 ===== -->
        <el-tab-pane label="网络配置生成" name="netconfig-usage">
          <h3>网络配置生成器使用指南</h3>
          <p>生成 Ubuntu netplan、RHEL ifcfg 或通用 nmcli 配置脚本，支持物理口、Bond、VLAN、网桥；下载内容与预览内容永远是同一份。</p>

          <el-collapse v-model="netconfigActive" class="mt-3">

            <el-collapse-item title="操作步骤" name="nc1">
              <el-steps direction="vertical" :active="5">
                <el-step title="第1步：选择系统与格式" description="顶部切换 OS 与格式（os × format 严格配套：netplan 只能配 Ubuntu，ifcfg 只能配 RHEL）。Ubuntu 静态 IP 服务器推荐 netplan + networkd。" />
                <el-step title="第2步：添加组件" description="按钮添加物理接口 / Bond / VLAN / 网桥，行数不限；每行可复制/删除，参数变化自动重新预览。" />
                <el-step title="第3步：配置参数" description="每行可选 DHCP 或填 IP/掩码（10.0.0.1/24 或掩码列）、网关、DNS。Bond 从接口逗号分隔；VLAN 选父接口 + VLAN 号（名称自动生成 父接口.VLAN号）。" />
                <el-step title="第4步：核对预览" description="左侧编辑右侧预览；出现“参数有误 N 项”时逐条修正（接口名/IP/网关同子网/VLAN ID 等）；预览标签显示「下载内容 = 预览内容」才可下载。" />
                <el-step title="第5步：下载并应用" description="下载的文件直接在目标机器上执行（见下节「下载后怎么用」）。" />
              </el-steps>
            </el-collapse-item>

            <el-collapse-item title="配置类型说明" name="nc2">
              <el-table :data="netconfigRows" border size="small">
                <el-table-column prop="item" label="类型" width="150" />
                <el-table-column prop="desc" label="说明" />
              </el-table>
              <p class="muted-sm mt-2">被 Bond/网桥引用的从接口不需要（也不应该）单独配 IP；netplan 产物会为“被引用但未声明”的物理口自动补一段空声明。</p>
            </el-collapse-item>

            <el-collapse-item title="下载后怎么用" name="nc3">
              <p class="lead-primary">Ubuntu 22.04+ (netplan → 99-opstk.yaml)</p>
              <pre class="code-block"># 1. 复制配置文件
sudo cp 99-opstk.yaml /etc/netplan/
# 2. 应用配置
sudo netplan apply
# 3. 验证
ip addr show</pre>

              <p class="lead-success mt-3">RHEL 8+ (nmcli → apply-network.sh)</p>
              <pre class="code-block"># 1. 加可执行权限
chmod +x apply-network.sh
# 2. 执行配置脚本（set -e：中途失败即停，不会改一半继续跑）
sudo bash apply-network.sh
# 3. 验证
nmcli device status</pre>

              <p class="lead-success mt-3">RHEL 家族 (ifcfg → ifcfg-files.txt)</p>
              <pre class="code-block"># 1. 把各 ifcfg-ethX / ifcfg-bond0 等文件复制到
sudo cp ifcfg-* /etc/sysconfig/network-scripts/
# 2. 重启网络
sudo systemctl restart NetworkManager   # 或 network 服务（按现场而定）</pre>
            </el-collapse-item>

          </el-collapse>
        </el-tab-pane>


        <el-tab-pane label="常见概念" name="concept">
          <h3>常见概念解释</h3>
          <el-collapse v-model="conceptActive">
            <el-collapse-item title="ProxyDHCP（代理 DHCP）" name="c1">
              <p>不分配 IP，只额外广播 PXE/ZTP 引导信息。机器同时收到两份回应：从原 DHCP 拿到 IP，从本工具拿到引导地址。适合「接入现有网络即装/开局」。是本工具「部署」确认弹窗的默认模式（影响面最小）。</p>
            </el-collapse-item>
            <el-collapse-item title="relay 中继模式" name="c2">
              <p>工具完全不跑 DHCP，只提供 TFTP+HTTP 文件服务，靠交换机的 ip helper-address 把请求中继过来。适合大规模、集中式部署。</p>
            </el-collapse-item>
            <el-collapse-item title="netplan renderer：networkd vs NetworkManager" name="c3">
              <p>物理网卡（eth0/ens*）推荐 networkd（服务器静态 IP 稳定）；无线/动态认证场景用 NetworkManager。该选项只在格式选了 netplan 时出现。</p>
            </el-collapse-item>
            <el-collapse-item title="bond 聚合模式" name="c4">
              <p>mode 0-6 七种：mode 1 (active-backup) 主备，最常用、无需交换机配置，可指定 primary 主接口；mode 4 (802.3ad/LACP) 需交换机两端同步配置，支持 lacp_rate 与 hash 策略；mode 2 (balance-xor) 按 hash 策略分流。参数输入框会随所选模式自动增减。</p>
            </el-collapse-item>
            <el-collapse-item title="凭据加密与密钥" name="c5">
              <p>所有设备密码/SSH 私钥/enable 密码以 Fernet 对称加密存储于数据库，密钥是 backend/.env 里的 credential_key（首次启动自动生成，跨进程加锁写入）。密钥与数据库相互独立：删库不会重生密钥（旧密文仍可解密）；.env 丢失则所有已加密凭据永远打不开 —— 所以两者必须一起备份。 credential_key 损坏时服务启动日志会给出可操作的长提示（恢复原密钥，不要重新生成）。</p>
            </el-collapse-item>
            <el-collapse-item title="TextFSM 与解析链" name="c6">
              <p>巡检输出的解析按顺序尝试：厂商专用解析（如 Comware 的 CPU/内存/温度精解析）→ ntc-templates 内置模板 → 自定义 TextFSM 模板 → 指标正则 → 原文摘要。解析状态有 ok / warning / critical / unknown / unsupported 五种，指标卡片按状态着色。</p>
            </el-collapse-item>
            <el-collapse-item title="候选命令与 unsupported" name="c7">
              <p>同一指标在不同机型的命令名可能不同。模板指标可配置候选命令：主命令被设备拒绝（Unrecognized / Too many parameters / % Invalid input，且判据严格防止误判）时自动尝试候选；全部被拒则标注「该机型不支持此指标」（unsupported），而不是含糊的“未解析”。</p>
            </el-collapse-item>
            <el-collapse-item title="落位与认领（ZTP）" name="c8">
              <p>落位 = 设备在机房的物理位置（机架/机柜/U位）+ 规划的主机名/管理 IP。设备到货时没有 MAC，先按落位登记（MAC 留空）；上电后 dnsmasq 租约里出现它的 MAC，「待认领设备」自动列出，点「认领到落位」即完成绑定。认领后重新生成并部署，设备就会按 MAC 拿到自己落位的配置；未认领的设备拿 default.cfg（管理口 DHCP 取址）。</p>
            </el-collapse-item>
            <el-collapse-item title="/pxe/serve 与 /ztp 的 token 门禁" name="c9">
              <p>这两个 HTTP 根是无认证的（装机/开局客户端不支持认证头），但内容含机密（口令哈希/明文口令）。可在 backend/.env 配置 pxe_serve_token / ztp_serve_token（≥16 位）启用 URL 门禁：/pxe/serve/&lt;token&gt;/ks.cfg 或 ?t=&lt;token&gt;；空 = 不启用。启用/更换后必须重新部署一次。TFTP 侧（iPXE 固件、H3C 的 ZTP 配置）不经 HTTP，不受此开关影响。</p>
            </el-collapse-item>
          </el-collapse>
        </el-tab-pane>
      </el-tabs>
    </CardSection>
  </div>
</template>

<script setup>
import { ref, reactive, computed } from "vue"
import { useRoute, useRouter } from "vue-router"
import { HELP_TOPICS } from "../router"
import PageHeader from "../components/PageHeader.vue"
import CardSection from "../components/CardSection.vue"

// 主题 tab 路由化（IA 收尾）：激活主题不再存本地 ref，由 /help/:topic 路由参数派生 ——
// 刷新 / 浏览器前进后退 / 深链都停在同一个主题。
// 点击 tab 走 set → router.push 改 URL，激活态随路由回到 get（模板里 v-model 原样保留）；
// :topic 非法时兜底显示第一个主题，URL 纠偏由 router/index.js 全局守卫重定向完成。
const route = useRoute()
const router = useRouter()
const activeTab = computed({
  get: () => (HELP_TOPICS[route.params.topic] ? route.params.topic : "beginner"),
  set: (name) => { if (HELP_TOPICS[name]) router.push("/help/" + name) },
})
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
  { name: "访问地址", path: "http://localhost:8000" },
]
const remotePaths = [
  { name: "IP 地址", path: "10.128.118.113" },
  { name: "登录账号", path: "yang / <口令> (sudo 免密)" },
  { name: "项目目录", path: "/opt/opstk/" },
  { name: "容器", path: "opstoolkit（docker compose 管理）" },
  { name: "数据库", path: "/opt/opstk/backend/data/ops.db" },
  { name: "PXE TFTP", path: "/srv/tftp/" },
  { name: "PXE HTTP", path: "/srv/opstk/pxe-web/" },
  { name: "ZTP HTTP", path: "/srv/opstk/ztp-web/" },
  { name: "ISO 存储", path: "/srv/opstk/iso/" },
  { name: "镜像", path: "opstk-opstoolkit:latest" },
  { name: "访问地址", path: "http://10.128.118.113:8000" },
]

const quickActive = ref("q4")
const runModes = [
  { mode: "本地 uvicorn（Windows/Linux）", for: "开发调试", pxe: "不可用（非 Linux）", note: "改代码热更新，数据库在 backend/data/" },
  { mode: "直接部署 Linux（systemd/venv）", for: "生产（性能最好）", pxe: "可用", note: "需 dnsmasq + iPXE + sudo 免密，见「部署指南」" },
  { mode: "Docker Compose（当前生产方式）", for: "生产（易迁移）", pxe: "可用", note: "host 网络 + privileged，挂载清单见下" },
]
const dailyOps = [
  { page: "登录", func: "JWT 认证", how: "admin（初始密码见服务日志；token 存本地，401 自动跳回登录页）" },
  { page: "仪表盘", func: "总览", how: "四张卡（CT 设备/IT 服务器/巡检模板/巡检任务）+ 近期巡检任务（可回放）+ 最近 PXE 装机记录 + 快捷入口" },
  { page: "资产管理", func: "设备台账", how: "新增资产分 CT/IT 两类；CT 填 IP/厂商/角色并关联凭据；IT 服务器只填基础信息" },
  { page: "凭据管理", func: "加密密码", how: "录入 SSH 用户名/密码/私钥/enable 密码（Fernet 加密），删除被引用的凭据会被拒绝并点名资产" },
  { page: "CT 巡检", func: "批量巡检", how: "多选设备 + 模板（或自定义命令），按设备分组实时输出，任务落库可下载 txt/json/csv、对比、回放" },
  { page: "CT 巡检", func: "模板管理", how: "系统模板只读（查看/克隆/导出），自定义模板可编辑/删除，支持 JSON 导入导出" },
  { page: "告警管理", func: "阈值告警", how: "建规则（cpu/memory/temperature + >、<、≥、≤ + 阈值）→ 下次巡检自动判定 → 告警记录页看触发条目" },
  { page: "网络配置生成", func: "生成脚本", how: "选 OS 与格式，表格配置物理口/Bond/VLAN/网桥，实时预览，下载与预览同内容" },
  { page: "PXE 装机", func: "一键装机", how: "放 ISO → 提取（选系统/版本）→ 建模板（目标盘三模式）→ 部署（默认 ProxyDHCP）→ 裸机接线开机" },
  { page: "ZTP 开局", func: "设备初始化", how: "建模板（H3C/华为VRP5/VRP8/思科）→ 落位登记/设备清单 → 生成配置 → 部署（DHCP 网卡必须真实）" },
  { page: "使用帮助", func: "在线手册", how: "9 个主题：小白手册/工具手册/CT 巡检/网络配置/PXE/ZTP/部署指南/生成器指南/常见概念" },
]
const dataPaths = [
  { path: "/opt/opstk/backend/data/ops.db", content: "SQLite 数据库 (资产/凭据/模板/任务)", backup: "停服后直接拷贝" },
  { path: "/opt/opstk/backend/.env", content: "JWT 密钥 + 凭据加密密钥 + PXE/ZTP token", backup: "必须一并备份" },
  { path: "/etc/dnsmasq.d/", content: "PXE/ZTP dnsmasq 配置 (opstk-pxe.conf / opstk-ztp.conf)", backup: "可由重新部署再生" },
  { path: "/srv/tftp/", content: "iPXE 固件 (ipxe.efi/undionly.kpxe)", backup: "可从系统包重装" },
  { path: "/srv/opstk/pxe-web/", content: "应答文件 + 内核文件", backup: "重要, tar 打包" },
  { path: "/srv/opstk/ztp-web/", content: "ZTP 设备配置 (明文口令, 勿外传)", backup: "可由重新生成再生" },
  { path: "/srv/opstk/iso/", content: "ISO 镜像文件", backup: "可重新下载" },
]

const netOverviewActive = ref("n2")
const netconfigActive = ref("nc1")
const pxeActive = ref("p3")
const inspectActive = ref("i2")
const inspectTimeouts = [
  { item: "TCP 预检超时", value: "3 秒", note: "连接前的端口探测；不通立即报「设备不可达」，不进入 SSH 流程" },
  { item: "连接超时", value: "10 秒", note: "SSH 建连阶段单独计时（与命令读取分开）" },
  { item: "命令读取超时", value: "60 秒", note: "发命令后等待回显的最长时间" },
  { item: "批量并发上限", value: "10 台", note: "同时连接的设备数上限，避免压垮网络" },
]
const netValidation = [
  { rule: "接口名", note: "字母/数字/._:-，1-32 位（sda、bond0、eth0.100 都合法）" },
  { rule: "主机名", note: "RFC1123 单标签：字母/数字/连字符，首尾不能是连字符" },
  { rule: "IP 与掩码", note: "IPv4 每段 ≤255；掩码必须是连续掩码（255.255.0.0 合法，255.0.255.0 拒绝）；前缀 0..32" },
  { rule: "网关", note: "必须是合法 IPv4 且与接口 IP 同子网（未写前缀按 /24 判）；DHCP 行填网关会被拒绝" },
  { rule: "mode=static", note: "必须给 IP（bond 无 mode 开关不受此限）；被引用的从接口不配地址不报错" },
  { rule: "从接口", note: "非空、不重复、逐个过接口名白名单；primary 必须是本 bond 的成员端口" },
  { rule: "VLAN", note: "VLAN ID 1..4094；父接口只能是已有物理口或 Bond" },
  { rule: "DNS", note: "每个元素必须是合法 IPv4；从接口/DHCP 未填 IP 的行禁用 DNS 输入" },
  { rule: "空配置", note: "物理口/Bond/VLAN/网桥全为空时拒绝生成（不会配置任何网络设备）" },
  { rule: "结构矛盾", note: "primary 不在从接口里、聚合互相成环、同一网卡被 bond 与 bridge 同时引用 → 拒绝" },
]
const pxeFields = [
  { field: "名称", required: "是", desc: "自定义模板名，方便区分" },
  { field: "系统类型", required: "是", desc: "按安装器家族分岔：kickstart 家族（rhel/centos/rocky/almalinux/oraclelinux/openeuler/kylin/uos/anolis/fedora）生成 Kickstart；ubuntu 生成 autoinstall；debian/openSUSE 保存允许但生成时明确拒绝（暂不支持自动装机，仅识别与提取）" },
  { field: "系统版本", required: "是", desc: "需与 ISO 提取目录一致（ubuntu/22.04），决定内核/应答文件 URL" },
  { field: "管理员", required: "是", desc: "安装后的管理用户名（字母/数字/下划线，不以 - 开头），已加入 sudo" },
  { field: "管理员密码", required: "新建必填", desc: "加密存储，生成 shadow 哈希；编辑时留空 = 不修改；绝不代填默认口令" },
  { field: "root 密码", required: "否", desc: "仅 kickstart 家族（RHEL 系/RHEL 克隆/openEuler/麒麟/UOS 等）显示；生成 rootpw --iscrypted 哈希" },
  { field: "时区/语言/键盘", required: "否", desc: "默认 Asia/Shanghai / en_US.UTF-8 / us" },
  { field: "SSH 公钥", required: "否", desc: "每行一个公钥，写入 authorized_keys 免密登录" },
  { field: "分区方案", required: "是", desc: "LVM（推荐）/ 直通分区 / 自定义分区表（ZFS 暂未支持：后端未实现，选项已移除）" },
  { field: "目标磁盘", required: "是", desc: "自动（最大盘，仅 RHEL 系）/ 按盘名 / 按序列号或型号" },
  { field: "最小容量 (GB)", required: "否", desc: "自动选盘时过滤小盘（如排除装了引导的 U 盘）" },
  { field: "清空目标盘", required: "否", desc: "默认开：只清空目标盘，其它盘一律不碰" },
  { field: "自定义分区表", required: "custom", desc: "逐行定义挂载点/大小/fstype/VG+LV；必须含 / 分区；rest 只能最后一行" },
  { field: "其它数据盘", required: "否", desc: "默认不动数据；要挂载须二选一：格式化新建，或填已有文件系统 UUID/卷标（仅 RHEL 系）" },
  { field: "RAID", required: "custom", desc: "仅自定义分区表；级别 0/1/5/6/10，成员填分区序号（如 3,4）" },
  { field: "网络", required: "否", desc: "DHCP 或静态（网卡名/IP/掩码/网关/DNS）" },
  { field: "镜像源", required: "否", desc: "apt/yum 源，留空用默认；RHEL 系给本机 repo 时会自动探测 stage2 与 AppStream" },
  { field: "额外软件 / 安装后脚本", required: "否", desc: "追加包（逗号分隔）；装完执行的 shell 脚本" },
]
const pxeFiles = [
  { file: "user-data", role: "Ubuntu 应答", desc: "autoinstall: 账号/目标盘/网络/软件包/late-commands" },
  { file: "ks.cfg", role: "RHEL 应答", desc: "Kickstart：ignoredisk 钉死目标盘 + clearpart + 分区/账号/网络" },
  { file: "meta-data", role: "cloud-init", desc: "主机名、实例 ID 等元数据" },
  { file: "boot.ipxe", role: "iPXE 菜单", desc: "告诉 iPXE 去哪里下载内核、传什么内核参数（含 console=）" },
  { file: "dnsmasq.conf", role: "网络服务", desc: "DHCP + TFTP 配置，三种部署模式各不相同" },
  { file: "vmlinuz / initrd", role: "内核", desc: "从 ISO 提取，经 HTTP 下载到内存" },
  { file: "installer.squashfs", role: "安装器文件系统", desc: "Ubuntu live 介质必须项，casper 按 iso_url 取" },
]
const ztpFiles = [
  { file: "ztp/default.cfg", vendor: "通用", desc: "未登记设备统一拿的基础配置（管理口 DHCP 取址，避免多台设备撞同一 IP）" },
  { file: "ztp/<序列号或主机名>.cfg", vendor: "H3C/华为", desc: "已登记（设备清单或已认领落位）设备的专属配置，按 MAC 匹配下发" },
  { file: "ztp_intermediate.txt", vendor: "华为", desc: "中间文件，描述需下载的文件列表（华为 ZTP 机制）" },
  { file: "ztp_bootstrap.py", vendor: "思科", desc: "Python 脚本，负责拉取并应用对应 .cfg（IOS-XE ZTP）" },
  { file: "ztp_note.txt", vendor: "H3C", desc: "说明文件（H3C 走 auto-config，直接 TFTP 取 .cfg，无需脚本）" },
  { file: "dnsmasq.conf", vendor: "通用", desc: "Option 66/67（H3C/华为）或 150+67（思科）+ 按 MAC 的 tag 匹配 + DHCP 池" },
  { file: "README.txt", vendor: "通用", desc: "部署说明：文件放哪、dnsmasq 怎么配、登记/认领行为、VRP8 实测差异提醒" },
]
const troubleActive = ref("t1")
const deployReqs = [
  { item: "操作系统", req: "Linux (Rocky/RHEL 9 / Ubuntu 22.04)", note: "本机部署 PXE/ZTP 必需" },
  { item: "Python", req: "Python 3 + venv（容器自带）", note: "依赖见 requirements.txt" },
  { item: "dnsmasq", req: "已安装", note: "DHCP+TFTP 服务（PXE/ZTP 共用）" },
  { item: "iPXE 固件", req: "ipxe / ipxe-qemu 包", note: "ipxe.efi + undionly.kpxe" },
  { item: "sudo 免密", req: "NOPASSWD 白名单", note: "管控 dnsmasq/挂载 ISO/写配置" },
  { item: "端口", req: "8000 (Web) + 67/udp (DHCP) + 69/udp (TFTP)", note: "确保未被占用" },
]
const composeConfig = [
  { key: "network_mode: host", why: "PXE 需要广播 DHCP 数据包，必须用宿主机网络，不得用 bridge" },
  { key: "privileged: true", why: "挂载 ISO 需要访问 /dev/loop 设备" },
  { key: "restart: unless-stopped", why: "服务崩溃或重启后自动恢复" },
]
const composeMounts = [
  { vol: "./backend/.env", why: "JWT/凭据密钥 —— 不挂的话重建容器会丢密钥，加密凭据永久打不开" },
  { vol: "./data → /app/backend/data", why: "SQLite 数据库，重建容器不丢数据" },
  { vol: "./frontend/dist、./backend/app", why: "改代码后 restart 即生效，不必重建镜像" },
  { vol: "/srv/opstk/ztp-web", why: "ZTP 设备配置落盘；不挂则 /ztp 静态服务不会注册、容器重建即丢" },
  { vol: "/var/lib/dnsmasq (只读)", why: "dnsmasq 租约文件 —— ZTP 落位认领靠它学到上电设备的 MAC" },
  { vol: "/srv/opstk/state", why: "宿主机 dnsmasq 重载握手状态，容器靠它确认配置真的生效" },
  { vol: "/etc/dnsmasq.d、/srv/tftp、/srv/opstk/iso、/srv/opstk/mnt", why: "dnsmasq 配置 / 固件 / 镜像 / 挂载点" },
]
const volumeConfig = [
  { vol: "./data", path: "/app/backend/data", desc: "SQLite 数据库 (资产/凭据/模板/任务)" },
  { vol: "./backend/.env", path: "/app/backend/.env", desc: "密钥文件（必须挂，重建容器不丢密钥）" },
  { vol: "/srv/tftp", path: "/srv/tftp", desc: "iPXE 固件 (ipxe.efi/undionly.kpxe)" },
  { vol: "/srv/opstk/pxe-web", path: "/srv/opstk/pxe-web", desc: "应答文件 + 内核 (vmlinuz/initrd/squashfs)" },
  { vol: "/srv/opstk/ztp-web", path: "/srv/opstk/ztp-web", desc: "ZTP 设备配置（/ztp 静态服务的数据源）" },
  { vol: "/srv/opstk/iso", path: "/srv/opstk/iso", desc: "ISO 镜像文件" },
  { vol: "/var/lib/dnsmasq", path: "/var/lib/dnsmasq (只读)", desc: "dnsmasq 租约 —— ZTP 落位认领的 MAC 来源" },
]

const netconfigRows = [
  { item: "物理接口", desc: "单口静态/DHCP 配置，DHCP 行也可配 DNS" },
  { item: "链路聚合 Bond", desc: "mode 0-6（balance-rr/active-backup/…/802.3ad），从接口逗号分隔；参数随 mode 变化（lacp 速率/hash 策略/主接口）" },
  { item: "VLAN 子接口", desc: "基于物理口或 Bond 划分 VLAN 子接口（父接口.VLAN号），可配 IP/网关/DNS" },
  { item: "网桥 Bridge", desc: "多接口打成二层网桥；有 IP 即静态、无 IP 即纯二层，适合虚拟化/KVM" },
]

const pxeModes = [
  { mode: "standalone", dhcp: "完整分配 IP + PXE 引导", scene: "专用装机 VLAN，网段内无其他 DHCP" },
  { mode: "proxy", dhcp: "不分配 IP，只广播引导信息", scene: "接入现有网络即装，不破坏现有 DHCP（部署默认值）" },
  { mode: "relay", dhcp: "不跑 DHCP，仅 TFTP", scene: "大规模集中式，交换机 ip-helper 中继" },
]

const ztpOptions = [
  { vendor: "H3C (Comware 7)", opt: "option 66 (TFTP) + 67 (文件名)", mech: "auto-config，空配置启动时自动拉取 ztp/<主机名或序列号>.cfg" },
  { vendor: "华为 VRP8 (CE/NE)", opt: "option 66 (TFTP) + 67 (中间文件)", mech: "ZTP，先取中间文件再按指引下载配置（真机验证）" },
  { vendor: "华为 VRP5", opt: "option 66 + 67 (中间文件)", mech: "同上，命令按 VRP5 语法生成（产物标注未真机验证）" },
  { vendor: "思科 IOS-XE", opt: "option 150 (TFTP) + 67 (脚本名)", mech: "ZTP，下载 Python 脚本再拉取配置" },
]
</script>

<style scoped>
.help-page h3 { margin: 0 0 16px; }
.help-page p { line-height: 1.8; color: var(--ot-text-2); }
.help-page ol,
.help-page ul { line-height: 2; padding-left: 20px; }
.help-page li { margin-bottom: 4px; }

.help-page pre.code-block {
  background: var(--ot-bg-code);
  color: var(--ot-code-fg);
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 13px;
  line-height: 1.6;
  overflow-x: auto;
  white-space: pre;
  font-family: 'Cascadia Code', 'Fira Code', Consolas, monospace;
}

/* ── 本页内联样式收编（间距/强调色取 tokens 语义名） ── */
.help-tabs { min-height: 520px; }
.mt-1 { margin-top: var(--ot-space-1); }
.mt-2 { margin-top: var(--ot-space-2); }
.mt-3 { margin-top: var(--ot-space-3); }
.mb-2 { margin-bottom: var(--ot-space-2); }
.mv-2 { margin: var(--ot-space-2) 0; }
.mv-3 { margin: var(--ot-space-3) 0; }
.tight-p { margin: var(--ot-space-1) 0; }
.muted-sm { color: var(--ot-text-3); font-size: var(--ot-font-sm); }
.lead-primary { font-weight: 600; color: var(--ot-primary); }
.lead-success { font-weight: 600; color: var(--ot-success); }
.indent-ol { margin: var(--ot-space-1) 0; margin-left: 20px; }
</style>
