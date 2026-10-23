<!--
  NodePlan.vue —— 装机后网络规划（额外功能，默认关闭）

  用户 2026-10-21 的硬约束：这是 PXE 装机之后的**额外**功能，必须由人手动开启，不能自动。
  所以本页第一眼就是开关状态：
    · 全局关 → 顶部黄条 + 所有写操作按钮禁用（后端也会 409，前端只是提前告知）；
    · 全局开 → 可以登记规划，但每台机器还要单独打开（表格里那个开关）；
    · 只有"全局开 + 该机开"才允许生成（后端两道闸都校验，界面不重复造规则）。

  这一版只做 M1：登记 / 逐机开关 / 单机与批量 dry-run 生成（下载脚本）。
  下发（M2）与自校验（M3）不在此页，避免让人误以为点了就会改生产网络。
-->
<template>
  <div class="page">
    <div class="page-header">
      <div>
        <h2 class="page-title">装机后网络规划</h2>
        <p class="page-desc">登记机架与规划 IP/端口/路由，批量生成并下发正式配置。</p>
      </div>
      <div class="header-actions">
        <span class="switch-label">全局开关</span>
        <el-switch :model-value="globalOn" :loading="switching" :disabled="!isAdmin"
                   @change="toggleGlobal" />
        <el-button :icon="Refresh" @click="loadAll">刷新</el-button>
        <el-button :disabled="!globalOn" @click="$refs.csvInput.click()">导入 CSV</el-button>
        <input ref="csvInput" type="file" accept=".csv,text/csv" style="display:none"
               @change="onCsvPicked" />
        <el-button type="primary" :icon="Plus" :disabled="!globalOn" @click="openDialog()">
          新建规划
        </el-button>
      </div>
    </div>

    <el-alert v-if="connecting" type="info" :closable="false" show-icon
              title="正在连接后端（可能刚重启），会自动重试…" />

    <el-alert v-if="!globalOn" type="warning" :closable="false" show-icon
              title="本功能未开启"
              description="它属于额外功能：即使机器已经装好也不会自动启用。开启后仍需在每台机器上单独打开，才允许生成配置。" />

    <el-table :data="plans" stripe size="small" v-loading="loading" class="mt-3"
              @selection-change="onSelect">
      <el-table-column type="selection" width="42" />
      <el-table-column prop="serial" label="机箱 SN" min-width="150">
        <template #default="{ row }">
          <span>{{ row.serial || '—' }}</span>
          <el-tag v-if="row.serial_trusted === false" size="small" type="warning" class="ml-1">
            不可信
          </el-tag>
          <el-tag v-if="row.key_source === 'mac'" size="small" type="info" class="ml-1">按 MAC</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="rack" label="机架位置" width="120" />
      <el-table-column prop="planned_hostname" label="规划主机名" width="130" />
      <el-table-column prop="mgmt_ip" label="管理 IP" width="130" />
      <el-table-column prop="os_type" label="系统" width="80" />
      <el-table-column label="该机开启" width="90">
        <template #default="{ row }">
          <el-switch :model-value="row.enabled" :disabled="!globalOn"
                     @change="(v) => togglePlan(row, v)" />
        </template>
      </el-table-column>
      <el-table-column prop="status" label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="statusType(row.status)">{{ statusLabel(row.status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="190" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" :disabled="!canGenerate(row)"
                     @click="generateOne(row)">生成配置</el-button>
          <el-button link type="warning" size="small" :disabled="!canGenerate(row)"
                     @click="openApply(row)">下发</el-button>
          <el-button link type="success" size="small" :disabled="!row.change_id"
                     @click="syncPlan(row)">拉取结果</el-button>
          <el-button link type="primary" size="small" :disabled="!globalOn"
                     @click="openDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除这条规划？" @confirm="remove(row)">
            <template #reference>
              <el-button link type="danger" size="small" :disabled="!globalOn">删除</el-button>
            </template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>

    <div class="footer-actions mt-3">
      <el-button type="primary" :disabled="!selection.length || !globalOn" @click="generateBatch">
        批量生成（已选 {{ selection.length }} 台）
      </el-button>
      <span class="hint">批量里某台不合法只标红那一台；「谨慎模式」会整批中止。</span>
      <el-checkbox v-model="cautious" :disabled="!globalOn">谨慎模式（任一台不合法就整批中止）</el-checkbox>
    </div>

    <!-- 新建 / 编辑 -->
    <el-dialog v-model="dialogVisible" :title="form.id ? '编辑规划' : '新建规划'" width="760px">
      <el-form label-width="110px" label-position="right">
        <el-form-item label="装机记录 ID">
          <el-input v-model="form.install_id" :disabled="!!form.id" placeholder="PXE 装机记录 id（界面从机器列表进入时会自动填）" />
        </el-form-item>
        <el-form-item label="机箱 SN">
          <el-input v-model="form.serial" placeholder="dmidecode / sysfs 读到的机箱序列号（业务主键）" />
          <div class="hint">取不到 SN 时勾下面的「SN 不可信」，改用 MAC 登记。</div>
        </el-form-item>
        <el-form-item label="SN 不可信">
          <el-switch v-model="untrusted" @change="onUntrustedChange" />
        </el-form-item>
        <el-form-item v-if="untrusted" label="MAC">
          <el-input v-model="form.mac" placeholder="BC:24:11:AA:BB:CC（这时的业务键）" />
        </el-form-item>
        <el-form-item label="机架位置">
          <el-input v-model="form.rack" placeholder="A01-03-U12" />
        </el-form-item>
        <el-form-item label="规划主机名">
          <el-input v-model="form.planned_hostname" placeholder="web-01" />
        </el-form-item>
        <el-form-item label="管理 IP">
          <el-input v-model="form.mgmt_ip" placeholder="可后补" />
        </el-form-item>
        <el-form-item label="系统 / 后端">
          <el-select v-model="form.os_type" style="width: 140px">
            <el-option label="Ubuntu" value="ubuntu" />
            <el-option label="RHEL 系" value="rhel" />
          </el-select>
          <el-select v-model="form.fmt" style="width: 180px; margin-left: 8px" clearable
                     placeholder="按系统自动选">
            <el-option label="netplan（Ubuntu）" value="netplan" />
            <el-option label="nmcli" value="nmcli" />
            <el-option label="ifcfg" value="ifcfg" />
          </el-select>
        </el-form-item>

        <el-divider content-position="left">网卡 / 聚合 / VLAN / 网桥</el-divider>
        <div v-for="(it, idx) in form.interfaces" :key="idx" class="iface-row">
          <el-select v-model="it.kind" style="width: 110px">
            <el-option label="网卡" value="interface" />
            <el-option label="聚合" value="bond" />
            <el-option label="VLAN" value="vlan" />
            <el-option label="网桥" value="bridge" />
          </el-select>
          <template v-if="it.kind === 'interface'">
            <el-input v-model="it.spec.name" placeholder="eth0" style="width: 110px" />
            <el-select v-model="it.spec.mode" style="width: 100px">
              <el-option label="静态" value="static" />
              <el-option label="DHCP" value="dhcp" />
            </el-select>
            <el-input v-model="it.spec.ip" placeholder="IP" style="width: 140px" />
            <el-input-number v-model="it.spec.cidr" :min="1" :max="32" placeholder="掩码" style="width: 100px" />
            <el-input v-model="it.spec.gateway" placeholder="网关" style="width: 140px" />
            <el-button link type="primary" @click="openRoutes(idx)">
              路由（{{ (it.spec.routes || []).length }}）
            </el-button>
          </template>
          <template v-else>
            <el-input v-model="it.specText" type="textarea" :rows="2" placeholder='{"name":"bond0", ...}（生成器入参 JSON）'
                      style="flex: 1" />
          </template>
          <el-button link type="danger" @click="form.interfaces.splice(idx, 1)">删除</el-button>
        </div>
        <el-button size="small" @click="addIface">+ 添加一项</el-button>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>

    <!-- 明细路由（一个网卡可以挂多条网段） -->
    <el-dialog v-model="routesVisible" title="明细路由" width="560px">
      <el-alert type="info" :closable="false" show-icon
                title="除了默认网关，还可以给这块网卡挂多条网段"
                description="目标网段写 CIDR（如 10.20.0.0/16），下一跳可留空（表示直连/on-link）。" />
      <el-table :data="routesDraft" size="small" class="mt-3">
        <el-table-column label="目标网段">
          <template #default="{ row }"><el-input v-model="row.to" placeholder="10.20.0.0/16" /></template>
        </el-table-column>
        <el-table-column label="下一跳（可空）">
          <template #default="{ row }"><el-input v-model="row.via" placeholder="10.0.0.254" /></template>
        </el-table-column>
        <el-table-column width="70">
          <template #default="{ $index }">
            <el-button link type="danger" @click="routesDraft.splice($index, 1)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-button size="small" class="mt-3" @click="routesDraft.push({ to: '', via: '' })">
        + 添加一条
      </el-button>
      <template #footer>
        <el-button @click="routesVisible = false">取消</el-button>
        <el-button type="primary" @click="saveRoutes">保存</el-button>
      </template>
    </el-dialog>

    <!-- 下发（二次确认：默认禁止改管理口） -->
    <el-dialog v-model="applyVisible" title="下发配置到目标机" width="560px">
      <el-alert type="warning" :closable="false" show-icon
                title="这会真的改目标机的网络配置"
                description="平台只负责布置脚本并启动一次性 timer（90 秒后执行）；应用、探测、失败回滚都在目标机本地完成。" />
      <el-form label-width="130px" class="mt-3">
        <el-form-item label="目标机 IP">
          <el-input v-model="applyForm.target_ip" placeholder="留空则用规划里的管理 IP" />
        </el-form-item>
        <el-form-item label="SSH 用户">
          <el-input v-model="applyForm.ssh_user" placeholder="ops" />
        </el-form-item>
        <el-form-item label="私钥路径（容器内）">
          <el-input v-model="applyForm.ssh_key_path" placeholder="/tmp/e2e_key（必须是容器内可见的路径）" />
        </el-form-item>
        <el-form-item label="允许改管理网卡">
          <el-checkbox v-model="applyForm.allow_mgmt_change">
            我确认允许修改 SSH 所走的那块网卡（一改就可能失联，需带外兜底）
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="applyVisible = false">取消</el-button>
        <el-button type="danger" :loading="applying" :disabled="!applyForm.allow_mgmt_change"
                   @click="doApply">确认下发</el-button>
      </template>
    </el-dialog>

    <!-- 设备下发（网络设备：口令登录 + 是否保存） -->
    <el-dialog v-model="deviceApplyVisible" title="下发配置到网络设备" width="600px">
      <el-alert type="warning" :closable="false" show-icon
                title="这会真的改设备的运行配置"
                description="平台会推配置并回读对账；**默认不保存**（persist=false），对账不通过就 return 丢弃未保存配置。" />
      <el-form label-width="150px" class="mt-3">
        <el-form-item label="设备管理 IP">
          <el-input v-model="deviceForm.target_ip" placeholder="留空则用规划里的管理 IP" />
        </el-form-item>
        <el-form-item label="SSH 端口">
          <el-input-number v-model="deviceForm.ssh_port" :min="1" :max="65535" />
        </el-form-item>
        <el-form-item label="SSH 用户">
          <el-input v-model="deviceForm.ssh_user" placeholder="admin" />
        </el-form-item>
        <el-form-item label="SSH 口令">
          <el-input v-model="deviceForm.ssh_password" type="password" show-password
                    placeholder="设备本地账号口令（不走 key）" />
        </el-form-item>
        <el-form-item label="对账通过后保存">
          <el-checkbox v-model="deviceForm.persist">
            保存启动配置（save / write memory）；不勾 = 只改运行配置，重启即恢复
          </el-checkbox>
        </el-form-item>
        <el-form-item label="允许改管理口">
          <el-checkbox v-model="deviceForm.allow_mgmt_change">
            我确认允许修改管理 VLAN/管理口（一改就可能失联，需带外兜底）
          </el-checkbox>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deviceApplyVisible = false">取消</el-button>
        <el-button type="danger" :loading="applyingDevice"
                   :disabled="!deviceForm.allow_mgmt_change" @click="doApplyDevice">
          确认下发
        </el-button>
      </template>
    </el-dialog>

    <!-- 结果与逐项对账 -->
    <el-dialog v-model="syncVisible" title="下发结果与逐项对账" width="860px">
      <template v-if="syncOut">
        <el-alert v-if="!syncOut.ok" type="error" :closable="false" show-icon
                  :title="'没取到结果：' + (syncOut.reason || '目标机不可达')"
                  :description="'已尝试：' + JSON.stringify(syncOut.tried || [])" />
        <template v-else>
          <div class="sync-head">
            <el-tag :type="syncOut.status === 'applied' ? 'success' : 'warning'">
              {{ syncOut.status === 'applied' ? '已生效' : '已回滚' }}
            </el-tag>
            <span class="ml-2">目标机 {{ syncOut.host }}</span>
            <span v-if="syncOut.result?.finished_at" class="hint ml-2">
              完成于 {{ syncOut.result.finished_at }}
            </span>
          </div>

          <el-divider content-position="left">逐项对账（期望 vs 实际）</el-divider>
          <el-table :data="syncOut.verify?.items || []" size="small" border>
            <el-table-column prop="item" label="项" width="130" />
            <el-table-column label="期望" min-width="180">
              <template #default="{ row }"><code>{{ row.expected }}</code></template>
            </el-table-column>
            <el-table-column label="实际" min-width="220">
              <template #default="{ row }"><code>{{ Array.isArray(row.actual) ? row.actual.join(', ') : row.actual }}</code></template>
            </el-table-column>
            <el-table-column label="结果" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.ok ? 'success' : 'danger'">
                  {{ row.ok ? '一致' : '不一致' }}
                </el-tag>
              </template>
            </el-table-column>
          </el-table>
          <p class="hint mt-3">
            对账说明：地址/默认路由/DNS 必须与规划逐项一致才算成功——「ping 通」不作为判定依据
            （IP 写错一位也可能 ping 通网关）。目标机本地那层只决定该不该回滚。
          </p>

          <template v-if="(syncOut.errors || []).length">
            <el-divider content-position="left">被设备拒绝的命令</el-divider>
            <el-table :data="syncOut.errors" size="small" border>
              <el-table-column prop="line" label="命令" width="280" />
              <el-table-column prop="output" label="设备回显" />
            </el-table>
          </template>

          <el-collapse class="mt-3">
            <el-collapse-item title="目标机探测原文（ip -o / resolv）">
              <pre class="raw">{{ syncOut.probe_text }}</pre>
            </el-collapse-item>
            <el-collapse-item title="目标机执行日志">
              <pre class="raw">{{ syncOut.apply_log }}</pre>
            </el-collapse-item>
          </el-collapse>
        </template>
      </template>
    </el-dialog>

    <!-- 生成结果 -->
    <el-dialog v-model="resultVisible" title="生成结果（dry-run，未下发）" width="820px">
      <el-alert type="info" :closable="false" show-icon
                title="只是生成脚本，没有改任何机器"
                description="下载后人工核对，确认无误再进入下发流程（M2）。" />
      <div v-for="(it, i) in results" :key="i" class="result-row">
        <el-tag size="small" :type="it.status === 'generated' ? 'success' : 'danger'">
          {{ it.status === 'generated' ? '已生成' : '失败' }}
        </el-tag>
        <span class="ml-2">{{ it.file_name || it.plan_id }}</span>
        <span v-if="it.sha256" class="hint ml-2">sha256 {{ it.sha256.slice(0, 12) }}…</span>
        <div v-if="it.error" class="result-error">{{ it.error }}</div>
        <div v-if="it.content" class="result-content">
          <pre>{{ it.content }}</pre>
          <el-button link type="primary" size="small" @click="download(it)">下载脚本</el-button>
        </div>
      </div>
    </el-dialog>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import http from '../api/index.js'

const plans = ref([])
const loading = ref(false)
const connecting = ref(false)
const globalOn = ref(false)
const switching = ref(false)
const isAdmin = ref(true)      // 后端会再校验；这里只用于禁用按钮（下面按登录用户纠正）
const selection = ref([])
const cautious = ref(false)

const dialogVisible = ref(false)
const saving = ref(false)
const untrusted = ref(false)
const form = ref(emptyForm())

const resultVisible = ref(false)
const results = ref([])

const applyVisible = ref(false)
const applying = ref(false)
const applyTarget = ref(null)
const applyForm = ref({ target_ip: '', ssh_user: 'ops', ssh_key_path: '',
                        allow_mgmt_change: false })
const syncVisible = ref(false)
const syncOut = ref(null)

const routesVisible = ref(false)
const routesIndex = ref(-1)
const routesDraft = ref([])

const deviceApplyVisible = ref(false)
const applyingDevice = ref(false)
const deviceTarget = ref(null)
const deviceForm = ref({ target_ip: '', ssh_port: 22, ssh_user: 'admin', ssh_password: '',
                          persist: false, allow_mgmt_change: false })

function emptyForm() {
  return { id: '', install_id: '', serial: '', mac: '', rack: '', planned_hostname: '',
           mgmt_ip: '', os_type: 'ubuntu', fmt: '', interfaces: [] }
}
function addIface() {
  form.value.interfaces.push({ kind: 'interface', specText: '{}',
                               spec: { name: '', mode: 'static', ip: '', cidr: 24, gateway: '' } })
}
function onUntrustedChange(v) {
  // 不可信 SN 时业务键改走 MAC；清掉 SN 的"可信"语义由后端按 serial_trusted 处理
  if (v) form.value.serial_trusted = false
}
function onSelect(rows) { selection.value = rows }

function openRoutes(idx) {
  routesIndex.value = idx
  const cur = form.value.interfaces[idx]?.spec?.routes || []
  routesDraft.value = cur.map((r) => ({ to: r.to || '', via: r.via || '' }))
  routesVisible.value = true
}

function saveRoutes() {
  const it = form.value.interfaces[routesIndex.value]
  if (it) {
    it.spec.routes = routesDraft.value
      .filter((r) => (r.to || '').trim())
      .map((r) => ({ to: r.to.trim(), via: (r.via || '').trim() || null }))
  }
  routesVisible.value = false
}

const canGenerate = (row) => globalOn.value && row.enabled

function statusType(s) {
  return ({ applied: 'success', generated: 'info', failed: 'danger', rolled_back: 'warning' })[s] || 'info'
}
function statusLabel(s) {
  return ({ pending: '待生成', generated: '已生成', applying: '下发中',
            applied: '已生效', failed: '失败', rolled_back: '已回滚' })[s] || s
}

async function loadAll(attempt = 0) {
  loading.value = true
  try {
    // _silent：失败由这里统一提示，避免"加载失败"和拦截器的提示重复弹两次
    const st = await http.get('/it/nodeplan/settings', { _silent: true })
    globalOn.value = !!st.data.enabled
    const r = await http.get('/it/nodeplan/plans', { _silent: true })
    plans.value = r.data
    connecting.value = false
  } catch (e) {
    const detail = e?.response?.data?.detail
    // 没有 response = 网络层失败（后端重启/网络抖动）。退避重试 3 次，
    // 只有 3 次都失败才提示 —— 单次抖动不该给运维弹红条（2026-10-23 用户反馈过一次）。
    // 覆盖"后端重启"这段窗口：容器重启通常 10~20 秒，3 次×4 秒不够用。
    // 重试期间只显示轻提示（connecting），全部失败才弹错误。
    const delay = [1000, 2000, 4000, 8000, 15000][attempt]
    if (!detail && attempt < 5) {
      connecting.value = true
      await new Promise((r) => setTimeout(r, delay))
      return loadAll(attempt + 1)
    }
    connecting.value = false
    ElMessage.error(detail || '后端暂时无响应（可能正在重启），已重试 5 次；请点右上角「刷新」再试')
  } finally {
    loading.value = false
  }
}

async function toggleGlobal(v) {
  switching.value = true
  try {
    await http.put('/it/nodeplan/settings', { enabled: v })
    globalOn.value = v
    ElMessage.success(v ? '已开启（仍需逐台开启）' : '已关闭')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '切换失败')
  } finally {
    switching.value = false
  }
}

async function togglePlan(row, v) {
  try {
    await http.post(`/it/nodeplan/plans/${row.id}/${v ? 'enable' : 'disable'}`)
    row.enabled = v
    ElMessage.success(v ? '该机已开启' : '该机已关闭')
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '切换失败')
  }
}

function openDialog(row) {
  if (row) {
    form.value = {
      id: row.id, install_id: row.install_id || '', serial: row.serial || '',
      mac: row.mac || '', rack: row.rack || '', planned_hostname: row.planned_hostname || '',
      mgmt_ip: row.mgmt_ip || '', os_type: row.os_type || 'ubuntu', fmt: row.fmt || '',
      interfaces: (row.interfaces || []).map((i) => ({
        kind: i.kind,
        spec: i.kind === 'interface' ? { ...i.spec } : {},
        specText: i.kind === 'interface' ? '{}' : JSON.stringify(i.spec || {}, null, 2),
      })),
    }
    untrusted.value = row.serial_trusted === false
  } else {
    form.value = emptyForm()
    untrusted.value = false
    addIface()
  }
  dialogVisible.value = true
}

function payload() {
  const ifaces = form.value.interfaces.map((it) => {
    let spec = it.spec
    if (it.kind !== 'interface') {
      try { spec = JSON.parse(it.specText || '{}') } catch { spec = null }
      if (spec === null) throw new Error('有一项的 JSON 写错了，请检查')
    }
    return { kind: it.kind, name: (spec.name || ''), spec }
  })
  return {
    install_id: form.value.install_id || null,
    serial: form.value.serial || '',
    serial_trusted: !untrusted.value,
    key_source: untrusted.value ? 'mac' : 'serial',
    mac: form.value.mac || '', rack: form.value.rack || '',
    planned_hostname: form.value.planned_hostname || '', mgmt_ip: form.value.mgmt_ip || '',
    os_type: form.value.os_type, fmt: form.value.fmt || '', interfaces: ifaces,
  }
}

async function save() {
  saving.value = true
  try {
    const body = payload()
    if (form.value.id) await http.put(`/it/nodeplan/plans/${form.value.id}`, body)
    else await http.post('/it/nodeplan/plans', body)
    ElMessage.success('已保存')
    dialogVisible.value = false
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || e.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(row) {
  try {
    await http.delete(`/it/nodeplan/plans/${row.id}`)
    ElMessage.success('已删除')
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '删除失败')
  }
}

async function generateOne(row) {
  try {
    const r = await http.post(`/it/nodeplan/plans/${row.id}/generate`)
    results.value = [{ ...r.data, status: 'generated' }]
    resultVisible.value = true
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '生成失败')
  }
}

function openApply(row) {
  if (row.ztp_device_id) {                 // 网络设备走另一条路（没有 systemd timer）
    deviceTarget.value = row
    deviceForm.value = { target_ip: row.mgmt_ip || '', ssh_port: 22, ssh_user: 'admin',
                         ssh_password: '', persist: false, allow_mgmt_change: false }
    deviceApplyVisible.value = true
    return
  }
  applyTarget.value = row
  applyForm.value = { target_ip: row.mgmt_ip || '', ssh_user: 'ops',
                      ssh_key_path: '', allow_mgmt_change: false }
  applyVisible.value = true
}

async function doApply() {
  applying.value = true
  try {
    const r = await http.post(`/it/nodeplan/plans/${applyTarget.value.id}/apply`, applyForm.value)
    ElMessage.success(`已布置到 ${r.data.target}，90 秒后点「拉取结果」`)
    applyVisible.value = false
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '下发失败')
  } finally {
    applying.value = false
  }
}

