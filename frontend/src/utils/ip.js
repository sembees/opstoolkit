// 从 NetConfig.vue 抽出的 IP 校验工具（供 node --test 单测；语义与原实现一字不改）。
// 定位不变：这只是"体验层"的前置校验，真正的边界仍在后端（生成期 422）。

// 中4：前缀必须 0..32、IP 每段必须 0..255；非法输入当场报错，绝不透传成 payload 里的坏值
export const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/

export function isIpv4(s) {
  const m = IPV4_RE.exec((s || "").trim())
  if (!m) return false
  return m.slice(1).every(p => Number(p) <= 255)
}

// IPv6（2026-10-18）：只做**地址字面量**校验，规则与后端生成期的 _v6_check 对齐 ——
// ip6 允许 `addr` 或 `addr/prefix`（前缀 0..128）；gateway6 只允许裸地址（不能带前缀）。
// 不做 IPv4-mapped / zone-id（`%eth0`）这类边角，后端同样不收。
export function isIpv6(s) {
  const t = (s || "").trim()
  if (!t || t.includes("%")) return false
  const halves = t.split("::")
  if (halves.length > 2) return false
  const head = halves[0] ? halves[0].split(":") : []
  const tail = halves.length === 2 && halves[1] ? halves[1].split(":") : []
  const groups = head.concat(tail)
  if (!groups.every(g => /^[0-9A-Fa-f]{1,4}$/.test(g))) return false
  // "::" 至少压缩掉一组；没有 "::" 就必须正好 8 组
  return halves.length === 2 ? groups.length <= 7 : groups.length === 8
}

export function isIpv6Cidr(s) {
  const t = (s || "").trim()
  const i = t.indexOf("/")
  if (i < 0) return isIpv6(t)
  const p = t.slice(i + 1)
  return /^\d{1,3}$/.test(p) && Number(p) <= 128 && isIpv6(t.slice(0, i))
}

export function parseIpCidr(val) {
  const s = (val || "").trim()
  if (!s) return null
  const parts = s.split("/")
  if (parts.length > 2) return { error: `IP「${s}」非法：只能有一个 "/"` }
  const ip = parts[0].trim()
  if (!isIpv4(ip)) return { error: `IP「${s}」非法：应为 IPv4，如 10.0.0.1 或 10.0.0.1/24` }
  if (parts.length === 1) return { ip }
  const p = parts[1].trim()
  if (!/^\d{1,2}$/.test(p) || Number(p) > 32) return { error: `掩码「${s}」非法：/ 后面的前缀必须是 0..32 的整数` }
  return { ip, cidr: Number(p) }
}

export function ipToInt(ip) {
  return IPV4_RE.exec(ip).slice(1).reduce((a, o) => ((a << 8) + Number(o)) >>> 0, 0) >>> 0
}

export function sameSubnet(ip, gw, prefix) {
  const mask = prefix === 0 ? 0 : (0xFFFFFFFF << (32 - prefix)) >>> 0
  return ((ipToInt(ip) & mask) >>> 0) === ((ipToInt(gw) & mask) >>> 0)
}
