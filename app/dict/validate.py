"""
指标字典与红线规则的一致性校验

为什么需要这个脚本：
    两个 JSON 之间存在交叉引用（markers.redline ↔ redlines.rule_id、
    redlines.marker_id ↔ markers.id），而这类数据错误**不会抛异常**——
    程序照常运行，只是在真正匹配时悄悄失败。
    等到演示时才发现「尿酸偏高没触发限嘌呤」，就来不及了。

用法：
    python app/dict/validate.py
退出码：0 = 全部通过；1 = 存在问题
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
APP = HERE.parent
MARKERS_F = HERE / "markers.json"
REDLINES_F = APP / "rules" / "redlines.json"

REQUIRED_MARKER = ["id", "standard_name", "aliases", "unit"]
REQUIRED_RULE = ["rule_id", "marker", "marker_id", "flag", "restriction",
                 "nutrient_key", "action", "trigger_foods", "advice_template"]


def main() -> int:
    errors: list[str] = []
    warns: list[str] = []

    markers_doc = json.loads(MARKERS_F.read_text(encoding="utf-8"))
    rules_doc = json.loads(REDLINES_F.read_text(encoding="utf-8"))
    markers = markers_doc["markers"]
    rules = rules_doc["rules"]

    print(f"指标字典：{len(markers)} 条")
    print(f"红线规则：{len(rules)} 条")
    print()

    # ---- 1. 字段完整性 ----
    for m in markers:
        for k in REQUIRED_MARKER:
            if not m.get(k):
                errors.append(f"指标 {m.get('id')} 缺少必需字段 {k}")
    for r in rules:
        for k in REQUIRED_RULE:
            if k not in r:
                errors.append(f"规则 {r.get('rule_id')} 缺少必需字段 {k}")

    # ---- 2. id 唯一 ----
    ids = [m["id"] for m in markers]
    dup = {i for i in ids if ids.count(i) > 1}
    if dup:
        errors.append(f"指标 id 重复：{sorted(dup)}")

    # ---- 3. 别名归一化后不冲突 ----
    # 必须与 engine.py 用同一套归一化逻辑，否则校验会漏掉真正的冲突。
    # 曾经的教训：校验用的是简单 lower()，而引擎会去掉括号，
    # 于是「WBC(尿)」在校验里不冲突、在引擎里却和「WBC」撞车，
    # 导致血常规白细胞计数被误判成尿检项目。
    import re as _re

    def norm_like_engine(s: str) -> str:
        s = _re.sub(r"\s+", "", str(s or ""))
        s = _re.sub(r"[（(].*?[)）]", "", s)
        return s.lower()

    alias_map: dict[str, str] = {}
    for m in markers:
        for a in m["aliases"]:
            key = norm_like_engine(a)
            if key in alias_map and alias_map[key] != m["id"]:
                errors.append(
                    f"别名冲突 '{a}'（归一化后='{key}'）："
                    f"同时属于 {alias_map[key]} 与 {m['id']}。"
                    f"尿检类别名请写成「尿WBC」而非「WBC(尿)」，"
                    f"因为归一化会去掉括号。"
                )
            else:
                alias_map[key] = m["id"]

    # ---- 4. 规则的 marker_id 必须在字典里 ----
    known_ids = set(ids)
    for r in rules:
        mid = r.get("marker_id")
        if mid and mid not in known_ids:
            # 血压是测量值，不在检验字典里，属于合法例外
            if mid == "bp_systolic":
                warns.append(f"{r['rule_id']} 引用 {mid}（血压为测量值，字典中不收录，属预期行为）")
            else:
                errors.append(f"{r['rule_id']} 引用的 marker_id '{mid}' 在字典中不存在")

    # ---- 5. markers.redline 必须能匹配到规则前缀 ----
    rule_prefixes = {r["rule_id"].split("_")[0] for r in rules}
    for m in markers:
        rl = m.get("redline")
        if rl and rl not in rule_prefixes:
            errors.append(f"指标 {m['id']} 声明 redline='{rl}'，但规则中没有该前缀")

    # ---- 6. 反向：每条规则应至少被一个指标引用 ----
    for r in rules:
        pre = r["rule_id"].split("_")[0]
        if not any(m.get("redline") == pre for m in markers):
            warns.append(f"规则 {r['rule_id']}（前缀 {pre}）没有被任何指标引用")

    # ---- 7. action 与 nutrient_key 的搭配 ----
    for r in rules:
        if r["nutrient_key"] == "iron" and r["action"] != "boost":
            errors.append(f"{r['rule_id']} nutrient_key=iron 必须搭配 action=boost，"
                          f"否则会把「补铁」当成「限制含铁食物」，方向完全反了")
        if r["action"] == "boost" and r["nutrient_key"] != "iron":
            warns.append(f"{r['rule_id']} action=boost 但 nutrient_key 不是 iron，请确认")

    # ---- 8. 模板占位符 ----
    for r in rules:
        if "{trigger_foods}" in r["advice_template"] and not r["trigger_foods"]:
            errors.append(f"{r['rule_id']} 模板用了 {{trigger_foods}} 但列表为空")

    # ---- 9. 覆盖情况统计 ----
    print("各红线覆盖的指标：")
    by_pre: dict[str, list[str]] = {}
    for r in rules:
        by_pre.setdefault(r["rule_id"].split("_")[0], []).append(r["marker"])
    for pre in sorted(by_pre):
        print(f"   {pre}: {len(by_pre[pre])} 条 —— {', '.join(by_pre[pre])}")

    boost = [r for r in rules if r["action"] == "boost"]
    print(f"\nboost 类规则（建议补充，非限制）：{len(boost)} 条")
    for r in boost:
        print(f"   {r['rule_id']}: {r['restriction']}")

    # ---- 输出 ----
    print()
    if warns:
        print(f"提示 {len(warns)} 条：")
        for w in warns:
            print(f"   · {w}")
    if errors:
        print(f"\n错误 {len(errors)} 条：")
        for e in errors:
            print(f"   ✗ {e}")
        print("\n校验未通过。")
        return 1

    print("校验通过 ✅ 字典与规则交叉引用一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
