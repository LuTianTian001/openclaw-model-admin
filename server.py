#!/usr/bin/env python3
import copy
import json
import os
import shutil
import subprocess
import time
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"


def _path_from_env(var_name: str, default: Path) -> Path:
    raw = os.environ.get(var_name, "").strip()
    if not raw:
        return default
    return Path(raw).expanduser()


# 默认与官方 OpenClaw 一致：~/.openclaw/openclaw.json；未显式指定时会话库随配置文件目录推导（agents/main/sessions/sessions.json）
def _default_openclaw_config_path() -> Path:
    """默认 openclaw.json：OPENCLAW_HOME 优先，否则 ~/.openclaw/openclaw.json。"""
    raw = os.environ.get("OPENCLAW_HOME", "").strip()
    if raw:
        return Path(raw).expanduser() / "openclaw.json"
    return Path.home() / ".openclaw" / "openclaw.json"


_DEFAULT_CONFIG = _default_openclaw_config_path()
CONFIG_PATH = _path_from_env("OPENCLAW_CONFIG_PATH", _DEFAULT_CONFIG)


def _default_sessions_path_for_config(config_path: Path) -> Path:
    return config_path.parent / "agents" / "main" / "sessions" / "sessions.json"


SESSION_STORE_PATH = _path_from_env(
    "OPENCLAW_SESSIONS_PATH",
    _default_sessions_path_for_config(CONFIG_PATH),
)

# GET /api/state 每次跑 openclaw config validate 很慢（常 1～3s+）；按 openclaw.json 的 mtime 缓存结果。保存成功后会 prime 缓存避免紧接着再跑 CLI。
_CLI_VALIDATE_CACHE: dict = {"key": None, "result": None}

# build_state 内只读一次 sessions.json，避免同一请求内多次 parse 大文件
_MISSING_SESSION_SNAPSHOT = object()

SERVICE_NAME = os.environ.get("OPENCLAW_GATEWAY_SERVICE", "openclaw-gateway.service").strip() or "openclaw-gateway.service"
HOST = os.environ.get("OPENCLAW_MODEL_ADMIN_HOST", "0.0.0.0")
PORT = int(os.environ.get("OPENCLAW_MODEL_ADMIN_PORT", "8765"))
MAIN_SESSION_KEY = "agent:main:main"

BUILTIN_PROVIDERS = ["openai-codex", "github-copilot"]


def normalize_provider_id(name: str) -> str:
    """自定义供应商在配置中必须小写；与内置列表比对时忽略大小写。"""
    s = (name or "").strip() if isinstance(name, str) else ""
    if not s:
        return s
    low = s.lower()
    if low in BUILTIN_PROVIDERS:
        return low
    return low


def normalize_model_ref_provider_lower(ref: str) -> str:
    """将 provider/model 中供应商段转为规范形式（内置归一为小写名，自定义为小写）。"""
    if not isinstance(ref, str) or "/" not in ref:
        return ref
    p, _, mid = ref.partition("/")
    mid = mid.strip()
    p = normalize_provider_id(p)
    if not p or not mid:
        return ref
    return f"{p}/{mid}"


def resolve_provider_key_in_provs(provs: object, p_name: str) -> str | None:
    """在 models.providers 中解析供应商键（兼容尚未迁移完的大小写）。"""
    if not isinstance(provs, dict) or not isinstance(p_name, str):
        return None
    want = normalize_provider_id(p_name.strip())
    if not want:
        return None
    if want in provs:
        return want
    for k in provs:
        if isinstance(k, str) and k.strip().lower() == want:
            return k
    return None


# 删除自定义供应商时：绝不自动删除这些文件名（OpenClaw 自带渠道等系统凭据）
PROTECTED_CREDENTIAL_BASENAMES = frozenset(
    {
        "telegram-bot-token",
    }
)
ALLOWED_MODEL_ENTRY_KEYS = {"alias", "params", "streaming"}
THINKING_PARAM_MAX_LEN = 64
ADMIN_PREFS_PATH = _path_from_env("OPENCLAW_MODEL_ADMIN_PREFS_PATH", ROOT / "admin-prefs.json")

def read_admin_prefs():
    """管理后台自用偏好（openclaw.json schema 不含会话级推理展示默认值）。"""
    default = {"reasoningDisplay": "off"}
    if not ADMIN_PREFS_PATH.exists():
        return dict(default)
    try:
        raw = json.loads(ADMIN_PREFS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return dict(default)
        rv = raw.get("reasoningDisplay", "off")
        if rv not in ("on", "off"):
            rv = "off"
        return {"reasoningDisplay": rv}
    except Exception:
        return dict(default)

def write_admin_prefs(**kwargs):
    cur = read_admin_prefs()
    for k, v in kwargs.items():
        if k == "reasoningDisplay" and v in ("on", "off"):
            cur[k] = v
    ADMIN_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ADMIN_PREFS_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return cur

def read_config():
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

def normalize_model_overrides(config):
    migrations = []
    defaults = config.setdefault("agents", {}).setdefault("defaults", {})
    models = defaults.get("models")
    if not isinstance(models, dict):
        return migrations

    for ref, raw_entry in list(models.items()):
        if not isinstance(raw_entry, dict):
            models[ref] = {}
            migrations.append(f"{ref}: reset non-object model config")
            continue

        entry = raw_entry
        raw_params = entry.get("params")
        params = raw_params if isinstance(raw_params, dict) else {}
        if "params" in entry and not isinstance(raw_params, dict):
            migrations.append(f"{ref}.params: reset non-object params")

        for key in list(entry.keys()):
            if key in ALLOWED_MODEL_ENTRY_KEYS:
                continue
            params[key] = entry.pop(key)
            migrations.append(f"{ref}.{key} -> params.{key}")

        if params:
            entry["params"] = params
        elif "params" in entry:
            entry.pop("params", None)

    return migrations

def migrate_reasoning_effort_off_model_definitions(config):
    """OpenClaw 的 models.providers.*.models[] 为 strict schema，不允许 reasoningEffort；迁入 agents.defaults.models.<ref>.params。"""
    migrations = []
    providers = config.get("models", {}).get("providers", {})
    if not isinstance(providers, dict):
        return migrations
    agents_models = config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
    if not isinstance(agents_models, dict):
        return migrations
    for p_name, p in providers.items():
        if not isinstance(p, dict):
            continue
        for m in p.get("models", []) or []:
            if not isinstance(m, dict) or "reasoningEffort" not in m:
                continue
            raw = m.pop("reasoningEffort", None)
            effort_str = raw.strip() if isinstance(raw, str) else (str(raw).strip() if raw is not None else "")
            mid = m.get("id")
            if not effort_str or not isinstance(mid, str) or not mid.strip():
                migrations.append(f"{p_name}/{mid}: 移除非法键 reasoningEffort（无有效值）")
                continue
            ref = f"{p_name}/{mid}"
            entry = agents_models.setdefault(ref, {})
            if not isinstance(entry, dict):
                entry = {}
                agents_models[ref] = entry
            pr = dict(entry.get("params") or {}) if isinstance(entry.get("params"), dict) else {}
            if "reasoningEffort" not in pr:
                pr["reasoningEffort"] = effort_str
                entry["params"] = pr
                migrations.append(f"{ref}: reasoningEffort 已迁入 agents.defaults.models.params")
            else:
                migrations.append(f"{ref}: 已忽略模型上的 reasoningEffort（params 中已有）")
    return migrations


def _lowercase_provider_segment_in_ref_keys_under(obj: object) -> int:
    """递归 dict：键名形如 provider/model 时，将供应商段规范为小写（自定义全小写，内置归一）。"""
    n = 0
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, dict):
                n += _lowercase_provider_segment_in_ref_keys_under(v)
            elif isinstance(v, list):
                for x in v:
                    n += _lowercase_provider_segment_in_ref_keys_under(x)
        for k in list(obj.keys()):
            if not isinstance(k, str) or "/" not in k or k not in obj:
                continue
            nk = normalize_model_ref_provider_lower(k)
            if nk == k:
                continue
            if nk in obj:
                del obj[k]
                n += 1
                continue
            obj[nk] = obj.pop(k)
            n += 1
    elif isinstance(obj, list):
        for x in obj:
            n += _lowercase_provider_segment_in_ref_keys_under(x)
    return n


def _lowercase_auth_profile_provider_keys(auth: dict) -> int:
    """auth.profiles 的键名中供应商段改为小写（如 VX001:foo -> vx001:foo）。"""
    profs = auth.get("profiles")
    if not isinstance(profs, dict):
        return 0
    n = 0
    for key in list(profs.keys()):
        if not isinstance(key, str):
            continue
        if ":" in key:
            left, sep, rest = key.partition(":")
            nl = normalize_provider_id(left)
            nk = nl + sep + rest if nl != left else key
        else:
            nl = normalize_provider_id(key)
            nk = nl if nl != key else key
        if nk == key or key not in profs:
            continue
        if nk in profs:
            del profs[key]
            n += 1
            continue
        profs[nk] = profs.pop(key)
        n += 1
    return n


