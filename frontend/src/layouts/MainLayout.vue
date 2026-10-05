<template>
  <el-container class="ot-root">
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
              <el-dropdown-item command="password">修改密码</el-dropdown-item>
              <el-dropdown-item divided command="logout">退出登录</el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </el-header>
      <el-main class="ot-main">
        <router-view />
      </el-main>
    </el-container>

    <!-- 修改密码：后端 POST /api/auth/password 早已存在（backend/app/api/auth.py:27，
         body {old_password, new_password}，原口令不对返回 401 + detail「原口令不正确」，
         成功返回 {ok:true}），但前端一直没有入口 —— 帮助页建议"首次登录后修改密码"
         却无处可点。注意：改成功后旧 token 并不会失效（后端只更新口令哈希，无任何
         失效机制），所以不登出、不跳转，只提示下次登录用新口令。 -->
    <el-dialog v-model="pwdDialogVisible" title="修改密码" width="440px">
      <el-alert
        type="info"
        :closable="false"
        show-icon
        class="stack-3"
        title="修改成功后当前登录保持不变（旧 token 仍然有效至过期），下次登录请使用新口令。"
      />
      <!-- label-width 必须容得下最长的标签「确认新口令」+ 必填星号：
           90px 时该标签会折成两行（"确认新口"/"令"），截图实测；104px 够三行统一对齐。 -->
      <el-form ref="pwdFormRef" :model="pwdForm" :rules="pwdRules" label-width="104px">
        <el-form-item label="原口令" prop="old_password">
          <el-input v-model="pwdForm.old_password" type="password" show-password placeholder="请输入当前口令" />
        </el-form-item>
        <el-form-item label="新口令" prop="new_password">
          <el-input v-model="pwdForm.new_password" type="password" show-password placeholder="至少 8 位，且不能与原口令相同" />
        </el-form-item>
        <el-form-item label="确认新口令" prop="confirm_password">
          <el-input v-model="pwdForm.confirm_password" type="password" show-password placeholder="再输入一遍新口令" @keyup.enter="submitPwd" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="pwdDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="pwdSaving" @click="submitPwd">确认修改</el-button>
      </template>
    </el-dialog>
  </el-container>
</template>

<script setup>
import { computed, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '../stores/auth'
import http, { flattenDetail } from '../api'
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
  if (cmd === 'password') {
    openPwdDialog()
  } else if (cmd === 'logout') {
    auth.logout()
    router.push('/login')
  }
}

// ── 修改密码（顶栏用户菜单入口，见上方弹窗） ─────────────────────────
// 后端接口早已存在：POST /api/auth/password（backend/app/api/auth.py:27），
// body {old_password, new_password}；原口令不对 → 401 + detail「原口令不正确」，
// 成功 → {ok:true}。新口令约束以后端 ChangePasswordIn 为准（schemas.py:257）：
// ≥ 8 位在表单里前置校验；"不得设回历史默认值"不在前端硬编码，交由后端 422 文案提示。
const pwdDialogVisible = ref(false)
const pwdSaving = ref(false)
const pwdFormRef = ref()
const pwdForm = reactive({ old_password: '', new_password: '', confirm_password: '' })

const pwdRules = {
  old_password: [{ required: true, message: '请输入原口令', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新口令', trigger: 'blur' },
    { min: 8, message: '新口令至少 8 位', trigger: 'blur' }, // 对齐后端 ChangePasswordIn（schemas.py:268）
    {
      validator: (rule, value, cb) => {
        if (value && value === pwdForm.old_password) cb(new Error('新口令不能与原口令相同'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  confirm_password: [
    { required: true, message: '请再次输入新口令', trigger: 'blur' },
    {
      validator: (rule, value, cb) => {
        if (value && value !== pwdForm.new_password) cb(new Error('两次输入的新口令不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
}

function resetPwdForm() {
  pwdForm.old_password = ''
  pwdForm.new_password = ''
  pwdForm.confirm_password = ''
}

function openPwdDialog() {
  resetPwdForm()
  pwdFormRef.value?.clearValidate() // 清掉上一次打开残留的校验红字（首次打开时 ref 还不存在，可安全跳过）
  pwdDialogVisible.value = true
}

async function submitPwd() {
  await pwdFormRef.value.validate(async (valid) => {
    if (!valid) return
    pwdSaving.value = true
    try {
      // ★ _keepAuth：本接口的 401 表示「原口令不正确」（业务校验失败），不是会话失效。
      //   不带它会被 api/index.js 的响应拦截器当成登录过期：清 token + 踢回 /login，
      //   等于输错一次原口令就被强制登出。_silent 关掉拦截器的统一 toast，
      //   失败原因由下面显式弹出，保证展示的是后端 detail 而非 axios 通用报错。
      await http.post(
        '/auth/password',
        { old_password: pwdForm.old_password, new_password: pwdForm.new_password },
        { _keepAuth: true, _silent: true }
      )
      ElMessage.success('密码已修改')
      pwdDialogVisible.value = false
      resetPwdForm()
      // ★ 后端改口令后不使旧 token 失效（auth.py 只更新哈希后 commit，无吊销机制），
      //   当前会话照常可用 —— 按约定**不做**登出/跳登录页，弹窗里的提示交代了这一点。
    } catch (e) {
      ElMessage.error(flattenDetail(e.response?.data?.detail, e.message || '修改失败'))
    } finally {
      pwdSaving.value = false
    }
  })
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

/* 原写在 el-container / el-main 上的内联样式收进类（前端单元红线：改过的文件
   不得再出现内联 style 属性），取值与原内联完全一致，渲染不变。 */
.ot-root { height: 100vh; }
.ot-main {
  background: var(--ot-bg-page);
  overflow-y: auto;
}
</style>
