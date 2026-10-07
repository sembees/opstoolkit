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

// FastAPI 的校验错误 detail 是**列表**（每项带 loc/msg）。直接丢给 ElMessage 会显示成
// [object Object]，运维只知道"失败"却不知道错在哪个字段 —— 这里拍平成一行，
// 并保留 loc 里的字段路径（例如 disk_config.partitions.2.fstype: 不在白名单内）。
//
// 外部审查 U5-F9：这段逻辑以前在拦截器、downloadZip、NetConfig、Ztp 各抄了一份，
// 同一个 422 在不同地方显示成不同样子（有的拍平、有的渲染成 JSON）。统一到这里。
export function flattenDetail(detail, fallback = '请求失败', sep = '；') {
  if (typeof detail === 'string') return detail || fallback
  if (Array.isArray(detail)) {
    const text = detail.map((d) => {
      if (d && d.loc) {
        const loc = d.loc.filter((x) => x !== 'body').join('.')
        return (loc ? loc + ': ' : '') + (d.msg || '')
      }
      return typeof d === 'string' ? d : JSON.stringify(d)
    }).join(sep)
    return text || fallback
  }
  if (detail && typeof detail === 'object') return JSON.stringify(detail)
  return fallback
}

http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    // 401 默认当作「登录已失效」处理（清 token + 跳登录页）。例外：改口令接口
    // POST /auth/password 用 401 表示「原口令不正确」这种业务校验失败（见
    // backend/app/api/auth.py:39）—— 请求带 _keepAuth 时不触发登出，由调用方自己
    // 弹后端 detail；否则用户输错一次原口令就被强制踢回登录页，改密码反而把人登出。
    if (err.response?.status === 401 && !err.config?._keepAuth) {
      localStorage.removeItem('opstk_token')
      localStorage.removeItem('opstk_user')
      if (location.pathname !== '/login') location.href = '/login'
    }
    const msg = flattenDetail(err.response?.data?.detail, err.message || '请求失败')
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
    // 与拦截器同一套拍平（U5-F9）
    detail = flattenDetail(detail, '')
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


// ── PXE ISO 镜像传输（冻结契约：字段名不得改动，见任务契约） ─────────────────
// 这些接口都在 /api 前缀下（baseURL 已含），路径不写 /api。
// ★ 前缀必须是 /it/pxe：后端 pxe 路由挂载在 `api_router.include_router(pxe.router, prefix="/it/pxe")`
//   （见 backend/app/api/__init__.py），与既有的 /it/pxe/iso/list 同一组。
//   ★ 踩坑记录：契约初稿把这里写成 /pxe/iso/* —— 少了 `it` 段，实测 /api/pxe/iso/list 直接 404。
// 注意：space / transfers 是 1.5s 级别的**轮询** GET —— 带 _silent: true，
// 后端未就绪（404）或不支持的平台不会每 1.5s 弹一次红色报错，只让数据保持旧值。
export function pxeIsoSpace() {
  return http.get('/it/pxe/iso/space', { _silent: true })
}

export function pxeIsoTransfers() {
  return http.get('/it/pxe/iso/transfers', { _silent: true })
}

// POST /it/pxe/iso/fetch {"url","filename","sha256"?} → 202 {"id","state"}；409/400 的
// 中文 detail 由响应拦截器统一弹出（保持非 _silent，用户必须看到失败原因）。
export function pxeIsoFetch(payload) {
  return http.post('/it/pxe/iso/fetch', payload)
}

// POST /it/pxe/iso/upload/init {"filename","size","sha256"?} → {"id","received","chunk_size"}
export function pxeIsoUploadInit(payload) {
  return http.post('/it/pxe/iso/upload/init', payload)
}

// POST /it/pxe/iso/upload/chunk（multipart：id / offset / chunk 文件字段）
// axios 在浏览器端对 FormData 会自动带上 multipart 边界，这里不手写 Content-Type。
// 分块失败要走前端自己的重试计数，_silent: true 关掉拦截器的逐次弹错，由调用方
// 在 3 次重试耗尽后给出一条明确的中文报错。
export function pxeIsoUploadChunk(id, offset, chunkBlob) {
  const fd = new FormData()
  fd.append('id', id)
  fd.append('offset', String(offset))
  fd.append('chunk', chunkBlob, 'chunk.bin')
  return http.post('/it/pxe/iso/upload/chunk', fd, { _silent: true })
}

// POST /it/pxe/iso/upload/finish {"id"} → {"ok",name,size,detected}
export function pxeIsoUploadFinish(id) {
  return http.post('/it/pxe/iso/upload/finish', { id })
}

// POST /it/pxe/iso/transfers/{id}/cancel → {"ok":true}
export function pxeIsoTransferCancel(id) {
  return http.post('/it/pxe/iso/transfers/' + encodeURIComponent(id) + '/cancel')
}
