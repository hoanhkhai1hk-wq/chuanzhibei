"""周报与测量记录测试

================================================================================
重点：血压必须由日常层触发「限钠」红线——这是日常层存在的核心理由之一
（报告里没有血压值，这条红线只能靠测量记录激活），不是顺手加的小功能。
================================================================================
"""

from __future__ import annotations

import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import db as store                        # noqa: E402
from app.daily import service as daily                  # noqa: E402

store.DB_PATH = Path(tempfile.mkdtemp(prefix="tjzh_week_")) / "test.db"
store.init_db()

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


def test_bp_flag() -> None:
    check("150/95 偏高", daily.bp_flag(150, 95), "high")
    check("140/90 临界值算偏高", daily.bp_flag(140, 90), "high")
    check("139/89 正常", daily.bp_flag(139, 89), "normal")
    check("120/80 正常", daily.bp_flag(120, 80), "normal")
    check("缺值待确认", daily.bp_flag(None, None), "unknown")
    check("只填一个待确认", daily.bp_flag(150, None), "high")   # 单独收缩压超即可判定
    check("阈值有出处", daily.BP_THRESHOLD, (140, 90))


def test_bp_activates_redline() -> None:
    before = {r["rule_id"] for r in store.get_active_redlines()}
    check("起始无红线", before, set())

    s = daily.record_numeric("me", "2026-10-17", "blood_pressure",
                             systolic=150, diastolic=95)
    check("返回激活的红线名", s["activated_redline"], "限钠")

    after = store.get_active_redlines()
    rules = {r["rule_id"] for r in after}
    check("R03 已激活", any(r.startswith("R03") for r in rules), True)
    sodium_rule = [r for r in after if r["nutrient_key"] == "sodium"][0]
    check("限钠红线的 nutrient_key", sodium_rule["nutrient_key"], "sodium")
    check("限钠红线带触发食物", len(sodium_rule["trigger_foods"]) > 0, True)

    # 正常血压不激活新的
    store.execute_delete = None
    s2 = daily.record_numeric("me", "2026-10-18", "blood_pressure",
                              systolic=120, diastolic=80)
    check("正常血压不激活", s2["activated_redline"], None)

    # 高钠菜品应撞上刚激活的限钠红线
    lib = daily.search_dishes("")
    salty = next((d for d in lib if d["tags"]["sodium"] == "high"), None)
    check("库里有高钠菜品", salty is not None, True)
    if salty:
        store.add_meal("me", "2026-10-17", "lunch", [salty["name"]])
        hits = {h["restriction"] for h in daily.today_summary("me", "2026-10-17")["hits"]}
        check("高钠菜品撞限钠", "限钠" in hits, True)


def test_week_summary() -> None:
    # 用独立日期，避免与上面几个用例共享同一天（同一天的餐次会累加进断言）
    end = "2026-10-25"
    store.add_meal("me", end, "lunch", ["毛肚"])
    store.add_numeric("me", end, "blood_pressure", systolic=150, diastolic=95)
    store.add_numeric("me", end, "blood_glucose", glucose=6.2)

    w = daily.week_summary("me", end, 7)
    check("返回 7 天", len(w["series"]), 7)
    check("日期升序", w["series"][0]["date"] < w["series"][-1]["date"], True)
    check("末日为指定日", w["series"][-1]["date"], end)

    last = w["series"][-1]
    check("末日有餐次", last["meals"], 1)
    check("末日血压回填", (last["systolic"], last["diastolic"]), (150, 95))
    check("末日血糖回填", last["glucose"], 6.2)
    check("无记录日血压为空", w["series"][0]["systolic"], None)
    check("热量是数值", isinstance(last["kcal"], int), True)

    # 只取 3 天
    check("天数可指定", len(daily.week_summary("me", end, 3)["series"]), 3)


def test_other_kinds() -> None:
    s = daily.record_numeric("me", "2026-10-19", "weight", weight=68.5)
    check("体重不触发红线", s["activated_redline"], None)
    nums = daily.today_summary("me", "2026-10-19")["numerics"]
    check("体重已记录", nums[-1]["weight"], 68.5)
    check("体重类型正确", nums[-1]["kind"], "weight")

    s2 = daily.record_numeric("me", "2026-10-19", "blood_glucose", glucose=7.5)
    check("血糖不激活红线", s2["activated_redline"], None)


def main() -> int:
    print("=== 血压判定阈值 ===")
    test_bp_flag()
    print("=== 血压激活限钠红线 ===")
    test_bp_activates_redline()
    print("=== 周报聚合 ===")
    test_week_summary()
    print("=== 其他记录类型 ===")
    test_other_kinds()

    print(f"\n通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("周报与测量记录测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
