#!/usr/bin/env python3
"""
离线校验：sessions 覆盖存储 ↔ 管理端展示 ref ↔ OpenClaw modelKey（网关白名单）。
不启动 HTTP；不修改磁盘 sessions（迁移函数在其它集成流中测）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def load_server():
    spec = importlib.util.spec_from_file_location("ocma_server", ROOT / "server.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main() -> None:
    srv = load_server()
    failed: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        if not cond:
            failed.append(name + (f" ({detail})" if detail else ""))

    # 1) 拆分存储 → 展示 ref 规范化
    ref, via = srv._effective_model_ref_for_session(
        {"providerOverride": "alibailian", "modelOverride": "qwen3.5-plus"},
        "vx001/gpt-5.4",
    )
    check("split→展示 ref", ref == "alibailian/qwen3.5-plus" and via)

    # 2) 无覆盖 → 回落 primary；供应商段规范化（模型 id 保持磁盘原样，与 models.providers[].models[].id 一致）
    ref2, via2 = srv._effective_model_ref_for_session({}, "alibailian/qwen3.5-plus")
    check("无覆盖→primary", ref2 == "alibailian/qwen3.5-plus" and not via2)
    ref2b, via2b = srv._effective_model_ref_for_session({}, "VX001/gpt-5.4")
    check("无覆盖→primary 供应商段小写", ref2b == "vx001/gpt-5.4" and not via2b)

    # 3) 遗留：整串写在 modelOverride、无 provider → 网关会用渠道默认供应商拼出错误 key
    k_bad = srv._openclaw_model_key_from_session_override(
        {"modelOverride": "alibailian/qwen3.5-plus"},
        "vx001",
    )
    check(
        "遗留 mo 整串 + 默认 vx001 → 错误 key（说明为何必须拆分）",
        k_bad == "vx001/alibailian/qwen3.5-plus",
    )

    # 4) 拆分后 modelKey 与 agents.defaults.models 键一致
    k_ok = srv._openclaw_model_key_from_session_override(
        {"providerOverride": "alibailian", "modelOverride": "qwen3.5-plus"},
        "vx001",
    )
    check("拆分后 openclaw key", k_ok == "alibailian/qwen3.5-plus")

    # 5) set_session 用的拆分工具
    sp = srv.split_model_ref_for_session_store("ciii/gpt-5.4")
    check("split_model_ref", sp == ("ciii", "gpt-5.4"))

    # 6) mo 以当前 provider 为前缀时保持 OpenClaw modelKey 单段语义
    k_pref = srv._openclaw_model_key_from_session_override(
        {"providerOverride": "vx001", "modelOverride": "vx001/gpt-5.4"},
        "alibailian",
    )
    check("mo 已带 provider/ 前缀时 modelKey", k_pref == "vx001/gpt-5.4")

    print("—— 模型显示 / modelKey 链路 ——")
    if failed:
        for x in failed:
            print("[FAIL]", x)
        print("—— 总计: 失败 ——")
        raise SystemExit(1)
    print("[PASS] 全部断言通过")
    print("—— 总计: 通过 ——")


if __name__ == "__main__":
    main()
