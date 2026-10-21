"""多模型层测试：注册表 / 回落 / call_llm 唯一出口

================================================================================
重点不是「能不能调通网络」，而是三件结构性的事：
    1. 选了没接的模型**不能报错、不能崩**，要静默回落到默认模型；
    2. 回落**必须留下提示**，不能让用户以为切换生效了；
    3. 业务代码里只能有一个出口，换模型不改业务代码。
================================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import provider                    # noqa: E402
from app.llm.provider import (                  # noqa: E402
    DEFAULT_MODEL_KEY, MODELS, call_llm, list_models, resolve_model,
)

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


# ─────────────────────────────── 注册表 ──────────────────────────────────────

def test_registry() -> None:
    check("三个模型", sorted(MODELS), ["deepseek", "kimi", "qwen"])
    check("DeepSeek 已接通", MODELS["deepseek"].is_ready, True)
    check("通义千问是占位", MODELS["qwen"].is_ready, False)
    check("Kimi 是占位", MODELS["kimi"].is_ready, False)

    # 界面上要显示的名字，按需求写死
    check("DeepSeek 显示名", MODELS["deepseek"].label, "DeepSeek V4.1")
    check("通义千问显示名", MODELS["qwen"].label, "通义千问")
    check("Kimi 显示名", MODELS["kimi"].label, "Kimi")

    # 默认模型必须 ready，否则整个回落链没有落点
    check("默认模型已接通", MODELS[DEFAULT_MODEL_KEY].is_ready, True)

    # 三家的 Key 走不同的环境变量，不能互相覆盖
    keys = {m.env_key for m in MODELS.values()}
    check("环境变量各不同", len(keys), 3)

    listed = list_models()
    check("列表长度", len(listed), 3)
    check("默认项有标记", [m["key"] for m in listed if m["is_default"]], ["deepseek"])
    check("只有一项 ready", len([m for m in listed if m["ready"]]), 1)


def test_resolve_fallback() -> None:
    spec, notice = resolve_model("deepseek")
    check("选 DeepSeek 用 DeepSeek", spec.key, "deepseek")
    check("选 DeepSeek 无提示", notice, "")

    for key in ("qwen", "kimi"):
        spec, notice = resolve_model(key)
        check(f"{key} 回落到默认", spec.key, DEFAULT_MODEL_KEY)
        check(f"{key} 有提示", notice, "该模型正在接入中，当前使用 DeepSeek")

    # 未登记的 key 同样不能抛异常
    spec, notice = resolve_model("claude-999")
    check("未登记也回落", spec.key, DEFAULT_MODEL_KEY)
    check("未登记有提示", "未登记" in notice, True)

    # 空值 / None 走默认，且**不能有提示**：
    # 前端已经不再传 model，这两条是正常调用的必经之路，
    # 一旦这里冒出一句「未登记的模型「None」」，每次解读都会挂个假警告
    check("空串走默认", resolve_model("")[0].key, DEFAULT_MODEL_KEY)
    check("空串无提示", resolve_model("")[1], "")
    check("None 走默认", resolve_model(None)[0].key, DEFAULT_MODEL_KEY)
    check("None 无提示", resolve_model(None)[1], "")
    check("全空格无提示", resolve_model("   ")[1], "")
    check("大小写不敏感", resolve_model("DeepSeek")[0].key, "deepseek")


# ─────────────────────────── call_llm 唯一出口 ──────────────────────────────

def test_call_llm_falls_back() -> None:
    """用假 _chat 拦住网络，只看它到底选了哪个模型。"""
    calls: list[tuple[str, bool]] = []
    original = provider._chat

    def fake_chat(spec, messages, *, max_tokens, temperature, json_mode,
                  timeout=provider.DEFAULT_READ_TIMEOUT):
        calls.append((spec.key, json_mode))
        return "模型回复：尿酸 520。"

    provider._chat = fake_chat
    try:
        # 前端已经不传 model 了，这条是主路径：必须静默走默认模型，不能带提示
        r0 = call_llm("问题")
        check("不传 model 用默认模型", calls[-1][0], "deepseek")
        check("不传 model 无提示", r0.notice, "")

        r1 = call_llm("问题", "deepseek")
        check("用 DeepSeek 调用", calls[-1][0], "deepseek")
        check("DeepSeek 无提示", r1.notice, "")
        check("回显实际模型", r1.model_label, "DeepSeek V4.1")

        r2 = call_llm("问题", "qwen")
        check("选通义时实际用 DeepSeek", calls[-1][0], "deepseek")
        check("选通义时仍返回提示", r2.notice, "该模型正在接入中，当前使用 DeepSeek")
        check("回显仍是实际模型", r2.model_label, "DeepSeek V4.1")

        r3 = call_llm("问题", "kimi", system="你是助手", json_mode=True)
        check("Kimi 也回落", calls[-1][0], "deepseek")
        check("json_mode 透传", calls[-1][1], True)
        check("Kimi 有提示", bool(r3.notice), True)
    finally:
        provider._chat = original


def test_business_code_has_single_exit() -> None:
    """业务模块里不允许再出现各自的 OpenAI 客户端或模型分支。"""
    import inspect

    import app.llm.chat as chat_mod
    import app.llm.explainer as expl_mod

    for mod in (chat_mod, expl_mod):
        src = inspect.getsource(mod)
        check(f"{mod.__name__} 不再自建 client", "OpenAI(" in src, False)
        check(f"{mod.__name__} 不直接判定模型分支", 'if model ==' in src, False)

    # provider 才是唯一持有 client 的地方
    provider_src = inspect.getsource(provider)
    check("provider 持有 client", "OpenAI(" in provider_src, True)
    check("call_llm 存在且有 model 参数",
          "model" in inspect.signature(call_llm).parameters, True)


def main() -> int:
    print("=== 模型注册表 ===")
    test_registry()
    print("=== 回落与提示 ===")
    test_resolve_fallback()
    print("=== call_llm 唯一出口 ===")
    test_call_llm_falls_back()
    test_business_code_has_single_exit()

    print(f"\n通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("多模型层测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
