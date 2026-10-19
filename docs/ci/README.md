# CI 启用说明（GitHub Actions）

## 现状

工作流内容已经写好并随仓库一起版本化，但**不能直接放在 `.github/workflows/ci.yml`**：

```
! [remote rejected] ci-workflow -> ci-workflow
  (refusing to allow a Personal Access Token to create or update workflow
   `.github/workflows/ci.yml` without `workflow` scope)
```

GitHub 对 `.github/workflows/**` 的推送有额外要求：推它的 token 必须带 `workflow` 权限。
当前使用的 PAT 没有这个 scope，所以任何包含该路径的 push 都会被**整次拒绝**
（不是这次提交被拒，是整次 push 失败）—— 这也是为什么这份 YAML 放在 `docs/ci/`。

## 两种启用方式（任选其一）

### A. 给 token 加 `workflow` 权限（推荐，之后可以正常改 CI）

1. GitHub → Settings → Developer settings → Fine-grained tokens（或 Classic tokens）；
2. 编辑当前 token，勾上 **Workflow**（Classic 的 `workflow` scope / Fine-grained 的
   `Actions: Read and write` + `Workflows: Read and write`）；
3. 本地执行：

```bash
mkdir -p .github/workflows
git mv docs/ci/github-actions-ci.yml .github/workflows/ci.yml   # 或 cp 后 rm 原件
git add .github/workflows/ci.yml
git commit -m "ci: 启用 GitHub Actions（后端 pytest + 真 netplan + 前端）"
git push
```

### B. 不改 token，直接在网页上新增文件

1. 打开 `https://github.com/sembees/opstoolkit` → Add file → Create new file；
2. 文件名填 `.github/workflows/ci.yml`；
3. 把本目录 `github-actions-ci.yml` 的内容整段粘进去，提交。
   （网页提交走的是你浏览器登录态，不受 PAT scope 限制。）

## 三个作业在做什么

| 作业 | 目的 | 关键点 |
|---|---|---|
| `backend` | 全量 `python -m pytest tests -q` | Linux runner 上应当**全绿**；Windows 本机那 21 条失败是环境基线（依赖真实网卡/dnsmasq 单元），不是代码问题 |
| `netplan` | 装真 `netplan.io` 跑 `tests/test_netconfig_netplan_real.py` | 用真工具校验生成的 YAML（本地只有 Windows，靠这条兜语法与键名） |
| `frontend` | `npm ci` / `npm test` / `npm run build` | 单测用 Node 自带 runner，零新依赖 |

`backend` 里那段 "造 ens19 dummy 网卡" 是刻意的：部署用例会读真实网卡清单来断言
"部署前红线检查"，runner 只有 eth0 时它们会红（环境前提，不是缺陷）。

## 本地预演（不推 GitHub 也能验）

```bash
# 需要 act；镜像里没有 iproute2 时作业自己会装
act -j backend
act -j frontend
```

之前用 `act` 真跑过：`backend` 与 `frontend` 两个作业均通过；`netplan` 作业在 `act`
镜像里装 `netplan.io` 较慢，建议直接依赖 GitHub runner。
