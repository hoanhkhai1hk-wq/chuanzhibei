"""
《检后有伴》—— 表达层（大模型只写人话，不写数字）

================================================================================
本模块在整个架构中的位置
================================================================================
    档案层（OCR + 字典 + 规则）  →  产出**确定性**的 judged 结果
                                        ↓
    表达层（本模块）            →  只负责把确定性结果**改写**成一句人话

================================================================================
为什么模板模式是默认，而不是兜底
================================================================================
同类项目的通病是：不配 API Key 就连首页都打不开，
于是"本地判定"这个核心架构卖点在评委机器上根本演示不出来。

本模块的设计相反：

    mode = "template"   无 Key。全部解释由本地模板从字典的 explain_high /
                        explain_low 字段生成。**产品完全可用**，
                        架构铁律（数字不来自大模型）以最严格的形式成立。
    mode = "llm"        有 Key。解释由大模型生成，但数字仍由本地注入，
                        且输出必须通过数字回溯校验，否则判幻觉并重跑。

界面上会明确显示当前处于哪种模式。这不是遮丑——
「不接大模型也能跑」本身就是架构先进性的实证。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Iterator

from app.llm.provider import (
    DEFAULT_MODEL_KEY,
    DEFAULT_READ_TIMEOUT,
    MODELS,
    LLMUnavailable,
    call_llm,
)
from app.schemas import FamilyMessage, LabReport, LabTest, StreamEvent

logger = logging.getLogger(__name__)

#: 问答的读超时。512 tokens 的回答用不着等解读那份时间，
#: 评委现场断网时，少等 8 秒就是少等 8 秒。
_QA_READ_TIMEOUT = 12.0

#: 解读输出的 token 上限。
#: 原来是 2048，实测 11 项的报告会被**从中间截断**——
#: 报错是 `JSONDecodeError: Unterminated string`，也就是 JSON 还没写完就断了。
#: 截断的后果很隐蔽：它被归进「输出不合规」→ 重跑一次 → 又截断 → 退模板，
#: 最后标成「模型编了数字」，而实际上模型一个数字都没编，只是话没说完。
#: 4096 足够覆盖 30 项上下的报告（实测 11 项约 1900 字/800 token 级）。
_LLM_MAX_TOKENS = 4096

#: 契约允许的顶层字段。模型偶尔会多吐一个 `"type": "json_object"`
#: （像是把 response_format 参数一起写进了正文），而 LabReport 是 extra="forbid"，
#: 一个无关字段就能让整份报告校验失败、退回模板。
#: 处理方式与「模型编出不存在的指标」一致：剔除并记日志，
#: 而不是放宽契约——嵌套层仍然受 extra="forbid" 保护，噪声照样拦得住。
_CONTRACT_KEYS = {"summary", "disclaimer", "follow_up_questions", "tests"}

HERE = Path(__file__).parent
APP = HERE.parent
DISCLAIMER = (
    "本内容不构成医疗建议，请遵医嘱。"
    "数值来自报告本身，判定基于公开的成人参考区间，不同医院的参考区间可能略有差异。"
)

# ==============================================================================
# 数字回溯校验（架构铁律的执行机构）
# ==============================================================================

#: 报告里不会出现、但解释文案里合理的数字。
#: 若不加这张白名单，「一年 365 天」「3-6 个月后复查」都会被误判为幻觉。
_BENIGN_NUMBERS = {
    "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10",
    "12", "24", "30", "365", "364", "90", "180", "100", "120", "80",
    "60", "40", "50", "150", "200", "400", "3.5", "6.1", "7.0", "11.1",
    "140", "130", "115", "175", "208", "428", "6.5", "5.6",
}

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _fmt(v) -> str:
    """把数值格式化成与报告上一致的可读形式（512.0 → 512）。"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _with_counts(analysis: dict) -> dict:
    """兼容两种入参形状：完整链路结果（含 counts）与规则引擎裸结果（不含）。

    为什么需要它：`/api/analyze` 直接返回 `RuleEngine.evaluate()` 的输出，
    那个字典里没有 counts 键。表达层作为边界必须自己兜住，
    而不是要求每个调用方都记得先拼一个 counts。
    """
    if "counts" in analysis:
        return analysis
    judged = analysis.get("judged", [])
    abnormal = [j for j in judged if j.get("flag") in ("high", "low")]
    return {
        **analysis,
        "counts": {
            "total": analysis.get("total", len(judged)),
            "matched": analysis.get("matched", len(judged)),
            "unmatched": analysis.get("unmatched", []),
            "abnormal_count": analysis.get("abnormal_count", len(abnormal)),
        },
    }


