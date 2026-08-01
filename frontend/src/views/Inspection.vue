<template>
  <div>
    <!-- 巡检控制区 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <el-form label-width="90px">
        <el-row :gutter="16">
          <el-col :span="12">
            <el-form-item label="选择设备">
              <el-select v-model="selectedAssets" multiple filterable placeholder="选择 CT 设备" style="width: 100%">
                <el-option v-for="a in ctAssets" :key="a.id" :label="a.name + ' (' + a.host + ')'" :value="a.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="7">
            <el-form-item label="巡检模式">
              <el-radio-group v-model="mode">
                <el-radio-button label="default">默认巡检</el-radio-button>
                <el-radio-button label="custom">自定义命令</el-radio-button>
              </el-radio-group>
            </el-form-item>
          </el-col>
          <el-col :span="5">
            <el-form-item label=" ">
              <el-button type="primary" :loading="running" :disabled="!selectedAssets.length" @click="startInspection">
                <el-icon><VideoPlay /></el-icon> 开始巡检
              </el-button>
              <el-button @click="clearOutput" :disabled="running"><el-icon><Delete /></el-icon> 清屏</el-button>
            </el-form-item>
          </el-col>
        </el-row>

        <!-- 默认巡检：选模板 -->
        <el-form-item label="巡检模板" v-if="mode === 'default'">
          <el-select v-model="selectedTemplateId" clearable placeholder="自动匹配厂商默认模板" style="width: 420px" @change="onTemplateChange">
            <el-option-group v-for="(tpls, vendor) in groupedTemplates" :key="vendor" :label="vendorLabel(vendor)">
              <el-option v-for="t in tpls" :key="t.id" :value="t.id" :label="t.name + (t.is_system ? ' (系统)' : ' (自定义)') + ' - ' + t.items.length + '项'" />
            </el-option-group>
          </el-select>
          <el-button type="info" plain size="small" style="margin-left: 12px" @click="loadTemplates(); templateDrawer = true">
            <el-icon><Setting /></el-icon> 模板管理
          </el-button>
          <el-tag v-if="currentTemplate" size="small" style="margin-left: 8px" :type="currentTemplate.is_system ? 'info' : 'warning'">
            {{ currentTemplate.vendor }} | {{ currentTemplate.items.length }} 项指标
          </el-tag>
        </el-form-item>

        <el-form-item label="自定义命令" v-if="mode === 'custom'">
          <el-input v-model="customCommands" type="textarea" :rows="4" placeholder="每行一条命令" />
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 实时输出 -->
    <el-card shadow="never" v-show="outputLines.length" style="margin-bottom: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600"><el-icon><Monitor /></el-icon> 实时输出</span>
          <el-tag v-if="running" type="warning" size="small">执行中</el-tag>
          <el-tag v-else-if="completed" type="success" size="small">已完成</el-tag>
        </div>
      </template>
      <div class="terminal-output" ref="terminalRef">
        <div v-for="(line, i) in outputLines" :key="i" :class="'terminal-line-' + line.type">{{ line.text }}</div>
      </div>
    </el-card>

    <!-- 巡检进度 -->
    <el-card shadow="never" v-if="running && progressTotal > 0" style="margin-bottom: 16px">
      <div style="margin-bottom: 6px; font-size: 13px; color: #606266">
        巡检进度: {{ progressDone }} / {{ progressTotal }} 台已完成
        <span v-if="progressFailed" style="color: #f56c6c; margin-left: 8px">{{ progressFailed }} 台失败</span>
      </div>
      <el-progress
        :percentage="Math.round((progressDone + progressFailed) * 100 / progressTotal)"
        :status="progressFailed ? 'exception' : undefined"
        :stroke-width="18"
        :text-inside="true"
      />
    </el-card>

    <!-- 巡检结果 -->
    <el-card shadow="never" v-if="results.length">
      <template #header><span style="font-weight: 600"><el-icon><DataAnalysis /></el-icon> 巡检结果</span></template>
      <el-collapse v-model="activeResults">
        <el-collapse-item v-for="r in results" :key="r.asset_id" :name="r.asset_id">
          <template #title>
            <span>{{ r.asset_name }}</span>
            <el-tag :type="r.status === 'success' ? 'success' : 'danger'" size="small" style="margin-left: 12px">{{ r.status === 'success' ? '成功' : '失败' }}</el-tag>
          </template>
          <el-row :gutter="12" v-if="r.status === 'success'">
            <el-col :span="4" v-for="(m, key) in r.metrics" :key="key" style="margin-bottom: 8px">
              <el-card shadow="hover" body-style="padding: 12px; text-align: center">
                <div style="font-size: 11px; color: #909399">{{ m.label }}</div>
                <div style="font-size: 13px; font-weight: 600; margin-top: 4px" :style="{ color: statusColor(m.status) }">{{ m.summary }}</div>
              </el-card>
            </el-col>
          </el-row>
          <el-alert v-if="r.status === 'failed'" type="error" :title="r.error" :closable="false" />
          <el-table v-if="r.raw && r.raw.length" :data="r.raw" size="small" style="margin-top: 12px">
            <el-table-column prop="label" label="指标" width="120" />
            <el-table-column prop="cmd" label="命令" width="200" />
            <el-table-column prop="summary" label="解析摘要" min-width="200" />
            <el-table-column label="详情" width="70">
              <template #default="{ row }">
                <el-button link type="primary" size="small" @click="showRaw(row)">查看</el-button>
              </template>
            </el-table-column>
          </el-table>
        </el-collapse-item>
      </el-collapse>
      <div style="margin-top: 12px">
        <el-button size="small" @click="openCompare">巡检结果对比</el-button>
      </div>
    </el-card>

    <!-- 原文详情弹窗 -->
    <el-dialog v-model="rawDialogVisible" :title="rawDetail ? rawDetail.label : ''" width="720px">
      <div class="terminal-output" style="max-height: 400px">
        <div class="terminal-line-info">{{ rawDetail ? rawDetail.output : '' }}</div>
      </div>
    </el-dialog>

    <!-- 模板管理抽屉 -->
    <el-drawer v-model="templateDrawer" title="巡检模板管理" size="640px">
      <div style="margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center">
        <el-select v-model="tplFilterVendor" placeholder="全部厂商" clearable size="small" style="width: 140px" @change="loadTemplates">
          <el-option label="H3C" value="h3c" /><el-option label="华为" value="huawei" /><el-option label="思科" value="cisco" />
        </el-select>
        <el-button type="primary" size="small" @click="openTplEdit(null)"><el-icon><Plus /></el-icon> 新建模板</el-button>
        <el-upload accept=".json" :show-file-list="false" :before-upload="importTemplate" style="display:inline-block;margin-left:6px">
          <el-button size="small"><el-icon><Upload /></el-icon> 导入</el-button>
        </el-upload>
      </div>
      <el-table :data="templates" size="small" stripe>
        <el-table-column prop="name" label="模板名称" min-width="120" />
        <el-table-column prop="vendor" label="厂商" width="70">
          <template #default="{ row }">{{ vendorLabel(row.vendor) }}</template>
        </el-table-column>
        <el-table-column label="指标" width="55">
          <template #default="{ row }">{{ row.items.length }}</template>
        </el-table-column>
        <el-table-column label="类型" width="70">
          <template #default="{ row }">
            <el-tag :type="row.is_system ? 'info' : 'success'" size="small">{{ row.is_system ? '系统' : '自定义' }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="190" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openTplView(row)">查看</el-button>
            <el-button link type="success" size="small" @click="exportTemplate(row)">导出</el-button>
            <el-button link type="primary" size="small" @click="cloneTpl(row)">克隆</el-button>
            <el-button link type="warning" size="small" :disabled="row.is_system" @click="openTplEdit(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delTpl(row.id)">
              <template #reference><el-button link type="danger" size="small" :disabled="row.is_system">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-drawer>

    <!-- 模板查看弹窗 -->
    <el-dialog v-model="tplViewVisible" :title="(tplViewData ? tplViewData.name : '') + ' - 指标详情'" width="720px">
      <el-table :data="tplViewData ? tplViewData.items : []" size="small" stripe>
        <el-table-column type="index" width="40" />
        <el-table-column prop="label" label="指标名" width="140" />
        <el-table-column prop="command" label="下发命令" min-width="220" />
        <el-table-column prop="key" label="标识" width="100" />
        <el-table-column prop="textfsm" label="解析模板" width="160" />
      </el-table>
    </el-dialog>

    <!-- 模板编辑弹窗 -->
    <el-dialog v-model="tplEditVisible" :title="tplEditingId ? '编辑模板' : '新建模板'" width="820px" :close-on-click-modal="false">
      <el-form label-width="70px">
        <el-row :gutter="12">
          <el-col :span="12"><el-form-item label="名称"><el-input v-model="tplForm.name" /></el-form-item></el-col>
          <el-col :span="6">
            <el-form-item label="厂商">
              <el-select v-model="tplForm.vendor" style="width: 100%">
                <el-option label="H3C" value="h3c" /><el-option label="华为" value="huawei" /><el-option label="思科" value="cisco" /><el-option label="通用" value="generic" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6"><el-form-item label="备注"><el-input v-model="tplForm.description" /></el-form-item></el-col>
        </el-row>
        <div style="margin-bottom: 8px; font-size: 13px; color: #606266; font-weight: 600">巡检指标项（可增删改命令）</div>
        <el-table :data="tplForm.items" size="small" stripe border>
          <el-table-column type="index" width="38" />
          <el-table-column label="指标标识" width="120"><template #default="{ row }"><el-input v-model="row.key" size="small" /></template></el-table-column>
          <el-table-column label="显示名" width="120"><template #default="{ row }"><el-input v-model="row.label" size="small" /></template></el-table-column>
          <el-table-column label="下发命令" min-width="200"><template #default="{ row }"><el-input v-model="row.command" size="small" /></template></el-table-column>
          <el-table-column label="TextFSM" width="150"><template #default="{ row }"><el-input v-model="row.textfsm" size="small" placeholder="可选" /></template></el-table-column>
          <el-table-column label="操作" width="50"><template #default="{ $index }"><el-button link type="danger" size="small" @click="tplForm.items.splice($index,1)"><el-icon><Delete /></el-icon></el-button></template></el-table-column>
        </el-table>
        <el-button plain size="small" style="margin-top: 8px" @click="tplForm.items.push({ key: '', label: '', command: '', textfsm: '', unit: '' })"><el-icon><Plus /></el-icon> 添加指标</el-button>
      </el-form>
      <template #footer>
        <el-button @click="tplEditVisible = false">取消</el-button>
        <el-button type="primary" :loading="tplSaving" @click="saveTpl">保存模板</el-button>
      </template>
    </el-dialog>

    <!-- 巡检结果对比对话框 -->
    <el-dialog v-model="compareDialogVisible" title="巡检结果对比" width="700px">
      <el-form label-width="80px" size="small">
        <el-form-item label="资产 ID">
          <el-select v-model="compareAssetId" filterable placeholder="选择资产">
            <el-option v-for="a in ctAssets" :key="a.id" :label="a.name" :value="a.id" />
          </el-select>
          <el-button type="primary" size="small" style="margin-left: 12px" @click="doCompare" :loading="compareLoading">查询对比</el-button>
        </el-form-item>
      </el-form>
      <div v-if="compareData.delta && Object.keys(compareData.delta).length">
        <el-alert v-if="compareData.note" :title="compareData.note" type="info" :closable="false" style="margin-bottom: 12px" />
        <el-table :data="compareRows" size="small" border>
          <el-table-column prop="key" label="指标" width="100" />
          <el-table-column prop="prev" label="上次" width="140" />
          <el-table-column prop="latest" label="本次" width="140" />
          <el-table-column label="变化" width="100">
            <template #default="{ row }">
              <el-tag :type="row.trend === 'up' ? 'danger' : row.trend === 'down' ? 'success' : 'info'" size="small">{{ row.change }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="趋势" width="80">
            <template #default="{ row }">
              <el-icon v-if="row.trend === 'up'" color="#f56c6c"><Top /></el-icon>
              <el-icon v-else-if="row.trend === 'down'" color="#67c23a"><Bottom /></el-icon>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="时间" min-width="160">
            <template #default="{ row }">
              <div style="font-size: 12px; color: #999">{{ row.prev_time }}</div>
              <div style="font-size: 12px; color: #333">{{ row.latest_time }}</div>
            </template>
          </el-table-column>
        </el-table>
      </div>
      <div v-else-if="compareCalled" style="color: #999; text-align: center; padding: 40px">暂无对比数据</div>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, nextTick } from 'vue'
import http from '../api'
import { ElMessage } from 'element-plus'

const ctAssets = ref([])
const selectedAssets = ref([])
const mode = ref('default')
const customCommands = ref('')
const selectedTemplateId = ref('')
const running = ref(false)
const completed = ref(false)
const outputLines = ref([])
const results = ref([])
const activeResults = ref([])
const progressDone = ref(0)
const progressTotal = ref(0)
const progressFailed = ref(0)
const terminalRef = ref()
const rawDialogVisible = ref(false)
const rawDetail = ref(null)

// 模板管理状态
const templateDrawer = ref(false)
const templates = ref([])
const tplFilterVendor = ref('')
const tplViewVisible = ref(false)
const tplViewData = ref(null)
const tplEditVisible = ref(false)
const tplEditingId = ref(null)
const tplSaving = ref(false)
const tplForm = reactive({ name: '', vendor: 'h3c', description: '', items: [] })

const groupedTemplates = computed(() => {
  const g = {}
  for (const t of templates.value) {
    if (!g[t.vendor]) g[t.vendor] = []
    g[t.vendor].push(t)
  }
  return g
})

const currentTemplate = computed(() => templates.value.find(t => t.id === selectedTemplateId.value))

function vendorLabel(v) {
  return { h3c: 'H3C', huawei: '华为', cisco: '思科', generic: '通用' }[v] || v
}

function statusColor(s) {
  return { ok: '#52c41a', warning: '#faad14', critical: '#ff4d4f', unknown: '#909399' }[s] || '#909399'
}

function pushLine(text, type) {
  outputLines.value.push({ text, type: type || 'info' })
  nextTick(() => { if (terminalRef.value) terminalRef.value.scrollTop = terminalRef.value.scrollHeight })
}

function clearOutput() { outputLines.value = []; results.value = []; completed.value = false }
function showRaw(row) { rawDetail.value = row; rawDialogVisible.value = true }

function onTemplateChange() {
  if (currentTemplate.value) ElMessage.info('已选模板: ' + currentTemplate.value.name + ' (' + currentTemplate.value.items.length + ' 项)')
}

async function loadTemplates() {
  try {
    const q = tplFilterVendor.value ? ('?vendor=' + tplFilterVendor.value) : ''
    templates.value = await http.get('/ct/inspection/templates/list' + q)
  } catch (e) { /* handled */ }
}

function openTplView(row) { tplViewData.value = row; tplViewVisible.value = true }

function openTplEdit(row) {
  tplForm.name = ''
  tplForm.vendor = 'h3c'
  tplForm.description = ''
  tplForm.items = []
  tplEditingId.value = null
  if (row) {
    tplEditingId.value = row.id
    tplForm.name = row.name
    tplForm.vendor = row.vendor
    tplForm.description = row.description
    tplForm.items = row.items.map(i => ({ key: i.key, label: i.label, command: i.command, textfsm: i.textfsm || '', unit: i.unit || '' }))
  }
  tplEditVisible.value = true
}

async function saveTpl() {
  if (!tplForm.name) { ElMessage.warning('请输入模板名称'); return }
  if (!tplForm.items.length) { ElMessage.warning('请至少添加一个指标'); return }
  tplSaving.value = true
  try {
    const payload = { name: tplForm.name, vendor: tplForm.vendor, description: tplForm.description, items: tplForm.items }
    if (tplEditingId.value) await http.put('/ct/inspection/templates/' + tplEditingId.value, payload)
    else await http.post('/ct/inspection/templates', payload)
    ElMessage.success('保存成功')
    tplEditVisible.value = false
    loadTemplates()
  } finally { tplSaving.value = false }
}

async function exportTemplate(row) {
  try {
    const data = await http.get("/ct/inspection/templates/" + row.id + "/export")
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })
    const a = document.createElement("a")
    a.href = URL.createObjectURL(blob)
    a.download = (row.name || "template") + ".json"
    a.click()
    URL.revokeObjectURL(a.href)
    ElMessage.success("已导出")
  } catch (e) {
    ElMessage.error("导出失败")
  }
}

