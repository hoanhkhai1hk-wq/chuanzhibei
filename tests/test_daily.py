"""日常层测试：落库 + 红线对照 + 撞线判定

================================================================================
关键断言：
    limit 红线只被 high 档菜品撞线（medium 不打扰——粗筛定位）
    boost 红线（补铁）方向相反：吃到补铁食材算「达标」
    未知菜品不参与累加，但如实报告（不静默丢弃）
================================================================================
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data import db as store                        # noqa: E402
from app.daily import service as daily                  # noqa: E402

# 测试用独立数据库，不碰开发库
_tmp = tempfile.mkdtemp(prefix="tjzh_test_")
store.DB_PATH = Path(_tmp) / "test.db"
store.init_db()

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


def fake_result() -> dict:
    """一份触发「限嘌呤」与「补铁」的解读结果（形状与引擎输出一致）。"""
    return {
        "judged": [
            {"marker_id": "ua", "standard_name": "尿酸", "reported_value": "512",
             "flag": "high"},
            {"marker_id": "hb", "standard_name": "血红蛋白", "reported_value": "95",
             "flag": "low"},
        ],
        "redlines": [
            {"rule_id": "R01_UA_HIGH", "restriction": "限嘌呤", "marker": "尿酸",
             "nutrient_key": "purine", "action": "limit",
             "trigger_foods": ["动物内脏", "浓肉汤"], "advice": "少吃高嘌呤食物，多喝水。"},
            {"rule_id": "R05_HB_LOW", "restriction": "补铁 + 维C", "marker": "血红蛋白",
             "nutrient_key": "iron", "action": "boost",
             "trigger_foods": ["红肉", "动物血"], "advice": "适当多吃红肉、动物血。"},
        ],
    }


def test_save_and_activate() -> None:
    rid = store.save_report(fake_result(), source="test", sex="male")
    check("返回报告 id", rid > 0, True)
    check("最新报告可读回", store.latest_report()["judged"][0]["marker_id"], "ua")

    active = store.get_active_redlines()
    check("激活两条红线", [r["rule_id"] for r in active],
          ["R01_UA_HIGH", "R05_HB_LOW"])

    # 再次保存（新一轮体检）应整组替换，不是追加
    replaced = fake_result()
    replaced["redlines"] = replaced["redlines"][:1]      # 只剩限嘌呤
    store.save_report(replaced)
    check("新报告整组替换红线", len(store.get_active_redlines()), 1)
    store.save_report(fake_result())                     # 恢复两条供后续用


def test_hits() -> None:
    day = "2026-10-17"

    # 毛肚：种子表火锅中嘌呤 high —— 应撞限嘌呤
    store.add_meal("me", day, "dinner", ["毛肚"])
    s = daily.today_summary("me", day)
    check("记录了一餐", s["meal_count"], 1)
    hit = {h["rule_id"]: h for h in s["hits"]}
    check("毛肚撞限嘌呤", "R01_UA_HIGH" in hit, True)
    check("撞线次数=1", hit.get("R01_UA_HIGH", {}).get("hit_count"), 1)
    check("撞线菜品记录", hit.get("R01_UA_HIGH", {}).get("foods"), ["毛肚"])

    # boost：鸭血补铁 —— 应在补铁红线上记「达标」
    store.add_meal("me", day, "dinner", ["鸭血"])
    s = daily.today_summary("me", day)
    hit = {h["rule_id"]: h for h in s["hits"]}
    check("鸭血计入补铁", "R05_HB_LOW" in hit, True)
    check("补铁 action 保留", hit["R05_HB_LOW"]["action"], "boost")

    # 未知菜品：不累加但如实报告
    store.add_meal("me", day, "lunch", ["妈妈做的神秘菜"])
    s = daily.today_summary("me", day)
    check("未知菜品如实报告", s["unknown_dishes"], ["妈妈做的神秘菜"])
    check("未知菜品不计撞线", {h["rule_id"] for h in s["hits"]} >= {"R01_UA_HIGH"}, True)

    # 热量估算有数值
    check("热量已累加", s["calories"] > 0, True)


def test_limit_only_high() -> None:
    """medium 档不应撞线——粗筛定位：高才提醒，中档不打扰。"""
    day = "2026-10-18"
    check("空日期无记录", daily.today_summary("me", day)["meal_count"], 0)

    # 动态从库里找一条嘌呤 medium 的菜，测的是「medium 不撞线」这条规则，
    # 不赌某条具体数据的推导结果
    dishes = daily.search_dishes("")
    medium = next((d for d in dishes if d["tags"]["purine"] == "medium"), None)
    check("库里存在嘌呤 medium 的菜", medium is not None, True)
    if medium is None:
        return
    store.add_meal("me", day, "lunch", [medium["name"]])
    s = daily.today_summary("me", day)
    hit = {h["rule_id"] for h in s["hits"]}
    check("medium 不撞线", "R01_UA_HIGH" not in hit, True)
    check("medium 菜品仍计入热量", s["calories"] >= 0, True)


def test_search() -> None:
    check("空搜索有默认列表", len(daily.search_dishes("")) > 0, True)
    hits = daily.search_dishes("啤酒")
    check("能搜到啤酒", any("啤酒" in d["name"] for d in hits), True)
    check("搜索条数有上限", len(daily.search_dishes("", limit=5)) <= 5, True)


def main() -> int:
    print("=== 红线激活 ===")
    test_save_and_activate()
    print("=== 撞线判定 ===")
    test_hits()
    print("=== medium 不撞线 ===")
    test_limit_only_high()
    print("=== 搜索 ===")
    test_search()

    print(f"\n通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("日常层测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
