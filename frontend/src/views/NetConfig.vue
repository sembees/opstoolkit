<template>
  <div style="display:flex;gap:16px;height:calc(100vh - 120px)">
    <!-- 左侧: 网络组件编辑表 -->
    <div style="flex:1;overflow-y:auto">
      <el-card shadow="never" size="small">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:8px">
          <div>
            <el-select v-model="config.os" size="small" style="width:110px" @change="onOsChange">
              <el-option v-for="o in meta.os_options" :key="o.id" :label="o.name" :value="o.id" />
            </el-select>
            <el-select v-model="config.format" size="small" style="width:110px;margin-left:6px" @change="preview">
              <el-option v-for="f in meta.formats" :key="f.id" :label="f.name" :value="f.id" />
            </el-select>
            <el-input v-model="config.hostname" size="small" placeholder="主机名" style="width:130px;margin-left:6px" @input="preview" />
            <el-select v-if="config.format==='netplan'" v-model="config.netplan_renderer" size="small" style="width:130px;margin-left:6px" @change="preview">
              <el-option v-for="r in meta.netplan_renderers" :key="r.id" :label="r.name" :value="r.id" />
            </el-select>
          </div>
          <div>
            <el-button size="small" @click="addItem('iface')"><el-icon><Plus /></el-icon> 物理接口</el-button>
            <el-button size="small" @click="addItem('bond')"><el-icon><Plus /></el-icon> Bond</el-button>
            <el-button size="small" @click="addItem('vlan')"><el-icon><Plus /></el-icon> VLAN</el-button>
            <el-button size="small" @click="addItem('bridge')"><el-icon><Plus /></el-icon> Bridge</el-button>
            <el-button size="small" type="danger" plain @click="clearAll">清空</el-button>
          </div>
        </div>

        <el-table :data="items" size="small" stripe border>
          <el-table-column label="#" width="36">
            <template #default="{ $index }">{{ $index + 1 }}</template>
          </el-table-column>
          <el-table-column label="类型" width="80">
            <template #default="{ row }">
              <el-select v-model="row._type" size="small" @change="onTypeChange(row)" style="width:72px">
                <el-option label="物理" value="iface" />
                <el-option label="Bond" value="bond" />
                <el-option label="VLAN" value="vlan" />
                <el-option label="Bridge" value="bridge" />
              </el-select>
            </template>
          </el-table-column>
          <el-table-column label="接口名" width="130">
            <template #default="{ row }">
              <el-input v-model="row.name" size="small" placeholder="eth0" @input="preview" />
            </template>
          </el-table-column>
          <el-table-column label="模式" width="80">
            <template #default="{ row }">
              <el-select v-model="row.mode" size="small" @change="preview" v-if="row._type !== 'bond'">
                <el-option label="static" value="static" /><el-option label="dhcp" value="dhcp" />
              </el-select>
              <span v-else style="color:#999;font-size:12px">-</span>
            </template>
          </el-table-column>
          <el-table-column label="IP / 掩码" width="150">
            <template #default="{ row }">
              <el-input v-if="row._type !== 'bond'" v-model="row.ip" size="small" placeholder="10.0.0.1/24" @input="preview" :disabled="row.mode==='dhcp'" />
              <el-input v-else v-model="row.ip" size="small" placeholder="10.0.0.1/24" @input="preview" />
            </template>
          </el-table-column>
          <el-table-column label="网关" width="110">
            <template #default="{ row }">
              <el-input v-model="row.gateway" size="small" placeholder="网关" @input="preview" :disabled="row.mode==='dhcp'" />
            </template>
          </el-table-column>
          <el-table-column label="DNS" width="130">
            <template #default="{ row }">
              <el-input v-model="row.dnsStr" size="small" placeholder="8.8.8.8,逗号分隔" @input="onDnsChange(row)" :disabled="row.mode==='dhcp'" />
            </template>
          </el-table-column>
          <el-table-column label="Bond/从接口" min-width="130">
            <template #default="{ row }">
              <el-input v-if="row._type==='bond'" v-model="row.slavesStr" size="small" placeholder="eth0,eth1" @input="onSlavesChange(row)" />
              <el-input v-else-if="row._type==='vlan'" v-model="row.parent" size="small" placeholder="父接口(bond0/eth0)" @input="preview" />
              <el-input v-else-if="row._type==='bridge'" v-model="row.slavesStr" size="small" placeholder="从接口" @input="onSlavesChange(row)" />
              <span v-else style="color:#999;font-size:12px">-</span>
            </template>
          </el-table-column>
          <el-table-column label="Bond参数" width="120">
            <template #default="{ row }">
              <template v-if="row._type==='bond'">
                <el-select v-model="row.bondMode" size="small" style="width:65px" @change="preview">
                  <el-option v-for="m in meta.bond_modes" :key="m.id" :label="m.id" :value="m.id" />
                </el-select>
                <el-input v-model="row.miimon" size="small" style="width:50px;margin-left:4px" placeholder="100" @input="preview" />
              </template>
              <span v-else style="color:#999;font-size:12px">-</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="80" fixed="right">
            <template #default="{ $index }">
              <el-button link type="primary" size="small" @click="dupItem($index)">复制</el-button>
              <el-button link type="danger" size="small" @click="delItem($index)">删除</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div style="margin-top:12px;display:flex;justify-content:space-between">
          <el-button type="primary" @click="doGenerate" :loading="generating">生成配置</el-button>
          <el-button @click="doDownload" :disabled="!previewScript">下载 ({{ previewFilename }})</el-button>
        </div>
      </el-card>
    </div>

    <!-- 右侧: 实时预览 -->
    <div style="flex:1;overflow-y:auto">
      <el-card shadow="never" size="small">
        <template #header><span style="font-weight:600">实时预览</span></template>
        <pre class="preview-block" v-if="previewScript">{{ previewScript }}</pre>
        <div v-else style="color:#ccc;text-align:center;padding:60px 0">添加接口后自动预览</div>
      </el-card>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, watch } from "vue"
