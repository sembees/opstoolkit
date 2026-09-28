# 宿主机侧的 dnsmasq 重载单元

这里的东西**必须装到宿主机上**（不是容器里）。

## 为什么需要

OpsToolkit 的容器是 `network_mode: host` + `privileged`，但**既没挂 `/run/dbus`，
也没有 `pid: host`** —— 它物理上无法命令宿主机的 systemd。于是：

- 容器能把配置写进 `/etc/dnsmasq.d/opstk-pxe.conf`（该目录是挂载进去的）；
- 但"让正在跑的 dnsmasq **加载**这份新配置"这一步，只能在宿主机上做。

不装这套单元的直接后果：`/deploy` 把文件写下去了，正在跑的 dnsmasq 却还是旧配置。
磁盘上的扁平 `boot.ipxe` 可能已经是"未登记机器一律拒绝安装"的安全菜单，于是全网机器
都拿到一份对不上的菜单，**装机全部卡在 PXE 循环里**，而接口却返回成功、界面显示
"部署完成"。这个假成功是真实发生过的故障（RUNBOOK-STATE §5.41）。

## 安装

```bash
sudo bash deploy/host/install-opstk-dnsmasq-reload.sh
```

它会：装 `opstk-dnsmasq-reload.sh` 到 `/usr/local/sbin/`、装 `.path`/`.service` 两个
单元、`daemon-reload`、`enable --now` 那个 path 单元，并立刻执行一次。

**每次改动本目录下的脚本或单元，都要重跑这条命令**（`systemctl restart` 不够）。

## 它是怎么工作的

```
容器：写 /etc/dnsmasq.d/opstk-pxe.conf
        ↓ （PathChanged / PathModified 监视这个文件）
宿主机：opstk-dnsmasq-reload.path  →  触发 opstk-dnsmasq-reload.service
        ↓
        opstk-dnsmasq-reload.sh：
          flock 串行化 → dnsmasq --test 校验 → 内容没变就跳过 restart
          → systemctl restart dnsmasq → 确认 active → 复核 sha 未变
        ↓
        写状态文件 /srv/opstk/state/.dnsmasq-reload.state（原子替换）
          OK <配置sha> <完成时刻>   /   FAIL <原因> <时刻>
        ↓ （/srv/opstk/state 是挂进容器的）
容器：核对"sha 等于它刚写的那份、且时间戳不早于本次部署开始" → 才算部署成功
```

三个文件的分工：

| 文件 | 谁写 | 干什么用 |
|---|---|---|
| `.dnsmasq-reload.state` | 宿主脚本，每次运行 | **PXE 槽位**的完成/失败标记，应用侧据此判定"生效了没有" |
| `.dnsmasq-reload.state.ztp` | 宿主脚本，每次运行 | **ZTP 槽位**同上（两个模块各写各的配置，不能互相冒充） |
| `.dnsmasq-applied` | 宿主脚本，**仅成功时** | 记"上一次成功加载的两个 sha"（第 1 行 PXE、第 2 行 ZTP），据此跳过无变化的重启（也避免撞 systemd 启动限流） |
| `.dnsmasq-reload.link` | 宿主脚本，每次运行、**在可能失败的动作之前** | `RUN <版本> <时刻>`：证明"单元被触发过、脚本跑得起来、而且不是旧版脚本" |

## 触发机制（**实测**，别再凭印象改 `[Path]`）

真机上逐个事件测过（RUNBOOK §5.50），结论反直觉：

| 动作 | 是否触发 |
|---|---|
| `touch` / `os.utime`（只改 mtime，IN_ATTRIB） | **不触发** |
| 临时文件 + `os.replace`（rename 替换，IN_MOVED_TO） | **不可靠** —— watch 挂在被换掉的 inode 上，实测同一个部署里"第一次触发、紧接着第二次没有" |
| **原地写入**（IN_CLOSE_WRITE / IN_MODIFY） | **稳定触发** |
| 改成监视目录 `/etc/dnsmasq.d` 代替文件 | 对 rename 同样不触发，没解决问题 |

因此应用侧有一条硬契约：**写 `/etc/dnsmasq.d` 下的配置必须原地写，不许 rename 替换**
（见 `app/core/dhcp.py::write_conf` 与 `reload_preflight` 的探针）。
部署前的活体探测也是"原地写回同样的字节"，靠它证明链路通。

