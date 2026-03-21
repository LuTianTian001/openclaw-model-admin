# OpenClaw Model Admin

本仓库为 **Linux 上**与 **OpenClaw** 网关配套的 **Web 管理面板**：在浏览器里改默认模型与 fallback、模型库、`openclaw.json` 里各模型的思考参数，并读写 `sessions.json` 与会话侧策略对齐。**依赖**：Python 3.10+、本机 **`openclaw` CLI**（保存前 `config validate`）、与网关**同一 Unix 用户**与同一套数据目录（默认 `~/.openclaw`）。

**仅 GNU/Linux**；脚本为 `bash`，网关状态/端口回退依赖 **`systemctl`、`ss`**（无 systemd 时用环境变量里的自定义重启命令，见下表）。

### 详细操作手册（部署、Docker、systemd、FAQ）

一键能覆盖的由 **`install.sh` / `start.sh`** 处理；**一键做不到的**（用户权限、防火墙、Docker 里访问宿主机、自定义重启命令等）在手册里写了**原因与具体处理步骤**：请阅读 **[操作手册.md](./操作手册.md)**。联调 PocketClaw / 网关白名单 / 会话模型覆盖等实录见 **[踩坑记录.md](./踩坑记录.md)**。

---

## 安全

- 勿在公开渠道粘贴 **GitHub Token** 或 API 密钥；泄露须立即在 GitHub 后台撤销。

---

## 功能概览

- 路由：默认模型、fallback、主模型思考（`agents.defaults.models[<ref>].params.thinking`）
- 全局：推理展示、提权、`applyToSessions`；与电报私聊对齐非思考项到网页主会话（保留网页 `modelOverride`）；可清全会话 `thinkingLevel`
- 模型库：按供应商管理
- 保存前 `openclaw config validate`；「重启网关」默认 `systemctl restart`（可改）

---

## 安装并启动

### 人类：一键命令（仅此一条）

```bash
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | bash
```

无 **git**：`curl …/install.sh | USE_GIT=0 bash`。装完终端会提示 **`操作手册.md`** 路径；排障见 **[操作手册.md](./操作手册.md)**。

### 智能体：安装 / 排障短提示词

把下面代码框内**整段**粘贴到 **OpenClaw（或助手）**，**下一行**写报错或现象；**勿**贴 Token、密钥。与 **[给OpenClaw的部署排障提示词.md](./给OpenClaw的部署排障提示词.md)**「智能体用」正文一致（改一处请两处同步）。

```
任务：协助安装或排查 OpenClaw Model Admin（仅 GNU/Linux）。

人类安装命令（让用户在本机 Linux 执行）：
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | bash
无 git：curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | USE_GIT=0 bash
装完默认 ~/openclaw-model-admin；数据不在 ~/.openclaw 则在安装目录 .env 设 OPENCLAW_HOME 或 OPENCLAW_CONFIG_PATH（start.sh 会加载 .env）。

项目要点：Python≥3.10；面板须读写网关正在用的那份 openclaw.json（与网关同 Unix 用户或 .env 显式路径）；保存会跑 openclaw config validate，无 CLI 可设 OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1；重启网关默认 systemctl，否则 OPENCLAW_GATEWAY_RESTART_COMMAND；Docker 里访问宿主机网关勿用 127.0.0.1。

请你先用只读命令确认 uname -s、python3 版本、whoami、openclaw.json 路径与权限、是否在容器、command -v openclaw、面板端口默认 8765、systemctl/ss，再给可执行修复步骤；不要索要密钥。细则：https://github.com/LuTianTian001/openclaw-model-admin/blob/main/操作手册.md

用户现象：
```

数据不在 `~/.openclaw`：在安装目录编辑 **`.env`**（`start.sh` 会自动加载；一键安装若检测到无 `.env` 会从 **`.env.example`** 复制一份模板）。变量说明见下表、**`.env.example`** 与 **操作手册 §5、§10**。

手动：

```bash
git clone https://github.com/LuTianTian001/openclaw-model-admin.git
cd openclaw-model-admin && chmod +x start.sh && ./start.sh
```

浏览器：`http://127.0.0.1:8765`（默认监听 `0.0.0.0`）。

**常见坑（一键解决不了，见操作手册有步骤）**：① 面板用户与网关用户不一致 → 读写错目录；② Docker 里把网关填成 `127.0.0.1` → 应填宿主机/服务名可达地址；③ 容器内无 `openclaw` → 需 `SKIP_VALIDATE` 或镜像内装 CLI。

**systemd 常驻**：参考 **`openclaw-model-admin.service.example`** 与 **操作手册 §8**。

---

## 环境变量

| 变量 | 说明 | 默认 |
|------|------|------|
| `OPENCLAW_HOME` | 数据根目录（含 `openclaw.json`） | `~/.openclaw` |
| `OPENCLAW_CONFIG_PATH` | `openclaw.json` 绝对路径 | `$OPENCLAW_HOME/openclaw.json` |
| `OPENCLAW_SESSIONS_PATH` | `sessions.json` | 随配置目录推导 `.../agents/main/sessions/sessions.json` |
| `OPENCLAW_GATEWAY_SERVICE` | systemd 单元名 | `openclaw-gateway.service` |
| `OPENCLAW_GATEWAY_RESTART_COMMAND` | 替代 `systemctl restart` 的一条 shell 命令 | 未设则用 systemd |
| `OPENCLAW_GATEWAY_HEALTH_URL` | 网关 HTTP(S)，用于在线判定与自定义重启后探测 | 未设则 `systemctl` / `ss` |
| `OPENCLAW_GATEWAY_SS_MARKERS` | `ss -ltn` 输出中要匹配的子串，逗号分隔 | `127.0.0.1:18789,[::1]:18789` |
| `OPENCLAW_MODEL_ADMIN_HOST` / `PORT` | 面板监听 | `0.0.0.0` / `8765` |
| `OPENCLAW_MODEL_ADMIN_PREFS_PATH` | 面板偏好文件 | 项目目录 `admin-prefs.json` |
| `OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE` | `1` 跳过 CLI 校验（无 `openclaw` 时） | 不跳过 |
| `OPENCLAW_MODEL_ADMIN_DISABLE_STATE_VALIDATE_CACHE` | `1` 时每次打开页面都重新跑 `openclaw config validate`（调试用，**明显变慢**） | 不设置（默认按 `openclaw.json` 修改时间缓存校验结果） |

**页面加载**：刷新/二次进入时，后端会**缓存**对 `openclaw.json` 的 CLI 校验结果（文件未改则不再起子进程），首次进入或刚保存配置后仍会校验一次。

---

## Docker（Linux 宿主机）

```bash
cp docker-compose.example.yml docker-compose.yml
# 修改 volumes：宿主机 OpenClaw 数据目录 -> 容器内挂载点
docker compose up --build
```

容器内一般无 `openclaw`，示例默认 **`OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1`**。网关不在同一网络命名空间时，用 **`OPENCLAW_GATEWAY_HEALTH_URL`** 指向**宿主机可达**地址；需面板内重启时配 **`OPENCLAW_GATEWAY_RESTART_COMMAND`**（与当前 compose/权限一致即可）。

---

## 开发

```bash
python3 -m py_compile server.py
```

---

## 说明

直接改 `openclaw.json` 与 `sessions.json`，用前自行备份；行为以本机 OpenClaw 版本为准。
