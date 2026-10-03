<template>
  <!-- 根容器 .page（全局工具类）：纵向 flex，区块间距统一由容器 gap=16px 给出；
       高度自适应父容器（el-main 本身是有界 flex 项），不再写死"视口高度 − 顶栏高度"。
       .netconfig-page（见底部 scoped）只补 height:100% 让预览区可以 flex 撑满并内部滚动。 -->
  <div class="page netconfig-page">
    <PageHeader title="网络配置生成" desc="编辑物理接口 / Bond / VLAN / 网桥，实时生成对应发行版的网络配置脚本" />

    <!-- 上半部: 网络组件编辑表 -->
    <CardSection title="网络组件">
        <div class="row-between toolbar stack-3">
          <div class="toolbar-fields">
            <el-select v-model="config.os" size="small" class="ctrl-os" @change="preview">
              <el-option v-for="o in meta.os_options" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
            <el-select v-model="config.format" size="small" class="ctrl-format" @change="preview">
              <el-option v-for="f in meta.formats" :key="f.id" :label="f.name" :value="f.id" />
            </el-select>
            <el-input v-model="config.hostname" size="small" placeholder="主机名" class="ctrl-host" @input="preview" />
            <el-select v-if="config.format==='netplan'" v-model="config.netplan_renderer" size="small" class="ctrl-renderer" @change="preview">
              <el-option v-for="r in meta.netplan_renderers" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
          </div>
          <div>
            <el-button size="small" @click="addItem('iface')"><el-icon><Plus /></el-icon> 物理接口</el-button>
            <el-button size="small" @click="addItem('bond')"><el-icon><Plus /></el-icon> Bond</el-button>
            <el-button size="small" @click="addItem('vlan')"><el-icon><Plus /></el-icon> VLAN</el-button>
            <el-button size="small" @click="addItem('bridge')"><el-icon><Plus /></el-icon> Bridge</el-button>
            <el-button size="small" @click="doGenerate" :loading="generating" type="primary" :disabled="!metaLoaded">生成</el-button>
            <el-button size="small" @click="doDownload" :disabled="!previewValid">下载</el-button>
            <el-button size="small" type="danger" plain @click="clearAll">清空</el-button>
          </div>
        </div>

        <!-- 中11：元数据没拿到就阻止提交（不再拿硬编码默认值去提交） -->
        <!-- U5-F12：区分"还在加载"和"加载失败" —— 改前用加载中触发一次预览就会把
             "加载失败"写死，而加载成功后没人清它，红色横幅（写着"已禁止提交"）会一直挂着，
             与实际已经能生成的状态相反。 -->
        <el-alert v-if="metaLoading" type="info" :closable="false" show-icon class="stack-2"
                  title="正在加载网络配置元数据（os / 格式 选项）…" />
        <el-alert v-else-if="metaError" type="error" :closable="false" show-icon class="stack-2"
                  :title="metaError + '：已禁止提交，请刷新页面后重试'" />

        <!-- 高1：后端 422 的 detail 直接显示给运维（含 interfaces[0].gateway 这类字段路径） -->
        <el-alert v-if="serverError" type="error" :closable="false" show-icon class="stack-2">
          <template #title>生成失败（后端校验未通过），旧预览已作废</template>
          <pre class="err-mono">{{ serverError }}</pre>
        </el-alert>

        <!-- 前置校验：非法输入当场提示，不发到后端（只是体验层，边界仍在后端） -->
        <el-alert v-if="localErrors.length" type="error" :closable="false" show-icon class="stack-2">
          <template #title>参数有误，共 {{ localErrors.length }} 项，未提交</template>
          <ul class="err-list">
            <li v-for="(e, i) in localErrors" :key="i">{{ e }}</li>
          </ul>
        </el-alert>

        <!-- 跨模块一致性提示：静态 IP 落在 PXE/ZTP 的 DHCP 池内（可能两台机器同 IP）。
             只提示不拦 —— 静态 IP 与 DHCP 池在不同网段/不同现场时是正常用法。 -->
        <el-alert v-if="crossWarnings.length" type="warning" :closable="false" show-icon class="stack-2">
          <template #title>跨模块提示：静态地址与 DHCP 地址池重叠（{{ crossWarnings.length }} 项）</template>
          <ul class="err-list">
            <li v-for="(w, i) in crossWarnings" :key="i">{{ w }}</li>
          </ul>
        </el-alert>

        <el-table :data="items" size="small" stripe border max-height="380">
          <el-table-column label="#" width="36">
            <template #default="{ $index }">{{ $index + 1 }}</template>
          </el-table-column>
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-select v-model="row._type" size="small" @change="preview" class="ctrl-type">
                <el-option label="物理" value="iface" />
                <el-option label="Bond" value="bond" />
                <el-option label="VLAN" value="vlan" />
                <el-option label="Bridge" value="bridge" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="接口名" width="120">
            <template #default="{ row }">
              <span v-if="row._type==='vlan'" class="vlan-tag">{{ row.parent }}.{{ row.vlanId }}</span>
              <el-input v-else v-model="row.name" size="small" placeholder="eth0 / bond0 / br0" @input="preview" />
            </template>
          </el-table-column>
          <el-table-column label="模式" width="75">
            <template #default="{ row }">
              <!-- bond 的模式是聚合模式（在右侧参数列）；网桥可留空=自动（有 IP 即静态） -->
              <el-select v-model="row.mode" size="small" @change="preview" v-if="row._type !== 'bond' && row._type !== 'vlan'">
                <el-option v-if="row._type === 'bridge'" label="自动" value="" />
                <el-option label="static" value="static" /><el-option label="dhcp" value="dhcp" />
              </el-select>
              <span v-else-if="row._type === 'vlan'" class="cell-static">static</span>
              <span v-else class="cell-static">static</span>
            </template>
          </el-table-column>
          <el-table-column label="IP/掩码" width="140">
            <template #default="{ row, $index }">
              <el-input v-model="row.ip" size="small" placeholder="10.0.0.1/24" @input="preview"
                        :disabled="row.mode==='dhcp'" :class="{ 'input-invalid': errorRows.has($index) }" />
            </template>
          </el-table-column>
          <el-table-column label="网关" width="110">
            <template #default="{ row }">
              <el-input v-model="row.gateway" size="small" placeholder="网关" @input="preview" :disabled="row.mode==='dhcp'" />
            </template>
          </el-table-column>
          <el-table-column label="DNS" width="130">
            <template #default="{ row }">
              <el-input v-model="row.dnsStr" size="small"
                        :placeholder="dnsPlaceholder(row)"
                        @input="onDnsChange(row)" :disabled="dnsDisabled(row)" />
            </template>
          </el-table-column>
          <el-table-column label="从接口/父接口" min-width="130">
            <template #default="{ row }">
              <el-input v-if="row._type==='bond' || row._type==='bridge'" v-model="row.slavesStr" size="small" :placeholder="row._type==='bond'?'eth0,eth1':'网口名'" @input="onSlavesChange(row)" />
              <el-select v-else-if="row._type==='vlan'" v-model="row.parent" size="small" @change="onVlanParentChange(row)" class="ctrl-parent">
                <el-option v-for="iface in availableParents" :key="iface" :label="iface" :value="iface" />
              </el-select>
              <span v-else class="cell-static">-</span>
            </template>
          </el-table-column>
          <el-table-column label="Bond/VLAN参数" width="190">
            <template #default="{ row }">
              <template v-if="row._type==='bond'">
                <div class="inline-params">
                  <el-select v-model="row.bondMode" size="small" class="ctrl-bond-mode" @change="onBondModeChange(row)">
                    <el-option v-for="m in meta.bond_modes" :key="m.id" :label="m.name" :value="m.id" />
                  </el-select>
                  <span class="param-label">miimon</span>
                  <el-input v-model="row.miimon" size="small" class="ctrl-miimon" placeholder="100" @input="preview" />
                  <!-- mode 4 (802.3ad): lacp_rate -->
                  <template v-if="row.bondMode==4">
                    <span class="param-label">lacp</span>
                    <el-select v-model="row.lacpRate" size="small" class="ctrl-lacp" @change="preview">
                      <el-option label="slow" value="slow" /><el-option label="fast" value="fast" />
                    </el-select>
                  </template>
                  <!-- mode 2/4: xmit_hash_policy -->
                  <template v-if="row.bondMode==2||row.bondMode==4">
                    <span class="param-label">hash</span>
                    <el-select v-model="row.xmitHash" size="small" class="ctrl-bond-mode" @change="preview">
                      <el-option label="layer2" value="layer2" />
                      <el-option label="layer2+3" value="layer2+3" />
                      <el-option label="layer3+4" value="layer3+4" />
                    </el-select>
                  </template>
                </div>
                <div v-if="row.bondMode==1||row.bondMode==5||row.bondMode==6" class="inline-params">
                  <span class="param-label">primary</span>
                  <el-input v-model="row.primary" size="small" class="ctrl-primary" placeholder="主口" @input="preview" />
                </div>
              </template>
              <template v-else-if="row._type==='vlan'">
                <div class="inline-params">
                  <span class="param-label">ID</span>
                  <el-input-number v-model="row.vlanId" size="small" :min="1" :max="4094" class="ctrl-vlan-id" @change="onVlanIdChange(row)" controls-position="right" />
                </div>
              </template>
              <span v-else class="cell-static">-</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ $index }">
              <el-button link type="primary" size="small" @click="dupItem($index)">复制</el-button>
              <el-button link type="danger" size="small" @click="delItem($index)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>
    </CardSection>

    <!-- 下半部: 实时预览。preview-card 用 flex:1 + min-height:0 撑满页面剩余高度，
         代码块在卡体内部滚动 —— 卡片内不再留固定空高（原 height:calc(100% - 40px) 与
         内层 margin 一起造成过卡底的 60px 空带）。 -->
    <!-- 高2：预览与下载必须是同一份内容；参数改过之后旧预览立即标记失效 -->
    <CardSection title="实时预览" class="preview-card">
      <template #extra>
        <el-tag v-if="previewValid" size="small" type="success">下载内容 = 预览内容</el-tag>
        <el-tag v-else-if="previewScript" size="small" type="warning">预览已失效（参数已修改）</el-tag>
      </template>
      <pre v-if="previewScript" class="preview-block">{{ previewScript }}</pre>
      <div v-else class="preview-empty">
        {{ localErrors.length || serverError ? '当前参数未通过校验，暂无预览' : '添加接口后自动预览' }}
      </div>
    </CardSection>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from "vue"
