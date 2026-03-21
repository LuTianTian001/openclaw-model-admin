# OpenClaw Model Admin

面向 **OpenClaw** 网关生态的**本地 Web 管理面板**：在浏览器里调整默认模型与 fallback、模型库、`openclaw.json` 中的模型思考参数，并读写 `sessions.json` 做会话侧同步（与网关 `/status` 解析一致）。**纯 Python 标准库 + 本机 `openclaw` CLI（用于配置校验）**。

**平台范围：仅适配 GNU/Linux**（常见发行版；安装/启动脚本依赖 `bash`，状态与端口探测依赖 **`systemctl` / `ss`** 等 Linux 常见环境）。**不承诺**支持 macOS、Windows 等非 Linux 环境；若在非 Linux 上自行运行，属于未测试用法。

---

## 安全提示（必读）

- **切勿**在 Issue、PR、聊天或代码仓库中粘贴 **GitHub Personal Access Token** 或任何 API 密钥。若已泄露，请立刻到 GitHub → **Settings → Developer settings → Personal access tokens** **撤销**该令牌并重新生成。
- 推送本仓库到 GitHub 时，使用 **SSH 密钥** 或 **gh auth login**，不要把 token 写进仓库文件。

---

## 功能概览

- 路由：默认模型、备用顺位、主模型思考档位（写入 `agents.defaults.models[<ref>].params.thinking`）
- 全局：推理展示、提权、`applyToSessions` 下发；应用后从电报私聊同步非思考行为到网页主会话（保留网页 `modelOverride`）；清除全会话 `thinkingLevel` 使思考跟模型配置走
- 模型库：按供应商折叠、接入/编辑/删除模型
- 调用 `openclaw config validate` 校验配置；可选 `systemctl` 重启网关服务

---

## 环境要求（Linux）

- **操作系统**：**Linux**（x86_64 / aarch64 等常见架构；glibc 环境）
- **Python 3.10+**（推荐 3.12）
- 本机已安装 **`openclaw`** 且可在 PATH 中执行（用于保存配置前的校验）
- 已存在 OpenClaw 数据目录（默认当前运行用户的 `~/.openclaw/`，内含 `openclaw.json`；**请用与网关相同的用户**运行本面板，以便读写同一份配置与会话）
- 使用「重启网关」按钮时，默认走 **systemd**（`OPENCLAW_GATEWAY_SERVICE`）；无 systemd 时请配置 **`OPENCLAW_GATEWAY_RESTART_COMMAND`**

---

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENCLAW_HOME` | OpenClaw 数据根目录（其下为 `openclaw.json`、`agents/` 等）；与 `OPENCLAW_CONFIG_PATH` 二选一即可，**显式配置路径优先** | 未设置则用 `~/.openclaw` 推导默认 `openclaw.json` |
| `OPENCLAW_CONFIG_PATH` | `openclaw.json` 绝对路径 | `OPENCLAW_HOME/openclaw.json` 或 `~/.openclaw/openclaw.json` |
| `OPENCLAW_SESSIONS_PATH` | `sessions.json` 绝对路径 | **自动**：`<openclaw.json 所在目录>/agents/main/sessions/sessions.json` |
| `OPENCLAW_GATEWAY_SERVICE` | systemd 服务名（仅在未设 `OPENCLAW_GATEWAY_RESTART_COMMAND` 时用于重启与状态） | `openclaw-gateway.service` |
| `OPENCLAW_GATEWAY_RESTART_COMMAND` | **非 systemd** 或 **Docker 内重启宿主机网关** 时：由 shell 执行的一条命令（如 `sudo systemctl restart openclaw-gateway.service`、`docker restart 容器名`） | 未设置则用 `systemctl restart` |
| `OPENCLAW_GATEWAY_HEALTH_URL` | 网关 HTTP(S) 地址，用于**状态与自定义重启后的就绪探测**（标准库 `urllib`，镜像内无需 curl） | 未设置则尝试 `systemctl is-active`，再回退 `ss` 匹配端口 |
| `OPENCLAW_GATEWAY_SS_MARKERS` | 无健康 URL 且无法 `systemctl` 时，在 `ss -ltn` 输出中匹配的子串，逗号分隔 | `127.0.0.1:18789,[::1]:18789` |
| `OPENCLAW_MODEL_ADMIN_HOST` | 监听地址 | `0.0.0.0` |
| `OPENCLAW_MODEL_ADMIN_PORT` | 端口 | `8765` |
| `OPENCLAW_MODEL_ADMIN_PREFS_PATH` | 面板自用偏好（推理展示等） | 项目目录下 `admin-prefs.json` |
| `OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE` | 设为 `1`/`true`/`yes`/`on` 时**跳过** `openclaw config validate`（仅建议无 CLI 的测试环境） | 未设置（不跳过） |

---

## 困难与对策（部署前扫一眼）

| 困难 | 对策 |
|------|------|
| 机器上没有 **git** | `USE_GIT=0`：用 GitHub 源码包 + `tar`（需 **curl 或 wget**）；有 **rsync** 时同步更稳 |
| 网关数据不在 `~/.openclaw` | 设置 **`OPENCLAW_HOME`**（推荐）或 **`OPENCLAW_CONFIG_PATH`**；可写入安装目录 **`.env`**，`start.sh` 会自动加载 |
| 面板用户与网关用户不一致 | 用**同一 Unix 用户**运行面板（或 systemd 里 `User=` 与网关一致）；否则读写的是另一份主目录 |
| 没有 **`openclaw` CLI** | 安装 CLI，或 **`OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1`**（生产自行评估） |
| 没有 **systemd** / 「重启网关」无效 | 设置 **`OPENCLAW_GATEWAY_RESTART_COMMAND`**（由面板进程用户执行）；若需验证恢复，同时设 **`OPENCLAW_GATEWAY_HEALTH_URL`**（自定义重启时强烈建议） |
| 网关监听**非默认端口** | 设置 **`OPENCLAW_GATEWAY_HEALTH_URL`**，或 **`OPENCLAW_GATEWAY_SS_MARKERS`** 为 `127.0.0.1:你的端口` 等子串 |
| **Docker** 跑面板、网关在宿主机 | 挂载**整目录** `.openclaw`；一般 **`SKIP_VALIDATE=1`**；重启命令可用 **`OPENCLAW_GATEWAY_RESTART_COMMAND`** 调宿主机（需挂载 socket 或 SSH，依你的编排而定） |
| 端口 **8765** 被占用 | **`OPENCLAW_MODEL_ADMIN_PORT`** 改为空闲端口 |
| `curl \| bash` 环境变量不生效 | 用 `bash -s` 并前置导出：`curl ... \| OPENCLAW_HOME=/var/lib/openclaw bash -s` 或先写入安装目录 `.env` 再 `./start.sh` |

---

## 一键安装并启动（推荐）

本机需 **Python 3.10+**。默认用 **git** 克隆；若未安装 git，见下一节 **`USE_GIT=0`**。**不必**手动填 `sessions` 路径：程序按 `openclaw.json` 所在目录推导 `agents/main/sessions/sessions.json`。

```bash
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | bash
```

**无 git**（仅需 curl/wget + tar）：

```bash
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | USE_GIT=0 bash
```

自定义安装目录、fork 仓库、或指定 OpenClaw 数据根目录：

```bash
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | INSTALL_DIR=~/my-admin REPO=你的用户名/openclaw-model-admin bash
# 或指定数据根目录（等价于 openclaw.json 在 $OPENCLAW_HOME/openclaw.json）：
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | OPENCLAW_HOME=/你的/.openclaw bash
```

安装脚本会：**检查 Python 版本**、**提示**未找到的配置文件与 `openclaw` CLI（不强制中断，便于先装面板再补环境）。`SKIP_OPENCLAW_CHECK=1` 可关闭 CLI 提示。

---

## 快速开始（手动克隆）

```bash
git clone https://github.com/LuTianTian001/openclaw-model-admin.git
cd openclaw-model-admin

