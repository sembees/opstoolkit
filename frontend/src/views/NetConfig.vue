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
          <el-table-column label="#" width="30">
            <template #default="{ $index }">{{ $index + 1 }}</template>
          </el-table-column>
          <el-table-column label="类型" width="68">
            <template #default="{ row }">
              <el-select v-model="row._type" size="small" @change="preview" class="ctrl-type">
                <el-option label="物理" value="iface" />
                <el-option label="Bond" value="bond" />
                <el-option label="VLAN" value="vlan" />
                <el-option label="Bridge" value="bridge" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="接口名" width="106">
            <template #default="{ row }">
              <span v-if="row._type==='vlan'" class="vlan-tag">{{ row.parent }}.{{ row.vlanId }}</span>
              <el-input v-else v-model="row.name" size="small" placeholder="eth0 / bond0 / br0" @input="preview" />
            </template>
          </el-table-column>
          <!-- 模式列 96px：要容得下 static/dhcp 文本 + 下拉箭头（70px 会显示成 "d..."） -->
          <el-table-column label="模式" width="96">
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
          <el-table-column label="IP/掩码" width="118">
            <template #default="{ row, $index }">
              <el-input v-model="row.ip" size="small" placeholder="10.0.0.1/24" @input="preview"
                        :disabled="row.mode==='dhcp'" :class="{ 'input-invalid': errorRows.has($index) }" />
            </template>
          </el-table-column>
          <el-table-column label="网关" width="94">
            <template #default="{ row }">
              <el-input v-model="row.gateway" size="small" placeholder="网关" @input="preview" :disabled="row.mode==='dhcp'" />
            </template>
          </el-table-column>
          <!-- IPv6 静态（2026-10-18）：后端三种格式（netplan/nmcli/ifcfg）都已支持，
               这里补上唯一缺的入口 —— 没入口的功能等于没做。留空=不配 IPv6。
               注意：**dhcp 行也允许填 IPv6**（DHCPv4 + 静态 IPv6 是双栈常见配法），所以不禁用。 -->
          <el-table-column label="IPv6 地址" width="132">
            <template #default="{ row }">
              <el-input v-model="row.ip6" size="small" placeholder="可选 2001:db8::5/64"
                        @input="preview" />
            </template>
          </el-table-column>
          <el-table-column label="IPv6 网关" width="132">
            <template #default="{ row }">
              <el-input v-model="row.gateway6" size="small" placeholder="可选 2001:db8::1"
                        @input="preview" />
            </template>
          </el-table-column>
          <el-table-column label="DNS" width="88">
            <template #default="{ row }">
              <el-input v-model="row.dnsStr" size="small"
                        :placeholder="dnsPlaceholder(row)"
                        @input="onDnsChange(row)" :disabled="dnsDisabled(row)" />
            </template>
          </el-table-column>
          <el-table-column label="从接口/父接口" min-width="112">
            <template #default="{ row }">
              <el-input v-if="row._type==='bond' || row._type==='bridge'" v-model="row.slavesStr" size="small" :placeholder="row._type==='bond'?'eth0,eth1':'网口名'" @input="onSlavesChange(row)" />
              <el-select v-else-if="row._type==='vlan'" v-model="row.parent" size="small" @change="onVlanParentChange(row)" class="ctrl-parent">
                <el-option v-for="iface in availableParents" :key="iface" :label="iface" :value="iface" />
              </el-select>
              <span v-else class="cell-static">-</span>
            </template>
          </el-table-column>
          <!-- Bond/VLAN 参数：改成"标签 + 控件"的两列网格。
               原先所有控件塞在一个 inline 容器里（模式/miimon/lacp/hash/primary 挤成一行再换行），
               190px 宽根本放不下，看起来一团；现在每个参数一行、标签右对齐，参数随 mode 增减也一目了然。 -->
          <el-table-column label="Bond/VLAN 参数" width="282">
            <template #default="{ row }">
              <template v-if="row._type==='bond'">
                <div class="param-grid">
                  <span class="param-label">模式</span>
                  <el-select v-model="row.bondMode" size="small" @change="onBondModeChange(row)">
                    <el-option v-for="m in meta.bond_modes" :key="m.id" :label="m.name" :value="m.id" />
                  </el-select>
                  <span class="param-label">miimon</span>
                  <el-input v-model="row.miimon" size="small" placeholder="100" @input="preview" />
                  <!-- mode 4 (802.3ad): lacp_rate -->
                  <template v-if="row.bondMode==4">
                    <span class="param-label">lacp 速率</span>
                    <el-select v-model="row.lacpRate" size="small" @change="preview">
                      <el-option label="slow" value="slow" /><el-option label="fast" value="fast" />
                    </el-select>
                  </template>
                  <!-- mode 2/4: xmit_hash_policy -->
                  <template v-if="row.bondMode==2||row.bondMode==4">
                    <span class="param-label">hash 策略</span>
                    <el-select v-model="row.xmitHash" size="small" @change="preview">
                      <el-option label="layer2" value="layer2" />
                      <el-option label="layer2+3" value="layer2+3" />
                      <el-option label="layer3+4" value="layer3+4" />
                    </el-select>
                  </template>
                  <template v-if="row.bondMode==1||row.bondMode==5||row.bondMode==6">
                    <span class="param-label">主接口</span>
                    <el-input v-model="row.primary" size="small" placeholder="如 eth0" @input="preview" />
                  </template>
                </div>
              </template>
              <template v-else-if="row._type==='vlan'">
                <div class="param-grid">
                  <span class="param-label">VLAN ID</span>
                  <el-input-number v-model="row.vlanId" size="small" :min="1" :max="4094"
                                   controls-position="right" @change="onVlanIdChange(row)" />
                </div>
              </template>
              <span v-else class="cell-static">-</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="190" fixed="right">
            <template #default="{ $index, row }">
              <!-- 复制/删除必须在同一行：`.table-actions` 抵消 EP 相邻按钮的 12px 外边距
                   （全站工具类，定义在 styles/main.css；80px 列宽 + 12px 间距本来必折行） -->
              <div class="table-actions">
                <!-- ★ 额外路由（2026-10-15）：按 netplan 的语义挂在**设备**上，
                     所以入口放在每行的操作列里；有路由时把条数显示出来，
                     否则"我明明加过路由"会因为藏在弹窗里而看不见。 -->
                <el-button link type="primary" size="small" @click="openRoutes($index)">
                  路由<template v-if="(row.routes || []).length">（{{ (row.routes || []).length }}）</template>
                </el-button>
                <el-button link type="primary" size="small" @click="dupItem($index)">复制</el-button>
                <el-button link type="danger" size="small" @click="delItem($index)">删除</el-button>
              </div>
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

    <!-- ★ 额外路由编辑（2026-10-15）。做成弹窗而不是加列：路由是"一对多"（一台设备可以
         好几条），塞进表格只会把每行撑爆。按 netplan 的语义挂在设备上。 -->
    <el-dialog v-model="routesDialog" title="额外路由" width="720px" append-to-body>
      <div class="routes-hint">
        目标填 <code>default</code>（默认路由）或网段（如 <code>10.20.0.0/16</code>，裸 IP 按 /32）；
        <code>下一跳</code>可留空（on-link 直连路由）。<b>网关</b>字段仍是默认路由的便捷写法，
        这里再加一条 <code>default</code> 会与它并存、由 metric 决定谁优先。
      </div>
      <el-table :data="routesDraft" size="small" empty-text="还没有路由，点下面的「添加一条」">
        <el-table-column label="目标" min-width="180">
          <template #default="{ row }">
            <el-input v-model="row.to" size="small" placeholder="default 或 10.20.0.0/16" />
          </template>
        </el-table-column>
        <el-table-column label="下一跳" min-width="150">
          <template #default="{ row }">
            <el-input v-model="row.via" size="small" placeholder="10.10.10.1（可空）" />
          </template>
        </el-table-column>
        <el-table-column label="metric" width="110">
          <template #default="{ row }">
            <el-input v-model="row.metric" size="small" placeholder="可空" />
          </template>
        </el-table-column>
        <el-table-column label="on-link" width="86">
          <template #default="{ row }">
            <el-switch v-model="row.on_link" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70">
          <template #default="{ $index }">
            <el-button link type="danger" size="small" @click="routesDraft.splice($index, 1)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <template #footer>
        <el-button size="small" @click="routesDraft.push({ to: '', via: '', metric: '', on_link: false })">
          添加一条
        </el-button>
        <el-button size="small" type="primary" @click="saveRoutes">保存</el-button>
        <el-button size="small" @click="routesDialog = false">取消</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted } from "vue"
