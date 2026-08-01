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
        <!-- ===== 快速开始 ===== -->
        <el-tab-pane label="快速开始" name="quick">
          <h3>快速上手</h3>
          <el-steps direction="vertical" :active="5">
            <el-step title="登录" description="默认账号 admin / admin@123，首次登录后请尽快修改密码。" />
            <el-step title="录入资产" description="在「资产管理」添加设备/服务器，填写 IP、厂商、设备类型。" />
            <el-step title="绱定凭据" description="在「凭据管理」添加登录账号密码（加密存储），回到资产里关联。" />
            <el-step title="开始使用" description="根据需求进入对应功能页面：巡检 / 网络配置 / PXE 装机 / ZTP 开局。" />
            <el-step title="下载结果" description="配置生成后点「下载 ZIP」获取全部文件，拷贝到目标机器执行。" />
          </el-steps>
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
          <p>面向 Ubuntu 22.04+ 和 RHEL/Rocky/Alma 8+，生成可直接执行的网络配置脚本。</p>
          <el-table :data="netconfigRows" border size="small" style="margin:12px 0">
            <el-table-column prop="item" label="配置项" width="140" />
            <el-table-column prop="desc" label="说明" />
          </el-table>
          <el-alert type="success" :closable="false" title="renderer 怎么选" description="物理网卡静态 IP → networkd（服务器首选，稳定）；无线/4G/5G 动态认证 → NetworkManager。混搭策略：物理网卡交 networkd，无线交 NetworkManager。" style="margin:12px 0" />
          <ol>
            <li>选择系统（Ubuntu / RHEL）和格式（nmcli 或 netplan）。</li>
            <li>添加物理接口、聚合(bond)、VLAN、网桥(bridge)，填写 IP/网关/DNS。</li>
            <li>点「生成配置」，右侧实时预览脚本。</li>
            <li>点「下载」获取 .sh 或 .yaml 文件，拷贝到服务器执行。</li>
          </ol>
        </el-tab-pane>

        <!-- ===== PXE ===== -->
        <el-tab-pane label="PXE 装机" name="pxe">
          <h3>PXE 装机</h3>
          <p>全自动装 Ubuntu / RHEL 系统。OpsToolkit 本机可直接作为完整 PXE 服务器，点「部署」即可启动全链路，裸机接线即装。</p>

          <el-alert type="success" :closable="false" title="本机完整 PXE 服务器模式" description="点「部署」后，本工具自动完成：检测网卡/IP/网关 → 生成 dnsmasq 配置 → 落地应答文件 → 复制 iPXE 固件 → 修复 SELinux 上下文 → 重启 dnsmasq。全部自动，无需手动编辑任何配置文件。" style="margin:12px 0" />

          <el-alert type="warning" :closable="false" title="环境要求：本机部署需 Linux" description="DHCP/TFTP 是系统服务，需要 Linux 环境 (Rocky/RHEL/CentOS/Ubuntu) + sudo 免密权限。Windows 上仅支持「生成配置 + 下载 ZIP」，无法直接运行 PXE 服务。" style="margin:12px 0" />

          <h4 style="margin:16px 0 8px">三种部署模式</h4>
          <el-table :data="pxeModes" border size="small" style="margin:8px 0">
            <el-table-column prop="mode" label="模式" width="130" />
            <el-table-column prop="dhcp" label="DHCP 行为" width="220" />
            <el-table-column prop="scene" label="适用场景" />
          </el-table>

          <h4 style="margin:16px 0 8px">使用步骤</h4>
          <ol>
            <li>【提供内核】将 OS 安装 ISO 上传到服务器 <code>/srv/opstk/iso/</code> 目录，然后在「ISO 镜像管理」面板点「提取」。系统会自动挂载 ISO 并提取 vmlinuz、initrd、squashfs 到 HTTP 目录。</li>
            <li>【创建模板】在「PXE 装机」页面新建装机模板，填写系统类型/版本、磁盘分区、管理账号、网络等。</li>
            <li>【一键部署】点模板旁的「部署」按钮。系统自动检测本机网卡和网络，生成全套配置，落地文件，重启 dnsmasq。部署日志实时显示在下方。</li>
            <li>【裸机开装】裸机接上网线（与服务器同一网段），BIOS/UEFI 设为 PXE 网络启动即可自动安装。</li>
            <li>【独立部署】也可点「生成配置」或「下载 ZIP」，在别的 Linux 服务器上手动部署。</li>
          </ol>

          <el-alert type="info" :closable="false" title="PXE 引导全链路" description="裸机 → DHCP 分配 IP → TFTP 下载 iPXE 固件 → HTTP 下载 Linux 内核(vmlinuz+initrd) → 加载 squashfs → autoinstall/Kickstart 自动安装。全程无人值守。" style="margin:12px 0" />
        </el-tab-pane>

        <!-- ===== ZTP ===== -->
        <el-tab-pane label="ZTP 开局" name="ztp">
          <h3>ZTP 配置开局</h3>
          <p>为网络/安全设备（H3C、华为、思科）生成开局配置，设备首次上电空配置时自动拉取。</p>
          <el-table :data="ztpOptions" border size="small" style="margin:12px 0">
            <el-table-column prop="vendor" label="厂商" width="90" />
            <el-table-column prop="opt" label="DHCP Option" width="180" />
            <el-table-column prop="mech" label="工作机制" />
          </el-table>
          <ol>
            <li>「ZTP 开局」页面新建模板，选择厂商，填写管理 VLAN、网关、账号、端口等。</li>
            <li>添加设备清单（MAC + 主机名），每台设备会生成单独的 .cfg。</li>
            <li>点「生成配置」，选择投递模式（同样支持 standalone/proxy/relay）。</li>
            <li>点「下载 ZIP」，把 .cfg 放入 TFTP 的 ztp/ 目录，部署 dnsmasq。</li>
            <li>设备上电后空配置启动，自动向 DHCP 请求并下载配置文件。</li>
          </ol>
        </el-tab-pane>

        <!-- ===== 概念 ===== -->
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

const activeTab = ref("quick")
const conceptActive = ref("c1")

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
</style>
