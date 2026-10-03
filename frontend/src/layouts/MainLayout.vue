<template>
  <el-container style="height: 100vh">
    <el-aside width="220px" style="background: var(--ot-sider-bg)">
      <div style="height: 56px; display: flex; align-items: center; justify-content: center; color: var(--ot-text-inverse); font-size: 16px; font-weight: 700; letter-spacing: 1px;">
        OpsToolkit
      </div>
      <el-menu class="ot-sider-menu" :default-active="route.path" router style="border: none">
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="background: var(--ot-bg-container); display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--ot-border-light);">
        <span style="font-size: 16px; font-weight: 600; color: var(--ot-text-1)">{{ currentTitle }}</span>
        <el-dropdown @command="handleCommand">
          <span style="cursor: pointer; display: flex; align-items: center; gap: 6px;">
            <el-icon><User /></el-icon>
            {{ auth.user?.display_name || 'admin' }}
            <el-icon><ArrowDown /></el-icon>
          </span>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main style="background: var(--ot-bg-page); overflow-y: auto">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const menuItems = router.options.routes[1].children.map((c) => ({
  path: '/' + c.path,
  title: c.meta.title,
  icon: c.meta.icon,
}))

const currentTitle = computed(() => {
  const match = menuItems.find((m) => m.path === route.path)
  return match ? match.title : ''
})

function handleCommand(cmd) {
  if (cmd === 'logout') {
    auth.logout()
    router.push('/login')
  }
}
</script>

<style scoped>
/* 侧栏菜单配色走 Element Plus 自己的 CSS 变量，**不要**用 el-menu 的
   background-color / text-color / active-text-color 三个 prop ——
   原因是 EP 内部用 `new TinyColor(props.backgroundColor).shade(20)` 现算 hover 底色
   （node_modules/element-plus/es/components/menu/src/use-menu-color.mjs），
   往 prop 里传 `var(--x)` 会被 TinyColor 判成非法颜色，hover 底色会算成黑。
   改成 CSS 变量后完全不经过那段 JS；hover 底色由 tokens.css 的
   --ot-sider-bg-hover 显式给出（等值搬自老算法 shade(20)）。 */
.ot-sider-menu {
  --el-menu-bg-color: var(--ot-sider-bg);
  --el-menu-text-color: var(--ot-sider-text);
  --el-menu-hover-text-color: var(--ot-sider-text);
  --el-menu-hover-bg-color: var(--ot-sider-bg-hover);
  --el-menu-active-color: var(--ot-sider-active);
}
</style>
