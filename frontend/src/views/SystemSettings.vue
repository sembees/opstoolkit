<!--
  SystemSettings.vue —— 系统设置（工具箱）

  存在的理由：把"全局开关/系统状态"从各业务页顶部收拢到一处。
  以前"装机后网络规划"的总开关长在业务页里，既不像设置，也不好找。
-->
<template>
  <div class="page">
    <h2 class="page-title">系统设置</h2>

    <el-card shadow="never" class="mt-3">
      <template #header>功能开关</template>
      <div class="row">
        <div>
          <div class="row-title">装机后网络规划</div>
          <div class="row-desc">额外功能，默认关闭；开启后才允许登记规划（每台机器还需单独开启）。</div>
        </div>
        <el-switch :model-value="planOn" :loading="saving" :disabled="!isAdmin"
                   @change="togglePlan" />
      </div>
    </el-card>

    <el-card shadow="never" class="mt-3">
      <template #header>系统状态</template>
      <el-descriptions :column="2" size="small" border>
        <el-descriptions-item label="后端"> {{ health.status || '未知' }} </el-descriptions-item>
        <el-descriptions-item label="应用"> {{ health.app || '—' }} </el-descriptions-item>
        <el-descriptions-item label="老设备 SSH 兼容">
          {{ health.legacy_ssh ? '支持' : '不支持' }}
        </el-descriptions-item>
        <el-descriptions-item label="说明"> {{ health.legacy_ssh_detail || '—' }} </el-descriptions-item>
      </el-descriptions>
      <el-button class="mt-3" :icon="Refresh" @click="loadHealth">刷新状态</el-button>
    </el-card>

    <el-card shadow="never" class="mt-3">
      <template #header>其它</template>
      <div class="links">
        <el-button link type="primary" @click="$router.push('/setup')">安装引导</el-button>
        <el-button link type="primary" @click="$router.push('/help/beginner')">使用帮助</el-button>
        <el-button link type="primary" @click="$router.push('/jobaudit')">变更审计</el-button>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Refresh } from '@element-plus/icons-vue'
import http from '../api/index.js'

const planOn = ref(false)
const saving = ref(false)
const isAdmin = ref(true)
const health = ref({})

async function loadPlan() {
  try {
    const r = await http.get('/it/nodeplan/settings', { _silent: true })
    planOn.value = !!r.data.enabled
  } catch (e) {
    /* 开关读不到不影响其它设置显示 */
  }
}

async function togglePlan(v) {
  saving.value = true
  try {
    await http.put('/it/nodeplan/settings', { enabled: v })
    planOn.value = v
    ElMessage.success(v ? '已开启（每台机器仍需单独开启）' : '已关闭')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '切换失败')
  } finally {
    saving.value = false
  }
}

async function loadHealth() {
  try {
    const r = await fetch('/health')
    health.value = r.ok ? await r.json() : {}
  } catch (e) {
    health.value = {}
  }
}

onMounted(() => {
  try {
    const u = JSON.parse(localStorage.getItem('opstk_user') || '{}')
    isAdmin.value = (u.role || '') === 'admin'
  } catch { isAdmin.value = false }
  loadPlan()
  loadHealth()
})
</script>

<style scoped>
.page { padding: 4px; }
.page-title { margin: 0; font-size: 18px; }
.mt-3 { margin-top: 12px; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
.row-title { font-size: 14px; }
.row-desc { color: var(--el-text-color-secondary); font-size: 12px; margin-top: 2px; }
.links { display: flex; gap: 8px; }
</style>