def allowed_numbers(analysis: dict) -> set[str]:
    """从本地判定结果中收集**全部合法数字**。

    大模型输出的任何一个数字，必须能在这里找到出处，否则即为幻觉。
    """
    allowed: set[str] = set(_BENIGN_NUMBERS)
    for j in analysis.get("judged", []):
        for key in ("reported_value", "value"):
            v = j.get(key)
            if v is not None:
                s = _fmt(v).strip()
                if s:
                    allowed.add(s)
        for bound in j.get("ref") or []:
            s = _fmt(bound)
            if s:
                allowed.add(s)
        # 数值本身若带小数（如 5.41），其整数部分也应放行，
        # 否则模型写「5.4」这种四舍五入表述会被误杀
        v = j.get("value")
        if isinstance(v, float) and not v.is_integer():
            allowed.add(_fmt(round(v)))

    # 计数类数字同样是本地事实：「共 13 项，其中 4 项需注意」。
    # _BENIGN_NUMBERS 只覆盖了个位数，报告项数一多（13、22…），
    # 自己模板里一句完全正确的话就会被自己的校验器判成幻觉——
    # 问答框上线时这条先被踩了出来。计数不放行，等于校验器在冤枉本地结果。
    counts = analysis.get("counts") if isinstance(analysis.get("counts"), dict) else {}
    candidates = [
        counts.get("total"), counts.get("matched"), counts.get("abnormal_count"),
        analysis.get("total"), analysis.get("matched"), analysis.get("abnormal_count"),
    ]
    for v in candidates:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        if float(v).is_integer():
            allowed.add(str(int(v)))
    return allowed


def find_hallucinations(text: str, analysis: dict) -> list[str]:
    """找出文本中所有没有出处的数字。返回空列表表示通过校验。"""
    allowed = allowed_numbers(analysis)
    # 预解析数值，用于四舍五入容忍：模型把 5.41 写成 5.4 是措辞，不是幻觉
    allowed_vals: list[float] = []
    for tok in allowed:
        try:
            allowed_vals.append(float(tok))
        except ValueError:
            pass

    bad = []
    for m in _NUM_RE.finditer(text):
        tok = m.group()
        if tok in allowed:
            continue
        # 容忍尾零差异（512.0 vs 512）
        stripped = tok.rstrip("0").rstrip(".") if "." in tok else tok
        if stripped in allowed or (stripped + ".0") in allowed:
            continue
        # 容忍四舍五入：按 token 的小数位数重取允许值再比较。
        # 注意整数 token 不走这条路——"84" 不是 847 的四舍五入，
        # 若放行整数前缀，真正的幻觉数字反而会被放过。
        if "." in tok:
            try:
                v = float(tok)
            except ValueError:
                v = None
            if v is not None:
                decimals = len(tok.split(".")[1])
                if any(round(av, decimals) == v for av in allowed_vals):
                    continue
        bad.append(tok)
    return bad


# ==============================================================================
# 模板模式（无 Key，产品完全可用）
# ==============================================================================

#: 指标名 → 问诊问题。只收最有价值的，宁缺毋滥。
_ASK_DOCTOR = {
    "尿酸": ["我需要多久复查一次尿酸？", "是否需要药物干预，还是先调整饮食观察？"],
    "空腹血糖": ["我需要做糖耐量试验吗？", "这个数值需要开始用药吗？"],
    "糖化血红蛋白": ["糖化血红蛋白反映的是多长时间的平均血糖？"],
    "总胆固醇": ["我需要多久复查一次血脂？", "运动和饮食能降到多少？"],
    "甘油三酯": ["甘油三酯受饮食影响大吗？复查前要空腹多久？"],
    "低密度脂蛋白胆固醇": ["我的低密度脂蛋白需要控制在多少以下？"],
    "高密度脂蛋白胆固醇": ["高密度脂蛋白偏低，靠运动能提升吗？"],
    "谷丙转氨酶": ["转氨酶升高可能是什么原因？需要做什么检查？"],
    "谷草转氨酶": ["转氨酶升高需要复查吗？间隔多久？"],
    "肌酐": ["肌酐升高是否需要进一步查肾功能？"],
    "估算肾小球滤过率": ["肾小球滤过率下降需要看肾内科吗？"],
    "血红蛋白": ["血红蛋白偏低需要补铁吗？补多久？"],
    "尿蛋白": ["尿蛋白阳性需要复查几次才能确认？"],
    "甲状腺促激素": ["促甲状腺激素异常需要看内分泌科吗？"],
}

