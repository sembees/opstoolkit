// oslabel.js — OS 下拉标签的唯一构造函数（纯函数，可单测）
//
// 为什么要单独抽出来：
//   产品里有两个"诚实标注"必须同时体现在标签上：
//     · auto_install=false → 能识别 ISO，但不能自动装机（生成时明确拒绝）
//     · verified=false     → 能生成配置，但**本环境从未真机装过**
//   后者是 2026-10-19 审计发现的缺口：`verified` 早已进 API（os_catalog.py 的 os-catalog 载荷），
//   但界面上一个字都没显示 ⇒ 用户分不清"验证过"和"没验证过"。
//   抽成纯函数后可以用 node --test 锁住这四种组合，避免以后又被改回去。

/**
 * 生成 OS 下拉项与提示用的标签。
 * @param {{name?: string, auto_install?: boolean, verified?: boolean}} o
 * @returns {string} 例如 "银河麒麟 Kylin（未验证）" / "Foo（暂不支持自动安装；未验证）"
 */
export function osLabel(o) {
  if (!o || !o.name) return ''
  const notes = []
  // 用 === false 而不是 !x：字段缺失（老接口/本地兜底数据）时**不要**乱加标注
  if (o.auto_install === false) notes.push('暂不支持自动安装')
  if (o.verified === false) notes.push('未验证')
  return notes.length ? `${o.name}（${notes.join('；')}）` : o.name
}

/**
 * 选中未验证系统时界面上的那一行提示；已验证或已支持时返回空串。
 * @param {{verified?: boolean}} o
 * @returns {string}
 */
export function osVerifyHint(o) {
  if (!o || o.verified !== false) return ''
  return '该系统能生成配置，但本环境尚未真机验证过（装完请自行复核网络与 SSH）'
}
