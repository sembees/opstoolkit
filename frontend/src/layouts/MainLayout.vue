<template>
  <el-container style="height: 100vh">
    <el-aside width="220px" style="background: #001529">
      <div style="height: 56px; display: flex; align-items: center; justify-content: center; color: #fff; font-size: 16px; font-weight: 700; letter-spacing: 1px;">
        OpsToolkit
      </div>
      <el-menu :default-active="route.path" router background-color="#001529" text-color="#a6adb4" active-text-color="#fff" style="border: none">
        <el-menu-item v-for="item in menuItems" :key="item.path" :index="item.path">
          <el-icon><component :is="item.icon" /></el-icon>
          <span>{{ item.title }}</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header style="background: #fff; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid #e8e8e8;">
        <span style="font-size: 16px; font-weight: 600; color: #333">{{ currentTitle }}</span>
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
      <el-main style="background: #f0f2f5; overflow-y: auto">
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