import http, { downloadZip } from "../api"

const meta = reactive({ os_options: [], formats: [], bond_modes: [], netplan_renderers: [] })
const config = reactive({ os: "rhel", format: "nmcli", hostname: "", netplan_renderer: "networkd" })
const items = ref([])
const previewScript = ref("")
const previewFilename = ref("")
const generating = ref(false)

let previewTimer = null
function preview() {
  clearTimeout(previewTimer)
  previewTimer = setTimeout(doGenerate, 400)
}

function onOsChange() { preview() }
function onDnsChange(row) { row.dns = (row.dnsStr || "").split(",").map(s => s.trim()).filter(Boolean); preview() }
function onSlavesChange(row) { row.slaves = (row.slavesStr || "").split(",").map(s => s.trim()).filter(Boolean); preview() }
function onTypeChange(row) {
  if (row._type === "vlan") { row.mode = "static"; row.parent = row.parent || "eth0" }
  if (row._type === "bond") { row.bondMode = row.bondMode || 1; row.miimon = row.miimon || 100 }
  preview()
}

function addItem(typ) {
  const base = { _type: typ, name: "", mode: "static", ip: "", gateway: "", dns: [], dnsStr: "", slaves: [], slavesStr: "", parent: "", bondMode: 1, miimon: 100 }
  if (typ === "iface") { base.name = "eth" + items.value.length; base.mode = "dhcp" }
  else if (typ === "bond") { base.name = "bond" + items.value.filter(i => i._type === "bond").length; base.slavesStr = "eth0,eth1"; onSlavesChange(base) }
  else if (typ === "vlan") { base.name = (items.value.find(i => i._type === "iface")?.name || "eth0") + ".100"; base.parent = items.value.find(i => i._type === "iface")?.name || "eth0" }
  else if (typ === "bridge") { base.name = "br" + items.value.filter(i => i._type === "bridge").length; base.slavesStr = "eth0"; onSlavesChange(base) }
  items.value.push(base)
  preview()
}

function dupItem(idx) {
  const orig = items.value[idx]
  const copy = JSON.parse(JSON.stringify(orig))
  copy.name = copy.name ? copy.name + "-copy" : ""
  items.value.splice(idx + 1, 0, copy)
  preview()
}

function delItem(idx) { items.value.splice(idx, 1); preview() }
function clearAll() { items.value = []; previewScript.value = "" }