async function importTemplate(file) {
  try {
    const text = await file.text()
    const data = JSON.parse(text)
    await http.post("/ct/inspection/templates/import", data)
    ElMessage.success("导入成功: " + (data.name || ""))
    loadTemplates()
  } catch (e) {
    ElMessage.error("导入失败: " + (e.response?.data?.detail || e.message))
  }
  return false
}

async function cloneTpl(row) {
  await http.post('/ct/inspection/templates/' + row.id + '/clone')
  ElMessage.success('克隆成功')
  loadTemplates()
}

async function delTpl(id) {
  await http.delete('/ct/inspection/templates/' + id)
  ElMessage.success('已删除')
  loadTemplates()
}

async function startInspection() {
  clearOutput()
  running.value = true
  progressDone.value = 0
  progressFailed.value = 0
  progressTotal.value = selectedAssets.value.length
  const commands = mode.value === 'custom' ? customCommands.value.split('\n').map(c => c.trim()).filter(Boolean) : null
  const tmpl = currentTemplate.value
  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws'
  const token = localStorage.getItem('opstk_token') || ''
  const ws = new WebSocket(wsProto + '://' + location.host + '/api/ct/inspection/ws?token=' + encodeURIComponent(token))

  ws.onopen = () => {
    pushLine('[连接已建立] 开始巡检 ' + selectedAssets.value.length + ' 台设备...', 'info')
    const payload = { asset_ids: selectedAssets.value, kind: mode.value, commands: commands }
    if (mode.value === 'default' && tmpl) {
      payload.template = tmpl.name
      pushLine('[模板] ' + tmpl.vendor + ' / ' + tmpl.name + ' (' + tmpl.items.length + ' 项)', 'info')
    }
    ws.send(JSON.stringify(payload))
  }

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.type === 'start') pushLine('\n--- ' + msg.asset_name + ' ---', 'info')
    else if (msg.type === 'cmd') pushLine('> ' + msg.cmd, 'cmd')
    else if (msg.type === 'output') msg.output.split('\n').forEach(l => { if (l.trim()) pushLine(l, 'info') })
    else if (msg.type === 'error') pushLine('[ERROR] ' + msg.error, 'err')
    else if (msg.type === 'done') pushLine('[完成] ' + msg.asset_name, 'ok')
    else if (msg.type === 'progress') {
      progressDone.value = msg.done || 0
      progressFailed.value = msg.failed || 0
      progressTotal.value = msg.total || 0
    }
    else if (msg.type === 'complete') {
      results.value = msg.results || []
      activeResults.value = results.value.map(r => r.asset_id)
      completed.value = true
      running.value = false
      pushLine('\n=== 全部巡检完成 ===', 'ok')
      ws.close()
    }
    else if (msg.type === 'fatal') { pushLine('[FATAL] ' + msg.error, 'err'); running.value = false }
  }

  ws.onerror = () => { pushLine('[WebSocket 连接失败]', 'err'); running.value = false }
  ws.onclose = () => { running.value = false }
}

