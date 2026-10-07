<template>
  <div class="page">
    <PageHeader title="PXE 装机" desc="维护 ISO 引导介质与装机模板，生成 PXE 部署文件并跟踪裸机装机进度" />

    <!-- PXE 服务器本机状态 -->
    <CardSection title="PXE 服务器（本机）" icon="Cpu">
      <template #extra>
        <div class="head-actions">
          <el-tag v-if="serverStatus.dnsmasq" :type="serverStatus.dnsmasq.active ? 'success' : 'danger'" size="small">
            dnsmasq {{ serverStatus.dnsmasq.active ? '运行中' : '未运行' }}
          </el-tag>
          <el-button size="small" @click="controlService('start')" :disabled="serverStatus.supported === false">启动</el-button>
          <el-button size="small" @click="controlService('stop')" :disabled="serverStatus.supported === false">停止</el-button>
          <el-button size="small" @click="controlService('restart')" :disabled="serverStatus.supported === false">重启 dnsmasq</el-button>
          <el-button size="small" @click="loadServerStatus"><el-icon><Refresh /></el-icon> 刷新</el-button>
        </div>
      </template>
      <el-descriptions v-if="serverStatus.supported" :column="3" size="small" border>
        <el-descriptions-item label="sudo 免密">{{ serverStatus.sudo_ok ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="开机自启">{{ serverStatus.dnsmasq && serverStatus.dnsmasq.enabled ? '是' : '否' }}</el-descriptions-item>
        <el-descriptions-item label="TFTP">{{ serverStatus.tftp_root }}</el-descriptions-item>
      </el-descriptions>
      <el-row :gutter="16" class="mt-2" v-if="serverStatus.supported">
        <el-col :span="12">
          <div class="group-label">TFTP 文件</div>
          <el-tag v-for="f in serverStatus.tftp_files" :key="f" size="small" class="file-tag">{{ f }}</el-tag>
          <span v-if="!serverStatus.tftp_files || !serverStatus.tftp_files.length" class="list-empty">暂无文件</span>
        </el-col>
        <el-col :span="12">
          <div class="group-label">HTTP 文件</div>
          <el-tag v-for="f in serverStatus.web_files" :key="f" size="small" class="file-tag">{{ f }}</el-tag>
          <span v-if="!serverStatus.web_files || !serverStatus.web_files.length" class="list-empty">暂无文件</span>
        </el-col>
      </el-row>
      <div v-if="deployLog.length" class="mt-2">
        <div class="group-label">部署日志</div>
        <div class="terminal-output log-pre log-200">{{ deployLog.join("\n") }}</div>
      </div>
      <el-alert v-if="serverStatus.supported === false" type="warning" :closable="false" class="mt-2">本机部署需 Linux 环境（当前：{{ serverStatus.platform }}），可用「下载 ZIP」手动部署</el-alert>
    </CardSection>
    <!-- ISO 管理 -->
    <CardSection title="ISO 镜像管理" icon="Files">
      <template #extra>
        <el-button size="small" @click="loadIsos"><el-icon><Refresh /></el-icon> 刷新</el-button>
      </template>
      <el-alert v-if="isoList.supported === false" type="warning" :closable="false" class="mb-2">需 Linux 环境</el-alert>
      <el-table v-else :data="isoList.isos || []" size="small" empty-text="尚无 ISO 文件，请将 ISO 上传到服务器 /srv/opstk/iso/ 目录">
        <el-table-column prop="name" label="ISO 文件" min-width="280" />
        <el-table-column prop="size_mb" label="大小 (MB)" width="110" />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-select v-model="row._osType" size="small" class="os-type-select">
              <el-option label="Ubuntu" value="ubuntu" />
              <el-option label="RHEL" value="rhel" />
            </el-select>
            <el-input v-model="row._osVer" size="small" class="os-ver-input" placeholder="22.04" />
            <el-button type="primary" link size="small" @click="askExtract(row)" :loading="row._extracting">提取</el-button>
            <el-popconfirm title="确定删除?" @confirm="delIso(row.name)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
      <div v-if="extractLog.length" class="mt-2">
        <div class="group-label">提取日志</div>
        <div class="terminal-output log-pre log-200">{{ extractLog.join('\n') }}</div>
      </div>
    </CardSection>
    <!-- 模板列表 -->
    <CardSection title="PXE 装机模板" icon="Cpu">
      <template #extra>
        <el-button type="primary" @click="openProfileDialog()"><el-icon><Plus /></el-icon> 新建装机模板</el-button>
      </template>
      <el-table :data="profiles" stripe size="small" empty-text="暂无装机模板">
        <el-table-column prop="name" label="模板名称" min-width="130" />
        <el-table-column label="系统" width="120">
          <template #default="{ row }">
            <el-tag size="small">{{ osLabel(row) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="disk_scheme" label="磁盘" width="70" />
        <el-table-column prop="net_mode" label="网络" width="70" />
        <el-table-column prop="timezone" label="时区" width="120" />
        <el-table-column prop="mirror" label="镜像源" min-width="160" show-overflow-tooltip />
        <el-table-column label="操作" width="280" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="openGenDialog(row)">生成配置</el-button>
            <el-button type="primary" link size="small" @click="deployProfile(row)">部署</el-button>
            <el-button link type="primary" size="small" @click="openProfileDialog(row)">编辑</el-button>
            <el-popconfirm title="确定删除?" @confirm="delProfile(row.id)">
              <template #reference><el-button type="danger" link size="small">删除</el-button></template>
            </el-popconfirm>
          </template>
        </el-table-column>
      </el-table>
    </CardSection>

    <!-- 装机记录 -->
    <CardSection title="装机记录" icon="Monitor">
      <el-table :data="installs" stripe size="small" empty-text="暂无装机记录">
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
    </CardSection>

    <!-- 模板编辑弹窗 -->
    <el-dialog v-model="profileDialog" :title="editingId ? '编辑装机模板' : '新建装机模板'" width="920px" :close-on-click-modal="false">
      <el-form :model="form" label-width="90px" size="default">
        <el-divider content-position="left">基本信息</el-divider>
        <el-row :gutter="12">
          <el-col :span="8"><el-form-item label="模板名称"><el-input v-model="form.name" /></el-form-item></el-col>
          <el-col :span="8">
            <el-form-item label="系统">
              <el-select v-model="form.os_type" @change="onOsChange">
                <el-option label="Ubuntu 22.04+" value="ubuntu" /><el-option label="RHEL/Rocky/Alma 8+" value="rhel" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item>
              <template #label>
                版本 <el-tooltip v-if="form.os_version && !mediaReady" placement="top" effect="light">
                  <template #content>
                    <div class="tip-body">没有 {{ form.os_type }}/{{ form.os_version }}/ 的引导介质，装机时 iPXE 会报 "Could not boot image"</div>
                  </template>
                  <el-icon class="tip-icon"><WarningFilled /></el-icon>
                </el-tooltip>
              </template>
              <el-select v-model="form.os_version" filterable allow-create default-first-option
                         class="w-full" placeholder="选一个已提取介质的版本">
                <el-option v-for="v in versionOptions" :key="v.value" :label="v.label" :value="v.value" />
              </el-select>
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
            <!-- 提示放在**输入框右侧**（图标 tooltip），而不是塞进标签：
                 标签宽只有 90px，「管理员密码」+ 图标会把最后一个字挤到第二行（实测截图所见）。
                 用 .inline-unit（输入框占满 + 图标贴右，nowrap）保证整行不折。 -->
            <el-form-item label="管理员密码">
              <div class="inline-unit">
                <el-input v-model="form.admin_password" type="password" show-password
                          :placeholder="editingId ? '留空不修改' : '新建必填'" />
                <el-tooltip v-if="!editingId && !form.admin_password" placement="top" effect="light">
                  <template #content>
                    <div class="tip-body">新建时必填：这是裸机 root 密码，后端不允许留空，也不会代填任何默认值。</div>
                  </template>
                  <el-icon class="tip-icon"><WarningFilled /></el-icon>
                </el-tooltip>
              </div>
            </el-form-item>
          </el-col>
          <el-col :span="8"><el-form-item label="root密码" v-if="form.os_type === 'rhel'"><el-input v-model="form.root_password" type="password" show-password /></el-form-item></el-col>
        </el-row>
        <el-form-item label="SSH公钥">
          <el-input v-model="form.ssh_keys_text" type="textarea" :rows="2" placeholder="每行一个公钥" />
        </el-form-item>

        <el-divider content-position="left">磁盘</el-divider>
        <div class="group-label">目标盘与分区方案</div>
        <el-row :gutter="12">
          <el-col :span="8">
            <el-form-item label="分区方案">
              <el-select v-model="form.disk_scheme">
                <el-option label="LVM (推荐)" value="lvm" />
                <el-option label="直通分区" value="direct" />
                <!-- ZFS 选项已移除：后端 disk_scheme 白名单只收 lvm/direct/custom
                     （backend/app/core/schemas.py _check_scheme），保留此项则保存必然 422；
                     后端实现 ZFS 后再恢复。 -->
                <el-option label="自定义分区表" value="custom" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <el-form-item label="目标磁盘">
              <el-select v-model="form.disk_target_mode">
                <el-option label="自动（最大盘）" value="auto" />
                <el-option label="按盘名指定" value="name" />
                <el-option label="按序列号/型号" value="match" />
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
            <el-form-item label="型号（兜底）">
              <el-input v-model="form.disk_model" placeholder="如 INTEL SSDSC2KB480G8" />
            </el-form-item>
          </el-col>
          <el-col :span="8">
            <!-- 原标签「忽略小于(GB)」在 90px 的 label 宽度下自己折成两行（(GB) 掉到第二行）。
                 改成短标签 + 行内单位：标签不折行，单位也不多占一行。 -->
            <el-form-item label="最小容量">
              <div class="inline-unit">
                <el-input-number v-model="form.disk_min_size_gb" :min="0" :max="100000"
                                 controls-position="right" class="w-full" />
                <span class="text-muted">GB</span>
              </div>
            </el-form-item>
          </el-col>
          <el-col :span="16">
            <el-form-item label="清空目标盘">
              <el-switch v-model="form.disk_wipe" />
              <span class="switch-note">
                只清空"目标磁盘"，其它盘一律不碰
              </span>
            </el-form-item>
          </el-col>
        </el-row>
        <div v-if="form.disk_target_mode === 'auto'" class="disk-auto-line">
          <span>自动选盘：换硬件不用改模板；<b class="warn-text">Ubuntu 不支持自动选盘</b>，须按盘名指定</span> <el-tooltip placement="top" effect="light">
            <template #content>
              <div class="tip-body">
                RHEL 系（anaconda 没有现成的自动选盘原语）在 <code>%pre</code> 里按
                "非可移动、非光驱、容量达标、按盘名排序取第一块"选出目标盘，
                再 <code>%include</code> 生成出来的分区片段。盘名不再写死，NVMe(<code>nvme0n1</code>) /
                virtio-blk(<code>vda</code>) 都能装。<br />
                <b class="warn-text">Ubuntu 的 layout=custom 不支持自动选盘</b>：
                subiquity 没有"自动挑最大盘"的写法，必须显式指定目标盘（否则生成时直接 422 拒绝）。
              </div>
            </template>
            <el-button link type="primary" size="small">实现细节</el-button>
          </el-tooltip>
        </div>

        <div v-if="form.os_type === 'ubuntu' && form.disk_scheme === 'custom'" class="form-hint mb-3">
          <b>Ubuntu 自定义分区：请用「按盘名」指定目标盘</b> <el-tooltip placement="top" effect="light">
            <template #content>
              <div class="alert-detail">
                subiquity 认目标盘的方式与 RHEL 不同（真机实测，见 RUNBOOK-STATE §5.47）：
                <ul class="alert-list">
                  <li>
                    <code>serial</code>：subiquity 取的是 sysfs 的
                    <code>/sys/block/sdX/device/serial</code>，而虚拟化（QEMU/virtio-scsi）下该属性为空
                    → 报 <code>matched no disk</code>，装不上；
                  </li>
                  <li><code>wwn</code> / <code>model</code>：盘可能没有 WWN，或同型号多盘时产生歧义；</li>
                  <li>
                    它<b>不认识</b>的键（如 <code>id_path</code>）：<b>不报错</b>，而是退回"匹配第一块盘" ——
                    若数据盘排在前面就<b>直接抹掉数据盘</b>（产品已拒绝此键）。
                  </li>
                </ul>
                所以 Ubuntu 侧请选 <b>按盘名</b> 填系统盘设备名（如 <code>sdb</code>）：
                名字填错会明确报错、不会错装，但<b>务必确认它与「数据盘」不是同一块盘</b>
                （系统盘会被清空分区）。<br />
                <b>未验证</b>：真机上若磁盘提供真实序列号 / WWN，
                <code>serial</code> / <code>wwn</code> 也许可用 —— 本环境无真机，未做验证，
                因此不作为结论。
              </div>
            </template>
            <el-button link type="primary" size="small">subiquity 实测差异</el-button>
          </el-tooltip>
        </div>

        <template v-if="form.disk_scheme === 'custom'">
          <el-divider content-position="left">自定义分区表</el-divider>
          <div class="help-text">
            <code>rest</code> 只能放在最后一行；挂载点留空 = 只建分区不挂载 <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="tip-body">大小为 <code>512M</code>/<code>20G</code>/<code>rest</code>（<code>rest</code> 只能放在最后一行，表示用掉剩余空间）；挂载点留空 = 只建分区不挂载；填了 VG 与 LV 才是 LVM 逻辑卷。</div>
              </template>
              <el-button link type="primary" size="small">大小写法</el-button>
            </el-tooltip>
          </div>
          <el-table :data="form.partitions" size="small" class="table-gap">
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
                <el-button link type="danger" size="small" @click="form.partitions.splice(scope.$index, 1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="mb-3">
            <el-button size="small" @click="addPartition()">+ 加一个分区</el-button>
            <el-button size="small" @click="applyPreset()">套用：EFI + boot + swap + LVM(/)</el-button>
          </div>

          <el-divider content-position="left">其它数据盘（默认<b>不格式化</b>）</el-divider>
          <div class="help-text">
            这里只做"挂载"。要格式化别的盘必须显式打开下面的开关 —— 生产上默认不动数据盘。<br />
            <span class="warn-text">识别方式必须填「容量」/「序列号」/「WWID」之一，盘名只作备注</span> <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="tip-body">盘名（sda/sdb）由内核探测顺序决定，<b>同一台机器两次启动都可能互换</b>，拿它当"别碰这块盘"的判据会把系统盘排除掉、让安装落到数据盘上并抹掉它（真机实测过）。容量写法如 <code>30G</code>（G/M/T 按二进制，GB/MB/TB 按十进制）。</div>
              </template>
              <el-button link type="primary" size="small">为什么?</el-button>
            </el-tooltip><br />
            挂载点必须二选一：① 打开「格式化」；② 填「挂已有文件系统」的 UUID / 卷标 <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="tip-body">要给数据盘填「挂载点」必须二选一：① 打开「格式化」（建新分区再挂）；② 在「挂已有文件系统」里填该分区文件系统的 <b>UUID</b> 或<b>卷标</b>（保留数据、不格式化）。</div>
              </template>
              <el-button link type="primary" size="small">细节</el-button>
            </el-tooltip><br />
            <span class="warn-text">「挂已有文件系统」只在 RHEL 系实现，Ubuntu 会被后端拒绝</span> <el-tooltip placement="top" effect="light">
              <template #content>
                <div class="tip-body">「挂已有文件系统」只在 <b>RHEL 系</b>（kickstart 的
                <code>part &lt;挂载点&gt; --onpart=UUID=… --noformat</code>）实现；
                Ubuntu 侧的 <code>preserve: true</code> 官方文档确实存在，但本项目<b>尚未验证</b>，
                后端会拒绝（fail-closed，不赌）。填了它会自动关掉「格式化」——
                既有文件系统不能被格式化。另外：用 clearpart 时 <code>--onpart</code> 只能指向
                <b>主分区</b>（官方原文：不能用在逻辑分区上），产物里会写明这条边界。
                「文件系统」列可留空：<code>--fstype</code> 在该组合下是否必需官方文档没说，
                填了就原样写进产物。</div>
              </template>
              <el-button link type="primary" size="small">细节</el-button>
            </el-tooltip>
            <!-- 后端契约：不格式化时不会建分区，挂载点会被丢弃 ⇒ 直接 422（fail-closed），
                 别让运维填完挂载点才撞一个 422。 -->
          </div>
          <el-table :data="form.data_disks" size="small" class="table-gap">
            <el-table-column label="容量（识别用）" width="130">
              <template #default="{ row }">
                <el-input v-model="row.size" placeholder="30G" />
              </template>
            </el-table-column>
            <el-table-column label="序列号（识别用）" width="150">
              <template #default="{ row }">
                <el-input v-model="row.serial" placeholder="可空" />
              </template>
            </el-table-column>
            <el-table-column label="WWID（识别用）" width="150">
              <template #default="{ row }">
                <el-input v-model="row.wwid" placeholder="可空" />
              </template>
            </el-table-column>
            <el-table-column label="盘名（仅备注）" width="130">
              <template #default="{ row }"><el-input v-model="row.name" placeholder="可空" /></template>
            </el-table-column>
            <el-table-column label="挂载点" width="150">
              <template #default="{ row }"><el-input v-model="row.mount" placeholder="/data" /></template>
            </el-table-column>
            <el-table-column label="挂已有文件系统" width="240">
              <template #default="{ row }">
                <el-select
                  v-model="row.existing_kind"
                  clearable
                  placeholder="不用"
                  class="existing-kind"
                  @change="onExistingKindChange(row)"
                >
                  <el-option label="UUID" value="uuid" />
                  <el-option label="卷标" value="label" />
                </el-select>
                <el-input
                  v-if="row.existing_kind"
                  v-model="row.existing_value"
                  placeholder="文件系统 UUID / 卷标"
                  class="existing-value"
                />
              </template>
            </el-table-column>
            <el-table-column label="文件系统" width="120">
              <template #default="{ row }">
                <el-select v-model="row.fstype" clearable>
                  <el-option v-for="f in FSTYPES" :key="f" :label="f" :value="f" />
                </el-select>
              </template>
            </el-table-column>
            <el-table-column label="格式化" width="90">
              <template #default="{ row }">
                <el-switch v-model="row.wipe" :disabled="!!row.existing_kind" />
              </template>
            </el-table-column>
            <el-table-column label="操作" width="70">
              <template #default="scope">
                <el-button link type="danger" size="small" @click="form.data_disks.splice(scope.$index, 1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="mb-3">
            <el-button
              size="small"
              @click="form.data_disks.push({ size: '', serial: '', wwid: '', name: '', mount: '', fstype: 'xfs', wipe: false, existing_kind: '', existing_value: '' })"
            >
              + 加一块数据盘
            </el-button>
          </div>

          <el-divider content-position="left">RAID（可选）</el-divider>
          <el-table :data="form.raid" size="small" class="table-gap">
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
                <el-button link type="danger" size="small" @click="form.raid.splice(scope.$index, 1)">删除</el-button>
              </template>
            </el-table-column>
          </el-table>
          <div class="mb-3">
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
      <el-form label-width="90px" size="small" class="mb-3">
        <el-row :gutter="8">
          <el-col :span="8">
            <el-form-item label="部署模式">
              <el-select v-model="genForm.deploy_mode" class="w-full">
                <el-option label="独立DHCP (专用装机网络)" value="standalone" />
                <el-option label="ProxyDHCP (与现有DHCP并存)" value="proxy" />
                <el-option label="中继模式 (仅TFTP, 依赖交换机)" value="relay" />
              </el-select>
            </el-form-item>
          </el-col>
          <el-col :span="6"><el-form-item label="主机名"><el-input v-model="genForm.hostname" /></el-form-item></el-col>
          <!-- 占位文案要能在 span=6 的窄字段里显示完：原先「PXE服务本机IP，如 10.128.118.113」
               被截断成「PXE服务本机 IP, …」（截图实测）。具体 IP 示例见使用帮助。 -->
          <el-col :span="6"><el-form-item label="PXE服务IP"><el-input v-model="genForm.server_ip" placeholder="留空用本机 IP" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="内核路径"><el-input v-model="genForm.kernel_path" placeholder="留空由后端按模板版本推导" /></el-form-item></el-col>
          <el-col :span="6"><el-form-item label="initrd"><el-input v-model="genForm.initrd_path" placeholder="留空由后端按模板版本推导" /></el-form-item></el-col>
          <!-- 同上：原「留空由后端生成，格式 http://<IP>:8000/pxe/serve」在 span=8 里被截断成
               「留空由后端生成, 格式 http://<I…」；格式说明已在下方注释与帮助文档里写明。 -->
          <el-col :span="8"><el-form-item label="HTTP根地址"><el-input v-model="genForm.http_root" placeholder="留空由后端生成" /></el-form-item></el-col>
        </el-row>
        <el-alert v-if="genMediaNote" type="warning" :closable="false" show-icon class="mb-2">
          {{ genMediaNote }}
        </el-alert>
        <el-button type="primary" size="small" @click="doGenerate" :loading="generating"><el-icon><Check /></el-icon> {{ genStale ? '重新生成' : '生成文件' }}</el-button>
          <el-button size="small" @click="doDownload" :disabled="!Object.keys(genFiles).length || genStale"><el-icon><Download /></el-icon> 下载 ZIP</el-button>
          <!-- U5-F7：预览与下载必须是同一份内容 -->
          <el-tag v-if="genStale" size="small" type="warning" class="tag-gap">预览已失效（参数或装机记录已变化，请重新生成）</el-tag>
          <el-tag v-else-if="Object.keys(genFiles).length" size="small" type="success" class="tag-gap">下载内容 = 预览内容</el-tag>
      </el-form>
      <el-tabs v-model="activeFile" v-if="Object.keys(genFiles).length">
        <el-tab-pane v-for="(_, name) in genFiles" :key="name" :label="name" :name="name">
          <div class="terminal-output log-pre log-420">{{ genFiles[name] }}</div>
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
          <el-select v-model="deployForm.deploy_mode" class="w-full">
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
        <template #title>
          将在本网段启动完整 DHCP，与其他 DHCP 冲突会断网 <el-tooltip placement="top" effect="light">
            <template #content>
              <div class="tip-body">部署前请确认本网段无其他 DHCP 服务器，否则会与现有 DHCP 冲突导致断网。</div>
            </template>
            <el-button link type="primary" size="small">风险细节</el-button>
          </el-tooltip>
        </template>
      </el-alert>
      <div v-else class="mode-note">
        {{ deployForm.deploy_mode === 'proxy' ? 'ProxyDHCP 模式与现有 DHCP 并存，不分配地址，影响面小。' : '中继模式仅提供引导，依赖外部 DHCP 与交换机 IP helpers。' }}
      </div>
      <!-- ★ 外部审查 U5-F8：部署与「生成配置」弹窗用的是**两套参数**，界面以前对此只字不提。
           事实（都读过后端）：部署按**模板**生成（内核路径留空时由后端按模板 os_type/os_version
           推导）、http_root 由后端强制为 http://<server_ip>:8000/pxe/serve（不吃前端传值）、
           installs 传空时自动回落到该模板在库里的装机记录。这里把将写入的东西说清楚，
           部署后再显示实际写盘的文件清单。 -->
      <el-descriptions :column="1" size="small" border class="mt-10">
        <el-descriptions-item label="将按模板生成">
          {{ deployRow ? (deployRow.os_type + '/' + deployRow.os_version) : '-' }}
          —— 内核/initrd 路径按模板版本推导 <el-tooltip placement="top" effect="light">
            <template #content>
              <div class="tip-body">要覆盖路径，请用「生成配置」弹窗里的路径再部署。</div>
            </template>
            <el-button link type="primary" size="small">如何覆盖</el-button>
          </el-tooltip>
        </el-descriptions-item>
        <el-descriptions-item label="将带上装机记录">
          {{ deployInstalls.length }} 条（模板里已登记的 MAC → 各自菜单/应答文件）
        </el-descriptions-item>
        <el-descriptions-item label="HTTP根地址">
          由后端强制为本机 http://&lt;PXE服务IP&gt;:8000/pxe/serve
        </el-descriptions-item>
      </el-descriptions>
      <div v-if="deployWritten.length" class="written-note">
        上次实际写入 {{ deployWritten.length }} 个文件：{{ deployWritten.join('、') }}
      </div>
      <template #footer>
        <el-button @click="deployDialog = false">取消</el-button>
        <el-button type="primary" :loading="deploying" @click="confirmDeploy">确认部署</el-button>
      </template>
    </el-dialog>

    <!-- ISO 提取确认（U5-F10）：把目标目录写出来，避免 RHEL 镜像被提到 ubuntu/22.04 -->
    <el-dialog v-model="extractConfirm.visible" title="确认提取引导介质" width="520px">
      <div class="dialog-body">
        将把 <b>{{ extractConfirm.row ? extractConfirm.row.name : '' }}</b> 里的引导文件提取到：<br />
        <code class="path-code">/srv/opstk/pxe-web/{{ (extractConfirm.row && extractConfirm.row._osType || '').trim()
          }}/{{ (extractConfirm.row && extractConfirm.row._osVer || '').trim() }}/</code>
        <div class="warn-note">
          目录名必须与模板里的「系统 + 版本」完全一致，否则内核 URL 是 404 <el-tooltip placement="top" effect="light">
            <template #content>
              <div class="tip-body">生成出来的内核 URL 对不上时，机器端只报 "Could not boot image"。提取是长任务，请确认系统和版本没选错。</div>
            </template>
            <el-button link type="primary" size="small">细节</el-button>
          </el-tooltip>
        </div>
      </div>
      <template #footer>
        <el-button @click="extractConfirm.visible = false">取消</el-button>
        <el-button type="primary" :loading="extractConfirm.row && extractConfirm.row._extracting"
                   @click="extractConfirm.visible = false; extractIso(extractConfirm.row)">开始提取</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, onBeforeUnmount, computed } from "vue"
import http, { downloadZip } from "../api"
import { ElMessage } from "element-plus"
import PageHeader from "../components/PageHeader.vue"
import CardSection from "../components/CardSection.vue"

const profiles = ref([])
const installs = ref([])
const profileDialog = ref(false)
const editingId = ref(null)
const saving = ref(false)
const genDialog = ref(false)
const generating = ref(false)
const genFiles = ref({})
// 当前预览对应的参数指纹（U5-F7：参数/装机记录变了就作废，见 genStale）+ 请求序号
const genKey = ref("")
const genMediaNote = ref("")
let genSeq = 0
const activeFile = ref("")
const serverStatus = ref({ supported: false })
const deployLog = ref([])
const deploying = ref(false)

// 部署确认弹窗：deploy_mode 默认 proxy（与现有 DHCP 并存，更安全）；server_ip 留空由后端自动探测
const deployDialog = ref(false)
const deployRow = ref(null)
const deployForm = reactive({ deploy_mode: "proxy", server_ip: "" })
const deployWritten = ref([])   // U5-F8：部署后展示实际写盘的文件清单
const isoList = ref({ supported: false, isos: [] })
// 已提取的引导介质（os_type/os_version）。"版本"必须从这里选：生成出来的 kernel URL
// 是按 os_type+os_version 拼的，版本对不上就是 404（iPXE 只会报 Could not boot image）。
const mediaList = ref({ supported: true, media: [] })
const extractLog = ref([])

async function loadServerStatus() {
  // ★ 外部审查 U5-F5：轮询链只能有一条。本函数末尾会重新排一个定时器，而
  //   controlService/extractIso 也会调用它 —— 不先清掉待执行的那个，每点一次服务
  //   控制就多出一条 5s 轮询链，onBeforeUnmount 也只能停掉其中一条（长期 SPA 会话里
  //   请求速率线性增长，组件销毁后还有链在打接口）。
  clearTimeout(serverPollTimer)
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
  deployWritten.value = []
  deployDialog.value = true
}

// U5-F8：部署后会带上"该模板在库里的装机记录"（installs 传空时后端回落到 DB）。
// 界面必须把这件事说清楚，否则运维会以为部署的就是刚才预览的那一份。
const deployInstalls = computed(() => {
  const row = deployRow.value
  if (!row) return []
  return (installs.value || []).filter(i => i.profile_id === row.id)
})

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
    deployWritten.value = r.files_written || []
    if (r.ok) ElMessage.success("部署完成，已写入 " + deployWritten.value.length + " 个文件")
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
// ★ 外部审查 U5-F10：改前 `row._osType || "ubuntu"`、`row._osVer || "22.04"` —— 忘了选系统
//   类型就会把 RHEL 镜像提取到 ubuntu/22.04/，之后按模板版本永远找不到介质（提取是长任务，
//   错一次要重跑）。现在：值必须显式给，且先在确认框里把**目标目录**写出来。
const extractConfirm = ref({ visible: false, row: null })

function askExtract(row) {
  const osType = (row._osType || "").trim()
  const osVer = (row._osVer || "").trim()
  if (!osType) { ElMessage.warning("请先选择这个 ISO 的系统类型（Ubuntu / RHEL）"); return }
  if (!osVer) { ElMessage.warning("请先填写版本号（必须与模板里的版本完全一致，否则装机 404）"); return }
  extractConfirm.value = { visible: true, row }
}

async function extractIso(row) {
  const osType = (row._osType || "").trim()
  const osVer = (row._osVer || "").trim()
  if (!osType || !osVer) {
    // 纵深：即使别处再调用本函数，也绝不回退到与镜像无关的默认值
    ElMessage.error("系统类型与版本都必须显式填写，不能留空（不再回退 ubuntu/22.04）")
    return
  }
  row._extracting = true
  extractLog.value = []
  try {
    const r = await http.post("/it/pxe/iso/" + encodeURIComponent(row.name) + "/extract", {
      os_type: osType, os_version: osVer
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

// ★ 功能 G：选了「挂已有文件系统」就把「格式化」关掉并置灰 —— 既有文件系统不能格式化
//   （后端对 existing_* + wipe=true 直接 422，因为 wipe 会清掉分区表、数据就没了）。
//   这里**显式**关（并让控件变灰），而不是在提交时偷偷改值 —— 运维看得见自己填的东西变了。
//   「文件系统」列**不动**：`--fstype` 在该组合下是否必需，pykickstart 文档没说，
//   所以不替运维猜、也不拦 —— 填了就透传进产物。
function onExistingKindChange(row) {
  if (row.existing_kind) row.wipe = false
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
  // §5.42：数据盘必须带稳定识别条件（size/serial/wwid），否则**不能提交**。
  // 这里**绝不能 filter 静默丢行** —— 之前那样写会把"只填了盘名"的旧行悄悄丢掉，
  // 后端于是看到"没有数据盘"、%pre 排除集为空、自动选盘选中数据盘并抹掉它
  // （MiMo R2 的 H2 指出的正是一条带回退的静默数据丢失路径）。
  const ddRaw = form.data_disks.filter(d =>
    (d.size || d.serial || d.wwid || d.name || d.mount))
  const ddBad = ddRaw.filter(d => !(d.size || d.serial || d.wwid))
  if (ddBad.length) {
    throw new Error(
      '有 ' + ddBad.length + ' 行数据盘没填「容量 / 序列号 / WWID」：' +
      '盘名（sda/sdb）由内核探测顺序决定，同一台机器两次启动都可能互换，' +
      '用它识别"别碰这块盘"会把系统盘排除掉、让安装落到数据盘上并抹掉它。' +
      '请至少填一个识别条件（推荐容量，如 30G）。')
  }
  const dd = ddRaw.map(d => {
    const o = {
      size: d.size || "", serial: d.serial || "", wwid: d.wwid || "",
      name: d.name || "", mount: d.mount || "", fstype: d.fstype || "xfs", wipe: !!d.wipe,
    }
    // ★ 功能 G：挂**已有**文件系统（UUID / 卷标，不格式化、保留数据）。
    //   这里**不静默丢值**：填了种类没填值、或填在 Ubuntu 上，都在本地直接拦住并说清原因
    //   （后端也会拒，但让运维等到点保存才知道没必要）。
    if (d.existing_kind) {
      const v = String(d.existing_value || "").trim()
      if (!v) {
        throw new Error("有数据盘选了「挂已有文件系统」但没填 UUID/卷标：请填上，或把这一列清空。")
      }
      if (form.os_type !== "rhel") {
        throw new Error(
          "「挂已有文件系统」目前只在 RHEL 系模板实现（kickstart 的 " +
          "part <挂载点> --onpart=UUID=… --noformat）；Ubuntu 侧的等价写法尚未验证，" +
          "后端会按 fail-closed 拒绝。请改用 RHEL 系模板，或先手工挂载。")
      }
      if (d.existing_kind === "uuid") o.existing_uuid = v
      else o.existing_label = v
      // 既有文件系统**不**重新格式化：wipe 必须为 false（后端对 wipe=true 会 422）。
      // fstype 不动：`--fstype` 在该组合下是否必需文档没说，填了就透传（不替运维猜）。
      o.wipe = false
    }
    return o
  })
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
    size: d.size || "", serial: d.serial || "", wwid: d.wwid || "",
    name: d.name || "", mount: d.mount || "", fstype: d.fstype || "xfs", wipe: !!d.wipe,
    // 功能 G：把 existing_uuid / existing_label 折回界面上的"种类 + 值"两段
    existing_kind: d.existing_uuid ? "uuid" : (d.existing_label ? "label" : ""),
    existing_value: d.existing_uuid || d.existing_label || "",
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
  if (!form.name) { ElMessage.warning("请输入模板名称"); return }
  // 新建时管理员密码必填 —— 后端 POST 走 _require_admin_password（422：不允许留空，
  // 也不代填默认口令）。以前这里一律发 null，于是**新建模板永远失败**，
  // 而后端只在响应体里说明原因，界面上只看到"失败了"。
  // 编辑（PUT）时留空 = 不修改，是允许的，所以只在新建时拦。
  if (!editingId.value && !form.admin_password) {
    ElMessage.warning("新建模板必须填写管理员密码（裸机 root 密码，不允许留空）")
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
  } catch (e) {
    // ★ 外部审查 U5-F2：buildPayload() 会为"数据盘没填稳定标识"等**安全原因**抛 Error
    //   （不是 HTTP 错误，axios 拦截器不介入）。以前只有 try/finally，于是保存按钮
    //   "点了没反应"、告警只进 console —— 运维最省事的"绕过"就是删掉那几行数据盘，
    //   结果 %pre 排除集为空，正好回到会抹盘的路径。这里必须把原因显示出来。
    ElMessage.error((e && e.message) || "保存失败")
    return
  } finally { saving.value = false }
}

async function delProfile(id) {
  await http.delete("/it/pxe/profiles/" + id)
  ElMessage.success("已删除")
  loadProfiles()
}

// 生成弹窗：默认内核路径必须来自**真实介质**，不能写死 22.04/rhel9
// ★ 外部审查 U5-F6：改前固定填 `ubuntu/22.04/vmlinuz` / `rhel/9/vmlinuz`，与模板的
//   os_version 及已提取介质目录无关 —— 模板是 ubuntu/24.04 时生成的 iPXE 指向不存在的
//   vmlinuz，机器端只报 "Could not boot image"，与本页自己写的契约（版本对不上就是 404）
//   直接矛盾。现在按 os_type + os_version 从 /it/pxe/media/list 里取真实存在的介质；
//   取不到就**留空**（后端 `_to_pxeconfig` 会按模板版本推导），并在弹窗里明确提示。
function mediaFor(row) {
  const list = (mediaList.value.media || []).filter(m => m.os_type === row.os_type)
  const exact = list.filter(m => String(m.os_version) === String(row.os_version))
  return exact.find(m => m.complete) || exact[0]
      || list.find(m => m.complete) || list[0] || null
}

function mediaDefaults(row) {
  const m = mediaFor(row)
  if (!m) {
    return {
      kernel_path: "", initrd_path: "", squashfs_path: "",
      note: `没有 ${row.os_type}/${row.os_version}/ 的引导介质：内核路径留空，由后端按模板版本推导。`
          + `若后端也找不到，iPXE 会报 "Could not boot image" —— 请先在下面的 ISO 管理里提取介质。`,
    }
  }
  const base = m.os_type + "/" + m.os_version + "/"
  const files = m.files || []
  return {
    kernel_path: m.kernel ? base + m.kernel : "",
    initrd_path: m.initrd ? base + m.initrd : "",
    // squashfs 只是 Ubuntu 的 casper 介质；目录里没有就留空（不编一个不存在的文件名）
    squashfs_path: files.includes("installer.squashfs") ? base + "installer.squashfs" : "",
    note: (m.os_type === row.os_type && String(m.os_version) === String(row.os_version))
      ? ""
      : `模板版本 ${row.os_type}/${row.os_version} 没有介质，已临时改用实际存在的 ${m.os_type}/${m.os_version}/。`
        + `若这不是你要装的版本，请先在下面「ISO 镜像管理」里把 ${row.os_type}/${row.os_version} 的介质提取出来再重新生成。`,
  }
}

function openGenDialog(row) {
  genForm.hostname = "server01"
  genForm.server_ip = ""
  genForm.http_root = ""
  const d = mediaDefaults(row)
  genForm.kernel_path = d.kernel_path
  genForm.initrd_path = d.initrd_path
  genForm.squashfs_path = d.squashfs_path
  genMediaNote.value = d.note
  genDialog.value = true
  genFiles.value = {}
  genKey.value = ""
  sessionStorage.setItem("pxe_profile_id", row.id)
  doGenerate()
}

// ★ 外部审查 U5-F7：把"生成时用的那一套参数（含 installs 快照）"记下来，下载/部署前先核对。
//   改前 doGenerate 与 doDownload 各自现取 `installs.value`，而后台 10s 轮询会刷新它 ——
//   于是「下载的 ZIP 里 MAC→主机名映射」与屏幕上核对过的预览可能不是同一份。
function genBody() {
  return {
    hostname: genForm.hostname,
    server_ip: genForm.server_ip,
    http_root: genForm.http_root,
    kernel_path: genForm.kernel_path,
    initrd_path: genForm.initrd_path,
    squashfs_path: genForm.squashfs_path,
    deploy_mode: genForm.deploy_mode,
    installs: installs.value.map(i => ({ mac: i.mac, hostname: i.hostname })),
  }
}
const genBodyKey = computed(() => JSON.stringify(genBody()))
const genStale = computed(() =>
  !!Object.keys(genFiles.value).length && genBodyKey.value !== genKey.value)

async function doGenerate() {
  generating.value = true
  const seq = ++genSeq
  try {
    const pid = sessionStorage.getItem("pxe_profile_id")
    const body = genBody()
    const key = JSON.stringify(body)
    const res = await http.post("/it/pxe/profiles/" + pid + "/generate", body)
    if (seq !== genSeq) return          // 期间又发起了新的生成 ⇒ 丢弃这次响应
    genFiles.value = res.files
    genKey.value = key
    const keys = Object.keys(res.files)
    if (keys.length) activeFile.value = keys[0]
    ElMessage.success("生成完成: " + keys.length + " 个文件")
  } finally { if (seq === genSeq) generating.value = false }
}

async function doDownload() {
  // 预览已被参数/装机记录变化作废时必须先重新生成（否则"审核的不是下载的"）
  if (genStale.value) { ElMessage.warning("参数或装机记录已变化，请先点「生成文件」再下载"); return }
  const pid = sessionStorage.getItem("pxe_profile_id")
  // 只有真拿到 ZIP 才提示"下载已开始"（外部审查 U5-F3）
  const ok = await downloadZip("/it/pxe/profiles/" + pid + "/download", genBody())
  if (ok !== true) return
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
onMounted(() => { loadProfiles(); loadInstalls(); loadServerStatus(); loadIsos(); loadMedia(); startInstallPolling() })
</script>

<style scoped>
/* 卡头右侧动作区：dnsmasq 状态 tag 在前（沿用 EP 相邻按钮间距），与标题同行等高 */
.head-actions { display: flex; align-items: center; }
.head-actions .el-tag { margin-right: var(--ot-space-2); }

/* 区块间距（等值搬自原内联 margin，能对上 token 的用 token） */
.mt-2 { margin-top: var(--ot-space-2); }
.mb-2 { margin-bottom: var(--ot-space-2); }
.mb-3 { margin-bottom: var(--ot-space-3); }
.mt-10 { margin-top: 10px; }

/* 卡片内小节标题 / 文件标签 / 空态文案 */
.group-label { margin-bottom: var(--ot-space-1); font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.file-tag { margin: 2px; }
.list-empty { color: var(--ot-text-4); font-size: var(--ot-font-xs); }

/* 终端日志：在全局 .terminal-output（pre-wrap / 480px）基础上收窄，用联合选择器保证覆盖 */
.terminal-output.log-pre { white-space: pre; }
.terminal-output.log-200 { max-height: 200px; }
.terminal-output.log-420 { max-height: 420px; }

/* 表格行内控件宽度（保留原像素，不做刻度改写） */
.os-type-select { width: 90px; margin-right: 6px; }
.os-ver-input { width: 80px; margin-right: 6px; }
.w-full { width: 100%; }
/* 输入框 + 行内单位（如 GB）：不换行、垂直居中，单位贴输入框右侧 */
.inline-unit { display: flex; align-items: center; gap: var(--ot-space-1); width: 100%; white-space: nowrap; }
.existing-kind { width: 84px; }
.existing-value { width: 146px; margin-left: var(--ot-space-1); }
.table-gap { margin-bottom: 6px; }

/* 提示与帮助文案（--ot-warning / --ot-text-3 与原 --el-* 变量同值，见 tokens.css） */
.form-hint { margin-top: var(--ot-space-1); font-size: var(--ot-font-xs); line-height: 1.4; color: var(--ot-warning); }
.warn-text { color: var(--ot-warning); }
.warn-note { margin-top: 10px; font-size: var(--ot-font-xs); color: var(--ot-warning); }
.switch-note { margin-left: var(--ot-space-2); font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.help-text { margin-bottom: 6px; font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.alert-detail { font-size: var(--ot-font-xs); line-height: 1.6; }
.alert-list { margin: var(--ot-space-1) 0 0 var(--ot-space-4); padding: 0; }
/* 悬浮长说明：tooltip 气泡内容限宽（#content slot 带 scoped 属性，样式可达传送后的节点） */
.tip-body { max-width: 420px; font-size: var(--ot-font-xs); line-height: 1.6; }
.tip-icon { color: var(--ot-warning); cursor: help; }
/* 压成一行的区块说明（替代原整段 disk-auto-note / el-alert 说明） */
.disk-auto-line { margin-bottom: var(--ot-space-3); font-size: var(--ot-font-xs); color: var(--ot-text-3); display: flex; align-items: baseline; flex-wrap: wrap; gap: 0 var(--ot-space-2); }
.mode-note { margin-top: var(--ot-space-2); font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.written-note { margin-top: var(--ot-space-2); font-size: var(--ot-font-xs); color: var(--ot-text-3); }
.dialog-body { line-height: 1.7; }
.path-code { font-size: var(--ot-font-sm); }
</style>