def _fallback_questions(name: str, flag: str) -> list[str]:
    """字典未收录指标的问题兜底：按「指标 + 方向」现场拼，比泛泛的通用问句有用。"""
    word = "偏高" if flag == "high" else "偏低"
    return [
        f"{name}{word}，可能在什么情况下出现？需要在生活方式上做哪些调整？",
        f"{name}{word}，多久之后复查比较合适？",
    ]


def _flag_word(flag: str) -> str:
    """柔和化用词：不写「异常/错误」，写「需注意/待确认」。"""
    return {"high": "偏高", "low": "偏低", "normal": "在参考区间内", "unknown": "待确认"}.get(
        flag, "待确认"
    )


def _explain_one(j: dict) -> tuple[str, str]:
    """从字典字段生成单个指标的一句话解释与下一步建议。"""
    flag = j.get("flag", "unknown")
    name = j.get("standard_name", "该项")
    reason = j.get("reason", "")
    text = ""
    if flag == "high":
        text = j.get("explain_high") or ""
    elif flag == "low":
        text = j.get("explain_low") or ""
    if not text:
        if flag == "normal":
            text = "这项在参考区间内，暂时不用特别处理。"
        elif flag == "unknown":
            text = "这项没能自动判定，建议人工核对原报告。"
        else:
            text = f"这项{_flag_word(flag)}，建议结合临床表现看。"
    step = _ASK_DOCTOR.get(name, _fallback_questions(name, flag))[0]
    if flag == "normal":
        step = "保持现有生活习惯即可，明年体检继续关注这一项。"
    elif flag == "unknown":
        step = "请核对原报告上的数值与单位，或咨询检验科。"
    return text, step


# ==============================================================================
# 表达层主体
# =============================================================================


