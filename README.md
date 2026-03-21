# OpenClaw Model Admin

面向 **OpenClaw** 网关生态的**本地 Web 管理面板**：在浏览器里调整默认模型与 fallback、模型库、`openclaw.json` 中的模型思考参数，并读写 `sessions.json` 做会话侧同步（与网关 `/status` 解析一致）。**纯 Python 标准库 + 本机 `openclaw` CLI（用于配置校验）**。

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

## 环境要求

- **Python 3.10+**（推荐 3.12）
- 本机已安装 **`openclaw`** 且可在 PATH 中执行（用于保存配置前的校验）
- 已存在 OpenClaw 数据目录（默认当前运行用户的 `~/.openclaw/`，内含 `openclaw.json`；**请用与网关相同的用户**运行本面板，以便读写同一份配置与会话）
- Linux 上若使用「重启网关」按钮，需有 **systemd** 及对应单元（默认可通过环境变量改名）

---

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENCLAW_CONFIG_PATH` | `openclaw.json` 绝对路径 | `~/.openclaw/openclaw.json` |
| `OPENCLAW_SESSIONS_PATH` | `sessions.json` 绝对路径 | **自动**：`<openclaw.json 所在目录>/agents/main/sessions/sessions.json`（与仅改 `OPENCLAW_CONFIG_PATH` 时联动） |
| `OPENCLAW_GATEWAY_SERVICE` | systemd 服务名 | `openclaw-gateway.service` |
| `OPENCLAW_MODEL_ADMIN_HOST` | 监听地址 | `0.0.0.0` |
| `OPENCLAW_MODEL_ADMIN_PORT` | 端口 | `8765` |
| `OPENCLAW_MODEL_ADMIN_PREFS_PATH` | 面板自用偏好（推理展示等） | 项目目录下 `admin-prefs.json` |
| `OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE` | 设为 `1`/`true`/`yes`/`on` 时**跳过** `openclaw config validate`（仅建议无 CLI 的测试环境） | 未设置（不跳过） |

---

## 一键安装并启动（推荐）

本机需已安装 **git**、**Python 3.10+**，且已按 OpenClaw 官方方式初始化过 **`~/.openclaw/`**（含 `openclaw.json`）。**不必**手动填 `sessions` 路径：只要 `openclaw.json` 在默认位置，程序会自动使用同目录下的 `agents/main/sessions/sessions.json`。

```bash
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | bash
```

自定义安装目录或 fork 仓库：

```bash
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | INSTALL_DIR=~/my-admin REPO=你的用户名/openclaw-model-admin bash
```

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

---

## 使用 Docker（可选）

镜像内**默认没有** `openclaw` CLI；若不做校验，可设 `OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1`（**生产环境请自行评估风险**）。

```bash
cp docker-compose.example.yml docker-compose.yml
# 将 volumes 里的 /path/on/host/.openclaw 换成你宿主机上的 OpenClaw 数据目录（整个文件夹挂载）
docker compose up --build
```

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

本工具直接修改 `openclaw.json` 与 `sessions.json`，使用前请自行备份。与 OpenClaw 网关、CLI 行为以你本机安装版本为准。
