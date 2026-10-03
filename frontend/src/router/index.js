import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// ── 主布局子路由 ─────────────────────────────────────────────────────────────
// 本步（信息架构第一步）只给每项加 meta.group，**路径/名称一律不动**：
// 路径迁移与重定向放到下一步，所以不存在收藏链接失效的问题。
// 菜单显示顺序由下面的 MENU_GROUP_ORDER + 组内声明顺序决定（与 children 声明序解耦）。
const layoutChildren = [
  { path: 'dashboard', name: 'dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '仪表盘', icon: 'Odometer', group: '总览' } },
  { path: 'assets', name: 'assets', component: () => import('../views/Assets.vue'), meta: { title: '资产管理', icon: 'Box', group: '基础数据' } },
  { path: 'credentials', name: 'credentials', component: () => import('../views/Credentials.vue'), meta: { title: '凭据管理', icon: 'Key', group: '基础数据' } },
  { path: 'inspection', name: 'inspection', component: () => import('../views/Inspection.vue'), meta: { title: 'CT 巡检', icon: 'Monitor', group: '网络设备' } },
  { path: 'netconfig', name: 'netconfig', component: () => import('../views/NetConfig.vue'), meta: { title: '网络配置生成', icon: 'SetUp', group: '服务器交付' } },
  { path: 'pxe', name: 'pxe', component: () => import('../views/Pxe.vue'), meta: { title: 'PXE 装机', icon: 'Cpu', group: '服务器交付' } },
  { path: 'ztp', name: 'ztp', component: () => import('../views/Ztp.vue'), meta: { title: 'ZTP 开局', icon: 'Share', group: '服务器交付' } },
  { path: 'alerts', name: 'alerts', component: () => import('../views/Alerts.vue'), meta: { title: '告警管理', icon: 'Bell', group: '网络设备' } },
  { path: 'help', name: 'help', component: () => import('../views/Help.vue'), meta: { title: '使用帮助', icon: 'Reading', group: '帮助' } },
]

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue') },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    redirect: '/dashboard',
    meta: { title: 'OpsToolkit' },
    children: layoutChildren,
  },
]

// ── 侧栏菜单数据（显式导出，MainLayout import 它） ──────────────────────────
// 不再让 MainLayout 按 routes[1].children 下标取 —— 那样以后路由数组顺序一变就静默错位。
// 这里从路由配置**本身**推导：分组顺序显式声明（MENU_GROUP_ORDER），组成员 = meta.group
// 归属该组的子路由（组内按声明顺序），菜单数据永远和路由表一致。
const MENU_GROUP_ORDER = ['总览', '网络设备', '服务器交付', '基础数据', '帮助']

export const menuGroups = MENU_GROUP_ORDER.map((group) => ({
  name: group,
  items: layoutChildren
    .filter((c) => c.meta.group === group)
    .map((c) => ({ path: `/${c.path}`, title: c.meta.title, icon: c.meta.icon })),
}))

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to, from, next) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) next({ name: 'login' })
  else if (to.name === 'login' && auth.isLoggedIn) next({ name: 'dashboard' })
  else next()
})

export default router
