# 管理端前后端对照笔记本

> 目的：改 `server.py` 或 `static/index.html` 时**按表核对**，避免只改一端。  
> 维护约定：新增/删除 API、改 `build_state` 字段、改 POST body 时**同步更新本文件**。

---

## 一、改动必查清单（按顺序勾）

| 步骤 | 内容 |
|------|------|
| 1 | `server.py`：`do_GET` / `do_POST` 路由与 `_send_json` 形状 |
| 2 | `static/index.html`：`apiReq(` / `fetch(` 所有调用点 |
| 3 | `build_state()` 返回字段 ↔ `renderFromState` / 其它读 `state.` 的脚本 |
| 4 | 本文件「API 总表」「state 字段表」 |
| 5 | `CODEX-MEMORY-SNAPSHOT.md`（若用户可见行为或 API 语义变了） |
| 6 | `_test_*.py` / 手工：`python3 -m py_compile server.py` |

---

## 二、HTTP API 总表（与前端一一对应）

约定：多数变更接口为 **POST** + `Content-Type: application/json`；另有只读 **GET**：`/api/state`、`/api/gateway/logs`、`/api/usage/snapshot`、`/api/openclaw/version-check`。  
前端封装：`apiReq(path, body)` — `body` 省略时发 **GET**；日志/用量、OpenClaw 版本检查目前用 `fetch` 直 GET。

