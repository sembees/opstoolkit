<template>
  <div>
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

    <el-card style="margin-top: 16px" shadow="never">
      <template #header><span style="font-weight: 600">近期巡检任务</span></template>
      <el-table :data="recentTasks" stripe size="small" empty-text="暂无巡检记录">
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
      </el-table>
    </el-card>

    <el-card style="margin-top: 16px" shadow="never">
      <template #header><span style="font-weight: 600">最近 PXE 装机记录</span></template>
      <el-table :data="recentInstalls" stripe size="small" empty-text="暂无 PXE 装机记录">
        <el-table-column prop="hostname" label="主机名" min-width="120" />
        <el-table-column prop="mac" label="MAC" width="150" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="pxeStatusTag(row.status)" size="small">{{ row.status }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="160">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <el-card style="margin-top: 16px" shadow="never">
      <template #header><span style="font-weight: 600">快捷入口</span></template>
      <el-space wrap>
        <el-button type="primary" plain @click="$router.push('/inspection')"><el-icon><Monitor /></el-icon> CT 巡检</el-button>
        <el-button type="success" plain @click="$router.push('/netconfig')"><el-icon><Connection /></el-icon> 网络配置生成</el-button>
        <el-button plain @click="$router.push('/assets')"><el-icon><Coin /></el-icon> 资产管理</el-button>
      </el-space>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue"
import http from "../api"

const recentTasks = ref([])
const recentInstalls = ref([])

const statCards = ref([
  { label: "CT 设备", value: 0, icon: "Monitor", color: "#1890ff" },
  { label: "IT 服务器", value: 0, icon: "Platform", color: "#52c41a" },
  { label: "巡检模板", value: 0, icon: "DataAnalysis", color: "#faad14" },
  { label: "巡检任务", value: 0, icon: "List", color: "#722ed1" },
])

const statusTag = (s) => ({ done: "success", running: "warning", failed: "danger", pending: "info" }[s] || "info")
const statusText = (s) => ({ done: "已完成", running: "执行中", failed: "失败", pending: "等待中" }[s] || s)
const pxeStatusTag = (s) => ({ done: "success", installing: "warning", failed: "danger", pending: "info", booting: "warning" }[s] || "info")
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
  color: #999;
}
</style>
