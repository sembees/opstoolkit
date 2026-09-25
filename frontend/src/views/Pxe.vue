<template>
  <div>
    <!-- PXE 服务器本机状态 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
        <span style="font-weight: 600">
          <el-icon><Cpu /></el-icon> PXE 服务器（本机）
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
        <el-descriptions-item label="sudo 免密">{{ serverStatus.sudo_ok ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="开机自启">{{ serverStatus.dnsmasq && serverStatus.dnsmasq.enabled ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="TFTP">{{ serverStatus.tftp_root }}</el-descriptions-item>
      </el-descriptions>
      <el-row :gutter="16" style="margin-top: 8px" v-if="serverStatus.supported">
        <el-col :span="12">
          <div style="font-size: 12px; color: #999; margin-bottom: 4px">TFTP 文件</div>
          <el-tag v-for="f in serverStatus.tftp_files" :key="f" size="small" style="margin: 2px">{{ f }}</el-tag>
          <span v-if="!serverStatus.tftp_files || !serverStatus.tftp_files.length" style="color: #ccc; font-size: 12px">空</span>
        </el-col>
        <el-col :span="12">
          <div style="font-size: 12px; color: #999; margin-bottom: 4px">HTTP 文件</div>
          <el-tag v-for="f in serverStatus.web_files" :key="f" size="small" style="margin: 2px">{{ f }}</el-tag>
          <span v-if="!serverStatus.web_files || !serverStatus.web_files.length" style="color: #ccc; font-size: 12px">空</span>
        </el-col>
      </el-row>
      <div v-if="deployLog.length" style="margin-top: 8px">
        <div style="font-size: 12px; color: #999; margin-bottom: 4px">部署日志</div>
        <div class="terminal-output" style="white-space: pre; max-height: 200px">{{ deployLog.join("\n") }}</div>
      </div>
      <el-alert v-if="serverStatus.supported === false" type="warning" :closable="false" style="margin-top: 8px">本机部署需 Linux 环境（当前：{{ serverStatus.platform }}），可用「下载 ZIP」手动部署</el-alert>
    </el-card>
        <!-- ISO 管理 -->
        <el-card shadow="never" style="margin-bottom: 16px">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px">
            <span style="font-weight: 600"><el-icon><Files /></el-icon> ISO 镜像管理</span>
            <el-button size="small" @click="loadIsos"><el-icon><Refresh /></el-icon> 刷新</el-button>
          </div>
          <el-alert v-if="isoList.supported === false" type="warning" :closable="false" style="margin-bottom: 8px">需 Linux 环境</el-alert>
          <el-table v-else :data="isoList.isos || []" size="small" empty-text="尚无 ISO 文件，请将 ISO 上传到服务器 /srv/opstk/iso/ 目录">
            <el-table-column prop="name" label="ISO 文件" min-width="280" />
            <el-table-column prop="size_mb" label="大小 (MB)" width="110" />
            <el-table-column label="操作" width="280" fixed="right">
              <template #default="{ row }">
                <el-select v-model="row._osType" size="small" style="width: 90px; margin-right: 6px">
                  <el-option label="Ubuntu" value="ubuntu" />
                  <el-option label="RHEL" value="rhel" />
                </el-select>
                <el-input v-model="row._osVer" size="small" style="width: 80px; margin-right: 6px" placeholder="22.04" />
                <el-button type="success" link size="small" @click="extractIso(row)" :loading="row._extracting">提取</el-button>
                <el-popconfirm title="确定删除?" @confirm="delIso(row.name)">
                  <template #reference><el-button type="danger" link size="small">删除</el-button></template>
                </el-popconfirm>
              </template>
            </el-table-column>
          </el-table>
          <div v-if="extractLog.length" style="margin-top: 8px">
            <div style="font-size: 12px; color: #999; margin-bottom: 4px">提取日志</div>
            <div class="terminal-output" style="white-space: pre; max-height: 200px">{{ extractLog.join('\n') }}</div>
          </div>
        </el-card>
        <!-- 模板列表 -->
    <el-card shadow="never" style="margin-bottom: 16px">
      <div style="display: flex; justify-content: space-between; margin-bottom: 12px">
        <span style="font-weight: 600"><el-icon><Cpu /></el-icon> PXE 装机模板</span>
        <el-button type="primary" @click="openProfileDialog()"><el-icon><Plus /></el-icon> 新建装机模板</el-button>
      </div>
      <el-table :data="profiles" stripe size="small">
        <el-table-column prop="name" label="模板名称" min-width="130" />
        <el-table-column label="系统" width="120">
          <template #default="{ row }">
            <el-tag size="small" :type="row.os_type === 'ubuntu' ? 'success' : 'danger'">{{ osLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="disk_scheme" label="磁盘" width="70" />
        <el-table-column prop="net_mode" label="网络" width="70" />
        <el-table-column prop="timezone" label="时区" width="120" />
        <el-table-column prop="mirror" label="镜像源" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openGenDialog(row)">生成配置</el-button>
            <el-button type="success" link size="small" @click="deployProfile(row)">部署</el-button>
            <el-button link type="primary" size="small" @click="openProfileDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delProfile(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 装机记录 -->
    <el-card shadow="never">
      <template #header><span style="font-weight: 600"><el-icon><Monitor /></el-icon> 装机记录</span></template>
      <el-table :data="installs" stripe size="small">
        <el-table-column prop="hostname" label="主机名" min-width="120" />
        <el-table-column prop="mac" label="MAC 地址" width="160" />
        <el-table-column prop="ip" label="分配 IP" width="120" />
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="160">
          <template #default="{ row }">{{ row.created_at ? row.created_at.replace('T',' ').slice(0,19) : '' }}</template>
        </el-table-column>
        <el-table-column label="操作" width="90" fixed="right">
          <template #default="{ row }">
            <el-popconfirm title="确定删除?" @confirm="delInstall(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </el-card>

    <!-- 模板编辑弹窗 -->
    <el-dialog v-model="profileDialog" :title="editingId ? '编辑装机模板' : '新建装机模板'" width="760px" :close-on-click-modal="false">
      <el-form :model="form" label-width="90px" size="default">
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="模板名"><el-input v-model="form.name" /></el-form-item></el-col>
          <el-col :span="8">
            <el-form-item label="系统">
              <el-select v-model="form.os_type" @change="onOsChange">
                <el-option label="Ubuntu 22.04+" value="ubuntu" /><el-option label="RHEL/Rocky/Alma 8+" value="rhel" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="版本">
              <el-select v-model="form.os_version" filterable allow-create default-first-option
                         style="width: 100%" placeholder="选一个已提取介质的版本">
                <el-option v-for="v in versionOptions" :key="v.value" :label="v.label" :value="v.value" />
              </el-select>
              <div v-if="form.os_version && !mediaReady"
                   style="margin-top: 4px; font-size: 12px; line-height: 1.4; color: var(--el-color-warning)">
                没有 {{ form.os_type }}/{{ form.os_version }}/ 的引导介质，装机时 iPXE 会报 "Could not boot image"
              </div>
            </el-form-item>
          </el-col>
        </el-row>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="时区"><el-input v-model="form.timezone" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="语言"><el-input v-model="form.locale" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="键盘"><el-input v-model="form.keyboard" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">账号</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="管理员"><el-input v-model="form.admin_user" /></el-form-item></el-col>
          <el-col :span="8">
            <el-form-item label="管理员密码">
              <el-input v-model="form.admin_password" type="password" show-password
                        :placeholder="editingId ? '留空不修改' : '新建必填'" />
              <div v-if="!editingId && !form.admin_password"
                   style="margin-top: 4px; font-size: 12px; line-height: 1.4; color: var(--el-color-warning)">
                新建时必填：这是裸机 root 口令，后端不允许留空，也不会代填任何默认值
              </div>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="root密码" v-if="form.os_type === 'rhel'"><el-input v-model="form.root_password" type="password" show-password /></el-form-item></el-col>
        </el-row>
        <el-form-item label="SSH公钥">
          <el-input v-model="form.ssh_keys_text" type="textarea" :rows="2" placeholder="每行一个公钥" />
        </el-form-item>

        <el-divider content-position="left">磁盘</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="分区方案">
              <el-select v-model="form.disk_scheme">
                <el-option label="LVM (推荐)" value="lvm" />
                <el-option label="直通分区" value="direct" />
                <el-option label="ZFS" value="zfs" />
                <el-option label="自定义分区表" value="custom" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="目标磁盘">
              <el-select v-model="form.disk_target_mode">
                <el-option label="自动（选最大的盘）" value="auto" />
                <el-option label="按盘名指定" value="name" />
                <el-option label="按序列号/型号匹配" value="match" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8" v-if="form.disk_target_mode === 'name'">
            <el-form-item label="磁盘名">
              <el-input v-model="form.disk_name" placeholder="sda / vda / nvme0n1" />
            </el-form-item>
          </el-col>
          <el-col :span="8" v-if="form.disk_target_mode === 'match'">
            <el-form-item label="序列号">
              <el-input v-model="form.disk_serial" placeholder="序列号最稳，如 S3Z1NB0K123456" />
            </el-form-item>
          </el-col>
          <el-col :span="8" v-if="form.disk_target_mode === 'match'">
            <el-form-item label="型号（序列号为空时用）">
              <el-input v-model="form.disk_model" placeholder="如 INTEL SSDSC2KB480G8" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="忽略小于(GB)">
              <el-input-number v-model="form.disk_min_size_gb" :min="0" :max="100000"
                               controls-position="right" style="width: 100%" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="清空目标盘">
              <el-switch v-model="form.disk_wipe" />
              <span style="margin-left: 8px; font-size: 12px; color: var(--el-text-color-secondary)">
                只清空"目标磁盘"，其它盘一律不碰
              </span>
            </el-form-item>
          </el-col>
        </el-row>
        <el-alert v-if="form.disk_target_mode === 'auto'" type="info" :closable="false"
                  style="margin-bottom: 12px"
                  title="自动选盘：换硬件不用改模板" />
        <div v-if="form.disk_target_mode === 'auto'" style="margin: -8px 0 12px; font-size: 12px; line-height: 1.6; color: var(--el-text-color-secondary)">
          Ubuntu 交给 subiquity 的"最大盘"规则；RHEL 系（anaconda 没有现成的自动选盘原语）
          在 <code>%pre</code> 里按"非可移动、非光驱、容量达标、按盘名排序取第一块"选出目标盘，
          再 <code>%include</code> 生成出来的分区片段。盘名不再写死，NVMe(<code>nvme0n1</code>) /
          virtio-blk(<code>vda</code>) 都能装。
        </div>

        <template v-if="form.disk_scheme === 'custom'">
          <el-divider content-position="left">自定义分区表</el-divider>
          <div style="margin-bottom: 6px; font-size: 12px; color: var(--el-text-color-secondary)">
            大小为 <code>512M</code>/<code>20G</code>/<code>rest</code>（<code>rest</code> 只能放在最后一行，表示用掉剩余空间）；
            挂载点留空 = 只建分区不挂载；填了 VG 与 LV 才是 LVM 逻辑卷。
          </div>
          <el-table :data="form.partitions" size="small" style="margin-bottom: 6px">
            <el-table-column label="挂载点" width="140">
              <template #default="{ row }"><el-input v-model="row.mount" placeholder="/ 或 swap" /></template>
            </el-table-column>
            <el-table-column label="大小" width="120">
              <template #default="{ row }"><el-input v-model="row.size" placeholder="20G / rest" /></template>
            </el-table-column>
            <el-table-column label="文件系统" width="130">
              <template #default="{ row }">
                <el-select v-model="row.fstype" clearable placeholder="自动">
                  <el-option v-for="f in FSTYPES" :key="f" :label="f" :value="f" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="卷组 VG" width="120">
              <template #default="{ row }"><el-input v-model="row.vg" placeholder="留空=普通分区" /></template>
            </el-table-column>
            <el-table-column label="逻辑卷 LV" width="120">
              <template #default="{ row }"><el-input v-model="row.lv" placeholder="留空=普通分区" /></template>
            </el-table-column>
            <el-table-column label="操作" width="70">
              <template #default="scope">
                <el-button link type="danger" @click="form.partitions.splice(scope.$index, 1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div style="margin-bottom: 12px">
            <el-button size="small" @click="addPartition()">+ 加一个分区</el-button>
            <el-button size="small" @click="applyPreset()">套用：EFI + boot + swap + LVM(/)</el-button>
          </div>

          <el-divider content-position="left">其它数据盘（默认<b>不格式化</b>）</el-divider>
          <div style="margin-bottom: 6px; font-size: 12px; color: var(--el-text-color-secondary)">
            这里只做"挂载"。要格式化别的盘必须显式打开下面的开关 —— 生产上默认不动数据盘。
          </div>
          <el-table :data="form.data_disks" size="small" style="margin-bottom: 6px">
            <el-table-column label="盘名" width="140">
              <template #default="{ row }"><el-input v-model="row.name" placeholder="sdb" /></template>
            </el-table-column>
            <el-table-column label="挂载点" width="160">
              <template #default="{ row }"><el-input v-model="row.mount" placeholder="/data" /></template>
            </el-table-column>
            <el-table-column label="文件系统" width="130">
              <template #default="{ row }">
                <el-select v-model="row.fstype" clearable>
                  <el-option v-for="f in FSTYPES" :key="f" :label="f" :value="f" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="格式化" width="100">
              <template #default="{ row }"><el-switch v-model="row.wipe" /></template>
            </el-table-column>
            <el-table-column label="操作" width="70">
              <template #default="scope">
                <el-button link type="danger" @click="form.data_disks.splice(scope.$index, 1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div style="margin-bottom: 12px">
            <el-button size="small" @click="form.data_disks.push({ name: '', mount: '', fstype: 'xfs', wipe: false })">
              + 加一块数据盘
            </el-button>
          </div>

          <el-divider content-position="left">RAID（可选）</el-divider>
          <el-table :data="form.raid" size="small" style="margin-bottom: 6px">
            <el-table-column label="名称" width="110">
              <template #default="{ row }"><el-input v-model="row.name" placeholder="md0" /></template>
            </el-table-column>
            <el-table-column label="级别" width="110">
              <template #default="{ row }">
                <el-select v-model="row.level">
                  <el-option v-for="l in [0, 1, 5, 6, 10]" :key="l" :label="'RAID' + l" :value="l" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="成员（上面第几个分区，逗号分隔）">
              <template #default="{ row }"><el-input v-model="row.devices" placeholder="例如 3,4" /></template>
            </el-table-column>
            <el-table-column label="挂载点" width="130">
              <template #default="{ row }"><el-input v-model="row.mount" placeholder="/data" /></template>
            </el-table-column>
            <el-table-column label="文件系统" width="120">
              <template #default="{ row }">
                <el-select v-model="row.fstype" clearable>
                  <el-option v-for="f in FSTYPES" :key="f" :label="f" :value="f" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="操作" width="70">
              <template #default="scope">
                <el-button link type="danger" @click="form.raid.splice(scope.$index, 1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div style="margin-bottom: 12px">
            <el-button size="small" @click="form.raid.push({ name: 'md0', level: 1, devices: '', mount: '', fstype: 'xfs' })">
              + 加一个 RAID
            </el-button>
          </div>
        </template>

        <el-divider content-position="left">网络</el-divider>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="模式">
              <el-select v-model="form.net_mode"><el-option label="DHCP" value="dhcp" /><el-option label="静态" value="static" /></el-select>
            </el-form-item>
          </el-col>
          <template v-if="form.net_mode === 'static'">
            <el-col :span="8"><el-form-item label="网卡名"><el-input v-model="form.net_interface" placeholder="ens33" /></el-form-item></el-col>
            <el-col :span="8"><el-form-item label="IP"><el-input v-model="form.net_ip" /></el-form-item></el-col>
          </template>
        </el-row>
        <el-row :gutter="12" v-if="form.net_mode === 'static'">
          <el-col :span="8"><el-form-item label="掩码"><el-input v-model="form.net_netmask" placeholder="255.255.255.0" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="网关"><el-input v-model="form.net_gateway" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="DNS"><el-input v-model="form.net_dns" placeholder="逗号分隔" /></el-form-item></el-col>
        </el-row>

        <el-divider content-position="left">镜像与软件</el-divider>
        <el-form-item label="镜像源"><el-input v-model="form.mirror" placeholder="http://mirror/rocky/9/BaseOS/x86_64/os/" /></el-form-item>
        <el-form-item label="额外软件"><el-input v-model="form.extra_packages_text" placeholder="逗号分隔: vim, net-tools, htop" /></el-form-item>
        <el-form-item label="安装后脚本"><el-input v-model="form.post_script" type="textarea" :rows="3" placeholder="bash 脚本 (可选)" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="profileDialog = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveProfile">保存模板</el-button>
      </template>
    </el-dialog>

    <!-- 配置生成弹窗 -->
    <el-dialog v-model="genDialog" title="PXE 部署文件生成" width="860px" top="5vh">
      <el-form label-width="90px" size="small" style="margin-bottom: 12px">
        <el-row :gutter="8">
          <el-col :span="8">
            <el-form-item label="部署模式">
              <el-select v-model="genForm.deploy_mode" style="width:100%">
                <el-option label="独立DHCP (专用装机网络)" value="standalone" />
                <el-option label="ProxyDHCP (与现有DHCP并存)" value="proxy" />
                <el-option label="中继模式 (仅TFTP, 依赖交换机)" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6"><el-form-item label="主机名"><el-input v-model="genForm.hostname" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="PXE服务IP"><el-input v-model="genForm.server_ip" placeholder="PXE服务本机IP，如 10.128.118.113" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="内核路径"><el-input v-model="genForm.kernel_path" placeholder="rhel/9/vmlinuz" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="initrd"><el-input v-model="genForm.initrd_path" placeholder="rhel/9/initrd.img" /></el-form-item></el-col>
          <el-col :span="8"><el-form-item label="HTTP根地址"><el-input v-model="genForm.http_root" placeholder="留空由后端生成，格式 http://<IP>:8000/pxe/serve" /></el-form-item></el-col>
        </el-row>
        <el-button type="primary" size="small" @click="doGenerate" :loading="generating"><el-icon><Check /></el-icon> 生成文件</el-button>
          <el-button type="success" size="small" @click="doDownload" :disabled="!Object.keys(genFiles).length"><el-icon><Download /></el-icon> 下载 ZIP</el-button>
      </el-form>
      <el-tabs v-model="activeFile" v-if="Object.keys(genFiles).length">
        <el-tab-pane v-for="(_, name) in genFiles" :key="name" :label="name" :name="name">
          <div class="terminal-output" style="white-space: pre; max-height: 420px">{{ genFiles[name] }}</div>
        </el-tab-pane>
      </el-tabs>
    </el-dialog>

    <!-- 部署确认弹窗：deploy_mode 默认 proxy（更安全），server_ip 留空由后端自动探测 -->
    <el-dialog v-model="deployDialog" title="确认部署" width="560px" :close-on-click-modal="false">
      <el-form label-width="100px" size="default">
        <el-form-item label="装机模板">
          <span>{{ deployRow ? (deployRow.name || ('#' + deployRow.id)) : '-' }}</span>
        </el-form-item>
        <el-form-item label="部署模式">
          <el-select v-model="deployForm.deploy_mode" style="width: 100%">
            <el-option label="ProxyDHCP (与现有DHCP并存, 推荐)" value="proxy" />
            <el-option label="独立DHCP (专用装机网络)" value="standalone" />
            <el-option label="中继模式 (仅TFTP, 依赖交换机)" value="relay" />
          </el-select>
        </el-form-item>
        <el-form-item label="PXE服务IP">
          <el-input v-model="deployForm.server_ip" placeholder="留空则由后端自动探测本机IP" />
        </el-form-item>
      </el-form>
      <el-alert v-if="deployForm.deploy_mode === 'standalone'" type="warning" :closable="false" show-icon>
        将在本网段启动完整 DHCP，确认无其他 DHCP 服务器，否则会与现有 DHCP 冲突导致断网！
      </el-alert>
      <el-alert v-else type="info" :closable="false" show-icon>
        {{ deployForm.deploy_mode === 'proxy' ? 'ProxyDHCP 模式与现有 DHCP 并存，不分配地址，影响面小。' : '中继模式仅提供引导，依赖外部 DHCP 与交换机 IP helpers。' }}
      </el-alert>
      <template #footer>
        <el-button @click="deployDialog = false">取消</el-button>
        <el-button type="primary" :loading="deploying" @click="confirmDeploy">确认部署</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, computed } from "vue"
import http, { downloadZip } from "../api"
import { ElMessage } from "element-plus"

const profiles = ref([])
const installs = ref([])
const profileDialog = ref(false)
const editingId = ref(null)
const saving = ref(false)
const genDialog = ref(false)
const generating = ref(false)
const genFiles = ref({})
const activeFile = ref("")
const serverStatus = ref({ supported: false })
const deployLog = ref([])
const deploying = ref(false)

// 部署确认弹窗：deploy_mode 默认 proxy（与现有 DHCP 并存，更安全）；server_ip 留空由后端自动探测
const deployDialog = ref(false)
const deployRow = ref(null)
const deployForm = reactive({ deploy_mode: "proxy", server_ip: "" })
const isoList = ref({ supported: false, isos: [] })
// 已提取的引导介质（os_type/os_version）。"版本"必须从这里选：生成出来的 kernel URL
// 是按 os_type+os_version 拼的，版本对不上就是 404（iPXE 只会报 Could not boot image）。
const mediaList = ref({ supported: true, media: [] })
const extractLog = ref([])

async function loadServerStatus() {
  try { serverStatus.value = await http.get("/it/pxe/server/status") } catch(e) {}
  serverPollTimer = setTimeout(loadServerStatus, 5000)
}
async function controlService(action) {
  try { const r = await http.post("/it/pxe/server/service", { action }); ElMessage.success(r.msg || action) } catch(e) {}
  loadServerStatus()
}
function deployProfile(row) {
  deployRow.value = row
  deployForm.deploy_mode = "proxy"
  deployForm.server_ip = ""
  deployDialog.value = true
}

async function confirmDeploy() {
  const row = deployRow.value
  if (!row) return
  deploying.value = true
  try {
    const r = await http.post("/it/pxe/profiles/" + row.id + "/deploy", {
      deploy_mode: deployForm.deploy_mode,
      server_ip: (deployForm.server_ip || "").trim(),
      hostname: "default",
      installs: []
    })
    deployLog.value = r.log || []
    if (r.ok) ElMessage.success("部署完成")
    else ElMessage.warning("部署未完全成功，查看日志")
    deployDialog.value = false
    loadServerStatus()
  } finally { deploying.value = false }
}

async function loadIsos() {
  try { isoList.value = await http.get("/it/pxe/iso/list") } catch(e) {}
}
async function loadMedia() {
  try { mediaList.value = await http.get("/it/pxe/media/list") } catch(e) {}
}
async function extractIso(row) {
  row._extracting = true
  extractLog.value = []
  try {
    const r = await http.post("/it/pxe/iso/" + encodeURIComponent(row.name) + "/extract", {
      os_type: row._osType || "ubuntu", os_version: row._osVer || "22.04"
    })
    extractLog.value = r.log || []
    if (r.ok) { ElMessage.success("提取完成"); loadServerStatus() }
    else ElMessage.warning("提取未完全成功，查看日志")
  } finally { row._extracting = false }
}
async function delIso(name) {
  try {
    await http.delete("/it/pxe/iso/" + encodeURIComponent(name))
    ElMessage.success("已删除")
    loadIsos()
  } catch(e) {}
}


const FSTYPES = ["ext4", "xfs", "btrfs", "vfat", "fat32", "swap"]
const emptyForm = () => ({
  name: "", os_type: "rhel", os_version: "9.3",
  timezone: "Asia/Shanghai", locale: "en_US.UTF-8", keyboard: "us",
  admin_user: "ops", admin_password: "", root_password: "",
  ssh_keys_text: "",
  // 磁盘：默认"自动选盘"。以前这里默认 sda，等于把盘名写死 ——
  // 在 NVMe(nvme0n1) / virtio-blk(vda) 的机器上必然装不上。
  disk_scheme: "lvm",
  disk_target_mode: "auto", disk_name: "sda", disk_serial: "", disk_model: "",
  disk_min_size_gb: 0, disk_wipe: true,
  partitions: [], data_disks: [], raid: [],
  net_mode: "dhcp", net_interface: "ens33", net_ip: "", net_netmask: "255.255.255.0", net_gateway: "", net_dns: "",
  mirror: "", extra_packages_text: "", post_script: "",
})
const form = reactive(emptyForm())

const genForm = reactive({ hostname: "server01", server_ip: "", http_root: "", kernel_path: "", initrd_path: "", squashfs_path: "", deploy_mode: "standalone" })

function osLabel(row) { return row.os_type + " " + row.os_version }
function statusType(s) { return { pending: "info", booting: "warning", installing: "warning", done: "success", failed: "danger" }[s] || "info" }
function statusLabel(s) { return { pending: "待装机", booting: "引导中", installing: "安装中", done: "完成", failed: "失败" }[s] || s }

function versionsFor(osType) {
  const seen = new Set()
  const out = []
  for (const m of (mediaList.value.media || [])) {
    if (m.os_type !== osType || seen.has(m.os_version)) continue
    seen.add(m.os_version)
    out.push({ value: m.os_version, label: m.os_version + (m.complete ? "" : "（介质不完整）") })
  }
  return out
}
const versionOptions = computed(() => {
  const out = versionsFor(form.os_type)
  // 当前值即使没有介质也要留在下拉里，否则 el-select 会把输入框显示成空的
  if (form.os_version && !out.some(o => o.value === form.os_version)) {
    out.push({ value: form.os_version, label: form.os_version + "（无介质）" })
  }
  return out
})
const mediaReady = computed(() => (mediaList.value.media || []).some(
  m => m.os_type === form.os_type && m.os_version === form.os_version && m.complete))

function defaultVersion(osType) {
  const ready = (mediaList.value.media || []).filter(m => m.os_type === osType && m.complete)
  if (ready.length) return ready[0].os_version
  return osType === "ubuntu" ? "22.04" : "9.3"   // 没有介质时的历史占位值
}

function onOsChange() {
  form.os_version = defaultVersion(form.os_type)
}

function addPartition(p) {
  form.partitions.push(p || { mount: "", size: "", fstype: "", vg: "", lv: "" })
}
// 规格里的标准布局：EFI + boot + swap + LVM 吃掉剩余空间
function applyPreset() {
  form.partitions = [
    { mount: "/boot/efi", size: "512M", fstype: "fat32", vg: "", lv: "" },
    { mount: "/boot", size: "1G", fstype: "ext4", vg: "", lv: "" },
    { mount: "swap", size: "8G", fstype: "swap", vg: "", lv: "" },
    { mount: "/", size: "rest", fstype: "ext4", vg: "vg0", lv: "root" },
  ]
}

function buildDiskConfig() {
  const t = { mode: form.disk_target_mode }
  // mode=auto 时后端会忽略 name —— 这里干脆不发，避免"以为自动其实写死"
  if (form.disk_target_mode === "name" && form.disk_name) t.name = form.disk_name
  if (form.disk_target_mode === "match") {
    if (form.disk_serial) t.serial = form.disk_serial
    else if (form.disk_model) t.model = form.disk_model
  }
  if (form.disk_min_size_gb) t.min_size_gb = form.disk_min_size_gb
  const dc = { target: t, wipe: !!form.disk_wipe, layout: form.disk_scheme }
  if (form.disk_scheme !== "custom") return dc
  const parts = form.partitions
    .filter(p => p.mount || p.size || p.vg || p.lv)
    .map(p => {
      const o = {}
      if (p.mount) o.mount = p.mount
      if (p.size) o.size = p.size
      if (p.fstype) o.fstype = p.fstype
      if (p.vg || p.lv) { o.vg = p.vg; o.lv = p.lv }   // 后端要求两者同时出现，否则 422
      return o
    })
  if (parts.length) dc.partitions = parts
  const dd = form.data_disks
    .filter(d => d.name)
    .map(d => ({ name: d.name, mount: d.mount || "", fstype: d.fstype || "xfs", wipe: !!d.wipe }))
  if (dd.length) dc.data_disks = dd
  const rd = form.raid
    .filter(r => r.name && r.devices)
    .map(r => ({
      name: r.name, level: r.level,
      devices: String(r.devices).split(",").map(s => s.trim()).filter(Boolean).map(n => "part." + String(n).padStart(2, "0")),
      mount: r.mount || "", fstype: r.fstype || "xfs",
    }))
  if (rd.length) dc.raid = rd
  return dc
}

function buildPayload() {
  return {
    name: form.name, os_type: form.os_type, os_version: form.os_version,
    timezone: form.timezone, locale: form.locale, keyboard: form.keyboard,
    admin_user: form.admin_user,
    admin_password: form.admin_password || null,
    root_password: form.root_password || null,
    ssh_keys: form.ssh_keys_text.split("\n").map(k => k.trim()).filter(Boolean),
    disk_scheme: form.disk_scheme,
    disk_config: buildDiskConfig(),
    net_mode: form.net_mode,
    net_config: form.net_mode === "static" ? {
      interface: form.net_interface, ip: form.net_ip,
      netmask: form.net_netmask, gateway: form.net_gateway,
      dns: form.net_dns.split(",").map(d => d.trim()).filter(Boolean),
    } : {},
    mirror: form.mirror,
    extra_packages: form.extra_packages_text.split(",").map(p => p.trim()).filter(Boolean),
    post_script: form.post_script,
  }
}

function fillForm(p) {
  Object.assign(form, emptyForm())
  form.name = p.name; form.os_type = p.os_type; form.os_version = p.os_version
  form.timezone = p.timezone; form.locale = p.locale; form.keyboard = p.keyboard
  form.admin_user = p.admin_user
  form.ssh_keys_text = (p.ssh_keys || []).join("\n")
  form.disk_scheme = p.disk_scheme || "lvm"
  const dc = p.disk_config || {}
  const dt = dc.target || {}
  if (dt.mode) form.disk_target_mode = dt.mode
  else if (dc.disk) form.disk_target_mode = "name"   // 老模板只存 {"disk":"sda"}，语义等价
  else form.disk_target_mode = "auto"
  form.disk_name = dt.name || dc.disk || "sda"
  form.disk_serial = dt.serial || ""
  form.disk_model = dt.model || ""
  form.disk_min_size_gb = dt.min_size_gb || 0
  form.disk_wipe = dc.wipe !== false
  form.partitions = (dc.partitions || []).map(q => ({
    mount: q.mount || "", size: q.size || "", fstype: q.fstype || "",
    vg: q.vg || "", lv: q.lv || "",
  }))
  form.data_disks = (dc.data_disks || []).map(d => ({
    name: d.name || "", mount: d.mount || "", fstype: d.fstype || "xfs", wipe: !!d.wipe,
  }))
  form.raid = (dc.raid || []).map(r => ({
    name: r.name || "", level: r.level || 1,
    // 后端用 part.01 这种标识；界面上让运维填"第几个分区"更好懂
    devices: (r.devices || []).map(x => String(x).replace(/^part\.0*/, "")).join(","),
    mount: r.mount || "", fstype: r.fstype || "xfs",
  }))
  form.net_mode = p.net_mode
  const nc = p.net_config || {}
  form.net_interface = nc.interface || "ens33"; form.net_ip = nc.ip || ""
  form.net_netmask = nc.netmask || "255.255.255.0"; form.net_gateway = nc.gateway || ""
  form.net_dns = (nc.dns || []).join(",")
  form.mirror = p.mirror
  form.extra_packages_text = (p.extra_packages || []).join(",")
  form.post_script = p.post_script
}

function openProfileDialog(row) {
  Object.assign(form, emptyForm())
  editingId.value = null
  if (row) { fillForm(row); editingId.value = row.id }
  // 新建时把版本对齐到真正已提取的介质，避免默认值（9.3）与实际目录（rhel/9）不一致 → 404
  else form.os_version = defaultVersion(form.os_type)
  profileDialog.value = true
}

async function saveProfile() {
  if (!form.name) { ElMessage.warning("请输入模板名"); return }
  // 新建时管理员密码必填 —— 后端 POST 走 _require_admin_password（422：不允许留空，
  // 也不代填默认口令）。以前这里一律发 null，于是**新建模板永远失败**，
  // 而后端只在响应体里说明原因，界面上只看到"失败了"。
  // 编辑（PUT）时留空 = 不修改，是允许的，所以只在新建时拦。
  if (!editingId.value && !form.admin_password) {
    ElMessage.warning("新建模板必须填写管理员密码（裸机 root 口令，不允许留空）")
    return
  }
  saving.value = true
  try {
    const payload = buildPayload()
    if (editingId.value) await http.put("/it/pxe/profiles/" + editingId.value, payload)
    else await http.post("/it/pxe/profiles", payload)
    ElMessage.success("保存成功")
    profileDialog.value = false
    loadProfiles()
  } finally { saving.value = false }
}

async function delProfile(id) {
  await http.delete("/it/pxe/profiles/" + id)
  ElMessage.success("已删除")
  loadProfiles()
}

function openGenDialog(row) {
  genForm.hostname = "server01"
  genForm.server_ip = ""
  genForm.http_root = ""
  if (row.os_type === "ubuntu") {
    genForm.kernel_path = "ubuntu/22.04/vmlinuz"
    genForm.initrd_path = "ubuntu/22.04/initrd"
    genForm.squashfs_path = "ubuntu/22.04/installer.squashfs"
  } else {
    genForm.kernel_path = "rhel/9/vmlinuz"
    genForm.initrd_path = "rhel/9/initrd.img"
    genForm.squashfs_path = ""
  }
  genDialog.value = true
  genFiles.value = {}
  sessionStorage.setItem("pxe_profile_id", row.id)
  doGenerate()
}

async function doGenerate() {
  generating.value = true
  try {
    const pid = sessionStorage.getItem("pxe_profile_id")
    const body = {
      hostname: genForm.hostname,
      server_ip: genForm.server_ip,
      http_root: genForm.http_root,
      kernel_path: genForm.kernel_path,
      initrd_path: genForm.initrd_path,
      squashfs_path: genForm.squashfs_path,
      deploy_mode: genForm.deploy_mode,
      installs: installs.value.map(i => ({ mac: i.mac, hostname: i.hostname })),
    }
    const res = await http.post("/it/pxe/profiles/" + pid + "/generate", body)
    genFiles.value = res.files
    const keys = Object.keys(res.files)
    if (keys.length) activeFile.value = keys[0]
    ElMessage.success("生成完成: " + keys.length + " 个文件")
  } finally { generating.value = false }
}

async function doDownload() {
  const pid = sessionStorage.getItem("pxe_profile_id")
  await downloadZip("/it/pxe/profiles/" + pid + "/download", {
    hostname: genForm.hostname,
    server_ip: genForm.server_ip,
    http_root: genForm.http_root,
    kernel_path: genForm.kernel_path,
    initrd_path: genForm.initrd_path,
    squashfs_path: genForm.squashfs_path,
    deploy_mode: genForm.deploy_mode,
    installs: installs.value.map(i => ({ mac: i.mac, hostname: i.hostname })),
  })
  ElMessage.success("下载已开始")
}

async function delInstall(id) {
  await http.delete("/it/pxe/installs/" + id)
  ElMessage.success("已删除")
  loadInstalls()
}

async function loadProfiles() { profiles.value = await http.get("/it/pxe/profiles") }
async function loadInstalls() { installs.value = await http.get("/it/pxe/installs") }

// 自动轮询装机状态（有活跃装机时每10s刷新）
let installTimer = null
function startInstallPolling() {
  clearTimeout(installTimer)
  installTimer = setTimeout(async () => {
    await loadInstalls()
    const hasActive = installs.value.some(i =>
      ["pending", "booting", "installing"].includes(i.status)
    )
    if (hasActive) startInstallPolling()
  }, 10000)
}

let serverPollTimer = null
onBeforeUnmount(() => { clearTimeout(installTimer); clearTimeout(serverPollTimer) })
onMounted(() => { loadProfiles(); loadInstalls(); loadServerStatus(); loadIsos(); loadMedia(); startInstallPolling(); serverPollTimer = setTimeout(loadServerStatus, 5000) })
</script>
