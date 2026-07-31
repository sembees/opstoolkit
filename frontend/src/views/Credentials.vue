<template>
  <el-card shadow="never">
    <div style="display: flex; justify-content: space-between; margin-bottom: 16px">
      <span style="font-size: 14px; color: #999">设备登录凭据（密码加密存储）</span>
      <el-button type="primary" @click="openDialog()"><el-icon><Plus /></el-icon> 新增凭据</el-button>
    </div>
    <el-table :data="creds" stripe>
      <el-table-column prop="name" label="名称" min-width="140" />
      <el-table-column prop="username" label="用户名" width="120" />
      <el-table-column prop="device_type" label="默认 Device Type" width="150" />
      <el-table-column prop="port" label="端口" width="70" />
      <el-table-column label="密码" width="70">
        <template #default="{ row }">
          <el-tag :type="row.has_password ? 'success' : 'info'" size="small">{{ row.has_password ? '已设' : '无' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="密钥" width="70">
        <template #default="{ row }">
          <el-tag :type="row.has_ssh_key ? 'success' : 'info'" size="small">{{ row.has_ssh_key ? '已设' : '无' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="remark" label="备注" min-width="120" show-overflow-tooltip />
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button type="primary" link size="small" @click="openDialog(row)">编辑</el-button>
          <el-popconfirm title="确定删除?" @confirm="del(row.id)">
            <template #reference><el-button type="danger" link size="small">删除</el-button></template>
          </el-popconfirm>
        </template>
      </el-table-column>
    </el-table>
    <el-dialog v-model="dialogVisible" :title="editing ? '编辑凭据' : '新增凭据'" width="540px">
      <el-form ref="formRef" :model="form" :rules="rules" label-width="110px">
        <el-form-item label="名称" prop="name"><el-input v-model="form.name" /></el-form-item>
        <el-form-item label="用户名" prop="username"><el-input v-model="form.username" /></el-form-item>
        <el-form-item label="密码"><el-input v-model="form.password" type="password" show-password placeholder="留空则不修改" /></el-form-item>
        <el-form-item label="Enable 密钥"><el-input v-model="form.enable_secret" type="password" show-password placeholder="思科 enable / H3C super" /></el-form-item>
        <el-form-item label="SSH 私钥"><el-input v-model="form.ssh_key" type="textarea" :rows="4" placeholder="PEM 格式私钥（可选）" /></el-form-item>
        <el-form-item label="默认端口"><el-input-number v-model="form.port" :min="1" :max="65535" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="form.remark" /></el-form-item>
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

const creds = ref([])
const dialogVisible = ref(false)
const editing = ref(null)
const saving = ref(false)
const formRef = ref()
const emptyForm = () => ({ name: '', username: '', password: '', enable_secret: '', ssh_key: '', device_type: '', port: 22, remark: '' })
const form = reactive(emptyForm())
const rules = {
  name: [{ required: true, message: '必填', trigger: 'blur' }],
  username: [{ required: true, message: '必填', trigger: 'blur' }],
}
async function load() { creds.value = await http.get('/assets/credentials') }
function openDialog(row) {
  Object.assign(form, emptyForm())
  if (row) { editing.value = row.id; form.name = row.name; form.username = row.username; form.device_type = row.device_type; form.port = row.port; form.remark = row.remark }
  else editing.value = null
  dialogVisible.value = true
}
async function save() {
  await formRef.value.validate(async (valid) => {
    if (!valid) return
    saving.value = true
    try {
      const payload = { ...form }
      if (editing.value) await http.put(`/assets/credentials/${editing.value}`, payload)
      else await http.post('/assets/credentials', payload)
      ElMessage.success('保存成功')
      dialogVisible.value = false
      load()
    } finally { saving.value = false }
  })
}
async function del(id) {
  await http.delete(`/assets/credentials/${id}`)
  ElMessage.success('已删除')
  load()
}
onMounted(load)
</script>
