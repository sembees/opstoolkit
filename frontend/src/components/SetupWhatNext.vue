<template>
  <div class="todo-grid">
    <div class="todo-item">
      <div class="todo-title">配 DNS / 服务器部署</div>
      <div class="todo-desc">在 PXE 装机页维护模板，部署后 dnsmasq 自动下发 DHCP/TFTP。</div>
      <el-button size="small" @click="$router.push('/pxe')">去 PXE 装机</el-button>
    </div>
    <div class="todo-item">
      <div class="todo-title">建巡检模板</div>
      <div class="todo-desc">选厂商模板或自建，一键批量巡检网络设备。</div>
      <el-button size="small" @click="$router.push('/inspection/templates')">去 CT 巡检 · 模板</el-button>
    </div>
    <div class="todo-item">
      <div class="todo-title">加资产</div>
      <div class="todo-desc">把服务器 / 网络设备录进资产库，巡检与装机都从这里取信息。</div>
      <el-button size="small" @click="$router.push('/assets')">去资产管理</el-button>
    </div>
    <div class="todo-item">
      <div class="todo-title">测通知</div>
      <div class="todo-desc">在 .env 配好飞书凭据后，巡检告警会自动推送到群；使用帮助里有逐步说明。</div>
      <el-button size="small" @click="$router.push('/help/deploy')">看部署指南</el-button>
    </div>
  </div>
</template>

<script setup>
// 安装向导第 5 步与「已完成」状态共用的「接下来做什么」清单。
// 只做跳转引导，不在这里实现任何功能（避免与各页面重复两套入口）。
</script>

<style scoped>
.todo-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: var(--ot-space-3);
}
.todo-item {
  border: 1px solid var(--ot-border-light);
  border-radius: var(--ot-radius);
  padding: var(--ot-space-3);
  display: flex;
  flex-direction: column;
  gap: var(--ot-space-2);
}
.todo-title { font-weight: 600; color: var(--ot-text-1); }
.todo-desc {
  color: var(--ot-text-2);
  font-size: var(--ot-font-sm);
  line-height: 1.7;
  flex: 1;
}
</style>