import http, { flattenDetail } from "../api"
import PageHeader from "../components/PageHeader.vue"
import CardSection from "../components/CardSection.vue"
// IP 校验抽成了可单测模块（语义与原内联实现一字不改，见 src/utils/ip.js 与 tests/ip.test.mjs）
import { isIpv4, parseIpCidr, sameSubnet, isIpv6, isIpv6Cidr } from "../utils/ip.js"

const meta = reactive({ os_options: [], formats: [], bond_modes: [], netplan_renderers: [] })
const config = reactive({ os: "rhel", format: "nmcli", hostname: "", netplan_renderer: "networkd" })
const items = ref([])
const previewScript = ref("")
const previewFilename = ref("")
const previewKey = ref("")          // 生成当前预览所用的 payload 指纹（所见即所得的依据）

// ── 额外路由编辑状态（弹窗里是**草稿**：取消就不该改动表格里的行）──
const routesDialog = ref(false)
const routesDraft = ref([])
const routesIndex = ref(-1)

function openRoutes(index) {
  routesIndex.value = index
  const cur = items.value[index].routes || []
  // 深拷贝成草稿：metric 在输入框里是字符串（空串 = 未填），保存时再归一
  routesDraft.value = cur.map(r => ({
    to: r.to || "", via: r.via || "",
    metric: (r.metric === null || r.metric === undefined) ? "" : String(r.metric),
    on_link: !!r.on_link,
  }))
  routesDialog.value = true
}