import http, { flattenDetail } from "../api"
import PageHeader from "../components/PageHeader.vue"
import CardSection from "../components/CardSection.vue"

const meta = reactive({ os_options: [], formats: [], bond_modes: [], netplan_renderers: [] })
const config = reactive({ os: "rhel", format: "nmcli", hostname: "", netplan_renderer: "networkd" })
const items = ref([])
const previewScript = ref("")
const previewFilename = ref("")
const previewKey = ref("")          // 生成当前预览所用的 payload 指纹（所见即所得的依据）
const generating = ref(false)
const metaLoaded = ref(false)
const metaLoading = ref(true)       // U5-F12：区分"还在加载"与"加载失败"
const metaError = ref("")
const serverError = ref("")
// 跨模块一致性提示（H）：静态 IP 落在 PXE/ZTP 的 DHCP 池内 → 可能两台机器同 IP
const crossWarnings = ref([])
// U5-F11：预览请求序号（只接受最新一次请求的结果，旧请求的失败不能清掉新预览）
let previewSeq = 0
// 在途请求计数：控制"生成"按钮的 loading 状态（失败后必须能再点，见 doGenerate 的注释）
let previewInFlight = 0

let previewTimer = null
function preview() {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(doGenerate, 400)
}

function onDnsChange(row) {
  row.dns = (row.dnsStr || "").split(",").map(s => s.trim()).filter(Boolean)
  preview()
}
function onSlavesChange(row) {
  row.slaves = (row.slavesStr || "").split(",").map(s => s.trim()).filter(Boolean)
  preview()
}

