import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

// ── 「使用帮助」主题表（key → 中文标题，单一来源） ────────────────────────────
// 标题以 Help.vue 各 el-tab-pane 的 label 实际文案为准；唯一例外是 netconfig-usage，
// 它的 label 与 netconfig 重复（都是「网络配置生成」），面包屑标题改用该面板
// 自己的 h3「网络配置生成器使用指南」以免两个主题在面包屑里无法区分。
// Help.vue 也 import 本表做 :topic 参数校验。
export const HELP_TOPICS = {
  beginner: '小白手册',
  quick: '工具使用手册',
  inspect: 'CT 巡检',
  netconfig: '网络配置生成',
  pxe: 'PXE 装机',
  ztp: 'ZTP 开局',
  deploy: '部署指南',
  // 原为「网络配置生成器使用指南」——主题列表里会被截断成与 netconfig 同名（都是「网络配置生成」），
  // 用户分不清两个 tab；这里收短成能完整显示的区分名（slug 不变，旧链接 /help/netconfig-usage 仍有效）。
  'netconfig-usage': '配置生成器用法',
  concept: '常见概念',
}

// ── 主布局子路由 ─────────────────────────────────────────────────────────────
// 信息架构第一步给每项加了 meta.group；本步（IA 收尾）把仅有的两处页内 tab 提升为
// 真子路由（可深链 / 刷新保持 / 浏览器前进后退）：inspection 与 help 的路径字符串
// 原样保留、改为重定向入口，各自挂**复用同一组件**的子路由，靠 meta.tab 区分状态。
// 子路由通过 meta 合并继承父记录的 icon/group（route.meta = matched 逐层合并），
// title 由子路由覆盖；menuGroups 只遍历顶层记录，所以菜单仍每页一个入口。
// 菜单显示顺序由下面的 MENU_GROUP_ORDER + 组内声明顺序决定（与 children 声明序解耦）。
const layoutChildren = [
  { path: 'dashboard', name: 'dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '仪表盘', icon: 'Odometer', group: '总览' } },
  { path: 'assets', name: 'assets', component: () => import('../views/Assets.vue'), meta: { title: '资产管理', icon: 'Box', group: '基础数据' } },
  { path: 'credentials', name: 'credentials', component: () => import('../views/Credentials.vue'), meta: { title: '凭据管理', icon: 'Key', group: '基础数据' } },
  // CT 巡检：页内「执行 / 模板」两个状态路由化（Inspection.vue 的「模板」面即模板管理抽屉）
  {
    path: 'inspection',
    name: 'inspection',
    redirect: '/inspection/run',
    meta: { title: 'CT 巡检', icon: 'Monitor', group: '网络设备' },
    children: [
      { path: 'run', name: 'inspection-run', component: () => import('../views/Inspection.vue'), meta: { tab: 'run', title: 'CT 巡检 · 执行' } },
      { path: 'templates', name: 'inspection-templates', component: () => import('../views/Inspection.vue'), meta: { tab: 'templates', title: 'CT 巡检 · 模板' } },
    ],
  },
  { path: 'netconfig', name: 'netconfig', component: () => import('../views/NetConfig.vue'), meta: { title: '网络配置生成', icon: 'SetUp', group: '服务器交付' } },
  { path: 'pxe', name: 'pxe', component: () => import('../views/Pxe.vue'), meta: { title: 'PXE 装机', icon: 'Cpu', group: '服务器交付' } },
  { path: 'ztp', name: 'ztp', component: () => import('../views/Ztp.vue'), meta: { title: 'ZTP 开局', icon: 'Share', group: '服务器交付' } },
  { path: 'alerts', name: 'alerts', component: () => import('../views/Alerts.vue'), meta: { title: '告警管理', icon: 'Bell', group: '网络设备' } },
  // 首次访问引导向导：不带 meta.group ⇒ menuGroups 不会把它收进侧栏（它只在
  // "安装未完成"时由登录跳转/顶栏横幅入口出现，不该常驻菜单）。完成后进入此页
  // 只会看到"已完成"说明（后端 checks/complete 已 403 关闭）。
  { path: 'setup', name: 'setup', component: () => import('../views/Setup.vue'), meta: { title: '安装引导', icon: 'Compass' } },
  // 使用帮助：左侧 9 个主题 tab 路由化为 /help/:topic（主题 key/标题见 HELP_TOPICS）
  {
    path: 'help',
    name: 'help',
    redirect: '/help/beginner',
    meta: { title: '使用帮助', icon: 'Reading', group: '帮助' },
    children: [
      // meta.title 是 :topic 非法兜底值；合法主题的标题由全局守卫按参数覆盖
      { path: ':topic', name: 'help-topic', component: () => import('../views/Help.vue'), meta: { tab: 'topic', title: '使用帮助' } },
    ],
  },
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
  // 「使用帮助」子路由：:topic 非法 → 回落第一个主题（重定向天然替换 URL）；
  // 合法 → 把该主题的中文标题写进本次导航的 meta（供 MainLayout 面包屑第三级显示）。
  // 全局守卫每次导航都会跑（子路由同记录换参数时 beforeEnter 不重跑，这里必须放全局）；
  // to.meta 是 mergeMetaFields 每次导航现拼的新对象，改它不会污染路由记录本身。
  if (to.meta.tab === 'topic') {
    if (!HELP_TOPICS[to.params.topic]) return next('/help/beginner')
    to.meta.title = HELP_TOPICS[to.params.topic]
  }
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) next({ name: 'login' })
  else if (to.name === 'login' && auth.isLoggedIn) next({ name: 'dashboard' })
  else next()
})

export default router
