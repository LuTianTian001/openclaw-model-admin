# 给智能体的短提示词（OpenClaw Model Admin）

- **人类自己装**：只用 README **一条** `curl … | bash`。
- **给 OpenClaw / 助手**：复制下面 **「智能体用」** 从「任务：」到「用户现象：」的**全部文字**（约 15 行），下一行写现象；**勿**贴 Token、密钥。

---

## 智能体用（从下一行复制到「用户现象：」）

任务：协助安装或排查 OpenClaw Model Admin（仅 GNU/Linux）。

人类安装命令（让用户在本机 Linux 执行）：
curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | bash
无 git：curl -fsSL https://raw.githubusercontent.com/LuTianTian001/openclaw-model-admin/main/install.sh | USE_GIT=0 bash
装完默认 ~/openclaw-model-admin；数据不在 ~/.openclaw 则在安装目录 .env 设 OPENCLAW_HOME 或 OPENCLAW_CONFIG_PATH（start.sh 会加载 .env）。

项目要点：Python≥3.10；面板须读写网关正在用的那份 openclaw.json（与网关同 Unix 用户或 .env 显式路径）；保存会跑 openclaw config validate，无 CLI 可设 OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE=1；重启网关默认 systemctl，否则 OPENCLAW_GATEWAY_RESTART_COMMAND；Docker 里访问宿主机网关勿用 127.0.0.1。

请你先用只读命令确认 uname -s、python3 版本、whoami、openclaw.json 路径与权限、是否在容器、command -v openclaw、面板端口默认 8765、systemctl/ss，再给可执行修复步骤；不要索要密钥。细则：https://github.com/LuTianTian001/openclaw-model-admin/blob/main/操作手册.md

用户现象：

---

（正文与 `README.md` 安装节「智能体」代码块一致，修改时请同步。）
