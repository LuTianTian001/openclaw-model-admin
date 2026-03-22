#!/usr/bin/env python3
import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import fcntl
except ImportError:
    fcntl = None  # type: ignore

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

# 管理端自报版本（仅用于页面展示，便于多台环境对照）
PANEL_META_VERSION = os.environ.get("OPENCLAW_MODEL_ADMIN_VERSION", "1.2.0").strip() or "1.2.0"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# OpenClaw CLI 远端版本缓存（npm），默认 12 小时刷新；展开「本机信息」可 force=1 跳过缓存
_OPENCLAW_LATEST_CACHE: dict = {"ts": 0.0, "latest": None, "error": None}
_OPENCLAW_VERSION_CHECK_INTERVAL_SEC = max(60, _env_int("OPENCLAW_ADMIN_OPENCLAW_VERSION_CHECK_SEC", 43200))
# 本机 openclaw -V 短时缓存，避免每次 /api/state 都 spawn
_OPENCLAW_CLI_VER_CACHE: dict = {"ts": 0.0, "version": None, "error": None}
_OPENCLAW_CLI_VER_CACHE_TTL_SEC = max(15, _env_int("OPENCLAW_ADMIN_OPENCLAW_CLI_CACHE_SEC", 120))


# POST JSON 体上限（防异常大包占内存）；与监听地址/鉴权等网络策略无关
MAX_POST_BODY_BYTES = max(64 * 1024, _env_int("OPENCLAW_MODEL_ADMIN_MAX_BODY_BYTES", 8 * 1024 * 1024))


