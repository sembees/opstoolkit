import axios from 'axios'
import { ElMessage } from 'element-plus'

const http = axios.create({
  baseURL: '/api',
  timeout: 120000,
})

http.interceptors.request.use((config) => {
  const token = localStorage.getItem('opstk_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    if (err.response?.status === 401) {
      localStorage.removeItem('opstk_token')
      localStorage.removeItem('opstk_user')
      if (location.pathname !== '/login') location.href = '/login'
    }
    const msg = err.response?.data?.detail || err.message || '请求失败'
    ElMessage.error(msg)
    return Promise.reject(err)
  }
)

export default http


// 下载后端打包的 zip（POST，返回 blob 流）
export async function downloadZip(url, body) {
  const token = localStorage.getItem('opstk_token')
  const res = await fetch('/api' + url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (token || '') },
    body: JSON.stringify(body || {}),
  })
  if (!res.ok) {
    ElMessage.error('下载失败: ' + res.status)
    return
  }
  const blob = await res.blob()
  const objUrl = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = objUrl
  const cd = res.headers.get('content-disposition')
  a.download = cd ? (cd.match(/filename="?([^"]+)"?/) || [])[1] || 'deploy.zip' : 'deploy.zip'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(objUrl)
}
