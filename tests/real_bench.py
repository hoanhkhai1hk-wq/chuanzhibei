"""真实报告实测：把手上 8 张真实体检报告跑一遍完整链路。

================================================================================
为什么必须做这件事
================================================================================
此前全部结论建立在 12 份**合成样本**上——合成样本排版规整、字体单一、
没有反光与褶皱，等于开卷考试。真实报告才是闭卷，而评委问的恰恰是
「你这套东西在真实材料上到底行不行」。

产出两个数：
    字典命中率 = 解析出的行里，能归一到指标字典的比例
    判定可用率 = 命中行里能拿到参考区间并给出高/低判定的比例
另外如实记录未命中的指标名——这些就是下一步扩充字典的清单。

用法：
    python tests/real_bench.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.ocr.engine import extract_text          # noqa: E402
from app.ocr.parser import parse_markers         # noqa: E402
from app.pipeline import redact                  # noqa: E402
from app.rules.engine import RuleEngine          # noqa: E402

HERE = Path(__file__).resolve().parent
SAMPLES = HERE / "ocr_bench" / "samples"
OUT = HERE / "ocr_bench" / "results"
SEX = "male"


def run_one(path: Path, engine: RuleEngine) -> dict:
    raw = extract_text(path)
    clean, redacted = redact(raw)
    markers = parse_markers(clean)
    res = engine.evaluate(markers, SEX)

    judged = res["judged"]
    decidable = [j for j in judged if j["flag"] in ("high", "low", "normal")]
    return {
        "file": path.name,
        "ocr_chars": len(clean),
        "rows": len(markers),
        "matched": res["matched"],
        "unmatched": res["unmatched"],
        "decidable": len(decidable),
        "abnormal": res["abnormal_count"],
        "redlines": [r["restriction"] for r in res["redlines"]],
        "redacted": redacted,
        "sample_rows": [
            f'{j["standard_name"]} {j["reported_value"]} → {j["flag"]}'
            for j in judged[:4]
        ],
    }


def main() -> int:
    images = sorted([p for p in SAMPLES.iterdir()
                     if p.suffix.lower() in (".jpg", ".jpeg", ".png")
                     and not p.name.startswith("synth_")])
    if not images:
        print("[跳过] samples/ 里没有真实报告图")
        return 0

    engine = RuleEngine()
    OUT.mkdir(parents=True, exist_ok=True)
    results = []

    print(f"真实报告实测 · 共 {len(images)} 份\n")
    print(f'{"文件":<38}{"OCR字符":>8}{"解析行":>7}{"命中":>6}{"可判定":>7}{"异常":>5}{"红线":>5}')
    print("─" * 76)

    tot_rows = tot_matched = tot_decidable = 0
    all_unmatched: list[str] = []

    for p in images:
        try:
            r = run_one(p, engine)
        except Exception as e:                     # noqa: BLE001
            print(f"{p.name:<38}  失败：{type(e).__name__}: {e}")
            continue
        results.append(r)
        tot_rows += r["rows"]
        tot_matched += r["matched"]
        tot_decidable += r["decidable"]
        all_unmatched.extend(r["unmatched"])
        print(f'{r["file"]:<38}{r["ocr_chars"]:>8}{r["rows"]:>7}'
              f'{r["matched"]:>6}{r["decidable"]:>7}{r["abnormal"]:>5}'
              f'{len(r["redlines"]):>5}')

    print("─" * 76)
    hit = (tot_matched / tot_rows) if tot_rows else 0
    use = (tot_decidable / tot_matched) if tot_matched else 0
    print(f"{'合计':<38}{'':>8}{tot_rows:>7}{tot_matched:>6}{tot_decidable:>7}")
    print(f"\n字典命中率  {hit:.0%}   （解析出的行里能归一到字典的比例）")
    print(f"判定可用率  {use:.0%}   （命中行里能给出高/低判定的比例）")

    from collections import Counter
    miss = Counter(all_unmatched)
    print(f"\n未命中指标（下一步扩充字典的清单，共 {len(miss)} 个）：")
    for name, n in miss.most_common(30):
        print(f"   {n}×  {name}")

    payload = {
        "note": "真实报告实测。命中率与判定可用率的定义见 tests/real_bench.py 文件头。",
        "sex": SEX,
        "files": results,
        "totals": {"rows": tot_rows, "matched": tot_matched,
                   "decidable": tot_decidable,
                   "hit_rate": round(hit, 4), "usable_rate": round(use, 4)},
        "unmatched_top": [{"name": n, "count": c} for n, c in miss.most_common(50)],
    }
    (OUT / "real_report.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n明细已写入 {OUT / 'real_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