// 巡检结果对比
const compareDialogVisible = ref(false)
const compareAssetId = ref("")
const compareLoading = ref(false)
const compareCalled = ref(false)
const compareData = ref({})
const compareRows = ref([])

function openCompare() {
  compareAssetId.value = ctAssets.value.length > 0 ? ctAssets.value[0].id : ""
  compareDialogVisible.value = true
  compareCalled.value = false
  compareData.value = {}
  compareRows.value = []
}

async function doCompare() {
  if (!compareAssetId.value) return
  compareLoading.value = true
  try {
    compareData.value = await http.get("/ct/inspection/results/compare?asset_id=" + compareAssetId.value + "&limit=2")
    compareCalled.value = true
    const d = compareData.value.delta || {}
    const snaps = compareData.value.snapshots || []
    compareRows.value = Object.entries(d).map(([key, val]) => ({
      key,
      prev: val.prev ?? val.prev_summary ?? "-",
      latest: val.latest ?? val.latest_summary ?? "-",
      change: val.change ?? val.trend,
      trend: val.trend,
      prev_time: snaps.length > 1 ? (snaps[1].time || "-") : "-",
      latest_time: snaps.length > 0 ? (snaps[0].time || "-") : "-",
    }))
  } catch (e) {
    ElMessage.error("对比查询失败")
  } finally {
    compareLoading.value = false
  }
}

onMounted(async () => {
  try {
    ctAssets.value = await http.get('/assets?category=ct')
    await loadTemplates()
  } catch (e) { /* handled */ }
})
</script>