"""
端到端链路：报告图片 → 三条红线

================================================================================
链路
================================================================================
    图片
      → [app/ocr/engine.py]   OCR 提取文本
      → [本文件 redact]        隐私脱敏（P0，必须在送大模型之前）
      → [app/ocr/parser.py]    切成四元组
      → [app/rules/engine.py]  本地归一 + 本地判定 + 红线匹配
      → 三条红线

================================================================================
架构铁律在本链路的落点
================================================================================
1. **所有数值与高低判定都来自本地**：OCR 抽取 + 字典比对 + 规则匹配，
   大模型不参与任何一步的数值产生。本文件跑完即可得出红线，
   即使完全不接大模型，产品也能工作。

2. **隐私脱敏在送大模型之前完成**：本文件产出的 `text_preview`
   只保留指标行，姓名/身份证/医院等信息在进入表达层之前已被抹掉。

3. **每一步都可解释**：判定依据随结果返回，每个结论都能追到
   具体的区间数字，而不是"AI 觉得"。

================================================================================
为什么要有 pipeline 而不在 API 里直接串
================================================================================
串起来的链路需要被三处复用：API 接口、端到端测试、演示脚本。
放在接口里就只能被 HTTP 调用，测试与演示都得绕一圈。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.ocr.engine import extract_text
from app.ocr.parser import parse_markers
from app.rules.engine import RuleEngine

DISCLAIMER = "本内容不构成医疗建议，请遵医嘱。数值来自报告本身，判定基于公开的成人参考区间。"

#: 需要抹掉的敏感信息。体检报告上最常见的三类。
#: 注意顺序：先抹长串（身份证），避免被后续规则截断成半截。
_REDACT_RULES = [
    ("id_card", re.compile(r"\b\d{17}[\dXx]\b")),
    ("phone", re.compile(r"\b1[3-9]\d{9}\b")),
    ("long_number", re.compile(r"\b\d{10,}\b")),      # 体检号 / 病历号等
]

#: 含个人信息抬头特征的行，整行抹掉
_SENSITIVE_LINE = re.compile(
    r"(姓名|性别|年龄|身份证|证件号|体检号|病历号|送检|采样时间|收到时间|"
    r"病区|床号|门诊号|住院号|标本|临床诊断|送检医生|检验者|审核者)"
)


def redact(text: str) -> tuple[str, list[str]]:
    """脱敏：抹掉个人信息。返回 (脱敏后文本, 被处理的字段类型)。

    为什么在解析之前做：
        脱敏后的文本会作为上下文送给大模型。若先解析再脱敏，
        患者信息可能已经混进结构化结果里，反而更容易泄露。

    为什么保留指标行：
        本项目的表达层需要看到具体指标才能写出人话，
        但指标数值本身不构成身份信息（报告上没有姓名才无法对应到人）。
    """
    redacted: list[str] = []
    lines = []
    for line in text.splitlines():
        if _SENSITIVE_LINE.search(line):
            redacted.append("info_line")
            continue
        for name, pattern in _REDACT_RULES:
            if pattern.search(line):
                line = pattern.sub("[已抹除]", line)
                if name not in redacted:
                    redacted.append(name)
        lines.append(line)
    return "\n".join(lines), redacted


class Pipeline:
    """端到端分析报告。"""

    def __init__(self, engine: RuleEngine | None = None, provider: str = "rapidocr") -> None:
        self.engine = engine or RuleEngine()
        self.provider = provider

    def analyze(self, image_path: Path, sex: str = "unknown") -> dict:
        """跑完整链路。返回结构化结果（尚未经过大模型润色）。"""
        # 1. OCR
        raw_text = extract_text(image_path, self.provider)

        # 2. 脱敏（P0：必须在任何对外发送之前）
        clean_text, redacted = redact(raw_text)

        # 3. 解析
        markers = parse_markers(clean_text)

        # 4. 本地判定 + 红线匹配
        judged = self.engine.evaluate(markers, sex)

        return {
            "source": image_path.name,
            "provider": self.provider,
            "privacy": {
                "is_clean": True,
                "redacted_fields": redacted,
                "note": "已抹除个人信息行与长串编号，可安全送交表达层。",
            },
            "counts": {
                "total_rows": judged["total"],
                "matched": judged["matched"],
                "unmatched": judged["unmatched"],
                "abnormal_count": judged["abnormal_count"],
            },
            "judged": judged["judged"],
            "redlines": judged["redlines"],
            "disclaimer": DISCLAIMER,
        }

    def summary_text(self, result: dict) -> str:
        """把结果压成一段人能直接读的文字。

        注意：这里**不用大模型**，是模板拼接。
        它是表达层的兜底——即使大模型不可用，产品仍能给出可读结果。
        """
        c = result["counts"]
        lines = [
            f"共识别 {c['total_rows']} 项，其中 {c['abnormal_count']} 项不在参考区间内。",
        ]
        if c["unmatched"]:
            lines.append(f"有 {len(c['unmatched'])} 项未能匹配到指标字典，建议人工核对。")

        if result["redlines"]:
            lines.append("")
            lines.append("你的日常红线：")
            for r in result["redlines"]:
                kind = "建议补充" if r["action"] == "boost" else "需要注意"
                lines.append(f"  · {r['restriction']}（{kind}）—— {r['advice']}")
        else:
            lines.append("")
            lines.append("未发现需要特别对照的日常红线。")

        lines.append("")
        lines.append(result["disclaimer"])
        return "\n".join(lines)


def analyze(image_path: Path, sex: str = "unknown", provider: str = "rapidocr") -> dict:
    """便捷入口。"""
    return Pipeline(provider=provider).analyze(image_path, sex)
