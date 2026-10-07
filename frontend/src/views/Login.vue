<template>
  <div class="login-bg">
    <el-card class="login-card" shadow="always">
      <div style="text-align: center; margin-bottom: 28px">
        <h1 style="font-size: 24px; color: var(--ot-primary); margin-bottom: 6px">OpsToolkit</h1>
        <p style="color: var(--ot-text-3); font-size: 14px">运维工具合集</p>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent="handleLogin">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="form.username" placeholder="请输入用户名" :prefix-icon="User" size="large" />
        </el-form-item>
        <el-form-item label="密码" prop="password">
          <el-input v-model="form.password" type="password" placeholder="请输入密码" :prefix-icon="Lock" size="large" show-password @keyup.enter="handleLogin" />
        </el-form-item>
        <el-button type="primary" size="large" style="width: 100%" :loading="loading" @click="handleLogin">登 录</el-button>
      </el-form>
      <p style="text-align: center; color: var(--ot-text-4); font-size: 12px; margin-top: 16px">管理员账号 admin（初始密码见服务日志）</p>
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive } from 'vue'
import { useRouter } from 'vue-router'
import { User, Lock } from '@element-plus/icons-vue'
import { useAuthStore } from '../stores/auth'
import { useSetupStore } from '../stores/setup'
import { ElMessage } from 'element-plus'

const router = useRouter()
const auth = useAuthStore()
const setup = useSetupStore()
const formRef = ref()
const loading = ref(false)

const form = reactive({ username: 'admin', password: '' })
const rules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

async function handleLogin() {
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    loading.value = true
    try {
      await auth.login(form.username, form.password)
      ElMessage.success('登录成功')
      // 登录后自动引导（任务书）：安装未完成 ⇒ 直接进 /setup 跟着 5 步走。
      // 状态拉不到时按"不需要引导"处理，别把人钉在登录页。
      const st = await setup.refresh()
      router.push(st.needsSetup ? '/setup' : '/dashboard')
    } catch (e) {
      // error already handled by interceptor
    } finally {
      loading.value = false
    }
  })
}
</script>

<style scoped>
.login-bg {
  height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, var(--ot-login-from) 0%, var(--ot-login-to) 50%, var(--ot-login-from) 100%);
}
.login-card {
  width: 400px;
  border-radius: 8px;
}
</style>
