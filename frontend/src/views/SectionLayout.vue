<!--
  SectionLayout.vue —— 合并入口的页签布局

  背景（2026-10-23 用户反馈）：导航重构时把「资产与凭据」「网络配置」合并成了父路由 + 子路由，
  但**只做了子路由、没做页签** —— 结果菜单点进去只能到第一个子页，
  `/assets/credentials` 与 `/network/single` 从界面上根本够不到（原「网络配置生成」等于被藏了）。

  做法：父路由挂这个布局，页签由 `route.meta.tabs` 声明（[{name, label}]），
  内容用 <router-view/>。这样 URL 仍是真子路由（可深链、可刷新、可前进后退）。
-->
<template>
  <div class="section">
    <el-tabs :model-value="active" @tab-change="onChange">
      <el-tab-pane v-for="t in tabs" :key="t.name" :label="t.label" :name="t.name" />
    </el-tabs>
    <router-view />
  </div>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

const tabs = computed(() => route.meta.tabs || [])
// 当前子路由的 name 就是页签名（子路由 name 与 tabs[].name 一一对应）
const active = computed(() => route.name)

function onChange(name) {
  if (name && name !== route.name) router.push({ name })
}
</script>

<style scoped>
.section :deep(.el-tabs__header) { margin-bottom: 8px; }
</style>