async function doApplyDevice() {
  applyingDevice.value = true
  try {
    const r = await http.post(`/it/nodeplan/plans/${deviceTarget.value.id}/apply-device`,
                              deviceForm.value)
    const out = r.data
    syncOut.value = { ok: out.ok, status: out.status, host: deviceForm.value.target_ip,
                      result: { finished_at: '' }, verify: out.verify,
                      errors: out.errors, apply_log: out.transcript,
                      probe_text: '' }
    syncVisible.value = true
    deviceApplyVisible.value = false
    if (out.ok) ElMessage.success(out.saved ? '对账通过且已保存' : '对账通过（未保存，重启即恢复）')
    else ElMessage.warning('设备拒绝了部分命令，已丢弃未保存配置')
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '设备下发失败')
  } finally {
    applyingDevice.value = false
  }
}

async function syncPlan(row) {
  try {
    const r = await http.post(`/it/nodeplan/plans/${row.id}/sync`, {
      ssh_user: 'ops', ssh_key_path: applyForm.value.ssh_key_path || '',
    })
    syncOut.value = r.data
    syncVisible.value = true
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '拉取失败')
  }
}

async function onCsvPicked(ev) {
  const file = ev.target.files?.[0]
  ev.target.value = ""
  if (!file) return
  const text = await file.text()
  try {
    const r = await http.post('/it/nodeplan/plans/import', { csv: text })
    const out = r.data
    if (out.skipped) {
      const detail = (out.errors || []).slice(0, 8)
        .map((e) => `第 ${e.row} 行：${e.error}`).join('\n')
      ElMessage.warning(`导入完成：成功 ${out.created} 行，跳过 ${out.skipped} 行\n${detail}`)
    } else {
      ElMessage.success(`导入完成：${out.created} 行`)
    }
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '导入失败')
  }
}