def migrate_custom_provider_names_to_lowercase(config: dict) -> list[str]:
    """自定义供应商键、agents 内 ref 键、auth profile 键：供应商名一律小写（保存时自动迁移）。"""
    migrations: list[str] = []
    m = config.get("models")
    provs = m.get("providers") if isinstance(m, dict) else None
    if isinstance(provs, dict):
        for ok in list(provs.keys()):
            if not isinstance(ok, str) or not ok.strip():
                continue
            nk = ok.strip().lower()
            if nk in BUILTIN_PROVIDERS:
                continue
            if ok == nk:
                continue
            if nk in provs:
                migrations.append(
                    f"无法将 models.providers 的 {ok!r} 改为小写：已存在 {nk!r}，请手动合并后保存"
                )
                continue
            provs[nk] = provs.pop(ok)
            migrations.append(f"models.providers: {ok!r} -> {nk!r}")
    defs = (config.get("agents") or {}).get("defaults")
    if isinstance(defs, dict):
        am = defs.get("models")
        if isinstance(am, dict):
            new_am: dict = {}
            for ref, ent in list(am.items()):
                if not isinstance(ref, str):
                    new_am[ref] = ent
                    continue
                nref = normalize_model_ref_provider_lower(ref)
                if nref != ref:
                    migrations.append(f"agents.defaults.models: {ref!r} -> {nref!r}")
                if nref in new_am and isinstance(new_am[nref], dict) and isinstance(ent, dict):
                    merged = dict(new_am[nref])
                    merged.update(ent)
                    new_am[nref] = merged
                else:
                    new_am[nref] = ent
            defs["models"] = new_am
        mb = defs.get("model")
        if isinstance(mb, dict):
            pr = mb.get("primary")
            if isinstance(pr, str) and "/" in pr:
                npr = normalize_model_ref_provider_lower(pr)
                if npr != pr:
                    migrations.append(f"agents.defaults.model.primary: {pr!r} -> {npr!r}")
                    mb["primary"] = npr
            fbs = mb.get("fallbacks")
            if isinstance(fbs, list):
                nlist = []
                for x in fbs:
                    if isinstance(x, str) and "/" in x:
                        nx = normalize_model_ref_provider_lower(x)
                        if nx != x:
                            migrations.append(f"agents.defaults.model.fallback: {x!r} -> {nx!r}")
                        nlist.append(nx)
                    else:
                        nlist.append(x)
                mb["fallbacks"] = nlist
    ag = config.get("agents")
    if isinstance(ag, dict):
        c = _lowercase_provider_segment_in_ref_keys_under(ag)
        if c:
            migrations.append(f"agents 子树: {c} 处 ref 键已小写化供应商名")
    auth = config.get("auth")
    if isinstance(auth, dict):
        c = _lowercase_auth_profile_provider_keys(auth)
        if c:
            migrations.append(f"auth.profiles: {c} 个键已小写化供应商段")
    for path in _iter_agent_models_json_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        provs = data.get("providers")
        if not isinstance(provs, dict):
            continue
        touched = False
        for ok in list(provs.keys()):
            if not isinstance(ok, str) or not ok.strip():
                continue
            nk = ok.strip().lower()
            if nk in BUILTIN_PROVIDERS:
                continue
            if ok == nk:
                continue
            if nk in provs:
                migrations.append(f"无法小写化 {path.name} 中 {ok!r}：已存在 {nk!r}")
                continue
            provs[nk] = provs.pop(ok)
            touched = True
            migrations.append(f"{path.name}: models.providers {ok!r} -> {nk!r}")
        if touched:
            try:
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except OSError as e:
                migrations.append(f"{path.name} 写入失败: {e}")
    return migrations


def normalize_sessions_provider_overrides_lowercase() -> dict:
    """sessions.json：将非内置的 providerOverride 规范为小写，与会话外配置一致。"""
    if not SESSION_STORE_PATH.exists():
        return {"updated": 0, "exists": False}
    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"updated": 0, "exists": True, "error": "parse_failed"}
    if not isinstance(store, dict):
        return {"updated": 0, "exists": True, "error": "not_object"}
    updated = 0
    for raw in store.values():
        if not isinstance(raw, dict):
            continue
        po = raw.get("providerOverride")
        if not isinstance(po, str) or not po.strip():
            continue
        ps = po.strip()
        low = ps.lower()
        if low in BUILTIN_PROVIDERS:
            if ps != low:
                raw["providerOverride"] = low
                updated += 1
            continue
        if ps != low:
            raw["providerOverride"] = low
            updated += 1
    if updated > 0:
        try:
            SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return {"updated": 0, "exists": True, "error": "write_failed"}
    return {"updated": updated, "exists": True}


def write_config(config):
    migrations = migrate_custom_provider_names_to_lowercase(config)
    migrations.extend(normalize_model_overrides(config))
    migrations.extend(migrate_reasoning_effort_off_model_definitions(config))
    ag = config.get("agents")
    if isinstance(ag, dict):
        defs = ag.get("defaults")
        if isinstance(defs, dict):
            if defs.pop("thinkingDefault", None) is not None:
                migrations.append("agents.defaults.thinkingDefault 已移除（思考仅由各模型 params.thinking 决定）")
            am = defs.get("models")
            if isinstance(am, dict) and "/" in am:
                am.pop("/", None)
                migrations.append(
                    '已移除 agents.defaults.models 无效键 "/"（易干扰 provider/model 解析，导致 ciii 等 ref 与 /status 观感不一致）'
                )
    # 强制清理：禁止内置供应商出现在 providers 列表中，确保其走系统内置 Auth 逻辑
    if "models" in config and "providers" in config["models"]:
        config["models"]["providers"] = {k: v for k, v in config["models"]["providers"].items() if k.strip() and k not in BUILTIN_PROVIDERS}
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return migrations

def _cli_validate_cache_disabled() -> bool:
    return os.environ.get("OPENCLAW_MODEL_ADMIN_DISABLE_STATE_VALIDATE_CACHE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _cli_validate_cache_key():
    if not CONFIG_PATH.exists():
        return None
    try:
        return str(CONFIG_PATH.resolve()), CONFIG_PATH.stat().st_mtime_ns
    except OSError:
        return None


def _prime_cli_validate_cache(validation: dict) -> None:
    """保存成功后写入缓存，避免紧随其后的 GET /api/state 再 spawn openclaw。"""
    key = _cli_validate_cache_key()
    if key is None:
        return
    _CLI_VALIDATE_CACHE["key"] = key
    _CLI_VALIDATE_CACHE["result"] = copy.deepcopy(validation)


def validate_config_file(*, use_cache: bool = False):
    if os.environ.get("OPENCLAW_MODEL_ADMIN_SKIP_VALIDATE", "").strip().lower() in ("1", "true", "yes", "on"):
        return {"valid": True, "issues": [], "raw": ""}
    if use_cache and not _cli_validate_cache_disabled():
        key = _cli_validate_cache_key()
        if key and _CLI_VALIDATE_CACHE.get("key") == key and _CLI_VALIDATE_CACHE.get("result") is not None:
            return copy.deepcopy(_CLI_VALIDATE_CACHE["result"])
    result = run_command(["openclaw", "config", "validate"], timeout=20)
    output = "\n".join([p for p in [result.get("stdout", ""), result.get("stderr", "")] if p]).strip()

    if result.get("code") == -1:
        out = {"valid": False, "issues": [f"配置校验命令执行失败: {result.get('stderr') or 'unknown'}"], "raw": output}
        if use_cache and not _cli_validate_cache_disabled():
            key = _cli_validate_cache_key()
            if key is not None:
                _CLI_VALIDATE_CACHE["key"] = key
                _CLI_VALIDATE_CACHE["result"] = copy.deepcopy(out)
        return out

    valid = "Config invalid" not in output
    issues = []
    for line in output.splitlines():
        s = line.strip()
        if s.startswith("×"):
            issues.append(s[1:].strip())
    if not valid and not issues:
        issues = [output or "配置无效"]

    out = {"valid": valid, "issues": issues, "raw": output}
    if use_cache and not _cli_validate_cache_disabled():
        key = _cli_validate_cache_key()
        if key is not None:
            _CLI_VALIDATE_CACHE["key"] = key
            _CLI_VALIDATE_CACHE["result"] = copy.deepcopy(out)
    return out


def save_config_with_validation(config):
    previous_raw = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else "{}\n"
    migrations = write_config(config)
    validation = validate_config_file(use_cache=False)
    if not validation["valid"]:
        CONFIG_PATH.write_text(previous_raw, encoding="utf-8")
        brief = "；".join(validation["issues"][:3]) if validation["issues"] else (validation["raw"] or "未知错误")
        raise ValueError(f"配置校验失败，已自动回滚：{brief}")
    _prime_cli_validate_cache(validation)
    sess_norm = normalize_sessions_provider_overrides_lowercase()
    return {"migrations": migrations, "validation": validation, "sessionProviderNormalize": sess_norm}

def run_command(args, timeout=5):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"ok": result.returncode == 0, "stdout": result.stdout.strip(), "stderr": result.stderr.strip(), "code": result.returncode}
    except Exception as e:
        return {"ok": False, "stdout": "", "stderr": str(e), "code": -1}


