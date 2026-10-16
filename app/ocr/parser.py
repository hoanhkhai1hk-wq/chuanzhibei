"""
报告文本 → 四元组解析器

================================================================================
位置
================================================================================
    图片 →[OCR 引擎]→ 文本 →[本文件]→ 四元组 →[规则引擎]→ 红线

本解析器**故意做得朴素**：不依赖任何指标体系，纯靠版式规律。
指标归一与区间判定是下游规则引擎的职责（app/rules/engine.py），
这里只负责把一行行文字切成结构化字段。

================================================================================
这一版正则踩过两个坑，都由自检抓出，改动前请先跑
    python tests/ocr_bench/bench.py --selftest
================================================================================

坑 1：name 的字符类里不能含空白
    否则会跨过间隔把后面的数值一起吞掉。
    实测表现：血红蛋白那行的 name 吃成「血红蛋白(Hb)  135  g/L」，
    value 取到了参考区间的下界 130。

坑 2：不能用一条正则同时切出 unit 与 ref
    unit 含数字（×10⁹/L）时，遇到没有单位的行就会把参考区间一起吃掉；
    而 unit 不容数字时，含上标的单位又切不出来。
    解法：正则只切出 name/value/箭头/剩余部分，
    再用 REF_TAIL_RE 从剩余部分的**尾部**反推参考区间。
    这个缺陷曾让解析器静默漏掉约 1/3 的行（66% 而非 100%）。
"""

from __future__ import annotations

import re

LINE_RE = re.compile(
    r"^\s*(?P<name>[^\s\d][\u4e00-\u9fa5A-Za-z0-9（）()·/.\-]*)"
    r"\s+"
    r"(?P<value>[<>]?\s*[-+]?\d+(?:[.]\d+)?)"
    r"\s*(?P<arrow>[↑↓*★※]*)\s*"
    r"(?P<rest>.*)$"
)

#: 从行尾反推「参考区间」，剩下的部分算作单位。
#: unit 必须非贪婪，否则会把区间一起吃掉。
REF_TAIL_RE = re.compile(
    r"^(?P<unit>.*?)\s*"
    r"(?P<ref>\d+(?:[.]\d+)?\s*[-~至]\s*\d+(?:[.]\d+)?)\s*$"
)

#: 表头行等噪声，直接跳过
HEADER_WORDS = {"项目", "结果", "单位", "参考范围", "参考区间", "检验项目",
                "英文名", "缩写", "提示", "序号", "名称"}


def parse_markers(text: str) -> list[dict]:
    """把一段 OCR 文本切成四元组列表。

    返回元素形如：
        {"name": "尿酸(UA)", "value": "512", "unit": "μmol/L", "ref": "208-428"}

    name 保留报告原文（可能带英文缩写），
    归一到标准指标名是规则引擎的工作。
    """
    out: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        name = (m.group("name") or "").strip()
        if not name or name in HEADER_WORDS:
            continue

        rest = (m.group("rest") or "").strip()
        unit, ref = "", ""
        if rest:
            t = REF_TAIL_RE.match(rest)
            if t:
                unit = (t.group("unit") or "").strip()
                ref = (t.group("ref") or "").replace(" ", "")
            else:
                unit = rest

        out.append({
            "name": name,
            "value": (m.group("value") or "").replace(" ", ""),
            "unit": unit,
            "ref": ref,
            "arrow": (m.group("arrow") or "").strip(),
        })
    return out
