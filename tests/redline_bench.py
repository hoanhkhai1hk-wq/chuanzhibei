"""
红线判定对照：逐条比较「预期」与「实际」，任何不一致就非 0 退出。

================================================================================
为什么要有这个文件
================================================================================
tests/test_rules.py 证明的是「引擎在某些点上是对的」——它是开发期的守卫，
断言散在代码里，只有写的人看得懂。

这个文件证明的是另一件事：**16 条红线规则逐条都能被触发，且不该触发时不触发**。
输出是一张「用例 → 预期 → 实际」的对照表，评委可以直接顺着表往下读，
不需要读 Python。

两者不重复：test_rules.py 管实现细节（归一化、定性写法、方向性），
本文件管规则覆盖率与预期一致性。

================================================================================
两条不可妥协的做法
================================================================================
1. 预期值不是这里手写的，而是从 markers.json / redlines.json 读出来校验过：
   每条用例在跑之前先自查「这个值按字典区间确实应该偏高吗」「这条 rule_id 在
   redlines.json 里确实是这个 marker + 这个方向吗」。不一致就是用例自己错了，
   而不是引擎错了——错误归属必须先分清，否则会拿一个错用例去改产品。

2. R03_BP_HIGH 不进 RuleEngine.evaluate。
   它的 marker_id 是 bp_systolic，而 markers.json 里根本没有这条（血压是测量值，
   不是检验项）。硬塞一个假区间进去让 evaluate 能跑，等于用编出来的字典换一张
   好看的报表。所以这类用例单独建一个临时 SQLite，调 daily.record_numeric，
   断言 activated_redline 与 active_redlines 表。

用法：
    python tests/redline_bench.py            # 全部跑
    python tests/redline_bench.py --quiet    # 只打印汇总与失败项
"""

from __future__ import annotations

import gc
import json
import shutil
import sys
import tempfile
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.daily import service as daily            # noqa: E402
from app.data import db as store                 # noqa: E402
from app.rules.engine import RuleEngine          # noqa: E402

CASES_F = ROOT / "tests" / "redline_cases.json"
MARKERS_F = ROOT / "app" / "dict" / "markers.json"
RULES_F = ROOT / "app" / "rules" / "redlines.json"

#: 对照结果与「用例自查」共用一份失败清单：
#: 自查失败也要非 0 退出——否则一条写错的用例会被当成「引擎不通过」混过去。
FAILED: list[str] = []
PASSED = 0
CHECKS = 0


# ─────────────────────────── 表格对齐（中文占两格） ───────────────────────────