def _gateway_health_url() -> str:
    return os.environ.get("OPENCLAW_GATEWAY_HEALTH_URL", "").strip()


def _gateway_ss_markers() -> list[str]:
    raw = os.environ.get("OPENCLAW_GATEWAY_SS_MARKERS", "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return ["127.0.0.1:18789", "[::1]:18789"]


def _probe_http_url(url: str) -> bool:
    """用标准库探测 HTTP(S)，不依赖 curl（Docker slim 友好）；失败时再尝试 curl。"""
    try:
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        req = Request(url, method="HEAD", headers={"User-Agent": "openclaw-model-admin"})
        try:
            with urlopen(req, timeout=3) as resp:
                code = getattr(resp, "status", None) or resp.getcode()
                return code is not None and int(code) < 500
        except HTTPError as e:
            return int(e.code) < 500
    except Exception:
        pass
    try:
        from urllib.request import urlopen

        with urlopen(url, timeout=3) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            return code is not None and int(code) < 500
    except Exception:
        pass
    r = run_command(
        ["curl", "-sf", "--max-time", "3", "-o", "/dev/null", url],
        timeout=6,
    )
    return bool(r.get("ok"))


def probe_gateway_active() -> bool:
    """判断网关是否在线：优先 HTTP(S) 健康 URL，其次 systemd，再 ss 端口特征。"""
    url = _gateway_health_url()
    if url:
        return _probe_http_url(url)
    st = run_command(["systemctl", "is-active", SERVICE_NAME], timeout=5)
    if st.get("ok"):
        return st.get("stdout") == "active"
    pr = run_command(["ss", "-ltn"], timeout=5)
    if pr.get("ok"):
        out = pr.get("stdout") or ""
        return any(m in out for m in _gateway_ss_markers())
    return True

def _session_key_label(session_key: str) -> str:
    if session_key == MAIN_SESSION_KEY:
        return "主会话"
    if ":telegram:direct:" in session_key:
        return "Telegram 私聊"
    if ":telegram:slash:" in session_key:
        return "Telegram 斜杠"
    if ":telegram:group:" in session_key:
        tail = session_key.split(":telegram:group:", 1)[-1]
        return f"Telegram 群 {tail}" if tail else "Telegram 群组"
    if ":cron:" in session_key:
        return "定时任务"
    return session_key


def _effective_model_ref_for_session(raw: dict, primary: str) -> tuple:
    """返回 (model_ref, via_override)。"""
    po = raw.get("providerOverride") if isinstance(raw.get("providerOverride"), str) else ""
    mo = raw.get("modelOverride") if isinstance(raw.get("modelOverride"), str) else ""
    po, mo = po.strip(), mo.strip()
    if po and mo:
        return f"{po}/{mo}", True
    if mo and "/" in mo:
        return mo, True
    if mo:
        return mo, True
    ref = (primary or "").strip()
    return (ref if ref else "—", False)


def _primary_ref_from_config(config: dict) -> str:
    try:
        p = (config.get("agents") or {}).get("defaults", {}).get("model", {}).get("primary") or ""
        return p.strip() if isinstance(p, str) else ""
    except Exception:
        return ""


def _safe_positive_int(val) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val if val > 0 else None
    if isinstance(val, float):
        i = int(val)
        return i if i > 0 else None
    if isinstance(val, str) and val.strip():
        try:
            i = int(val.strip().replace(",", ""), 10)
            return i if i > 0 else None
        except ValueError:
            return None
    return None


def _positive_int_from_payload(val, default: int) -> int:
    """
    解析前端提交的 contextWindow / maxTokens。
    JSON null、前端 NaN 序列化、空串、非数字等一律回落 default，避免 int(None) 导致 400。
    """
    if default <= 0:
        raise ValueError("default 须为正整数")
    x = _safe_positive_int(val)
    return x if x is not None else default


def _model_limits_from_config_providers(config: dict, ref: str) -> tuple[int | None, int | None]:
    """
    从 openclaw.json 的 models.providers[*].models[] 取该 ref 的 contextWindow、maxTokens。
    内置/仅存在于合并 catalog 的 ref 若无独立 provider 块则返回 (None, None)。
    """
    if not ref or ref.strip() in ("—", "") or "/" not in ref:
        return None, None
    ref_n = normalize_model_ref_provider_lower(ref.strip())
    prov_id, _, mid = ref_n.partition("/")
    mid = mid.strip()
    prov_id = normalize_provider_id(prov_id.strip())
    if not prov_id or not mid:
        return None, None
    provs = (config.get("models") or {}).get("providers")
    if not isinstance(provs, dict):
        return None, None
    pk = resolve_provider_key_in_provs(provs, prov_id)
    if not pk:
        return None, None
    block = provs.get(pk)
    if not isinstance(block, dict):
        return None, None
    for m in block.get("models") or []:
        if not isinstance(m, dict):
            continue
        mid_c = m.get("id")
        if not isinstance(mid_c, str) or mid_c.strip() != mid:
            continue
        cw = _safe_positive_int(m.get("contextWindow"))
        mt = _safe_positive_int(m.get("maxTokens"))
        return cw, mt
    return None, None


def sync_all_sessions_context_tokens_from_config(config: dict) -> dict:
    """
    按当前磁盘逻辑：用各会话 effective ref 在 models.providers 里的 contextWindow，
    覆盖写入 sessions.json 的 contextTokens（与 OpenClaw 优先读 entry.contextTokens 的行为对齐）。
    在 providers 中找不到该 ref 时删除 contextTokens，回落到 OpenClaw 合并模型表/内置默认值。
    maxTokens 仅存在于模型定义，由 openclaw.json 合并进 catalog，无需写入会话文件。
    """
    if not SESSION_STORE_PATH.exists():
        return {"ok": False, "error": "sessions.json 不存在", "path": str(SESSION_STORE_PATH)}

    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"读取 sessions.json 失败: {e}"}

    if not isinstance(store, dict):
        return {"ok": False, "error": "sessions 根节点应为对象"}

    primary = _primary_ref_from_config(config)
    updated = 0
    removed = 0
    for key, raw in store.items():
        if key in ("global", "unknown") or not isinstance(raw, dict):
            continue
        ref, _via = _effective_model_ref_for_session(raw, primary)
        cw, _mt = _model_limits_from_config_providers(config, ref)
        if cw is not None:
            if raw.get("contextTokens") != cw:
                raw["contextTokens"] = cw
                updated += 1
        else:
            if "contextTokens" in raw:
                del raw["contextTokens"]
                removed += 1

    if updated == 0 and removed == 0:
        return {
            "ok": True,
            "sessionsContextUpdated": 0,
            "sessionsContextRemoved": 0,
            "changed": False,
        }

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = SESSION_STORE_PATH.parent / f"sessions.json.bak.ctxsync-{stamp}"
    try:
        shutil.copy2(SESSION_STORE_PATH, backup_path)
    except Exception as e:
        return {"ok": False, "error": f"备份 sessions.json 失败: {e}"}

    try:
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        try:
            shutil.copy2(backup_path, SESSION_STORE_PATH)
        except Exception:
            pass
        return {"ok": False, "error": f"写入 sessions.json 失败（已尝试从备份恢复）: {e}"}

    return {
        "ok": True,
        "sessionsContextUpdated": updated,
        "sessionsContextRemoved": removed,
        "changed": True,
        "backupPath": str(backup_path),
    }


def _session_preview_priority(session_key: str) -> int:
    """管理端「当前聊天」预览：优先电报私聊（与多数用户看 /status 的会话一致），避免误选 cron/群等 updatedAt 更高的键。"""
    if ":telegram:direct:" in session_key:
        return 40
    if ":telegram:slash:" in session_key:
        return 35
    if ":telegram:group:" in session_key or ":telegram:channel:" in session_key:
        return 30
    if session_key == MAIN_SESSION_KEY:
        return 25
    if ":telegram:" in session_key:
        return 20
    return 10


def _thinking_str_from_params_raw(raw_th) -> str:
    """agents.defaults.models.*.params.thinking 原始值 → 展示用字符串（与 build_state 列表一致）。"""
    if raw_th is None:
        return "off"
    if isinstance(raw_th, str):
        return raw_th.strip() or "off"
    return str(raw_th).strip() or "off"


