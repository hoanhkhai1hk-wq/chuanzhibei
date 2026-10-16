"""从参考数据种子表构建菜品库（app/dict/dishes.json）

================================================================================
为什么用脚本生成而不是手工维护
================================================================================
种子表（参考数据/中国品牌食品营养_种子表.md）有 300+ 条、列结构不统一，
手工转录既慢又必然抄错。脚本做三件事：

    1. 解析 markdown 表格（按表头自适应列映射）
    2. 从「数值 + 名称关键词 + 分区」推导六档标签
    3. 输出与 Dish 契约一致的 JSON，供日常层加载

================================================================================
标签推导的诚实边界（重要）
================================================================================
推导是**启发式**：脂肪/碳水有数值依据，嘌呤/钠/酒只能靠名称关键词。
所以菜品库的定位是「日常对照的粗筛」——告诉用户"今天碰到几条线"，
不是精确营养计算。这个边界写进了输出文件的 meta 与 README，
产品文案也不承诺精确克数。

用法：
    python -m app.dict.build_dishes
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).parent
APP = HERE.parent
REPO = APP.parent
SEED = REPO / "参考数据" / "中国品牌食品营养_种子表.md"
OUT = HERE / "dishes.json"

# ────────────────────────────── 解析 ──────────────────────────────

_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def _num(s: str) -> float | None:
    m = _NUM.search(str(s or "").replace(",", ""))
    return float(m.group()) if m else None


def _clean(s: str) -> str:
    return s.strip().strip("*").strip()


# 一级分区 → 分类
SECTION_CATEGORY = {
    "奶茶": "饮品", "酒类": "酒类", "甜品": "甜品",
    "便利店": "便利店", "外卖": "快餐", "超市": "超市",
    "乳制品": "乳制品", "蛋白": "乳制品",
}

# 二级分区（中式常见菜品内部）→ 分类
SUB_CATEGORY = {
    "家常菜": "家常菜", "粥": "主食", "粉": "主食", "面": "主食",
    "外卖平台套餐": "快餐", "火锅食材": "火锅", "火锅蘸料": "火锅",
    "烧烤": "烧烤", "早餐": "早餐", "甜品": "甜品", "饮品": "饮品",
}


def parse_tables(text: str) -> list[dict]:
    """解析全部表格行。按表头自适应列映射，行记录带分区上下文。"""
    items: list[dict] = []
    h1 = h2 = ""          # 当前一级 / 二级分区标题
    colmap: dict[str, int] = {}   # 归一化字段名 -> 列下标

    def _canonical(cell: str) -> str | None:
        """表头单元格 -> 归一化字段名。表头常带单位后缀（热量(kcal)）。"""
        for key, canon in (("热量", "kcal"), ("蛋白质", "protein"), ("脂肪", "fat"),
                           ("碳水", "carb"), ("规格", "spec"), ("备注", "note")):
            if key in cell:
                return canon
        for key, canon in (("产品", "name"), ("菜品", "name"),
                           ("食材", "name"), ("蘸料", "name"), ("酒款", "name")):
            if cell.startswith(key) or key in cell:
                return canon
        return None

    for line in text.splitlines():
        raw = line.strip()
        if raw.startswith("## "):
            title = _clean(raw[3:])
            if "使用说明" in title:
                break            # 说明区的示例表格不是数据，到此为止
            h1, h2 = title, ""
            continue
        if raw.startswith("### "):
            h2 = _clean(raw[4:])
            continue
        if not raw.startswith("|"):
            continue
        cells = [c.strip() for c in raw.strip("|").split("|")]
        # 分隔行：**所有**单元格都是横线/冒号。
        # 这里曾用过 any()——只要有一格是空就把整行跳过，
        # 结果备注留空的真实数据行（如「伯牙绝弦（正常糖）」）全被误杀。
        if cells and all(set(c) <= {"-", ":", " "} for c in cells):
            continue
        low = [c.lower() for c in cells]
        # 表头行：含「热量」或「产品」「菜品」等字段名，据此重建列映射
        if any(k in c for c in low for k in ("热量", "产品", "菜品", "食材", "蘸料", "酒款")):
            colmap = {}
            for i, cell in enumerate(cells):
                canon = _canonical(cell)
                if canon and canon not in colmap:
                    colmap[canon] = i
            continue

        def cell_at(canon: str, default: str = "") -> str:
            i = colmap.get(canon)
            return cells[i] if i is not None and i < len(cells) else default

        name = _clean(cell_at("name", cells[0]))
        if not name or name in ("", "-"):
            continue

        items.append({
            "name": name,
            "spec": _clean(cell_at("spec")),
            "brand": h2 if h1.startswith("奶茶") or h1.startswith("酒类") else "",
            "section": h1,
            "subsection": h2,
            "kcal": _num(cell_at("kcal")),
            "protein": _num(cell_at("protein")),
            "fat": _num(cell_at("fat")),
            "carb": _num(cell_at("carb")),
            "note": _clean(cell_at("note")),
        })
    return items


# ──────────────────────────── 标签推导 ────────────────────────────

#: 嘌呤高的关键词。动物内脏、浓汤、部分海鲜是 R01 的典型触发物
_PURINE_HIGH = re.compile(r"内脏|肝|腰|脑|肚|心|毛肚|浓汤|汤底|沙丁|凤尾|贝|蚝|蛤|蚌|蟹|鱼子")
#: 嘌呤中等：普通鱼虾肉、豆制品（含量介于中间，不必一刀切）
_PURINE_MED = re.compile(r"鱼|虾|肉|啤酒|豆腐|鸭血")

#: 钠高的关键词：腌制、加工、重酱
_SODIUM_HIGH = re.compile(
    r"腌|咸|榨菜|酸菜|酱菜|方便面|火腿|午餐肉|香肠|腊肉|培根|酱|腐乳|蘸料|麻酱|沙茶|酱油|泡面")
#: 钠中等：快餐主食类
_SODIUM_MED = re.compile(r"汉堡|披萨|比萨|炸鸡|薯条|汉堡|便当|套餐|拌面|小面|米线|热干面")

#: 铁丰富的关键词（R05 补铁红线的 boost 匹配用）
_IRON = re.compile(r"鸭血|猪血|血豆腐|肝|牛肉|红肉|菠菜|橙|猕猴桃|瘦")

#: 高糖关键词（没有碳水数值时的兜底）。备注也算：「糖+油」「糖多」是种子里的人话标注
_SUGAR_HIGH_NAME = re.compile(r"糖醋|拔丝|蛋糕|甜品|冰淇淋|可乐|汽水|冰红茶|果茶|蜂蜜")

#: 高脂关键词（没有脂肪数值时的兜底）
_FAT_HIGH_NAME = re.compile(r"五花|肥|油炸|油碟|麻酱|薯条|炸鸡|炸|奶油|芝士|起司|黄油|油多")


def category_of(it: dict) -> str:
    for key, cat in SUB_CATEGORY.items():
        if key in it["subsection"]:
            return cat
    for key, cat in SECTION_CATEGORY.items():
        if key in it["section"]:
            return cat
    return "其他"


def derive_tags(it: dict) -> dict:
    """从数值与名称推导六档标签。规则见文件头注释的「诚实边界」。"""
    name = it["name"]
    cat = category_of(it)
    fat, carb = it["fat"], it["carb"]

    # —— 酒：酒类分区即高（R06 的 nutrient_key 已指向 alcohol 档）
    alcohol = "high" if cat == "酒类" else "low"
    # 啤酒嘌呤也高，其余酒低
    purine = "high" if (cat == "酒类" and "啤" in name) else "low"

    # —— 嘌呤
    if cat != "酒类":
        if _PURINE_HIGH.search(name) or "火锅" in it["subsection"] and _PURINE_MED.search(name):
            purine = "high"
        elif _PURINE_MED.search(name):
            purine = "medium"

    # —— 钠
    if _SODIUM_HIGH.search(name) or _SODIUM_HIGH.search(it["note"]):
        sodium = "high"
    elif _SODIUM_MED.search(name) or cat in ("快餐", "便利店"):
        sodium = "medium"
    else:
        sodium = "low"

    # —— 糖
    if re.search(r"无糖|零卡|不加糖|代糖", name):
        sugar = "low"
    elif "少" in name and ("糖" in name or "甜" in name):
        sugar = "medium"
    elif carb is not None and carb >= 30:
        sugar = "high"
    elif carb is not None and carb >= 15:
        sugar = "medium"
    elif _SUGAR_HIGH_NAME.search(name) or _SUGAR_HIGH_NAME.search(it["note"]) \
            or re.search(r"糖[+多]|冰糖", it["note"]) \
            or cat == "甜品" or "正常糖" in name:
        sugar = "high"
    else:
        sugar = "low"

    # —— 脂肪
    if fat is not None and fat >= 12:
        fat_tag = "high"
    elif fat is not None and fat >= 5:
        fat_tag = "medium"
    elif _FAT_HIGH_NAME.search(name) or _FAT_HIGH_NAME.search(it["note"]):
        fat_tag = "high"
    else:
        fat_tag = "low"

    return {"sodium": sodium, "sugar": sugar, "purine": purine,
            "fat": fat_tag, "alcohol": alcohol}


def main() -> int:
    text = SEED.read_text(encoding="utf-8")
    items = parse_tables(text)
    if len(items) < 100:
        print(f"[错误] 只解析出 {len(items)} 行，种子表格式可能变了，请检查")
        return 1

    dishes, seen = [], set()
    for it in items:
        if it["name"] in seen:                 # 同名条目只留第一条
            continue
        seen.add(it["name"])
        aliases = [it["name"]]
        if it["brand"] and it["brand"] not in it["name"]:
            aliases.append(f"{it['brand']}{it['name']}")
        dishes.append({
            "name": it["name"],
            "aliases": aliases,
            "category": category_of(it),
            "calories": it["kcal"],
            "portion_note": it["spec"] or ("每100g" if "火锅" in it["subsection"] else "一份"),
            "tags": derive_tags(it),
        })

    out = {
        "version": "0.1.0",
        "updated": "2026-10-17",
        "count": len(dishes),
        "source_primary": "参考数据/中国品牌食品营养_种子表.md（产品包装与公开平台数据）",
        "honesty_note": (
            "标签为启发式推导：脂肪/碳水有数值依据；嘌呤/钠/酒靠名称关键词。"
            "定位是日常对照的粗筛，不是精确营养计算。"
        ),
        "dishes": dishes,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")

    # 简报
    from collections import Counter
    cats = Counter(d["category"] for d in dishes)
    tags = Counter()
    for d in dishes:
        for k, v in d["tags"].items():
            if v == "high":
                tags[k] += 1
    print(f"已生成 {len(dishes)} 条菜品 → {OUT}")
    print(f"分类分布：{dict(cats)}")
    print(f"各档 high 数量：{dict(tags)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
