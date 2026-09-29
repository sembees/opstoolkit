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
    // FastAPI 的校验错误 detail 是**列表**（每项带 loc/msg）。直接丢给 ElMessage 会显示成
    // [object Object]，运维只知道"保存失败"却不知道错在哪个字段 —— 所以这里拍平成一行，
    // 并保留 loc 里的字段路径（例如 disk_config.partitions.2.fstype: 不在白名单内）。
    let msg = err.response?.data?.detail ?? err.message ?? '请求失败'
    if (Array.isArray(msg)) {
      msg = msg.map((d) => {
        if (d && d.loc) {
          const loc = d.loc.filter((x) => x !== 'body').join('.')
          return (loc ? loc + ': ' : '') + (d.msg || '')
        }
        return typeof d === 'string' ? d : JSON.stringify(d)
      }).join('；')
    } else if (msg && typeof msg === 'object') {
      msg = JSON.stringify(msg)
    }
    if (!err.config?._silent) {
      ElMessage.error(msg)
    }
    return Promise.reject(err)
  }
)

export default http


// 下载后端打包的 zip（POST，返回 blob 流）
// ★ 外部审查 U5-F3：以前失败时只弹一句"下载失败: 422"就 `return undefined`，
//   调用方却无条件提示"下载已开始"（假成功）；fetch 抛错（含 30s abort）更是没人接。
//   现在：成功返回 true / 失败返回 false 并把后端 detail 带出来，调用方必须看返回值。
export async function downloadZip(url, body) {
  const token = localStorage.getItem('opstk_token')
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), 30000)
  let res
  try {
    res = await fetch('/api' + url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: 'Bearer ' + (token || '') },
      body: JSON.stringify(body || {}),
      signal: ctrl.signal,
    })
  } catch (e) {
    clearTimeout(timer)
    ElMessage.error('下载失败：' + (e && e.name === 'AbortError'
      ? '请求超时（30s）' : ((e && e.message) || '网络错误')))
    return false
  }
  clearTimeout(timer)
  if (!res.ok) {
    let detail = ''
    try {
      const text = await res.text()
      try { detail = JSON.parse(text).detail } catch (e) { detail = text }
    } catch (e) { /* 读不出来就算了，下面给状态码 */ }
    if (Array.isArray(detail)) {
      detail = detail.map((d) => (d && d.loc ? d.loc.join('.') + ': ' : '') + ((d && d.msg) || '')).join('；')
    } else if (detail && typeof detail === 'object') {
      detail = JSON.stringify(detail)
    }
    ElMessage.error('下载失败(' + res.status + ')：' + (detail || '后端未给出原因'))
    return false
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
  return true
}