def thinking_from_agents_defaults(config: dict, model_ref: str) -> str:
    """openclaw.json 中 agents.defaults.models[ref].params.thinking；无则 off（与 /status 在「无会话 thinkingLevel」时的主路径一致）。"""
    if not model_ref or model_ref == "—" or "/" not in model_ref:
        return "off"
    am = (config.get("agents") or {}).get("defaults", {}).get("models") or {}
    if not isinstance(am, dict):
        return "off"
    r0 = model_ref.strip()
    entry = am.get(r0)
    if not isinstance(entry, dict):
        entry = am.get(normalize_model_ref_provider_lower(r0))
    if not isinstance(entry, dict):
        return "off"
    params = entry.get("params")
    if not isinstance(params, dict):
        return "off"
    t = params.get("thinking")
    if isinstance(t, str) and t.strip():
        return t.strip()
    return "off"


def _session_entry_to_preview(config: dict, primary: str, key: str, raw: dict) -> dict:
    """单条会话 → 与电报 /status 一致的 Think 解析（thinkingLevel 优先，否则 params.thinking）。"""
    ref, via = _effective_model_ref_for_session(raw, primary)
    cfg_th = thinking_from_agents_defaults(config, ref)
    tl_raw = raw.get("thinkingLevel")
    if isinstance(tl_raw, str) and tl_raw.strip():
        tl = tl_raw.strip()
        status_think = tl
        src = "session"
    else:
        tl = None
        status_think = cfg_th
        src = "config"
    return {
        "sessionKey": key,
        "sessionLabel": _session_key_label(key),
        "modelRef": ref,
        "viaOverride": via,
        "thinkingLevel": tl,
        "configThinking": cfg_th,
        "statusThink": status_think,
        "statusThinkSource": src,
    }


# 从电报会话抄到 agent:main:main 的「行为字段」（不含 thinkingLevel：思考只跟各模型在前端的 params.thinking）
# 不碰 providerOverride、modelOverride 等，网页端模型选择保持独立
_BEHAVIOR_KEYS_TELEGRAM_TO_WEB = (
    "reasoningLevel",
    "elevatedLevel",
    "verboseLevel",
    "fastMode",
    "queueMode",
    "queueDebounceMs",
    "queueCap",
    "queueDrop",
    "execHost",
    "execSecurity",
    "execAsk",
    "execNode",
)

_MODEL_IDENTITY_KEYS_PRESERVED_ON_WEB = frozenset(
    {
        "providerOverride",
        "modelOverride",
        "model",
        "modelProvider",
        "authProfileOverride",
    }
)


def pick_telegram_direct_session_key(store: dict) -> str | None:
    """与 build_session_previews 一致：updatedAt 最新的 telegram 私聊键。"""
    td = [k for k, v in store.items() if isinstance(v, dict) and ":telegram:direct:" in k]
    if not td:
        return None
    return max(td, key=lambda k: int((store[k] or {}).get("updatedAt") or 0))


def sync_web_session_from_telegram_direct(source_session_key: str | None = None) -> dict:
    """
    直接写 sessions.json：把电报私聊上的提权/推理展示/队列/exec 等与网页主会话对齐。
    不包含 thinkingLevel：Think 由 openclaw.json 里各 ref 的 params.thinking 决定，由 clear_session_thinking_levels 统一清会话覆盖。
    保留网页 modelOverride 等模型相关键。
    """
    if not SESSION_STORE_PATH.exists():
        return {"ok": False, "error": "sessions.json 不存在", "path": str(SESSION_STORE_PATH)}

    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"读取 sessions.json 失败: {e}"}

    if not isinstance(store, dict):
        return {"ok": False, "error": "sessions 根节点应为对象"}

    src_key = (source_session_key or "").strip() if isinstance(source_session_key, str) else ""
    if src_key:
        if ":telegram:direct:" not in src_key:
            return {"ok": False, "error": "sourceSessionKey 须为 telegram 私聊键（包含 :telegram:direct:）"}
        if src_key not in store or not isinstance(store.get(src_key), dict):
            return {"ok": False, "error": f"源会话不存在: {src_key}"}
    else:
        src_key = pick_telegram_direct_session_key(store)
        if not src_key:
            return {"ok": False, "error": "未找到任何 agent:*:telegram:direct:* 会话，无法对齐"}

    src = store[src_key]
    if not isinstance(src, dict):
        return {"ok": False, "error": "源会话数据无效"}

    main = store.get(MAIN_SESSION_KEY)
    if not isinstance(main, dict):
        return {
            "ok": False,
            "error": f"{MAIN_SESSION_KEY} 不存在或无效，请先用 PocketClaw / 网页聊一次以建立主会话",
        }

    preserved_model_keys = [k for k in _MODEL_IDENTITY_KEYS_PRESERVED_ON_WEB if k in main]

    changes: dict = {}
    for field in _BEHAVIOR_KEYS_TELEGRAM_TO_WEB:
        before = copy.deepcopy(main.get(field, "__missing__"))
        if field in src:
            main[field] = copy.deepcopy(src[field])
            after = copy.deepcopy(main[field])
        else:
            if field in main:
                del main[field]
            after = "__missing__"
        if before != after:
            changes[field] = {"before": None if before == "__missing__" else before, "after": None if after == "__missing__" else after}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = SESSION_STORE_PATH.parent / f"sessions.json.bak.sync-web-{stamp}"
    try:
        shutil.copy2(SESSION_STORE_PATH, backup_path)
    except Exception as e:
        return {"ok": False, "error": f"备份 sessions.json 失败: {e}"}

    try:
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        try:
            shutil.copy2(backup_path, SESSION_STORE_PATH)
        except Exception:
            pass
        return {"ok": False, "error": f"写入 sessions.json 失败（已尝试从备份恢复）: {e}"}

    return {
        "ok": True,
        "sourceSessionKey": src_key,
        "targetSessionKey": MAIN_SESSION_KEY,
        "backupPath": str(backup_path),
        "preservedModelFields": preserved_model_keys,
        "changes": changes,
    }


def main_session_route_drift(config: dict, session_store=_MISSING_SESSION_SNAPSHOT) -> dict:
    """网页主会话是否因 modelOverride 等与「默认路由 primary」脱节。"""
    try:
        primary = (config.get("agents") or {}).get("defaults", {}).get("model", {}).get("primary") or ""
    except Exception:
        primary = ""
    primary = primary.strip() if isinstance(primary, str) else ""
    store = _read_session_store() if session_store is _MISSING_SESSION_SNAPSHOT else session_store
    if not store:
        return {
            "hasOverride": False,
            "effectiveRef": primary or "—",
            "primary": primary or "—",
            "matchesPrimary": True,
        }
    raw = store.get(MAIN_SESSION_KEY)
    if not isinstance(raw, dict):
        return {
            "hasOverride": False,
            "effectiveRef": primary or "—",
            "primary": primary or "—",
            "matchesPrimary": True,
        }
    po = (raw.get("providerOverride") or "").strip() if isinstance(raw.get("providerOverride"), str) else ""
    mo = (raw.get("modelOverride") or "").strip() if isinstance(raw.get("modelOverride"), str) else ""
    has_override = bool(po or mo)
    ref, _via = _effective_model_ref_for_session(raw, primary)
    ref_s = ref.strip() if isinstance(ref, str) else ""
    matches = (not has_override) or (bool(primary) and ref_s == primary)
    return {
        "hasOverride": has_override,
        "effectiveRef": ref_s or "—",
        "primary": primary or "—",
        "matchesPrimary": bool(matches),
    }


def _read_session_store():
    if not SESSION_STORE_PATH.exists():
        return None
    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    return store if isinstance(store, dict) else None


def build_session_previews(config, session_store=_MISSING_SESSION_SNAPSHOT) -> list:
    """并排预览：电报私聊 + 网页主会话（agent:main:main），便于核对 Web 与 Telegram 是否同一 ref、同一 /status Think。"""
    try:
        primary = (config.get("agents") or {}).get("defaults", {}).get("model", {}).get("primary") or ""
    except Exception:
        primary = ""
    primary = primary if isinstance(primary, str) else ""
    store = _read_session_store() if session_store is _MISSING_SESSION_SNAPSHOT else session_store
    if not store:
        return []
    out = []
    bk = pick_telegram_direct_session_key(store)
    if bk:
        p = _session_entry_to_preview(config, primary, bk, store[bk])
        p["previewTitle"] = "电报私聊"
        out.append(p)

    wm = store.get(MAIN_SESSION_KEY)
    if isinstance(wm, dict):
        oc = wm.get("origin") if isinstance(wm.get("origin"), dict) else {}
        is_web = (
            wm.get("lastChannel") == "webchat"
            or oc.get("provider") == "webchat"
            or oc.get("surface") == "webchat"
        )
        if is_web:
            p = _session_entry_to_preview(config, primary, MAIN_SESSION_KEY, wm)
            p["previewTitle"] = "网页 / PocketClaw 主会话"
            out.append(p)
    return out


