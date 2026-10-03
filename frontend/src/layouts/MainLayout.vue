<template>
  <el-container style="height: 100vh">
    <el-aside :width="collapsed ? '64px' : '220px'" class="ot-sider">
      <div class="ot-brand">
        <span v-if="!collapsed">OpsToolkit</span>
        <span v-else>OT</span>
      </div>
      <el-menu
        class="ot-sider-menu"
        :default-active="activeMenu"
        :collapse="collapsed"
        :collapse-transition="false"
        router
      >
        <el-menu-item-group v-for="group in menuGroups" :key="group.name" :title="group.name">
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path">
            <el-icon><component :is="item.icon" /></el-icon>
            <template #title><span>{{ item.title }}</span></template>
          </el-menu-item>
        </el-menu-item-group>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="ot-header">
        <el-button
          text
          class="ot-collapse-btn"
          :title="collapsed ? '展开菜单' : '折叠菜单'"
          :aria-label="collapsed ? '展开菜单' : '折叠菜单'"
          @click="collapsed = !collapsed"
        >
          <el-icon :size="18"><component :is="collapsed ? 'Expand' : 'Fold'" /></el-icon>
        </el-button>
        <el-breadcrumb separator="/" class="ot-crumb">
          <el-breadcrumb-item>OpsToolkit</el-breadcrumb-item>
          <el-breadcrumb-item>{{ currentGroup }}</el-breadcrumb-item>
          <el-breadcrumb-item>{{ currentTitle }}</el-breadcrumb-item>
        </el-breadcrumb>
        <div class="ot-header-spacer"></div>
        <el-tag
          size="small"
          :type="isLocalHost ? 'info' : 'warning'"
          effect="plain"
          :title="`当前访问地址 ${envHost}`"
        >
          {{ isLocalHost ? '本地' : '生产' }} · {{ envHost }}
        </el-tag>
        <el-dropdown @command="handleCommand">
          <span class="ot-user">
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
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useAuthStore } from '../stores/auth'
// 菜单数据改由路由配置显式导出（见 router/index.js 的 menuGroups），
// 不再按 routes[1].children 下标取 —— 路由数组以后怎么调整都不会静默错位。
import { menuGroups } from '../router'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

// 侧栏折叠状态（组件私有；刷新回到展开态，行为可预期）
const collapsed = ref(false)

// 环境标识：只读当前访问地址（window.location.host），**不发任何后端请求**。
// host 含 127.0.0.1 或 localhost 视为本地，其余按生产提示。
const envHost = window.location.host
const isLocalHost = envHost.includes('127.0.0.1') || envHost.includes('localhost')

// 面包屑：二级 = 所属分组，三级 = 页面标题；路由匹配不到 meta 时兜底，绝不留空串。
const currentGroup = computed(() => route.meta.group || '其他')
const currentTitle = computed(
  () => route.meta.title || (typeof route.name === 'string' && route.name) || '未命名'
)

// 侧栏高亮：**必须按"所属菜单项"匹配，不能拿 route.path 精确比**。
// 为什么：页内 tab 提升为真路由后会出现子路由（/inspection/templates、/help/pxe），
// 而菜单项的 index 是父路径（/inspection、/help）——精确比较会匹配不上，
// 结果是"深链进去以后侧栏一个项都不高亮"，看着像坏了。
const activeMenu = computed(() => {
  const p = route.path
  for (const g of menuGroups) {
    for (const it of g.items) {
      if (p === it.path || p.startsWith(it.path + '/')) return it.path
    }
  }
  return p
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

.ot-sider {
  background: var(--ot-sider-bg);
  transition: width 0.2s ease;
  overflow: hidden; /* 折叠动画期间避免文字挤出侧栏 */
}

.ot-brand {
  height: 56px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ot-text-inverse);
  font-size: var(--ot-font-lg);
  font-weight: 700;
  letter-spacing: 1px;
  white-space: nowrap;
}

.ot-header {
  background: var(--ot-bg-container);
  display: flex;
  align-items: center;
  gap: var(--ot-space-3);
  border-bottom: 1px solid var(--ot-border-light);
}

.ot-collapse-btn {
  color: var(--ot-text-2);
  font-size: var(--ot-font-lg);
}

.ot-header-spacer {
  flex: 1;
}

.ot-user {
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: var(--ot-space-2);
  color: var(--ot-text-1);
}

/* ── 折叠态兜底（EP 2.14.3 实测行为，别按文档想当然） ──────────────────────
   .el-menu--collapse 的官方隐藏规则只命中「根菜单直接子级」
   （.el-menu--collapse>.el-menu-item>span 等，见 dist/index.css）；
   本项目菜单项包在 el-menu-item-group>ul 里，选择器不命中，
   且 .el-menu-item-group__title 在 collapse 下**没有任何**隐藏规则。
   不补这两条的话，64px 折叠栏会挤出「总览」「仪表盘」等文字直接破版。
   按 EP 同款方式（visibility + 零尺寸）处理，展开态完全不受影响。 */
.ot-sider-menu.el-menu--collapse :deep(.el-menu-item-group__title) {
  display: none;
}
.ot-sider-menu.el-menu--collapse :deep(.el-menu-item > span) {
  visibility: hidden;
  width: 0;
  height: 0;
  overflow: hidden;
  display: inline-block;
}
</style>
