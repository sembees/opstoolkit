<template>
  <div class="page">
    <PageHeader title="仪表盘" desc="资产与装机概览" />
    <el-row :gutter="16">
      <el-col :span="6" v-for="card in statCards" :key="card.label">
        <el-card shadow="hover">
          <div class="metric-card">
            <el-icon :size="32" :color="card.color"><component :is="card.icon" /></el-icon>
            <div class="metric-value" :style="{ color: card.color }">{{ card.value }}</div>
            <div class="metric-label">{{ card.label }}</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <CardSection title="近期巡检任务">
      <el-table :data="recentTasks" stripe size="small" empty-text="暂无巡检任务">
        <el-table-column prop="name" label="任务名称" min-width="160" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusTag(row.status)" size="small">{{ statusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="设备数" width="80">
          <template #default="{ row }">{{ row.result_count }}/{{ row.asset_count }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" :disabled="row.status !== 'done' && row.status !== 'failed'" @click="replayTask(row)">回放</el-button>
          </template>
        </el-table-column>
      </el-table>
    </CardSection>

    <CardSection title="最近 PXE 装机记录">
      <el-table :data="recentInstalls" stripe size="small" empty-text="暂无 PXE 装机记录">
        <el-table-column prop="hostname" label="主机名" min-width="120" />
        <el-table-column prop="mac" label="MAC" width="150" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="pxeStatusTag(row.status)" size="small">{{ pxeStatusText(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </CardSection>

    <CardSection title="快捷入口">
      <el-space wrap>
        <el-button type="primary" plain @click="$router.push('/inspection')"><el-icon><Monitor /></el-icon> CT 巡检</el-button>
        <el-button plain @click="$router.push('/netconfig')"><el-icon><Connection /></el-icon> 网络配置生成</el-button>
        <el-button plain @click="$router.push('/assets')"><el-icon><Coin /></el-icon> 资产管理</el-button>
      </el-space>
    </CardSection>

    <!-- 任务回放对话框 -->
    <el-dialog v-model="replayVisible" :title="'任务回放: ' + replayName" width="720px">
      <el-alert v-if="!replayLog.length" title="无回放日志" type="info" :closable="false" />
      <div class="terminal-output" style="max-height: 500px" v-else>
        <div v-for="(ev, i) in replayLog" :key="i" class="replay-line">
          <span v-if="ev.type === 'start'" class="replay-start">── ── {{ ev.asset_name }} ──</span>
          <span v-else-if="ev.type === 'cmd'" class="replay-cmd">> {{ ev.cmd }}</span>
          <span v-else-if="ev.type === 'output'" class="replay-out">{{ ev.output }}</span>
          <span v-else-if="ev.type === 'error'" class="replay-err">[ERROR] {{ ev.error }}</span>
          <span v-else-if="ev.type === 'done'" class="replay-ok">[OK] {{ ev.asset_name }}</span>
          <span v-else>{{ ev.type }}</span>
        </div>
      </div>
    </el-dialog>

  </div>
</template>

<script setup>
import { ref, onMounted } from "vue"
import http from "../api"
import { ElMessage } from "element-plus"
import PageHeader from "../components/PageHeader.vue"
import CardSection from "../components/CardSection.vue"

const recentTasks = ref([])
const recentInstalls = ref([])
const replayVisible = ref(false)
const replayName = ref("")
const replayLog = ref([])

async function replayTask(row) {
  replayName.value = row.name || row.id
  replayVisible.value = true
  replayLog.value = []
  try {
    const data = await http.get("/ct/inspection/tasks/" + row.id + "/replay")
    replayLog.value = data.log || []
  } catch (e) {
    ElMessage.error("回放加载失败")
  }
}


// 四张指标卡图标必须**同一族**（都描边、笔画粗细相当）。
// 原为 Monitor / Platform / DataAnalysis / List：其中 Platform 与 List 字形实心感很重，
// 与另两个描边图标混在一起显得不齐（审计的"描边/实心混用"，已看图确认）。
// 换成 Monitor / Cpu / DataAnalysis / Tickets —— 四个都是描边族、粗细相近。
const statCards = ref([
  { label: "CT 设备", value: 0, icon: "Monitor", color: "var(--ot-accent-1)" },
  { label: "IT 服务器", value: 0, icon: "Cpu", color: "var(--ot-accent-2)" },
  { label: "巡检模板", value: 0, icon: "DataAnalysis", color: "var(--ot-accent-3)" },
  { label: "巡检任务", value: 0, icon: "Tickets", color: "var(--ot-accent-4)" },
])

const statusTag = (s) => ({ done: "success", running: "warning", failed: "danger", pending: "info" }[s] || "info")
const statusText = (s) => ({ done: "已完成", running: "执行中", failed: "失败", pending: "等待中" }[s] || s)
const pxeStatusTag = (s) => ({ done: "success", installing: "warning", failed: "danger", pending: "info", booting: "warning" }[s] || "info")
const pxeStatusText = (s) => ({ pending: "待装机", booting: "引导中", installing: "安装中", done: "完成", failed: "失败" }[s] || s)
const fmtTime = (t) => t ? new Date(t).toLocaleString("zh-CN") : "-"

onMounted(async () => {
  try {
    const data = await http.get("/dashboard")
    const a = data.assets || {}
    statCards.value[0].value = a.ct || 0
    statCards.value[1].value = a.it || 0
    statCards.value[2].value = data.template_count || 0
    // count tasks from recent
    recentTasks.value = data.recent_tasks || []
    statCards.value[3].value = recentTasks.value.length
    recentInstalls.value = data.recent_installs || []
  } catch (e) {
    // keep defaults
  }
})
</script>

<style scoped>
.metric-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 8px 0;
}
.metric-value {
  font-size: 28px;
  font-weight: 700;
}
.metric-label {
  font-size: 13px;
  color: var(--ot-text-3);
}
/* 任务回放对话框日志（原内联样式收编；颜色仍走 token） */
.replay-line {
  margin-bottom: 2px;
  font-size: var(--ot-font-xs);
}
.replay-start { color: var(--ot-primary); }
.replay-cmd { color: var(--ot-warning); }
.replay-out { color: var(--ot-text-3); }
.replay-err { color: var(--ot-danger); }
.replay-ok { color: var(--ot-success); }
</style>
