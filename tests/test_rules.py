"""
本地规则引擎测试

不依赖 pytest，直接运行即可：
    python tests/test_rules.py

为什么要有这个：
    判定逻辑是「架构铁律」的落地点——所有数字与高低结论都在这里产生，
    不经过大模型。这里出错，下游全错，而且不会有异常抛出。
    所以每一步都要有可复现的断言。

    尤其要覆盖「方向性错误」：把补铁当成限铁、把血常规当成尿检，
    这类错误程序不会报错，但给出的是完全相反的建议。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.rules.engine import make_engine  # noqa: E402

FAILED: list[str] = []
PASSED = 0


def check(label: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{label}: 得到 {got!r}，期望 {want!r}")


def main() -> int:
    e = make_engine()

    # ---------------------------------------------------------------- 归一化
    # 重点是「尿检项」与「血常规项」不能互相串台。
    # 曾经的 bug：「WBC(尿)」去掉括号后等于「WBC」，覆盖后导致
    # 血常规白细胞计数被判成尿白细胞。
    for raw, want in [
        ("白细胞计数WBC", "wbc"), ("WBC", "wbc"), ("白细胞", "wbc"),
        ("尿白细胞", "u_wbc"), ("尿WBC", "u_wbc"), ("LEU", "u_wbc"),
        ("红细胞计数", "rbc"), ("尿红细胞", "u_rbc"), ("尿潜血", "u_rbc"),
        ("空腹血糖", "glu"), ("血糖", "glu"),
        ("尿糖", "u_glucose"), ("尿葡萄糖", "u_glucose"),
        ("尿蛋白", "u_protein"), ("尿酸(UA)", "ua"), ("血红蛋白(Hb)", "hb"),
        ("谷丙转氨酶", "alt"), ("ALT", "alt"),
        ("低密度脂蛋白胆固醇", "ldl"), ("LDL-C", "ldl"),
    ]:
        check(f"归一 {raw}", e.normalize_name(raw), want)

    check("归一化无别名冲突", e.alias_conflicts, [])

    # -------------------------------------------------------------- 高低判定
    check("尿酸 512 偏高", e.judge("ua", "512", "male")[0], "high")
    check("尿酸 300 正常", e.judge("ua", "300", "male")[0], "normal")
    check("血红蛋白 90 偏低", e.judge("hb", "90", "male")[0], "low")
    check("空腹血糖 5.41 正常", e.judge("glu", "5.41", "male")[0], "normal")
    check("空腹血糖 7.2 偏高", e.judge("glu", "7.2", "male")[0], "high")

    # 性别分支：肌酐 100 对男性正常(57-111)，对女性偏高(44-97)
    check("肌酐 100 男性正常", e.judge("cr", "100", "male")[0], "normal")
    check("肌酐 100 女性偏高", e.judge("cr", "100", "female")[0], "high")

    # 定性项
    check("尿蛋白 阳性", e.judge("u_protein", "+")[0], "high")
    check("尿蛋白 阴性", e.judge("u_protein", "阴性")[0], "normal")

    # 未收录指标应返回 unknown，而不是乱猜
    check("未收录指标", e.normalize_name("某某没收录的指标"), None)

    # -------------------------------------------------------------- 红线匹配
    res = e.evaluate([
        {"name": "尿酸(UA)", "value": "512", "unit": "μmol/L"},
        {"name": "血红蛋白(Hb)", "value": "90", "unit": "g/L"},
        {"name": "空腹血糖(GLU)", "value": "5.41", "unit": "mmol/L"},
        {"name": "甘油三酯(TG)", "value": "2.8", "unit": "mmol/L"},
        {"name": "尿蛋白", "value": "+", "unit": ""},
        {"name": "某某没收录的指标", "value": "1", "unit": ""},
    ], sex="male")

    check("总项数", res["total"], 6)
    check("命中字典数", res["matched"], 5)
    check("未识别项", res["unmatched"], ["某某没收录的指标"])
    check("异常项数", res["abnormal_count"], 4)

    ids = sorted(r["rule_id"] for r in res["redlines"])
    check("触发的红线", ids,
          ["R01_UA_HIGH", "R04_TG_HIGH", "R05_HB_LOW", "R07_UPRO_POS"])

    # 方向性断言：补铁必须是「建议补充」，不能是「限制」
    boost = [r for r in res["redlines"] if r["rule_id"] == "R05_HB_LOW"]
    check("补铁规则存在", len(boost), 1)
    if boost:
        check("补铁 action 为 boost", boost[0]["action"], "boost")
        check("补铁 nutrient_key", boost[0]["nutrient_key"], "iron")
        check("补铁文案不含「少吃」", "少吃" in boost[0]["advice"], False)

    # 正常指标不应触发红线
    check("正常血糖不触发红线",
          any(r["rule_id"].startswith("R02") for r in res["redlines"]), False)

    # ---------------------------------------------------------- 定性指标判定
    # 中国检验报告上定性结果几乎都写成「阴性（-）」「阳性（+）」，
    # 修复前「阴性（-）」会被判成 unknown——最常见的写法反而判不出来。
    for val, want in [("阴性（-）", "normal"), ("阴性(-)", "normal"),
                      ("阴性", "normal"), ("未见", "normal"),
                      ("阳性（+）", "high"), ("阳性(+)", "high"),
                      ("弱阳性（±）", "high"), ("微量", "high"),
                      ("2+", "high"), ("+++", "high"), ("±", "high")]:
        flag, _ = e.judge("u_protein", val, "male")
        check(f"定性 {val!r}", flag, want)

    # 非定性的描述性文本（如尿液颜色）不应被当成异常
    flag, why = e.judge("u_protein", "淡黄色", "male")
    check("描述性文本不误判", flag, "unknown")
    check("无法判定时如实说明", "无法识别" in why["reason"], True)

    # ------------------------------------------------------------------ 输出
    print(f"通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("规则引擎测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