async function doGenerate() {
  const payload = { os: config.os, format: config.format, hostname: config.hostname, interfaces: [], bonds: [], vlans: [], bridges: [] }
  if (config.format === "netplan") payload.netplan_renderer = config.netplan_renderer

  for (const row of items.value) {
    if (row._type === "iface") {
      const ipCidr = parseIpCidr(row.ip)
      payload.interfaces.push({
        name: row.name, mode: row.mode,
        ip: ipCidr?.ip || row.ip, cidr: ipCidr?.cidr,
        gateway: row.gateway, dns: row.dns || []
      })
    } else if (row._type === "bond") {
      const ipCidr = parseIpCidr(row.ip)
      payload.bonds.push({
        name: row.name, mode: row.bondMode || 1, interfaces: row.slaves || [],
        ip: ipCidr?.ip || "", cidr: ipCidr?.cidr || "",
        gateway: row.gateway, dns: row.dns || [], miimon: row.miimon || 100
      })
    } else if (row._type === "vlan") {
      const ipCidr = parseIpCidr(row.ip)
      payload.vlans.push({
        parent: row.parent, vlan_id: parseInt(row.name.split(".").pop()) || 100,
        mode: row.mode,
        ip: ipCidr?.ip || row.ip, cidr: ipCidr?.cidr || "",
        gateway: row.gateway
      })
    } else if (row._type === "bridge") {
      const ipCidr = parseIpCidr(row.ip)
      payload.bridges.push({
        name: row.name, interfaces: row.slaves || [],
        ip: ipCidr?.ip || "", cidr: ipCidr?.cidr || "",
        gateway: row.gateway
      })
    }
  }

  try {
    const resp = await http.post("/it/netconfig/generate", payload)
    previewScript.value = resp.script
    previewFilename.value = resp.filename
  } catch (e) {
    // keep previous preview
  }
}

function parseIpCidr(val) {
  if (!val) return null
  const m = val.match(/^(.+?)(?:\/(\d+))?$/)
  return m ? { ip: m[1], cidr: parseInt(m[2]) || undefined } : { ip: val }
}

async function doDownload() {
  generating.value = true
  try {
    await downloadZip("/it/netconfig/download", itemsToPayload())
  } finally { generating.value = false }
}

function itemsToPayload() {
  const payload = { os: config.os, format: config.format, hostname: config.hostname, interfaces: [], bonds: [], vlans: [], bridges: [] }
  for (const row of items.value) {
    if (row._type === "iface") {
      const ipCidr = parseIpCidr(row.ip)
      payload.interfaces.push({ name: row.name, mode: row.mode, ip: ipCidr?.ip || row.ip, cidr: ipCidr?.cidr, gateway: row.gateway, dns: row.dns || [] })
    } else if (row._type === "bond") {
      const ipCidr = parseIpCidr(row.ip)
      payload.bonds.push({ name: row.name, mode: row.bondMode || 1, interfaces: row.slaves || [], ip: ipCidr?.ip || "", cidr: ipCidr?.cidr || "", gateway: row.gateway, dns: row.dns || [], miimon: row.miimon || 100 })
    } else if (row._type === "vlan") {
      const ipCidr = parseIpCidr(row.ip)
      payload.vlans.push({ parent: row.parent, vlan_id: parseInt(row.name.split(".").pop()) || 100, mode: row.mode, ip: ipCidr?.ip || row.ip, cidr: ipCidr?.cidr || "", gateway: row.gateway })
    } else if (row._type === "bridge") {
      const ipCidr = parseIpCidr(row.ip)
      payload.bridges.push({ name: row.name, interfaces: row.slaves || [], ip: ipCidr?.ip || "", cidr: ipCidr?.cidr || "", gateway: row.gateway })
    }
  }
  return payload
}

onMounted(async () => {
  try {
    const data = await http.get("/it/netconfig/meta")
    Object.assign(meta, data)
  } catch (e) { /* defaults */ }
  // initial demo
  addItem("iface")
  addItem("bond")
})
</script>

<style scoped>
.preview-block {
  background: #1e1e1e;
  color: #d4d4d4;
  padding: 12px 16px;
  border-radius: 6px;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre;
  overflow-x: auto;
  font-family: monospace;
  min-height: 200px;
  max-height: calc(100vh - 220px);
  overflow-y: auto;
}
</style>