| 方法 | 路径 | 前端调用处 | 请求体要点 | 成功响应要点 |
|------|------|------------|------------|--------------|
| GET | `/` | 浏览器 | — | `index.html` |
| GET | `/api/state` | `refresh()` → `apiReq('/api/state')` | — | `{ ok, state }`；`state` 见第三节 |
| GET | `/api/gateway/logs?lines=` | `loadGatewayLogs()` → `fetch` | `lines` 默认 200，范围约 50–2000 | `{ ok, text, lines[], lineCount, source?, service?, path?, error? }`；失败时 `ok:false` + `error` |
| GET | `/api/usage/snapshot` | `loadUsageSnapshot()`：`refresh`、展开、点档、**轮询**；轮询每 tick **并行** `days=1,7,30`（与后台三槽一致），`usageCachedAt` 按档记录，仅**当前选中档**有变时 `renderUsageBody` | 同上；**`force`** 仍支持，UI 不用 | 前端 **`OCMA_USAGE_POLL_MS`** 默认 300000，须与 **`OPENCLAW_ADMIN_USAGE_BG_INTERVAL_SEC`**（默认 300）人工对齐。页载 `ensureUsagePollStarted`。`OPENCLAW_ADMIN_USAGE_BG_DISABLE=1` 关后台。另：`OPENCLAW_ADMIN_USAGE_GATEWAY=0`、`OPENCLAW_ADMIN_USAGE_GATEWAY_RETRIES` |
| GET | `/api/backup/list` | `loadBackupListFromApi()` → `fetch` | — | `{ ok, backups[], backupDir, retentionDays, intervalSec }` |
| GET | `/api/openclaw/version-check` | `fetchOpenclawVersionCheck()` → `fetch` | query：`force=0|1`；`1` 时跳过服务端 npm 缓存 | `{ ok, currentVersion?, currentError?, latestVersion?, latestError?, checkedAt, fromCache, compare, updateAvailable, isLatest }` |
| POST | `/api/openclaw/update` | `runOpenclawUpdateClick()` → `apiReq` | 可选 `noRestart: true` | `{ ok, result?, error?, stderr?, exitCode? }`；成功时 `result` 为 CLI JSON；**无 `state`** |
| POST | `/api/backup/create` | `backupNowClick()` | `reason?` | `{ ok, meta.adminBackup }`（`id`, `path`, `pruned`, `hasConfig`, `hasSessions`…） |
| POST | `/api/backup/restore` | `confirmRestoreBackup()` | `id`（目录名如 `20260323_153022`） | `{ ok, state, meta.adminRestore }`；会先 `pre-restore` 再写入并 `validate` |
| POST | `/api/selection` | `applyFullDeploy()` ← `saveSelection()`、会话区按钮 | `primary`, `fallbacks[]`, `elevatedDefault`, `reasoningDisplay`, `applyToSessions`, `clearModelOverrides`；**可选**后端支持但当前 UI 未发：`primaryThinkingEnabled`, `primaryThinkingValue`, `sessionKey` | `{ ok, state, meta }`；`meta` 含 `migrations`, `sessionSync`, `sessionThinkingCleared`, `sessionContextSync`；**已无** `webTelegramSync` |
| POST | `/api/session/model-override` | `pushAllSessionRowsFromUi()`、**跟随全局**前置清本条 | `sessionKey`；`modelRef` 或 `clear: true` | `{ ok, state, meta.sessionModelOverride }` |
| POST | `/api/session/behavior` | `pushAllSessionRowsFromUi()`、**跟随全局**前置清本条 | `sessionKey`；`reasoningLevel` / `elevatedLevel` 可为 `null` 清键 | `{ ok, state, meta.sessionBehavior }` |
| POST | `/api/model` | `addModel()` | `provider`, `baseUrl`, `apiKey`, `auth`, `api`, `modelId`, `modelName`, `thinkingEnabled`, `thinkingValue`, `inputs`, `contextWindow`, `maxTokens` | `{ ok, state, meta }`（`sessionContextSync`, `sessionThinkingCleared`, `migrations?`） |
| POST | `/api/model/delete` | `deleteModel()` | `ref`；前端另发 `deleteProviderIfEmpty: true`（**后端当前忽略**，行为已是「无模型则删供应商块」） | `{ ok, state, meta }` |
| POST | `/api/model/test`（及别名 `/api/model/ttft`） | 模型库测速、`batchTestProviderModels` | `ref` | **无 `state`**：`{ ok, seconds?, error?, ... }`；前端不得假设必有 `state` |
| POST | `/api/provider/fetch-models` | 模型库「获取模型列表」：拉**全量远端 id**预览（不写配置） | `provider` | `{ ok, meta.providerFetchModels }` **无 `state`**：`remoteIds`（最多 1000 条展示）、`remoteIdsTruncated`、`inConfigRemoteIds`（默认勾选）、`remoteCount`、`localOnlyCount`、`message` 等 |
| POST | `/api/provider/sync-remote-models` | 弹层「应用同步」 | `provider`, `remoteIds[]`（与弹窗一致的作用域）, `selectedIds[]`（可空，空则移除作用域内全部） | `{ ok, state, meta.providerSyncRemoteModels }`（`added`/`removed`、`addedCount`/`removedCount`；未改可为 `message: 无变更`） |
| POST | `/api/provider/add-models` | 保留：仅批量追加 id（无弹窗时用） | `provider`, `ids[]` | `{ ok, state, meta.providerAddModels }` |
| POST | `/api/provider/delete` | `deleteWholeProvider()` | `provider` | `{ ok, state, meta }`（大量清理字段） |
| POST | `/api/probe` | `runProbe()` | `{}` | `{ ok, timestamp, checks[], summary, results }` |
| POST | `/api/restart` | `applyFullDeploy()`、`addModel` / `deleteModel` / `deleteWholeProvider` / `restartService()` | `{}` | `{ ok, state }` |

### 已移除（勿再引用）

- `POST /api/sync-web-from-telegram` — 已删除；无前端、无「应用」时自动对齐电报→网页主会话。
- `meta.webTelegramSync` — 已删除。

### 后端曾存在、前端未使用（已删路由以免遗漏）

- ~~`POST /api/session-align`~~ — 与 `GET /api/state` 语义重复，已从 `server.py` 移除。

---

## 三、`build_state()` → 前端 `state` 字段

