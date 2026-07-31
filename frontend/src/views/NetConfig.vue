<template>
  <el-row :gutter="16">
    <el-col :span="13">
      <el-card shadow="never">
        <template #header><span style="font-weight: 600"><el-icon><Connection /></el-icon> 网络配置生成器</span></template>
        <el-form label-width="80px" size="default">
          <el-row :gutter="12">
            <el-col :span="8">
              <el-form-item label="系统">
                <el-select v-model="config.os" @change="onOsChange">
                  <el-option v-for="o in meta.os_options" :key="o.id" :label="o.name" :value="o.id" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="格式">
                <el-select v-model="config.format">
                  <el-option v-for="f in meta.formats" :key="f.id" :label="f.name" :value="f.id" :disabled="f.id === 'netplan' && config.os !== 'ubuntu'" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="8">
              <el-form-item label="主机名"><el-input v-model="config.hostname" placeholder="web01" /></el-form-item>
            </el-col>
          </el-row>

          <el-divider content-position="left">物理接口</el-divider>
          <div v-for="(iface, i) in config.interfaces" :key="'if'+i" style="margin-bottom: 10px">
            <el-row :gutter="8" align="middle">
              <el-col :span="3"><el-input v-model="iface.name" placeholder="eth0" size="small" /></el-col>
              <el-col :span="4">
                <el-select v-model="iface.mode" size="small">
                  <el-option label="静态" value="static" /><el-option label="DHCP" value="dhcp" />
                </el-select>
              </el-col>
              <template v-if="iface.mode === 'static'">
                <el-col :span="5"><el-input v-model="iface.ip" placeholder="IP" size="small" /></el-col>
                <el-col :span="3"><el-input-number v-model="iface.cidr" :min="1" :max="32" placeholder="CIDR" size="small" controls-position="right" style="width:100%" /></el-col>
                <el-col :span="4"><el-input v-model="iface.gateway" placeholder="网关" size="small" /></el-col>
                <el-col :span="3"><el-input v-model="iface.dnsText" placeholder="DNS(逗号)" size="small" /></el-col>
              </template>
              <el-col :span="2"><el-button type="danger" link size="small" @click="config.interfaces.splice(i,1)"><el-icon><Delete /></el-icon></el-button></el-col>
            </el-row>
          </div>
          <el-button plain size="small" @click="addIface"><el-icon><Plus /></el-icon> 添加接口</el-button>

          <el-divider content-position="left">链路聚合 (Bond)</el-divider>
          <div v-for="(bond, i) in config.bonds" :key="'bd'+i" style="margin-bottom: 12px; border: 1px solid #ebeef5; border-radius: 4px; padding: 10px">
            <el-row :gutter="8" align="middle">
              <el-col :span="4"><el-input v-model="bond.name" placeholder="bond0" size="small" /></el-col>
              <el-col :span="7">
                <el-select v-model="bond.mode" size="small">
                  <el-option v-for="m in meta.bond_modes" :key="m.id" :label="m.name" :value="m.id" />
                </el-select>
              </el-col>
              <el-col :span="8"><el-input v-model="bond.ifaceText" placeholder="从接口(逗号分隔): eth1,eth2" size="small" /></el-col>
              <el-col :span="3"><el-input-number v-model="bond.miimon" :min="0" size="small" controls-position="right" style="width:100%" /></el-col>
              <el-col :span="2"><el-button type="danger" link size="small" @click="config.bonds.splice(i,1)"><el-icon><Delete /></el-icon></el-button></el-col>
            </el-row>
            <el-row :gutter="8" align="middle" style="margin-top: 6px">
              <el-col :span="6"><el-input v-model="bond.ip" placeholder="IP" size="small" /></el-col>
              <el-col :span="3"><el-input-number v-model="bond.cidr" :min="1" :max="32" size="small" controls-position="right" style="width:100%" /></el-col>
              <el-col :span="5"><el-input v-model="bond.gateway" placeholder="网关" size="small" /></el-col>
              <el-col :span="5"><el-input v-model="bond.primary" placeholder="主接口(active-backup)" size="small" /></el-col>
              <el-col :span="5"><el-input v-model="bond.dnsText" placeholder="DNS(逗号)" size="small" /></el-col>
            </el-row>
          </div>
          <el-button plain size="small" @click="addBond"><el-icon><Plus /></el-icon> 添加聚合</el-button>

          <el-divider content-position="left">VLAN</el-divider>
          <div v-for="(vlan, i) in config.vlans" :key="'vl'+i" style="margin-bottom: 10px">
            <el-row :gutter="8" align="middle">
              <el-col :span="5"><el-input v-model="vlan.parent" placeholder="父接口 bond0" size="small" /></el-col>
              <el-col :span="3"><el-input-number v-model="vlan.vlan_id" :min="1" :max="4094" size="small" controls-position="right" style="width:100%" /></el-col>
              <el-col :span="6"><el-input v-model="vlan.ip" placeholder="IP" size="small" /></el-col>
              <el-col :span="3"><el-input-number v-model="vlan.cidr" :min="1" :max="32" size="small" controls-position="right" style="width:100%" /></el-col>
              <el-col :span="5"><el-input v-model="vlan.gateway" placeholder="网关(可选)" size="small" /></el-col>
              <el-col :span="2"><el-button type="danger" link size="small" @click="config.vlans.splice(i,1)"><el-icon><Delete /></el-icon></el-button></el-col>
            </el-row>
          </div>
          <el-button plain size="small" @click="addVlan"><el-icon><Plus /></el-icon> 添加 VLAN</el-button>

          <el-divider content-position="left">网桥 (Bridge)</el-divider>
          <div v-for="(br, i) in config.bridges" :key="'br'+i" style="margin-bottom: 10px">
            <el-row :gutter="8" align="middle">
              <el-col :span="4"><el-input v-model="br.name" placeholder="br0" size="small" /></el-col>
              <el-col :span="9"><el-input v-model="br.ifaceText" placeholder="成员接口(逗号): eth0,eth1" size="small" /></el-col>
              <el-col :span="5"><el-input v-model="br.ip" placeholder="IP" size="small" /></el-col>
              <el-col :span="3"><el-input-number v-model="br.cidr" :min="1" :max="32" size="small" controls-position="right" style="width:100%" /></el-col>
              <el-col :span="3"><el-button type="danger" link size="small" @click="config.bridges.splice(i,1)"><el-icon><Delete /></el-icon></el-button></el-col>
            </el-row>
          </div>
          <el-button plain size="small" @click="addBridge"><el-icon><Plus /></el-icon> 添加网桥</el-button>

          <el-divider />
          <el-button type="primary" @click="generate" :loading="loading"><el-icon><Check /></el-icon> 生成配置</el-button>
        </el-form>
      </el-card>
    </el-col>

    <el-col :span="11">
      <el-card shadow="never">
        <template #header>
          <div style="display: flex; justify-content: space-between; align-items: center">
            <span style="font-weight: 600"><el-icon><Document /></el-icon> 生成结果</span>
            <el-button v-if="script" type="success" plain size="small" @click="download"><el-icon><Download /></el-icon> 下载</el-button>
          </div>
        </template>
        <div v-if="script" class="terminal-output" style="white-space: pre; max-height: 600px">{{ script }}</div>
        <el-empty v-else description="填写配置后点击「生成配置」" />
      </el-card>
    </el-col>
  </el-row>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import http from '../api'
