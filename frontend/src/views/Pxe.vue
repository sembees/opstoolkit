<template>
  <div>
    <!-- PXE 服务器本机状态 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
        <span style="font-weight: 600">
          <el-icon><Cpu /></el-icon> PXE 服务器（本机）
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
        <el-descriptions-item label="sudo 免密">{{ serverStatus.sudo_ok ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="开机自启">{{ serverStatus.dnsmasq && serverStatus.dnsmasq.enabled ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="TFTP">{{ serverStatus.tftp_root }}</el-descriptions-item>
      </el-descriptions>
      <el-row :gutter="16" style="margin-top: 8px" v-if="serverStatus.supported">
        <el-col :span="12">
          <div style="font-size: 12px; color: #999; margin-bottom: 4px">TFTP 文件</div>
          <el-tag v-for="f in serverStatus.tftp_files" :key="f" size="small" style="margin: 2px">{{ f }}</el-tag>
          <span v-if="!serverStatus.tftp_files || !serverStatus.tftp_files.length" style="color: #ccc; font-size: 12px">空</span>
        </el-col>
        <el-col :span="12">
          <div style="font-size: 12px; color: #999; margin-bottom: 4px">HTTP 文件</div>
          <el-tag v-for="f in serverStatus.web_files" :key="f" size="small" style="margin: 2px">{{ f }}</el-tag>
          <span v-if="!serverStatus.web_files || !serverStatus.web_files.length" style="color: #ccc; font-size: 12px">空</span>
        </el-col>
      </el-row>
      <div v-if="deployLog.length" style="margin-top: 8px">
        <div style="font-size: 12px; color: #999; margin-bottom: 4px">部署日志</div>
        <div class="terminal-output" style="white-space: pre; max-height: 200px">{{ deployLog.join("\n") }}</div>
      </div>
      <el-alert v-if="serverStatus.supported === false" type="warning" :closable="false" style="margin-top: 8px">本机部署需 Linux 环境（当前：{{ serverStatus.platform }}），可用「下载 ZIP」手动部署</el-alert>
    </el-card>
        <!-- ISO 管理 -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
            <span style="font-weight: 600"><el-icon><Files /></el-icon> ISO 镜像管理</span>
            <el-button size="small" @click="loadIsos"><el-icon><Refresh /></el-icon> 刷新</el-button>
          </div>
          <el-alert v-if="isoList.supported === false" type="warning" :closable="false" style="margin-bottom: 8px">需 Linux 环境</el-alert>
          <el-table v-else :data="isoList.isos || []" size="small" empty-text="尚无 ISO 文件，请将 ISO 上传到服务器 /srv/opstk/iso/ 目录">
            <el-table-column prop="name" label="ISO 文件" min-width="280" />
            <el-table-column prop="size_mb" label="大小 (MB)" width="110" />
            <el-table-column label="操作" width="280" fixed="right">
              <template #default="{ row }">
                <el-select v-model="row._osType" size="small" style="width: 90px; margin-right: 6px">
                  <el-option label="Ubuntu" value="ubuntu" />
                  <el-option label="RHEL" value="rhel" />
                </el-select>
                <el-input v-model="row._osVer" size="small" style="width: 80px; margin-right: 6px" placeholder="22.04" />
                <el-button type="success" link size="small" @click="extractIso(row)" :loading="row._extracting">提取</el-button>
                <el-popconfirm title="确定删除?" @confirm="delIso(row.name)">
                  <template #reference><el-button type="danger" link size="small">删除</el-button></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="extractLog.length" style="margin-top: 8px">
            <div style="font-size: 12px; color: #999; margin-bottom: 4px">提取日志</div>
            <div class="terminal-output" style="white-space: pre; max-height: 200px">{{ extractLog.join('\n') }}</div>
          </div>
        </el-card>
        <!-- 模板列表 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; margin-bottom: 12px">
        <span style="font-weight: 600"><el-icon><Cpu /></el-icon> PXE 装机模板</span>
        <el-button type="primary" @click="openProfileDialog()"><el-icon><Plus /></el-icon> 新建装机模板</el-button>
      </div>
      <el-table :data="profiles" stripe size="small">
        <el-table-column prop="name" label="模板名称" min-width="130" />
        <el-table-column label="系统" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.os_type === 'ubuntu' ? 'success' : 'danger'">{{ osLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="disk_scheme" label="磁盘" width="70" />
        <el-table-column prop="net_mode" label="网络" width="70" />
        <el-table-column prop="timezone" label="时区" width="120" />
        <el-table-column prop="mirror" label="镜像源" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openGenDialog(row)">生成配置</el-button>
            <el-button type="success" link size="small" @click="deployProfile(row)">部署</el-button>
            <el-button link type="primary" size="small" @click="openProfileDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delProfile(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 装机记录 -->
    <el-card shadow="never">
      <template #header><span style="font-weight: 600"><el-icon><Monitor /></el-icon> 装机记录</span></template>
      <el-table :data="installs" stripe size="small">
        <el-table-column prop="hostname" label="主机名" min-width="120" />
        <el-table-column prop="mac" label="MAC 地址" width="160" />
        <el-table-column prop="ip" label="分配 IP" width="120" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="160">
          <template #default="{ row }">{{ row.created_at ? row.created_at.replace('T',' ').slice(0,19) : '' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-popconfirm title="确定删除?" @confirm="delInstall(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 模板编辑弹窗 -->
    <el-dialog v-model="profileDialog" :title="editingId ? '编辑装机模板' : '新建装机模板'" width="760px" :close-on-click-modal="false">
      <el-form :model="form" label-width="90px" size="default">
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="模板名"><el-input v-model="form.name" /></el-form-item></el-col>
          <el-col :span="8">
            <el-form-item label="系统">
              <el-select v-model="form.os_type" @change="onOsChange">
                <el-option label="Ubuntu 22.04+" value="ubuntu" /><el-option label="RHEL/Rocky/Alma 8+" value="rhel" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="版本"><el-input v-model="form.os_version" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="时区"><el-input v-model="form.timezone" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="语言"><el-input v-model="form.locale" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="键盘"><el-input v-model="form.keyboard" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">账号</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="管理员"><el-input v-model="form.admin_user" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="管理员密码"><el-input v-model="form.admin_password" type="password" show-password placeholder="留空不修改" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="root密码" v-if="form.os_type === 'rhel'"><el-input v-model="form.root_password" type="password" show-password /></el-form-item></el-col>
        </el-row>
        <el-form-item label="SSH公钥">
          <el-input v-model="form.ssh_keys_text" type="textarea" :rows="2" placeholder="每行一个公钥" />
        </el-form-item>

        <el-divider content-position="left">磁盘</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="分区方案">
              <el-select v-model="form.disk_scheme">
                <el-option label="LVM (推荐)" value="lvm" /><el-option label="直通分区" value="direct" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="磁盘名"><el-input v-model="form.disk_name" placeholder="sda / nvme0n1" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">网络</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="模式">
              <el-select v-model="form.net_mode"><el-option label="DHCP" value="dhcp" /><el-option label="静态" value="static" /></el-select>
            </el-form-item>
          </el-col>
          <template v-if="form.net_mode === 'static'">
            <el-col :span="8"><el-form-item label="网卡名"><el-input v-model="form.net_interface" placeholder="ens33" /></el-form-item></el-col>
            <el-col :span="8"><el-form-item label="IP"><el-input v-model="form.net_ip" /></el-form-item></el-col>
          </template>
        </el-row>
        <el-row :gutter="12" v-if="form.net_mode === 'static'">
          <el-col :span="8"><el-form-item label="掩码"><el-input v-model="form.net_netmask" placeholder="255.255.255.0" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="网关"><el-input v-model="form.net_gateway" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DNS"><el-input v-model="form.net_dns" placeholder="逗号分隔" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">镜像与软件</el-divider>
        <el-form-item label="镜像源"><el-input v-model="form.mirror" placeholder="http://mirror/rocky/9/BaseOS/x86_64/os/" /></el-form-item>
        <el-form-item label="额外软件"><el-input v-model="form.extra_packages_text" placeholder="逗号分隔: vim, net-tools, htop" /></el-form-item>
        <el-form-item label="安装后脚本"><el-input v-model="form.post_script" type="textarea" :rows="3" placeholder="bash 脚本 (可选)" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="profileDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveProfile">保存模板</el-button>
      </template>
    </el-dialog>

    <!-- 配置生成弹窗 -->
    <el-dialog v-model="genDialog" title="PXE 部署文件生成" width="860px" top="5vh">
      <el-form label-width="90px" size="small" style="margin-bottom: 12px">
        <el-row :gutter="8">
          <el-col :span="8">
            <el-form-item label="部署模式">
              <el-select v-model="genForm.deploy_mode" style="width:100%">
                <el-option label="独立DHCP (专用装机网络)" value="standalone" />
                <el-option label="ProxyDHCP (与现有DHCP并存)" value="proxy" />
                <el-option label="中继模式 (仅TFTP, 依赖交换机)" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6"><el-form-item label="主机名"><el-input v-model="genForm.hostname" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="PXE服务IP"><el-input v-model="genForm.server_ip" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="内核路径"><el-input v-model="genForm.kernel_path" placeholder="rhel/9/vmlinuz" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="initrd"><el-input v-model="genForm.initrd_path" placeholder="rhel/9/initrd.img" /></el-form-item></el-col>
        </el-row>
        <el-button type="primary" size="small" @click="doGenerate" :loading="generating"><el-icon><Check /></el-icon> 生成文件</el-button>
          <el-button type="success" size="small" @click="doDownload" :disabled="!Object.keys(genFiles).length"><el-icon><Download /></el-icon> 下载 ZIP</el-button>
      </el-form>
      <el-tabs v-model="activeFile" v-if="Object.keys(genFiles).length">
        <el-tab-pane v-for="(_, name) in genFiles" :key="name" :label="name" :name="name">
          <div class="terminal-output" style="white-space: pre; max-height: 420px">{{ genFiles[name] }}</div>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, computed } from "vue"
import http, { downloadZip } from "../api"
import { ElMessage } from "element-plus"

const profiles = ref([])
const installs = ref([])
const profileDialog = ref(false)
const editingId = ref(null)
const saving = ref(false)
const genDialog = ref(false)
const generating = ref(false)
const genFiles = ref({})
const activeFile = ref("")
const serverStatus = ref({ supported: false })
const deployLog = ref([])
const deploying = ref(false)
const isoList = ref({ supported: false, isos: [] })
const extractLog = ref([])

async function loadServerStatus() {
  try { serverStatus.value = await http.get("/it/pxe/server/status") } catch(e) {}
  serverPollTimer = setTimeout(loadServerStatus, 5000)
}
async function controlService(action) {
  try { const r = await http.post("/it/pxe/server/service", { action }); ElMessage.success(r.msg || action) } catch(e) {}
  loadServerStatus()
}
async function deployProfile(row) {
  deploying.value = true
  try {
    const r = await http.post("/it/pxe/profiles/" + row.id + "/deploy", { deploy_mode: "standalone", hostname: "default", installs: [] })
    deployLog.value = r.log || []
    if (r.ok) ElMessage.success("部署完成")
    else ElMessage.warning("部署未完全成功，查看日志")
    loadServerStatus()
  } finally { deploying.value = false }
}

async function loadIsos() {
  try { isoList.value = await http.get("/it/pxe/iso/list") } catch(e) {}
}
async function extractIso(row) {
  row._extracting = true
  extractLog.value = []
  try {
    const r = await http.post("/it/pxe/iso/" + encodeURIComponent(row.name) + "/extract", {
      os_type: row._osType || "ubuntu", os_version: row._osVer || "22.04"
    })
    extractLog.value = r.log || []
    if (r.ok) { ElMessage.success("提取完成"); loadServerStatus() }
    else ElMessage.warning("提取未完全成功，查看日志")
  } finally { row._extracting = false }
}
async function delIso(name) {
  try {
    await http.delete("/it/pxe/iso/" + encodeURIComponent(name))
    ElMessage.success("已删除")
    loadIsos()
  } catch(e) {}
}


const emptyForm = () => ({
  name: "", os_type: "rhel", os_version: "9.3",
  timezone: "Asia/Shanghai", locale: "en_US.UTF-8", keyboard: "us",
  admin_user: "ops", admin_password: "", root_password: "",
  ssh_keys_text: "",
  disk_scheme: "lvm", disk_name: "sda",
  net_mode: "dhcp", net_interface: "ens33", net_ip: "", net_netmask: "255.255.255.0", net_gateway: "", net_dns: "",
  mirror: "", extra_packages_text: "", post_script: "",
})
const form = reactive(emptyForm())

const genForm = reactive({ hostname: "server01", server_ip: "192.168.1.100", http_root: "", kernel_path: "", initrd_path: "", squashfs_path: "", deploy_mode: "standalone" })

function osLabel(row) { return row.os_type + " " + row.os_version }
function statusType(s) { return { pending: "info", booting: "warning", installing: "warning", done: "success", failed: "danger" }[s] || "info" }
function statusLabel(s) { return { pending: "待装机", booting: "引导中", installing: "安装中", done: "完成", failed: "失败" }[s] || s }

function onOsChange() {
  if (form.os_type === "ubuntu") form.os_version = "22.04"
  else form.os_version = "9.3"
}

function buildPayload() {
  return {
    name: form.name, os_type: form.os_type, os_version: form.os_version,
    timezone: form.timezone, locale: form.locale, keyboard: form.keyboard,
    admin_user: form.admin_user,
    admin_password: form.admin_password || null,
    root_password: form.root_password || null,
    ssh_keys: form.ssh_keys_text.split("\n").map(k => k.trim()).filter(Boolean),
    disk_scheme: form.disk_scheme,
    disk_config: { disk: form.disk_name },
    net_mode: form.net_mode,
    net_config: form.net_mode === "static" ? {
      interface: form.net_interface, ip: form.net_ip,
      netmask: form.net_netmask, gateway: form.net_gateway,
      dns: form.net_dns.split(",").map(d => d.trim()).filter(Boolean),
    } : {},
    mirror: form.mirror,
    extra_packages: form.extra_packages_text.split(",").map(p => p.trim()).filter(Boolean),
    post_script: form.post_script,
  }
}

function fillForm(p) {
  Object.assign(form, emptyForm())
  form.name = p.name; form.os_type = p.os_type; form.os_version = p.os_version
  form.timezone = p.timezone; form.locale = p.locale; form.keyboard = p.keyboard
  form.admin_user = p.admin_user
  form.ssh_keys_text = (p.ssh_keys || []).join("\n")
  form.disk_scheme = p.disk_scheme
  form.disk_name = (p.disk_config || {}).disk || "sda"
  form.net_mode = p.net_mode
  const nc = p.net_config || {}
  form.net_interface = nc.interface || "ens33"; form.net_ip = nc.ip || ""
  form.net_netmask = nc.netmask || "255.255.255.0"; form.net_gateway = nc.gateway || ""
  form.net_dns = (nc.dns || []).join(",")
  form.mirror = p.mirror
  form.extra_packages_text = (p.extra_packages || []).join(",")
  form.post_script = p.post_script
}

function openProfileDialog(row) {
  Object.assign(form, emptyForm())
  editingId.value = null
  if (row) { fillForm(row); editingId.value = row.id }
  profileDialog.value = true
}

async function saveProfile() {
  if (!form.name) { ElMessage.warning("请输入模板名"); return }
  saving.value = true
  try {
    const payload = buildPayload()
    if (editingId.value) await http.put("/it/pxe/profiles/" + editingId.value, payload)
    else await http.post("/it/pxe/profiles", payload)
    ElMessage.success("保存成功")
    profileDialog.value = false
    loadProfiles()
  } finally { saving.value = false }
}

async function delProfile(id) {
  await http.delete("/it/pxe/profiles/" + id)
  ElMessage.success("已删除")
  loadProfiles()
}

function openGenDialog(row) {
  genForm.hostname = "server01"
  genForm.server_ip = "192.168.1.100"
  genForm.http_root = "http://192.168.1.100/pxe"
  if (row.os_type === "ubuntu") {
    genForm.kernel_path = "ubuntu/22.04/vmlinuz"
    genForm.initrd_path = "ubuntu/22.04/initrd"
    genForm.squashfs_path = "ubuntu/22.04/installer.squashfs"
  } else {
    genForm.kernel_path = "rhel/9/vmlinuz"
    genForm.initrd_path = "rhel/9/initrd.img"
    genForm.squashfs_path = ""
  }
  genDialog.value = true
  genFiles.value = {}
  sessionStorage.setItem("pxe_profile_id", row.id)
  doGenerate()
}

async function doGenerate() {
  generating.value = true
  try {
    const pid = sessionStorage.getItem("pxe_profile_id")
    const body = {
      hostname: genForm.hostname,
      server_ip: genForm.server_ip,
      http_root: genForm.http_root || ("http://" + genForm.server_ip + "/pxe"),
      kernel_path: genForm.kernel_path,
      initrd_path: genForm.initrd_path,
      squashfs_path: genForm.squashfs_path,
      deploy_mode: genForm.deploy_mode,
      installs: installs.value.map(i => ({ mac: i.mac, hostname: i.hostname })),
    }
    const res = await http.post("/it/pxe/profiles/" + pid + "/generate", body)
    genFiles.value = res.files
    const keys = Object.keys(res.files)
    if (keys.length) activeFile.value = keys[0]
    ElMessage.success("生成完成: " + keys.length + " 个文件")
  } finally { generating.value = false }
}

async function doDownload() {
  const pid = sessionStorage.getItem("pxe_profile_id")
  await downloadZip("/it/pxe/profiles/" + pid + "/download", {
    hostname: genForm.hostname,
    server_ip: genForm.server_ip,
    http_root: genForm.http_root || ("http://" + genForm.server_ip + "/pxe"),
    kernel_path: genForm.kernel_path,
    initrd_path: genForm.initrd_path,
    squashfs_path: genForm.squashfs_path,
    deploy_mode: genForm.deploy_mode,
    installs: installs.value.map(i => ({ mac: i.mac, hostname: i.hostname })),
  })
  ElMessage.success("下载已开始")
}

async function delInstall(id) {
  await http.delete("/it/pxe/installs/" + id)
  ElMessage.success("已删除")
  loadInstalls()
}

async function loadProfiles() { profiles.value = await http.get("/it/pxe/profiles") }
async function loadInstalls() { installs.value = await http.get("/it/pxe/installs") }

// 自动轮询装机状态（有活跃装机时每10s刷新）
let installTimer = null
function startInstallPolling() {
  clearTimeout(installTimer)
  installTimer = setTimeout(async () => {
    await loadInstalls()
    const hasActive = installs.value.some(i =>
      ["pending", "booting", "installing"].includes(i.status)
    )
    if (hasActive) startInstallPolling()
  }, 10000)
}

let serverPollTimer = null
onBeforeUnmount(() => { clearTimeout(installTimer); clearTimeout(serverPollTimer) })
onMounted(() => { loadProfiles(); loadInstalls(); loadServerStatus(); loadIsos(); startInstallPolling(); serverPollTimer = setTimeout(loadServerStatus, 5000) })
</script>
