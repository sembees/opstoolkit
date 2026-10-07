<template>
  <div class="page">
    <PageHeader title="安装引导" :desc="doneSummary" />

    <!-- 已完成（复访 /setup）：后端已把体检/完成接口关掉，这里只给"接下来做什么" -->
    <CardSection v-if="alreadyDone" title="安装引导已完成" icon="CircleCheckFilled">
      <el-alert
        type="success"
        :closable="false"
        show-icon
        class="stack-4"
        title="安装引导已完成。之后系统直接进入工作状态，不再出现本向导；顶栏也不会再有提醒。"
      />
      <SetupWhatNext class="stack-4" />
      <div class="row-between next-bar">
        <span class="text-muted">以后想再检查运行环境，可在宿主机执行 packaging/doctor.sh；本页的体检接口完成后已关闭。</span>
        <el-button @click="$router.push('/pxe')">前往 PXE 装机</el-button>
      </div>
    </CardSection>

    <!-- 五步向导 -->
    <template v-else>
      <CardSection icon="Compass" title="跟着这 5 步走完，系统就能正式使用">
        <el-steps :active="finishDone ? 5 : step" align-center finish-status="success" class="setup-steps">
          <el-step title="改管理员口令" description="安装脚本只打印一次初始口令" />
          <el-step title="环境体检" description="端口、目录、磁盘一键检查" />
          <el-step title="添加镜像" description="放入系统 ISO（三种方式任选）" />
          <el-step title="网络准备" description="识别管理网卡与网段（只读）" />
          <el-step title="完成" description="关闭向导，开始使用" />
        </el-steps>
      </CardSection>

      <CardSection :title="stepTitles[step]" :icon="stepIcons[step]">
        <!-- ── 第 1 步：改管理员口令 ─────────────────────────────────────── -->
        <template v-if="step === 0">
          <el-alert
            v-if="setup.stepsDone.password_changed"
            type="success"
            :closable="false"
            show-icon
            class="stack-4"
            title="检测到初始口令已经修改过，本步可以直接进下一页。如需更换，仍可在下方重新修改。"
          />
          <el-alert
            v-else
            type="warning"
            :closable="false"
            show-icon
            class="stack-4"
            title="为什么要改：初始口令只在服务日志里打印过一次，谁拿到日志谁就等于拿到了管理员权限。"
            description="改完这一步，后面 4 步才会放开（不修改不允许进入下一步）。取初始口令：docker logs opstoolkit 2>&1 | grep 初始随机口令"
          />
          <el-form
            ref="pwdFormRef"
            :model="pwdForm"
            :rules="pwdRules"
            label-width="104px"
            class="setup-form"
            @submit.prevent
          >
            <el-form-item label="原口令" prop="old_password">
              <el-input
                v-model="pwdForm.old_password"
                type="password"
                show-password
                placeholder="粘贴日志里打印的初始随机口令"
              />
            </el-form-item>
            <el-form-item label="新口令" prop="new_password">
              <el-input
                v-model="pwdForm.new_password"
                type="password"
                show-password
                placeholder="至少 8 位；不要用 admin@123 之类的历史默认值"
              />
            </el-form-item>
            <el-form-item label="确认新口令" prop="confirm_password">
              <el-input
                v-model="pwdForm.confirm_password"
                type="password"
                show-password
                placeholder="再输入一遍新口令"
                @keyup.enter="submitPassword"
              />
            </el-form-item>
          </el-form>
          <div class="form-actions">
            <el-button type="primary" :loading="pwdSaving" @click="submitPassword">
              {{ setup.stepsDone.password_changed ? '再次修改口令' : '确认修改口令' }}
            </el-button>
            <span v-if="passwordOk" class="pwd-ok">口令已生效，可以进入下一步。</span>
          </div>
        </template>

        <!-- ── 第 2 步：环境体检 ─────────────────────────────────────────── -->
        <template v-if="step === 1">
          <div class="row-between stack-3">
            <span class="text-muted">体检只读不改：每一条都会告诉你「为什么 / 怎么修」，不会替你动任何东西。</span>
            <el-button size="small" :loading="checksLoading" @click="loadChecks(true)">重新体检</el-button>
          </div>
          <el-alert
            v-if="checksForbidden"
            type="warning"
            :closable="false"
            show-icon
            class="stack-4"
            title="体检与完成引导需要管理员（admin）账号。请用管理员登录后再回到本页。"
          />
          <template v-else>
            <el-alert
              v-if="errorCount > 0"
              type="error"
              :closable="false"
              show-icon
              class="stack-3"
              :title="`有 ${errorCount} 项「需处理」—— 照每条下面的命令修好后点「重新体检」，全绿（或只剩黄色注意）才能点下一步。`"
            />
            <el-alert
              v-else
              type="success"
              :closable="false"
              show-icon
              class="stack-3"
              title="没有「需处理」的项了。黄色的「注意」不挡使用，可以以后再配。"
            />
            <div class="check-list">
              <div v-for="it in checks" :key="it.id" class="check-item">
                <div class="check-head">
                  <el-tag size="small" :type="levelMeta(it.level).tag" effect="light">
                    {{ levelMeta(it.level).label }}
                  </el-tag>
                  <span class="check-title">{{ it.title }}</span>
                </div>
                <div class="check-detail">{{ it.detail }}</div>
                <div v-if="it.fix" class="check-fix">
                  <code class="check-cmd">{{ it.fix }}</code>
                  <el-button size="small" text type="primary" @click="copyText(it.fix)">复制</el-button>
                </div>
              </div>
            </div>
          </template>
        </template>

        <!-- ── 第 3 步：添加镜像 ─────────────────────────────────────────── -->
        <template v-if="step === 2">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="stack-4"
            title="这一步只是引导，不在这里传文件 —— 添加镜像有三个入口，选一个顺手的就行。"
          />
          <div class="howto-title">方式一（推荐）：在页面里添加</div>
          <div class="howto-desc">
            去 PXE 装机页的「ISO 镜像管理」卡片点「添加镜像」：支持从 URL 拉取，
            也可以从你自己的电脑分块上传。添加后记得点「提取」，装机才用得上。
          </div>
          <div class="stack-4">
            <el-button type="primary" size="small" @click="$router.push('/pxe')">前往 PXE 装机 → 添加镜像</el-button>
          </div>
          <div class="howto-title">方式二：scp 直接放到服务器（大文件最快）</div>
          <div class="howto-cmd-row stack-3">
            <code class="howto-cmd">scp 你的镜像.iso root@&lt;本机IP&gt;:/srv/opstk/iso/</code>
            <el-button size="small" text type="primary" @click="copyText(scpCmd)">复制</el-button>
          </div>
          <div class="howto-title">方式三：先看空间再决定</div>
          <div class="howto-cmd-row stack-3">
            <span>当前各目录的可用空间（只读展示；改路径请改部署 .env，别在这里改）：</span>
            <el-button size="small" text type="primary" :loading="storageLoading" @click="loadStorage">刷新</el-button>
          </div>
          <el-table v-if="storageDirs.length" :data="storageDirs" size="small">
            <el-table-column prop="name" label="目录" min-width="150" />
            <el-table-column prop="path" label="路径" min-width="170" show-overflow-tooltip />
            <el-table-column label="状态" min-width="140">
              <template #default="{ row }">
                <span v-if="!row.exists" class="st-err">不存在</span>
                <span v-else-if="row.writable_required && !row.writable" class="st-err">不可写</span>
                <span v-else-if="!row.writable" class="st-muted">只读挂载（正常）</span>
                <span v-else class="st-ok">可写</span>
              </template>
            </el-table-column>
            <el-table-column label="可用 / 总量" min-width="150">
              <template #default="{ row }">
                <span v-if="row.exists">{{ fmtBytes(row.free_bytes) }} / {{ fmtBytes(row.total_bytes) }}</span>
                <span v-else class="st-muted">—</span>
              </template>
            </el-table-column>
          </el-table>
        </template>

        <!-- ── 第 4 步：网络准备 ─────────────────────────────────────────── -->
        <template v-if="step === 3">
          <el-alert
            type="info"
            :closable="false"
            show-icon
            class="stack-4"
            title="这一步只读展示本机的网络识别结果，什么都不会改。核对无误继续即可。"
          />
          <template v-if="net && Object.keys(net).length">
            <div class="net-grid">
              <div class="net-item"><span class="net-k">管理网卡</span><span>{{ net.interface || '—' }}</span></div>
              <div class="net-item"><span class="net-k">本机 IP</span><span>{{ net.server_ip || '—' }}</span></div>
              <div class="net-item"><span class="net-k">默认网关</span><span>{{ net.gateway || '—' }}</span></div>
              <div class="net-item">
                <span class="net-k">建议 DHCP 范围</span>
                <span v-if="net.dhcp_start && net.dhcp_end">{{ net.dhcp_start }} ~ {{ net.dhcp_end }}</span>
                <span v-else>未取得（后续可在 PXE 模板里手填）</span>
              </div>
            </div>
            <div v-if="netWarnings.length" class="net-warn-list stack-3">
              <div v-for="(w, i) in netWarnings" :key="i" class="net-warn">· {{ w }}</div>
            </div>
          </template>
          <el-alert
            v-else
            type="warning"
            :closable="false"
            show-icon
            class="stack-3"
            title="未探测到网络信息（需要 Linux 环境）。不影响完成引导，装 PXE 时再按页面提示填写。"
          />
          <div class="precond">
            <div class="section-title stack-2">本机部署 PXE 的前置条件（照着核对就行）：</div>
            <div class="precond-item">1. 装机网段里没有别的 DHCP 服务器（两台抢答会让装机随机失败）；</div>
            <div class="precond-item">2. 宿主机已执行过一次 deploy/host/install-opstk-dnsmasq-reload.sh（装重载单元）；</div>
            <div class="precond-item">3. 镜像已放进 ISO 目录并点过「提取」（上一步的引导）；</div>
            <div class="precond-item">4. 要装机的机器与这台服务器在同一广播域（二层可达）。</div>
          </div>
        </template>

        <!-- ── 第 5 步：完成 ─────────────────────────────────────────────── -->
        <template v-if="step === 4">
          <template v-if="!finishDone">
            <el-alert
              type="info"
              :closable="false"
              show-icon
              class="stack-4"
              title="点下面的按钮完成安装引导：系统会记录完成时间并关闭向导（之后这些接口一律 403，不留后门）。"
            />
            <div class="finish-list">
              <div class="finish-item">① 管理员口令：{{ passwordOk ? '已修改' : '尚未修改（回第 1 步）' }}</div>
              <div class="finish-item">② 环境体检：{{ checksErrorCount === null ? '尚未体检（回第 2 步）' : (errorCount === 0 ? '无「需处理」项' : `仍有 ${errorCount} 项需处理（回第 2 步）`) }}</div>
            </div>
            <el-button
              type="primary"
              size="large"
              :loading="finishSaving"
              :disabled="finishBlocked"
              @click="finish"
            >
              完成安装引导
            </el-button>
            <div v-if="finishBlocked" class="finish-blocked">还差：{{ finishBlockReason }}</div>
          </template>
          <template v-else>
            <el-result icon="success" title="安装引导完成" sub-title="向导已关闭（接口 403），以后从左侧菜单正常使用即可。" />
            <div class="section-title stack-3">接下来做什么（都是可选，按需来）：</div>
            <SetupWhatNext />
          </template>
        </template>
      </CardSection>

      <!-- 底部按钮 -->
      <div class="row-between step-bar">
        <el-button :disabled="step === 0" @click="step = Math.max(0, step - 1)">上一步</el-button>
        <div class="step-hint">{{ stepHint }}</div>
        <el-button v-if="step < 4" type="primary" :disabled="nextBlocked" @click="goNext">下一步</el-button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import http, { flattenDetail } from '../api'