const availableParents = computed(() => {
  const names = []
  for (const row of items.value) {
    if (row._type === "iface" || row._type === "bond") {
      if (row.name) names.push(row.name)
    }
  }
  return names.length ? names : ["eth0"]
})

function onVlanIdChange(row) {
  row.name = (row.parent || "eth0") + "." + (row.vlanId || 100)
  preview()
}

function onBondModeChange(row) {
  if (row.bondMode == 4) { row.lacpRate = row.lacpRate || "slow" }
  if (row.bondMode == 2 || row.bondMode == 4) { row.xmitHash = row.xmitHash || "layer2+3" }
  preview()
}

function onVlanParentChange(row) {
  row.name = (row.parent || "eth0") + "." + (row.vlanId || 100)
  preview()
}

function addItem(typ) {
  const base = { _type: typ, name: "", mode: "static", ip: "", gateway: "", dns: [], dnsStr: "", slaves: [], slavesStr: "", parent: "", bondMode: 1, miimon: 100, vlanId: 100, primary: "", lacpRate: "slow", xmitHash: "layer2+3" }
  if (typ === "iface") { base.name = "eth" + items.value.filter(i => i._type === "iface").length; base.mode = "dhcp" }
  // 网桥：默认"自动"（有 IP 即静态，无 IP 即纯二层）；bond 与 vlan 的模式不由该下拉控制
  else if (typ === "bridge") { base.name = "br" + items.value.filter(i => i._type === "bridge").length; base.mode = ""; base.slavesStr = "eth0"; onSlavesChange(base) }
  else if (typ === "bond") { base.name = "bond" + items.value.filter(i => i._type === "bond").length; base.slavesStr = "eth0,eth1"; onSlavesChange(base) }
  else if (typ === "vlan") { base.vlanId = 100; base.parent = items.value.find(i => i._type === "iface")?.name || availableParents.value[0] || "eth0"; base.name = base.parent + "." + base.vlanId }
  items.value.push(base)
  preview()
}