def resolve_active_chat_session(config, session_store=_MISSING_SESSION_SNAPSHOT):
    """从 sessions.json 选一条「预览用」会话（优先电报私聊），字段与 /status Think 解析一致。"""
    try:
        primary = (config.get("agents") or {}).get("defaults", {}).get("model", {}).get("primary") or ""
    except Exception:
        primary = ""
    primary = primary if isinstance(primary, str) else ""
    prim_ref = primary.strip() or "—"
    cfg_only = thinking_from_agents_defaults(config, prim_ref)
    empty = {
        "sessionKey": None,
        "sessionLabel": "（无会话记录）",
        "modelRef": prim_ref,
        "viaOverride": False,
        "thinkingLevel": None,
        "configThinking": cfg_only,
        "statusThink": cfg_only,
        "statusThinkSource": "config",
    }
    store = _read_session_store() if session_store is _MISSING_SESSION_SNAPSHOT else session_store
    if store is None:
        if not SESSION_STORE_PATH.exists():
            return empty
        pr = primary.strip() or "—"
        ct = thinking_from_agents_defaults(config, pr)
        return {
            "sessionKey": None,
            "sessionLabel": "（会话库读取失败）",
            "modelRef": pr,
            "viaOverride": False,
            "thinkingLevel": None,
            "configThinking": ct,
            "statusThink": ct,
            "statusThinkSource": "config",
        }
    if not store:
        return empty

    candidates = []
    for k, raw in store.items():
        if k in ("global", "unknown") or not isinstance(raw, dict):
            continue
        ts = int(raw.get("updatedAt") or 0)
        candidates.append((k, raw, ts))
    if not candidates:
        return empty

    candidates.sort(
        key=lambda item: (-_session_preview_priority(item[0]), -item[2], item[0])
    )
    best_key, raw_best, _ts = candidates[0]
    return _session_entry_to_preview(config, primary, best_key, raw_best)


def sync_session_defaults(
    elevated_default=None,
    reasoning_default=None,
    clear_model_overrides=False,
    session_key=None,
    *,
    strip_session_thinking=True,
):
    """同步提权、推理展示；按需去掉会话 thinkingLevel（仅管理端「应用/下发」时调用，电报 /think 写入可保留到下次应用）。"""
    if not SESSION_STORE_PATH.exists():
        return {
            "updated": 0,
            "path": str(SESSION_STORE_PATH),
            "exists": False,
            "clearedModelOverrides": 0,
            "strippedThinking": 0,
        }

    store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    if not isinstance(store, dict):
        return {
            "updated": 0,
            "path": str(SESSION_STORE_PATH),
            "exists": True,
            "clearedModelOverrides": 0,
            "strippedThinking": 0,
        }

    updated = 0
    cleared_model_overrides = 0
    stripped_thinking = 0
    for key, raw in store.items():
        if not isinstance(raw, dict):
            continue
        if session_key and key != session_key:
            continue
        changed = False
        if strip_session_thinking and "thinkingLevel" in raw:
            del raw["thinkingLevel"]
            stripped_thinking += 1
            changed = True
        if isinstance(elevated_default, str) and elevated_default:
            if raw.get("elevatedLevel") != elevated_default:
                raw["elevatedLevel"] = elevated_default
                changed = True
        if reasoning_default in ("on", "off"):
            if raw.get("reasoningLevel") != reasoning_default:
                raw["reasoningLevel"] = reasoning_default
                changed = True
        if clear_model_overrides:
            had_model_override = False
            for field in ("modelOverride", "providerOverride"):
                if raw.get(field):
                    raw.pop(field, None)
                    had_model_override = True
                    changed = True
            if had_model_override:
                cleared_model_overrides += 1
        if changed:
            updated += 1

    if updated > 0:
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    return {
        "updated": updated,
        "path": str(SESSION_STORE_PATH),
        "exists": True,
        "clearedModelOverrides": cleared_model_overrides,
        "strippedThinking": stripped_thinking,
    }


def _thinking_value_for_params(tv: object) -> str:
    """接入配置 / 路由主模型：表单里的 thinkingValue → 写入 params（空则 low）。"""
    if isinstance(tv, str):
        s = tv.strip()
    else:
        s = str(tv).strip() if tv is not None else ""
    if not s:
        s = "low"
    if len(s) > THINKING_PARAM_MAX_LEN:
        raise ValueError(f"思考参数过长（最多 {THINKING_PARAM_MAX_LEN} 字符）")
    return s


def _set_agent_model_thinking(config: dict, ref_key: str, thinking_str: str) -> None:
    """仅更新 agents.defaults.models.<ref>.params.thinking（保留该 ref 上其它 params）。"""
    models = config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
    key = normalize_model_ref_provider_lower(ref_key.strip())
    ent = models.setdefault(key, {})
    if not isinstance(ent, dict):
        ent = {}
        models[key] = ent
    pr = dict(ent.get("params") or {}) if isinstance(ent.get("params"), dict) else {}
    pr["thinking"] = thinking_str
    pr.pop("reasoningEffort", None)
    ent["params"] = pr


def clear_session_thinking_levels():
    """保存模型思考等配置后调用：去掉各会话 thinkingLevel，使 agents.defaults.models.*.params.thinking 生效。"""
    if not SESSION_STORE_PATH.exists():
        return {"cleared": 0, "exists": False}
    store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    if not isinstance(store, dict):
        return {"cleared": 0, "exists": True}
    cleared = 0
    for raw in store.values():
        if isinstance(raw, dict) and "thinkingLevel" in raw:
            del raw["thinkingLevel"]
            cleared += 1
    if cleared > 0:
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"cleared": cleared, "exists": True}


def _refs_available_in_config(config: dict) -> set[str]:
    """当前配置中可作为 primary/fallback 的 provider/model ref 集合。"""
    refs: set[str] = set()
    agents = config.get("agents")
    if isinstance(agents, dict):
        for ab in agents.values():
            if not isinstance(ab, dict):
                continue
            defs = ab.get("defaults")
            if not isinstance(defs, dict):
                continue
            am = defs.get("models")
            if isinstance(am, dict):
                for k in am:
                    if isinstance(k, str) and "/" in k.strip():
                        refs.add(k.strip())
    mdl = config.get("models")
    if isinstance(mdl, dict):
        provs = mdl.get("providers")
        if isinstance(provs, dict):
            for pn, p in provs.items():
                if not isinstance(p, dict):
                    continue
                for m in p.get("models") or []:
                    if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip():
                        refs.add(f"{pn}/{m['id'].strip()}")
    return refs


def _pick_fallback_primary(config: dict) -> str:
    refs = sorted(_refs_available_in_config(config))
    return refs[0] if refs else ""


def _repair_model_routing_block(mb: dict, config: dict, removed_prefix: str) -> None:
    """单个 agents.*.defaults.model：删掉某供应商后修正 primary / fallbacks。"""
    if not isinstance(mb, dict):
        return
    refs_set = _refs_available_in_config(config)
    pr = mb.get("primary")
    pr = pr.strip() if isinstance(pr, str) else ""
    broken = (not pr) or pr.startswith(removed_prefix) or bool(refs_set and pr not in refs_set)
    if broken:
        mb["primary"] = _pick_fallback_primary(config)
    pr2 = mb.get("primary")
    pr2 = pr2.strip() if isinstance(pr2, str) else ""
    fbs = mb.get("fallbacks", [])
    if not isinstance(fbs, list):
        mb["fallbacks"] = []
        return
    new_fbs: list[str] = []
    seen: set[str] = set()
    for x in fbs:
        if not isinstance(x, str):
            continue
        xs = x.strip()
        if not xs or xs.startswith(removed_prefix):
            continue
        if refs_set and xs not in refs_set:
            continue
        if xs == pr2:
            continue
        if xs in seen:
            continue
        seen.add(xs)
        new_fbs.append(xs)
    mb["fallbacks"] = new_fbs


def _repair_all_agent_model_routing(config: dict, removed_prefix: str) -> None:
    """遍历 agents 下各条目的 defaults.model，避免仅修了 defaults 主块。"""
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return
    for ab in agents.values():
        if not isinstance(ab, dict):
            continue
        defs = ab.get("defaults")
        if not isinstance(defs, dict):
            continue
        mb = defs.get("model")
        if isinstance(mb, dict):
            _repair_model_routing_block(mb, config, removed_prefix)


def _deep_remove_agent_ref_keys(obj: object, prefix: str) -> int:
    """在 agents 子树中删除所有「模型 ref」形键（prefix 如 供应商名/，须含 /），含 alias、params.thinking 等整块。"""
    removed = 0
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if isinstance(k, str) and "/" in k and k.startswith(prefix):
                del obj[k]
                removed += 1
            else:
                removed += _deep_remove_agent_ref_keys(obj[k], prefix)
    elif isinstance(obj, list):
        for x in obj:
            removed += _deep_remove_agent_ref_keys(x, prefix)
    return removed


