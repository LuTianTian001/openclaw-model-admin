#!/usr/bin/env python3
"""5 项集成测试：校验管理端与 openclaw.json / sessions 一致行为。结束恢复 ciii 为 thinking off。"""
from __future__ import annotations

import json
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8765"
CFG = Path("/root/.openclaw/openclaw.json")
SESS = Path("/root/.openclaw/agents/main/sessions/sessions.json")


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
    return json.loads(CFG.read_text(encoding="utf-8"))


def ciii_thinking(cfg: dict) -> str | None:
    m = cfg.get("agents", {}).get("defaults", {}).get("models", {}).get("ciii/gpt-5.4", {})
    if not isinstance(m, dict):
        return None
    p = m.get("params")
    if not isinstance(p, dict):
        return None
    t = p.get("thinking")
    return t if isinstance(t, str) else None


def state_ciii_thinking() -> str | None:
    d = post("/api/state", None)
    if not d.get("ok"):
        raise RuntimeError(d)
    for m in d.get("state", {}).get("models", []):
        if m.get("ref") == "ciii/gpt-5.4":
            return m.get("thinking")
    return None


def validate_cli() -> bool:
    r = subprocess.run(
        ["openclaw", "config", "validate"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.returncode == 0 and "invalid" not in (r.stdout + r.stderr).lower()


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


def count_session_thinking_levels() -> int:
    data = json.loads(SESS.read_text(encoding="utf-8"))
    n = 0
    for v in data.values():
        if isinstance(v, dict) and "thinkingLevel" in v:
            n += 1
    return n


def main() -> None:
    results: list[tuple[str, bool, str]] = []
    cfg0 = read_cfg()

    # —— 测试 1：关思考 → 磁盘必须为 params.thinking == off，且 openclaw 校验通过
    r1 = post("/api/model", model_body(cfg0, thinking_on=False))
    ok1 = r1.get("ok") and ciii_thinking(read_cfg()) == "off" and validate_cli()
    results.append(("1 关思考→配置为 off 且 validate 通过", ok1, "" if ok1 else str(r1)[:200]))

    # —— 测试 2：开思考 + 自定义档位 → 写入指定值，/api/state 模型列表一致
    r2 = post("/api/model", model_body(read_cfg(), thinking_on=True, thinking_value="medium"))
    cfg2 = read_cfg()
    ok2 = r2.get("ok") and ciii_thinking(cfg2) == "medium" and state_ciii_thinking() == "medium"
    results.append(("2 开思考 medium→配置与 /api/state 一致", ok2, "" if ok2 else repr((ciii_thinking(cfg2), state_ciii_thinking()))))

    # —— 测试 3：保存模型会清理会话 thinkingLevel（注入一条再保存）
    data = json.loads(SESS.read_text(encoding="utf-8"))
    inject_key = next((k for k, v in data.items() if isinstance(v, dict)), None)
    removed_after = False
    if inject_key:
        backup_tl = data[inject_key].get("thinkingLevel")
        data[inject_key]["thinkingLevel"] = "low"
        SESS.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        before = count_session_thinking_levels()
        r3 = post("/api/model", model_body(read_cfg(), thinking_on=True, thinking_value="medium"))
        after = count_session_thinking_levels()
        meta = (r3.get("meta") or {}).get("sessionThinkingCleared") or {}
        cleared = int(meta.get("cleared", 0))
        ok3 = r3.get("ok") and after < before and cleared > 0
        results.append(("3 保存模型清理会话 thinkingLevel", ok3, f"before={before} after={after} cleared={cleared}"))
        # 不恢复 backup_tl：清理后本就没有或为 undefined；若需可再写回
    else:
        results.append(("3 保存模型清理会话 thinkingLevel", False, "无可用会话键"))

    # —— 测试 4：写入 thinkingDefault 后任意经 write_config 的保存应剥离
    cfg4 = read_cfg()
    cfg4.setdefault("agents", {}).setdefault("defaults", {})["thinkingDefault"] = "low"
    CFG.write_text(json.dumps(cfg4, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    r4 = post("/api/model", model_body(read_cfg(), thinking_on=True, thinking_value="low"))
    td = read_cfg().get("agents", {}).get("defaults", {}).get("thinkingDefault")
    ok4 = r4.get("ok") and td is None
    results.append(("4 write_config 剥离 thinkingDefault", ok4, "" if ok4 else f"thinkingDefault={td!r}"))

    # —— 测试 5：关思考后不得出现空 {}（须保留 params.thinking off），并再 validate
    r5 = post("/api/model", model_body(read_cfg(), thinking_on=False))
    ent = read_cfg().get("agents", {}).get("defaults", {}).get("models", {}).get("ciii/gpt-5.4")
    ok5 = (
        r5.get("ok")
        and isinstance(ent, dict)
        and isinstance(ent.get("params"), dict)
        and ent["params"].get("thinking") == "off"
        and validate_cli()
    )
    results.append(("5 关思考后非空 params（禁止 {} 回落 low）", ok5, "" if ok5 else repr(ent)))

    # —— 测试 6：主模型与 ciii 同 modelId 时，保存 ciii 思考应同步到 primary ref（常见：主 openai-codex/gpt-5.4 + 改 ciii/gpt-5.4）
    cfg_b = read_cfg()
    prim = (cfg_b.get("agents", {}).get("defaults", {}).get("model") or {}).get("primary")
    ciii_mid = cfg_b["models"]["providers"]["ciii"]["models"][0]["id"]
    ok6 = True
    extra6 = ""
    if isinstance(prim, str) and "/" in prim:
        _, pid = prim.split("/", 1)
        if pid.strip() == ciii_mid.strip():
            try:
                r6a = post("/api/model", model_body(read_cfg(), thinking_on=True, thinking_value="low"))
                cfg6 = read_cfg()
                meta6 = r6a.get("meta") or {}
                p_ent = (cfg6.get("agents", {}).get("defaults", {}).get("models") or {}).get(prim.strip())
                p_th = (p_ent.get("params") or {}).get("thinking") if isinstance(p_ent, dict) else None
                ok6 = (
                    r6a.get("ok")
                    and ciii_thinking(cfg6) == "low"
                    and p_th == "low"
                    and meta6.get("thinkingSyncedToPrimary") == prim.strip()
                )
                extra6 = f"primary={prim!r} meta.sync={meta6.get('thinkingSyncedToPrimary')!r}"
                r6b = post("/api/model", model_body(read_cfg(), thinking_on=False))
                cfg7 = read_cfg()
                p_ent7 = (cfg7.get("agents", {}).get("defaults", {}).get("models") or {}).get(prim.strip())
                p_th7 = (p_ent7.get("params") or {}).get("thinking") if isinstance(p_ent7, dict) else None
                ok6 = ok6 and r6b.get("ok") and ciii_thinking(cfg7) == "off" and p_th7 == "off"
            finally:
                post("/api/model", model_body(read_cfg(), thinking_on=False))
        else:
            extra6 = f"跳过（主模型 id {pid!r} ≠ ciii {ciii_mid!r}）"
    else:
        extra6 = "跳过（无 primary）"
    results.append(("6 同 modelId 时同步主模型 thinking", ok6, extra6))

    # 汇总
    print("—— 管理后台 6 项测试（8765）——")
    all_ok = True
    for name, ok, extra in results:
        all_ok = all_ok and ok
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if extra:
            print(f"       {extra}")
    print("—— 总计:", "全部通过" if all_ok else "存在失败 —— 请重启加载最新 server.py 后再测")
    if not all_ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
