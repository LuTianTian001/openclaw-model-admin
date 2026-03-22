# 更新日志

版本号与 **管理端自报版本**（页面「本机信息」里的「管理端版本」）对齐：默认取自 `server.py` 中 `PANEL_META_VERSION`，可用环境变量 `OPENCLAW_MODEL_ADMIN_VERSION` 覆盖。

## 1.2.0 — 2026-03-23

### 新增

- **本机信息**：展示 **OpenClaw CLI** 当前版本（`openclaw -V`，带短时进程内缓存）。
- **远端版本检查**：对比 **npm** `openclaw` 包稳定版；服务端默认 **12 小时**缓存（`OPENCLAW_ADMIN_OPENCLAW_VERSION_CHECK_SEC`）。
- **内置更新**：有更新时显示按钮，调用官方 **`openclaw update --yes --json`**（默认行为含网关重启，以 CLI 为准）；可用 `OPENCLAW_ADMIN_OPENCLAW_UPDATE_DISABLE=1` 禁用网页触发。
- **前端**：页载与每 **12 小时**后台轮询版本；**展开「本机信息」**时强制重新拉取 npm 最新版。

### API

- `GET /api/openclaw/version-check?force=0|1`
- `POST /api/openclaw/update` — body 可选 `{ "noRestart": true }`

### 其它

- `.env.example` 补充上述相关环境变量说明。
- `install.sh` 安装成功日志中打印 **v1.2.0**，强调仍为一键安装入口。

---

## 1.1.0 及更早

见 Git 历史与 `README.md` 功能列表；本文件自 1.2.0 起维护。

---

## 路线图（备忘）

- **v2.0.0（下一个大版本，规划中）**：计划落地 **三项**面向使用体验的能力；整体目标优先：**让新用户尽可能「一条命令完成可运行部署」**（减少对手动改防火墙、路径、Docker 网络等隐性步骤的依赖）。
- 一键入口保持为：`curl -fsSL …/install.sh | bash`（无 git 时 `USE_GIT=0`）；大版本将围绕该路径增强预检、模板与文档闭环。
