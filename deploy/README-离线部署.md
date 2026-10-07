# OpsToolkit 离线部署（小白版）

> 一句话：**把一个离线包拷到目标机，跑一条安装命令，打开网页跟着引导点完，就能用。**
> 全程**不需要联网**。出问题先跑 `sudo bash doctor.sh`，它会用中文告诉你哪里不对、怎么修。

---

## 0. 目标机需要什么（就这 4 条）

| # | 前提 | 怎么确认 |
|---|------|----------|
| 1 | Linux x86_64（Rocky/CentOS/Ubuntu 均可），内核 ≥ 3.10 | `uname -r` |
| 2 | **Docker 已安装**（含 compose 插件） | `docker --version && docker compose version` |
| 3 | root 权限（或 sudo） | `whoami` |
| 4 | 磁盘富余 ≥ 20 GB（放 ISO 镜像用） | `df -h /srv` |

**明确声明：安装与运行全程不需要联网。** 离线包里已经带好：
应用镜像（内含前后端 + dnsmasq + python3）、部署描述、安装脚本、体检脚本、
宿主机 systemd 单元和本文档。唯一的"外购件"是 Docker 本体（需提前装好）。

---

## 1. 三步装好

把交付给你的离线包文件（形如 `opstk-offline-v1.0.0-20251007.tar.zst`）用 U 盘/内网传到目标机，然后：

```bash
# 第 1 步：解压（没有 zstd 的包是 .tar.gz，用 tar -xzf）
sudo mkdir -p /opt/opstk-pkg
sudo tar --zstd -xf opstk-offline-*.tar.zst -C /opt/opstk-pkg

# 第 2 步：一键安装（自动完成：镜像加载 → 建目录 → 生成密钥 → 写配置 → 启动 → 健康检查）
cd /opt/opstk-pkg/opstk-offline-*/
sudo bash install.sh

# 第 3 步：按屏幕上打印的地址打开网页
#    http://<目标机IP>:8000
```

`install.sh` 跑完会在**结尾打印**：

- 可用的访问地址（自动探测本机 IP，多个网卡会列多条）；
- 「首次打开会引导你完成：**① 改管理员口令 ② 环境体检 ③ 添加 ISO 镜像**」；
- 初始管理员口令（只打印这一次；也可用 `docker logs opstoolkit 2>&1 | grep 初始随机口令` 再取一次）。

