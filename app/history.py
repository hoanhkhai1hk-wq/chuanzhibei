"""历史对比：多份报告的趋势差异（全部本地计算）

================================================================================
为什么这是壁垒功能
================================================================================
单份报告的解读，同类开源项目都能做；两份报告放在一起看趋势，
需要的是"留存 + 归一 + 对齐"三件事都做对了——这正好是本项目
档案层早就铺好的能力：reports 表存历史快照，指标已归一到标准名，
对比就是对齐 standard_name 之后的本地字符串与数值比对。

================================================================================
判定口径（简单、可解释，不玩花活）
================================================================================
    好转     上次异常（高/低）→ 这次正常
    恶化     上次正常 → 这次异常
    持续     两次都异常且方向相同
    反转     两次都异常但方向不同（如由高转低，需人工确认）
    新增     本次才出现的指标（字典/排版差异都会造成，如实标注）

热量、单位换算一概不做——对比的是"判定结果"而非"原始数值"，
口径与红线一致，结论才可解释。
"""

from __future__ import annotations

_VERDICT = {
    "improved": "好转",
    "worsened": "加重",
    "persistent": "持续异常",
    "reversed": "方向反转",
}


def compare(old: dict, new: dict) -> dict:
    """对比两份判定结果。old/new 均为规则引擎 evaluate() 的输出。"""
    old_map: dict[str, dict] = {}
    for j in old.get("judged", []):
        old_map.setdefault(j.get("standard_name", ""), j)

    rows: list[dict] = []
    counts = {"improved": 0, "worsened": 0, "persistent": 0, "reversed": 0,
              "new": 0, "normal": 0}

    for j in new.get("judged", []):
        name = j.get("standard_name", "")
        o = old_map.pop(name, None)
        nf = j.get("flag", "unknown")

        if o is None:
            counts["new"] += 1
            if nf in ("high", "low"):
                rows.append(_row(name, None, j, "new", "新增（本次首次出现）"))
            continue

        of = o.get("flag", "unknown")
        if of == nf:
            if nf in ("high", "low"):
                counts["persistent"] += 1
                rows.append(_row(name, o, j, "persistent", _VERDICT["persistent"]))
            else:
                counts["normal"] += 1
            continue

        if of in ("high", "low") and nf == "normal":
            counts["improved"] += 1
            rows.append(_row(name, o, j, "improved", _VERDICT["improved"]))
        elif of == "normal" and nf in ("high", "low"):
            counts["worsened"] += 1
            rows.append(_row(name, o, j, "worsened", _VERDICT["worsened"]))
        elif of in ("high", "low") and nf in ("high", "low"):
            counts["reversed"] += 1
            rows.append(_row(name, o, j, "reversed", _VERDICT["reversed"]))
        # unknown 的变化不进 rows（OCR 没读出来的对比没有意义），只计数

    return {
        "counts": counts,
        "rows": rows,
        "unmatched_before": [n for n in old_map],   # 上次有、这次没有
    }


def _row(name: str, o: dict | None, j: dict, verdict: str, text: str) -> dict:
    return {
        "standard_name": name,
        "verdict": verdict,
        "verdict_text": text,
        "from_flag": o.get("flag") if o else None,
        "to_flag": j.get("flag"),
        "from_value": str(o.get("reported_value", "")) if o else None,
        "to_value": str(j.get("reported_value", "")),
        "reason": j.get("reason", ""),
    }
