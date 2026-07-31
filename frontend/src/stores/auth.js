import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import http from '../api'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('opstk_token') || '')
  const user = ref(JSON.parse(localStorage.getItem('opstk_user') || 'null'))

  const isLoggedIn = computed(() => !!token.value)

  async function login(username, password) {
    const data = await http.post('/auth/login', { username, password })
    token.value = data.access_token
    user.value = { display_name: data.display_name, role: data.role }
    localStorage.setItem('opstk_token', data.access_token)
    localStorage.setItem('opstk_user', JSON.stringify(user.value))
  }

  function logout() {
    token.value = ''
    user.value = null
    localStorage.removeItem('opstk_token')
    localStorage.removeItem('opstk_user')
  }

  return { token, user, isLoggedIn, login, logout }
})