function dupItem(idx) { const copy = JSON.parse(JSON.stringify(items.value[idx])); copy.name = copy.name ? copy.name + "-copy" : ""; items.value.splice(idx + 1, 0, copy); preview() }
function delItem(idx) { items.value.splice(idx, 1); preview() }
function clearAll() { items.value = []; invalidatePreview() }

function _safeInt(v, d) { var n = parseInt(v); return isNaN(n) ? d : n }
function _safeStr(v) { return (v && v.trim()) ? v.trim() : null }

// ---------- 前置校验（中4 + 自由文本）：只是体验层，真正的边界在后端 ----------
const IFNAME_RE = /^[A-Za-z0-9._:-]{1,32}$/
const HOSTNAME_RE = /^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$/
const IPV4_RE = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/
const TYPE_LABEL = { iface: "物理接口", bond: "Bond", vlan: "VLAN", bridge: "网桥" }

function _isIpv4(s) {
  const m = IPV4_RE.exec((s || "").trim())
  if (!m) return false
  return m.slice(1).every(p => Number(p) <= 255)
}

// 中4：前缀必须 0..32、IP 每段必须 0..255；非法输入当场报错，绝不透传成 payload 里的坏值
function parseIpCidr(val) {
  const s = (val || "").trim()
  if (!s) return null
  const parts = s.split("/")
  if (parts.length > 2) return { error: `IP「${s}」非法：只能有一个 "/"` }
  const ip = parts[0].trim()
  if (!_isIpv4(ip)) return { error: `IP「${s}」非法：应为 IPv4，如 10.0.0.1 或 10.0.0.1/24` }
  if (parts.length === 1) return { ip }
  const p = parts[1].trim()
  if (!/^\d{1,2}$/.test(p) || Number(p) > 32) return { error: `掩码「${s}」非法：/ 后面的前缀必须是 0..32 的整数` }
  return { ip, cidr: Number(p) }
}

