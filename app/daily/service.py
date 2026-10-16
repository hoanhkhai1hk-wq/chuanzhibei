"""日常层：点选菜品 → 本地累加 → 对照红线 → 今日提示

================================================================================
与档案层共用同一套规则引擎
================================================================================
档案层回答「什么不能多吃」（由报告判定产生红线），
日常层回答「今天碰了几条」（由菜品标签累加产生撞线）。
两层的连接物是 nutrient_key：红线规则说"盯住嘌呤"，
菜品标签说"这道菜嘌呤高"——匹配是纯粹的本地字符串比对，
不涉及任何模型。

================================================================================
为什么是「点选」而不是「拍照识别」
================================================================================
食堂就那么几十道菜，可搜索点选 30 秒完成；
拍照识别不准会打断主流程。拍照只作为后续增强。
"""

from __future__ import annotations

import json
from datetime import date as _date
from pathlib import Path

APP = Path(__file__).resolve().parent.parent
DISHES_F = APP / "dict" / "dishes.json"

DEFAULT_MEMBER = "me"

#: boost 型红线（补铁）的匹配关键词。
#: 菜品标签里没有 iron 档——铁含量在种子里没有数值，
#: 靠名称关键词匹配种子表里明确标注补铁的食材（鸭血/肝脏/红肉等）。
IRON_FOODS = ("鸭血", "猪血", "血豆腐", "肝", "牛肉", "羊肉", "菠菜", "橙", "猕猴桃")

_DISH_INDEX: dict[str, dict] | None = None
_DISHES: list[dict] | None = None


def _load() -> tuple[list[dict], dict[str, dict]]:
    """懒加载菜品库并建索引（名称与别名都指向同一条）。

    与指标字典同策略：进程内只读一次，不重复解析 JSON。
    """
    global _DISHES, _DISH_INDEX
    if _DISHES is None:
        doc = json.loads(DISHES_F.read_text(encoding="utf-8"))
        _DISHES = doc["dishes"]
        _DISH_INDEX = {}
        for d in _DISHES:
            for key in [d["name"], *d.get("aliases", [])]:
                _DISH_INDEX.setdefault(key, d)
    return _DISHES, _DISH_INDEX


def search_dishes(query: str = "", limit: int = 60) -> list[dict]:
    """按名称模糊搜索。空 query 返回常用分类的前若干条。"""
    dishes, _ = _load()
    q = (query or "").strip().lower()
    if not q:
        picked: list[dict] = []
        for cat in ("家常菜", "快餐", "饮品", "火锅", "主食", "酒类"):
            picked.extend(d for d in dishes if d["category"] == cat)
        return picked[:limit]
    hits = [d for d in dishes
            if q in d["name"].lower() or any(q in a.lower() for a in d.get("aliases", []))]
    return hits[:limit]


def _resolve(name: str) -> dict | None:
    _, index = _load()
    return index.get(name.strip())


def today_summary(member_id: str = DEFAULT_MEMBER,
                  date: str | None = None) -> dict:
    """当日汇总：已记录的餐 + 各红线被碰情况。全部本地计算。

    撞线判定：
        limit 型红线  —— 吃了 tags[nutrient_key] == high 的菜即记一次；
                          medium 不撞线（粗筛定位：高才提醒，中档不打扰）。
        boost 型红线  —— 吃到了补铁食材记一次（方向与 limit 相反，
                          撞线是好事，文案也说"达标"而不是"超了"）。
    """
    from app.data import db

    day = date or _date.today().isoformat()
    redlines = db.get_active_redlines(member_id)
    meals = db.meals_for_date(member_id, day)
    numerics = db.numerics_for_date(member_id, day)

    hits: dict[str, dict] = {}
    calories = 0.0
    unknown_dishes: list[str] = []
    meal_count = len(meals)

    for meal in meals:
        for name in meal["dishes"]:
            dish = _resolve(name)
            if dish is None:
                unknown_dishes.append(name)
                continue
            calories += dish.get("calories") or 0
            tags = dish.get("tags", {})
            for r in redlines:
                key, action = r["nutrient_key"], r.get("action", "limit")
                if action == "boost":
                    matched = any(k in dish["name"] for k in IRON_FOODS)
                else:
                    matched = tags.get(key) == "high"
                if not matched:
                    continue
                slot = hits.setdefault(r["rule_id"], {
                    "rule_id": r["rule_id"],
                    "restriction": r["restriction"],
                    "marker": r["marker"],
                    "action": action,
                    "hit_count": 0,
                    "foods": [],
                })
                slot["hit_count"] += 1
                if dish["name"] not in slot["foods"]:
                    slot["foods"].append(dish["name"])

    hit_list = sorted(hits.values(), key=lambda h: -h["hit_count"])
    return {
        "member_id": member_id,
        "date": day,
        "meal_count": meal_count,
        "calories": round(calories),
        "redlines_active": len(redlines),
        "hits": hit_list,
        "unknown_dishes": unknown_dishes,
        "meals": meals,
        "numerics": numerics,
    }