import { ElMessage } from 'element-plus'

const meta = reactive({ os_options: [], formats: [], bond_modes: [] })
const loading = ref(false)
const script = ref('')
const filename = ref('')

const config = reactive({
  os: 'rhel', format: 'nmcli', hostname: '',
  interfaces: [], bonds: [], vlans: [], bridges: [],
})

function addIface() { config.interfaces.push({ name: '', mode: 'static', ip: '', cidr: 24, gateway: '', dnsText: '' }) }
function addBond() { config.bonds.push({ name: '', mode: 1, ifaceText: '', ip: '', cidr: 24, gateway: '', miimon: 100, primary: '', dnsText: '' }) }
function addVlan() { config.vlans.push({ parent: '', vlan_id: 100, ip: '', cidr: 24, gateway: '' }) }
function addBridge() { config.bridges.push({ name: '', ifaceText: '', ip: '', cidr: 24 }) }

function onOsChange() { if (config.os !== 'ubuntu') config.format = 'nmcli' }

function buildPayload() {
  const parseDns = (t) => (t || '').split(',').map(s => s.trim()).filter(Boolean)
  return {
    os: config.os, hostname: config.hostname || null, format: config.format,
    interfaces: config.interfaces.map(i => ({ name: i.name, mode: i.mode, ip: i.ip || null, cidr: i.cidr, gateway: i.gateway || null, dns: parseDns(i.dnsText) })),
    bonds: config.bonds.map(b => ({ name: b.name, mode: b.mode, interfaces: (b.ifaceText || '').split(',').map(s => s.trim()).filter(Boolean), ip: b.ip || null, cidr: b.cidr, gateway: b.gateway || null, miimon: b.miimon, primary: b.primary || null, dns: parseDns(b.dnsText) })),
    vlans: config.vlans.map(v => ({ parent: v.parent, vlan_id: v.vlan_id, mode: 'static', ip: v.ip || null, cidr: v.cidr, gateway: v.gateway || null })),
    bridges: config.bridges.map(br => ({ name: br.name, interfaces: (br.ifaceText || '').split(',').map(s => s.trim()).filter(Boolean), ip: br.ip || null, cidr: br.cidr })),
  }
}

async function generate() {
  if (!config.interfaces.length && !config.bonds.length && !config.bridges.length) {
    ElMessage.warning('请至少添加一个接口/聚合/网桥')
    return
  }
  loading.value = true
  try {
    const res = await http.post('/it/netconfig/generate', buildPayload())
    script.value = res.script
    filename.value = res.filename
    ElMessage.success('配置已生成')
  } finally { loading.value = false }
}

function download() {
  const blob = new Blob([script.value], { type: 'text/plain' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename.value || 'apply-network.sh'
  a.click()
  URL.revokeObjectURL(url)
}

onMounted(async () => {
  try {
    const data = await http.get('/it/netconfig/meta')
    Object.assign(meta, data)
  } catch (e) { /* handled */ }
  addIface()
})
</script>