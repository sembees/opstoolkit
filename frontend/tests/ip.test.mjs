// NetConfig 抽出的 IP 校验工具的单元测试（Node 内置 test runner，不引第三方依赖）。
// 覆盖点与 NetConfig.vue 里的前置校验口径一一对应：错误文案原文断言，防止抽模块时改语义。
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { isIpv4, parseIpCidr, ipToInt, sameSubnet, isIpv6, isIpv6Cidr } from '../src/utils/ip.js'

// ── IPv4 ────────────────────────────────────────────────────────────────
test('isIpv4: 合法 IPv4（含 trim 与全 255）', () => {
  assert.equal(isIpv4('10.0.0.1'), true)
  assert.equal(isIpv4(' 192.168.1.254 '), true)   // 原实现先 trim 再匹配
  assert.equal(isIpv4('255.255.255.255'), true)
})

test('isIpv4: 每段 >255 非法', () => {
  assert.equal(isIpv4('256.0.0.1'), false)
  assert.equal(isIpv4('10.0.0.256'), false)
})

test('isIpv4: 段数不对非法', () => {
  assert.equal(isIpv4('10.0.0'), false)
  assert.equal(isIpv4('10.0.0.1.5'), false)
  assert.equal(isIpv4('10.0.0.'), false)
})

test('isIpv4: 非数字与空值非法', () => {
  assert.equal(isIpv4('a.b.c.d'), false)
  assert.equal(isIpv4('10.0.0.x'), false)
  assert.equal(isIpv4(''), false)
  assert.equal(isIpv4(null), false)               // (s || "") 兜底，不抛异常
})

// ── parseIpCidr：返回形状 null | {ip} | {ip,cidr} | {error} ─────────────
test('parseIpCidr: 合法 10.0.0.1/24 → {ip, cidr}', () => {
  assert.deepEqual(parseIpCidr('10.0.0.1/24'), { ip: '10.0.0.1', cidr: 24 })
})

test('parseIpCidr: 裸 IP → {ip}；空值 → null', () => {
  assert.deepEqual(parseIpCidr('10.0.0.1'), { ip: '10.0.0.1' })
  assert.deepEqual(parseIpCidr(''), null)
  assert.deepEqual(parseIpCidr('   '), null)
})

test('parseIpCidr: 每段 >255 非法（错误文案原文）', () => {
  assert.deepEqual(parseIpCidr('256.0.0.1'),
    { error: 'IP「256.0.0.1」非法：应为 IPv4，如 10.0.0.1 或 10.0.0.1/24' })
})

test('parseIpCidr: 段数不对非法（错误文案原文）', () => {
  assert.deepEqual(parseIpCidr('10.0.0'),
    { error: 'IP「10.0.0」非法：应为 IPv4，如 10.0.0.1 或 10.0.0.1/24' })
})

test('parseIpCidr: 前缀 >32 非法（错误文案原文）', () => {
  assert.deepEqual(parseIpCidr('10.0.0.1/33'),
    { error: '掩码「10.0.0.1/33」非法：/ 后面的前缀必须是 0..32 的整数' })
})

test('parseIpCidr: 多余斜杠非法（错误文案原文）', () => {
  assert.deepEqual(parseIpCidr('10.0.0.1/8/8'),
    { error: 'IP「10.0.0.1/8/8」非法：只能有一个 "/"' })
})

test('parseIpCidr: 前缀 0 与 32 是合法边界', () => {
  assert.deepEqual(parseIpCidr('10.0.0.1/0'), { ip: '10.0.0.1', cidr: 0 })
  assert.deepEqual(parseIpCidr('10.0.0.1/32'), { ip: '10.0.0.1', cidr: 32 })
})

// ── ipToInt / sameSubnet ────────────────────────────────────────────────
test('ipToInt: 基本换算（>>>0 保持无符号）', () => {
  assert.equal(ipToInt('0.0.0.1'), 1)
  assert.equal(ipToInt('10.0.0.1'), 167772161)
  assert.equal(ipToInt('255.255.255.255'), 4294967295)
})

test('sameSubnet: 同网段 true、跨网段 false（/24）', () => {
  assert.equal(sameSubnet('10.0.0.1', '10.0.0.254', 24), true)
  assert.equal(sameSubnet('10.0.0.1', '10.0.1.1', 24), false)
})

test('sameSubnet: /0 边界（掩码 0，任何两址都算同网）', () => {
  assert.equal(sameSubnet('10.0.0.1', '192.168.255.254', 0), true)
})

test('sameSubnet: /32 边界（必须逐位相同）', () => {
  assert.equal(sameSubnet('10.0.0.1', '10.0.0.1', 32), true)
  assert.equal(sameSubnet('10.0.0.1', '10.0.0.2', 32), false)
})

// ── IPv6 ────────────────────────────────────────────────────────────────
test('isIpv6: 合法压缩写法（2001:db8::5、::1）', () => {
  assert.equal(isIpv6('2001:db8::5'), true)
  assert.equal(isIpv6('::1'), true)
})

test('isIpv6: 8 组无 :: 合法', () => {
  assert.equal(isIpv6('2001:0db8:0000:0000:0000:0000:0000:0005'), true)
})

test('isIpv6: 9 组非法、双 :: 非法', () => {
  assert.equal(isIpv6('1:2:3:4:5:6:7:8:9'), false)
  assert.equal(isIpv6('2001:db8::1::2'), false)
})

test('isIpv6: 非十六进制非法（2001:db8::zz）', () => {
  assert.equal(isIpv6('2001:db8::zz'), false)
})

test('isIpv6: zone-id（%eth0）非法', () => {
  assert.equal(isIpv6('fe80::1%eth0'), false)
})

test('isIpv6: IPv4 字面量非法（不做 IPv4-mapped）', () => {
  assert.equal(isIpv6('::ffff:192.168.1.1'), false)
  assert.equal(isIpv6('192.168.1.1'), false)
})

test('isIpv6Cidr: 裸地址与合法前缀（0..128 边界）', () => {
  assert.equal(isIpv6Cidr('2001:db8::1'), true)
  assert.equal(isIpv6Cidr('2001:db8::5/64'), true)
  assert.equal(isIpv6Cidr('2001:db8::5/0'), true)
  assert.equal(isIpv6Cidr('2001:db8::5/128'), true)
})

test('isIpv6Cidr: 前缀 >128 非法、多余斜杠非法', () => {
  assert.equal(isIpv6Cidr('2001:db8::1/129'), false)
  assert.equal(isIpv6Cidr('2001:db8::1/64/8'), false)
})

test('isIpv6Cidr: 非法地址带前缀也非法', () => {
  assert.equal(isIpv6Cidr('2001:db8::zz/64'), false)
})
