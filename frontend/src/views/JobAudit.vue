<!--
  JobAudit.vue —— 变更审计（config_jobs）

  为什么要有这一页：`config_jobs` / `config_job_items` 已经把"每次批量生成/下发"的
  脚本原文、sha256、逐台结果都存下来了（不可变留档），但没有界面去看 ——
  出问题时运维只能翻数据库。这一页只读：列表 + 详情（逐台状态/脚本/探测结果）。
-->
<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">变更审计</h2>
        <p class="page-desc">每次批量生成/下发都留档：谁、何时、对哪些机器、脚本原文与逐台结果。</p>
      </div>
      <el-button :icon="Refresh" @click="load">刷新</el-button>
    </div>

    <el-table :data="jobs" stripe size="small" v-loading="loading" class="mt-3">
      <el-table-column prop="created_at" label="时间" width="200" />
      <el-table-column prop="change_id" label="变更号" width="220" />
      <el-table-column prop="mode" label="模式" width="100" />
      <el-table-column prop="status" label="状态" width="110" />
      <el-table-column prop="created_by" label="操作人" width="120" />
      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openDetail(row)">逐台结果</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-dialog v-model="detailVisible" title="变更详情" width="820px">
      <el-table :data="items" size="small" border>
        <el-table-column prop="plan_id" label="规划" width="200" />
        <el-table-column prop="status" label="结果" width="110" />
        <el-table-column prop="script_name" label="脚本" width="150" />
        <el-table-column label="sha256" width="140">
          <template #default="{ row }">
            <code class="mono">{{ (row.script_sha || '').slice(0, 12) }}</code>
          </template>
        </el-table-column>
        <el-table-column prop="error" label="错误" min-width="200" />
      </el-table>
    </el-dialog>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import http from '../api/index.js'

const jobs = ref([])
const items = ref([])
const loading = ref(false)
const detailVisible = ref(false)

async function load() {
  loading.value = true
  try {
    jobs.value = (await http.get('/it/nodeplan/jobs', { _silent: true })).data
  } catch (e) {
    ElMessage.error('加载失败（后端可能正在重启），请点刷新重试')
  } finally {
    loading.value = false
  }
}

async function openDetail(row) {
  try {
    const d = (await http.get(`/it/nodeplan/jobs/${row.id}`)).data
    items.value = d.items || []
    detailVisible.value = true
  } catch (e) {
    ElMessage.error('加载详情失败')
  }
}

onMounted(load)
</script>

<style scoped>
.page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0 0 4px; font-size: 18px; }
.page-desc { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.mt-3 { margin-top: 12px; }
.mono { font-family: var(--el-font-family-mono, monospace); font-size: 12px; }
</style>