function _ipToInt(ip) {
  return IPV4_RE.exec(ip).slice(1).reduce((a, o) => ((a << 8) + Number(o)) >>> 0, 0) >>> 0
}
function _sameSubnet(ip, gw, prefix) {
  const mask = prefix === 0 ? 0 : (0xFFFFFFFF << (32 - prefix)) >>> 0
  return ((_ipToInt(ip) & mask) >>> 0) === ((_ipToInt(gw) & mask) >>> 0)
}
// 被 bond/bridge 引用的从接口不配地址（生成器整段跳过它）
const slaveNames = computed(() => {
  const s = new Set()
  for (const r of items.value) {
    if (r._type === "bond" || r._type === "bridge") for (const n of (r.slaves || [])) s.add(n)
  }
  return s
})
// DNS 输入框只在"会真的写出 DNS"时才启用：从接口不配 DNS，DHCP 口或填了 IP 的口才配。
// 禁用时不提交残值（后端不收到、生成器也不会静默丢弃）。
function dnsDisabled(row) {
  return slaveNames.value.has(row.name) || (!parseIpCidr(row.ip)?.ip && row.mode !== "dhcp")
}
function dnsPlaceholder(row) {
  if (slaveNames.value.has(row.name)) return "从接口不配 DNS"
  return dnsDisabled(row) ? "先填 IP" : "8.8.8.8,114.114.114.114"
}

const validation = computed(() => {
  const errs = []
  const push = (row, text) => errs.push({ row, text })
  const host = (config.hostname || "").trim()
  if (host && !HOSTNAME_RE.test(host)) push(null, `主机名「${host}」非法：只允许字母/数字/连字符（RFC1123 单标签）`)
  if (!items.value.length) push(null, "至少需要一个物理接口 / Bond / VLAN / 网桥（空配置不会生成任何网络设备）")

  const slaves = slaveNames.value

  items.value.forEach((row, idx) => {
    const label = `第 ${idx + 1} 行 ${TYPE_LABEL[row._type] || row._type}`
    if (row._type === "vlan") {
      if (!IFNAME_RE.test(row.parent || "")) push(idx, `${label}：父接口「${row.parent}」非法（只允许字母/数字/._:-）`)
      const vid = Number(row.vlanId)
      if (!Number.isInteger(vid) || vid < 1 || vid > 4094) push(idx, `${label}：VLAN ID 必须是 1..4094`)
    } else if (!IFNAME_RE.test(row.name || "")) {
      push(idx, `${label}：接口名「${row.name}」非法（只允许字母/数字/._:-，最长 32）`)
    }
    if (row._type === "bond" || row._type === "bridge") {
      const list = row.slaves || []
      if (!list.length) push(idx, `${label}：从接口不能为空`)
      for (const s of list) if (!IFNAME_RE.test(s)) push(idx, `${label}：从接口「${s}」非法`)
    }
    // 后端缺陷：primary 必须是本 bond 的成员端口（内核只认已加入该 bond 的从接口，
    // 写成别的网卡这条参数不生效，后端也会 422）
    if (row._type === "bond" && row.primary && !(row.slaves || []).includes(row.primary)) {
      push(idx, `${label}：主接口「${row.primary}」不在从接口列表里（内核要求 primary 是本 bond 的成员端口）`)
    }

    const parsed = parseIpCidr(row.ip)
    if (parsed && parsed.error) push(idx, `${label}：${parsed.error}`)
    const gw = (row.gateway || "").trim()
    if (gw) {
      if (row.mode === "dhcp") push(idx, `${label}：mode=dhcp 时网关会被丢弃，请清空网关或改为 static`)
      else if (!_isIpv4(gw)) push(idx, `${label}：网关「${gw}」不是合法 IPv4`)
      else if (parsed?.ip) {
        const prefix = parsed.cidr ?? 24      // 与后端一致：未写前缀按 /24 校验
        if (!_sameSubnet(parsed.ip, gw, prefix)) {
          push(idx, `${label}：网关 ${gw} 不在 ${parsed.ip}/${prefix} 子网内（生成的默认路由不可达，后端也会 422）`)
        }
      }
    }
    // 后端缺陷 8 的规则：mode=static 必须给 IP（bond 无 mode 开关，不在此列）
    if (row.mode === "static" && !parsed?.ip && row._type !== "bond" && !slaves.has(row.name)) {
      push(idx, `${label}：mode=static 时 IP 必填（留空会生成一个既非 DHCP 也无地址的接口）`)
    }
    // DHCP 行的 IP 输入框已禁用，残值会在 buildPayload 里被剔除；这里不再报错，只提示残值丢弃
    if (!dnsDisabled(row)) {
      for (const d of (row.dns || [])) if (!_isIpv4(d)) push(idx, `${label}：DNS「${d}」不是合法 IPv4`)
    }
  })
  return errs
})
const localErrors = computed(() => validation.value.map(e => e.text))
const errorRows = computed(() => new Set(validation.value.filter(e => e.row !== null).map(e => e.row)))

