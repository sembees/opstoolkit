# PXE 装机：目标盘与数据盘的识别规则

本文回答一个问题：**"装到哪块盘""哪块盘不许碰"到底是怎么判定的**，
以及 Ubuntu 与 RHEL 两条路径的差异与已知限制。

> 详细取证与踩坑记录见仓库外的运行手册（`RUNBOOK-STATE.md` §5.42 / §5.45 / §5.46 / §5.47）。

## 1. 一条贯穿始终的原则：只接受"不生效就大声报错"的标识

**失效方式比功能本身更重要。** 给安装器一个它不认的标识，有两种可能的结局：

| 结局 | 例子 | 生产可接受性 |
|---|---|---|
| 大声报错，装机中止 | `matched no disk`；`path: /dev/sdX` 不存在 | ✅ 安全失败 —— 装不上，但不会装错 |
| **静默退化成"没条件"**，于是匹配到第一块盘 | 未知键（如 `id_path`） | ❌ **可能直接抹掉数据盘** |

所以产品**在生成期就拒绝**那些"不生效会静默退化"的键。真机实测过：连写两次、
两个不同的 `id_path` 值，都打到同一块 30G 数据盘，20G 系统盘一根没动。

**推论（工程上必须记住）**：不要用"是否推进到了下一阶段"来判断某个标识生效了 ——
那会被更早出现的日志行骗到。**唯一可信的判据是**：看安装器日志里"它到底选了哪块盘"
（curtin 的 `get_path_to_storage_volume … 'path': '/dev/sdX'`），
或者看装完后**哪块盘的标记变了**。

## 2. RHEL 系（anaconda / kickstart）

anaconda 没有"自动选盘"原语，所以目标盘在 `%pre` 里现场挑，再 `%include` 生成的片段。

- `target.mode=auto`：候选 = 整盘、非可移动、非 USB、非 `zram/loop/sr`、容量达标，
  并且**不是**任何 `data_disks` 声明的那块盘；按名字排序取第一块。
- `target.mode=match`：按 `serial` 或 `model` 精确命中（**两者都为空会被拒绝** ——
  空匹配串会命中任意盘）。
- `target.mode=name`：直接写死盘名。

**数据盘怎么排除**：`data_disks` 必须给出**稳定属性** `size` / `serial` / `wwid` 之一，
**只写 `name` 会被拒绝**。原因见下节。

## 3. 为什么 `data_disks` 不能用设备名（重要）

`sdX` 由内核探测顺序决定。同一套虚机配置实测出现过：

```
VM142/VM144:  scsi0:0:0:0 -> sda(30G 数据盘)   scsi0:0:0:1 -> sdb(20G 系统盘)
VM143:        scsi0:0:0:0 -> sdb(30G 数据盘)   scsi0:0:0:1 -> sda(20G 系统盘)   ← 反了
```

而 VM143 **同一台机器两次启动还出现过两种顺序**。按名字做排除集时，
排除掉的可能是**系统盘**，于是目标盘落到数据盘上、`clearpart` 把它抹掉。
**三台并发里就中了 1 台** —— 单机验证永远碰不到。

因此：`data_disks` 只认 `size` / `serial` / `wwid`；`name` 仅作备注。
`%pre` 会逐个声明校验"至少要命中一块"，并为**每个声明**单独校验（声明两块只中一块也要中止）。

## 4. Ubuntu（subiquity / curtin）——差异与限制

Ubuntu 侧的目标盘与数据盘由 `storage.config` 的条目表达，**语义与 RHEL 不同**：

| 键 | 说明 |
|---|---|
| `path`（= `target.mode=name`） | 设备名，如 `/dev/sdb`。**目前唯一可靠的指定方式**；名字错会大声报错 |
| `serial` | subiquity 取 **sysfs** 的 `/sys/block/sdX/device/serial`。虚拟化（QEMU/virtio-scsi）下该属性**为空** → `matched no disk` |
| `wwn` / `model` / `vendor` | 盘没有 WWN、或同型号多盘时歧义 |
| `id_path` 等未知键 | **不报错，退回"匹配第一块盘"** → 可能抹数据盘，产品已拒绝 |

**当前限制**：在"没有真实序列号/WWN、且多块盘同型号"的环境（即本项目的测试环境，
也是很多虚拟化环境）里，**Ubuntu 自定义分区只能用 `target.mode=name` 指定目标盘**。
请务必确认填的盘名与"数据盘"**不是同一块**。

> **未验证**：真机上若磁盘提供真实序列号 / WWN，`serial` / `wwn` 也许可用。
> 本项目没有真机，未做验证，因此不把它写成结论。

### Ubuntu 侧已实现的其他必要项（缺一即装不上，均真机验证过）

- `grub_device: true` **必需**：缺了就报
  `did not create needed bootloader partition`（这个报错会把人误导到分区层面去找，实际与分区无关）。
- `ptable`：有 `/boot/efi` 用 `gpt`，否则用 `msdos`（与 RHEL 侧的 `--location=mbr` 一致）。
- `size` 里的 `rest` 必须翻成 **`-1`**（subiquity 不接受 `"rest"`）。
- `disk.wipe` 是**模式字符串**（`"superblock"`），不是布尔 `true` —— 传 `true` 会
  `ValueError - wipe mode True not supported`，且 `wipe` 默认就是 true，
  所以这一条能让**每一个** Ubuntu 自定义模板都装不上。

## 5. 同一块盘不能既当系统盘又是数据盘

生成期会校验"目标盘的身份"与"每个 `data_disks` 声明的身份"是否重叠
（比 `name` 与 `serial`；只比名字不够 —— 数据盘推荐只给 `size/serial/wwid`，名字是空的）。
重叠即拒绝：目标盘会被清空分区，那等于把声明要保护的数据盘抹掉。

## 6. 排查这类问题的方法

1. **先看安装器的选盘日志**：RHEL 侧 `%pre` 会把磁盘清单（含 `size/serial/wwn`）、
   每个数据盘声明的命中情况、以及最终 `target` 打到**串口**与 `%pre` 日志；
   Ubuntu 侧看 curtin 日志的 `get_path_to_storage_volume`。
2. **给应答文件临时加 `error-commands`**（装机失败时仍会执行），可以把安装器内部状态
   （crash 文件、syslog、curtin 日志）打到串口 —— 这是定位"串口上只显示
   `An error occurred`"这类问题的唯一有效手段。
3. **判据不要用"是否推进到下一阶段"**（见第 1 节）。