def _strip_agents_models_key_prefix(config: dict, prefix: str) -> int:
    """删除 agents 下任意深度的 per-model 配置键（不限于 defaults.models）。"""
    agents = config.get("agents")
    if not isinstance(agents, dict):
        return 0
    return _deep_remove_agent_ref_keys(agents, prefix)


def _purge_auth_profiles_for_provider(config: dict, p_name: str) -> list[str]:
    """移除 auth.profiles 中与某 models.providers 供应商绑定的项（provider 字段或 profile 键名）。"""
    removed: list[str] = []
    auth = config.get("auth")
    if not isinstance(auth, dict):
        return removed
    profs = auth.get("profiles")
    if not isinstance(profs, dict):
        return removed
    key_prefix = f"{p_name}:"
    for key in list(profs.keys()):
        if not isinstance(key, str):
            continue
        ent = profs.get(key)
        drop = key == p_name or key.startswith(key_prefix)
        if not drop and isinstance(ent, dict) and ent.get("provider") == p_name:
            drop = True
        if drop:
            del profs[key]
            removed.append(key)
    return removed


def _clear_sessions_overrides_for_provider(p_name: str) -> dict:
    """sessions.json：去掉指向已删供应商的 providerOverride / modelOverride。"""
    if not SESSION_STORE_PATH.exists():
        return {"clearedSessions": 0, "exists": False}
    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"clearedSessions": 0, "exists": True, "error": "sessions 解析失败"}
    if not isinstance(store, dict):
        return {"clearedSessions": 0, "exists": True, "error": "sessions 根非对象"}
    prefix = p_name + "/"
    cleared = 0
    for raw in store.values():
        if not isinstance(raw, dict):
            continue
        changed = False
        po = raw.get("providerOverride")
        po = po.strip() if isinstance(po, str) else ""
        mo = raw.get("modelOverride")
        mo = mo.strip() if isinstance(mo, str) else ""
        po_n = normalize_provider_id(po) if po else ""
        p_norm = normalize_provider_id(p_name)
        if po_n and po_n == p_norm:
            raw.pop("providerOverride", None)
            raw.pop("modelOverride", None)
            changed = True
        elif mo:
            if mo.startswith(prefix) or (
                "/" in mo and normalize_provider_id(mo.split("/", 1)[0]) == p_norm
            ):
                raw.pop("modelOverride", None)
                changed = True
        if changed:
            cleared += 1
    if cleared > 0:
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"clearedSessions": cleared, "exists": True}


def _iter_agent_models_json_paths() -> list[Path]:
    """OpenClaw 按 agents/<agentId>/agent/models.json 与 openclaw.json 合并模型表。"""
    agents_dir = CONFIG_PATH.parent / "agents"
    if not agents_dir.is_dir():
        return []
    out: list[Path] = []
    try:
        for sub in sorted(agents_dir.iterdir()):
            if not sub.is_dir():
                continue
            cand = sub / "agent" / "models.json"
            if cand.is_file():
                out.append(cand)
    except OSError:
        return []
    return out


def _provider_in_agent_models_json(p_name: str) -> bool:
    want = normalize_provider_id(p_name)
    for path in _iter_agent_models_json_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        provs = data.get("providers") if isinstance(data, dict) else None
        if isinstance(provs, dict) and resolve_provider_key_in_provs(provs, want):
            return True
    return False


def _provider_block_from_agent_models_json(p_name: str) -> dict | None:
    want = normalize_provider_id(p_name)
    for path in _iter_agent_models_json_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        provs = data.get("providers") if isinstance(data, dict) else None
        if isinstance(provs, dict):
            pk = resolve_provider_key_in_provs(provs, want)
            blk = provs.get(pk) if pk else None
            if isinstance(blk, dict):
                return copy.deepcopy(blk)
    return None


def _remove_provider_from_agent_models_json_files(p_name: str) -> list[str]:
    """从各 agent 的 models.json 中移除同名 providers 键（与 openclaw.json 删除保持一致）。"""
    edited: list[str] = []
    want = normalize_provider_id(p_name)
    for path in _iter_agent_models_json_paths():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        provs = data.get("providers")
        if not isinstance(provs, dict):
            continue
        pk = resolve_provider_key_in_provs(provs, want)
        if not pk:
            continue
        del provs[pk]
        try:
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            edited.append(str(path))
        except OSError:
            continue
    return edited


def _openclaw_home_dir() -> Path:
    return CONFIG_PATH.parent.resolve()


def _looks_like_filesystem_path_str(s: str) -> bool:
    t = s.strip()
    if not t or t.startswith("http://") or t.startswith("https://"):
        return False
    if t.startswith("~") or "/" in t or "\\" in t:
        return True
    low = t.lower()
    for suf in (".pem", ".key", ".crt", ".json", "-token", "_token"):
        if low.endswith(suf):
            return True
    return False


def _credential_file_paths_in_provider_block(p: dict) -> list[Path]:
    """从 models.providers[供应商] 块中收集指向 openclaw/credentials/ 下的现有文件路径。"""
    out: list[Path] = []
    try:
        home = _openclaw_home_dir()
        cred_root = (home / "credentials").resolve()
    except OSError:
        return out

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for _k, v in o.items():
                if isinstance(v, str) and _looks_like_filesystem_path_str(v):
                    raw = Path(v).expanduser()
                    try:
                        cand = raw.resolve() if raw.is_absolute() else (home / v.lstrip("/")).resolve()
                    except OSError:
                        walk(v)
                        continue
                    if cand.is_file() and cred_root in cand.parents:
                        out.append(cand)
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(p)
    seen: set[str] = set()
    uniq: list[Path] = []
    for x in out:
        sx = str(x)
        if sx not in seen:
            seen.add(sx)
            uniq.append(x)
    return uniq


def _resolved_paths_under_credentials_referenced_anywhere(config: dict) -> frozenset[str]:
    """当前完整 openclaw.json 中仍被任意字段引用的、credentials/ 下已存在文件的绝对路径集合。"""
    refs: set[str] = set()
    try:
        home = _openclaw_home_dir()
        cred_root = (home / "credentials").resolve()
    except OSError:
        return frozenset()

    def walk(o: object) -> None:
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
        elif isinstance(o, str) and _looks_like_filesystem_path_str(o):
            raw = Path(o).expanduser()
            try:
                cand = raw.resolve() if raw.is_absolute() else (home / o.lstrip("/")).resolve()
            except OSError:
                return
            if cand.is_file() and cred_root in cand.parents:
                refs.add(str(cand))

    walk(config)
    return frozenset(refs)


def _unlink_provider_credential_files(paths: list[Path], remaining_config: dict) -> list[str]:
    """仅删除位于 openclaw/credentials/ 下、且删除供应商后配置中已不再引用、且非系统保护文件名的路径。"""
    removed: list[str] = []
    try:
        cred_root = (_openclaw_home_dir() / "credentials").resolve()
    except OSError:
        return removed
    if not cred_root.is_dir():
        return removed
    still_used = _resolved_paths_under_credentials_referenced_anywhere(remaining_config)
    for p in paths:
        try:
            r = p.resolve()
        except OSError:
            continue
        if cred_root not in r.parents:
            continue
        if not r.is_file():
            continue
        if r.name in PROTECTED_CREDENTIAL_BASENAMES:
            continue
        if str(r) in still_used:
            continue
        try:
            r.unlink()
            removed.append(str(r))
        except OSError:
            pass
    return removed


def probe_provider(p_name, url):
    # 针对内置供应商的特殊健康检查
    if p_name in BUILTIN_PROVIDERS or (url and "(openai-codex)" in url):
        config = read_config()
        # 1. 检查网关端口是否活跃
        gw_check = run_command(["ss", "-tulpn"])
        port_ok = "18789" in (gw_check.get("stdout") or "")
        # 2. 检查是否有对应的认证配置文件
        auth_ok = any(p_name in k for k in config.get("auth", {}).get("profiles", {}).keys())
        return port_ok and auth_ok
    
    if not url or not url.startswith("http"): return False
    try:
        res = subprocess.run(["curl", "-Is", "--max-time", "3", url], capture_output=True)
        return res.returncode == 0
    except Exception:
        return False