| 字段 | 前端主要使用处 | 说明 |
|------|----------------|------|
| `panelMeta.version`, `panelMeta.sessionsPath`, `panelMeta.openclawCliVersion` | 本机信息折叠区 | `openclawCliVersion` 来自 `openclaw -V`（短时缓存） |
| `configPath` | 同上 | |
| `primary`, `fallbacks` | 路由下拉、`populate` | |
| `reasoningDisplay`, `elevatedDefault` | 全局选项 select | 推理展示存管理端偏好，非仅 openclaw schema |
| `gatewayActive`, `alerts`, `configValid`, `configIssues` | 顶栏告警、`renderFromState` | |
| `sessionPreviews` | 运行状态多会话卡片 | 项内含 `sessionKey`, `previewTitle`, `modelRef`, `viaOverride`, `statusThink`, `sessionReasoningLevel`, `sessionElevatedLevel` 等（见 `_session_entry_to_preview`） |
| `activeChatSession` | `sessionPreviews` 空时兜底预览 | |
| `providers`, `models` | 统计、模型库、`groupedModels`, `populate` | 每项形状见 `build_state` |
| `mainSessionRoute` | 诊断 `main_session_model`：只展示说明文案；**会话内单独选模型与全局不同不算故障**，该项恒 `ok: true` | |
| `alignmentHints` | 若前端展示提示 | 当前可能未用，保留兼容 |

**改 `build_state` 或 `_session_entry_to_preview` 时**：必搜 `state.` / `sessionPreviews` / `activeChatSession`。

---

## 四、前端专用约定（易踩坑）

1. **Tab 切换**：使用 `.tab-nav .tab-btn[data-tab="…"]`，**禁止**再用 `[onclick*="tab-xxx"]`（模型路径可能含子串，导致 Tab 失灵）。
2. **`/api/model/test`**、**`/api/openclaw/update`**：响应无 `state`，与 `/api/selection` 等不同。
3. **`apiReq`**：`!r.ok || d.ok === false` 即抛错；依赖 `d.state` 的调用需在成功后检查 `d.state` 再 `renderFromState`。
4. **运行状态会话行**：`#runtimeInfo` 每次 `innerHTML` 重建后依赖 `refreshSessionModelRows` + `bindRuntimeSessionModelOverride`（`dataset.sessionModelBound` 在容器上，不需重绑）。
5. **内联 `onclick` 与模型 ref**：ref 若含 `'` 会破坏 HTML；长期应改为 `data-*` + 委托（已知风险，记入踩坑记录）。

---

## 五、服务与缓存

- 改静态页后：`systemctl restart openclaw-model-admin`（或等价进程重启）；`index.html` 已 `Cache-Control: no-store`。
- `GET /api/state` 内 `openclaw config validate` 可能缓存（见 `server.py` 注释与环境变量）。

---

## 六、修订记录（人工追加）

| 日期 | 变更摘要 |
|------|----------|
| 2026-03-23 | 初版：API 总表、state 字段、Tab/data-tab 约定、移除电报同步与 `webTelegramSync`、删除未使用 `/api/session-align` |
| 2026-03-23 | `applyFullDeploy`：`/api/selection` → `pushAllSessionRowsFromUi`（按界面写各会话）→ `/api/restart`；顶栏「应用」与会话「写入会话」「跟随全局」（跟随先清本条再 refresh）共用 |
| 2026-03-23 | `fetch-models` 预览全量远端 id；`sync-remote-models` 按勾选增删（未在列表中的本地模型不动）；`add-models` 仍可选 |
| 2026-03-23 | 路由 Tab：`GET /api/gateway/logs` + `GET /api/usage/snapshot`；`OPENCLAW_GATEWAY_LOG_FILE` 兜底 |
| 2026-03-23 | 首页折叠顺序（各 Tab 下方统一）：**使用情况** → **备份与恢复** → **网关日志** → **本机信息**（原网关/本机在路由 Tab 内，已移出） |
| 2026-03-23 | 用量区审计：`#homeUsageBody` **事件委托**切换天数（避免每次 `render` 重复绑定）；去掉无效 `skipCache` / 冗余 `usageToolbarHighlightDays`；`OCMA_USAGE_LIMIT` / `OCMA_USAGE_POLL_MS` 常量 |
| 2026-03-23 | 首页折叠「备份与恢复」：`POST /api/backup/create`、`/api/backup/restore`，`GET /api/backup/list`；进程内定时备份（默认 3600s、保留 7 天） |
| 2026-03-23 | **v1.2.0**：`GET /api/openclaw/version-check`、`POST /api/openclaw/update`；`panelMeta.openclawCliVersion`；本机信息区 12h 轮询 + 展开强制检查；详见 `CHANGELOG.md` |