def _config_lock_disabled() -> bool:
    return os.environ.get("OPENCLAW_MODEL_ADMIN_DISABLE_CONFIG_LOCK", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _config_flock_path() -> Path:
    return CONFIG_PATH.parent / f"{CONFIG_PATH.name}.lock"


@contextmanager
def _config_lock_shared():
    """跨线程串行化读 openclaw.json（与写互斥）；不阻止其他进程读 JSON。"""
    if fcntl is None or _config_lock_disabled():
        yield
        return
    p = _config_flock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


@contextmanager
def _config_lock_exclusive():
    """保存配置整段持有，避免并发写导致 JSON 损坏或与读交错。"""
    if fcntl is None or _config_lock_disabled():
        yield
        return
    p = _config_flock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a+b") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _atomic_write_utf8(path: Path, text: str) -> None:
    """同卷 tmp + replace，降低进程崩溃时留下半截 JSON 的概率。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)

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
    _atomic_write_utf8(ADMIN_PREFS_PATH, json.dumps(cur, ensure_ascii=False, indent=2) + "\n")
    return cur

def read_config():
    with _config_lock_shared():
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
    _atomic_write_utf8(CONFIG_PATH, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
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
    with _config_lock_exclusive():
        previous_raw = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.exists() else "{}\n"
        migrations = write_config(config)
        validation = validate_config_file(use_cache=False)
        if not validation["valid"]:
            _atomic_write_utf8(CONFIG_PATH, previous_raw)
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


def fetch_gateway_logs(*, lines: int = 200) -> dict:
    """
    网关近期日志：优先 journalctl -u 网关单元；失败时可读 OPENCLAW_GATEWAY_LOG_FILE 尾部。
    ClawPanel 式「日志 tail」在本机的等价实现。
    """
    n = max(50, min(2000, int(lines)))
    unit = SERVICE_NAME
    timeout = max(8, min(45, 6 + n // 40))
    r = run_command(
        ["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"],
        timeout=timeout,
    )
    if r.get("ok"):
        text = (r.get("stdout") or "").strip()
        ls = text.splitlines() if text else []
        return {
            "ok": True,
            "source": "journalctl",
            "service": unit,
            "lineCount": len(ls),
            "lines": ls,
            "text": text,
        }
    log_file = os.environ.get("OPENCLAW_GATEWAY_LOG_FILE", "").strip()
    if log_file:
        p = Path(log_file)
        if p.is_file():
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
                ls = raw.splitlines()
                tail = ls[-n:] if len(ls) > n else ls
                text = "\n".join(tail)
                return {
                    "ok": True,
                    "source": "file",
                    "path": str(p),
                    "service": unit,
                    "lineCount": len(tail),
                    "lines": tail,
                    "text": text,
                }
            except OSError as e:
                return {
                    "ok": False,
                    "service": unit,
                    "error": f"读取日志文件失败: {e}",
                    "lines": [],
                    "text": "",
                }
    err = (r.get("stderr") or r.get("stdout") or "journalctl 不可用或无权限").strip()
    return {"ok": False, "service": unit, "error": err, "lines": [], "text": ""}


def _usage_parse_int_field(v) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().lstrip("-").isdigit():
        try:
            return int(v.strip())
        except ValueError:
            return None
    return None


def _usage_session_effective_ref(raw: dict, primary: str) -> str:
    po = raw.get("providerOverride") if isinstance(raw.get("providerOverride"), str) else ""
    mo = raw.get("modelOverride") if isinstance(raw.get("modelOverride"), str) else ""
    ps, ms = po.strip(), mo.strip()
    if ps and ms:
        return f"{ps}/{ms}"
    if ms and "/" in ms:
        return ms.strip()
    if ms:
        return ms.strip()
    p = (primary or "").strip()
    return p if p else "—"


def _usage_utc_date_range(days: int) -> tuple[str, str]:
    """与 ClawPanel usage 页一致：endDate=今天 UTC，startDate=向前 (days-1) 天。"""
    d = max(1, min(366, int(days)))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=d - 1)
    return start.isoformat(), end.isoformat()


def _iter_balanced_json_slices(text: str):
    """从文本中依次切出顶层 {...} 片段（不跨字符串转义，仅适用于 CLI 输出场景）。"""
    n = len(text)
    i = 0
    while i < n:
        start = text.find("{", i)
        if start < 0:
            return
        depth = 0
        for j in range(start, n):
            c = text[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : j + 1]
                    i = j + 1
                    break
        else:
            return


def _is_sessions_usage_payload(d: dict) -> bool:
    """识别 Gateway sessions.usage 成功体，避免把日志里其它 JSON 或错误片段当成用量。"""
    if d.get("ok") is False:
        return False
    if not isinstance(d.get("totals"), dict):
        return False
    if not isinstance(d.get("sessions"), list):
        return False
    return True


def _coerce_sessions_usage_dict(d: dict) -> dict | None:
    """兼容极少数包装形态（如 { result: { totals, sessions } }）。"""
    if _is_sessions_usage_payload(d):
        return d
    for key in ("result", "data", "payload"):
        inner = d.get(key)
        if isinstance(inner, dict) and _is_sessions_usage_payload(inner):
            return inner
    return None


def _parse_sessions_usage_from_cli_streams(*, stdout: str, stderr: str) -> dict | None:
    """
    从 openclaw gateway call 输出中解析 sessions.usage 结果。
    优先 stdout；遍历所有平衡括号 JSON 块，取**最后一个**符合 sessions.usage 形态的 dict，
    避免 stderr 插件日志里含「首个 {」导致误解析、与其它 JSON 窜台。
    """
    candidates: list[dict] = []
    for chunk in (stdout or "", stderr or ""):
        for slice_ in _iter_balanced_json_slices(chunk):
            try:
                obj = json.loads(slice_)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                candidates.append(obj)
    for obj in reversed(candidates):
        coerced = _coerce_sessions_usage_dict(obj)
        if coerced is not None:
            return coerced
    return None


def _gateway_call_sessions_usage_json(*, start_date: str, end_date: str, limit: int) -> tuple[dict | None, str | None]:
    """
    调用本机 Gateway 的 sessions.usage（与 ClawPanel WebSocket 请求同源）。
    需网关在线；可选 OPENCLAW_GATEWAY_TOKEN / OPENCLAW_GATEWAY_PASSWORD / OPENCLAW_GATEWAY_URL。
    """
    params = json.dumps({"startDate": start_date, "endDate": end_date, "limit": limit}, separators=(",", ":"))
    timeout_sec = max(25, min(120, int(os.environ.get("OPENCLAW_ADMIN_USAGE_GATEWAY_TIMEOUT", "90"))))
    args: list[str] = [
        "openclaw",
        "gateway",
        "call",
        "sessions.usage",
        "--json",
        "--params",
        params,
        "--timeout",
        str(timeout_sec * 1000),
    ]
    token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "").strip()
    pwd = os.environ.get("OPENCLAW_GATEWAY_PASSWORD", "").strip()
    url = os.environ.get("OPENCLAW_GATEWAY_URL", "").strip()
    if not token or not url:
        try:
            cfg = read_config()
            gw = cfg.get("gateway") if isinstance(cfg.get("gateway"), dict) else {}
            if not token:
                auth = gw.get("auth") if isinstance(gw.get("auth"), dict) else {}
                tok = auth.get("token")
                if isinstance(tok, str) and tok.strip():
                    token = tok.strip()
            if not url:
                remote = gw.get("remote") if isinstance(gw.get("remote"), dict) else {}
                u = remote.get("url")
                if isinstance(u, str) and u.strip():
                    url = u.strip()
        except Exception:
            pass
    if token:
        args.extend(["--token", token])
    if pwd:
        args.extend(["--password", pwd])
    if url:
        args.extend(["--url", url])
    r = run_command(args, timeout=timeout_sec + 15)
    out = (r.get("stdout") or "").strip()
    err = (r.get("stderr") or "").strip()
    parsed = _parse_sessions_usage_from_cli_streams(stdout=out, stderr=err)
    if parsed is not None:
        return parsed, None
    if not r.get("ok"):
        msg = (err or out or "gateway call 失败").strip()
        return None, msg[:2500]
    return None, "未能从 openclaw gateway call 输出中解析有效的 sessions.usage JSON（已忽略非用量片段）"


def _usage_gateway_call_disabled() -> bool:
    v = os.environ.get("OPENCLAW_ADMIN_USAGE_GATEWAY", "").strip().lower()
    return v in ("0", "false", "off", "skip", "no")


def _gateway_call_sessions_usage_with_retries(
    *, start_date: str, end_date: str, limit: int
) -> tuple[dict | None, str | None]:
    n = max(1, min(4, int(os.environ.get("OPENCLAW_ADMIN_USAGE_GATEWAY_RETRIES", "2"))))
    last: str | None = None
    for i in range(n):
        su, serr = _gateway_call_sessions_usage_json(start_date=start_date, end_date=end_date, limit=limit)
        if su is not None:
            return su, None
        last = serr
        if i + 1 < n and serr:
            low = serr.lower()
            if "gateway connect" in low or "1006" in low or "gateway closed" in low or "timeout" in low:
                time.sleep(min(1.2, 0.35 * (i + 1)))
                continue
        break
    return None, last


def _zero_usage_totals_dict() -> dict:
    return {
        "input": 0,
        "output": 0,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": 0,
        "totalCost": 0.0,
        "inputCost": 0.0,
        "outputCost": 0.0,
        "cacheReadCost": 0.0,
        "cacheWriteCost": 0.0,
        "missingCostEntries": 0,
    }


def _build_local_store_sessions_usage_payload(
    *,
    start_date: str,
    end_date: str,
    limit: int,
    usage_session_rows: list[dict],
    usage_model_rows: list[dict],
    token_total_sum: int,
) -> dict:
    """
    网关不可用时，用 sessions.json 已有字段拼出与前端 ClawPanel 结构兼容的用量对象，
    避免整页「Gateway 报错」；费用/消息/按日等为 0 或未统计。
    """
    lim = max(1, min(200, int(limit)))
    empty = _zero_usage_totals_dict()
    totals = {**empty, "totalTokens": max(0, int(token_total_sum))}
    sessions: list[dict] = []
    for row in (usage_session_rows or [])[:lim]:
        key = (row.get("sessionKey") or row.get("sessionKeyShort") or "") or "—"
        mref = row.get("modelRef") if isinstance(row.get("modelRef"), str) else ""
        mref = mref.strip() or "—"
        prov, mname = "", mref
        if "/" in mref:
            a, b = mref.split("/", 1)
            prov, mname = a.strip(), b.strip() or mref
        tt = row.get("totalTokens")
        tti = int(tt) if isinstance(tt, int) and tt >= 0 else 0
        usage_inner = {
            "totalTokens": tti,
            "totalCost": 0.0,
            "messageCounts": {"total": 0, "user": 0, "assistant": 0, "errors": 0},
            "modelUsage": (
                [{"provider": prov or None, "model": mname or None, "tokens": tti, "cost": 0, "count": 1}]
                if mname and mname != "—"
                else []
            ),
        }
        sessions.append(
            {
                "key": key,
                "sessionId": None,
                "agentId": None,
                "channel": None,
                "model": mref,
                "modelProvider": prov or None,
                "usage": usage_inner,
            }
        )
    by_model: list[dict] = []
    for mr in (usage_model_rows or [])[:20]:
        ref = (mr.get("modelRef") if isinstance(mr.get("modelRef"), str) else "") or "未知"
        prov2, mod2 = "", ref
        if "/" in ref:
            x, y = ref.split("/", 1)
            prov2, mod2 = x.strip(), (y.strip() or ref)
        tok = int(mr.get("totalTokens") or 0)
        cnt = int(mr.get("sessionCount") or 0)
        by_model.append(
            {
                "model": mod2,
                "provider": prov2 or None,
                "count": cnt,
                "totals": {**empty, "totalTokens": tok, "totalCost": 0.0},
            }
        )
    aggregates = {
        "messages": {"total": 0, "user": 0, "assistant": 0, "errors": 0},
        "tools": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
        "byModel": by_model,
        "byProvider": [],
        "byAgent": [],
        "byChannel": [],
        "daily": [],
    }
    return {
        "updatedAt": int(time.time() * 1000),
        "startDate": start_date,
        "endDate": end_date,
        "sessions": sessions,
        "totals": totals,
        "aggregates": aggregates,
    }


def _empty_sessions_usage_payload(*, start_date: str, end_date: str) -> dict:
    """与 ClawPanel 同结构的空用量（仅界面占位，无第二套 UI）。"""
    z = _zero_usage_totals_dict()
    return {
        "updatedAt": int(time.time() * 1000),
        "startDate": start_date,
        "endDate": end_date,
        "sessions": [],
        "totals": dict(z),
        "aggregates": {
            "messages": {"total": 0, "user": 0, "assistant": 0, "errors": 0},
            "tools": {"totalCalls": 0, "uniqueTools": 0, "tools": []},
            "byModel": [],
            "byProvider": [],
            "byAgent": [],
            "byChannel": [],
            "daily": [],
        },
    }


def _sessions_usage_is_effectively_empty(su: object) -> bool:
    """
    网关 sessions.usage 在选定日期内无 jsonl 活动时，常返回结构合法但 sessions=[]、Token 全 0。
    此时若本地 sessions 仍有数据，应回退到 sessions.json 合成，避免界面「全空」。
    """
    if not isinstance(su, dict):
        return True
    sess = su.get("sessions")
    n = len(sess) if isinstance(sess, list) else 0
    if n > 0:
        return False
    tot = su.get("totals") if isinstance(su.get("totals"), dict) else {}
    try:
        tok = int(tot.get("totalTokens") or 0)
    except (TypeError, ValueError):
        tok = 0
    return tok <= 0


def build_usage_snapshot(*, usage_days: int = 7, usage_limit: int = 20) -> dict:
    """
    使用情况：始终返回 ClawPanel 同构的 sessionsUsage（网关优先；否则由 sessions.json 填充；再无则空壳）。
    前端只渲染单一用量界面。
    """
    usage_days = max(1, min(366, int(usage_days)))
    usage_limit = max(1, min(200, int(usage_limit)))
    out: dict = {
        "sessionsPath": str(SESSION_STORE_PATH),
        "configPath": str(CONFIG_PATH),
        "gatewayService": SERVICE_NAME,
        "primaryRoute": "",
        "sessionsCount": 0,
        "providersCount": 0,
        "modelsCount": 0,
        "sessionsFileBytes": None,
        "configFileBytes": None,
        "openclawHomeFreeBytes": None,
        "openclawHomeTotalBytes": None,
        "latestSessionTouchMs": None,
        "gatewayActiveSince": None,
        "usageDays": usage_days,
        "usageLimit": usage_limit,
    }
    primary = ""
    try:
        config = read_config()
        primary = ((config.get("agents") or {}).get("defaults", {}).get("model", {}) or {}).get("primary") or ""
        primary = primary.strip() if isinstance(primary, str) else ""
        out["primaryRoute"] = primary
        provs = (config.get("models") or {}).get("providers")
        if isinstance(provs, dict):
            out["providersCount"] = len(provs)
            mc = 0
            for _pk, block in provs.items():
                if isinstance(block, dict) and isinstance(block.get("models"), list):
                    mc += sum(1 for m in block["models"] if isinstance(m, dict))
            out["modelsCount"] = mc
    except Exception:
        pass

    session_rows: list[dict] = []
    model_list: list[dict] = []
    token_sum = 0
    sess_n = 0
    latest_ms: int | None = None
    store = _read_session_store()
    if isinstance(store, dict):
        sess_n = len(store)
        out["sessionsCount"] = sess_n
        by_model: dict[str, dict] = {}
        for sk, raw in store.items():
            if not isinstance(raw, dict):
                continue
            for key in ("updatedAt", "updated_at", "lastActivityAt", "lastSeenAt"):
                v = raw.get(key)
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    iv = int(v)
                    if iv > 1_000_000_000_000:
                        ts_ms = iv
                    elif iv > 946684800:
                        ts_ms = iv * 1000
                    else:
                        continue
                    latest_ms = ts_ms if latest_ms is None else max(latest_ms, ts_ms)
                elif isinstance(v, str) and v.strip().isdigit():
                    iv = int(v.strip())
                    if iv > 1_000_000_000_000:
                        ts_ms = iv
                    elif iv > 946684800:
                        ts_ms = iv * 1000
                    else:
                        continue
                    latest_ms = ts_ms if latest_ms is None else max(latest_ms, ts_ms)

            tt = _usage_parse_int_field(raw.get("totalTokens"))
            if tt is not None and tt >= 0:
                token_sum += tt
            ctx = _usage_parse_int_field(raw.get("contextTokens"))
            mref = _usage_session_effective_ref(raw, primary)
            sks = sk if isinstance(sk, str) else str(sk)
            session_rows.append(
                {
                    "sessionKey": sks,
                    "sessionKeyShort": (sks[:64] + "…") if len(sks) > 64 else sks,
                    "sessionLabel": _session_key_label(sks),
                    "modelRef": mref,
                    "totalTokens": tt,
                    "contextTokens": ctx,
                }
            )
            agg = by_model.setdefault(mref, {"totalTokens": 0, "sessionCount": 0})
            agg["sessionCount"] += 1
            if tt is not None and tt >= 0:
                agg["totalTokens"] += tt

        out["latestSessionTouchMs"] = latest_ms
        session_rows.sort(key=lambda r: (r.get("totalTokens") or 0), reverse=True)
        session_rows = session_rows[:120]
        model_list = [
            {"modelRef": ref, "totalTokens": ag["totalTokens"], "sessionCount": ag["sessionCount"]}
            for ref, ag in by_model.items()
        ]
        model_list.sort(key=lambda r: (r["totalTokens"], r["sessionCount"]), reverse=True)
        model_list = model_list[:50]

    if SESSION_STORE_PATH.exists():
        try:
            out["sessionsFileBytes"] = SESSION_STORE_PATH.stat().st_size
        except OSError:
            pass
    if CONFIG_PATH.exists():
        try:
            out["configFileBytes"] = CONFIG_PATH.stat().st_size
        except OSError:
            pass
    try:
        home = CONFIG_PATH.expanduser().resolve().parent
        du = shutil.disk_usage(home)
        out["openclawHomeFreeBytes"] = du.free
        out["openclawHomeTotalBytes"] = du.total
    except Exception:
        pass
    st = run_command(
        ["systemctl", "show", SERVICE_NAME, "-p", "ActiveEnterTimestamp", "--value"],
        timeout=5,
    )
    if st.get("ok") and (st.get("stdout") or "").strip():
        out["gatewayActiveSince"] = (st.get("stdout") or "").strip()
    sd, ed = _usage_utc_date_range(usage_days)
    if _usage_gateway_call_disabled():
        su = None
    else:
        su, _serr = _gateway_call_sessions_usage_with_retries(
            start_date=sd, end_date=ed, limit=usage_limit
        )
    local_ok = sess_n > 0 or bool(session_rows)
    if su is not None and not _sessions_usage_is_effectively_empty(su):
        out["sessionsUsage"] = su
    elif local_ok:
        out["sessionsUsage"] = _build_local_store_sessions_usage_payload(
            start_date=sd,
            end_date=ed,
            limit=usage_limit,
            usage_session_rows=session_rows,
            usage_model_rows=model_list,
            token_total_sum=token_sum,
        )
    elif su is not None:
        out["sessionsUsage"] = su
    else:
        out["sessionsUsage"] = _empty_sessions_usage_payload(start_date=sd, end_date=ed)

    out["usageDays"] = usage_days
    out["usageLimit"] = usage_limit
    return out


# —— 用量快照：按 (days,limit) 多槽内存缓存（与 UI「今天/7天/30天」三档对应）+ 后台定时刷新 ——
_ADMIN_USAGE_BG_INTERVAL_SEC = max(60, _env_int("OPENCLAW_ADMIN_USAGE_BG_INTERVAL_SEC", 300))
_ADMIN_USAGE_CACHE_LOCK = threading.Lock()
# (days, limit) -> {"usage": dict, "updatedAtMs": int}
_ADMIN_USAGE_CACHES: dict[tuple[int, int], dict] = {}


def _usage_norm_days_limit(days: int, limit: int) -> tuple[int, int]:
    d = max(1, min(366, int(days)))
    lim = max(1, min(200, int(limit)))
    return (d, lim)


def _usage_background_preset_keys() -> list[tuple[int, int]]:
    """与管理端用量 Tab 一致：今天=1、7 天、30 天，limit=20。"""
    lim = 20
    return [(1, lim), (7, lim), (30, lim)]


def refresh_usage_snapshot_cache_entry(days: int, limit: int) -> dict:
    """完整构建并写入对应 (days,limit) 槽；返回 usage 字典。"""
    usage = build_usage_snapshot(usage_days=days, usage_limit=limit)
    key = _usage_norm_days_limit(days, limit)
    at_ms = int(time.time() * 1000)
    with _ADMIN_USAGE_CACHE_LOCK:
        _ADMIN_USAGE_CACHES[key] = {"usage": usage, "updatedAtMs": at_ms}
    return usage


def _get_usage_cache_row(days: int, limit: int) -> dict | None:
    key = _usage_norm_days_limit(days, limit)
    with _ADMIN_USAGE_CACHE_LOCK:
        row = _ADMIN_USAGE_CACHES.get(key)
    if not row or not isinstance(row.get("usage"), dict):
        return None
    return row


def usage_snapshot_http_payload(*, days: int, limit: int, force: bool) -> dict:
    """供 GET /api/usage/snapshot 使用；返回含 usageCachedAt / usageFromCache。"""
    if force:
        usage = refresh_usage_snapshot_cache_entry(days, limit)
        row = _get_usage_cache_row(days, limit) or {}
        at_ms = int(row.get("updatedAtMs") or int(time.time() * 1000))
        return {
            "ok": True,
            "usage": usage,
            "usageCachedAt": at_ms,
            "usageFromCache": False,
        }
    row = _get_usage_cache_row(days, limit)
    if row is not None:
        return {
            "ok": True,
            "usage": row["usage"],
            "usageCachedAt": int(row.get("updatedAtMs") or 0),
            "usageFromCache": True,
        }
    usage = refresh_usage_snapshot_cache_entry(days, limit)
    row2 = _get_usage_cache_row(days, limit) or {}
    at_ms = int(row2.get("updatedAtMs") or int(time.time() * 1000))
    return {
        "ok": True,
        "usage": usage,
        "usageCachedAt": at_ms,
        "usageFromCache": False,
    }


def _usage_background_refresh_loop() -> None:
    while True:
        time.sleep(_ADMIN_USAGE_BG_INTERVAL_SEC)
        for days, lim in _usage_background_preset_keys():
            try:
                refresh_usage_snapshot_cache_entry(days, lim)
            except Exception:
                pass


def start_usage_background_refresher() -> None:
    if os.environ.get("OPENCLAW_ADMIN_USAGE_BG_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return

    def _bootstrap() -> None:
        for days, lim in _usage_background_preset_keys():
            try:
                refresh_usage_snapshot_cache_entry(days, lim)
            except Exception:
                pass
        _usage_background_refresh_loop()

    t = threading.Thread(target=_bootstrap, name="openclaw-admin-usage-bg", daemon=True)
    t.start()


# —— 配置 + 会话自动/手动备份（默认每小时、保留 7 天）——
BACKUP_ID_RE = re.compile(r"^\d{8}_\d{6}(_[0-9]+)?$")
ADMIN_BACKUP_RETENTION_DAYS = max(1, _env_int("OPENCLAW_ADMIN_BACKUP_RETENTION_DAYS", 7))
ADMIN_BACKUP_INTERVAL_SEC = max(60, _env_int("OPENCLAW_ADMIN_BACKUP_INTERVAL_SEC", 3600))


def _admin_backup_dir() -> Path:
    raw = os.environ.get("OPENCLAW_ADMIN_BACKUP_DIR", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (CONFIG_PATH.parent / "openclaw-model-admin-backups").resolve()


def prune_old_backups(backup_root: Path, *, keep_days: int) -> int:
    """按目录 mtime 删除超过 keep_days 的备份子目录。"""
    if not backup_root.is_dir():
        return 0
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for child in list(backup_root.iterdir()):
        if not child.is_dir():
            continue
        if not BACKUP_ID_RE.match(child.name):
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def create_admin_backup(*, reason: str = "manual") -> dict:
    """
    将 openclaw.json 与 sessions.json（若存在）复制到带时间戳的子目录，并清理过期备份。
    """
    backup_root = _admin_backup_dir()
    backup_root.mkdir(parents=True, exist_ok=True)
    base = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backup_root / base
    if dest.exists():
        i = 2
        while (backup_root / f"{base}_{i}").exists():
            i += 1
        dest = backup_root / f"{base}_{i}"
    dest.mkdir(parents=False)
    meta = {
        "id": dest.name,
        "reason": (reason or "manual")[:120],
        "createdAt": datetime.now().isoformat(timespec="seconds"),
        "configPath": str(CONFIG_PATH),
        "sessionsPath": str(SESSION_STORE_PATH),
    }
    has_cfg = False
    has_sess = False
    with _config_lock_shared():
        if CONFIG_PATH.is_file():
            shutil.copy2(CONFIG_PATH, dest / "openclaw.json")
            has_cfg = True
        if SESSION_STORE_PATH.is_file():
            shutil.copy2(SESSION_STORE_PATH, dest / "sessions.json")
            has_sess = True
    (dest / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pruned = prune_old_backups(backup_root, keep_days=ADMIN_BACKUP_RETENTION_DAYS)
    return {
        "id": dest.name,
        "path": str(dest),
        "pruned": pruned,
        "hasConfig": has_cfg,
        "hasSessions": has_sess,
        "backupDir": str(backup_root),
        "retentionDays": ADMIN_BACKUP_RETENTION_DAYS,
    }


def list_admin_backups() -> dict:
    backup_root = _admin_backup_dir()
    rows: list[dict] = []
    if backup_root.is_dir():
        for child in sorted(backup_root.iterdir(), key=lambda p: p.name, reverse=True):
            if not child.is_dir() or not BACKUP_ID_RE.match(child.name):
                continue
            cfg = child / "openclaw.json"
            ses = child / "sessions.json"
            meta_path = child / "meta.json"
            reason = "—"
            if meta_path.is_file():
                try:
                    mj = json.loads(meta_path.read_text(encoding="utf-8"))
                    if isinstance(mj, dict) and isinstance(mj.get("reason"), str):
                        reason = mj["reason"]
                except Exception:
                    pass
            try:
                mtime = child.stat().st_mtime
                created = datetime.fromtimestamp(mtime).isoformat(timespec="seconds")
            except OSError:
                created = "—"
            rows.append(
                {
                    "id": child.name,
                    "hasConfig": cfg.is_file(),
                    "hasSessions": ses.is_file(),
                    "reason": reason,
                    "createdAt": created,
                }
            )
    return {
        "backups": rows,
        "backupDir": str(backup_root),
        "retentionDays": ADMIN_BACKUP_RETENTION_DAYS,
        "intervalSec": ADMIN_BACKUP_INTERVAL_SEC,
    }


def restore_admin_backup(backup_id: str) -> dict:
    bid = (backup_id or "").strip()
    if not BACKUP_ID_RE.match(bid):
        raise ValueError("无效的备份 id")
    src = _admin_backup_dir() / bid
    if not src.is_dir():
        raise ValueError("备份不存在")
    cfg_src = src / "openclaw.json"
    if not cfg_src.is_file():
        raise ValueError("备份中缺少 openclaw.json")
    cfg_text = cfg_src.read_text(encoding="utf-8")
    json.loads(cfg_text)
    cfg_out = cfg_text if cfg_text.endswith("\n") else cfg_text + "\n"
    ses_src = src / "sessions.json"
    ses_text: str | None = None
    if ses_src.is_file():
        ses_text = ses_src.read_text(encoding="utf-8")
        json.loads(ses_text)
        if not ses_text.endswith("\n"):
            ses_text = ses_text + "\n"
    pre = create_admin_backup(reason="pre-restore")
    with _config_lock_exclusive():
        previous_raw = CONFIG_PATH.read_text(encoding="utf-8") if CONFIG_PATH.is_file() else "{}\n"
        try:
            CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic_write_utf8(CONFIG_PATH, cfg_out)
            validation = validate_config_file(use_cache=False)
            if not validation["valid"]:
                _atomic_write_utf8(CONFIG_PATH, previous_raw)
                brief = "；".join(validation["issues"][:3]) if validation["issues"] else (validation.get("raw") or "")
                raise ValueError(f"恢复后配置未通过校验，已回滚：{brief}")
            _prime_cli_validate_cache(validation)
        except ValueError:
            raise
        except Exception:
            _atomic_write_utf8(CONFIG_PATH, previous_raw)
            raise
    if ses_text is not None:
        SESSION_STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_utf8(SESSION_STORE_PATH, ses_text)
    ctx = sync_all_sessions_context_tokens_from_config(read_config())
    cleared = clear_session_thinking_levels()
    return {
        "restoredFrom": bid,
        "preRestoreBackupId": pre.get("id"),
        "sessionContextSync": ctx,
        "sessionThinkingCleared": cleared,
    }


def _backup_scheduler_loop() -> None:
    while True:
        time.sleep(ADMIN_BACKUP_INTERVAL_SEC)
        try:
            create_admin_backup(reason="scheduled")
        except Exception:
            pass


def start_admin_backup_scheduler() -> None:
    if os.environ.get("OPENCLAW_ADMIN_BACKUP_DISABLE", "").strip().lower() in ("1", "true", "yes", "on"):
        return
    t = threading.Thread(target=_backup_scheduler_loop, name="openclaw-admin-backup", daemon=True)
    t.start()


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
    if ":openclaw-weixin:direct:" in session_key:
        return "微信私聊"
    if ":openclaw-weixin:" in session_key:
        return "微信会话"
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
    if ":openclaw-weixin:direct:" in session_key:
        return 39
    if ":telegram:slash:" in session_key:
        return 35
    if ":telegram:group:" in session_key or ":telegram:channel:" in session_key:
        return 30
    if session_key == MAIN_SESSION_KEY:
        return 25
    if ":telegram:" in session_key:
        return 20
    if ":openclaw-weixin:" in session_key:
        return 19
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
    srl = raw.get("reasoningLevel")
    session_reasoning = (
        srl.strip()
        if isinstance(srl, str) and srl.strip() in ("on", "off")
        else None
    )
    selv = raw.get("elevatedLevel")
    session_elevated = (
        selv.strip()
        if isinstance(selv, str) and selv.strip() in ("off", "full")
        else None
    )
    return {
        "sessionKey": key,
        "sessionLabel": _session_key_label(key),
        "modelRef": ref,
        "viaOverride": via,
        "thinkingLevel": tl,
        "configThinking": cfg_th,
        "statusThink": status_think,
        "statusThinkSource": src,
        "sessionReasoningLevel": session_reasoning,
        "sessionElevatedLevel": session_elevated,
    }


def pick_telegram_direct_session_key(store: dict) -> str | None:
    """与 build_session_previews 一致：updatedAt 最新的 telegram 私聊键。"""
    td = [k for k, v in store.items() if isinstance(v, dict) and ":telegram:direct:" in k]
    if not td:
        return None
    return max(td, key=lambda k: int((store[k] or {}).get("updatedAt") or 0))


def pick_weixin_direct_session_key(store: dict) -> str | None:
    """openclaw-weixin 插件：私聊会话键形如 agent:*:openclaw-weixin:direct:*@im.wechat"""
    wx = [k for k, v in store.items() if isinstance(v, dict) and ":openclaw-weixin:direct:" in k]
    if not wx:
        return None
    return max(wx, key=lambda k: int((store[k] or {}).get("updatedAt") or 0))


def set_session_model_override(
    session_key: str,
    *,
    clear: bool = False,
    model_ref: str | None = None,
) -> dict:
    """
    写入 sessions.json：设置或清除单条会话的 modelOverride（与 OpenClaw 全路径 ref 一致），并去掉 providerOverride。
    clear=True 时移除覆盖，会话回落到全局路由 primary。
    """
    sk = (session_key or "").strip()
    if not sk or sk in ("global", "unknown"):
        return {"ok": False, "error": "无效的 sessionKey"}
    if not SESSION_STORE_PATH.exists():
        return {"ok": False, "error": "sessions.json 不存在", "path": str(SESSION_STORE_PATH)}

    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"读取 sessions.json 失败: {e}"}

    if not isinstance(store, dict):
        return {"ok": False, "error": "sessions 根节点应为对象"}

    if sk not in store or not isinstance(store.get(sk), dict):
        return {"ok": False, "error": "会话不存在，请先在对应渠道发起过对话"}

    raw = store[sk]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = SESSION_STORE_PATH.parent / f"sessions.json.bak.modelov-{stamp}"
    try:
        shutil.copy2(SESSION_STORE_PATH, backup_path)
    except Exception as e:
        return {"ok": False, "error": f"备份 sessions.json 失败: {e}"}

    try:
        if clear:
            raw.pop("modelOverride", None)
            raw.pop("providerOverride", None)
            mode = "cleared"
        else:
            ref_in = model_ref.strip() if isinstance(model_ref, str) else ""
            if not ref_in or "/" not in ref_in:
                return {"ok": False, "error": "modelRef 须为非空的 provider/model"}
            ref_n = normalize_model_ref_provider_lower(ref_in)
            raw["modelOverride"] = ref_n
            raw.pop("providerOverride", None)
            mode = "set"
        raw["updatedAt"] = int(time.time() * 1000)
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        try:
            shutil.copy2(backup_path, SESSION_STORE_PATH)
        except Exception:
            pass
        return {"ok": False, "error": f"写入 sessions.json 失败（已尝试从备份恢复）: {e}"}

    ctx_sync = sync_all_sessions_context_tokens_from_config(read_config())

    return {
        "ok": True,
        "sessionKey": sk,
        "mode": mode,
        "modelOverride": None if clear else raw.get("modelOverride"),
        "backupPath": str(backup_path),
        "sessionContextSync": ctx_sync,
    }


def set_session_behavior(session_key: str, payload: dict) -> dict:
    """
    写入 sessions.json：单条会话的 reasoningLevel / elevatedLevel。
    JSON null 表示删除该键（跟随全局 agents.defaults + admin 偏好）。
    """
    sk = (session_key or "").strip()
    if not sk or sk in ("global", "unknown"):
        return {"ok": False, "error": "无效的 sessionKey"}
    if not SESSION_STORE_PATH.exists():
        return {"ok": False, "error": "sessions.json 不存在", "path": str(SESSION_STORE_PATH)}

    try:
        store = json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        return {"ok": False, "error": f"读取 sessions.json 失败: {e}"}

    if not isinstance(store, dict):
        return {"ok": False, "error": "sessions 根节点应为对象"}

    if sk not in store or not isinstance(store.get(sk), dict):
        return {"ok": False, "error": "会话不存在，请先在对应渠道发起过对话"}

    if "reasoningLevel" not in payload and "elevatedLevel" not in payload:
        return {"ok": False, "error": "请至少提供 reasoningLevel 或 elevatedLevel（可用 null 表示跟随全局）"}

    raw = store[sk]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = SESSION_STORE_PATH.parent / f"sessions.json.bak.behavior-{stamp}"
    try:
        shutil.copy2(SESSION_STORE_PATH, backup_path)
    except Exception as e:
        return {"ok": False, "error": f"备份 sessions.json 失败: {e}"}

    try:
        if "reasoningLevel" in payload:
            v = payload["reasoningLevel"]
            if v is None:
                raw.pop("reasoningLevel", None)
            elif isinstance(v, str) and v in ("on", "off"):
                raw["reasoningLevel"] = v
            else:
                return {"ok": False, "error": "reasoningLevel 须为 on、off 或 null"}
        if "elevatedLevel" in payload:
            v = payload["elevatedLevel"]
            if v is None:
                raw.pop("elevatedLevel", None)
            elif isinstance(v, str) and v in ("off", "full"):
                raw["elevatedLevel"] = v
            else:
                return {"ok": False, "error": "elevatedLevel 须为 off、full 或 null"}
        raw["updatedAt"] = int(time.time() * 1000)
        SESSION_STORE_PATH.write_text(json.dumps(store, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as e:
        try:
            shutil.copy2(backup_path, SESSION_STORE_PATH)
        except Exception:
            pass
        return {"ok": False, "error": f"写入 sessions.json 失败（已尝试从备份恢复）: {e}"}

    ctx_sync = sync_all_sessions_context_tokens_from_config(read_config())
    return {
        "ok": True,
        "sessionKey": sk,
        "reasoningLevel": raw.get("reasoningLevel"),
        "elevatedLevel": raw.get("elevatedLevel"),
        "backupPath": str(backup_path),
        "sessionContextSync": ctx_sync,
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
    """并排预览：电报私聊、微信私聊（openclaw-weixin）、网页主会话（agent:main:main）。"""
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

    wk = pick_weixin_direct_session_key(store)
    if wk:
        p = _session_entry_to_preview(config, primary, wk, store[wk])
        p["previewTitle"] = "微信私聊"
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
        "sessionReasoningLevel": None,
        "sessionElevatedLevel": None,
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
            "sessionReasoningLevel": None,
            "sessionElevatedLevel": None,
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


def _resolve_provider_api_key(provider: dict) -> str:
    raw = provider.get("apiKey")
    if not isinstance(raw, str):
        return ""
    s = raw.strip()
    if not s:
        return ""
    if _looks_like_filesystem_path_str(s):
        try:
            home = _openclaw_home_dir()
            raw_path = Path(s).expanduser()
            path = raw_path.resolve() if raw_path.is_absolute() else (home / s.lstrip("/")).resolve()
        except OSError:
            return ""
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8").strip()
            except OSError:
                return ""
        return ""
    return s


def _model_test_http_timeout() -> float:
    """与 ClawPanel test_model 一致默认 30s；可用环境变量覆盖。"""
    return max(10.0, float(os.environ.get("OPENCLAW_MODEL_ADMIN_TEST_TIMEOUT", "30")))


def _normalize_base_url_for_model_test(raw: str) -> str:
    """对齐 ClawPanel（Rust normalize_base_url / dev-api _normalizeBaseUrl）：去尾缀、Ollama 11434 补 /v1。"""
    base = (raw or "").strip().rstrip("/")
    for suf in (
        "/api/chat",
        "/api/generate",
        "/api/tags",
        "/api",
        "/chat/completions",
        "/completions",
        "/responses",
        "/messages",
        "/models",
    ):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    base = base.rstrip("/")
    if base.endswith(":11434"):
        return f"{base}/v1"
    return base


def _normalize_model_api_type_for_test(raw: str) -> str:
    """对齐 ClawPanel normalize_model_api_type：openai-responses 在探测时走 chat/completions。"""
    t = (raw or "").strip().lower()
    if t in ("anthropic", "anthropic-messages"):
        return "anthropic-messages"
    if t in ("google-gemini", "google-generative-ai"):
        return "google-gemini"
    if t in (
        "openai",
        "openai-completions",
        "openai-responses",
        "openai-codex-responses",
        "",
    ):
        return "openai-completions"
    return "openai-completions"


def _prepare_test_base_url(base: str, api_cat: str) -> str:
    b = _normalize_base_url_for_model_test(base)
    if api_cat == "anthropic-messages":
        if not b.rstrip("/").endswith("/v1"):
            b = b.rstrip("/") + "/v1"
    return b


def _http_post_json(url: str, headers: dict[str, str], payload: dict, timeout: float) -> tuple[int, str]:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    h = dict(headers)
    req = Request(url, data=body, method="POST", headers=h)
    try:
        with urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            text = resp.read().decode("utf-8", errors="replace")
            return int(code), text
    except HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return int(e.code), text
    except URLError as e:
        raise RuntimeError(str(e.reason) if getattr(e, "reason", None) else str(e)) from e


def _extract_api_error_message(text: str, status: int) -> str:
    try:
        v = json.loads(text)
        if isinstance(v, dict):
            err = v.get("error")
            if isinstance(err, dict):
                m = err.get("message")
                if isinstance(m, str) and m.strip():
                    return m.strip()
            m2 = v.get("message")
            if isinstance(m2, str) and m2.strip():
                return m2.strip()
    except Exception:
        pass
    return f"HTTP {status}"


def _extract_reply_preview_clawpanel(text: str, api_cat: str) -> str:
    try:
        v = json.loads(text)
    except Exception:
        return "（模型已响应）"
    if not isinstance(v, dict):
        return "（模型已响应）"
    if api_cat == "anthropic-messages":
        content = v.get("content")
        if isinstance(content, list):
            parts = [
                b.get("text")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            joined = "".join(x for x in parts if isinstance(x, str))
            if joined:
                return (joined[:50] + "…") if len(joined) > 50 else joined
    if api_cat == "google-gemini":
        cands = v.get("candidates")
        if isinstance(cands, list) and cands:
            parts = (
                (cands[0] or {}).get("content", {}).get("parts")
                if isinstance(cands[0], dict)
                else None
            )
            if isinstance(parts, list) and parts:
                t = parts[0].get("text") if isinstance(parts[0], dict) else None
                if isinstance(t, str) and t:
                    return (t[:50] + "…") if len(t) > 50 else t
    ch0 = v.get("choices", [{}])[0] if isinstance(v.get("choices"), list) else {}
    if isinstance(ch0, dict):
        msg = ch0.get("message")
        if isinstance(msg, dict):
            c = msg.get("content")
            if isinstance(c, str) and c.strip():
                return (c[:50] + "…") if len(c) > 50 else c
            rc = msg.get("reasoning_content")
            if isinstance(rc, str) and rc.strip():
                s = "[reasoning] " + rc
                return (s[:50] + "…") if len(s) > 50 else s
    out = v.get("output")
    if isinstance(out, dict):
        tx = out.get("text")
        if isinstance(tx, str) and tx.strip():
            return (tx[:50] + "…") if len(tx) > 50 else tx
    return "（模型已响应）"


def clawpanel_style_model_test(
    base_url: str, api_key: str, model_id: str, eff_api_raw: str, timeout: float
) -> tuple[float, str, str]:
    """
    与 ClawPanel test_model 对齐：非流式短请求，测整段往返耗时。
    返回 (elapsed_seconds, detail_string, outcome) ，outcome 为 ok | soft | hard。
    """
    from urllib.parse import quote

    api_cat = _normalize_model_api_type_for_test(eff_api_raw)
    base = _prepare_test_base_url(base_url, api_cat)
    t0 = time.perf_counter()
    if api_cat == "anthropic-messages":
        url = f"{base.rstrip('/')}/messages"
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 16,
        }
        hdrs = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            hdrs["x-api-key"] = api_key
        status, text = _http_post_json(url, hdrs, payload, timeout)
    elif api_cat == "google-gemini":
        mid_q = quote(model_id, safe="")
        qkey = quote(api_key or "", safe="")
        url = f"{base.rstrip('/')}/models/{mid_q}:generateContent?key={qkey}"
        payload = {"contents": [{"role": "user", "parts": [{"text": "Hi"}]}]}
        status, text = _http_post_json(
            url, {"Content-Type": "application/json"}, payload, timeout
        )
    else:
        url = f"{base.rstrip('/')}/chat/completions"
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 16,
            "stream": False,
        }
        hdrs = {"Content-Type": "application/json"}
        if api_key:
            hdrs["Authorization"] = f"Bearer {api_key}"
        status, text = _http_post_json(url, hdrs, payload, timeout)

    elapsed = time.perf_counter() - t0

    if status in (401, 403):
        return elapsed, _extract_api_error_message(text, status), "hard"
    if 200 <= status < 300:
        return elapsed, _extract_reply_preview_clawpanel(text, api_cat), "ok"
    msg = _extract_api_error_message(text, status)
    soft = (
        f"⚠ 连接正常（API 返回 HTTP {status}，部分模型对简单测试不兼容，不影响实际使用）"
        f" · {msg[:200]}"
    )
    return elapsed, soft, "soft"


def measure_model_test(ref: str) -> dict:
    """模型连通性测试：逻辑对齐 ClawPanel test_model，返回整段请求耗时（秒）。"""
    if not isinstance(ref, str) or "/" not in ref.strip():
        return {"ok": False, "error": "无效的模型 ref"}
    ref_n = normalize_model_ref_provider_lower(ref.strip())
    p_name, _, mid = ref_n.partition("/")
    mid = mid.strip()
    if not p_name or not mid:
        return {"ok": False, "error": "无效的模型 ref"}
    try:
        config = read_config()
    except Exception as e:
        return {"ok": False, "error": f"读取配置失败: {e}"}
    provs = config.get("models", {}).get("providers", {})
    if not isinstance(provs, dict):
        return {"ok": False, "error": "配置中无 models.providers"}
    pk = resolve_provider_key_in_provs(provs, p_name)
    if not pk:
        return {"ok": False, "error": f"找不到供应商「{p_name}」"}
    provider = provs.get(pk)
    if not isinstance(provider, dict):
        return {"ok": False, "error": "供应商块无效"}
    if pk in BUILTIN_PROVIDERS or (provider.get("auth") or "").strip().lower() == "oauth":
        return {"ok": False, "error": "内置或 OAuth 供应商无法在管理端直测，请使用带 API Key 的自定义线路"}
    base_url = (provider.get("baseUrl") or "").strip()
    if not base_url.startswith("http"):
        return {"ok": False, "error": "无效的 baseUrl（需 http(s)）"}
    api_key = _resolve_provider_api_key(provider)
    if not api_key:
        return {"ok": False, "error": "未配置 API Key（或密钥文件不可读）"}
    model_entry = None
    for m in provider.get("models", []) or []:
        if isinstance(m, dict) and str(m.get("id", "")).strip() == mid:
            model_entry = m
            break
    if not model_entry:
        return {"ok": False, "error": f"找不到模型「{mid}」"}
    p_api = provider.get("api") if isinstance(provider.get("api"), str) else ""
    m_api = model_entry.get("api") if isinstance(model_entry.get("api"), str) else ""
    eff = (
        m_api.strip() if m_api.strip() else None
    ) or (
        p_api.strip() if isinstance(p_api, str) and p_api.strip() else None
    ) or "openai-completions"
    timeout = _model_test_http_timeout()
    test_cat = _normalize_model_api_type_for_test(eff)
    try:
        elapsed, detail, outcome = clawpanel_style_model_test(
            base_url, api_key, mid, eff, timeout
        )
    except Exception as e:
        return {"ok": False, "error": str(e), "api": eff, "testCategory": test_cat}
    sec = round(elapsed, 3)
    if outcome == "hard":
        return {"ok": False, "error": detail, "api": eff, "testCategory": test_cat}
    out: dict = {
        "ok": True,
        "seconds": sec,
        "api": eff,
        "testCategory": test_cat,
    }
    if outcome == "soft":
        out["softWarning"] = detail
    else:
        out["replyPreview"] = detail
    return out


def _http_get_raw(url: str, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    h = dict(headers)
    req = Request(url, method="GET", headers=h)
    try:
        with urlopen(req, timeout=timeout) as resp:
            code = getattr(resp, "status", None) or resp.getcode()
            text = resp.read().decode("utf-8", errors="replace")
            return int(code), text
    except HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        return int(e.code), text
    except URLError as e:
        raise RuntimeError(str(e.reason) if getattr(e, "reason", None) else str(e)) from e


def _parse_remote_models_list_json(text: str) -> list[str]:
    """解析 OpenAI 兼容 GET /v1/models 或 Ollama OpenAI 兼容列表 JSON。"""
    ids: list[str] = []
    try:
        v = json.loads(text)
    except Exception:
        return ids
    if not isinstance(v, dict):
        return ids
    data = v.get("data")
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                mid = item.get("id")
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
    models = v.get("models")
    if isinstance(models, list):
        for item in models:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name")
                if isinstance(mid, str) and mid.strip():
                    ids.append(mid.strip())
    # 去重保序
    seen: set[str] = set()
    out: list[str] = []
    for x in ids:
        k = x.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(x)
    return out


def _infer_default_api_for_new_models(provider: dict) -> str:
    p_api = provider.get("api") if isinstance(provider.get("api"), str) else ""
    if isinstance(p_api, str) and p_api.strip() and p_api.strip() != "per-model":
        return p_api.strip()
    apis: list[str] = []
    for m in provider.get("models", []) or []:
        if not isinstance(m, dict):
            continue
        a = m.get("api")
        if isinstance(a, str) and a.strip():
            apis.append(a.strip())
    if not apis:
        return "openai-completions"
    u = list(dict.fromkeys(apis))
    return u[0] if len(u) == 1 else "openai-completions"


def _fetch_remote_model_ids_for_provider(provider_raw: str) -> tuple[str, dict, list[str], set[str]]:
    """
    校验供应商并 GET OpenAI 兼容 /v1/models，返回 (pk, provider_dict, remote_ids, existing_id_set)。
    不写入配置。
    """
    p_try = normalize_provider_id((provider_raw or "").strip())
    if not p_try:
        raise ValueError("缺少供应商名称")
    if p_try in BUILTIN_PROVIDERS:
        raise ValueError("内置供应商不支持远程拉取列表")
    config = read_config()
    provs = config.setdefault("models", {}).setdefault("providers", {})
    if not isinstance(provs, dict):
        provs = {}
        config["models"]["providers"] = provs
    pk = resolve_provider_key_in_provs(provs, p_try) or p_try
    if pk not in provs:
        raise ValueError("未找到该供应商")
    provider = provs[pk]
    if not isinstance(provider, dict):
        raise ValueError("供应商块无效")
    if (provider.get("auth") or "").strip().lower() == "oauth":
        raise ValueError("OAuth 供应商不支持在管理端拉取模型列表")
    base_url = (provider.get("baseUrl") or "").strip()
    if not base_url.startswith("http"):
        raise ValueError("无效的 baseUrl（需 http(s)）")
    if isinstance(base_url, str) and base_url.strip().startswith("("):
        raise ValueError("合成展示行不可拉取列表")
    eff_api = _infer_default_api_for_new_models(provider)
    api_cat = _normalize_model_api_type_for_test(eff_api)
    if api_cat == "google-gemini":
        raise ValueError("Gemini 协议暂不支持远程拉取，请手动添加模型")
    api_key = _resolve_provider_api_key(provider)
    if not api_key and api_cat == "anthropic-messages":
        raise ValueError("未配置 API Key（或密钥文件不可读）")
    base = _prepare_test_base_url(base_url, api_cat)
    list_url = f"{base.rstrip('/')}/models"
    timeout = max(15.0, min(120.0, _model_test_http_timeout() * 2))
    hdrs: dict[str, str] = {}
    if api_cat == "anthropic-messages":
        hdrs["anthropic-version"] = "2023-06-01"
        if api_key:
            hdrs["x-api-key"] = api_key
    else:
        hdrs["Content-Type"] = "application/json"
        if api_key:
            hdrs["Authorization"] = f"Bearer {api_key}"
    status, text = _http_get_raw(list_url, hdrs, timeout)
    if status == 404 and api_cat == "anthropic-messages":
        raise ValueError("远端未提供模型列表接口（HTTP 404），请手动添加模型")
    if not (200 <= status < 300):
        raise ValueError(_extract_api_error_message(text, status))
    remote_ids = _parse_remote_models_list_json(text)
    if not remote_ids:
        raise ValueError("远端返回的模型列表为空或无法解析")
    models_list = provider.get("models") or []
    if not isinstance(models_list, list):
        models_list = []
    existing = {
        str(m.get("id", "")).strip()
        for m in models_list
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("id", "").strip()
    }
    return pk, provider, remote_ids, existing


def fetch_provider_remote_models_preview(provider_raw: str) -> dict:
    """
    拉取远端模型 id 列表（不修改配置）。返回弹窗内展示用的 remoteIds + 已在库中的 id（默认勾选）。
    未出现在远端列表中的本地模型不在弹窗内，应用同步时不受影响。
    """
    pk, _provider, remote_ids, existing = _fetch_remote_model_ids_for_provider(provider_raw)
    preview_cap = 1000
    shown = remote_ids[:preview_cap]
    in_config_remote = [mid for mid in shown if mid in existing]
    remote_set_full = set(remote_ids)
    local_only = sorted(mid for mid in existing if mid not in remote_set_full)
    return {
        "provider": pk,
        "remoteCount": len(remote_ids),
        "remoteIds": shown,
        "remoteIdsTruncated": len(remote_ids) > preview_cap,
        "inConfigRemoteIds": in_config_remote,
        "localOnlyCount": len(local_only),
        "localOnlySample": local_only[:8],
        "message": (
            f"远端 {len(remote_ids)} 个模型，下列展示 {len(shown)} 个。"
            "已在配置中的默认勾选；取消勾选将从配置中移除。"
            + (f"另有 {len(local_only)} 个本地模型不在远端列表中，不会被此弹窗改动。" if local_only else "")
        ),
    }


def _purge_agents_defaults_model_ref(config: dict, p_name: str, m_id: str) -> None:
    """从 agents.defaults.models 中移除 provider/model 条目（与 /api/model/delete 一致）。"""
    mid = (m_id or "").strip()
    if not mid:
        return
    ref = normalize_model_ref_provider_lower(f"{p_name}/{mid}")
    am_del = config.get("agents", {}).get("defaults", {}).get("models", {})
    if not isinstance(am_del, dict):
        return
    if ref in am_del:
        del am_del[ref]
        return
    for k in list(am_del.keys()):
        if isinstance(k, str) and normalize_model_ref_provider_lower(k) == ref:
            del am_del[k]
            break


def _normalize_id_list_for_sync(raw: object, max_len: int, field: str) -> list[str]:
    if not isinstance(raw, list):
        raise ValueError(f"{field} 须为数组")
    out: list[str] = []
    seen: set[str] = set()
    for x in raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        k = s.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(s)
        if len(out) > max_len:
            raise ValueError(f"{field} 过长（上限 {max_len}）")
    return out


def sync_provider_remote_model_selection(provider_raw: str, remote_ids_raw: object, selected_ids_raw: object) -> dict:
    """
    按弹窗勾选同步：remoteIds 为本次远端列表范围；selectedIds 为保留在配置中的 id。
    - 在 remoteIds 内未勾选的：从供应商模型列表删除并清理 agents.defaults.models。
    - 勾选的：保留；若尚不存在则追加。
    - 不在 remoteIds 中的本地模型：原样保留（不受本次操作影响）。
    """
    max_n = 2000
    remote_scope = _normalize_id_list_for_sync(remote_ids_raw, max_n, "remoteIds")
    selected = _normalize_id_list_for_sync(selected_ids_raw, max_n, "selectedIds")
    if not remote_scope:
        raise ValueError("remoteIds 不能为空")
    remote_set = set(remote_scope)
    for s in selected:
        if s not in remote_set:
            raise ValueError("勾选项必须都属于本次提交的 remoteIds")

    p_try = normalize_provider_id((provider_raw or "").strip())
    if not p_try:
        raise ValueError("缺少供应商名称")
    if p_try in BUILTIN_PROVIDERS:
        raise ValueError("内置供应商不可同步")
    config = read_config()
    provs = config.setdefault("models", {}).setdefault("providers", {})
    if not isinstance(provs, dict):
        provs = {}
        config["models"]["providers"] = provs
    pk = resolve_provider_key_in_provs(provs, p_try) or p_try
    if pk not in provs:
        raise ValueError("未找到该供应商")
    provider = provs[pk]
    if not isinstance(provider, dict):
        raise ValueError("供应商块无效")

    models_list = provider.get("models") or []
    if not isinstance(models_list, list):
        models_list = []
    id_to_model: dict[str, dict] = {}
    for m in models_list:
        if isinstance(m, dict):
            mid = str(m.get("id", "")).strip()
            if mid:
                id_to_model[mid] = m
    old_ids = set(id_to_model.keys())
    old_order = [
        str(m.get("id", "")).strip()
        for m in models_list
        if isinstance(m, dict) and str(m.get("id", "")).strip()
    ]

    final_order: list[str] = []
    seen_order: set[str] = set()
    for m in models_list:
        if not isinstance(m, dict):
            continue
        mid = str(m.get("id", "")).strip()
        if not mid or mid in remote_set:
            continue
        if mid not in seen_order:
            seen_order.add(mid)
            final_order.append(mid)
    for mid in selected:
        if mid not in seen_order:
            seen_order.add(mid)
            final_order.append(mid)

    to_add_ids = [mid for mid in final_order if mid not in old_ids]
    max_append = 500
    if len(to_add_ids) > max_append:
        raise ValueError(f"单次最多新增 {max_append} 个模型，请减少勾选")

    new_ids_set = set(final_order)
    removed_ids = sorted(old_ids - new_ids_set)
    if not removed_ids and not to_add_ids and old_order == final_order:
        return {
            "provider": pk,
            "added": [],
            "removed": [],
            "addedCount": 0,
            "removedCount": 0,
            "message": "无变更",
        }

    for mid in removed_ids:
        _purge_agents_defaults_model_ref(config, pk, mid)

    eff_api = _infer_default_api_for_new_models(provider)
    prev_reasoning = True
    if models_list:
        om = next((x for x in models_list if isinstance(x, dict)), None)
        if isinstance(om, dict) and "reasoning" in om:
            prev_reasoning = bool(om.get("reasoning"))
    use_api = eff_api

    new_models: list[dict] = []
    added: list[str] = []
    for mid in final_order:
        if mid in id_to_model:
            new_models.append(id_to_model[mid])
        else:
            new_m: dict = {
                "id": mid,
                "name": mid,
                "reasoning": prev_reasoning,
                "input": ["text"],
                "contextWindow": 200000,
                "maxTokens": 32768,
            }
            if isinstance(use_api, str) and use_api.strip() and use_api.strip() != "per-model":
                new_m["api"] = use_api.strip()
            new_models.append(new_m)
            added.append(mid)

    provider["models"] = new_models
    if not new_models:
        del provs[pk]

    save_meta = save_config_with_validation(config)
    ctx = sync_all_sessions_context_tokens_from_config(config)
    cleared = clear_session_thinking_levels()
    return {
        "provider": pk,
        "added": added,
        "removed": removed_ids,
        "addedCount": len(added),
        "removedCount": len(removed_ids),
        "migrations": save_meta.get("migrations", []),
        "sessionContextSync": ctx,
        "sessionThinkingCleared": cleared,
    }


def add_models_to_provider_by_ids(provider_raw: str, ids_raw: object) -> dict:
    """
    将指定模型 id 追加写入 models.providers.*.models（跳过已存在的 id）。
    """
    p_try = normalize_provider_id((provider_raw or "").strip())
    if not p_try:
        raise ValueError("缺少供应商名称")
    if p_try in BUILTIN_PROVIDERS:
        raise ValueError("内置供应商不可通过此接口批量添加")
    if not isinstance(ids_raw, list):
        raise ValueError("ids 须为非空数组")
    want_ids: list[str] = []
    seen_lower: set[str] = set()
    for x in ids_raw:
        if not isinstance(x, str):
            continue
        s = x.strip()
        if not s:
            continue
        k = s.lower()
        if k in seen_lower:
            continue
        seen_lower.add(k)
        want_ids.append(s)
    if not want_ids:
        raise ValueError("请至少选择一个模型 id")
    config = read_config()
    provs = config.setdefault("models", {}).setdefault("providers", {})
    if not isinstance(provs, dict):
        provs = {}
        config["models"]["providers"] = provs
    pk = resolve_provider_key_in_provs(provs, p_try) or p_try
    if pk not in provs:
        raise ValueError("未找到该供应商")
    provider = provs[pk]
    if not isinstance(provider, dict):
        raise ValueError("供应商块无效")
    models_list = provider.setdefault("models", [])
    if not isinstance(models_list, list):
        models_list = []
        provider["models"] = models_list
    existing = {
        str(m.get("id", "")).strip()
        for m in models_list
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("id", "").strip()
    }
    eff_api = _infer_default_api_for_new_models(provider)
    prev_reasoning = True
    if models_list:
        om = next((x for x in models_list if isinstance(x, dict)), None)
        if isinstance(om, dict) and "reasoning" in om:
            prev_reasoning = bool(om.get("reasoning"))
    use_api = eff_api
    added: list[str] = []
    max_new = 500
    for mid in want_ids:
        if len(added) >= max_new:
            break
        if mid in existing:
            continue
        new_m: dict = {
            "id": mid,
            "name": mid,
            "reasoning": prev_reasoning,
            "input": ["text"],
            "contextWindow": 200000,
            "maxTokens": 32768,
        }
        if isinstance(use_api, str) and use_api.strip() and use_api.strip() != "per-model":
            new_m["api"] = use_api.strip()
        models_list.append(new_m)
        existing.add(mid)
        added.append(mid)
    if not added:
        return {
            "provider": pk,
            "added": [],
            "addedCount": 0,
            "message": "所选模型均已存在于配置中",
        }
    save_meta = save_config_with_validation(config)
    ctx = sync_all_sessions_context_tokens_from_config(config)
    return {
        "provider": pk,
        "added": added,
        "addedCount": len(added),
        "migrations": save_meta.get("migrations", []),
        "sessionContextSync": ctx,
    }


def _parse_openclaw_v_output(text: str) -> str | None:
    m = re.search(r"OpenClaw\s+([\w][\w.-]*)", text or "")
    return m.group(1) if m else None


def _get_openclaw_cli_version_raw() -> tuple[str | None, str | None]:
    try:
        cp = subprocess.run(
            ["openclaw", "-V"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        out = (cp.stdout or "") + (cp.stderr or "")
        ver = _parse_openclaw_v_output(out)
        if ver:
            return ver, None
        if cp.returncode != 0:
            return None, (out.strip() or "openclaw -V 非零退出")[:500]
        return None, "无法解析 openclaw -V 输出"
    except FileNotFoundError:
        return None, "未找到 openclaw 命令（PATH）"
    except subprocess.TimeoutExpired:
        return None, "openclaw -V 超时"
    except Exception as e:
        return None, str(e)


def _get_openclaw_cli_version_cached() -> tuple[str | None, str | None]:
    global _OPENCLAW_CLI_VER_CACHE
    now = time.time()
    ts = float(_OPENCLAW_CLI_VER_CACHE.get("ts") or 0)
    v = _OPENCLAW_CLI_VER_CACHE.get("version")
    e = _OPENCLAW_CLI_VER_CACHE.get("error")
    if (now - ts) < float(_OPENCLAW_CLI_VER_CACHE_TTL_SEC) and (v is not None or e is not None):
        return v if isinstance(v, str) else None, e if isinstance(e, str) else None
    v2, e2 = _get_openclaw_cli_version_raw()
    _OPENCLAW_CLI_VER_CACHE = {"ts": now, "version": v2, "error": e2}
    return v2, e2


def _npm_view_openclaw_version() -> tuple[str | None, str | None]:
    try:
        cp = subprocess.run(
            ["npm", "view", "openclaw", "version"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        raw = (cp.stdout or "").strip()
        if cp.returncode != 0:
            err = ((cp.stderr or "").strip() or raw or "npm view 失败")[:800]
            return None, err
        if not raw:
            return None, "npm 无输出"
        line = raw.splitlines()[0].strip()
        return line, None
    except FileNotFoundError:
        return None, "未找到 npm 命令"
    except subprocess.TimeoutExpired:
        return None, "npm view 超时"
    except Exception as e:
        return None, str(e)


def _oc_version_tuple(v: str) -> tuple[int, ...]:
    t: list[int] = []
    for part in (v or "").strip().split("."):
        if part.isdigit():
            t.append(int(part))
        else:
            break
    return tuple(t)


def _openclaw_versions_compare(current: str | None, latest: str | None) -> str:
    """返回 'older' | 'same' | 'newer' | 'unknown'。"""
    if not current or not latest:
        return "unknown"
    tc, tl = _oc_version_tuple(current), _oc_version_tuple(latest)
    if not tc or not tl:
        return "unknown"
    if tl > tc:
        return "older"
    if tc > tl:
        return "newer"
    return "same"


def openclaw_version_check_payload(*, force_refresh_latest: bool) -> dict:
    global _OPENCLAW_LATEST_CACHE
    now = time.time()
    current, cur_err = _get_openclaw_cli_version_cached()
    ts = float(_OPENCLAW_LATEST_CACHE.get("ts") or 0)
    stale = (now - ts) >= float(_OPENCLAW_VERSION_CHECK_INTERVAL_SEC)
    need_fetch = force_refresh_latest or ts <= 0.0 or stale

    if need_fetch:
        lat, lat_err = _npm_view_openclaw_version()
        _OPENCLAW_LATEST_CACHE = {"ts": now, "latest": lat, "error": lat_err}
    else:
        lat = _OPENCLAW_LATEST_CACHE.get("latest")
        lat_err = _OPENCLAW_LATEST_CACHE.get("error")

    cache_ts = float(_OPENCLAW_LATEST_CACHE.get("ts") or now)
    checked_iso = datetime.fromtimestamp(cache_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rel = _openclaw_versions_compare(current, lat if isinstance(lat, str) else None)
    update_available = rel == "older"
    is_latest: bool | None
    if lat_err and not lat:
        is_latest = None
    elif rel == "older":
        is_latest = False
    elif rel in ("same", "newer"):
        is_latest = True
    else:
        is_latest = None

    return {
        "ok": True,
        "currentVersion": current,
        "currentError": cur_err,
        "latestVersion": lat,
        "latestError": lat_err if isinstance(lat_err, str) else None,
        "checkedAt": checked_iso,
        "fromCache": not need_fetch,
        "compare": rel,
        "updateAvailable": update_available,
        "isLatest": is_latest,
    }


def _parse_openclaw_update_stdout(stdout: str) -> dict | None:
    raw = (stdout or "").strip()
    if not raw:
        return None
    i = raw.find("{")
    if i < 0:
        return None
    try:
        return json.loads(raw[i:])
    except json.JSONDecodeError:
        return None


def run_openclaw_builtin_update(*, no_restart: bool) -> dict:
    global _OPENCLAW_LATEST_CACHE, _OPENCLAW_CLI_VER_CACHE
    if _env_truthy("OPENCLAW_ADMIN_OPENCLAW_UPDATE_DISABLE"):
        return {"ok": False, "error": "已禁用网页端一键更新（OPENCLAW_ADMIN_OPENCLAW_UPDATE_DISABLE）"}
    timeout_sec = max(60, _env_int("OPENCLAW_ADMIN_OPENCLAW_UPDATE_TIMEOUT_SEC", 1800))
    cmd = ["openclaw", "update", "--yes", "--json"]
    if no_restart:
        cmd.append("--no-restart")
    try:
        cp = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"openclaw update 超过 {timeout_sec}s 仍未结束"}
    except FileNotFoundError:
        return {"ok": False, "error": "未找到 openclaw 命令"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

    parsed = _parse_openclaw_update_stdout(cp.stdout or "")
    if cp.returncode == 0 and isinstance(parsed, dict):
        _OPENCLAW_LATEST_CACHE = {"ts": 0.0, "latest": None, "error": None}
        _OPENCLAW_CLI_VER_CACHE = {"ts": 0.0, "version": None, "error": None}
        return {"ok": True, "result": parsed, "stderr": (cp.stderr or "").strip()[:2000] or None}
    err_tail = ((cp.stderr or "").strip() or (cp.stdout or "").strip())[:2000]
    return {
        "ok": False,
        "error": err_tail or f"退出码 {cp.returncode}",
        "result": parsed,
        "exitCode": cp.returncode,
    }


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

    # 标准供应商（api 展示：优先磁盘 provider.api；否则若所有子模型 api 一致则显示该值；多协议并存则 per-model）
    for p_name, p in providers.items():
        p_api_raw = p.get("api") if isinstance(p.get("api"), str) else ""
        p_api_raw = p_api_raw.strip()
        models_list = p.get("models", []) or []
        per_model_apis: list[str] = []
        for m in models_list:
            if not isinstance(m, dict):
                continue
            ma = m.get("api")
            if isinstance(ma, str) and ma.strip():
                per_model_apis.append(ma.strip())
        distinct = list(dict.fromkeys(per_model_apis))
        if p_api_raw:
            prov_api_disp = p_api_raw
        elif len(distinct) == 1:
            prov_api_disp = distinct[0]
        elif len(distinct) > 1:
            prov_api_disp = "per-model"
        else:
            prov_api_disp = ""
        provider_items.append(
            {
                "name": p_name,
                "baseUrl": p.get("baseUrl", ""),
                "auth": p.get("auth", "api-key"),
                "api": prov_api_disp,
                "modelCount": len(models_list),
            }
        )
        for m in p.get("models", []):
            ref = f"{p_name}/{m['id']}"
            seen_refs.add(ref)
            m_entry = configured_models.get(ref)
            if not isinstance(m_entry, dict):
                m_entry = configured_models.get(normalize_model_ref_provider_lower(ref))
            m_entry = m_entry if isinstance(m_entry, dict) else {}
            m_params = m_entry.get("params", {}) if isinstance(m_entry.get("params"), dict) else {}
            th = _thinking_str_from_params_raw(m_params.get("thinking"))
            _m_api = m.get("api")
            _p_api = p.get("api") if isinstance(p.get("api"), str) else ""
            _eff_api = (
                _m_api.strip() if isinstance(_m_api, str) and _m_api.strip() else None
            ) or (_p_api.strip() if _p_api.strip() else None) or "openai-completions"
            model_items.append(
                {
                    "ref": ref,
                    "provider": p_name,
                    "id": m["id"],
                    "name": m.get("name", m["id"]),
                    "thinking": th,
                    "inputs": m.get("input", []),
                    "contextWindow": m.get("contextWindow"),
                    "maxTokens": m.get("maxTokens"),
                    "api": _eff_api,
                    "configured": True,
                }
            )

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
            model_items.append(
                {
                    "ref": ref,
                    "provider": p_name,
                    "id": m_id,
                    "name": f"{m_id} (内置)",
                    "thinking": th_in,
                    "inputs": ["text"],
                    "contextWindow": 1000000,
                    "maxTokens": 128000,
                    "api": "oauth",
                    "configured": True,
                    "elevated": params.get("elevated"),
                }
            )
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
    oc_cli_ver, _oc_cli_err = _get_openclaw_cli_version_cached()
    return {
        "panelMeta": {
            "version": PANEL_META_VERSION,
            "sessionsPath": str(SESSION_STORE_PATH),
            "openclawCliVersion": oc_cli_ver,
        },
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


def build_probe_report(state: dict) -> dict:
    """
    一键诊断：多检查项（ClawPanel 式结构化 health — 逐项 label/ok/detail，便于扩展与展示）。
    """
    checks: list[dict] = []
    ts = datetime.now().strftime("%H:%M:%S")

    alerts = state.get("alerts") if isinstance(state.get("alerts"), list) else []
    read_failed = any(
        isinstance(a, dict) and "配置读取失败" in str(a.get("msg") or "") for a in alerts
    )
    checks.append(
        {
            "id": "config_load",
            "label": "配置文件",
            "ok": not read_failed,
            "detail": str(CONFIG_PATH) if not read_failed else "openclaw.json 不可读",
        }
    )

    if not read_failed:
        live_val = validate_config_file(use_cache=False)
        cv_live = bool(live_val.get("valid"))
        issues_live = live_val.get("issues") if isinstance(live_val.get("issues"), list) else []
        issue_preview = "；".join(str(x) for x in issues_live[:2]) if issues_live else ""
        checks.append(
            {
                "id": "config_validate",
                "label": "结构 / CLI 校验",
                "ok": cv_live,
                "detail": "通过" if cv_live else (issue_preview or f"{len(issues_live)} 项问题"),
                "issues": issues_live[:12],
            }
        )
    else:
        checks.append(
            {
                "id": "config_validate",
                "label": "结构 / CLI 校验",
                "ok": False,
                "detail": "跳过（配置未加载）",
                "issues": [],
            }
        )

    gw = bool(state.get("gatewayActive"))
    checks.append(
        {
            "id": "gateway",
            "label": "网关连通",
            "ok": gw,
            "detail": "判定在线" if gw else "离线或不可达",
        }
    )

    st = run_command(["systemctl", "is-active", SERVICE_NAME], timeout=5)
    if st.get("ok") and (st.get("stdout") or "").strip():
        act = (st.get("stdout") or "").strip()
        checks.append(
            {
                "id": "systemd",
                "label": "systemd 单元",
                "ok": act == "active",
                "detail": f"{SERVICE_NAME}: {act}",
            }
        )

    if not gw:
        pr = run_command(["ss", "-ltn"], timeout=5)
        out = pr.get("stdout") or ""
        listen = ":18789" in out or "18789" in out
        checks.append(
            {
                "id": "port_18789",
                "label": "端口 18789",
                "ok": listen,
                "detail": "ss 可见监听" if listen else "未见监听（网关离线时对照）",
            }
        )

    hu = _gateway_health_url()
    if hu:
        hops = _probe_http_url(hu)
        detail = hu if len(hu) < 96 else hu[:93] + "…"
        checks.append(
            {
                "id": "gateway_health_url",
                "label": "健康检查 URL",
                "ok": hops,
                "detail": detail,
            }
        )

    if SESSION_STORE_PATH.exists():
        try:
            json.loads(SESSION_STORE_PATH.read_text(encoding="utf-8"))
            checks.append(
                {
                    "id": "sessions_json",
                    "label": "会话库",
                    "ok": True,
                    "detail": "sessions.json 可解析",
                }
            )
        except Exception as e:
            checks.append(
                {
                    "id": "sessions_json",
                    "label": "会话库",
                    "ok": False,
                    "detail": str(e)[:120],
                }
            )
    else:
        checks.append(
            {
                "id": "sessions_json",
                "label": "会话库",
                "ok": True,
                "optional": True,
                "detail": "尚无 sessions.json（新装或未产生会话）",
            }
        )

    prim = (state.get("primary") or "").strip() if isinstance(state.get("primary"), str) else ""
    prim_ok = bool(prim and "/" in prim)
    checks.append(
        {
            "id": "primary_route",
            "label": "主模型路由",
            "ok": prim_ok,
            "detail": prim if prim else "未设置 primary",
        }
    )

    fb = state.get("fallbacks") if isinstance(state.get("fallbacks"), list) else []
    filled_fb = [x for x in fb if isinstance(x, str) and x.strip()]
    fb_ok = all("/" in x.strip() for x in filled_fb)
    checks.append(
        {
            "id": "fallbacks_route",
            "label": "备用路由",
            "ok": len(filled_fb) == 0 or fb_ok,
            "detail": f"{len(filled_fb)} 条" if filled_fb else "无（可选）",
        }
    )

    mr = state.get("mainSessionRoute") if isinstance(state.get("mainSessionRoute"), dict) else {}
    matches = bool(mr.get("matchesPrimary", True))
    # 会话内单独选模型是支持的能力，不应在诊断里标红「不一致」误导用户
    if matches:
        ms_detail = "与全局 primary 一致"
    else:
        ms_detail = (
            "本会话已单独指定模型（modelOverride 等），与全局 primary 不同 — 属正常用法，不是故障"
        )
    checks.append(
        {
            "id": "main_session_model",
            "label": "网页主会话模型",
            "ok": True,
            "detail": ms_detail,
        }
    )

    r_cli = run_command(["openclaw", "--version"], timeout=10)
    if r_cli.get("ok"):
        vlines = (r_cli.get("stdout") or "").strip().splitlines()
        line0 = vlines[0].strip()[:100] if vlines else "ok"
        checks.append({"id": "openclaw_cli", "label": "OpenClaw CLI", "ok": True, "detail": line0})
    else:
        checks.append(
            {
                "id": "openclaw_cli",
                "label": "OpenClaw CLI",
                "ok": True,
                "optional": True,
                "detail": "未检测到命令（校验依赖 CLI 时安装 openclaw）",
            }
        )

    mem_health_path = _openclaw_home_dir() / "memory" / "health" / "last-health.json"
    if mem_health_path.is_file():
        try:
            mh = json.loads(mem_health_path.read_text(encoding="utf-8"))
            m_ok = bool(mh.get("ok", True))
            status = str(mh.get("health_status") or ("ok" if m_ok else "error"))[:40]
            checks.append(
                {
                    "id": "memory_health_report",
                    "label": "长期记忆（定时报告）",
                    "ok": m_ok,
                    "detail": status,
                }
            )
        except Exception as e:
            checks.append(
                {
                    "id": "memory_health_report",
                    "label": "长期记忆（定时报告）",
                    "ok": False,
                    "detail": str(e)[:80],
                }
            )
    else:
        checks.append(
            {
                "id": "memory_health_report",
                "label": "长期记忆（定时报告）",
                "ok": True,
                "optional": True,
                "detail": "无报告文件（可选）",
            }
        )

    providers = state.get("providers") if isinstance(state.get("providers"), list) else []
    provider_results: dict[str, bool] = {}
    for p in providers:
        name = p.get("name")
        if not isinstance(name, str) or not name:
            continue
        url = p.get("baseUrl", "")
        url_s = url if isinstance(url, str) else ""
        ok_p = probe_provider(name, url_s)
        provider_results[name] = ok_p
        checks.append(
            {
                "id": f"provider:{name}",
                "label": f"供应商 · {name}",
                "ok": ok_p,
                "detail": "可达 / 认证就绪" if ok_p else "端点不可达或内置项未就绪",
            }
        )

    def _is_failed(c: dict) -> bool:
        if c.get("optional"):
            return False
        return not c.get("ok", False)

    failed = [c for c in checks if _is_failed(c)]
    passed = sum(1 for c in checks if c.get("ok"))
    all_ok = len(failed) == 0

    return {
        "timestamp": ts,
        "checks": checks,
        "results": provider_results,
        "summary": {
            "all_ok": all_ok,
            "passed": passed,
            "total": len(checks),
            "failed_count": len(failed),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=HTTPStatus.OK, *, no_cache: bool = False):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if no_cache:
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
            self.send_header("Pragma", "no-cache")
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
        # 避免浏览器长期缓存单页，改 UI 后用户强刷即可看到新版本
        if path.name == "index.html":
            self.send_header("Cache-Control", "no-store, max-age=0, must-revalidate")
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
        elif path == "/api/gateway/logs":
            try:
                q = parse_qs(urlparse(self.path).query)
                raw_n = (q.get("lines") or ["200"])[0]
                try:
                    nlines = int(raw_n)
                except (TypeError, ValueError):
                    nlines = 200
                data = fetch_gateway_logs(lines=nlines)
                self._send_json({"ok": bool(data.get("ok")), **data})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e), "lines": [], "text": ""}, status=HTTPStatus.BAD_REQUEST)
        elif path == "/api/usage/snapshot":
            try:
                q = parse_qs(urlparse(self.path).query)
                try:
                    days = int((q.get("days") or ["7"])[0])
                except (TypeError, ValueError):
                    days = 7
                try:
                    lim = int((q.get("limit") or ["20"])[0])
                except (TypeError, ValueError):
                    lim = 20
                raw_force = (q.get("force") or q.get("rebuild") or ["0"])[0]
                force = str(raw_force).strip().lower() in ("1", "true", "yes", "on")
                self._send_json(usage_snapshot_http_payload(days=days, limit=lim, force=force), no_cache=True)
            except Exception as e:
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    no_cache=True,
                )
        elif path == "/api/backup/list":
            try:
                self._send_json({"ok": True, **list_admin_backups()})
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
        elif path == "/api/openclaw/version-check":
            try:
                q = parse_qs(urlparse(self.path).query)
                raw_force = (q.get("force") or ["0"])[0]
                force = str(raw_force).strip().lower() in ("1", "true", "yes", "on")
                self._send_json(openclaw_version_check_payload(force_refresh_latest=force), no_cache=True)
            except Exception as e:
                self._send_json(
                    {"ok": False, "error": str(e)},
                    status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    no_cache=True,
                )
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length < 0:
            length = 0
        if length > MAX_POST_BODY_BYTES:
            self._send_json(
                {"ok": False, "error": f"请求体过大（上限 {MAX_POST_BODY_BYTES} 字节）"},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return
        raw = self.rfile.read(length) if length > 0 else b""
        if length > 0:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as e:
                self._send_json(
                    {"ok": False, "error": f"请求体不是合法 JSON: {e}"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
            except UnicodeDecodeError as e:
                self._send_json(
                    {"ok": False, "error": f"请求体须为 UTF-8 文本: {e}"},
                    status=HTTPStatus.BAD_REQUEST,
                )
                return
        else:
            payload = {}
        try:
            if path == "/api/openclaw/update":
                no_restart = payload.get("noRestart") is True
                self._send_json(run_openclaw_builtin_update(no_restart=no_restart), no_cache=True)
            elif path == "/api/selection":
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
                # 思考只跟各模型在前端的 params.thinking：清掉所有会话 thinkingLevel，各渠道均走配置
                selection_extra_meta["sessionThinkingCleared"] = clear_session_thinking_levels()
                selection_extra_meta["sessionContextSync"] = sync_all_sessions_context_tokens_from_config(config)
                meta_out = {
                    "migrations": save_meta.get("migrations", []),
                    "sessionSync": session_sync,
                }
                meta_out.update(selection_extra_meta)
                self._send_json({"ok": True, "state": build_state(), "meta": meta_out})
            elif path == "/api/session/model-override":
                sk = payload.get("sessionKey")
                sk = sk.strip() if isinstance(sk, str) else ""
                clear_flag = payload.get("clear") is True
                if clear_flag:
                    meta = set_session_model_override(sk, clear=True)
                else:
                    mr = payload.get("modelRef")
                    mr = mr.strip() if isinstance(mr, str) else ""
                    if not mr:
                        raise ValueError("缺少 modelRef，或传 clear:true 以跟随全局路由")
                    meta = set_session_model_override(sk, clear=False, model_ref=mr)
                if not meta.get("ok"):
                    raise ValueError(meta.get("error") or "写入会话模型失败")
                self._send_json({"ok": True, "state": build_state(), "meta": {"sessionModelOverride": meta}})
            elif path == "/api/session/behavior":
                sk = payload.get("sessionKey")
                sk = sk.strip() if isinstance(sk, str) else ""
                body = {
                    k: payload[k]
                    for k in ("reasoningLevel", "elevatedLevel")
                    if k in payload
                }
                meta = set_session_behavior(sk, body)
                if not meta.get("ok"):
                    raise ValueError(meta.get("error") or "写入会话选项失败")
                self._send_json({"ok": True, "state": build_state(), "meta": {"sessionBehavior": meta}})
            elif path == "/api/model":
                config = read_config()
                raw_pv = payload.get("provider", "")
                raw_pv = raw_pv.strip() if isinstance(raw_pv, str) else ""
                p_name = normalize_provider_id(raw_pv)
                m_id = payload.get("modelId", "").strip() if isinstance(payload.get("modelId"), str) else ""
                if not m_id:
                    raise ValueError("缺少 modelId")
                if p_name not in BUILTIN_PROVIDERS:
                    provs = config.setdefault("models", {}).setdefault("providers", {})
                    if not isinstance(provs, dict):
                        provs = {}
                        config["models"]["providers"] = provs
                    pk = resolve_provider_key_in_provs(provs, p_name)
                    storage_key = pk if pk else p_name
                    p = provs.setdefault(storage_key, {"models": []})
                    # 供应商级 baseUrl/auth/密钥 与同块模型共享；接口类型 api 写入各模型 models[].api（OpenClaw：
                    # model.api ?? provider.api），保存时不再覆盖 p["api"]，避免同供应商下一模型牵连另一模型。
                    # 未写 per-model api 的条目仍回落到磁盘上已有的 provider.api（兼容旧配置）。
                    auth_raw = payload.get("auth")
                    auth_u = auth_raw.strip() if isinstance(auth_raw, str) else "api-key"
                    p["baseUrl"] = payload.get("baseUrl") if isinstance(payload.get("baseUrl"), str) else ""
                    p["auth"] = auth_u or "api-key"
                    if payload.get("apiKey"):
                        p["apiKey"] = payload.get("apiKey")
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
                    if isinstance(old_m, dict):
                        for _k in ("headers", "compat", "cost"):
                            if _k in old_m:
                                new_m[_k] = copy.deepcopy(old_m[_k])
                    api_raw = payload.get("api")
                    if isinstance(api_raw, str) and api_raw.strip():
                        new_m["api"] = api_raw.strip()
                    elif isinstance(old_m, dict):
                        _oa = old_m.get("api")
                        if isinstance(_oa, str) and _oa.strip():
                            new_m["api"] = _oa.strip()
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
                # 不按「仅 modelId 相同」把思考写到主模型：不同供应商同名模型须相互独立。
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
            elif path in ("/api/model/test", "/api/model/ttft"):
                ref = payload.get("ref")
                ref = ref.strip() if isinstance(ref, str) else ""
                if not ref:
                    raise ValueError("缺少 ref")
                out = measure_model_test(ref)
                body: dict = {"ok": bool(out.get("ok"))}
                for k in (
                    "seconds",
                    "error",
                    "api",
                    "testCategory",
                    "softWarning",
                    "replyPreview",
                ):
                    if k in out and out[k] is not None:
                        body[k] = out[k]
                self._send_json(body)
            elif path == "/api/provider/fetch-models":
                p_raw = (payload.get("provider") or "").strip()
                meta = fetch_provider_remote_models_preview(p_raw)
                self._send_json({"ok": True, "meta": {"providerFetchModels": meta}})
            elif path == "/api/provider/add-models":
                p_raw = (payload.get("provider") or "").strip()
                ids_body = payload.get("ids")
                meta_add = add_models_to_provider_by_ids(p_raw, ids_body)
                self._send_json({"ok": True, "state": build_state(), "meta": {"providerAddModels": meta_add}})
            elif path == "/api/provider/sync-remote-models":
                p_raw = (payload.get("provider") or "").strip()
                meta_sync = sync_provider_remote_model_selection(
                    p_raw,
                    payload.get("remoteIds"),
                    payload.get("selectedIds"),
                )
                self._send_json({"ok": True, "state": build_state(), "meta": {"providerSyncRemoteModels": meta_sync}})
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
                report = build_probe_report(state)
                self._send_json({"ok": True, **report})
            elif path == "/api/backup/create":
                reason_raw = payload.get("reason")
                reason = reason_raw.strip() if isinstance(reason_raw, str) else "manual"
                meta_b = create_admin_backup(reason=reason or "manual")
                self._send_json({"ok": True, "meta": {"adminBackup": meta_b}})
            elif path == "/api/backup/restore":
                bid = (payload.get("id") or "").strip()
                if not bid:
                    raise ValueError("缺少备份 id")
                meta_r = restore_admin_backup(bid)
                self._send_json({"ok": True, "state": build_state(), "meta": {"adminRestore": meta_r}})
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
            else:
                self.send_response(HTTPStatus.NOT_FOUND)
                self.end_headers()
        except Exception as e:
            self._send_json({"ok": False, "error": str(e)}, status=HTTPStatus.BAD_REQUEST)

if __name__ == "__main__":
    start_admin_backup_scheduler()
    start_usage_background_refresher()
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