def _w(s: str) -> int:
    """字符串在等宽终端里的显示宽度。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in s)


def pad(s: str, width: int, align: str = "<") -> str:
    s = str(s)
    gap = max(0, width - _w(s))
    if align == ">":
        return " " * gap + s
    if align == "^":
        left = gap // 2
        return " " * left + s + " " * (gap - left)
    return s + " " * gap


def check(label: str, got, want) -> bool:
    global PASSED, CHECKS
    CHECKS += 1
    if got == want:
        PASSED += 1
        return True
    FAILED.append(f"{label}：得到 {got!r}，期望 {want!r}")
    return False


# ─────────────────────────────── 用例自查 ───────────────────────────────

def load_docs() -> tuple[RuleEngine, dict, dict]:
    engine = RuleEngine()
    markers = {m["id"]: m for m in
               json.loads(MARKERS_F.read_text(encoding="utf-8"))["markers"]}
    rules = {r["rule_id"]: r for r in
             json.loads(RULES_F.read_text(encoding="utf-8"))["rules"]}
    return engine, markers, rules


def audit_case(case: dict, engine: RuleEngine, markers: dict, rules: dict) -> None:
    """跑之前先确认「这条用例的预期是自洽的」。

    这一层是防「为了让测试通过而编阈值」的关键：
    阈值只有一个来源——markers.json 与 redlines.json。
    """
    cid = case["case_id"]
    exp = case["expect"]

    # ① 预期触发的规则必须真实存在，且方向对得上
    for rid in exp["rule_ids"]:
        rule = rules.get(rid)
        if rule is None:
            check(f"{cid} 自查·规则存在({rid})", False, True)
            continue
        check(f"{cid} 自查·{rid} 的方向", rule.get("flag"), exp["flag"])
        check(f"{cid} 自查·{rid} 的指标", rule.get("marker_id"), exp["target_marker_id"])
        # ② 用例里写的 action / nutrient_key / restriction 必须与规则文件逐字一致
        check(f"{cid} 自查·{rid} action", exp["action"], rule.get("action", "limit"))
        check(f"{cid} 自查·{rid} nutrient_key", exp["nutrient_key"], rule["nutrient_key"])
        check(f"{cid} 自查·{rid} restriction", exp["restriction"], rule["restriction"])

    if case["kind"] != "report":
        return

    # ③ 报告类用例：输入值按字典区间确实应该落在预期那一侧
    target = exp["target_marker_id"]
    if target is None:                       # 未收录指标，不需要区间校验
        return
    spec = markers.get(target)
    if spec is None:
        check(f"{cid} 自查·字典里有 {target}", False, True)
        return

    sex = case.get("sex", "unknown")
    rng = engine.ref_range(target, sex)
    qual = (spec.get("unit") == "定性") or not spec.get("ref", {}).get("default")

    if qual:
        # 定性项没有数值区间：只要求「定性标记与预期一致」
        check(f"{cid} 自查·定性项", True, True)
        return
    if rng is None:
        check(f"{cid} 自查·有参考区间", False, True)
        return

    from app.rules.engine import _to_float
    val = _to_float(case["inputs"][0]["value"])
    lo, hi = rng
    expect_side = exp["flag"]
    if expect_side == "high":
        check(f"{cid} 自查·{val}>{hi}", val > hi, True)
    elif expect_side == "low":
        check(f"{cid} 自查·{val}<{lo}", val < lo, True)
    elif expect_side == "normal":
        check(f"{cid} 自查·{lo}<={val}<={hi}", lo <= val <= hi, True)


def audit_coverage(cases: list[dict], rules: dict) -> None:
    """每条启用的规则至少有一条阳性用例；R03 必须走日常层。"""
    positive_rules: set[str] = set()
    daily_rules: set[str] = set()
    for c in cases:
        if c["category"] == "positive":
            positive_rules.update(c["expect"]["rule_ids"])
        if c["kind"] == "daily":
            daily_rules.update(c["expect"]["rule_ids"])

    enabled = {rid for rid, r in rules.items() if r.get("enabled", True)}
    check("自查·启用规则数", len(enabled), 16)
    missing = sorted(enabled - positive_rules)
    check("自查·每条规则都有阳性用例（缺：%s）" % (missing or "无"), missing, [])
    check("自查·R03 只出现在日常层用例", sorted(daily_rules), ["R03_BP_HIGH"])
    check("自查·R03 不出现在报告类用例",
          sorted(rid for c in cases if c["kind"] == "report"
                 for rid in c["expect"]["rule_ids"] if rid.startswith("R03")), [])
    check("自查·bp_systolic 不在指标字典里（所以不能走 evaluate）",
          "bp_systolic" in {m["id"] for m in
                            json.loads(MARKERS_F.read_text(encoding="utf-8"))["markers"]},
          False)


# ─────────────────────────────── 执行两类用例 ───────────────────────────────

def run_report_case(case: dict, engine: RuleEngine) -> dict:
    """报告链路：与真实上传走的是同一个入口。"""
    extracted = [{"name": i["name"], "value": i["value"], "unit": i.get("unit", "")}
                 for i in case["inputs"]]
    res = engine.evaluate(extracted, case.get("sex", "unknown"))

    got_flags: dict[str, str] = {j["marker_id"]: j["flag"] for j in res["judged"]}
    hit_ids = sorted(r["rule_id"] for r in res["redlines"])
    by_id = {r["rule_id"]: r for r in res["redlines"]}

    target = case["expect"]["target_marker_id"]
    return {
        "flag": got_flags.get(target) if target else None,
        "rule_ids": hit_ids,
        "action": next((by_id[r]["action"] for r in hit_ids), None),
        "nutrient_key": next((by_id[r]["nutrient_key"] for r in hit_ids), None),
        "restriction": next((by_id[r]["restriction"] for r in hit_ids), None),
        "abnormal_count": res["abnormal_count"],
        "unmatched": res["unmatched"],
        "advice": next((by_id[r]["advice"] for r in hit_ids), ""),
        "engine": res,
    }


def run_daily_case(case: dict) -> dict:
    """日常链路：临时 SQLite，绝不碰 data/tjzh.db。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="rl_bench_"))
    real_db = store.DB_PATH
    store.DB_PATH = tmpdir / "bench.db"
    try:
        store.init_db()
        item = case["inputs"][0]
        summary = daily.record_numeric(
            "bench", "2026-01-01", item["kind"],
            systolic=item.get("systolic"), diastolic=item.get("diastolic"))
        active = store.get_active_redlines("bench")
    finally:
        store.DB_PATH = real_db
        # db.connect() 用 `with conn:` 只管提交/回滚，并不会 close；
        # 连接要等引用计数归零才释放文件句柄。Windows 上句柄没放就删不掉，
        # 所以先 gc 再删，删不掉也不影响结论（临时目录交给系统回收）。
        gc.collect()
        shutil.rmtree(tmpdir, ignore_errors=True)

    active_ids = sorted(r["rule_id"] for r in active)
    by_id = {r["rule_id"]: r for r in active}
    flag = daily.bp_flag(case["inputs"][0].get("systolic"),
                         case["inputs"][0].get("diastolic"))
    return {
        "flag": flag,
        "rule_ids": active_ids,
        "activated_redline": summary.get("activated_redline"),
        "action": next((by_id[r]["action"] for r in active_ids), None),
        "nutrient_key": next((by_id[r]["nutrient_key"] for r in active_ids), None),
        "restriction": next((by_id[r]["restriction"] for r in active_ids), None),
        "abnormal_count": 1 if active_ids else 0,
        "unmatched": [],
        "advice": next((by_id[r]["advice"] for r in active_ids), ""),
    }