def build_state():
    try:
        config = read_config()
    except Exception:
        return {"alerts": [{"level": "error", "msg": "配置读取失败"}]}

    agents = config.get("agents", {}).get("defaults", {})
    providers = config.get("models", {}).get("providers", {})
    configured_models = agents.get("models", {}) if isinstance(agents.get("models", {}), dict) else {}
    
    provider_items = []
    model_items = []
    seen_refs = set()

    # 标准供应商
    for p_name, p in providers.items():
        provider_items.append({"name": p_name, "baseUrl": p.get("baseUrl", ""), "auth": p.get("auth", "api-key"), "api": p.get("api", ""), "modelCount": len(p.get("models", []))})
        for m in p.get("models", []):
            ref = f"{p_name}/{m['id']}"
            seen_refs.add(ref)
            m_entry = configured_models.get(ref)
            if not isinstance(m_entry, dict):
                m_entry = configured_models.get(normalize_model_ref_provider_lower(ref))
            m_entry = m_entry if isinstance(m_entry, dict) else {}
            m_params = m_entry.get("params", {}) if isinstance(m_entry.get("params"), dict) else {}
            th = _thinking_str_from_params_raw(m_params.get("thinking"))
            model_items.append({"ref": ref, "provider": p_name, "id": m["id"], "name": m.get("name", m["id"]), "thinking": th, "inputs": m.get("input", []), "contextWindow": m.get("contextWindow"), "maxTokens": m.get("maxTokens"), "configured": True})

    # 核心：自动提取并保护内置供应商 (openai-codex)
    for ref in configured_models.keys():
        if ref and "/" in ref and ref not in seen_refs:
            model_cfg = configured_models.get(ref, {})
            params = model_cfg.get("params", {}) if isinstance(model_cfg, dict) else {}
            p_name, m_id = ref.split("/", 1)
            if not p_name: continue
            if not any(item["name"] == p_name for item in provider_items):
                provider_items.append({"name": p_name, "baseUrl": f"({p_name})", "auth": "oauth", "api": "oauth", "modelCount": 1})
            th_in = _thinking_str_from_params_raw(params.get("thinking"))
            model_items.append({"ref": ref, "provider": p_name, "id": m_id, "name": f"{m_id} (内置)", "thinking": th_in, "inputs": ["text"], "contextWindow": 1000000, "maxTokens": 128000, "configured": True, "elevated": params.get("elevated")})
            seen_refs.add(ref)

    config_issues = []
    for ref, raw in configured_models.items():
        if not isinstance(raw, dict):
            config_issues.append(f"agents.defaults.models.{ref}: 应为对象")
            continue
        unknown = [k for k in raw.keys() if k not in ALLOWED_MODEL_ENTRY_KEYS]
        if unknown:
            config_issues.append(f"agents.defaults.models.{ref}: 未识别键 {', '.join(unknown)}")
        if "params" in raw and not isinstance(raw.get("params"), dict):
            config_issues.append(f"agents.defaults.models.{ref}.params: 应为对象")

    cli_validation = validate_config_file(use_cache=True)
    if not cli_validation["valid"]:
        config_issues.extend(cli_validation["issues"])

    # 去重并保持顺序
    if config_issues:
        config_issues = list(dict.fromkeys(config_issues))

    session_snap = _read_session_store()
    gateway_active = probe_gateway_active()
    active_chat = resolve_active_chat_session(config, session_snap)
    session_previews = build_session_previews(config, session_snap)
    main_route = main_session_route_drift(config, session_snap)

    alignment_hints = []
    if isinstance(configured_models, dict) and "/" in configured_models:
        alignment_hints.append(
            "提示：agents.defaults.models 存在键 “/”，一般应使用 provider/model 形式的 ref；若行为异常可考虑删除该条。"
        )

    alerts = []
    if not gateway_active:
        alerts.append({"level": "error", "msg": "网关服务离线"})
    if config_issues:
        alerts.append({"level": "error", "msg": f"配置校验失败（{len(config_issues)} 项）"})
    
    prefs = read_admin_prefs()
    return {
        "configPath": str(CONFIG_PATH),
        "primary": agents.get("model", {}).get("primary", ""),
        "mainSessionRoute": main_route,
        "fallbacks": agents.get("model", {}).get("fallbacks", []),
        "reasoningDisplay": prefs.get("reasoningDisplay", "off"),
        "elevatedDefault": agents.get("elevatedDefault", "off"),
        "gatewayActive": gateway_active,
        "configValid": len(config_issues) == 0,
        "configIssues": config_issues,
        "activeChatSession": active_chat,
        "sessionPreviews": session_previews,
        "alignmentHints": alignment_hints,
        "providers": sorted(provider_items, key=lambda x: x["name"]),
        "models": sorted(model_items, key=lambda x: x["ref"]),
        "alerts": alerts
    }