// ---------- payload / 预览 / 下载 ----------
function buildPayload() {
  const payload = { os: config.os, format: config.format, hostname: _safeStr(config.hostname), interfaces: [], bonds: [], vlans: [], bridges: [] }
  if (config.format === "netplan") payload.netplan_renderer = config.netplan_renderer
  for (const row of items.value) {
    const parsed = parseIpCidr(row.ip) || {}
    // 中5：DHCP 行的地址输入框是禁用的，残值一律不带出去（后端也会 422）
    const dhcp = row.mode === "dhcp"
    const ip = dhcp ? null : (parsed.ip || _safeStr(row.ip))
    const cidr = dhcp ? null : (parsed.cidr ?? null)
    const gw = dhcp ? null : _safeStr(row.gateway)
    // DNS 输入框被禁用时不提交残值
    const dns = dnsDisabled(row) ? [] : (row.dns || [])
    if (row._type === "iface") {
      payload.interfaces.push({ name: row.name, mode: row.mode, ip: ip, cidr: cidr, gateway: gw, dns: dns })
    } else if (row._type === "bond") {
      payload.bonds.push({ name: row.name, mode: _safeInt(row.bondMode, 1), interfaces: row.slaves || [], ip: ip || "", cidr: cidr, gateway: gw, dns: dns, miimon: _safeInt(row.miimon, 100), primary: _safeStr(row.primary), lacp_rate: row.lacpRate || null, xmit_hash_policy: row.xmitHash || null })
    } else if (row._type === "vlan") {
      // dns 以前被这里丢掉，界面上的 DNS 输入框成了摆设；后端现已支持
      payload.vlans.push({ parent: row.parent, vlan_id: _safeInt(row.vlanId, 100), ip: ip, cidr: cidr, gateway: gw, dns: dns })
    } else if (row._type === "bridge") {
      // mode 以前完全没发出去（下拉被静默忽略）；"" 表示自动，后端按"有 IP 即静态"处理
      payload.bridges.push({ name: row.name, interfaces: row.slaves || [], mode: row.mode || null, ip: ip, cidr: cidr, gateway: gw, dns: dns })
    }
  }
  return payload
}

const payloadKey = computed(() => JSON.stringify(buildPayload()))
const previewStale = computed(() => !!previewScript.value && payloadKey.value !== previewKey.value)
const previewValid = computed(() => !!previewScript.value && !previewStale.value && !serverError.value)