> 默认安装位置：程序与配置在 `/opt/opstk`，数据在 `/srv/opstk` 与 `/srv/tftp`。
> 要装到别处/别的盘？看下面 [常见问题](#3-常见问题) 的「换盘」。

---

## 2. 首次打开网页后，跟着引导做三件事

1. **改管理员口令**：用初始口令登录后，第一件事就是改掉它（页面上有入口）。
2. **环境体检**：网页里有体检页；也可以在命令行跑 `sudo bash doctor.sh`，两者结论一致。
3. **添加 ISO 镜像**：把要用来装机的系统 ISO 上传/导入（支持浏览器分块上传，大镜像也可以）。

之后要用 **PXE 裸机装机 / ZTP 配置下发**，还需在**宿主机**（不是容器里）装一次 dnsmasq 重载单元：

```bash
sudo bash deploy/host/install-opstk-dnsmasq-reload.sh
```

为什么需要它：容器只能写 dnsmasq 的配置文件，"让正在跑的 dnsmasq 加载新配置"必须在宿主机做。
不装的话，界面会明确报"重载单元未就绪"（不会假成功）。原理见 `deploy/host/README.md`。

---

## 3. 常见问题

### 3.1 端口被占

| 现象 | 原因 | 怎么修 |
|------|------|--------|
| `53` 被占（体检提示 systemd-resolved） | 系统自带 DNS 解析服务占住 53 | `sudo systemctl disable --now systemd-resolved && sudo rm -f /etc/resolv.conf && echo 'nameserver 223.5.5.5' \| sudo tee /etc/resolv.conf`（DNS 地址换成你们的内网 DNS） |
| `67/69` 被别的 DHCP/TFTP 占 | 装过别的装机服务 | 停掉并禁用旧服务：`sudo systemctl disable --now <服务名>`；确认方法见 doctor.sh 输出 |
| `8000` 被占 | 其他程序占了网页端口 | 换端口安装：`sudo bash install.sh --port 18000`；已装好的改安装目录 `.env` 里 `OPSTK_PORT` 后重启容器 |
| 网页打不开 | 服务没起/端口不对 | `docker logs opstoolkit --tail 50`；`sudo bash doctor.sh` |

### 3.2 权限类

| 现象 | 怎么修 |
|------|--------|
| `install.sh` 提示必须 root | 用 `sudo bash install.sh` |
| 提示连不上 docker | `sudo systemctl start docker && sudo systemctl enable docker` |
| 数据目录不可写 | `sudo chown -R root:root /srv/opstk /srv/tftp`（容器以 root 跑，root 可写即可） |

### 3.3 换盘（数据放别的盘）

1. 把新盘挂好，比如挂到 `/data2`；
2. 旧数据整体搬过去（如果原来就有数据；TFTP 跟到数据根下面，与 --data-root 的默认一致）：
   `sudo rsync -a /srv/opstk/ /data2/opstk/ && sudo rsync -a /srv/tftp/ /data2/opstk/tftp/`
3. 改安装目录 `.env`（`/opt/opstk/.env`）两行：
   `OPSTK_DATA_ROOT=/data2/opstk`、`OPSTK_TFTP_ROOT=/data2/opstk/tftp`
4. 重启容器：`docker compose --project-directory /opt/opstk -f /opt/opstk/deploy/docker-compose.yml up -d`

> 全新机器一步到位：`sudo bash install.sh --data-root /data2/opstk`（TFTP 根会自动跟到
> `/data2/opstk/tftp`；要分开可用 `--tftp-root` 单独指定）

### 3.4 升级

拿到新离线包后：

```bash
tar --zstd -xf opstk-offline-<新版本>-*.tar.zst -C /opt/opstk-pkg
cd /opt/opstk-pkg/opstk-offline-<新版本>-*/
sudo bash install.sh --upgrade
```

升级**只换镜像、重建容器**；`/srv` 下的数据、SQLite 库、`backend/.env` 里的密钥一律原样保留（脚本保证不覆盖已有密钥）。

### 3.5 迁移到新机器（搬数据）

在旧机上打包三样：安装目录 `/opt/opstk`（含 `backend/.env` 密钥与 `data/` 数据库）、数据根 `/srv/opstk`、TFTP 根 `/srv/tftp`；在新机上用**同版本**离线包装好（先 `--no-start`），再把三样东西 rsync 到相同路径，最后启动。密钥随 `backend/.env` 走，老凭据才能解开。

### 3.6 忘记管理员口令

`docker exec -it opstoolkit python3 - <<'PY'` 方式较繁琐；最简单的办法是问管理员重置，或删除管理员表重建：
联系交付支持，或（确认无其他方式时）备份后删除 `data/ops.db` 里 `user` 表中该用户记录后重启容器重新生成初始口令。

### 3.7 目标机连 Docker 都没有

本离线包不含 Docker 本体。请在有网机器下载 Docker 离线安装包（rpm/deb 或 static binary）一并带到目标机安装；这是唯一的"外购件"。

---

## 4. 体检脚本 doctor.sh（只读，放心跑）

```bash
sudo bash doctor.sh            # 全套体检
sudo bash doctor.sh --port 18000   # 非默认端口时
```

它检查（**全程只读，不改任何东西**）：docker/compose 是否可用；53/67/69/网页端口被谁占
（重点：systemd-resolved 占 53 时会直接给出关闭命令）；数据目录是否存在可写；磁盘余量；
ISO 目录是否有镜像；是否 root；镜像/容器状态与 /health；四把密钥是否齐；宿主机 dnsmasq
重载单元是否安装。结论分 [正常] / [注意] / [需处理]，[需处理] 项都附带"怎么修"。

---

## 5. 安装目录与数据目录长什么样

```
/opt/opstk/                    ← 安装根（compose 项目根）
├── .env                       ← 部署变量（端口/数据根/镜像名；install.sh 生成，可手改后重启容器）
├── backend/.env               ← 应用密钥（credential_key/secret_key/pxe_serve_token/ztp_serve_token；chmod 600）
├── data/                      ← SQLite 数据库（ops.db）
└── deploy/
    ├── docker-compose.yml     ← 应用态部署描述
    ├── container-entrypoint.sh← 容器入口（compose 挂载进容器）
    └── .env.example           ← 变量说明（含默认值）

/srv/opstk/                    ← 数据根（OPSTK_DATA_ROOT）
├── iso/    pxe-web/    ztp-web/    state/    mnt/
/srv/tftp/                     ← TFTP 根（OPSTK_TFTP_ROOT），PXE 引导文件
```

---

## 6. 装了什么、怎么停

```bash
docker ps --filter name=opstoolkit          # 看容器
docker logs --tail 100 opstoolkit           # 看日志
docker compose --project-directory /opt/opstk -f /opt/opstk/deploy/docker-compose.yml restart   # 重启
docker compose --project-directory /opt/opstk -f /opt/opstk/deploy/docker-compose.yml down   # 停止并删除容器
```

`down` 只删容器，**不会**删除 `/srv/opstk`、`/srv/tftp`、`data/`、`.env` 里的任何数据与密钥（都是挂载目录/文件）。想彻底重装：`down` 后删掉 `/opt/opstk` 重新 `install.sh`（数据根没动的话数据还在）。

---

## 7. 给运维/交付的补充（非小白部分）

- 应用态 compose：`deploy/docker-compose.yml` —— 镜像自包含（前端已构建进镜像），**不挂源码**，只挂数据；
  所有宿主路径都是 `${OPSTK_XXX:-<默认值>}`，默认值与既有生产部署逐字一致。
- 开发态快速迭代（挂源码）仍然可用：
  `docker compose --project-directory . -f deploy/docker-compose.yml -f deploy/docker-compose.dev.yml up -d`（仓库根执行）。
- 重新打离线包：在有网机器上 `bash packaging/make-offline-bundle.sh --version <v>`（目标机不需要联网）。
- 宿主机 dnsmasq 重载单元的原理与排查：见 `deploy/host/README.md`。
