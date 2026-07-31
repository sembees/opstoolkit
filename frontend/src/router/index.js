import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '../stores/auth'

const routes = [
  { path: '/login', name: 'login', component: () => import('../views/Login.vue') },
  {
    path: '/',
    component: () => import('../layouts/MainLayout.vue'),
    redirect: '/dashboard',
    children: [
      { path: 'dashboard', name: 'dashboard', component: () => import('../views/Dashboard.vue'), meta: { title: '仪表盘', icon: 'Odometer' } },
      { path: 'assets', name: 'assets', component: () => import('../views/Assets.vue'), meta: { title: '资产管理', icon: 'Coin' } },
      { path: 'credentials', name: 'credentials', component: () => import('../views/Credentials.vue'), meta: { title: '凭据管理', icon: 'Key' } },
      { path: 'inspection', name: 'inspection', component: () => import('../views/Inspection.vue'), meta: { title: 'CT 巡检', icon: 'Monitor' } },
      { path: 'netconfig', name: 'netconfig', component: () => import('../views/NetConfig.vue'), meta: { title: '网络配置生成', icon: 'Connection' } },
      { path: 'pxe', name: 'pxe', component: () => import('../views/Pxe.vue'), meta: { title: 'PXE 装机', icon: 'Cpu' } },
      { path: 'ztp', name: 'ztp', component: () => import('../views/Ztp.vue'), meta: { title: 'ZTP 开局', icon: 'Connection' } },
      { path: 'help', name: 'help', component: () => import('../views/Help.vue'), meta: { title: '使用帮助', icon: 'Reading' } },
    ],
  },
]

const router = createRouter({ history: createWebHistory(), routes })

router.beforeEach((to, from, next) => {
  const auth = useAuthStore()
  if (to.name !== 'login' && !auth.isLoggedIn) next({ name: 'login' })
  else if (to.name === 'login' && auth.isLoggedIn) next({ name: 'dashboard' })
  else next()
})

export default router