function invalidatePreview() {
  previewSeq++                  // 让在途请求的响应失效（U5-F11）
  previewScript.value = ""
  previewFilename.value = ""
  previewKey.value = ""
  crossWarnings.value = []      // 预览失效时提示也得跟着失效，不能留着上一轮的告警
}

// 把 FastAPI 422 的 detail（list[{loc,msg}] 或 str）拍平成一行，保留字段路径。
// U5-F9：与拦截器/下载/ZTP 部署日志共用同一实现，避免同一错误各处显示不一致。
function _errText(e) {
  return flattenDetail(e?.response?.data?.detail, e?.message || "请求失败", "\n")
}

async function doGenerate() {
  clearTimeout(previewTimer)
  serverError.value = ""
  if (!metaLoaded.value) {
    // 中11：不拿硬编码默认值去提交后端不认识的 os/format。
    // U5-F12：只有"加载已结束且失败"才写成错误 —— 加载中触发预览（刚进页面就在主机名框里
    // 打字）不能把"还在加载"当"加载失败"，而那句错误以前没人清，会一直挂在页面上。
    if (!metaLoading.value) {
      metaError.value = metaError.value || "网络配置元数据加载失败（os / 格式 下拉为空）"
    }
    invalidatePreview()
    return
  }
  if (localErrors.value.length) {
    invalidatePreview()
    return
  }
  const payload = buildPayload()
  const key = JSON.stringify(payload)
  if (key === previewKey.value && previewScript.value) return   // 参数没变，不重复请求
  // ★ 外部审查 U5-F11：请求序号。参数 A 的慢请求回来时参数早已是 B ——
  //   改前它会在 catch 里 invalidatePreview() 并报"生成失败"，把 B 的**合法**预览清掉，
  //   运维看到的是"当前参数不合法"（其实合法），还要再动一下参数才能恢复。
  const seq = ++previewSeq
  previewInFlight++                 // 用**计数**而不是序号控制 loading 状态：
  generating.value = true           // invalidatePreview() 也会推进 previewSeq，
                                    // 若用 `seq === previewSeq` 判断，一次失败就会让
                                    // 生成按钮永远停在 loading（= 永久禁用）—— 那是比原缺陷更糟的回归。
  try {
    // _silent：错误提示由下面的常驻告警统一给出，避免 toast 一闪而过
    const resp = await http.post("/it/netconfig/generate", payload, { _silent: true })
    if (seq !== previewSeq) return          // 期间又发起了新请求 ⇒ 丢弃这次响应
    previewScript.value = resp.script
    previewFilename.value = resp.filename
    previewKey.value = key
    // H：静态 IP 落在 PXE/ZTP 的 DHCP 池内 ⇒ 可能和装机中的机器撞成同一个地址。
    // 只是提示（不同网段/不同现场时是正常用法），所以不拦、不清空预览。
    crossWarnings.value = resp.warnings || []
  } catch (e) {
    if (seq !== previewSeq) return          // 旧请求的失败不能动新请求的预览
    // 高1：失败必须让运维看见，并且不能留着上一次的预览假装成功
    invalidatePreview()
    serverError.value = _errText(e)
  } finally {
    previewInFlight = Math.max(0, previewInFlight - 1)
    if (previewInFlight === 0) generating.value = false
  }
}

