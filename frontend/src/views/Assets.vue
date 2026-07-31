<template>
  <el-card shadow="never">
    <div style="display: flex; justify-content: space-between; margin-bottom: 16px">
      <el-radio-group v-model="filterCategory" @change="loadAssets">
        <el-radio-button label="">全部</el-radio-button>
        <el-radio-button label="ct">CT 设备</el-radio-button>
        <el-radio-button label="it">IT 服务器</el-radio-button>
      </el-radio-group>
      <el-button type="primary" @click="openDialog()"><el-icon><Plus /></el-icon> 新增资产</el-button>
    </div>
    <el-table :data="assets" stripe>
      <el-table-column prop="name" label="名称" min-width="130" />
      <el-table-column label="类型" width="70">
        <template #default="{ row }">
          <el-tag :type="row.category === 'ct' ? 'primary' : 'success'" size="small">{{ row.category === 'ct' ? 'CT' : 'IT' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="vendor" label="厂商" width="80" />
      <el-table-column prop="device_role" label="角色" width="90" />
      <el-table-column prop="host" label="IP / 主机" width="130" />
      <el-table-column prop="port" label="端口" width="60" />
      <el-table-column prop="device_type" label="Device Type" width="130" />
      <el-table-column prop="location" label="位置" width="90" />
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button type="primary" link size="small" @click="openDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除?" @confirm="del(row.id)">
            <template #reference><el-button type="danger" link size="small">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑资产' : '新增资产'" width="580px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="100px">
        <el-form-item label="名称" prop="name"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="类型" prop="category">
          <el-select v-model="form.category"><el-option label="CT 网络设备" value="ct" /><el-option label="IT 服务器" value="it" /></el-select>
        </el-form-item>
        <el-form-item label="厂商" v-if="form.category === 'ct'">
          <el-select v-model="form.vendor" @change="form.device_type = ''">
            <el-option label="H3C" value="h3c" /><el-option label="华为" value="huawei" /><el-option label="思科" value="cisco" />
          </el-select>
        </el-form-item>
        <el-form-item label="角色"><el-input v-model="form.device_role" placeholder="switch / router / firewall / server" /></el-form-item>
        <el-form-item label="IP / 主机" prop="host"><el-input v-model="form.host" /></el-form-item>
        <el-form-item label="端口"><el-input-number v-model="form.port" :min="1" :max="65535" /></el-form-item>
        <el-form-item label="序列号"><el-input v-model="form.serial" /></el-form-item>
        <el-form-item label="MAC"><el-input v-model="form.mac" /></el-form-item>
        <el-form-item label="位置"><el-input v-model="form.location" /></el-form-item>
        <el-form-item label="关联凭据">
          <el-select v-model="form.credential_id" clearable placeholder="选择凭据">
            <el-option v-for="c in credentials" :key="c.id" :label="c.name" :value="c.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注"><el-input v-model="form.remark" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存</el-button>
      </template>
    </el-dialog>
  </el-card>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import http from '../api'
import { ElMessage } from 'element-plus'

const assets = ref([])
const credentials = ref([])
const filterCategory = ref('')
const dialogVisible = ref(false)
const editing = ref(null)
const saving = ref(false)
const formRef = ref()
const emptyForm = () => ({ name: '', category: 'ct', vendor: 'h3c', device_role: '', host: '', port: 22, device_type: '', serial: '', mac: '', location: '', remark: '', credential_id: null })
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '必填', trigger: 'blur' }],
  category: [{ required: true, message: '必填', trigger: 'change' }],
  host: [{ required: true, message: '必填', trigger: 'blur' }],
}
async function loadAssets() {
  assets.value = await http.get('/assets' + (filterCategory.value ? `?category=${filterCategory.value}` : ''))
}
async function loadCredentials() {
  credentials.value = await http.get('/assets/credentials')
}
function openDialog(row) {
  Object.assign(form, emptyForm())
  if (row) { Object.assign(form, row); editing.value = row.id }
  else editing.value = null
  dialogVisible.value = true
}
async function save() {
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    saving.value = true
    try {
      if (editing.value) await http.put(`/assets/${editing.value}`, { ...form })
      else await http.post('/assets', { ...form })
      ElMessage.success('保存成功')
      dialogVisible.value = false
      loadAssets()
    } finally { saving.value = false }
  })
}
async function del(id) {
  await http.delete(`/assets/${id}`)
  ElMessage.success('已删除')
  loadAssets()
}
onMounted(() => { loadAssets(); loadCredentials() })
</script>