async function generateBatch() {
  const ids = selection.value.map((r) => r.id)
  try {
    const r = await http.post('/it/nodeplan/jobs', {
      plan_ids: ids, skip_invalid: !cautious.value,
    })
    const job = r.data
    // 批量接口只回元数据；逐台内容需要再取一次 job 详情
    const detail = await http.get(`/it/nodeplan/jobs/${job.job_id}`)
    results.value = detail.data.items.map((it) => ({ ...it, status: it.status }))
    resultVisible.value = true
    ElMessage.success(`已生成 ${job.generated} 台，失败 ${job.failed} 台`)
    await loadAll()
  } catch (e) {
    ElMessage.error(e?.response?.data?.detail || '批量生成失败')
  }
}

function download(it) {
  const blob = new Blob([it.content || ''], { type: 'text/plain;charset=utf-8' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = it.file_name || 'config.txt'
  a.click()
  URL.revokeObjectURL(a.href)
}

onMounted(() => {
  try {
    const u = JSON.parse(localStorage.getItem('opstk_user') || '{}')
    isAdmin.value = (u.role || '') === 'admin'
  } catch { isAdmin.value = false }
  loadAll()

  // 后端重启后，用户往往只是切回这个标签页 —— 不应要求他手动点刷新。
  // 页面重新可见 / 网络恢复时，若数据还是空的就自动再拉一次（失败也不弹错）。
  const maybeReload = () => {
    if (document.visibilityState === 'visible' && plans.value.length === 0) loadAll()
  }
  document.addEventListener('visibilitychange', maybeReload)
  window.addEventListener('online', maybeReload)
  onBeforeUnmount(() => {
    document.removeEventListener('visibilitychange', maybeReload)
    window.removeEventListener('online', maybeReload)
  })
})
</script>

<style scoped>
.page { padding: 4px; }
.page-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
.page-title { margin: 0 0 4px; font-size: 18px; }
.page-desc { margin: 0; color: var(--el-text-color-secondary); font-size: 13px; }
.header-actions { display: flex; align-items: center; gap: 8px; white-space: nowrap; }
.switch-label { font-size: 13px; color: var(--el-text-color-regular); }
.mt-3 { margin-top: 12px; }
.ml-1 { margin-left: 4px; }
.ml-2 { margin-left: 8px; }
.hint { color: var(--el-text-color-secondary); font-size: 12px; }
.footer-actions { display: flex; align-items: center; gap: 12px; }
.iface-row { display: flex; align-items: flex-start; gap: 6px; margin-bottom: 8px; }
.result-row { padding: 8px 0; border-bottom: 1px dashed var(--el-border-color-light); }
.result-error { color: var(--el-color-danger); font-size: 12px; margin-top: 4px; }
.sync-head { display: flex; align-items: center; }
.raw { background: var(--el-fill-color-light); padding: 8px; max-height: 260px; overflow: auto; font-size: 12px; }
.result-content pre { background: var(--el-fill-color-light); padding: 8px; max-height: 240px; overflow: auto; font-size: 12px; }
</style>
