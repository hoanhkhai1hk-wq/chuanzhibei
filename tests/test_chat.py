"""问答层测试：事实构造 / 本地前置 / 数字回溯校验 / 脱敏

================================================================================
和 test_llm.py 一样，这里也用「故意造假的假大模型」来测校验器。
只测通过路径的校验器等于没测——必须证明它抓得住编出来的数字。
================================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.chat import QaAnswerer              # noqa: E402
from app.llm.explainer import Explainer, find_hallucinations  # noqa: E402
from app.pipeline import mask_pii, redact        # noqa: E402
from app.rules.engine import RuleEngine          # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


def sample_analysis() -> dict:
    """尿酸 520 偏高（参考 208-428）+ 血红蛋白 95 偏低 + 血糖正常。"""
    return RuleEngine().evaluate(
        [
            {"name": "尿酸(UA)", "value": "520", "unit": "μmol/L", "ref": "208-428"},
            {"name": "空腹血糖", "value": "5.41", "unit": "mmol/L", "ref": "3.9-6.1"},
            {"name": "血红蛋白", "value": "95", "unit": "g/L", "ref": "130-175"},
        ],
        "male",
    )


class FakeExplainer(Explainer):
    """假大模型：按剧本返回，用来验证校验与重写确实被执行。"""

    def __init__(self, replies: list[str]) -> None:
        super().__init__(api_key="fake-key")     # 有 Key → mode = "llm"
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.models: list[str] = []

    def complete(self, messages, max_tokens=512, temperature=0.3, model=None) -> str:
        # 最后一条 user 消息就是 prompt，记下来供断言
        self.prompts.append(messages[-1]["content"])
        self.models.append(model)
        return self.replies.pop(0)


def engine() -> RuleEngine:
    return RuleEngine()


# ─────────────────────────────── 脱敏 ──────────────────────────────────────

def test_mask_pii() -> None:
    q = "我叫张三，身份证 110101199001011234，电话 13800138000，尿酸高要紧吗"
    clean, fields = mask_pii(q)
    check("身份证被抹", "110101199001011234" in clean, False)
    check("手机号被抹", "13800138000" in clean, False)
    check("姓名被抹", "张三" in clean, False)
    check("问题本身保留", "尿酸高要紧吗" in clean, True)
    check("字段类型记录", set(fields) >= {"id_card", "phone", "name"}, True)

    # 「我是尿酸高」不能被当成自报姓名（这是很自然的表述）
    clean2, _ = mask_pii("我是尿酸高，要注意什么")
    check("「我是」不误伤", "尿酸高" in clean2, True)

    # 报告脱敏保持原有行为：抬头行整行丢弃
    report = "姓名 张三 性别 男\n尿酸 520 208-428"
    body, _ = redact(report)
    check("报告抬头整行丢弃", "张三" in body, False)
    check("指标行保留", "尿酸 520" in body, True)


# ─────────────────────────── 事实清单与本地前置 ────────────────────────────

def test_focus_and_facts() -> None:
    an = sample_analysis()
    qa = QaAnswerer(explainer=Explainer(api_key=""), markers=engine().markers)

    r = qa.answer("尿酸高是怎么回事？", an)
    # 事实清单 = 聚焦项在前 + 其余异常项（血红蛋白也异常，一并进上下文）
    check("聚焦项排第一", r["facts_used"][0], "尿酸")
    check("异常项进上下文", set(r["facts_used"]) >= {"尿酸", "血红蛋白"}, True)
    check("答案含报告值", "520" in r["answer"], True)
    check("答案含参考区间", "208-428" in r["answer"], True)
    check("答案用上字典解释", "痛风" in r["answer"], True)
    check("带出对应红线", "限嘌呤" in r["answer"], True)
    check("模板模式无幻觉", find_hallucinations(r["answer"], an), [])

    # 正常项的数值也必须答得出来（不是只答异常）
    r2 = qa.answer("我血糖多少？", an)
    check("正常项可追问", "5.41" in r2["answer"], True)
    check("正常项判定", "在参考区间内" in r2["answer"], True)


def test_food_context() -> None:
    an = sample_analysis()
    qa = QaAnswerer(explainer=Explainer(api_key=""), markers=engine().markers)

    r = qa.answer("我今天能吃火锅吗？", an)
    check("认出火锅类菜", len(r["foods"]) > 0, True)
    check("答案提到撞线", "限嘌呤" in r["answer"], True)
    # 撞线结论必须来自菜品库标签，而不是模型凭空判断
    tagged = [f for f in r["foods"]]
    check("菜品名来自本地库", all(isinstance(x, str) and x for x in tagged), True)

    # 库里查不到的食物：不能硬编，退回整体概览
    r3 = qa.answer("我能吃陨石吗？", an)
    check("未知食物不编", r3["foods"], [])


def test_measure_note() -> None:
    an = sample_analysis()
    qa = QaAnswerer(explainer=Explainer(api_key=""), markers=engine().markers)
    r = qa.answer("血压高平时要注意什么？", an)
    check("说明血压不在报告上", "不来自化验单" in r["answer"], True)
    check("指向日常层", "今日记录" in r["answer"], True)
    check("带出限钠红线", "限钠" in r["answer"], True)
    # 不能凭空编一个血压值出来
    check("不编血压数值", "120/80" in r["answer"], False)


# ─────────────────────────── 数字回溯校验与重写 ────────────────────────────

def test_llm_retry_on_hallucination() -> None:
    an = sample_analysis()
    fake = FakeExplainer([
        "你的尿酸是 847，明显超标。",          # 847 不在事实清单里 → 必须被抓住
        "你的尿酸是 520，高于参考区间 208-428。",
    ])
    qa = QaAnswerer(explainer=fake, markers=engine().markers)
    r = qa.answer("尿酸高是怎么回事", an)

    check("重写过一次", r["attempts"], 2)
    check("最终通过校验", r["validated"], True)
    check("最终答案正确", "520" in r["answer"], True)
    check("幻觉数字已消失", "847" in r["answer"], False)
    check("mode 标记为大模型", r["mode"], "llm")
    check("重写提示带上了违规数字", "847" in fake.prompts[1], True)
    check("重写要求出现", "重写" in fake.prompts[1], True)


def test_llm_falls_back_when_still_bad() -> None:
    an = sample_analysis()
    fake = FakeExplainer([
        "你的尿酸是 847。",
        "你的尿酸是 999。",
    ])
    qa = QaAnswerer(explainer=fake, markers=engine().markers)
    r = qa.answer("尿酸高是怎么回事", an)

    check("两次都不过就退回本地", r["mode"], "template")
    check("退回后仍通过校验", r["validated"], True)
    check("退回后答案有报告值", "520" in r["answer"], True)
    check("退回后没有编造数字", "847" in r["answer"] or "999" in r["answer"], False)

    # 模型抛异常（没网/超时）同样要退回本地，而不是 500
    class Boom(Explainer):
        def __init__(self):
            super().__init__(api_key="k")

        def complete(self, *a, **kw):
            raise RuntimeError("network down")

    r2 = QaAnswerer(explainer=Boom(), markers=engine().markers).answer("尿酸高", an)
    check("异常时退回本地", r2["mode"], "template")
    check("异常时仍有答案", bool(r2["answer"]), True)


def test_prompt_contains_facts_only() -> None:
    an = sample_analysis()
    fake = FakeExplainer(["尿酸 520，参考区间 208-428，偏高。"])
    qa = QaAnswerer(explainer=fake, markers=engine().markers)
    qa.answer("尿酸高是怎么回事", an)

    prompt = fake.prompts[0]
    check("prompt 含报告值", "520" in prompt, True)
    check("prompt 含参考区间", "208-428" in prompt, True)
    check("prompt 含红线", "限嘌呤" in prompt, True)
    check("prompt 不含姓名段", "姓名" in prompt, False)
    check("prompt 不含身份证", "身份证" in prompt, False)


def test_model_fallback_reaches_ui() -> None:
    """选了占位的模型：答案照给，但必须告诉界面「实际用的是 DeepSeek」。"""
    an = sample_analysis()
    fake = FakeExplainer(["你的尿酸是 520，高于参考区间 208-428。"])
    qa = QaAnswerer(explainer=fake, markers=engine().markers)

    r = qa.answer("尿酸高是怎么回事", an, model="qwen")
    check("选通义时不崩", bool(r["answer"]), True)
    check("实际模型是 DeepSeek", r["model"], "DeepSeek V4.1")
    check("提示回传给界面", r["model_notice"], "该模型正在接入中，当前使用 DeepSeek")
    check("选中的 key 透传到调用层", fake.models[-1], "qwen")

    r2 = qa.answer("尿酸高是怎么回事", an, model="deepseek")
    check("选 DeepSeek 无提示", r2["model_notice"], "")

    # 未登记的模型同样不能让接口挂掉
    r3 = qa.answer("尿酸高是怎么回事", an, model="xxx")
    check("未登记模型不崩", bool(r3["answer"]), True)


def test_counts_are_allowed() -> None:
    """计数类数字（共 N 项）是本地事实，不该被自己的校验器判成幻觉。"""
    an = sample_analysis()
    total = an["matched"]
    text = f"这次共判定 {total} 项，其中 {an['abnormal_count']} 项需要注意。"
    check("计数不算幻觉", find_hallucinations(text, an), [])


def main() -> int:
    print("=== 脱敏 ===")
    test_mask_pii()
    print("=== 事实清单与本地前置 ===")
    test_focus_and_facts()
    test_food_context()
    test_measure_note()
    print("=== 数字回溯校验与重写 ===")
    test_llm_retry_on_hallucination()
    test_llm_falls_back_when_still_bad()
    test_prompt_contains_facts_only()
    test_model_fallback_reaches_ui()
    test_counts_are_allowed()

    print(f"\n通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("问答层测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
