<template>
  <div>
    <!-- ZTP 服务器状态 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
        <span style="font-weight: 600">
          <el-icon><Cpu /></el-icon> ZTP 服务器（本机）
          <el-tag v-if="serverStatus.dnsmasq" :type="serverStatus.dnsmasq.active ? 'success' : 'danger'" size="small" style="margin-left: 8px">
            dnsmasq {{ serverStatus.dnsmasq.active ? '运行中' : '未运行' }}
          </el-tag>
        </span>
        <div>
          <el-button size="small" @click="controlService('start')" :disabled="serverStatus.supported === false">启动</el-button>
          <el-button size="small" @click="controlService('stop')" :disabled="serverStatus.supported === false">停止</el-button>
          <el-button size="small" @click="controlService('restart')" :disabled="serverStatus.supported === false">重启 dnsmasq</el-button>
          <el-button size="small" @click="loadServerStatus"><el-icon><Refresh /></el-icon> 刷新</el-button>
        </div>
      </div>
      <el-descriptions v-if="serverStatus.supported" :column="3" size="small" border>
        <el-descriptions-item label="小工具目录">{{ serverStatus.sudo_ok ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="TFTP">{{ serverStatus.dirs && serverStatus.dirs.tftp ? '已创建' : '未创建' }}</el-descriptions-item>
        <el-descriptions-item label="HTTP">{{ serverStatus.dirs && serverStatus.dirs.web ? '已创建' : '未创建' }}</el-descriptions-item>
      </el-descriptions>
      <el-alert v-if="serverStatus.supported === false" type="warning" :closable="false" style="margin-top: 8px">
        本机部署需 Linux 环境，当前：{{ serverStatus.platform }}
      </el-alert>
    </el-card>

    <!-- 模板列表 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; margin-bottom: 12px">
        <span style="font-weight: 600"><el-icon><Connection /></el-icon> ZTP 开局模板</span>
        <el-button type="primary" @click="openTemplateDialog()"><el-icon><Plus /></el-icon> 新建开局模板</el-button>
      </div>
      <el-table :data="templates" stripe size="small">
        <el-table-column prop="name" label="模板名称" min-width="130" />
        <el-table-column label="厂商" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="vendorType(row.vendor)">{{ vendorLabel(row.vendor) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="mgmt_vlan" label="管理VLAN" width="90" />
        <el-table-column prop="mgmt_gateway" label="网关" width="120" />
        <el-table-column prop="server_ip" label="ZTP服务器" width="120" />
        <el-table-column label="投递模式" width="110">
          <template #default="{ row }">{{ modeLabel(row.deploy_mode) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openGenDialog(row)">生成配置</el-button>
            <el-button link type="primary" size="small" @click="openTemplateDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delTemplate(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 设备清单 -->
    <el-card shadow="never">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600"><el-icon><Monitor /></el-icon> ZTP 设备清单（可选）</span>
          <el-button type="primary" size="small" @click="deviceDialog = true"><el-icon><Plus /></el-icon> 添加设备</el-button>
        </div>
      </template>
      <!-- R4/§5.54：登记设备是**可选**的，必须写清楚 —— 否则会被当成"必须先登记 MAC 才能开局" -->
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
        <template #title>不登记设备也能开局</template>
        <div style="font-size:12px;line-height:1.6">
          没登记的设备统一拿 <code>ztp/default.cfg</code>（管理口用 DHCP 取址，不会写死 IP，
          否则多台设备会撞同一个地址）；开局后到 DHCP 服务器上按 MAC 认领它们各自的地址。
          <br />
          想给**每台不同的**主机名 / 管理 IP，就在这里按 <b>MAC</b> 登记（DHCP 是按 MAC 匹配的；
          只填序列号不填 MAC 的设备仍会拿 default.cfg）。管理 IP 留空 = 该设备也用 DHCP 取址。
        </div>
      </el-alert>
      <el-table :data="devices" stripe size="small">
        <el-table-column prop="hostname" label="主机名" min-width="120" />
        <el-table-column prop="mac" label="MAC 地址" width="160" />
        <el-table-column prop="serial" label="序列号" width="140" />
        <el-table-column prop="mgmt_ip" label="管理 IP" width="120" />
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-popconfirm title="确定删除?" @confirm="delDevice(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 落位登记（落位 + 认领）：设备到货时只有落位/规划IP/主机名，没有 MAC ——
         MAC 由系统从 dnsmasq 租约里自动学到，运维只做一步「认领」，全程不手抄 MAC -->
    <el-card shadow="never" style="margin-top: 16px">
      <template #header>
        <div style="display: flex; justify-content: space-between; align-items: center">
          <span style="font-weight: 600"><el-icon><Location /></el-icon> ZTP 落位登记（落位 + 认领）</span>
          <div>
            <el-select v-model="posTemplateId" filterable placeholder="选择模板" size="small" style="width: 210px; margin-right: 8px" @change="loadPositionData">
              <el-option v-for="t in templates" :key="t.id" :label="t.name + ' (' + vendorLabel(t.vendor) + ')'" :value="t.id" />
            </el-select>
            <el-button type="primary" size="small" @click="openPosDialog()"><el-icon><Plus /></el-icon> 新增落位</el-button>
            <el-button size="small" @click="openImportDialog"><el-icon><Upload /></el-icon> 批量导入</el-button>
            <el-button size="small" @click="loadPositionData"><el-icon><Refresh /></el-icon> 刷新</el-button>
          </div>
        </div>
      </template>
      <el-alert type="info" :closable="false" show-icon style="margin-bottom: 10px">
        <template #title>不需要手抄 MAC</template>
        <div style="font-size:12px;line-height:1.6">
          设备到货时只有落位（机架/机柜/U位）和规划好的管理 IP/主机名 —— 先按落位登记（MAC 留空）；
          设备第一次上电向 DHCP 请求地址时，dnsmasq 的租约里就记录了它的 MAC，
          在下方「待认领设备」里点「认领到落位」即可。认领后<b>重新生成并部署</b>，
          设备就会拿到自己落位规划的主机名/管理 IP；未认领的落位只会拿到 <code>ztp/default.cfg</code>。
        </div>
      </el-alert>
      <el-alert v-if="obsNote" :type="obsOk ? 'info' : 'warning'" :closable="false" show-icon style="margin-bottom: 10px" :title="obsNote" />
      <el-divider content-position="left">待认领设备（来自 dnsmasq 租约）</el-divider>
      <el-table :data="observations" stripe size="small" :empty-text="obsOk
        ? '租约里暂时没有设备；设备上电接入开局网络后会出现在这里'
        : '读取不到 dnsmasq 租约（不是「没有设备」）—— 请先看上方说明'">
        <el-table-column prop="mac" label="MAC" width="160" />
        <el-table-column prop="ip" label="拿到的IP" width="130" />
        <el-table-column prop="hostname" label="租约主机名" min-width="120" />
        <el-table-column label="状态" width="190">
          <template #default="{ row }">
            <el-tag v-if="row.position" type="success" size="small">已指向落位 {{ row.position }}</el-tag>
            <el-tag v-else type="info" size="small">未认领</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" :disabled="!!row.position_id" @click="openClaimDialog(row)">认领到落位</el-button>
          </template>
        </el-table-column>
      </el-table>
      <el-divider content-position="left">落位表</el-divider>
      <el-table :data="positions" stripe size="small">
        <el-table-column prop="position" label="落位" min-width="120" />
        <el-table-column prop="hostname" label="主机名" min-width="110" />
        <el-table-column prop="mgmt_ip" label="规划管理IP" width="130" />
        <el-table-column prop="serial" label="序列号" width="130" />
        <el-table-column prop="mac" label="MAC" width="160" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag v-if="row.mac" type="success" size="small">已认领</el-tag>
            <el-tag v-else type="warning" size="small">待认领</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" @click="openPosDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delPosition(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 模板编辑弹窗 -->
    <el-dialog v-model="templateDialog" :title="editingId ? '编辑开局模板' : '新建开局模板'" width="820px" :close-on-click-modal="false">
      <el-form :model="form" label-width="100px" size="default">
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="模板名"><el-input v-model="form.name" /></el-form-item></el-col>
          <el-col :span="8">
            <el-form-item label="厂商">
              <el-select v-model="form.vendor" @change="onVendorChange">
                <el-option label="H3C (推荐)" value="h3c" />
                <el-option label="华为 VRP5 (S 系列交换机/AR 路由器)" value="huawei" />
                <el-option label="华为 VRP8 (CE/NE 系列，CloudEngine)" value="huawei-ce" />
                <el-option label="思科" value="cisco" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="域名"><el-input v-model="form.domain_name" placeholder="可选" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">管理网络</el-divider>
        <el-row :gutter="12">
          <el-col :span="6"><el-form-item label="管理VLAN"><el-input-number v-model="form.mgmt_vlan" :min="1" :max="4094" style="width:100%" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="管理SVI"><el-input v-model="form.mgmt_interface" /></el-form-item></el-col>
          <el-col :span="9"><el-form-item label="掩码"><el-input v-model="form.mgmt_netmask" /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="网关"><el-input v-model="form.mgmt_gateway" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DNS"><el-input v-model="form.dns_servers" placeholder="逗号分隔" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="NTP">
            <el-input v-model="form.ntp_server" placeholder="各现场不同；留空 = 不下发 NTP" />
          </el-form-item></el-col>
        </el-row>
        <el-form-item label="VLAN规划">
          <el-input v-model="form.vlans_text" type="textarea" :rows="2" placeholder="每行: VLAN号,名称" />
        </el-form-item>

        <el-divider content-position="left">账号与安全</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="管理员"><el-input v-model="form.admin_user" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="密码"><el-input v-model="form.admin_password" type="password" show-password placeholder="新建必填；编辑时留空=不修改" /></el-form-item></el-col>
          <el-col :span="8" v-if="form.vendor === 'cisco'"><el-form-item label="Enable密钥"><el-input v-model="form.enable_secret" type="password" show-password /></el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="SNMP团体"><el-input v-model="form.snmp_community" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">端口与自定义</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="上联口"><el-input v-model="form.uplink_port" placeholder="GigabitEthernet1/0/48" /></el-form-item></el-col>
          <el-col :span="16"><el-form-item label="接入口"><el-input v-model="form.access_ports" placeholder="逗号分隔" /></el-form-item></el-col>
        </el-row>
        <el-form-item label="自定义配置"><el-input v-model="form.extra_config" type="textarea" :rows="3" placeholder="追加的厂商 CLI (可选)" /></el-form-item>

        <el-divider content-position="left">ZTP 投递</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="投递模式">
              <el-select v-model="form.deploy_mode">
                <el-option label="独立DHCP" value="standalone" />
                <el-option label="ProxyDHCP" value="proxy" />
                <el-option label="中继模式" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="服务器IP"><el-input v-model="form.server_ip" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DHCP网卡">
            <el-input v-model="form.dhcp_iface" placeholder="必填，例：ens19（不能是承载默认路由的网卡）" />
          </el-form-item></el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="DHCP起始"><el-input v-model="form.dhcp_start" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DHCP结束"><el-input v-model="form.dhcp_end" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="HTTP根"><el-input v-model="form.http_root" /></el-form-item></el-col>
        </el-row>
      </el-form>
      <template #footer>
        <el-button @click="templateDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveTemplate">保存模板</el-button>
      </template>
    </el-dialog>

    <!-- 设备添加弹窗 -->
    <el-dialog v-model="deviceDialog" title="添加 ZTP 设备" width="560px">
      <el-form :model="devForm" label-width="80px" size="default">
        <el-form-item label="关联模板">
          <el-select v-model="devForm.template_id" filterable placeholder="选择模板" style="width:100%">
            <el-option v-for="t in templates" :key="t.id" :label="t.name + ' (' + vendorLabel(t.vendor) + ')'" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="主机名"><el-input v-model="devForm.hostname" placeholder="Core-SW01" /></el-form-item>
        <el-form-item label="MAC地址"><el-input v-model="devForm.mac" placeholder="aa:bb:cc:dd:ee:ff" /></el-form-item>
        <el-form-item label="序列号"><el-input v-model="devForm.serial" placeholder="可选, 用于文件命名" /></el-form-item>
        <el-form-item label="管理IP"><el-input v-model="devForm.mgmt_ip" placeholder="可选" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deviceDialog = false">取消</el-button>
        <el-button type="primary" @click="addDevice">添加</el-button>
      </template>
    </el-dialog>

    <!-- 生成弹窗 -->
    <el-dialog v-model="genDialog" title="ZTP 部署文件生成" width="900px" top="5vh">
      <el-form label-width="90px" size="small" style="margin-bottom: 12px">
        <el-row :gutter="8">
          <el-col :span="8">
            <el-form-item label="投递模式">
              <el-select v-model="genForm.deploy_mode" style="width:100%">
                <el-option label="独立DHCP (专用开局网络)" value="standalone" />
                <el-option label="ProxyDHCP (与现有DHCP并存)" value="proxy" />
                <el-option label="中继模式 (仅TFTP)" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="服务器IP"><el-input v-model="genForm.server_ip" /></el-form-item></el-col>
          <el-col :span="8" style="text-align:right">
            <el-button type="primary" size="small" @click="doGenerate" :loading="generating"><el-icon><Check /></el-icon> 生成文件</el-button>
          <el-button type="success" size="small" @click="doDownload" :disabled="!Object.keys(genFiles).length"><el-icon><Download /></el-icon> 下载 ZIP</el-button>
          <el-button type="warning" size="small" @click="doDeploy" :loading="deploying" :disabled="!Object.keys(genFiles).length"><el-icon><Promotion /></el-icon> 部署到本机</el-button>
          </el-col>
        </el-row>
        <el-form-item label="临时设备" v-if="genForm.devices.length">
          <el-tag v-for="(d, i) in genForm.devices" :key="i" closable @close="genForm.devices.splice(i,1)" size="small" style="margin-right:6px">
            {{ d.hostname }} / {{ d.mac || '无MAC' }}
          </el-tag>
        </el-form-item>
      </el-form>
      <!-- R4：DHCP 网卡没填时，生成的配置里是占位 eth0 —— 后端会**拒绝部署**
           （占位值 / 不存在的网卡 / 承载默认路由的骨干网卡都会被拦）。
           在界面上先说清楚，比让运维撞一个 422 友好。 -->
      <el-alert v-if="genIfacePlaceholder" type="warning" :closable="false" show-icon style="margin-bottom: 8px">
        <template #title>该模板没有指定「DHCP网卡」，生成的配置是占位 interface=eth0</template>
        <div style="font-size:12px;line-height:1.6">
          这份配置可以下载，但<strong>不能部署</strong>：dnsmasq 配了 bind-interfaces，网卡不存在会直接起不来
          （而它同时服务着 PXE）；填错成承载默认路由的网卡，则会在骨干网段上开 DHCP 池、抢答企业 DHCP。
          请点「编辑」把 DHCP网卡 填成宿主机上真实存在、且不承载默认路由的那张卡
          （本项目里是 <code>ens19</code>）。
        </div>
      </el-alert>
      <div style="margin-bottom: 8px">
        <el-button size="small" @click="addInlineDevice"><el-icon><Plus /></el-icon> 添加临时设备</el-button>
      </div>
      <el-tabs v-model="activeFile" v-if="Object.keys(genFiles).length">
        <el-tab-pane v-for="(_, name) in genFiles" :key="name" :label="name" :name="name">
          <div class="terminal-output" style="white-space: pre; max-height: 440px">{{ genFiles[name] }}</div>
        </el-tab-pane>
      </el-tabs>
      <el-alert v-if="deployResult.length" :type="deployOk ? 'success' : 'error'" :closable="false" style="margin-top: 12px">
        <div v-for="(ln, i) in deployResult" :key="i" style="font-family: monospace; font-size: 12px; white-space: pre-wrap">{{ ln }}</div>
      </el-alert>
    </el-dialog>

    <!-- 临时设备添加 -->
    <el-dialog v-model="inlineDevDialog" title="添加临时设备" width="480px" append-to-body>
      <el-form :model="inlineDev" label-width="80px" size="small">
        <el-form-item label="主机名"><el-input v-model="inlineDev.hostname" /></el-form-item>
        <el-form-item label="MAC"><el-input v-model="inlineDev.mac" placeholder="aa:bb:cc:00:00:01" /></el-form-item>
        <el-form-item label="序列号"><el-input v-model="inlineDev.serial" /></el-form-item>
        <el-form-item label="管理IP"><el-input v-model="inlineDev.mgmt_ip" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="inlineDevDialog = false">取消</el-button>
        <el-button type="primary" @click="confirmInlineDevice">添加</el-button>
      </template>
    </el-dialog>

    <!-- 落位新增/编辑弹窗 -->
    <el-dialog v-model="posDialog" :title="editingPosId ? '编辑落位' : '新增落位'" width="520px">
      <el-form :model="posForm" label-width="100px" size="default">
        <el-form-item label="所属模板">
          <el-select v-model="posForm.template_id" filterable placeholder="选择模板" style="width:100%">
            <el-option v-for="t in templates" :key="t.id" :label="t.name + ' (' + vendorLabel(t.vendor) + ')'" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="落位"><el-input v-model="posForm.position" placeholder="例如 A01-03-U12" /></el-form-item>
        <el-form-item label="规划管理IP"><el-input v-model="posForm.mgmt_ip" placeholder="规划好的管理 IP，例如 10.0.0.12" /></el-form-item>
        <el-form-item label="主机名"><el-input v-model="posForm.hostname" placeholder="可选，留空用落位编码兜底" /></el-form-item>
        <el-form-item label="序列号"><el-input v-model="posForm.serial" placeholder="可选" /></el-form-item>
        <el-form-item label="MAC"><el-input v-model="posForm.mac" placeholder="选填；留空 = 待认领（上电后从租约学到）" /></el-form-item>
        <el-form-item label="备注"><el-input v-model="posForm.remark" type="textarea" :rows="2" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="posDialog = false">取消</el-button>
        <el-button type="primary" :loading="savingPos" @click="savePosition">保存</el-button>
      </template>
    </el-dialog>

    <!-- 落位批量导入弹窗 -->
    <el-dialog v-model="importDialog" title="批量导入落位" width="640px">
      <el-alert type="info" :closable="false" style="margin-bottom: 8px">
        <div style="font-size:12px;line-height:1.6">
          每行一条，逗号分隔：<code>落位,管理IP,主机名,序列号,MAC,备注</code>；
          带表头也可以（自动跳过）；MAC 留空 = 待认领。
        </div>
      </el-alert>
      <el-form label-width="100px" size="default">
        <el-form-item label="所属模板">
          <el-select v-model="importForm.template_id" filterable placeholder="选择模板" style="width:100%">
            <el-option v-for="t in templates" :key="t.id" :label="t.name + ' (' + vendorLabel(t.vendor) + ')'" :value="t.id" />
          </el-select>
        </el-form-item>
        <el-form-item label="清空再导入">
          <el-switch v-model="importForm.replace" />
          <span style="margin-left:8px;font-size:12px;color:#909399">开启 = 先删除该模板已有全部落位（不可恢复）</span>
        </el-form-item>
        <el-form-item label="CSV 内容">
          <el-input v-model="importForm.csv" type="textarea" :rows="8" placeholder="A01-03-U12,10.0.0.12,sw12,SN12,,备注" />
        </el-form-item>
      </el-form>
      <el-alert v-if="importResult" :type="importResult.errors && importResult.errors.length ? 'warning' : 'success'" :closable="false">
        <div style="font-size:12px;line-height:1.6">
          新增 {{ importResult.created }} 条，更新 {{ importResult.updated }} 条，跳过 {{ importResult.skipped }} 条
          <div v-for="(e, i) in importResult.errors" :key="i" style="color:#b8860b">{{ e }}</div>
        </div>
      </el-alert>
      <template #footer>
        <el-button @click="importDialog = false">关闭</el-button>
        <el-button type="primary" :loading="importing" @click="doImport">导入</el-button>
      </template>
    </el-dialog>

    <!-- 认领到落位弹窗 -->
    <el-dialog v-model="claimDialog" title="认领到落位" width="520px" append-to-body>
      <div v-if="claimTarget" style="margin-bottom: 12px; font-size: 13px">
        设备 MAC：<b>{{ claimTarget.mac }}</b>
        <span v-if="claimTarget.ip">（拿到 IP {{ claimTarget.ip }}）</span>
      </div>
      <el-form label-width="100px" size="default">
        <el-form-item label="选择落位">
          <el-select v-model="claimPosId" filterable placeholder="选择一条待认领落位" style="width:100%">
            <el-option v-for="p in claimablePositions" :key="p.id"
                       :label="p.position + ' / ' + p.mgmt_ip + (p.hostname ? ' / ' + p.hostname : '')"
                       :value="p.id" />
          </el-select>
        </el-form-item>
      </el-form>
      <el-alert v-if="claimTarget && !claimablePositions.length" type="warning" :closable="false"
                title="该模板下没有「待认领」落位，请先新增落位（MAC 留空）" />
      <template #footer>
        <el-button @click="claimDialog = false">取消</el-button>
        <el-button type="primary" :loading="claiming" @click="confirmClaim">认领</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, computed, onMounted, onBeforeUnmount } from "vue"
import http, { downloadZip } from "../api"
import { ElMessage } from "element-plus"

const serverStatus = ref({})
const templates = ref([])
const devices = ref([])
const templateDialog = ref(false)
const deviceDialog = ref(false)
const editingId = ref(null)
const saving = ref(false)
const genDialog = ref(false)
const generating = ref(false)
const genFiles = ref({})
const activeFile = ref("")
const deploying = ref(false)
const deployResult = ref([])
const deployOk = ref(true)
// R4：当前生成用的模板的 DHCP 网卡（空 / eth0 = 占位值，生成的配置不能部署）
const genIface = ref("")
const genIfacePlaceholder = computed(
  () => ["", "eth0", "eth1", "auto", "detect"].includes((genIface.value || "").trim().toLowerCase())
)
const inlineDevDialog = ref(false)

const emptyForm = () => ({
  name: "", vendor: "h3c", domain_name: "",
  mgmt_vlan: 10, mgmt_interface: "Vlan-interface10", mgmt_netmask: "255.255.255.0",
  mgmt_gateway: "10.0.0.254", dns_servers: "114.114.114.114", ntp_server: "",
  vlans_text: "",
  admin_user: "admin", admin_password: "", enable_secret: "", snmp_community: "public",
  uplink_port: "", access_ports: "", extra_config: "",
  deploy_mode: "standalone", server_ip: "10.0.0.250", dhcp_iface: "eth0",
  dhcp_start: "10.0.0.100", dhcp_end: "10.0.0.200", http_root: "http://10.0.0.250:8000/ztp",
})
const form = reactive(emptyForm())

const devForm = reactive({ template_id: "", hostname: "", mac: "", serial: "", mgmt_ip: "" })
const inlineDev = reactive({ hostname: "", mac: "", serial: "", mgmt_ip: "" })

const genForm = reactive({ deploy_mode: "standalone", server_ip: "10.0.0.250", devices: [] })

function vendorLabel(v) { return { h3c: "H3C", huawei: "华为 VRP5", "huawei-ce": "华为 VRP8(CE)", cisco: "思科" }[v] || v }
function vendorType(v) { return { h3c: "primary", huawei: "success", "huawei-ce": "success", cisco: "warning" }[v] || "info" }
function modeLabel(m) { return { standalone: "独立DHCP", proxy: "ProxyDHCP", relay: "中继" }[m] || m }

function onVendorChange() {
  if (form.vendor === "huawei" || form.vendor === "huawei-ce") {
    form.mgmt_interface = form.mgmt_interface.replace("Vlan-interface", "Vlanif")
  } else if (form.vendor === "h3c") {
    form.mgmt_interface = form.mgmt_interface.replace("Vlanif", "Vlan-interface")
  }
  // VRP8(CE) 两条真机约束，提前把默认值补齐，省得生成时才发现（生成期也会 fail-closed 拦住）：
  //   · 本地用户名至少 6 位（`local-user ?` 帮助是 STRING<6-253>）
  //   · SNMP 团体名 8-32 位
  if (form.vendor === "huawei-ce") {
    if (!form.admin_user || form.admin_user.length < 6) form.admin_user = "opstkadm"
    if (!form.snmp_community || form.snmp_community.length < 8) form.snmp_community = "Opstk@2026"
  }
}

function buildPayload() {
  const vlans = form.vlans_text.split("\n").map(line => {
    const parts = line.split(",").map(s => s.trim())
    if (!parts[0]) return null
    return { id: parseInt(parts[0]) || 0, name: parts[1] || "" }
  }).filter(Boolean)
  return {
    name: form.name, vendor: form.vendor, domain_name: form.domain_name,
    mgmt_vlan: form.mgmt_vlan, mgmt_interface: form.mgmt_interface, mgmt_netmask: form.mgmt_netmask,
    mgmt_gateway: form.mgmt_gateway,
    dns_servers: form.dns_servers.split(",").map(d => d.trim()).filter(Boolean),
    ntp_server: form.ntp_server, vlans,
    admin_user: form.admin_user,
    admin_password: form.admin_password || null,
    enable_secret: form.enable_secret || null,
    snmp_community: form.snmp_community,
    uplink_port: form.uplink_port,
    access_ports: form.access_ports.split(",").map(p => p.trim()).filter(Boolean),
    extra_config: form.extra_config,
    deploy_mode: form.deploy_mode, server_ip: form.server_ip,
    dhcp_iface: form.dhcp_iface, dhcp_start: form.dhcp_start, dhcp_end: form.dhcp_end,
    http_root: form.http_root,
  }
}

function fillForm(t) {
  Object.assign(form, emptyForm())
  form.name = t.name; form.vendor = t.vendor; form.domain_name = t.domain_name || ""
  form.mgmt_vlan = t.mgmt_vlan; form.mgmt_interface = t.mgmt_interface; form.mgmt_netmask = t.mgmt_netmask
  form.mgmt_gateway = t.mgmt_gateway
  form.dns_servers = (t.dns_servers || []).join(",")
  form.ntp_server = t.ntp_server
  form.vlans_text = (t.vlans || []).map(v => v.id + "," + (v.name || "")).join("\n")
  form.admin_user = t.admin_user; form.snmp_community = t.snmp_community
  form.uplink_port = t.uplink_port || ""
  form.access_ports = (t.access_ports || []).join(",")
  form.extra_config = t.extra_config || ""
  form.deploy_mode = t.deploy_mode; form.server_ip = t.server_ip
  form.dhcp_iface = t.dhcp_iface; form.dhcp_start = t.dhcp_start; form.dhcp_end = t.dhcp_end
  form.http_root = t.http_root
}

function openTemplateDialog(row) {
  Object.assign(form, emptyForm())
  editingId.value = null
  if (row) { fillForm(row); editingId.value = row.id }
  templateDialog.value = true
}

async function saveTemplate() {
  if (!form.name) { ElMessage.warning("请输入模板名"); return }
  // R4：DHCP 网卡是必填的 —— 留空/占位 eth0 时生成的配置会被部署接口直接拒绝
  // （占位值、不存在的网卡、承载默认路由的骨干网卡都不允许）。与其让运维撞 422，
  // 不如在保存模板时就说清楚。
  const iface = (form.dhcp_iface || "").trim().toLowerCase()
  if (!iface || ["eth0", "eth1", "auto", "detect"].includes(iface)) {
    ElMessage.warning("请填写「DHCP网卡」：宿主机上真实存在、且不承载默认路由的那张卡（例：ens19）")
    return
  }
  // 新建时必须有设备管理员口令：ZTP 生成器现在**不代填**默认口令（旧的 ChangeMe@123
  // 是公开仓库里的常量，等于给设备发一个全网都知道的口令），没有口令的模板一生成就 422。
  // 挡在这里比让运维撞 422 友好。编辑时留空仍表示"不修改"（后端 null 时不改原值）。
  if (!editingId.value && !form.admin_password) {
    ElMessage.warning("请填写设备管理员口令（新建必填；编辑时留空表示不修改）")
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    if (editingId.value) await http.put("/ct/ztp/templates/" + editingId.value, payload)
    else await http.post("/ct/ztp/templates", payload)
    ElMessage.success("保存成功")
    templateDialog.value = false
    loadTemplates()
  } finally { saving.value = false }
}

async function delTemplate(id) {
  await http.delete("/ct/ztp/templates/" + id)
  ElMessage.success("已删除")
  loadTemplates()
}

async function addDevice() {
  if (!devForm.template_id) { ElMessage.warning("请选择模板"); return }
  await http.post("/ct/ztp/devices", { ...devForm })
  ElMessage.success("设备已添加")
  deviceDialog.value = false
  Object.assign(devForm, { template_id: "", hostname: "", mac: "", serial: "", mgmt_ip: "" })
  loadDevices()
}

async function delDevice(id) {
  await http.delete("/ct/ztp/devices/" + id)
  ElMessage.success("已删除")
  loadDevices()
}

// ---------- 落位登记（落位 + 认领） ----------
const posTemplateId = ref("")
const positions = ref([])
const observations = ref([])
const obsNote = ref("")
const obsOk = ref(true)     // 租约文件读到了=true（正常）；读不到=false（需要运维处理）
const posDialog = ref(false)
const editingPosId = ref(null)
const savingPos = ref(false)
const posForm = reactive({ template_id: "", position: "", hostname: "", mgmt_ip: "", serial: "", mac: "", remark: "" })
const importDialog = ref(false)
const importing = ref(false)
const importForm = reactive({ template_id: "", csv: "", replace: false })
const importResult = ref(null)
const claimDialog = ref(false)
const claimTarget = ref(null)
const claimPosId = ref("")
const claiming = ref(false)
const claimablePositions = computed(() => positions.value.filter(p => !p.mac))

function validIpv4(s) {
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec((s || "").trim())
  return !!m && m.slice(1).every(x => parseInt(x, 10) <= 255)
}

async function loadPositions() {
  const q = posTemplateId.value ? "?template_id=" + posTemplateId.value : ""
  positions.value = await http.get("/ct/ztp/positions" + q)
}

async function loadObservations() {
  const q = posTemplateId.value ? "?template_id=" + posTemplateId.value : ""
  try {
    const res = await http.get("/ct/ztp/observations" + q)
    observations.value = res.observations || []
    obsNote.value = res.note || ""
    // 读到租约文件（leases_path 非空）是**正常**情况，说明里只是"读了哪个文件、几条"；
    // 只有读不到时才是需要运维处理的告警。以前一律用 warning 黄条 ——
    // 于是正常部署下页面永远挂一条黄警告，久了就没人看告警了。
    obsOk.value = !!res.leases_path
  } catch (e) {
    // ★ 外部审查 U5-F4：失败时**必须清掉上一轮的列表**。以前只在成功分支赋值，
    //   切换模板后仍显示模板 A 的"待认领设备"，而此时点「认领到落位」会把 A 的 MAC
    //   绑到 B 的落位上（confirmClaim 发的是当前选中的 template_id）。
    observations.value = []
    obsOk.value = false
    obsNote.value = "租约读取失败：读取不到设备列表（请看后端 /ct/ztp/observations 的说明），"
      + "当前列表为空，**不要**据此认领。"
  }
}

function loadPositionData() { loadPositions(); loadObservations() }

function openPosDialog(row) {
  editingPosId.value = row ? row.id : null
  Object.assign(posForm, { template_id: posTemplateId.value, position: "", hostname: "", mgmt_ip: "", serial: "", mac: "", remark: "" })
  if (row) Object.assign(posForm, {
    template_id: row.template_id, position: row.position, hostname: row.hostname || "",
    mgmt_ip: row.mgmt_ip || "", serial: row.serial || "", mac: row.mac || "", remark: row.remark || "",
  })
  posDialog.value = true
}

async function savePosition() {
  if (!posForm.template_id) { ElMessage.warning("请选择模板"); return }
  if (!posForm.position.trim()) { ElMessage.warning("请输入落位编码，例如 A01-03-U12"); return }
  if (!validIpv4(posForm.mgmt_ip)) { ElMessage.warning("规划管理IP 需为合法 IPv4，例如 10.0.0.12"); return }
  savingPos.value = true
  try {
    if (editingPosId.value) await http.put("/ct/ztp/positions/" + editingPosId.value, { ...posForm })
    else await http.post("/ct/ztp/positions", { ...posForm })
    ElMessage.success("保存成功")
    posDialog.value = false
    loadPositions()
  } finally { savingPos.value = false }
}

async function delPosition(id) {
  await http.delete("/ct/ztp/positions/" + id)
  ElMessage.success("已删除")
  loadPositions()
}

function openImportDialog() {
  importForm.template_id = posTemplateId.value
  importResult.value = null
  importDialog.value = true
}

async function doImport() {
  if (!importForm.template_id) { ElMessage.warning("请选择模板"); return }
  importing.value = true
  try {
    importResult.value = await http.post("/ct/ztp/positions/import", { ...importForm })
    ElMessage.success("导入完成：新增 " + importResult.value.created + "，更新 " + importResult.value.updated + "，跳过 " + importResult.value.skipped)
    loadPositions()
  } finally { importing.value = false }
}

function openClaimDialog(row) {
  claimTarget.value = row
  claimPosId.value = ""
  claimDialog.value = true
}

async function confirmClaim() {
  if (!claimPosId.value) { ElMessage.warning("请选择一条待认领落位"); return }
  claiming.value = true
  try {
    await http.post("/ct/ztp/claim", {
      template_id: posTemplateId.value, position_id: claimPosId.value, mac: claimTarget.value.mac,
    })
    ElMessage.success("已认领，请重新生成并部署")
    claimDialog.value = false
    loadPositions()
    loadObservations()
  } finally { claiming.value = false }
}

function openGenDialog(row) {
  genForm.deploy_mode = row.deploy_mode || "standalone"
  genForm.server_ip = row.server_ip || "10.0.0.250"
  genForm.devices = []
  // R4：网卡是模板级的（部署接口不接受覆盖），生成/部署前先在界面上点出来
  genIface.value = (row.dhcp_iface || "").trim()
  genDialog.value = true
  genFiles.value = {}
  sessionStorage.setItem("ztp_template_id", row.id)
  doGenerate()
}

function addInlineDevice() {
  Object.assign(inlineDev, { hostname: "", mac: "", serial: "", mgmt_ip: "" })
  inlineDevDialog.value = true
}

function confirmInlineDevice() {
  genForm.devices.push({ ...inlineDev })
  inlineDevDialog.value = false
}

async function doGenerate() {
  generating.value = true
  try {
    const tid = sessionStorage.getItem("ztp_template_id")
    const body = {
      deploy_mode: genForm.deploy_mode,
      server_ip: genForm.server_ip,
      devices: genForm.devices,
    }
    const res = await http.post("/ct/ztp/templates/" + tid + "/generate", body)
    genFiles.value = res.files
    const keys = Object.keys(res.files)
    if (keys.length) activeFile.value = keys[0]
    ElMessage.success("生成完成: " + keys.length + " 个文件")
  } finally { generating.value = false }
}

async function doDownload() {
  const tid = sessionStorage.getItem("ztp_template_id")
  // 只有真拿到 ZIP 才提示"下载已开始"（外部审查 U5-F3：以前失败也提示成功）
  const ok = await downloadZip("/ct/ztp/templates/" + tid + "/download", {
    deploy_mode: genForm.deploy_mode,
    server_ip: genForm.server_ip,
    devices: genForm.devices,
  })
  if (ok !== true) return
  ElMessage.success("下载已开始")
}

async function doDeploy() {
  deploying.value = true
  deployResult.value = []
  deployOk.value = true
  try {
    const tid = sessionStorage.getItem("ztp_template_id")
    const res = await http.post("/ct/ztp/templates/" + tid + "/deploy", {
      deploy_mode: genForm.deploy_mode,
      server_ip: genForm.server_ip,
      devices: genForm.devices,
    })
    deployResult.value = res.log || []
    deployOk.value = !!res.ok
    if (res.ok) ElMessage.success("部署成功")
    else ElMessage.error("部署失败，请查看日志")
  } catch (e) {
    deployOk.value = false
    // 后端 ok=False 时返回 500 + detail（与 PXE 的 /deploy 同口径）；
    // axios 的 e.message 只有一句泛泛的英文，运维要的是后端那句中文原因。
    const detail = e?.response?.data?.detail
    deployResult.value = Array.isArray(detail) ? detail : [detail || e.message || "部署请求失败"]
    ElMessage.error("部署失败")
  } finally {
    deploying.value = false
  }
}

async function loadTemplates() { templates.value = await http.get("/ct/ztp/templates") }
async function loadDevices() { devices.value = await http.get("/ct/ztp/devices") }

async function loadServerStatus() {
  try { serverStatus.value = await http.get("/ct/ztp/server/status") } catch (e) {}
  serverPollTimer = setTimeout(loadServerStatus, 5000)
}

async function controlService(action) {
  try {
    const res = await http.post("/ct/ztp/server/service", { action })
    if (res.log) res.log.forEach(l => ElMessage.info(l))
    ElMessage.success(action + " 已执行")
    loadServerStatus()
  } catch (e) {
    ElMessage.error("服务控制失败")
  }
}

let serverPollTimer = null
onBeforeUnmount(() => { clearTimeout(serverPollTimer) })
onMounted(async () => {
  loadTemplates(); loadDevices(); loadServerStatus()
  // 落位登记默认定位到「生成配置」用过的那个模板（没有就用第一个）
  await loadTemplates()
  const remembered = sessionStorage.getItem("ztp_template_id")
  posTemplateId.value = remembered && templates.value.some(t => t.id === remembered)
    ? remembered
    : (templates.value[0] ? templates.value[0].id : "")
  loadPositionData()
})
</script>