class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=HTTPStatus.OK):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path):
        if not path.exists():
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()
            return
        body = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(STATIC_DIR / "index.html")
        elif path == "/api/state":
            try:
                self._send_json({"ok": True, "state": build_state()})
            except Exception as e:
                self._send_json(
                    {
                        "ok": False,
                        "error": f"build_state 失败: {e}",
                        "state": {"alerts": [{"level": "error", "msg": str(e)}], "configValid": False},
                    },
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                )
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        try:
            if path == "/api/selection":
                config = read_config()
                agents = config.setdefault("agents", {}).setdefault("defaults", {})
                praw = payload.get("primary", "")
                praw = praw.strip() if isinstance(praw, str) else ""
                agents.setdefault("model", {})["primary"] = (
                    normalize_model_ref_provider_lower(praw) if praw and "/" in praw else praw
                )
                fbl = payload.get("fallbacks", [])
                if not isinstance(fbl, list):
                    fbl = []
                agents["model"]["fallbacks"] = [
                    normalize_model_ref_provider_lower(x.strip())
                    if isinstance(x, str) and "/" in x.strip()
                    else x
                    for x in fbl
                ]
                agents.pop("thinkingDefault", None)
                if "elevatedDefault" in payload:
                    agents["elevatedDefault"] = payload["elevatedDefault"]
                if "reasoningDisplay" in payload and payload.get("reasoningDisplay") in ("on", "off"):
                    write_admin_prefs(reasoningDisplay=payload["reasoningDisplay"])
                selection_extra_meta: dict = {}
                primary_sel = (payload.get("primary") or "").strip() if isinstance(payload.get("primary"), str) else ""
                if primary_sel and "/" in primary_sel:
                    primary_sel = normalize_model_ref_provider_lower(primary_sel)
                if primary_sel and "/" in primary_sel and "primaryThinkingEnabled" in payload:
                    if payload.get("primaryThinkingEnabled") is True:
                        _set_agent_model_thinking(config, primary_sel, _thinking_value_for_params(payload.get("primaryThinkingValue", "")))
                    else:
                        _set_agent_model_thinking(config, primary_sel, "off")
                save_meta = save_config_with_validation(config)
                session_sync = None
                prefs = read_admin_prefs()
                if payload.get("applyToSessions", True):
                    session_sync = sync_session_defaults(
                        agents.get("elevatedDefault"),
                        reasoning_default=prefs.get("reasoningDisplay"),
                        clear_model_overrides=payload.get("clearModelOverrides", False),
                        session_key=payload.get("sessionKey") or None,
                        strip_session_thinking=True,
                    )
                # 网页模型可独立；提权/推理/队列/exec 等与最近活跃电报私聊一致（不抄 thinkingLevel）
                web_telegram_sync = sync_web_session_from_telegram_direct()
                # 思考只跟各模型在前端的 params.thinking：清掉所有会话 thinkingLevel，电报与网页均走配置
                selection_extra_meta["sessionThinkingCleared"] = clear_session_thinking_levels()
                selection_extra_meta["sessionContextSync"] = sync_all_sessions_context_tokens_from_config(config)
                meta_out = {
                    "migrations": save_meta.get("migrations", []),
                    "sessionSync": session_sync,
                    "webTelegramSync": web_telegram_sync,
                }
                meta_out.update(selection_extra_meta)
                self._send_json({"ok": True, "state": build_state(), "meta": meta_out})
            elif path == "/api/sync-web-from-telegram":
                sk = payload.get("sourceSessionKey")
                sk = sk.strip() if isinstance(sk, str) else None
                meta = sync_web_session_from_telegram_direct(sk)
                if not meta.get("ok"):
                    raise ValueError(meta.get("error") or "对齐失败")
                self._send_json({"ok": True, "state": build_state(), "meta": {"webTelegramSync": meta}})
            elif path == "/api/model":
                config = read_config()
                raw_pv = payload.get("provider", "")
                raw_pv = raw_pv.strip() if isinstance(raw_pv, str) else ""
                p_name = normalize_provider_id(raw_pv)
                m_id = payload.get("modelId", "").strip() if isinstance(payload.get("modelId"), str) else ""
                if not m_id:
                    raise ValueError("缺少 modelId")
                if p_name not in BUILTIN_PROVIDERS:
                    p = config.setdefault("models", {}).setdefault("providers", {}).setdefault(p_name, {"models": []})
                    p["baseUrl"], p["api"], p["auth"] = payload["baseUrl"], payload["api"], payload["auth"]
                    if payload.get("apiKey"): p["apiKey"] = payload.get("apiKey")
                    old_m = next((x for x in p.get("models", []) if isinstance(x, dict) and x.get("id") == m_id), None)
                    prev_reasoning = bool(old_m.get("reasoning")) if isinstance(old_m, dict) else None
                    if prev_reasoning is None:
                        prev_reasoning = True
                    # 勿写入 reasoningEffort：OpenClaw ModelDefinitionSchema 为 strict，会校验失败
                    cw = _positive_int_from_payload(payload.get("contextWindow"), 200000)
                    mt = _positive_int_from_payload(payload.get("maxTokens"), 8192)
                    if mt > cw:
                        mt = cw
                    new_m = {
                        "id": m_id,
                        "name": payload.get("modelName") or m_id,
                        "reasoning": prev_reasoning,
                        "input": payload.get("inputs", ["text"]),
                        "contextWindow": cw,
                        "maxTokens": mt,
                    }
                    p["models"] = [m for m in p["models"] if m["id"] != m_id] + [new_m]
                ref = normalize_model_ref_provider_lower(f"{p_name}/{m_id}")
                models_map = config.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
                entry = models_map.setdefault(ref, {})
                if not isinstance(entry, dict):
                    entry = {}
                    models_map[ref] = entry
                params = dict(entry.get("params") or {}) if isinstance(entry.get("params"), dict) else {}
                # 模型思考：关须写 params.thinking="off"；若删掉该键，OpenClaw 会对 reasoning:true 的模型回落为 low
                if "thinkingEnabled" in payload:
                    if payload.get("thinkingEnabled") is True:
                        params["thinking"] = _thinking_value_for_params(payload.get("thinkingValue", ""))
                    else:
                        params["thinking"] = "off"
                params.pop("reasoningEffort", None)
                if params:
                    entry["params"] = params
                elif "params" in entry:
                    entry.pop("params", None)
                meta_model: dict = {}
                # 默认主模型可能是 openai-codex/gpt-5.4，而用户在改 ciii/gpt-5.4：二者 modelId 相同但 ref 不同，
                # Telegram//status 跟的是 primary，只改 ciii 会表现为「改成 low 仍 off」。
                if "thinkingEnabled" in payload:
                    t_final = params.get("thinking", "off")
                    primary_block = config.get("agents", {}).get("defaults", {}).get("model")
                    primary_s = primary_block.get("primary") if isinstance(primary_block, dict) else None
                    if (
                        isinstance(primary_s, str)
                        and "/" in primary_s.strip()
                        and payload.get("mirrorThinkingToPrimary", True) is not False
                    ):
                        ps = primary_s.strip()
                        _, pm_id = ps.split("/", 1)
                        if pm_id.strip() == m_id.strip() and ps != ref:
                            _set_agent_model_thinking(config, ps, t_final)
                            meta_model["thinkingSyncedToPrimary"] = ps
                save_meta = save_config_with_validation(config)
                if save_meta.get("migrations"):
                    meta_model.setdefault("migrations", save_meta["migrations"])
                meta_model["sessionContextSync"] = sync_all_sessions_context_tokens_from_config(config)
                cleared = clear_session_thinking_levels()
                meta_model["sessionThinkingCleared"] = cleared
                self._send_json({"ok": True, "state": build_state(), "meta": meta_model})
            elif path == "/api/model/delete":
                config = read_config()
                ref = payload.get("ref")
                if not isinstance(ref, str) or "/" not in ref:
                    raise ValueError("无效 ref")
                p_name, m_id = ref.split("/", 1)
                p_name, m_id = p_name.strip(), m_id.strip()
                provs_del = config.get("models", {}).get("providers", {})
                pk = resolve_provider_key_in_provs(provs_del, p_name) if isinstance(provs_del, dict) else None
                if pk:
                    p_name = pk
                ref = normalize_model_ref_provider_lower(f"{p_name}/{m_id}")
                if p_name in config.get("models", {}).get("providers", {}):
                    p = config["models"]["providers"][p_name]
                    p["models"] = [m for m in p.get("models", []) if m["id"] != m_id]
                    if not p["models"]:
                        del config["models"]["providers"][p_name]
                am_del = config.get("agents", {}).get("defaults", {}).get("models", {})
                if isinstance(am_del, dict):
                    if ref in am_del:
                        del am_del[ref]
                    else:
                        for k in list(am_del.keys()):
                            if (
                                isinstance(k, str)
                                and normalize_model_ref_provider_lower(k) == ref
                            ):
                                del am_del[k]
                                break
                save_meta_del = save_config_with_validation(config)
                ctx_sync_del = sync_all_sessions_context_tokens_from_config(config)
                cleared_del = clear_session_thinking_levels()
                self._send_json(
                    {
                        "ok": True,
                        "state": build_state(),
                        "meta": {
                            "sessionThinkingCleared": cleared_del,
                            "sessionContextSync": ctx_sync_del,
                            "migrations": save_meta_del.get("migrations", []),
                        },
                    }
                )
            elif path == "/api/provider/delete":
                config = read_config()
                p_raw = (payload.get("provider") or "").strip()
                if not p_raw:
                    raise ValueError("缺少供应商名称")
                p_try = normalize_provider_id(p_raw)
                if p_try in BUILTIN_PROVIDERS:
                    raise ValueError("内置供应商不可删除")
                provs = config.setdefault("models", {}).setdefault("providers", {})
                if not isinstance(provs, dict):
                    provs = {}
                    config["models"]["providers"] = provs
                p_name = resolve_provider_key_in_provs(provs, p_try) or p_try
                prefix = p_name + "/"
                if p_name not in provs and not _provider_in_agent_models_json(p_try):
                    raise ValueError("未找到该供应商")
                provider_snap = None
                if p_name in provs and isinstance(provs.get(p_name), dict):
                    provider_snap = copy.deepcopy(provs[p_name])
                if provider_snap is None:
                    provider_snap = _provider_block_from_agent_models_json(p_name)
                stripped_models = _strip_agents_models_key_prefix(config, prefix)
                removed_auth_profiles = _purge_auth_profiles_for_provider(config, p_name)
                if p_name in provs:
                    del provs[p_name]
                _repair_all_agent_model_routing(config, prefix)
                save_meta = save_config_with_validation(config)
                merged_models_json_edited = _remove_provider_from_agent_models_json_files(p_name)
                removed_cred_files: list[str] = []
                if isinstance(provider_snap, dict):
                    cpaths = _credential_file_paths_in_provider_block(provider_snap)
                    removed_cred_files = _unlink_provider_credential_files(cpaths, config)
                sess_clean = _clear_sessions_overrides_for_provider(p_name)
                ctx_sync_pv = sync_all_sessions_context_tokens_from_config(config)
                cleared_pv = clear_session_thinking_levels()
                self._send_json(
                    {
                        "ok": True,
                        "state": build_state(),
                        "meta": {
                            "sessionThinkingCleared": cleared_pv,
                            "sessionContextSync": ctx_sync_pv,
                            "migrations": save_meta.get("migrations", []),
                            "removedAgentModelEntries": stripped_models,
                            "removedAuthProfiles": removed_auth_profiles,
                            "removedCredentialFiles": removed_cred_files,
                            "mergedAgentModelsJsonEdited": merged_models_json_edited,
                            "sessionOverridesCleared": sess_clean,
                        },
                    }
                )
            elif path == "/api/probe":
                state = build_state()
                results = {
                    p["name"]: probe_provider(p["name"], p.get("baseUrl", ""))
                    for p in state["providers"]
                }
                self._send_json({"ok": True, "results": results, "timestamp": datetime.now().strftime("%H:%M:%S")})
            elif path == "/api/restart":
                custom = os.environ.get("OPENCLAW_GATEWAY_RESTART_COMMAND", "").strip()
                if custom:
                    restart = run_command(["/bin/sh", "-lc", custom], timeout=90)
                    if not restart.get("ok"):
                        err = (restart.get("stderr") or restart.get("stdout") or "").strip() or "重启命令失败"
                        raise RuntimeError(err)
                    if _gateway_health_url():
                        ok = False
                        for _ in range(30):
                            if probe_gateway_active():
                                ok = True
                                break
                            time.sleep(0.5)
                        if not ok:
                            raise RuntimeError(
                                "已执行 OPENCLAW_GATEWAY_RESTART_COMMAND，但在 OPENCLAW_GATEWAY_HEALTH_URL 上未探测到恢复，请检查命令与 URL"
                            )
                else:
                    restart = run_command(["systemctl", "restart", SERVICE_NAME], timeout=20)
                    if not restart.get("ok"):
                        raise RuntimeError(restart.get("stderr") or restart.get("stdout") or "重启失败")
                    active = run_command(["systemctl", "is-active", SERVICE_NAME], timeout=5).get("stdout")
                    if active != "active":
                        raise RuntimeError(f"重启后服务状态异常: {active or 'unknown'}")
                self._send_json({"ok": True, "state": build_state()})
            elif path == "/api/session-align":
                # 与页头「同步」一致：只读当前磁盘配置与会话快照，不写 openclaw.json / sessions.json、不重启（请求体忽略）
                self._send_json({"ok": True, "state": build_state(), "meta": {"sessionSync": None}})
            else:
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, status=HTTPStatus.BAD_REQUEST)

if __name__ == "__main__":
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
