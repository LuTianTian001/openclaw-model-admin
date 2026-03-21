# 给 OpenClaw 的部署排障提示词（OpenClaw Model Admin）

本文件供 **OpenClaw 自部署 / 自建网关用户**在部署 **[openclaw-model-admin](https://github.com/LuTianTian001/openclaw-model-admin)** 遇到问题时，将**固定上下文**交给智能体，减少反复解释、提高自动排查质量。

---

## 你怎么用（人类操作）

1. 打开本文件，**完整复制**下方 **「【复制区域开始】」到「【复制区域结束】」** 之间的全部文字（含标题与列表）。
2. 粘贴到 **OpenClaw（或你用的助手）对话的第一条或回复开头**。
3. **紧接着另起一行**，用自然语言写：**现象**（报错原文、执行了什么命令、是否 Docker、面板端口、是否 systemd）、**你已尝试的步骤**（可选）。
4. **不要**粘贴 GitHub PAT、API Key、私钥、完整 `openclaw.json` 里的密钥字段；可打码或只说「某 provider 已配置」。

一键安装完成后，本文件位于安装目录（默认 `~/openclaw-model-admin/给OpenClaw的部署排障提示词.md`）。未克隆时也可在浏览器打开仓库中同名文件，或使用 Raw（见文末链接）。

---

【复制区域开始】

## 任务：协助排查 OpenClaw Model Admin 的部署与运行问题

### 项目是什么

- **OpenClaw Model Admin**：运行在 **GNU/Linux** 上的轻量 Web 管理面板（Python 3 标准库 HTTP + 静态页），用于编辑 **`openclaw.json`**、**`sessions.json`**（路径可配置）、调用本机 **`openclaw config validate`**（可跳过）、可选 **`systemctl` 重启网关**。
- **官方约束**：**仅支持 Linux**；安装脚本 `install.sh` / `start.sh` 在非 Linux 会直接退出。
- **最关键坑**：运行面板的 **Unix 用户**必须与 **OpenClaw 网关实际读写的数据目录**一致，或必须通过 **`OPENCLAW_HOME` / `OPENCLAW_CONFIG_PATH`** 显式指向网关正在使用的那份 `openclaw.json`，且进程对该路径有读写权限。否则表现为「改了不生效」「保存失败」「会话不对」。

### 权威参考（请你优先对照，不要臆造路径）

- 人类可读完整手册（含 Docker、systemd、FAQ）：仓库根目录 **`操作手册.md`**  
  Raw：<https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/操作手册.md>
- 一键安装脚本 Raw：<https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh>
- 环境变量模板：**`.env.example`**；运行时 **`start.sh` 会 `source` 安装目录下的 `.env`**。
- 默认安装命令：  
  `curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | bash`  
  无 git：`USE_GIT=0`。安装目录默认 **`$HOME/openclaw-model-admin`**。

### 请你（助手）的工作方式

1. **先诊断、后改配置**：在建议修改 `openclaw.json`、`.env`、systemd、compose 之前，先用**只读命令**确认事实（系统、用户、路径、进程、端口、是否在容器内）。
2. **按概率排序假设**：用户不一致 / 配置路径错 / 无 `openclaw` 导致校验失败 / 无 systemd 却使用默认重启 / **Docker 内将网关地址写成 `127.0.0.1`（实际指向容器自身而非宿主机）** / 防火墙与监听地址 `0.0.0.0`。
3. **输出可执行步骤**：给出具体命令、预期现象、失败时的下一条命令；涉及写文件前提醒备份。
4. **安全**：不要向用户索要 token、私钥；不要鼓励把密钥贴进聊天。日志与配置片段需打码。

### 建议的排查顺序（按需跳过，说明跳过理由）

1. `uname -s` 是否为 `Linux`。
2. `python3 -c "import sys; print(sys.version_info[:2])"` 是否 **≥ (3, 10)**。
3. 面板是否监听：`ss -ltn` 或用户提供的端口；默认 **`OPENCLAW_MODEL_ADMIN_PORT=8765`**。
4. **配置路径**：若用户能打开面板，以 **`/api/state` 返回的 `configPath`**（或页面展示）为准，与网关实际使用的 `openclaw.json` 是否一致；检查 **`OPENCLAW_HOME`、`OPENCLAW_CONFIG_PATH`** 与 **`whoami`**。
5. **`openclaw` CLI**：`command -v openclaw`；若无且为宿主机直跑，说明保存失败时可否临时 **`OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1`** 及风险。
6. **网关在线/重启**：默认 **`systemctl`** 与 **`OPENCLAW_GATEWAY_SERVICE`**（常为 `openclaw-gateway.service`）；若无 systemd 或权限不足，需 **`OPENCLAW_GATEWAY_RESTART_COMMAND`**。状态误判时配置 **`OPENCLAW_GATEWAY_HEALTH_URL`**（HTTP 探测，容器内需指向**可达的宿主机或服务名地址**）或 **`OPENCLAW_GATEWAY_SS_MARKERS`**。
7. **会话路径**：一般**不要手写**；默认 **`<openclaw.json 父目录>/agents/main/sessions/sessions.json`**，除非用户目录结构特殊才设 **`OPENCLAW_SESSIONS_PATH`**。

### 仓库内关键文件（便于你让用户 cat 或贴片段）

- `install.sh`、`start.sh`、`server.py`、`static/index.html`
- `.env.example`、`openclaw-model-admin.service.example`、`docker-compose.example.yml`

### 用户下一条消息将描述具体现象

请基于上述上下文与用户补充的现象，给出**分步解决方案**；若信息不足，**列出你需要用户执行并回传的关键命令输出**（仍注意打码）。

【复制区域结束】

---

## 链接速查（人类点开）

| 资源 | URL |
|------|-----|
| 本提示词 Raw | <https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/给OpenClaw的部署排障提示词.md> |
| 操作手册 Raw | <https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/操作手册.md> |
| 仓库首页 | <https://github.com/LuTianTian001/openclaw-model-admin> |

> 若 Raw 链接在终端或旧工具中打不开中文路径，请在 GitHub 网页上打开仓库根目录同名文件。
