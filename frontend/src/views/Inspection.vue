<template>
  <div>
    <el-card shadow="never" style="margin-bottom: 16px">
      <el-form label-width="90px">
        <el-row :gutter="16">
          <el-col :span="14">
            <el-form-item label="选择设备">
              <el-select v-model="selectedAssets" multiple filterable placeholder="选择 CT 设备" style="width: 100%">
                <el-option v-for="a in ctAssets" :key="a.id" :label="`${a.name} (${a.host})`" :value="a.id" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="10">
            <el-form-item label="巡检模式">
              <el-radio-group v-model="mode">
                <el-radio-button label="default">默认巡检</el-radio-button>
                <el-radio-button label="custom">自定义命令</el-radio-button>
              </el-radio-group>
            </el-form-item>
          </el-col>
        </el-row>
        <el-form-item label="自定义命令" v-if="mode === 'custom'">
          <el-input v-model="customCommands" type="textarea" :rows="4" placeholder="每行一条命令，例如&#10;display cpu-usage&#10;display interface brief" />
        </el-form-item>
        <el-form-item v-if="mode === 'default' && templatesLoaded">
          <el-alert type="info" :closable="false" show-icon>
            默认巡检将根据设备厂商自动下发关键指标命令（CPU 内存 接口 电源风扇 告警日志等）
          </el-alert>
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="running" :disabled="!selectedAssets.length" @click="startInspection">
            <el-icon><VideoPlay /></el-icon> 开始巡检
          </el-button>
          <el-button @click="clearOutput" :disabled="running"><el-icon><Delete /></el-icon> 清屏</el-button>
        </el-form-item>
      </el-form>
    </el-card>

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
    </el-card>

    <el-dialog v-model="rawDialogVisible" :title="rawDetail ? rawDetail.label : ''" width="720px">
      <div class="terminal-output" style="max-height: 400px">
        <div class="terminal-line-info">{{ rawDetail ? rawDetail.output : '' }}</div>
      </div>
      <div v-if="rawDetail && rawDetail.parsed && Array.isArray(rawDetail.parsed) && rawDetail.parsed.length" style="margin-top: 12px; color: #909399">
        {{ rawDetail.parsed.length }} 条结构化数据
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted, nextTick } from 'vue'
import http from '../api'

const ctAssets = ref([])
const selectedAssets = ref([])
const mode = ref('default')
const customCommands = ref('')
const templatesLoaded = ref(false)
const running = ref(false)
const completed = ref(false)
const outputLines = ref([])
const results = ref([])
const activeResults = ref([])
const terminalRef = ref()
const rawDialogVisible = ref(false)
const rawDetail = ref(null)

function statusColor(s) {
  return { ok: '#52c41a', warning: '#faad14', critical: '#ff4d4f', unknown: '#909399' }[s] || '#909399'
}

function pushLine(text, type) {
  outputLines.value.push({ text, type: type || 'info' })
  nextTick(() => { if (terminalRef.value) terminalRef.value.scrollTop = terminalRef.value.scrollHeight })
}

function clearOutput() {
  outputLines.value = []
  results.value = []
  completed.value = false
}

function showRaw(row) {
  rawDetail.value = row
  rawDialogVisible.value = true
}

async function startInspection() {
  clearOutput()
  running.value = true
  const commands = mode.value === 'custom'
    ? customCommands.value.split('\n').map(c => c.trim()).filter(Boolean)
    : null

  const wsProto = location.protocol === 'https:' ? 'wss' : 'ws'
  const wsUrl = wsProto + '://' + location.host + '/api/ct/inspection/ws'
  const ws = new WebSocket(wsUrl)

  ws.onopen = () => {
    pushLine('[连接已建立] 开始巡检 ' + selectedAssets.value.length + ' 台设备...', 'info')
    ws.send(JSON.stringify({
      asset_ids: selectedAssets.value,
      kind: mode.value,
      commands: commands,
    }))
  }

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data)
    if (msg.type === 'start') pushLine('\n--- ' + msg.asset_name + ' ---', 'info')
    else if (msg.type === 'cmd') pushLine('> ' + msg.cmd, 'cmd')
    else if (msg.type === 'output') {
      msg.output.split('\n').forEach(l => { if (l.trim()) pushLine(l, 'info') })
    }
    else if (msg.type === 'error') pushLine('[ERROR] ' + msg.error, 'err')
    else if (msg.type === 'done') pushLine('[完成] ' + msg.asset_name, 'ok')
    else if (msg.type === 'complete') {
      results.value = msg.results || []
      activeResults.value = results.value.map(r => r.asset_id)
      completed.value = true
      running.value = false
      pushLine('\n=== 全部巡检完成 ===', 'ok')
      ws.close()
    }
    else if (msg.type === 'fatal') {
      pushLine('[FATAL] ' + msg.error, 'err')
      running.value = false
    }
  }

  ws.onerror = () => { pushLine('[WebSocket 连接失败]', 'err'); running.value = false }
  ws.onclose = () => { running.value = false }
}

onMounted(async () => {
  try {
    ctAssets.value = await http.get('/assets?category=ct')
    await http.get('/ct/inspection/templates')
    templatesLoaded.value = true
  } catch (e) { /* handled by interceptor */ }
})
</script>