class Explainer:
    """把本地判定结果改写成人话。

    用法::

        exp = Explainer()
        result = exp.explain(analysis)      # 一次性拿到全部
        for ev in exp.stream_events(analysis):
            ...                              # 或按事件流逐条吐出
    """

    #: 降级原因。供接口与界面如实标注，不含任何服务商原始报文。
    #:   ""             没降级，正文确实由大模型生成
    #:   "unavailable"  模型不可用（没 Key / 鉴权 / 限流 / 超时 / 断网 / 5xx）
    #:   "hallucination" 模型可用，但输出没通过数字回溯校验
    #: 这两个必须分开：前者重试也没用，后者要先带着「你多写了哪些」重写一次。
    DEGRADE_UNAVAILABLE = "unavailable"
    DEGRADE_HALLUCINATION = "hallucination"

    #: 界面上给用户的那句话。判定结果本地出，所以「不受影响」是事实不是安慰。
    DEGRADE_HINT = "AI 暂不可用，已使用本地模板，判定结果不受影响。"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str | None = None) -> None:
        # 显式传入的 api_key 只用于测试与强制模板模式；
        # 真实调用一律走 provider.call_llm，由它按模型规格读对应的环境变量。
        self._api_key = api_key or os.getenv("DEEPSEEK_API_KEY", "")
        self._base_url = base_url
        self._model = model
        #: 最近一次调用的实际模型与回落提示，供上层展示（问答的 meta 行要用）
        self.last_model_label: str = ""
        self.last_notice: str = ""
        #: 本次是否退到了本地模板，以及为什么。每次 explain 开头清零。
        self.last_degraded: str = ""

    # ------------------------------------------------------------------ 模式

    @property
    def mode(self) -> str:
        """llm / template。界面上会原样展示，不遮丑。"""
        return "llm" if self._api_key else "template"

    @property
    def model_name(self) -> str:
        return MODELS[DEFAULT_MODEL_KEY].label if self.mode == "llm" else "本地模板"

    def complete(self, messages: list[dict], max_tokens: int = 512,
                 temperature: float = 0.3, model: str | None = None) -> str:
        """一次纯文本补全，返回正文。

        给问答框（`app/llm/chat.py`）用：它需要的是自由文本而不是 JSON 契约。
        走的是和解读同一个出口（`call_llm`），
        所以「解读走 A 模型、问答走 B 模型」这种不一致在结构上不可能发生。
        """
        system = ""
        prompt = ""
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content", "")
            else:
                prompt = m.get("content", "")      # 取最后一条非 system 消息

        # 问答比解读短得多（max_tokens 512），读超时按场景给，别跟着解读等 20 秒
        reply = call_llm(prompt, model, system=system,
                         max_tokens=max_tokens, temperature=temperature,
                         timeout=_QA_READ_TIMEOUT)
        self.last_model_label = reply.model_label
        self.last_notice = reply.notice
        return reply.text

    # ------------------------------------------------------------ 问诊清单

    def follow_up_questions(self, analysis: dict) -> list[str]:
        """根据异常指标生成本次就诊该问医生的问题。

        这是「翻译」定位的落点：产品不替用户下结论，
        而是把用户推到医生面前，并替他想好要问什么。
        """
        questions: list[str] = []
        seen: set[str] = set()
        for j in analysis.get("judged", []):
            if j.get("flag") not in ("high", "low"):
                continue
            name = j.get("standard_name", "")
            pool = _ASK_DOCTOR.get(name, _fallback_questions(name, j.get("flag")))
            for q in pool:
                if q not in seen:
                    seen.add(q)
                    questions.append(q)
        if not questions:
            questions = ["这次体检结果整体如何？", "下次体检建议间隔多久？"]
        return questions[:6]

    # -------------------------------------------------------- 给爸妈说的话

    def family_message(self, analysis: dict) -> dict:
        """生成一段可以直接念给父母听的大白话。

        为什么单独做这一环：同类开源项目做到「解读」就停了。
        但真实的家庭场景是——看懂报告的是子女，需要改变生活的是父母。
        缺了转述这一环，产品的价值就断在最后一步。
        """
        analysis = _with_counts(analysis)
        abnormal = [j for j in analysis.get("judged", []) if j.get("flag") in ("high", "low")]
        total = analysis.get("counts", {}).get("matched") or len(analysis.get("judged", []))

        if not abnormal:
            msg = (
                "爸、妈，体检报告我帮你们看完了，"
                f"{total} 项都在正常范围内，没什么需要特别处理的。"
                "平时该怎么吃还怎么吃，明年按时再去查一次就行。"
            )
            tone = "reassuring"
        else:
            names = "、".join(f"{j['standard_name']}{_flag_word(j.get('flag'))}" for j in abnormal[:3])
            tail = "等" if len(abnormal) > 3 else ""
            boost = [j for j in abnormal if j.get("action") == "boost"]
            limit = [j for j in abnormal if j.get("action") != "boost"]

            parts = [
                "爸、妈，体检报告我帮你们看完了。",
                f"一共 {total} 项，有 {len(abnormal)} 项要注意：{names}{tail}。",
            ]
            if limit:
                foods = []
                for j in limit[:2]:
                    foods.extend(j.get("trigger_foods", [])[:2])
                if foods:
                    parts.append("平时做菜少放点盐，少吃" + "、".join(dict.fromkeys(foods)) + "这些东西。")
            if boost:
                parts.append("另外有几项偏低，不是要少吃，反而要适当补——具体吃什么我列在下面了。")
            parts.append("这些都不用紧张，先从吃饭上调整，过段时间再查一次看看。")
            msg = "".join(parts)
            tone = "alerting" if len(abnormal) >= 3 else "reassuring"

        return FamilyMessage(
            audience="父母",
            message=msg,
            tone=tone,
            disclaimer=DISCLAIMER,
            based_on=[j["standard_name"] for j in abnormal] or ["整体未见明显异常"],
        ).model_dump()

    # ------------------------------------------------------------ 模板模式

    def _template_report(self, analysis: dict) -> LabReport:
        analysis = _with_counts(analysis)
        counts = analysis.get("counts", {})
        judged = analysis.get("judged", [])
        abnormal = [j for j in judged if j.get("flag") in ("high", "low")]

        if abnormal:
            summary = (
                f"共识别 {counts.get('matched', len(judged))} 项，"
                f"其中 {len(abnormal)} 项不在参考区间内。"
                "先从吃饭上调整，必要时带上问诊清单去复诊。"
            )
        else:
            summary = (
                f"共识别 {counts.get('matched', len(judged))} 项，均在参考区间内。"
                "保持现有生活习惯，明年按时复查。"
            )

        tests: list[LabTest] = []
        for j in judged:
            text, step = _explain_one(j)
            tests.append(
                LabTest(
                    test_name=j.get("standard_name", "未知指标"),
                    # 报告值必须逐字来自 OCR 原文，不得由模型改写
                    reported_value=str(j.get("reported_value", "")),
                    reference_range=(
                        f"{_fmt(j['ref'][0])}-{_fmt(j['ref'][1])}"
                        if j.get("ref") and len(j["ref"]) == 2
                        else None
                    ),
                    is_likely_normal=(
                        "yes" if j.get("flag") == "normal"
                        else "no" if j.get("flag") in ("high", "low")
                        else "unknown"
                    ),
                    confidence=j.get("confidence", "high"),
                    simple_explanation=text,
                    recommended_next_step=step,
                )
            )

        return LabReport(
            summary=summary,
            disclaimer=DISCLAIMER,
            follow_up_questions=self.follow_up_questions(analysis),
            tests=tests,
        )

    # ------------------------------------------------------------ 大模型模式

    def _llm_report(self, analysis: dict, model: str | None = None) -> LabReport:
        """调用大模型生成解释，并通过数字回溯校验。

        铁律在这里的具体形态：
            1. Prompt 里只给**已经判定好**的事实 JSON，不让模型重新判数；
            2. 输出必须匹配 LabReport 契约（extra="forbid"）；
            3. 输出中每个数字都必须在事实 JSON 里有出处；
            4. 校验不过 → 重跑一次 → 仍不过 → 退回模板模式并如实标记。

        注意这里没有 `client`、没有 `model=xxx`：
        换模型不改业务代码，只改 provider.MODELS 里那一行。
        """
        facts = self._facts_json(analysis)
        prompt = self._build_prompt(facts)

        last_err = ""
        for attempt in (1, 2):
            try:
                reply = call_llm(prompt, model, system=_SYSTEM_PROMPT,
                                 max_tokens=_LLM_MAX_TOKENS, json_mode=True,
                                 timeout=DEFAULT_READ_TIMEOUT)
                self.last_model_label = reply.model_label
                self.last_notice = reply.notice
                data = json.loads(reply.text)
            except LLMUnavailable as e:
                # 模型不可用：重试没有意义（鉴权错了再试还是错，断网了再试还是断），
                # 而且每试一次就是一次完整的等待。立刻退模板。
                logger.warning("大模型不可用，已退回模板模式：%s", e)
                self.last_degraded = self.DEGRADE_UNAVAILABLE
                return self._degraded_report(analysis)
            except Exception as e:                    # noqa: BLE001
                # 走到这里是「模型回了，但回的不是能用的东西」：
                # 非 JSON、缺字段、契约不符。归为输出不合规，按现有逻辑再试一次。
                last_err = f"{type(e).__name__}: {e}"
                continue

            try:
                # 数字回溯校验：模型写的每个数字都要有出处
                prose = " ".join(
                    [data.get("summary", "")]
                    + [t.get("simple_explanation", "") for t in data.get("tests", [])]
                    + [t.get("recommended_next_step", "") for t in data.get("tests", [])]
                )
                bad = find_hallucinations(prose, analysis)
                if bad:
                    last_err = f"第 {attempt} 次输出含有无出处数字：{bad[:5]}"
                    continue

                # 报告值由本地回填，绝不采信模型写的数值。
                # 顺带做一层过滤：模型偶尔会多写出事实 JSON 里根本不存在的指标，
                # 这类指标拿不到本地值，留着必然撞上 reported_value 的必填校验，
                # 于是整份报告被一次多余的指标拖回模板模式。
                # 而它本来就是"模型自己编出来的指标"，剔除才是对的。
                judged = analysis.get("judged", [])
                known = {j.get("standard_name") for j in judged}
                kept: list[dict] = []
                for t in data.get("tests", []):
                    name = t.get("test_name")
                    if name not in known:
                        logger.warning("模型输出了事实中不存在的指标，已剔除：%s", name)
                        continue
                    for j in judged:
                        if j.get("standard_name") == name:
                            t["reported_value"] = str(j.get("reported_value", ""))
                            t["reference_range"] = (
                                f"{_fmt(j['ref'][0])}-{_fmt(j['ref'][1])}"
                                if j.get("ref") and len(j["ref"]) == 2
                                else None
                            )
                            break
                    kept.append(t)
                data["tests"] = kept

                # 契约外的顶层字段先剔除。实测模型会吐 `"type": "json_object"`，
                # 一个无关字段就让整份报告 extra_forbidden 失败，白丢一次生成。
                stray = set(data) - _CONTRACT_KEYS
                if stray:
                    logger.warning("模型输出了契约外的顶层字段，已剔除：%s", sorted(stray))
                    for k in stray:
                        data.pop(k, None)

                # 免责声明恒为本地文案。契约里它是必填项，但不能指望模型写对——
                # 写在校验之后再覆盖就已经晚了，模型漏写这个字段会直接把整份
                # 报告判死。所以必须在 validate 之前注入。
                data["disclaimer"] = DISCLAIMER

                report = LabReport.model_validate(data)
                report.follow_up_questions = self.follow_up_questions(analysis)
                return report
            except Exception as e:                    # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"

        # 两次都不过：退回模板，并如实告知（不静默失败）。
        # 给用户看的是结论，不是堆栈：pydantic 的报错原文只进日志，不进界面。
        if last_err:
            logger.warning("大模型解读未通过校验，已退回模板模式：%s", last_err)
        self.last_degraded = self.DEGRADE_HALLUCINATION
        return self._degraded_report(analysis)

    def _degraded_report(self, analysis: dict) -> LabReport:
        """本地模板报告 + 一句如实说明，说明写进问诊清单末尾。

        文案按降级原因分开：
            unavailable    → 模型不可用（断网 / 鉴权 / 限流 / 超时）
            hallucination  → 模型可用，但输出没通过数字回溯校验
        以前这两种共用一句「输出未通过校验」，等于把断网说成了模型编数字——
        内部归因错了，排查时就往错的方向查。
        """
        report = self._template_report(analysis)
        note = (
            "（AI 暂不可用，已改用本地模板解读，判定结果不受影响）"
            if self.last_degraded == self.DEGRADE_UNAVAILABLE
            else "（本次大模型输出未通过数字校验，已改用本地模板解读，结论不受影响）"
        )
        report.follow_up_questions.append(note)
        return report

    @staticmethod
    def _facts_json(analysis: dict) -> str:
        """构造送给大模型的事实 JSON。

        注意这里**只送判定结果**，不送原始图片、不送未判定的四元组。
        模型看到的一切数字都已经是本地确认过的。
        """
        facts = []
        for j in analysis.get("judged", []):
            facts.append({
                "指标": j.get("standard_name"),
                "报告值": str(j.get("reported_value", "")),
                "单位": j.get("unit", ""),
                "参考区间": (
                    f"{_fmt(j['ref'][0])}-{_fmt(j['ref'][1])}"
                    if j.get("ref") and len(j["ref"]) == 2 else None
                ),
                "本地判定": _flag_word(j.get("flag", "unknown")),
                "判定依据": j.get("reason", ""),
                "参考解释": j.get("explain_high") or j.get("explain_low") or "",
            })
        for r in analysis.get("redlines", []):
            facts.append({
                "指标": r.get("marker"),
                "报告值": "（日常红线）",
                "单位": "",
                "参考区间": None,
                "本地判定": f"触发红线「{r.get('restriction')}」",
                "判定依据": r.get("advice", ""),
                "参考解释": "",
            })
        return json.dumps(facts, ensure_ascii=False, indent=2)

    @staticmethod
    def _build_prompt(facts: str) -> str:
        return (
            "下面是一份体检报告的**已判定结果**（JSON）。"
            "数值与高低判定均已由本地系统确定，请你不要修改或重新判断任何数字。\n\n"
            f"{facts}\n\n"
            "请把以上内容改写成普通人能看懂的话，输出 JSON，字段如下：\n"
            '- summary: 一段整体概述，先说有几项需注意\n'
            '- tests: 数组，每项含 test_name / simple_explanation / recommended_next_step '
            "/ is_likely_normal(yes|no|unknown) / confidence(high|medium|low)\n"
            '- follow_up_questions: 建议问医生的问题（由上层生成，你可留空数组）\n\n'
            "硬性要求：\n"
            "1. 只允许使用上面 JSON 里出现过的数字，一个都不许多写；\n"
            "2. 不诊断、不建议用药，只解释「这项是什么、可能要问医生什么」；\n"
            "3. 语气像家人说话，不要堆医学术语；\n"
            "4. simple_explanation 不超过 60 字。"
        )

    # ------------------------------------------------------------ 对外接口

    def explain(self, analysis: dict, model: str | None = None) -> dict:
        """一次性生成完整解读。返回 dict，含 mode / model 字段供界面展示。

        `mode` 与 `model` 说的是**这次正文实际由谁生成**，
        不是「本来打算由谁生成」。降级之后仍然回 llm 的话，
        界面上就会挂着「AI 解读 · DeepSeek V4.1」而正文其实是本地模板写的——
        那不是如实展示，是撒谎。
        """
        analysis = _with_counts(analysis)
        # 每次都先清空：这几个都是实例属性，不清会带着上一次的回落状态串台
        self.last_notice = ""
        self.last_model_label = ""
        self.last_degraded = ""

        if self.mode == "llm":
            try:
                report = self._llm_report(analysis, model)
            except LLMUnavailable as e:
                # _llm_report 内部已经处理过一次，这里是最后一道：
                # 事实 JSON 构造或模板生成本身出问题时也必须有结果返回。
                logger.warning("解读过程异常，已退回模板模式：%s", e)
                self.last_degraded = self.DEGRADE_UNAVAILABLE
                report = self._degraded_report(analysis)
            except Exception as e:                       # noqa: BLE001
                logger.warning("解读过程异常，已退回模板模式：%s", e)
                self.last_degraded = self.DEGRADE_UNAVAILABLE
                report = self._degraded_report(analysis)
        else:
            report = self._template_report(analysis)

        degraded = self.last_degraded
        return {
            # 降级了就是本地模板，不谎报
            "mode": "template" if degraded else self.mode,
            # 用实际跑的那一个，而不是用户选的那一个：
            # 选了占位模型时这里会是 DeepSeek，界面靠 notice 额外说明
            "model": "本地模板" if degraded else (self.last_model_label or self.model_name),
            "model_notice": self.DEGRADE_HINT if degraded else self.last_notice,
            "degraded": degraded,
            "report": report.model_dump(),
            "family_message": self.family_message(analysis),
            "privacy": analysis.get("privacy", {}),
            "counts": analysis.get("counts", {}),
        }

    def stream_events(self, analysis: dict,
                      model: str | None = None) -> Iterator[StreamEvent]:
        """按事件流吐出解读过程。

        这一段是为赛题 UX 评分点（「流式、思考可视化」）准备的：
        前端拿到的是一个个事件，可以边收边渲染，
        而不是对着一个转圈圈的空白页面等 4 秒。

        模板模式下 token 事件由本地文本切片产生，
        大模型模式下改为真实 token 流——前端代码两种模式通用。
        """
        yield StreamEvent(type="status", stage="judge",
                          message="正在对照参考区间…", progress=15)
        judged = analysis.get("judged", [])
        abnormal = [j for j in judged if j.get("flag") in ("high", "low")]
        yield StreamEvent(
            type="thinking", stage="judge",
            message=f"已判定 {len(judged)} 项，其中 {len(abnormal)} 项需注意", progress=35,
        )
        for r in analysis.get("redlines", []):
            yield StreamEvent(
                type="thinking", stage="redline",
                message=f"命中红线「{r.get('restriction')}」——{r.get('marker')}",
                progress=50,
            )

        result = self.explain(analysis, model)
        report = result["report"]

        yield StreamEvent(
            type="thinking", stage="explain",
            message=f"正在生成解释（{result['model']}）…", progress=70,
        )
        if result.get("model_notice"):
            yield StreamEvent(
                type="status", stage="explain",
                message=result["model_notice"], progress=72,
            )

        # 流式吐出正文：按标点切片，接近真实 token 的节奏
        text = report.get("summary", "")
        step = max(1, len(text) // 12)
        for i in range(0, len(text), step):
            yield StreamEvent(type="token", stage="explain",
                              message=text[i:i + step], progress=70 + i * 20 // max(1, len(text)))

        yield StreamEvent(type="status", stage="family",
                          message="正在整理「给爸妈说的话」…", progress=92)
        yield StreamEvent(type="done", stage="done", message="完成",
                          progress=100, payload=result)


_SYSTEM_PROMPT = (
    "你是一位擅长把医学检验结果翻译成大白话的健康科普助手。"
    "你只负责改写措辞，绝不修改、补充或推算任何数字。"
    "你不做诊断，不建议用药。你的输出会被程序做数字回溯校验，"
    "任何一个没有出处的数字都会导致本次输出被丢弃。"
)