另外 systemd 对**已经在运行/正在启动**的服务，`start` 是空操作：一次写入若落在上一次
运行还没结束的窗口里，这个边沿事件会被**吃掉**，应用只能等满超时。所以应用侧超时后会
**再原地写一次补边沿**（`dhcp.wait_host_reload_retrying`）—— 失败重试时看到
"已补触发 N 次"就是这条路径。

## 部署前的预检（应用侧，`dhcp.reload_preflight`）

`/deploy`（PXE）与 `/ct/ztp/templates/{tid}/deploy`（ZTP）在**写任何文件之前**都会先问一句
"这条链路通不通"：

1. `/srv/opstk/state` 存在且可写（容器要能写它、也要能删掉旧标记）；
2. `.dnsmasq-reload.link` 存在且版本 >= 应用要求的最低版本（`HOST_RELOAD_MIN_SCRIPT_VERSION`）；
3. 活体探测：**原地写回**配置文件的同样字节，等宿主脚本写出一个新的状态文件
   （PXE 写入自己的槽位、ZTP 写 `.ztp` 槽位）。

任何一条不过，部署就**零落盘**失败，并直接给出该做什么。这修掉的是
"文件全改完了才等到超时失败，网络半新半旧"这个更糟的结局（RUNBOOK-STATE §5.49）。
另外**落盘前还有一道红线检查**（`dhcp.check_dhcp_conf_safety`）：占位/不存在的网卡、
承载默认路由的骨干网卡、池落在骨干网段、以及"不可重复关键字"（如 `port=`）与目录里
别的 `.conf` 冲突 —— 全部拒绝。见 §5.50。

## 版本契约（重要）

`opstk-dnsmasq-reload.sh` 里的 `SCRIPT_VERSION` 必须 >= 应用侧
`backend/app/core/dhcp.py` 里的 `HOST_RELOAD_MIN_SCRIPT_VERSION`
（当前 v3：同时监视/重载 PXE 与 ZTP 两份配置，两个槽位各写各的状态文件）。

改了脚本的**协议**（例如状态文件的格式、新增标记文件）就两边一起 +1。这样一台装了旧版
脚本的宿主机不会"每次部署都超时失败"，而是在预检阶段被明确告知"重跑 install 脚本"。

## 排查

```bash
systemctl status opstk-dnsmasq-reload.path              # 单元活着吗
journalctl -u opstk-dnsmasq-reload --since '-10 min'    # 它跑过吗、报了什么
cat /srv/opstk/state/.dnsmasq-reload.state              # 最后一次结论
cat /srv/opstk/state/.dnsmasq-reload.link               # 版本标记
dnsmasq --test                                          # 配置本身合法吗
```

常见现象：

| 现象 | 原因 |
|---|---|
| 状态文件里 `FAIL test-failed:...` | 生成的配置本身不合法（`dnsmasq --test` 的输出被抹掉空白后附在原因里） |
| 状态文件里 `FAIL restart-failed` | systemd 拒绝重启（启动限流最常见；改成"内容未变不重启"后已大幅减少） |
| 状态文件里 `FAIL config-changed` | 重启期间配置又被另一个部署改了；那次部署会自己失败重试 |
| 应用侧说"没找到版本标记" | 没装单元，或者装的是旧版脚本 → 重跑 install 脚本 |
| 应用侧说"重载单元没有反应" | `.path` 没在跑（改过单元文件没 daemon-reload / 没重跑 install） |
| 应用侧说"状态目录不可写" | `/srv/opstk/state` 的属主或挂载方式不对 |

## 回滚（应用侧，`server._restore_previous`）

写完文件之后才发现失败时，应用只在**能证明宿主机没重启过 dnsmasq** 的情况下回滚
——即宿主脚本报的是 `test-failed` / `missing-conf` / `empty-sha` 这类
**restart 之前**的失败。那时把磁盘恢复成部署前的字节就等于回到一致的旧状态。

证明不了（状态文件缺失、读不了、或报的是 `restart-failed`/`not-active`/`config-changed`）
就**保持现状并明确写着"未回滚"**：那时把磁盘改回旧内容，只会让"磁盘 vs 正在运行的
守护进程"更不一致。
