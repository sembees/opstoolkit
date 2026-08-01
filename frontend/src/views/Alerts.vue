<template>
  <div>
    <!-- 告警规则管理 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; margin-bottom: 12px">
        <span style="font-weight: 600"><el-icon><Bell /></el-icon> 告警规则</span>
        <el-button type="primary" size="small" @click="openRuleDialog()"><el-icon><Plus /></el-icon> 新建规则</el-button>
      </div>
      <el-table :data="rules" stripe size="small">
        <el-table-column prop="name" label="规则名称" min-width="140" />
        <el-table-column prop="metric_key" label="指标" width="120" />
        <el-table-column label="条件" width="160">
          <template #default="{ row }">{{ opLabel(row.operator) }} {{ row.threshold }}</template>
        </el-table-column>
        <el-table-column label="启用" width="60">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" size="small" @change="toggleRule(row)" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openRuleDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delRule(row.id)">
              <template #reference><el-button link type="danger" size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 告警记录 -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; justify-content: space-between">
          <span style="font-weight: 600"><el-icon><Warning /></el-icon> 告警记录</span>
          <el-button size="small" @click="loadHistory"><el-icon><Refresh /></el-icon> 刷新</el-button>
        </div>
      </template>
      <el-table :data="history" stripe size="small" empty-text="暂无告警记录">
        <el-table-column prop="asset_name" label="资产" min-width="120" />
        <el-table-column prop="error" label="告警信息" min-width="300" show-overflow-tooltip />
        <el-table-column label="时间" width="160">
          <template #default="{ row }">{{ row.created_at ? new Date(row.created_at).toLocaleString("zh-CN") : "-" }}</template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 规则编辑对话框 -->
    <el-dialog v-model="ruleDialogVisible" :title="editingRule ? '编辑规则' : '新建规则'" width="480px">
      <el-form label-width="80px" size="small">
        <el-form-item label="规则名称"><el-input v-model="ruleForm.name" placeholder="如: CPU超过告警" /></el-form-item>
        <el-form-item label="指标 key"><el-select v-model="ruleForm.metric_key" filterable placeholder="选择巡检指标">
          <el-option label="cpu" value="cpu" /><el-option label="memory" value="memory" />
          <el-option label="temperature" value="temperature" />
        </el-select></el-form-item>
        <el-form-item label="条件">
          <el-select v-model="ruleForm.operator" style="width: 100px">
            <el-option label=">" value="gt" /><el-option label="<" value="lt" />
            <el-option label=">=" value="gte" /><el-option label="<=" value="lte" />
          </el-select>
          <el-input-number v-model="ruleForm.threshold" :precision="1" style="margin-left: 8px; width: 140px" />
        </el-form-item>
        <el-form-item label="启用"><el-switch v-model="ruleForm.enabled" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="ruleDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="ruleSaving" @click="saveRule">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, onMounted } from "vue"
import { ElMessage } from "element-plus"
import http from "../api"

const rules = ref([])
const history = ref([])
const ruleDialogVisible = ref(false)
const editingRule = ref(null)
const ruleSaving = ref(false)
const ruleForm = ref({ name: "", metric_key: "cpu", operator: "gt", threshold: 80, enabled: true })

function opLabel(op) {
  return { gt: ">", lt: "<", gte: ">=", lte: "<=" }[op] || op
}

function openRuleDialog(row) {
  if (row) {
    editingRule.value = row
    ruleForm.value = { ...row }
  } else {
    editingRule.value = null
    ruleForm.value = { name: "", metric_key: "cpu", operator: "gt", threshold: 80, enabled: true }
  }
  ruleDialogVisible.value = true
}

async function saveRule() {
  ruleSaving.value = true
  try {
    if (editingRule.value) {
      await http.put("/alerts/rules/" + editingRule.value.id, ruleForm.value)
      ElMessage.success("已更新")
    } else {
      await http.post("/alerts/rules", ruleForm.value)
      ElMessage.success("已创建")
    }
    ruleDialogVisible.value = false
    loadRules()
  } catch (e) {
    ElMessage.error(e.response?.data?.detail || "保存失败")
  } finally {
    ruleSaving.value = false
  }
}

async function toggleRule(row) {
  try {
    await http.put("/alerts/rules/" + row.id, { enabled: row.enabled })
  } catch (e) {
    row.enabled = !row.enabled
  }
}

async function delRule(id) {
  await http.delete("/alerts/rules/" + id)
  ElMessage.success("已删除")
  loadRules()
}

async function loadRules() {
  rules.value = await http.get("/alerts/rules")
}

async function loadHistory() {
  history.value = await http.get("/alerts/history")
}

onMounted(() => { loadRules(); loadHistory() })
</script>
