import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import http from '../api'

// 首次访问引导向导（/setup）的全局状态。
// 谁在用：Login.vue（登录成功后按 needs_setup 决定去哪）、MainLayout（顶栏横幅），
// Setup.vue（五步向导本体，完成时 markCompleted）。
// ★ MainLayout 只用本 store 刷状态亮横幅，**绝不改路由**（2026-10-07 事故修正：
//   挂载时 router.replace('/setup') 会把每次刷新都拽进向导，等于锁死整个应用）。
// 为什么放 store：横幅要出现在"所有页面"，向导完成的状态变化要立刻反映到横幅，
// 三处共享同一份状态最省事；请求本身带 _silent —— 拉不到就当作"不需要引导"，
// 绝不能因为状态接口的一次抖动把用户钉在登录页/向导页。
export const useSetupStore = defineStore('setup', () => {
  // null = 还没拉到（不引导）；true/false = 后端给过答案
  const needsSetup = ref(null)
  const stepsDone = ref({ password_changed: false, completed: false })
  const loaded = ref(false)

  const isNeeded = computed(() => needsSetup.value === true)

  async function refresh() {
    try {
      const st = await http.get('/setup/status', { _silent: true })
      needsSetup.value = !!st.needs_setup
      stepsDone.value = {
        password_changed: !!st.steps_done?.password_changed,
        completed: !!st.steps_done?.completed,
      }
      loaded.value = true
    } catch (e) {
      // 状态接口失败（网络抖动/后端重启中）：按"不需要引导"处理，绝不能把人困住。
      needsSetup.value = false
    }
    return { needsSetup: needsSetup.value, stepsDone: stepsDone.value }
  }

  function markCompleted() {
    needsSetup.value = false
    stepsDone.value = { password_changed: true, completed: true }
  }

  function markPasswordChanged() {
    stepsDone.value = { ...stepsDone.value, password_changed: true }
  }

  return { needsSetup, stepsDone, loaded, isNeeded, refresh, markCompleted, markPasswordChanged }
})
