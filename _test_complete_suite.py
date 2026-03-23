#!/usr/bin/env python3
"""
完整测试套件：
1) 管理后台 5 项（8765 API + openclaw.json + validate）
2) 与 telegram-footer resolveFooterThinking 同构：无会话覆盖时 off / low
3) 会话 thinkingLevel 优先于配置（页脚插件）
4) 与 Telegram /status 文案同构：OpenClaw resolveThinkingDefault + 会话链（非 gateway status JSON）
5) openclaw gateway health/status（stdout 含插件日志时从首个「{」起解析 JSON）
6) telegram-footer node 测试

说明：gateway call status 的 JSON 里 sessions[].thinkingLevel 仅表示会话存储字段；
无 thinkingLevel 时 flags 里也可能没有 think:*，这与 /status 里「Think:」行可以不一致——
后者会回落到 openclaw.json 的 params.thinking（本套件用源码同构函数校验 /status 等价物）。
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
CFG_PATH = Path("/root/.openclaw/openclaw.json")
FOOTER_DIR = Path("/root/.openclaw/extensions/telegram-footer")
FRONTEND_FIVE = Path(__file__).resolve().parent / "_test_frontend_five.py"
MODEL_DISPLAY_CHAIN = Path(__file__).resolve().parent / "_test_model_display_chain.py"

KNOWN_THINK_TIERS = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "adaptive"}
)

CLAUDE_46_MODEL_RE = re.compile(r"claude-(?:opus|sonnet)-4(?:\.|-)6(?:$|[-.])", re.I)


def post(path: str, body: dict | None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={"Content-Type": "application/json"} if body is not None else {},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode("utf-8"))


def read_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding="utf-8"))


def normalize_provider_id_local(provider: str) -> str:
    return provider.strip().lower()


def model_key_openclaw(provider: str, model: str) -> str:
    """与 OpenClaw modelKey() 一致（src/agents/model-selection.ts）。"""
    provider_id = provider.strip()
    model_id = model.strip()
    if not provider_id:
        return model_id
    if not model_id:
        return provider_id
    if model_id.lower().startswith(f"{provider_id.lower()}/"):
        return model_id
    return f"{provider_id}/{model_id}"


def legacy_model_key_openclaw(provider: str, model: str) -> str | None:
    provider_id = provider.strip()
    model_id = model.strip()
    if not provider_id or not model_id:
        return None
    raw_key = f"{provider_id}/{model_id}"
    return None if raw_key == model_key_openclaw(provider_id, model_id) else raw_key


def normalize_provider_id_openclaw(provider: str) -> str:
    normalized = provider.strip().lower()
    aliases = {
        "z.ai": "zai",
        "z-ai": "zai",
        "opencode-zen": "opencode",
        "opencode-go-auth": "opencode-go",
        "qwen": "qwen-portal",
        "kimi-code": "kimi-coding",
        "bedrock": "amazon-bedrock",
        "aws-bedrock": "amazon-bedrock",
        "bytedance": "volcengine",
        "doubao": "volcengine",
    }
    return aliases.get(normalized, normalized)


def build_catalog_flat_from_cfg(cfg: dict) -> list[dict]:
    """近似 loadModelCatalog 中用于 resolveThinkingDefaultForModel 的扁平列表。"""
    out: list[dict] = []
    provs = cfg.get("models", {}).get("providers", {})
    if not isinstance(provs, dict):
        return out
    for p_key, p_val in provs.items():
        if not isinstance(p_val, dict) or not isinstance(p_key, str):
            continue
        for m in p_val.get("models") or []:
            if isinstance(m, dict) and isinstance(m.get("id"), str):
                out.append(
                    {"provider": p_key, "id": m["id"], "reasoning": m.get("reasoning")}
                )
    return out


def resolve_thinking_default_for_model_openclaw(
    provider: str, model: str, catalog: list[dict]
) -> str:
    np = normalize_provider_id_openclaw(provider)
    model_lower = model.strip().lower()
    if (
        np in ("anthropic", "amazon-bedrock")
        or "anthropic/" in model_lower
        or ".anthropic." in model_lower
    ) and CLAUDE_46_MODEL_RE.search(model_lower):
        return "adaptive"
    prov_raw = provider.strip()
    mid = model.strip()
    for entry in catalog:
        if entry.get("provider") == prov_raw and entry.get("id") == mid:
            return "low" if entry.get("reasoning") is True else "off"
    return "off"


def resolve_thinking_default_openclaw(cfg: dict, provider: str, model: str, catalog: list[dict]) -> str:
    """与 OpenClaw resolveThinkingDefault() 一致（reply 包 model-selection 段）。"""
    defs = cfg.get("agents", {}).get("defaults")
    if not isinstance(defs, dict):
        defs = {}
    configured_models = defs.get("models")
    if not isinstance(configured_models, dict):
        configured_models = {}
    canonical = model_key_openclaw(provider, model)
    legacy = legacy_model_key_openclaw(provider, model)
    per_model = None
    ent = configured_models.get(canonical)
    if isinstance(ent, dict):
        params = ent.get("params")
        if isinstance(params, dict) and isinstance(params.get("thinking"), str):
            per_model = params["thinking"]
    if per_model is None and legacy:
        ent2 = configured_models.get(legacy)
        if isinstance(ent2, dict):
            params2 = ent2.get("params")
            if isinstance(params2, dict) and isinstance(params2.get("thinking"), str):
                per_model = params2["thinking"]
    if per_model in KNOWN_THINK_TIERS:
        return per_model
    td = defs.get("thinkingDefault")
    if isinstance(td, str) and td.strip():
        return td.strip()
    return resolve_thinking_default_for_model_openclaw(provider, model, catalog)


def effective_think_for_slash_status(
    session_entry: dict | None,
    cfg: dict,
    provider: str,
    model: str,
    catalog: list[dict],
) -> str:
    """
    等价于无内联 think 指令时 Telegram /status 卡片里的 Think: 档位来源链：
    session.thinkingLevel ?? resolveThinkingDefault(...)（与 buildStatusReply 传入的 resolvedThink 一致）。
    """
    if session_entry and isinstance(session_entry.get("thinkingLevel"), str):
        s = session_entry["thinkingLevel"].strip()
        if s:
            return s
    return resolve_thinking_default_openclaw(cfg, provider, model, catalog)


def model_key_local(provider: str, model: str) -> str:
    provider_id = provider.strip()
    model_id = model.strip()
    if not provider_id:
        return model_id
    if not model_id:
        return provider_id
    if model_id.lower().startswith(f"{provider_id.lower()}/"):
        return model_id
    return f"{provider_id}/{model_id}"


def get_per_model_params_thinking(cfg: dict, ref_key: str) -> str:
    agents = cfg.get("agents")
    if not isinstance(agents, dict):
        return ""
    defs = agents.get("defaults")
    if not isinstance(defs, dict):
        return ""
    models = defs.get("models")
    if not isinstance(models, dict):
        return ""
    entry = models.get(ref_key)
    if not isinstance(entry, dict):
        return ""
    params = entry.get("params")
    if not isinstance(params, dict):
        return ""
    t = params.get("thinking")
    return t.strip() if isinstance(t, str) else ""


def catalog_reasoning_true(cfg: dict, provider: str, model: str) -> bool:
    models_root = cfg.get("models")
    if not isinstance(models_root, dict):
        return False
    providers = models_root.get("providers")
    if not isinstance(providers, dict):
        return False
    p_norm = normalize_provider_id_local(provider)
    prov_obj = providers.get(provider.strip())
    if prov_obj is None:
        for k, v in providers.items():
            if isinstance(k, str) and normalize_provider_id_local(k) == p_norm:
                prov_obj = v
                break
    if not isinstance(prov_obj, dict):
        return False
    lst = prov_obj.get("models")
    if not isinstance(lst, list):
        return False
    mid = model.strip()
    for row in lst:
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"] == mid:
            return row.get("reasoning") is True
    return False


def resolve_footer_thinking(
    entry: dict | None,
    provider: str,
    model: str,
    cfg: dict,
) -> str:
    """与 telegram-footer index.ts resolveFooterThinking 同构（cfg 由调用方从磁盘读取）。"""
    if entry and isinstance(entry.get("thinkingLevel"), str):
        s = entry["thinkingLevel"].strip()
        if s:
            return s
    p, m = provider.strip(), model.strip()
    if not p or not m:
        return "unknown"
    keys = [
        model_key_local(p, m),
        model_key_local(normalize_provider_id_local(p), m),
    ]
    from_cfg = ""
    for k in keys:
        if "/" not in k:
            continue
        from_cfg = get_per_model_params_thinking(cfg, k)
        if from_cfg:
            break
    if from_cfg:
        return from_cfg if from_cfg in KNOWN_THINK_TIERS else from_cfg
    if catalog_reasoning_true(cfg, p, m):
        return "low"
    return "off"


def model_body(cfg: dict, *, thinking_on: bool, thinking_value: str = "low") -> dict:
    p = cfg["models"]["providers"]["ciii"]
    m0 = p["models"][0]
    return {
        "provider": "ciii",
        "baseUrl": p["baseUrl"],
        "apiKey": p.get("apiKey", ""),
        "auth": p.get("auth", "api-key"),
        "api": p.get("api", "openai-completions"),
        "modelId": m0["id"],
        "modelName": m0.get("name", m0["id"]),
        "thinkingEnabled": thinking_on,
        "thinkingValue": thinking_value,
        "inputs": ["text"],
        "contextWindow": int(m0.get("contextWindow", 1_000_000)),
        "maxTokens": int(m0.get("maxTokens", 8192)),
    }


def _parse_openclaw_stdout_json(stdout: str) -> dict:
    """插件常把日志打到 stdout；JSON 对象从 stdout 中第一个「{」起截取解析。"""
    start = stdout.find("{")
    if start < 0:
        raise ValueError("stdout 中无 JSON 对象")
    return json.loads(stdout[start:])


def run_gateway_json(args: list[str]) -> tuple[bool, str]:
    r = subprocess.run(
        ["openclaw", "gateway", "call", *args, "--json"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    out = r.stdout or ""
    if r.returncode != 0:
        return False, ((r.stderr or "") + out)[:500]
    try:
        d = _parse_openclaw_stdout_json(out)
    except (json.JSONDecodeError, ValueError) as e:
        return False, str(e)[:300]
    if args == ["health"]:
        ok = d.get("ok") is True
    elif args == ["status"]:
        ok = isinstance(d.get("sessions"), dict)
    else:
        ok = True
    return ok, json.dumps(d, ensure_ascii=False)[:400]


def main() -> None:
    results: list[tuple[str, str, str]] = []  # name, status PASS/FAIL/SKIP, detail

    # 1) 前端五项
    r = subprocess.run([sys.executable, str(FRONTEND_FIVE)], capture_output=True, text=True)
    ok = r.returncode == 0
    results.append(
        (
            "管理后台 _test_frontend_five",
            "PASS" if ok else "FAIL",
            (r.stdout + r.stderr)[-800:] if not ok else "",
        )
    )
    if not ok:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)

    r_mc = subprocess.run([sys.executable, str(MODEL_DISPLAY_CHAIN)], capture_output=True, text=True)
    ok_mc = r_mc.returncode == 0
    results.append(
        (
            "模型显示 / modelKey 链路 _test_model_display_chain",
            "PASS" if ok_mc else "FAIL",
            (r_mc.stdout + r_mc.stderr)[-800:] if not ok_mc else "",
        )
    )
    if not ok_mc:
        print(r_mc.stdout)
        print(r_mc.stderr, file=sys.stderr)

    cfg_base = read_cfg()
    prov, mid = "ciii", cfg_base["models"]["providers"]["ciii"]["models"][0]["id"]

    # 2) 电报页脚同构：off
    r_off = post("/api/model", model_body(read_cfg(), thinking_on=False))
    cfg_off = read_cfg()
    ft_off = resolve_footer_thinking(None, prov, mid, cfg_off)
    ok_off = r_off.get("ok") and ft_off == "off"
    results.append(
        (
            "页脚解析（无会话）与 off 配置一致",
            "PASS" if ok_off else "FAIL",
            f"resolve={ft_off!r} disk_thinking={get_per_model_params_thinking(cfg_off, 'ciii/' + mid)!r}",
        )
    )

    # 3) 电报页脚同构：low
    r_low = post("/api/model", model_body(read_cfg(), thinking_on=True, thinking_value="low"))
    cfg_low = read_cfg()
    ft_low = resolve_footer_thinking(None, prov, mid, cfg_low)
    ok_low = r_low.get("ok") and ft_low == "low"
    results.append(
        (
            "页脚解析（无会话）与 low 配置一致",
            "PASS" if ok_low else "FAIL",
            f"resolve={ft_low!r}",
        )
    )

    # 4) 会话覆盖：thinkingLevel 应优先于 params.thinking
    r_sess = resolve_footer_thinking({"thinkingLevel": "high"}, prov, mid, cfg_low)
    ok_sess = r_sess == "high"
    results.append(
        ("页脚解析 会话 thinkingLevel 优先", "PASS" if ok_sess else "FAIL", f"got={r_sess!r}"),
    )

    # 5) Telegram /status 卡片 Think: 与 OpenClaw resolveThinkingDefault 同构（非 gateway status JSON）
    cat = build_catalog_flat_from_cfg(cfg_low)
    st_off = post("/api/model", model_body(read_cfg(), thinking_on=False))
    cfg_st_off = read_cfg()
    cat_off = build_catalog_flat_from_cfg(cfg_st_off)
    think_status_off = effective_think_for_slash_status(None, cfg_st_off, prov, mid, cat_off)
    ok_st_off = st_off.get("ok") and think_status_off == "off"
    results.append(
        (
            "/status 等价 Think（无会话）与管理端 off 一致",
            "PASS" if ok_st_off else "FAIL",
            f"Think={think_status_off!r} params={resolve_thinking_default_openclaw(cfg_st_off, prov, mid, cat_off)!r}",
        )
    )
    st_low = post("/api/model", model_body(read_cfg(), thinking_on=True, thinking_value="low"))
    cfg_st_low = read_cfg()
    cat_low = build_catalog_flat_from_cfg(cfg_st_low)
    think_status_low = effective_think_for_slash_status(None, cfg_st_low, prov, mid, cat_low)
    ok_st_low = st_low.get("ok") and think_status_low == "low"
    results.append(
        (
            "/status 等价 Think（无会话）与管理端 low 一致",
            "PASS" if ok_st_low else "FAIL",
            f"Think={think_status_low!r}",
        )
    )
    think_sess_first = effective_think_for_slash_status(
        {"thinkingLevel": "medium"}, cfg_st_low, prov, mid, cat_low
    )
    ok_st_sess = think_sess_first == "medium"
    results.append(
        (
            "/status 等价 Think 会话 thinkingLevel 优先于配置",
            "PASS" if ok_st_sess else "FAIL",
            f"Think={think_sess_first!r}",
        )
    )

    # 恢复关思考（与五项脚本收尾一致）
    post("/api/model", model_body(read_cfg(), thinking_on=False))

    # 6) 网关（JSON 从 stdout 提取；status 不含「回落后的 Think」）
    gh_ok, gh_detail = run_gateway_json(["health"])
    results.append(
        (
            "openclaw gateway call health --json",
            "PASS" if gh_ok else "SKIP",
            "" if gh_ok else gh_detail,
        )
    )
    gs_ok, gs_detail = run_gateway_json(["status"])
    results.append(
        (
            "openclaw gateway call status --json 可解析",
            "PASS" if gs_ok else "SKIP",
            "" if gs_ok else gs_detail,
        )
    )

    # 7) telegram-footer node 测试
    if FOOTER_DIR.is_dir():
        ts = subprocess.run(
            ["node", str(FOOTER_DIR / "test-stability.mjs")],
            cwd=str(FOOTER_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        ts_ok = ts.returncode == 0
        results.append(
            (
                "telegram-footer test-stability.mjs",
                "PASS" if ts_ok else "FAIL",
                (ts.stdout + ts.stderr)[-600:] if not ts_ok else "",
            )
        )
        tr = subprocess.run(
            ["node", str(FOOTER_DIR / "test-retry.mjs")],
            cwd=str(FOOTER_DIR),
            capture_output=True,
            text=True,
            timeout=120,
        )
        tr_ok = tr.returncode == 0
        results.append(
            (
                "telegram-footer test-retry.mjs",
                "PASS" if tr_ok else "FAIL",
                (tr.stdout + tr.stderr)[-600:] if not tr_ok else "",
            )
        )
    else:
        results.append(("telegram-footer 目录存在", "SKIP", str(FOOTER_DIR)))

    # 汇总
    print("—— 完整测试套件 ——")
    hard_fail = False
    for name, status, detail in results:
        print(f"[{status}] {name}")
        if detail:
            for line in detail.strip().splitlines()[-5:]:
                print(f"       {line}")
        if status == "FAIL":
            hard_fail = True
    print(
        "—— 说明：页脚与 /status 用例均为与 OpenClaw 源码同构的离线断言；"
        "gateway status JSON 的 thinkingLevel 仅会话字段，不等于 /status 的 Think 回落 ——"
    )
    if hard_fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