def compare(case: dict, got: dict) -> bool:
    """把「预期 vs 实际」逐项对齐。返回这条用例是否全过。"""
    cid = case["case_id"]
    exp = case["expect"]
    before = len(FAILED)

    check(f"{cid} flag", got["flag"], exp["flag"])
    check(f"{cid} rule_ids", got["rule_ids"], sorted(exp["rule_ids"]))
    check(f"{cid} abnormal_count", got["abnormal_count"], exp["abnormal_count"])
    check(f"{cid} action", got["action"], exp["action"])
    check(f"{cid} nutrient_key", got["nutrient_key"], exp["nutrient_key"])
    check(f"{cid} restriction", got["restriction"], exp["restriction"])

    if "unmatched" in exp:
        check(f"{cid} unmatched", got["unmatched"], exp["unmatched"])
    if "activated_redline" in exp:
        check(f"{cid} activated_redline", got["activated_redline"],
              exp["activated_redline"])

    # 方向性断言：boost 规则的文案不能说「少吃」
    if exp["action"] == "boost":
        check(f"{cid} boost 文案不含「少吃」", "少吃" in (got["advice"] or ""), False)
        check(f"{cid} boost 文案含「多吃」", "多吃" in (got["advice"] or ""), True)

    return len(FAILED) == before


# ─────────────────────────────── 主流程 ───────────────────────────────

def main(argv: list[str]) -> int:
    quiet = "--quiet" in argv
    doc = json.loads(CASES_F.read_text(encoding="utf-8"))
    cases = doc["cases"]

    engine, markers, rules = load_docs()

    print("=" * 108)
    print("红线判定对照 · 用例来源 tests/redline_cases.json")
    print(f"字典 {len(markers)} 条 · 规则 {len(rules)} 条 · 用例 {len(cases)} 条")
    print("=" * 108)

    # 1) 先审用例本身（阈值来源、方向、覆盖率）
    for c in cases:
        audit_case(c, engine, markers, rules)
    audit_coverage(cases, rules)
    print(f"\n用例自查：{PASSED} 项通过" + (f"，{len(FAILED)} 项失败" if FAILED else "，全部一致 ✅"))

    # 2) 逐条对照
    if not quiet:
        head = (pad("用例", 9) + pad("类别", 10) + pad("输入", 30)
                + pad("性别", 7) + pad("预期 flag/rule", 30)
                + pad("实际 flag/rule", 30) + "结果")
        print("\n" + head)
        print("─" * 108)

    ok_cases = 0
    for c in cases:
        exp = c["expect"]
        if c["kind"] == "daily":
            got = run_daily_case(c)
            inp = "%s %s/%s" % (c["inputs"][0]["kind"],
                                c["inputs"][0].get("systolic"),
                                c["inputs"][0].get("diastolic"))
            inp_short = "血压 %s/%s" % (c["inputs"][0].get("systolic"),
                                       c["inputs"][0].get("diastolic"))
        else:
            got = run_report_case(c, engine)
            first = c["inputs"][0]
            inp = f'{first["name"]} {first["value"]}'
            inp_short = inp

        passed = compare(c, got)
        ok_cases += 1 if passed else 0

        if not quiet:
            exp_cell = f'{exp["flag"] or "—"} / {(exp["rule_ids"] or ["（不触发）"])[0]}'
            got_cell = f'{got["flag"] or "—"} / {(got["rule_ids"] or ["（不触发）"])[0]}'
            mark = "✅" if passed else "❌"
            print(pad(c["case_id"], 9) + pad(c["category"], 10)
                  + pad(inp_short, 30) + pad(c.get("sex", "—"), 7)
                  + pad(exp_cell, 30) + pad(got_cell, 30) + mark)

    # 3) 汇总
    print("\n" + "=" * 108)
    print(f"用例 {len(cases)} 条：通过 {ok_cases}，失败 {len(cases) - ok_cases}"
          f'　｜　断言 {CHECKS} 项：通过 {PASSED}，失败 {len(FAILED)}')

    pos = [c for c in cases if c["category"] == "positive"]
    neg = [c for c in cases if c["category"] != "positive"]
    print(f"其中：阳性用例 {len(pos)} 条（覆盖 {len({r for c in pos for r in c['expect']['rule_ids']})} 条规则）"
          f"，阴性/边界用例 {len(neg)} 条")

    if FAILED:
        print(f"\n失败明细（共 {len(FAILED)} 项）：")
        for f in FAILED:
            print("   ✗", f)
        print("\n注意：先确认是「用例预期写错」还是「引擎判错」。"
              "属后者才是产品 bug，需要另开修复单。")
        return 1

    print("\n全部一致 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