# 仅当 openclaw.json 不在 ~/.openclaw/ 时才需要：
# export OPENCLAW_CONFIG_PATH="/你的路径/openclaw.json"
# （sessions 会自动跟配置文件目录走，一般不必再设 OPENCLAW_SESSIONS_PATH）

chmod +x start.sh
./start.sh
```

浏览器打开：`http://127.0.0.1:8765`（监听 `0.0.0.0` 时可用本机局域网 IP）。

### 作为 systemd 服务常驻（可选）

仓库内 **`openclaw-model-admin.service.example`**：复制后修改 `User=`、`WorkingDirectory=`、`OPENCLAW_HOME` 等，再 `systemctl enable --now`。与网关**同一用户**、同一 `OPENCLAW_HOME` 最关键。

---

## 使用 Docker（可选，Linux 宿主机）

镜像内**默认没有** `openclaw` CLI；若不做校验，可设 `OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1`（**生产环境请自行评估风险**）。

```bash
cp docker-compose.example.yml docker-compose.yml
# 将 volumes 里的 /path/on/host/.openclaw 换成你宿主机上的 OpenClaw 数据目录（整个文件夹挂载）
docker compose up --build
```

容器内通常没有 `openclaw` CLI，故示例中保留 **`OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE`** 说明。若网关跑在宿主机且希望面板里点「重启」，需在 compose 里配置 **`OPENCLAW_GATEWAY_RESTART_COMMAND`**（例如挂载 `docker.sock` 后 `docker restart …`，或 `ssh` 到宿主机执行 `systemctl`——具体取决于你的安全模型），并设置 **`OPENCLAW_GATEWAY_HEALTH_URL`** 指向宿主机可达的网关地址（勿用 `127.0.0.1` 指容器自身）。

---

## 推送到 GitHub（中文说明即本 README）

1. 在 GitHub 新建**空仓库**（不要勾选添加 README，避免冲突）。
2. 本地：

```bash
cd openclaw-model-admin
git init
git add server.py static/ README.md .gitignore .env.example Dockerfile docker-compose.example.yml start.sh _test_*.py
git commit -m "初始提交：OpenClaw 模型管理面板"
git branch -M main
git remote add origin https://github.com/<你的用户名>/<仓库名>.git
```

3. 认证（**二选一，不要提交 token 到文件**）：
   - **SSH**：`git remote set-url origin git@github.com:<用户>/<仓库>.git`，并配置本机 SSH 公钥；
   - **HTTPS**：使用 `gh auth login`（GitHub CLI）或凭据管理器，**勿**把 `ghp_` 令牌写进仓库。

```bash
git push -u origin main
```

在 GitHub 仓库页面可补充：**仓库描述**、**About** 里的网站/文档链接；本 `README.md` 即为面向中文用户的说明。

---

## 开发与测试

```bash
python3 -m py_compile server.py
# 可选集成测试（会改本地配置时请谨慎）
# python3 _test_frontend_five.py
```

---

## 许可与免责

本工具直接修改 `openclaw.json` 与 `sessions.json`，使用前请自行备份。与 OpenClaw 网关、CLI 行为以你本机安装版本为准。**官方适配与说明均以 Linux 为准**，其他操作系统不在支持范围内。
