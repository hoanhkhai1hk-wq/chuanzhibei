"""表达层测试：模板模式 + 数字回溯校验

================================================================================
重点是用「故意造假的假大模型」验证校验器真的能抓幻觉。
校验器如果只用真实通过的数据测，等于没测——
必须证明它抓得住没有出处的数字，才能真正为架构铁律背书。
================================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.explainer import (           # noqa: E402
    Explainer,
    _with_counts,
    allowed_numbers,
    find_hallucinations,
)
from app.rules.engine import RuleEngine  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


def sample_analysis() -> dict:
    """一份带已知异常的判定结果（尿酸 512 偏高，参考 208-428）。"""
    engine = RuleEngine()
    return engine.evaluate(
        [
            {"name": "尿酸(UA)", "value": "512", "unit": "μmol/L", "ref": "208-428"},
            {"name": "空腹血糖", "value": "5.41", "unit": "mmol/L", "ref": "3.9-6.1"},
        ],
        "male",
    )


# ─────────────────────────────── _with_counts ───────────────────────────────

def test_with_counts() -> None:
    raw = sample_analysis()
    check("引擎裸结果无 counts", "counts" in raw, False)

    padded = _with_counts(raw)
    check("补齐后的 matched", padded["counts"]["matched"], raw["matched"])
    check("补齐后的 abnormal_count", padded["counts"]["abnormal_count"], raw["abnormal_count"])

    # 已带 counts 的入参必须原样透传，不得重复包裹
    twice = _with_counts(padded)
    check("幂等性", twice is padded, True)


# ──────────────────────────── 数字回溯校验 ──────────────────────────────────

def test_number_validation() -> None:
    analysis = sample_analysis()

    # 出处内数字：报告值、区间端点都应放行
    ok_text = "你的尿酸是 512，参考区间 208-428，偏高。建议 3-6 个月后复查。"
    check("合法数字通过", find_hallucinations(ok_text, analysis), [])

    # 典型幻觉：编造了一个报告上不存在的数值
    bad = "你的尿酸是 847，明显偏高。"
    bad_hits = find_hallucinations(bad, analysis)
    check("幻觉数字被抓", bad_hits, ["847"])

    # 白名单数字放行（复查周期等常识数字）
    benign = "建议 6 个月后复查，平时一天喝 2 升水。"
    check("常识数字放行", find_hallucinations(benign, analysis), [])

    # 尾零差异容忍：5.40 vs 5.41 不该误杀
    check("容忍四舍五入", find_hallucinations("血糖 5.4 在正常范围", analysis), [])

    # 允许集合应包含全部报告值与区间端点
    allowed = allowed_numbers(analysis)
    for expect in ("512", "5.41", "208", "428", "3.9", "6.1"):
        check(f"允许集合含 {expect}", expect in allowed, True)


# ──────────────────────────── 模板模式整体 ──────────────────────────────────

def test_template_mode() -> None:
    analysis = sample_analysis()
    exp = Explainer(api_key="")            # 强制无 Key → 模板模式
    check("无 Key 时为模板模式", exp.mode, "template")

    out = exp.explain(analysis)
    report = out["report"]

    check("mode 标记", out["mode"], "template")
    check("disclaimer 必填存在", bool(report["disclaimer"]), True)
    check("逐项解释覆盖全部指标", len(report["tests"]), analysis["matched"])

    # 架构铁律：报告值必须逐字来自本地判定，模板模式天然满足
    by_name = {t["test_name"]: t for t in report["tests"]}
    check("尿酸报告值逐字一致", by_name["尿酸"]["reported_value"], "512")
    check("异常项标记 no", by_name["尿酸"]["is_likely_normal"], "no")
    check("正常项标记 yes", by_name["空腹血糖"]["is_likely_normal"], "yes")
    check("参考区间格式", by_name["尿酸"]["reference_range"], "208-428")

    # 模板输出整体自检：不应含任何无出处数字
    prose = " ".join(
        [report["summary"]]
        + [t["simple_explanation"] for t in report["tests"]]
        + [t["recommended_next_step"] for t in report["tests"]]
    )
    check("模板模式无幻觉数字", find_hallucinations(prose, analysis), [])

    # 问诊清单：异常指标必须生成针对性问题
    qs = report["follow_up_questions"]
    check("问诊清单非空", len(qs) > 0, True)
    check("问诊含指标名", any("尿酸" in q for q in qs), True)

    # 给爸妈说的话
    fam = out["family_message"]
    check("家话提到异常项", "尿酸" in fam["message"], True)
    check("家话含免责声明", fam["disclaimer"] == report["disclaimer"], True)
    check("语气档位合法", fam["tone"] in ("reassuring", "neutral", "alerting"), True)


def test_family_message_tone() -> None:
    analysis = sample_analysis()
    exp = Explainer(api_key="")
    fam = exp.family_message(analysis)

    # 异常项 <3 时语气应为宽慰
    check("少量异常→宽慰", fam["tone"], "reassuring")
    check("提到具体项", "尿酸" in fam["message"], True)

    # boost（补铁）方向检查：触发血红蛋白偏低时应说「补」而不是「少吃」
    boosted = RuleEngine().evaluate(
        [{"name": "血红蛋白", "value": "95", "unit": "g/L", "ref": "130-175"}], "male")
    check("补铁触发", boosted["redlines"][0]["action"], "boost")
    fam2 = exp.family_message(_with_counts(boosted))
    # boost 方向的正确话术是「适当补」，且明确告诉用户「不是要少吃」
    check("补铁话术含「适当补」", ("适当补" in fam2["message"]), True)
    check("补铁话术澄清不是限制", ("不是要少吃" in fam2["message"]), True)


def main() -> int:
    print("=== _with_counts ===")
    test_with_counts()
    print("=== 数字回溯校验 ===")
    test_number_validation()
    print("=== 模板模式 ===")
    test_template_mode()
    print("=== 给爸妈的话 ===")
    test_family_message_tone()

    print(f"\n通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("表达层测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
