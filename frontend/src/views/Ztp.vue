<template>
  <div>
    <!-- ZTP 服务器状态 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
        <span style="font-weight: 600">
          <el-icon><Cpu /></el-icon> ZTP 服务器（本机）
          <el-tag v-if="serverStatus.dnsmasq" :type="serverStatus.dnsmasq.active ? 'success' : 'danger'" size="small" style="margin-left: 8px">
            dnsmasq {{ serverStatus.dnsmasq.active ? '运行中' : '未运行' }}
          </el-tag>
        </span>
        <div>
          <el-button size="small" @click="controlService('start')" :disabled="serverStatus.supported === false">启动</el-button>
          <el-button size="small" @click="controlService('stop')" :disabled="serverStatus.supported === false">停止</el-button>
          <el-button size="small" @click="controlService('restart')" :disabled="serverStatus.supported === false">重启 dnsmasq</el-button>
          <el-button size="small" @click="loadServerStatus"><el-icon><Refresh /></el-icon> 刷新</el-button>
        </div>
      </div>
      <el-descriptions v-if="serverStatus.supported" :column="3" size="small" border>
        <el-descriptions-item label="小工具目录">{{ serverStatus.sudo_ok ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="TFTP">{{ serverStatus.dirs && serverStatus.dirs.tftp ? '已创建' : '未创建' }}</el-descriptions-item>
        <el-descriptions-item label="HTTP">{{ serverStatus.dirs && serverStatus.dirs.web ? '已创建' : '未创建' }}</el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="serverStatus.supported === false" type="warning" :closable="false" style="margin-top: 8px">
        本机部署需 Linux 环境，当前：{{ serverStatus.platform }}
      </el-alert>
    </el-card>

    <!-- 模板列表 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; margin-bottom: 12px">
        <span style="font-weight: 600"><el-icon><Connection /></el-icon> ZTP 开局模板</span>
        <el-button type="primary" @click="openTemplateDialog()"><el-icon><Plus /></el-icon> 新建开局模板</el-button>
      </div>
      <el-table :data="templates" stripe size="small">
        <el-table-column prop="name" label="模板名称" min-width="130" />
        <el-table-column label="厂商" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="vendorType(row.vendor)">{{ vendorLabel(row.vendor) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="mgmt_vlan" label="管理VLAN" width="90" />
        <el-table-column prop="mgmt_gateway" label="网关" width="120" />
        <el-table-column prop="server_ip" label="ZTP服务器" width="120" />
        <el-table-column label="投递模式" width="110">
          <template #default="{ row }">{{ modeLabel(row.deploy_mode) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openGenDialog(row)">生成配置</el-button>
            <el-button link type="primary" size="small" @click="openTemplateDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delTemplate(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 设备清单 -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600"><el-icon><Monitor /></el-icon> ZTP 设备清单</span>
          <el-button type="primary" size="small" @click="deviceDialog = true"><el-icon><Plus /></el-icon> 添加设备</el-button>
        </div>
      </template>
      <el-table :data="devices" stripe size="small">
        <el-table-column prop="hostname" label="主机名" min-width="120" />
        <el-table-column prop="mac" label="MAC 地址" width="160" />
        <el-table-column prop="serial" label="序列号" width="140" />
        <el-table-column prop="mgmt_ip" label="管理 IP" width="120" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-popconfirm title="确定删除?" @confirm="delDevice(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 模板编辑弹窗 -->
    <el-dialog v-model="templateDialog" :title="editingId ? '编辑开局模板' : '新建开局模板'" width="820px" :close-on-click-modal="false">
      <el-form :model="form" label-width="100px" size="default">
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="模板名"><el-input v-model="form.name" /></el-form-item></el-col>
          <el-col :span="8">
            <el-form-item label="厂商">
              <el-select v-model="form.vendor" @change="onVendorChange">
                <el-option label="H3C (推荐)" value="h3c" />
                <el-option label="华为 (推荐)" value="huawei" />
                <el-option label="思科" value="cisco" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="域名"><el-input v-model="form.domain_name" placeholder="可选" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">管理网络</el-divider>
        <el-row :gutter="12">
          <el-col :span="6"><el-form-item label="管理VLAN"><el-input-number v-model="form.mgmt_vlan" :min="1" :max="4094" style="width:100%" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="管理SVI"><el-input v-model="form.mgmt_interface" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="掩码"><el-input v-model="form.mgmt_netmask" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="网关"><el-input v-model="form.mgmt_gateway" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DNS"><el-input v-model="form.dns_servers" placeholder="逗号分隔" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="NTP"><el-input v-model="form.ntp_server" /></el-form-item></el-col>
        </el-row>
        <el-form-item label="VLAN规划">
          <el-input v-model="form.vlans_text" type="textarea" :rows="2" placeholder="每行: VLAN号,名称" />
        </el-form-item>

        <el-divider content-position="left">账号与安全</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="管理员"><el-input v-model="form.admin_user" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="密码"><el-input v-model="form.admin_password" type="password" show-password placeholder="留空不修改" /></el-form-item></el-col>
          <el-col :span="8" v-if="form.vendor === 'cisco'"><el-form-item label="Enable密钥"><el-input v-model="form.enable_secret" type="password" show-password /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="SNMP团体"><el-input v-model="form.snmp_community" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">端口与自定义</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="上联口"><el-input v-model="form.uplink_port" placeholder="GigabitEthernet1/0/48" /></el-form-item></el-col>
          <el-col :span="16"><el-form-item label="接入口"><el-input v-model="form.access_ports" placeholder="逗号分隔" /></el-form-item></el-col>
        </el-row>
        <el-form-item label="自定义配置"><el-input v-model="form.extra_config" type="textarea" :rows="3" placeholder="追加的厂商 CLI (可选)" /></el-form-item>

        <el-divider content-position="left">ZTP 投递</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="投递模式">
              <el-select v-model="form.deploy_mode">
                <el-option label="独立DHCP" value="standalone" />
                <el-option label="ProxyDHCP" value="proxy" />
                <el-option label="中继模式" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="服务器IP"><el-input v-model="form.server_ip" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DHCP网卡"><el-input v-model="form.dhcp_iface" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="DHCP起始"><el-input v-model="form.dhcp_start" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DHCP结束"><el-input v-model="form.dhcp_end" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="HTTP根"><el-input v-model="form.http_root" /></el-form-item></el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="templateDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">保存模板</el-button>
      </template>
    </el-dialog>

    <!-- 设备添加弹窗 -->
    <el-dialog v-model="deviceDialog" title="添加 ZTP 设备" width="560px">
      <el-form :model="devForm" label-width="80px" size="default">
        <el-form-item label="关联模板">
          <el-select v-model="devForm.template_id" filterable placeholder="选择模板" style="width:100%">
            <el-option v-for="t in templates" :key="t.id" :label="t.name + ' (' + vendorLabel(t.vendor) + ')'" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="主机名"><el-input v-model="devForm.hostname" placeholder="Core-SW01" /></el-form-item>
        <el-form-item label="MAC地址"><el-input v-model="devForm.mac" placeholder="aa:bb:cc:dd:ee:ff" /></el-form-item>
        <el-form-item label="序列号"><el-input v-model="devForm.serial" placeholder="可选, 用于文件命名" /></el-form-item>
        <el-form-item label="管理IP"><el-input v-model="devForm.mgmt_ip" placeholder="可选" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deviceDialog = false">取消</el-button>
        <el-button type="primary" @click="addDevice">添加</el-button>
      </template>
    </el-dialog>

    <!-- 生成弹窗 -->
    <el-dialog v-model="genDialog" title="ZTP 部署文件生成" width="900px" top="5vh">
      <el-form label-width="90px" size="small" style="margin-bottom: 12px">
        <el-row :gutter="8">
          <el-col :span="8">
            <el-form-item label="投递模式">
              <el-select v-model="genForm.deploy_mode" style="width:100%">
                <el-option label="独立DHCP (专用开局网络)" value="standalone" />
                <el-option label="ProxyDHCP (与现有DHCP并存)" value="proxy" />
                <el-option label="中继模式 (仅TFTP)" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="服务器IP"><el-input v-model="genForm.server_ip" /></el-form-item></el-col>
          <el-col :span="8" style="text-align:right">
            <el-button type="primary" size="small" @click="doGenerate" :loading="generating"><el-icon><Check /></el-icon> 生成文件</el-button>
          <el-button type="success" size="small" @click="doDownload" :disabled="!Object.keys(genFiles).length"><el-icon><Download /></el-icon> 下载 ZIP</el-button>
          <el-button type="warning" size="small" @click="doDeploy" :loading="deploying" :disabled="!Object.keys(genFiles).length"><el-icon><Promotion /></el-icon> 部署到本机</el-button>
          </el-col>
        </el-row>
        <el-form-item label="临时设备" v-if="genForm.devices.length">
          <el-tag v-for="(d, i) in genForm.devices" :key="i" closable @close="genForm.devices.splice(i,1)" size="small" style="margin-right:6px">
            {{ d.hostname }} / {{ d.mac || '无MAC' }}
          </el-tag>
        </el-form-item>
      </el-form>
      <div style="margin-bottom: 8px">
        <el-button size="small" @click="addInlineDevice"><el-icon><Plus /></el-icon> 添加临时设备</el-button>
      </div>
      <el-tabs v-model="activeFile" v-if="Object.keys(genFiles).length">
        <el-tab-pane v-for="(_, name) in genFiles" :key="name" :label="name" :name="name">
          <div class="terminal-output" style="white-space: pre; max-height: 440px">{{ genFiles[name] }}</div>
        </el-tab-pane>
      </el-tabs>
      <el-alert v-if="deployResult.length" :type="deployOk ? 'success' : 'error'" :closable="false" style="margin-top: 12px">
        <div v-for="(ln, i) in deployResult" :key="i" style="font-family: monospace; font-size: 12px; white-space: pre-wrap">{{ ln }}</div>
      </el-alert>
    </el-dialog>

    <!-- 临时设备添加 -->
    <el-dialog v-model="inlineDevDialog" title="添加临时设备" width="480px" append-to-body>
      <el-form :model="inlineDev" label-width="80px" size="small">
        <el-form-item label="主机名"><el-input v-model="inlineDev.hostname" /></el-form-item>
        <el-form-item label="MAC"><el-input v-model="inlineDev.mac" placeholder="aa:bb:cc:00:00:01" /></el-form-item>
        <el-form-item label="序列号"><el-input v-model="inlineDev.serial" /></el-form-item>
        <el-form-item label="管理IP"><el-input v-model="inlineDev.mgmt_ip" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="inlineDevDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmInlineDevice">添加</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from "vue"
import http, { downloadZip } from "../api"
import { ElMessage } from "element-plus"

const serverStatus = ref({})
const templates = ref([])
const devices = ref([])
const templateDialog = ref(false)
const deviceDialog = ref(false)
const editingId = ref(null)
const saving = ref(false)
const genDialog = ref(false)
const generating = ref(false)
const genFiles = ref({})
const activeFile = ref("")
const deploying = ref(false)
const deployResult = ref([])
const deployOk = ref(true)
const inlineDevDialog = ref(false)

const emptyForm = () => ({
  name: "", vendor: "h3c", domain_name: "",
  mgmt_vlan: 10, mgmt_interface: "Vlan-interface10", mgmt_netmask: "255.255.255.0",
  mgmt_gateway: "10.0.0.254", dns_servers: "114.114.114.114", ntp_server: "10.0.0.254",
  vlans_text: "",
  admin_user: "admin", admin_password: "", enable_secret: "", snmp_community: "public",
  uplink_port: "", access_ports: "", extra_config: "",
  deploy_mode: "standalone", server_ip: "10.0.0.250", dhcp_iface: "eth0",
  dhcp_start: "10.0.0.100", dhcp_end: "10.0.0.200", http_root: "http://10.0.0.250:8000/ztp",
})
const form = reactive(emptyForm())

const devForm = reactive({ template_id: "", hostname: "", mac: "", serial: "", mgmt_ip: "" })
const inlineDev = reactive({ hostname: "", mac: "", serial: "", mgmt_ip: "" })

const genForm = reactive({ deploy_mode: "standalone", server_ip: "10.0.0.250", devices: [] })

function vendorLabel(v) { return { h3c: "H3C", huawei: "华为", cisco: "思科" }[v] || v }
function vendorType(v) { return { h3c: "primary", huawei: "success", cisco: "warning" }[v] || "info" }
function modeLabel(m) { return { standalone: "独立DHCP", proxy: "ProxyDHCP", relay: "中继" }[m] || m }

function onVendorChange() {
  if (form.vendor === "huawei") form.mgmt_interface = form.mgmt_interface.replace("Vlan-interface", "Vlanif")
  else if (form.vendor === "h3c") form.mgmt_interface = form.mgmt_interface.replace("Vlanif", "Vlan-interface")
}

function buildPayload() {
  const vlans = form.vlans_text.split("\n").map(line => {
    const parts = line.split(",").map(s => s.trim())
    if (!parts[0]) return null
    return { id: parseInt(parts[0]) || 0, name: parts[1] || "" }
  }).filter(Boolean)
  return {
    name: form.name, vendor: form.vendor, domain_name: form.domain_name,
    mgmt_vlan: form.mgmt_vlan, mgmt_interface: form.mgmt_interface, mgmt_netmask: form.mgmt_netmask,
    mgmt_gateway: form.mgmt_gateway,
    dns_servers: form.dns_servers.split(",").map(d => d.trim()).filter(Boolean),
    ntp_server: form.ntp_server, vlans,
    admin_user: form.admin_user,
    admin_password: form.admin_password || null,
    enable_secret: form.enable_secret || null,
    snmp_community: form.snmp_community,
    uplink_port: form.uplink_port,
    access_ports: form.access_ports.split(",").map(p => p.trim()).filter(Boolean),
    extra_config: form.extra_config,
    deploy_mode: form.deploy_mode, server_ip: form.server_ip,
    dhcp_iface: form.dhcp_iface, dhcp_start: form.dhcp_start, dhcp_end: form.dhcp_end,
    http_root: form.http_root,
  }
}

function fillForm(t) {
  Object.assign(form, emptyForm())
  form.name = t.name; form.vendor = t.vendor; form.domain_name = t.domain_name || ""
  form.mgmt_vlan = t.mgmt_vlan; form.mgmt_interface = t.mgmt_interface; form.mgmt_netmask = t.mgmt_netmask
  form.mgmt_gateway = t.mgmt_gateway
  form.dns_servers = (t.dns_servers || []).join(",")
  form.ntp_server = t.ntp_server
  form.vlans_text = (t.vlans || []).map(v => v.id + "," + (v.name || "")).join("\n")
  form.admin_user = t.admin_user; form.snmp_community = t.snmp_community
  form.uplink_port = t.uplink_port || ""
  form.access_ports = (t.access_ports || []).join(",")
  form.extra_config = t.extra_config || ""
  form.deploy_mode = t.deploy_mode; form.server_ip = t.server_ip
  form.dhcp_iface = t.dhcp_iface; form.dhcp_start = t.dhcp_start; form.dhcp_end = t.dhcp_end
  form.http_root = t.http_root
}

function openTemplateDialog(row) {
  Object.assign(form, emptyForm())
  editingId.value = null
  if (row) { fillForm(row); editingId.value = row.id }
  templateDialog.value = true
}

async function saveTemplate() {
  if (!form.name) { ElMessage.warning("请输入模板名"); return }
  saving.value = true
  try {
    const payload = buildPayload()
    if (editingId.value) await http.put("/ct/ztp/templates/" + editingId.value, payload)
    else await http.post("/ct/ztp/templates", payload)
    ElMessage.success("保存成功")
    templateDialog.value = false
    loadTemplates()
  } finally { saving.value = false }
}

async function delTemplate(id) {
  await http.delete("/ct/ztp/templates/" + id)
  ElMessage.success("已删除")
  loadTemplates()
}

async function addDevice() {
  if (!devForm.template_id) { ElMessage.warning("请选择模板"); return }
  await http.post("/ct/ztp/devices", { ...devForm })
  ElMessage.success("设备已添加")
  deviceDialog.value = false
  Object.assign(devForm, { template_id: "", hostname: "", mac: "", serial: "", mgmt_ip: "" })
  loadDevices()
}

async function delDevice(id) {
  await http.delete("/ct/ztp/devices/" + id)
  ElMessage.success("已删除")
  loadDevices()
}

function openGenDialog(row) {
  genForm.deploy_mode = row.deploy_mode || "standalone"
  genForm.server_ip = row.server_ip || "10.0.0.250"
  genForm.devices = []
  genDialog.value = true
  genFiles.value = {}
  sessionStorage.setItem("ztp_template_id", row.id)
  doGenerate()
}

function addInlineDevice() {
  Object.assign(inlineDev, { hostname: "", mac: "", serial: "", mgmt_ip: "" })
  inlineDevDialog.value = true
}

function confirmInlineDevice() {
  genForm.devices.push({ ...inlineDev })
  inlineDevDialog.value = false
}

async function doGenerate() {
  generating.value = true
  try {
    const tid = sessionStorage.getItem("ztp_template_id")
    const body = {
      deploy_mode: genForm.deploy_mode,
      server_ip: genForm.server_ip,
      devices: genForm.devices,
    }
    const res = await http.post("/ct/ztp/templates/" + tid + "/generate", body)
    genFiles.value = res.files
    const keys = Object.keys(res.files)
    if (keys.length) activeFile.value = keys[0]
    ElMessage.success("生成完成: " + keys.length + " 个文件")
  } finally { generating.value = false }
}

async function doDownload() {
  const tid = sessionStorage.getItem("ztp_template_id")
  await downloadZip("/ct/ztp/templates/" + tid + "/download", {
    deploy_mode: genForm.deploy_mode,
    server_ip: genForm.server_ip,
    devices: genForm.devices,
  })
  ElMessage.success("下载已开始")
}

async function doDeploy() {
  deploying.value = true
  deployResult.value = []
  deployOk.value = true
  try {
    const tid = sessionStorage.getItem("ztp_template_id")
    const res = await http.post("/ct/ztp/templates/" + tid + "/deploy", {
      deploy_mode: genForm.deploy_mode,
      server_ip: genForm.server_ip,
      devices: genForm.devices,
    })
    deployResult.value = res.log || []
    deployOk.value = !!res.ok
    if (res.ok) ElMessage.success("部署成功")
    else ElMessage.error("部署失败，请查看日志")
  } catch (e) {
    deployOk.value = false
    deployResult.value = [e.message || "部署请求失败"]
    ElMessage.error("部署请求失败")
  } finally {
    deploying.value = false
  }
}

async function loadTemplates() { templates.value = await http.get("/ct/ztp/templates") }
async function loadDevices() { devices.value = await http.get("/ct/ztp/devices") }

async function loadServerStatus() {
  try { serverStatus.value = await http.get("/ct/ztp/server/status") } catch (e) {}
}

async function controlService(action) {
  try {
    const res = await http.post("/ct/ztp/server/service", { action })
    if (res.log) res.log.forEach(l => ElMessage.info(l))
    ElMessage.success(action + " 已执行")
    loadServerStatus()
  } catch (e) {
    ElMessage.error("服务控制失败")
  }
}

onMounted(() => { loadTemplates(); loadDevices(); loadServerStatus() })
</script>