import PageHeader from '../components/PageHeader.vue'
import CardSection from '../components/CardSection.vue'
import SetupWhatNext from '../components/SetupWhatNext.vue'
import { useSetupStore } from '../stores/setup'

const setup = useSetupStore()

const step = ref(0)
const stepTitles = ['第 1 步 · 修改管理员口令', '第 2 步 · 环境体检', '第 3 步 · 添加系统镜像',
  '第 4 步 · 网络准备', '第 5 步 · 完成']
const stepIcons = ['Key', 'FirstAidKit', 'Files', 'Connection', 'CircleCheckFilled']

const alreadyDone = computed(() => setup.stepsDone.completed)
const doneSummary = computed(() =>
  alreadyDone.value
    ? '这台机器已完成安装引导。'
    : '跟着下面 5 步走完就能用；每一步都会告诉你「为什么 / 怎么修」。')

// ── 第 1 步：改口令（复用既有 POST /api/auth/password，不动它的语义） ────
const pwdFormRef = ref()
const pwdSaving = ref(false)
const pwdForm = reactive({ old_password: '', new_password: '', confirm_password: '' })
const pwdRules = {
  old_password: [{ required: true, message: '请输入原口令', trigger: 'blur' }],
  new_password: [
    { required: true, message: '请输入新口令', trigger: 'blur' },
    { min: 8, message: '新口令至少 8 位', trigger: 'blur' },
    {
      validator: (rule, value, cb) => {
        if (value && value === pwdForm.old_password) cb(new Error('新口令不能与原口令相同'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
  confirm_password: [
    { required: true, message: '请再次输入新口令', trigger: 'blur' },
    {
      validator: (rule, value, cb) => {
        if (value && value !== pwdForm.new_password) cb(new Error('两次输入的新口令不一致'))
        else cb()
      },
      trigger: 'blur',
    },
  ],
}

const passwordOk = computed(() => setup.stepsDone.password_changed)

async function submitPassword() {
  await pwdFormRef.value.validate(async (valid) => {
    if (!valid) return
    pwdSaving.value = true
    try {
      // ★ _keepAuth：对改口令接口而言 401 是「原口令不正确」的业务失败，不是会话失效
      //   （见 api/index.js 拦截器与 MainLayout 修改密码弹窗的同款处理）；
      //   _silent 关掉统一 toast，错误由这里显式弹出。
      await http.post(
        '/auth/password',
        { old_password: pwdForm.old_password, new_password: pwdForm.new_password },
        { _keepAuth: true, _silent: true },
      )
      setup.markPasswordChanged()
      ElMessage.success('口令已修改，下次登录请使用新口令')
    } catch (e) {
      ElMessage.error(flattenDetail(e.response?.data?.detail, '修改失败'))
    } finally {
      pwdSaving.value = false
    }
  })
}

// ── 第 2 步：环境体检（GET /api/setup/checks，admin，只读） ──────────────
const LEVEL = {
  ok: { tag: 'success', label: '通过' },
  warn: { tag: 'warning', label: '注意' },
  error: { tag: 'danger', label: '需处理' },
}
function levelMeta(level) {
  return LEVEL[level] || { tag: 'info', label: level || '未知' }
}
const checks = ref([])
const checksLoading = ref(false)
const checksForbidden = ref(false)
const checksErrorCount = ref(null)
const errorCount = computed(() => checksErrorCount.value || 0)

async function loadChecks(manual = false) {
  checksLoading.value = true
  try {
    const items = await http.get('/setup/checks', { _silent: true })
    checks.value = Array.isArray(items) ? items : []
    checksErrorCount.value = checks.value.filter((i) => i.level === 'error').length
    checksForbidden.value = false
    if (manual) {
      ElMessage.success(checksErrorCount.value
        ? `体检完成：${checksErrorCount.value} 项需处理`
        : '体检完成，没有需处理项')
    }
  } catch (e) {
    if (e.response?.status === 403) checksForbidden.value = true
    else ElMessage.error('体检失败：' + (e.message || '请稍后重试'))
  } finally {
    checksLoading.value = false
  }
}

// ── 第 3 步：添加镜像（三个入口只做引导，不在这里实现上传/拉取） ─────────
const scpCmd = 'scp 你的镜像.iso root@<本机IP>:/srv/opstk/iso/'
const storageDirs = ref([])
const storageLoading = ref(false)

async function loadStorage() {
  storageLoading.value = true
  try {
    const r = await http.get('/system/storage', { _silent: true })
    storageDirs.value = (r && r.dirs) || []
  } catch (e) {
    ElMessage.error(e.response?.status === 403
      ? '空间信息需要管理员账号查看'
      : '取空间信息失败，可稍后重试')
  } finally {
    storageLoading.value = false
  }
}

function fmtBytes(n) {
  const v = Number(n || 0)
  if (!v) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let i = 0
  let x = v
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024
    i++
  }
  return (i >= 3 ? x.toFixed(1) : Math.round(x)) + ' ' + units[i]
}

// ── 第 4 步：网络准备（GET /api/setup/network，复用后端网卡检测，只读） ──
const net = ref(null)
const netWarnings = computed(() => (net.value && net.value.warnings) || [])

async function loadNetwork() {
  try {
    net.value = await http.get('/setup/network', { _silent: true })
  } catch (e) {
    if (e.response?.status !== 403) {
      ElMessage.error('网络探测失败：' + (e.message || '请稍后重试'))
    }
    net.value = {}
  }
}

// ── 第 5 步：完成（POST /api/setup/complete；后端双重守卫） ──────────────
const finishSaving = ref(false)
const finishDone = ref(false)

const nextBlocked = computed(() => {
  if (step.value === 0) return !passwordOk.value
  if (step.value === 1) return checksForbidden.value || (checksErrorCount.value || 0) > 0
  return false
})
const finishBlocked = computed(() => !finishDone.value
  && (!passwordOk.value || checksErrorCount.value !== 0))
const finishBlockReason = computed(() => {
  if (!passwordOk.value) return '还没修改管理员口令（第 1 步）'
  if (checksErrorCount.value === null) return '还没做环境体检（第 2 步）'
  if (checksErrorCount.value > 0) return `体检还有 ${checksErrorCount.value} 项需处理（第 2 步）`
  return ''
})

function goNext() {
  const target = Math.min(4, step.value + 1)
  step.value = target
  if (target === 1 && !checks.value.length) loadChecks()
  if (target === 2 && !storageDirs.value.length) loadStorage()
  if (target === 3 && net.value === null) loadNetwork()
}

async function finish() {
  finishSaving.value = true
  try {
    await http.post('/setup/complete', {}, { _silent: true })
    finishDone.value = true
    setup.markCompleted()
    ElMessage.success('安装引导完成')
  } catch (e) {
    const detail = e.response?.data?.detail
    ElMessage.error(typeof detail === 'string' ? detail : '完成失败：请先检查口令与体检两项')
  } finally {
    finishSaving.value = false
  }
}

// ── 复制到剪贴板（https 下走 clipboard API；http 部署退回 execCommand） ──
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('已复制到剪贴板')
  } catch (e) {
    const ta = document.createElement('textarea')
    ta.value = text
    ta.setAttribute('class', 'copy-tmp-area')
    document.body.appendChild(ta)
    ta.select()
    let done = false
    try {
      done = document.execCommand('copy')
    } catch (e2) {
      done = false
    }
    document.body.removeChild(ta)
    if (done) ElMessage.success('已复制到剪贴板')
    else ElMessage.error('复制失败，请手动选中复制')
  }
}

const stepHint = computed(() => {
  if (step.value === 0) return passwordOk.value ? '第 1 步已完成' : '不修改初始口令，不能进入下一步'
  if (step.value === 1) {
    if (checksForbidden.value) return '需要管理员账号才能体检'
    if (checksErrorCount.value === null) return '点「重新体检」开始检查'
    return errorCount.value ? '处理完标红项，再重新体检' : '体检通过'
  }
  if (step.value === 2) return '镜像可现在添加，也可以以后再来'
  if (step.value === 3) return '只读展示，确认无误即可'
  return '完成前请确认第 1、2 步已就绪'
})

onMounted(() => {
  setup.refresh()   // 刷新 steps_done（在页头弹窗里改过口令时门槛要实时）
})
</script>

<style scoped>
.setup-steps { margin-bottom: var(--ot-space-2); }
.setup-form { max-width: 480px; }
.form-actions { display: flex; align-items: center; gap: var(--ot-space-3); }
.pwd-ok { color: var(--ot-success); font-size: var(--ot-font-sm); }

/* 体检清单 */
.check-list { display: flex; flex-direction: column; gap: var(--ot-space-2); }
.check-item {
  border: 1px solid var(--ot-border-light);
  border-radius: var(--ot-radius);
  padding: var(--ot-space-3);
  background: var(--ot-bg-container);
}
.check-head { display: flex; align-items: center; gap: var(--ot-space-2); }
.check-title { font-weight: 600; color: var(--ot-text-1); }
.check-detail {
  margin-top: var(--ot-space-1);
  color: var(--ot-text-2);
  font-size: var(--ot-font-sm);
  line-height: 1.7;
}
.check-fix {
  margin-top: var(--ot-space-2);
  display: flex;
  align-items: center;
  gap: var(--ot-space-2);
}
.check-cmd {
  background: var(--ot-bg-code);
  color: var(--ot-code-fg);
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: var(--ot-font-xs);
  padding: var(--ot-space-1) var(--ot-space-2);
  border-radius: var(--ot-radius-sm);
  word-break: break-all;
  line-height: 1.6;
}

/* 第 3 步 */
.howto-title { font-weight: 600; margin-bottom: var(--ot-space-1); color: var(--ot-text-1); }
.howto-desc {
  color: var(--ot-text-2);
  font-size: var(--ot-font-sm);
  margin-bottom: var(--ot-space-2);
  line-height: 1.7;
}
.howto-cmd-row {
  display: flex;
  align-items: center;
  gap: var(--ot-space-2);
  flex-wrap: wrap;
}
.howto-cmd {
  background: var(--ot-bg-code);
  color: var(--ot-code-fg);
  font-size: var(--ot-font-sm);
  padding: var(--ot-space-1) var(--ot-space-2);
  border-radius: var(--ot-radius-sm);
}
.st-err { color: var(--ot-danger); }
.st-muted { color: var(--ot-text-3); }
.st-ok { color: var(--ot-success); }

/* 第 4 步 */
.net-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: var(--ot-space-3);
  margin-bottom: var(--ot-space-3);
}
.net-item {
  border: 1px solid var(--ot-border-light);
  border-radius: var(--ot-radius);
  padding: var(--ot-space-3);
  display: flex;
  flex-direction: column;
  gap: var(--ot-space-1);
}
.net-k { color: var(--ot-text-3); font-size: var(--ot-font-sm); }
.net-warn-list { margin-top: var(--ot-space-3); }
.net-warn { color: var(--ot-warning); font-size: var(--ot-font-sm); line-height: 1.8; }
.precond { margin-top: var(--ot-space-4); }
.precond-item { color: var(--ot-text-2); font-size: var(--ot-font-sm); line-height: 1.9; }

/* 第 5 步 */
.finish-list { margin-bottom: var(--ot-space-4); }
.finish-item { color: var(--ot-text-2); line-height: 1.9; }
.finish-blocked {
  margin-top: var(--ot-space-2);
  color: var(--ot-warning);
  font-size: var(--ot-font-sm);
}

/* 底部按钮 */
.step-bar { margin-top: var(--ot-space-2); }
.step-hint { color: var(--ot-text-3); font-size: var(--ot-font-sm); }

/* execCommand 复制兜底用的离屏 textarea（无 hex、无内联样式） */
.copy-tmp-area { position: fixed; left: -9999px; top: 0; }
</style>
