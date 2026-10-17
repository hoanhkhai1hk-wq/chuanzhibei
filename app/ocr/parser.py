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

================================================================================
真实报告实测（tests/real_bench.py）暴露的另外三个坑
================================================================================
合成样本排版规整，跑出 100% 命中率属于开卷考试；
真实报告一上手命中率只有 15%，问题全在版式上：

坑 3：行首带序号
    真实报告常印成「3淋巴细胞计数  1.48」，首字符是数字，
    原正则要求 name 首字符非数字，这一行**整行匹配不上直接丢失**。
    解法：name 前允许一段可选序号。

坑 4：表头与抬头被当成指标
    「独墅湖科教创新区医院化验报告单」会被解析成名为"医院"的指标。
    解法：噪声词黑名单 + 要求指标名至少含 2 个汉字。

坑 5：数值与参考区间不同行（真实报告里极常见）
    版式是「名称 数值」换行再「参考范围 单位」，
    原解析器只在同一行找区间，于是拿不到区间、判不了高低。
    解法：本行无区间时，去看下一行的**开头**是否以区间起始，是则取用。
"""

from __future__ import annotations

import re

#: name 前允许一段可选序号（"3淋巴细胞计数"）。
#: 注意必须配合下方"至少 2 个汉字"的检查，否则 "4.00~10.00..." 会切出垃圾名。
LINE_RE = re.compile(
    r"^\s*(?:\d{1,2}\s*)?"
    r"(?P<name>[^\s\d][\u4e00-\u9fa5A-Za-z0-9（）()·/.\-]*)"
    r"\s+"
    r"(?P<value>[<>]?\s*[-+]?\d+(?:[.]\d+)?)"
    r"\s*(?P<arrow>[↑↓*★※]*)\s*"
    r"(?P<rest>.*)$"
)

#: 行首区间（用于跨行关联：「名称 数值」的下一行以区间开头）
REF_HEAD_RE = re.compile(
    r"^\s*(?P<ref>\d+(?:[.]\d+)?\s*[-~至]\s*\d+(?:[.]\d+)?)"
)

#: 明显不是指标的抬头/页脚噪声
NOISE_RE = re.compile(
    r"医院|报告单|化验|检验报告|体检报告|标本|样本|科室|申请|临床诊断|条码|"
    r"编号|No[:：]|备注|提示说明|送检|检验者|审核者|采集|采样|接收|打印|发票|"
    r"电话|地址|门诊|住院|床号|病区|医师|姓名|性别|年龄|本报告|医嘱|收费|状态|注:"
)

_CJK = re.compile(r"[\u4e00-\u9fa5]")

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
    lines = text.splitlines()
    out: list[dict] = []
    src_idx: list[int] = []         # 每个四元组来自原文的第几行，用于跨行找参考区间

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        m = LINE_RE.match(line)
        if not m:
            continue
        name = (m.group("name") or "").strip()
        if not name or name in HEADER_WORDS:
            continue
        # 噪声抬头 + 至少 2 个汉字：这两条把"医院报告单""4.00~10.00"这类挡在门外
        if NOISE_RE.search(name):
            continue
        if len(_CJK.findall(name)) < 2:
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
        src_idx.append(i)

    # 跨行关联：本行没拿到参考区间时，看它的下一行是否以区间开头。
    # 真实报告的常见版式是「名称 数值」与「参考范围 单位」分两行印，
    # 只在同行找区间会大量拿不到区间，进而判不了高低。
    for k, item in enumerate(out):
        if item["ref"]:
            continue
        nxt = src_idx[k] + 1
        if nxt >= len(lines):
            continue
        head = REF_HEAD_RE.match(lines[nxt])
        if head:
            item["ref"] = head.group("ref").replace(" ", "")

    return out
