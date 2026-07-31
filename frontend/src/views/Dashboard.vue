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
      <template #header><span style="font-weight: 600">CT 板块 - 设备巡检概览</span></template>
      <el-table :data="ctAssets" stripe size="small">
        <el-table-column prop="name" label="设备名称" min-width="140" />
        <el-table-column prop="vendor" label="厂商" width="80" />
        <el-table-column prop="host" label="IP" width="130" />
        <el-table-column prop="device_role" label="角色" width="90" />
        <el-table-column prop="location" label="位置" width="90" />
        <el-table-column label="操作" width="100">
          <template #default>
            <el-button type="primary" link size="small" @click="$router.push('/inspection')">巡检</el-button>
          </template>
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
import { ref, onMounted } from 'vue'
import http from '../api'

const ctAssets = ref([])
const statCards = ref([
  { label: 'CT 设备', value: 0, icon: 'Monitor', color: '#1890ff' },
  { label: 'IT 服务器', value: 0, icon: 'Platform', color: '#52c41a' },
  { label: '网络配置模板', value: '2', icon: 'Connection', color: '#faad14' },
  { label: '巡检模板', value: '3厂商', icon: 'DataAnalysis', color: '#722ed1' },
])

onMounted(async () => {
  try {
    const [ct, it] = await Promise.all([
      http.get('/assets?category=ct'),
      http.get('/assets?category=it'),
    ])
    ctAssets.value = ct.slice(0, 8)
    statCards.value[0].value = ct.length
    statCards.value[1].value = it.length
  } catch (e) { /* keep defaults */ }
})
</script>