// 高2：下载的就是预览里那一份文本（不再发第二个请求，避免两次请求之间参数变化导致不一致）
function doDownload() {
  if (!previewValid.value) return
  const name = (config.hostname ? config.hostname + "-" : "") + previewFilename.value
  const blob = new Blob([previewScript.value], { type: "text/plain;charset=utf-8" })
  const url = URL.createObjectURL(blob)
  const a = document.createElement("a")
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

onMounted(async () => {
  try {
    const data = await http.get("/it/netconfig/meta")
    Object.assign(meta, data)
    // 下拉值一律以服务端返回的为准：服务端不认识的值绝不提交（中11）
    if (!meta.os_options.some(o => o.id === config.os)) config.os = meta.os_options[0]?.id || config.os
    if (!meta.formats.some(f => f.id === config.format)) config.format = meta.formats[0]?.id || config.format
    if (!meta.netplan_renderers.some(r => r.id === config.netplan_renderer)) config.netplan_renderer = meta.netplan_renderers[0]?.id || config.netplan_renderer
    metaLoaded.value = !!(meta.os_options.length && meta.formats.length)
    if (!metaLoaded.value) metaError.value = "后端未返回 os / format 选项"
    else metaError.value = ""      // U5-F12：加载成功必须清掉之前的错误，否则横幅常驻
  } catch (e) {
    metaError.value = "网络配置元数据加载失败（os / 格式 下拉为空）"
  } finally {
    metaLoading.value = false      // 加载结束（成功或失败）—— 之后才允许报"加载失败"
  }
  addItem("iface")
  addItem("bond")
})
</script>

<style scoped>
/* 布局：根容器 .page（全局）负责纵向 flex 与 16px 区块间距；
   这里只补 height:100% —— 高度由父容器（el-main，已是有界 flex 项）决定，
   不再写死"视口高度 − 顶栏高度"。预览卡 flex:1 撑满剩余高度并内部滚动。 */
.netconfig-page {
  height: 100%;
}

/* 工具栏：row-between（全局）给两端对齐，这里补换行与组间距（原 margin-left 链改为 gap） */
.toolbar {
  flex-wrap: wrap;
  gap: var(--ot-space-1) var(--ot-space-2);
}
.toolbar-fields {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--ot-space-2);
}

/* 表单控件定宽（原内联宽度原样收编；控件宽度不属于被审计的布局高度硬编码） */
.ctrl-os { width: 100px; }
.ctrl-format { width: 100px; }
.ctrl-host { width: 120px; }
.ctrl-renderer { width: 130px; }
.ctrl-type { width: 68px; }
.ctrl-parent { width: 120px; }
.ctrl-bond-mode { width: 70px; }
.ctrl-miimon { width: 42px; }
.ctrl-lacp { width: 55px; }
.ctrl-primary { width: 110px; }
.ctrl-vlan-id { width: 75px; }

/* 表格内的小字说明（字号统一走 token --ot-font-xs，颜色一律 var(--ot-*)） */
.vlan-tag { font-size: var(--ot-font-xs); color: var(--ot-primary); }
.cell-static { font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.param-label { font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.inline-params {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--ot-space-1);
  line-height: 1.6;
}
.inline-params + .inline-params { margin-top: var(--ot-space-1); }

/* 预览卡：卡根 flex 撑满、卡体 flex 撑满、代码块 flex 撑满 —— 三层都是自适应，
   没有任何固定空高；原来的 height:calc(100% - 40px) 是 60px 空带的根源。 */
.preview-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.preview-card :deep(.el-card__body) {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.preview-block {
  margin: 0;
  flex: 1;
  min-height: 0;
  background: var(--ot-bg-code);
  color: var(--ot-code-fg);
  padding: var(--ot-space-3) var(--ot-space-4);
  border-radius: 6px;
  font-size: var(--ot-font-xs);
  line-height: 1.5;
  white-space: pre;
  overflow: auto;
  font-family: monospace;
}
.preview-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--ot-text-4);
  text-align: center;
  padding: var(--ot-space-6);
}

.err-list { margin: var(--ot-space-1) 0 0; padding-left: 18px; font-size: var(--ot-font-xs); line-height: 1.6; }
.err-mono { margin: var(--ot-space-1) 0 0; font-size: var(--ot-font-xs); line-height: 1.6; white-space: pre-wrap; word-break: break-all; }
.input-invalid :deep(.el-input__wrapper) { box-shadow: 0 0 0 1px var(--ot-danger) inset; }
</style>
