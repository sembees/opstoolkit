// oslabel.test.mjs — 锁住 OS 标签的"诚实标注"语义（node --test，零新依赖）
//
// 审计背景（2026-10-19）：verified=false 的 5 个系统（oraclelinux/kylin/uos/anolis/fedora）
// 在界面上和"验证过"的系统长得一模一样。这里把四种组合钉死，防止回退。
import test from 'node:test'
import assert from 'node:assert/strict'
import { osLabel, osVerifyHint } from '../src/utils/oslabel.js'

test('已验证 + 支持自动装机 → 只有名字', () => {
  assert.equal(osLabel({ name: 'Ubuntu', auto_install: true, verified: true }), 'Ubuntu')
})

test('未验证 + 支持自动装机 → 名字 +（未验证）', () => {
  assert.equal(osLabel({ name: '银河麒麟 Kylin', auto_install: true, verified: false }),
    '银河麒麟 Kylin（未验证）')
})

test('不支持自动装机 + 已验证 → 名字 +（暂不支持自动安装）', () => {
  assert.equal(osLabel({ name: 'Foo', auto_install: false, verified: true }),
    'Foo（暂不支持自动安装）')
})

test('两个都不满足 → 两个标注都在，用；分隔', () => {
  assert.equal(osLabel({ name: 'Bar', auto_install: false, verified: false }),
    'Bar（暂不支持自动安装；未验证）')
})

test('字段缺失时不做任何标注（老接口/本地兜底数据不能被误标）', () => {
  assert.equal(osLabel({ name: 'Baz' }), 'Baz')
  // verified 显式 false 才算"未验证"；undefined/null 都不算
  assert.equal(osLabel({ name: 'Baz', verified: null }), 'Baz')
  assert.equal(osLabel({ name: 'Baz', auto_install: null }), 'Baz')
})

test('空值不炸', () => {
  assert.equal(osLabel(null), '')
  assert.equal(osLabel({}), '')
  assert.equal(osLabel(undefined), '')
})

test('未验证提示：只在 verified===false 时出现', () => {
  assert.match(osVerifyHint({ verified: false }), /尚未真机验证/)
  assert.equal(osVerifyHint({ verified: true }), '')
  assert.equal(osVerifyHint({}), '')
  assert.equal(osVerifyHint(null), '')
})