function saveRoutes() {
  const rows = []
  for (const d of routesDraft.value) {
    const to = String(d.to || "").trim()
    if (!to) continue                      // 空目标的行直接丢弃（等于"没填这条"）
    const r = { to }
    const via = String(d.via || "").trim()
    if (via) r.via = via
    const m = String(d.metric ?? "").trim()
    if (m !== "") r.metric = Number(m)
    if (d.on_link) r.on_link = true
    rows.push(r)
  }
  items.value[routesIndex.value].routes = rows
  routesDialog.value = false
  preview()          // 让预览与下载立刻跟上（否则会显示"预览已失效"）
}
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
  const base = { _type: typ, name: "", mode: "static", ip: "", gateway: "", ip6: "", gateway6: "", dns: [], dnsStr: "", slaves: [], slavesStr: "", parent: "", bondMode: 1, miimon: 100, vlanId: 100, primary: "", lacpRate: "slow", xmitHash: "layer2+3", routes: [] }
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
const TYPE_LABEL = { iface: "物理接口", bond: "Bond", vlan: "VLAN", bridge: "网桥" }
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
      else if (!isIpv4(gw)) push(idx, `${label}：网关「${gw}」不是合法 IPv4`)
      else if (parsed?.ip) {
        const prefix = parsed.cidr ?? 24      // 与后端一致：未写前缀按 /24 校验
        if (!sameSubnet(parsed.ip, gw, prefix)) {
          push(idx, `${label}：网关 ${gw} 不在 ${parsed.ip}/${prefix} 子网内（生成的默认路由不可达，后端也会 422）`)
        }
      }
    }
    // IPv6 静态（2026-10-18）：与后端 fail-closed 口径一致，非法值当场拦住
    const ip6 = (row.ip6 || "").trim()
    const gw6 = (row.gateway6 || "").trim()
    // 后端缺陷 8 的规则：mode=static 必须给地址。★ 只配 IPv6 也算给了地址
    // （三种后端的 IPv6 段已与 IPv4 分支解耦，纯 IPv6 设备现在能正常生成）
    if (row.mode === "static" && !parsed?.ip && !ip6 && row._type !== "bond" && !slaves.has(row.name)) {
      push(idx, `${label}：mode=static 时 IP 必填（留空会生成一个既非 DHCP 也无地址的接口）`)
    }
    if (ip6 || gw6) {
      // ★ 2026-10-18：dhcp 行**允许**填 IPv6 —— 三种后端的 IPv6 段已与 IPv4 分支解耦，
      //   "DHCP 取 IPv4 + 静态 IPv6" 是双栈的常见配法（netplan dhcp4:true + addresses6 合法）。
      if (ip6 && !isIpv6Cidr(ip6)) {
        push(idx, `${label}：IPv6 地址「${ip6}」非法（应为 2001:db8::5 或 2001:db8::5/64）`)
      }
      if (gw6) {
        if (!ip6) push(idx, `${label}：填了 IPv6 网关就必须同时填 IPv6 地址（否则这条默认路由没有落点）`)
        else if (!isIpv6(gw6)) push(idx, `${label}：IPv6 网关「${gw6}」非法（只写裸地址，不带前缀）`)
      }
    }
    // DHCP 行的 IP 输入框已禁用，残值会在 buildPayload 里被剔除；这里不再报错，只提示残值丢弃
    if (!dnsDisabled(row)) {
      for (const d of (row.dns || [])) if (!isIpv4(d)) push(idx, `${label}：DNS「${d}」不是合法 IPv4`)
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
    // IPv6 静态（2026-10-18）：四类设备都带上；留空发 null（后端 Optional=None，不写任何 IPv6 行）
    const ip6 = dhcp ? null : _safeStr(row.ip6)
    const gw6 = dhcp ? null : _safeStr(row.gateway6)
    // ★ 额外路由（2026-10-15）：四类设备都带上；没配就不发这个键（老请求形状不变）
    const routes = (row.routes || []).length ? row.routes : undefined
    if (row._type === "iface") {
      payload.interfaces.push({ name: row.name, mode: row.mode, ip: ip, cidr: cidr, gateway: gw, ip6: ip6, gateway6: gw6, dns: dns, routes: routes })
    } else if (row._type === "bond") {
      payload.bonds.push({ name: row.name, mode: _safeInt(row.bondMode, 1), interfaces: row.slaves || [], ip: ip || "", cidr: cidr, gateway: gw, ip6: ip6, gateway6: gw6, dns: dns, miimon: _safeInt(row.miimon, 100), primary: _safeStr(row.primary), lacp_rate: row.lacpRate || null, xmit_hash_policy: row.xmitHash || null, routes: routes })
    } else if (row._type === "vlan") {
      // dns 以前被这里丢掉，界面上的 DNS 输入框成了摆设；后端现已支持
      payload.vlans.push({ parent: row.parent, vlan_id: _safeInt(row.vlanId, 100), ip: ip, cidr: cidr, gateway: gw, ip6: ip6, gateway6: gw6, dns: dns, routes: routes })
    } else if (row._type === "bridge") {
      // mode 以前完全没发出去（下拉被静默忽略）；"" 表示自动，后端按"有 IP 即静态"处理
      payload.bridges.push({ name: row.name, interfaces: row.slaves || [], mode: row.mode || null, ip: ip, cidr: cidr, gateway: gw, ip6: ip6, gateway6: gw6, dns: dns, routes: routes })
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
/* 额外路由弹窗顶部的一句说明：字段少，靠这行解释"目标/下一跳能填什么"，
   比给每列加 placeholder 更省纵向空间。 */
.routes-hint {
  margin-bottom: 8px;
  color: var(--ot-text-2);
  font-size: 12px;
  line-height: 1.6;
}
.routes-hint code {
  padding: 0 3px;
  background: var(--ot-fill-1);
  border-radius: 3px;
}

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
/* 工具栏两个下拉：定宽 100px 时被截断成「RHEL / Ro..」「nmcli 脚本 …」（用户反馈的"挤压"）。
   150px 仍差一点（截图实测仍是「RHEL / Rocky / Alm...」）⇒ 按完整文案所需给到 200px。
   工具栏右侧原本约 205px 空档（按钮右对齐），加宽后仍有余量；极窄窗口下 .toolbar 是 wrap 换行而非挤压按钮。 */
.ctrl-os { width: 200px; }
.ctrl-format { width: 130px; }
.ctrl-host { width: 120px; }
.ctrl-renderer { width: 130px; }
.ctrl-type { width: 68px; }
.ctrl-parent { width: 120px; }
/* Bond/VLAN 参数：标签 + 控件两列网格（标签右对齐、控件占满剩余宽度）。
   取代原先按控件写死的 .ctrl-* 宽度（70/42/55/110/75px）——那些宽度在 190px 列里必然换行。 */
.param-grid {
  display: grid;
  grid-template-columns: 62px minmax(0, 1fr);
  align-items: center;
  gap: var(--ot-space-1) var(--ot-space-2);
}
.param-grid .param-label { text-align: right; }
.param-grid :deep(.el-select),
.param-grid :deep(.el-input),
.param-grid :deep(.el-input-number) { width: 100%; }

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
