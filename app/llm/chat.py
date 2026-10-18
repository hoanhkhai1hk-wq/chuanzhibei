"""
《体检之后》—— 报告问答（表达层的第四段）

================================================================================
为什么问答不干脆「把报告丢给大模型随便聊」
================================================================================
那样看着聪明，实质上放弃了本项目唯一的架构资产：**数字的出处**。
模型一旦算出「你的尿酸超标 21%」，这句话没有任何本地依据——
用户没法核对，医生也没法核对，而它读起来非常像真的。

所以问答走的是和解读完全相同的三段：

    本地事实清单（每个数字都有出处）+ 用户问题  →  模型只写措辞  →  数字回溯校验
                                                        ↑
                                          校验不过就带着"你多写了哪些数字"重写一次

校验器直接复用 `app/llm/explainer.py` 的 `find_hallucinations`，
不另起一套标准——同一份报告，两处输出必须服从同一条铁律。

================================================================================
四类问题，四种本地前置
================================================================================
1. 指标类（"尿酸高是怎么回事"）
       按指标名/别名定位到 judged 里那一项，把 报告值 / 参考区间 / 本地判定 塞进事实
2. 饮食类（"我今天能吃火锅吗"）
       用菜品库从**问题文本里**捞出提到的菜，再拿当前红线逐条比对，
       得到「撞哪条线」的本地结论。这一条完全不需要模型——
       但模型的措辞更自然，所以仍然交给它写，只是事实部分由本地钉死。
3. 测量类（"血压高平时注意什么"）
       血压不在化验单上。**如实说明它来自日常层**，并带出「限钠」红线，
       而不是让模型硬编一个血压值。
4. 其它
       只给异常项清单与总体计数。

================================================================================
隐私
================================================================================
送给模型的内容只有：指标名、报告值、参考区间、本地判定、红线建议、菜品标签。
这些里面没有任何能指向具体个人的字段——判定结果里从来就没有姓名字段。
用户的问题本身先过 `mask_pii()`（只抹编号与自报姓名，不像报告那样整行丢弃）。
"""

from __future__ import annotations

import re
from typing import Optional

from app.llm.explainer import (
    DISCLAIMER,
    Explainer,
    _NUM_RE,
    _fmt,
    _flag_word,
    allowed_numbers,
    find_hallucinations,
)
from app.llm.provider import resolve_model
from app.pipeline import mask_pii

#: 问题长度上限。再长也不是在问一件事，只会把事实清单挤出上下文。
_MAX_QUESTION = 200

#: 事实清单里最多放几条指标（聚焦项 + 异常项）。
_MAX_FACTS = 12

#: 正常项只报名字、不报数字——少一个数字就少一次被误引用的机会。
_MAX_NORMAL_NAMES = 10

_KEY_TEXT = {
    "purine": "嘌呤", "sugar": "糖", "sodium": "钠",
    "fat": "脂肪", "alcohol": "酒精", "iron": "铁",
}

#: 血压/血糖/体重不在化验单上，但用户很自然会问。命中时如实说明来源。
_MEASURE_WORDS = {
    "血压": "报告上没有血压这一项——血压是当场测量的值，不来自化验单。"
            "你可以在「今日记录」里记一次，超过 140/90 会激活「限钠」红线。",
    "血糖": "报告上的血糖是抽血那一刻的值。平时的血糖要靠日常测量，"
            "在「今日记录」里记一次就能看到最近七天的曲线。",
    "体重": "报告上没有体重这一项。体重变化看趋势才有意义，"
            "在「今日记录」里记一次就行。",
}

_SYSTEM_PROMPT = (
    "你是家庭体检报告的解释员，服务对象是不懂医学术语的普通人。"
    "你只做一件事：把【事实清单】里的内容讲清楚。\n"
    "硬性要求（违反任何一条都算失败）：\n"
    "1. 只能使用【事实清单】里出现过的数字。清单里没有的数字一个都不要写，"
    "需要表达数量时用「少量」「一段时间」「定期」这类词代替。\n"
    "2. 不诊断疾病，不推荐药物、保健品或剂量，不给具体的摄入量建议。\n"
    "3. 不给百分比、倍数、超标幅度这类需要计算的结论。\n"
    "4. 清单里没有的信息，直接说「报告里没有这一项，建议问医生」，不要推测。\n"
    "5. 3 到 5 句话说清，像家人说话，不要堆医学术语，不要用 markdown 语法。"
)


class QaAnswerer:
    """把「用户问题 + 本地判定结果」变成一段可信的大白话回答。

    用法::

        qa = QaAnswerer(explainer=Explainer(), markers=RuleEngine().markers)
        reply = qa.answer("尿酸高是怎么回事", analysis)
    """

    def __init__(self, explainer: Optional[Explainer] = None,
                 markers: Optional[dict] = None) -> None:
        self.explainer = explainer or Explainer()
        #: marker_id → 字典条目，用来取 explain_high / explain_low。
        #: 由调用方注入而不是自己加载 JSON：字典在本项目里只有 RuleEngine 说了算。
        self.markers = markers or {}

    # ------------------------------------------------------------------ 入口

    def answer(self, question: str, analysis: dict, model: str | None = None) -> dict:
        """回答问题。返回可直接作为 `ChatReply` 的字典。

        `model` 是用户选的模型 key；不可用时由 provider 回落到默认模型，
        回落原因通过 `notice` 如实回给界面，不静默、不报错。
        """
        clean_q, masked = mask_pii((question or "").strip())
        clean_q = clean_q.strip().replace("\n", " ")[:_MAX_QUESTION]
        if not clean_q:
            return self._reply(
                answer="问题里除了个人信息没有别的内容，换个说法再问一次？",
                validated=True, attempts=0, facts=[], foods=[],
                analysis=analysis, mode="template", model="本地模板",
            )

        focus = self._focus_items(clean_q, analysis)
        foods = self._find_foods(clean_q)
        hits = self._food_hits(foods, analysis)
        measure = self._context_note(clean_q, analysis)

        facts, normal_names = self._build_facts(analysis, focus)

        text = ""
        validated = True
        attempts = 0
        used_llm = False

        if self.explainer.mode == "llm":
            try:
                text, validated, attempts = self._llm_answer(
                    clean_q, analysis, facts, normal_names, foods, hits, measure, model)
                used_llm = True
            except Exception:                       # noqa: BLE001
                # 大模型不可用（没网 / 超时 / 连续两次编数字）→ 退回本地，并如实标注。
                # 宁可承认「这次是本地生成的」，也不把没过校验的文本发给用户。
                text = ""

        if not text:
            text = self._template_answer(clean_q, analysis, focus, foods, hits, measure)
            validated = not find_hallucinations(text, analysis)
            attempts = 0

        # 实际用的模型：选了占位模型时这里会是 DeepSeek，配合 notice 一起显示
        spec, notice = resolve_model(model)
        return self._reply(
            answer=text,
            validated=validated,
            attempts=attempts,
            facts=[f.get("standard_name", "") for f in facts],
            foods=[d["name"] for d in foods],
            analysis=analysis,
            mode="llm" if used_llm else "template",
            model=spec.label if used_llm else "本地模板",
            notice=(notice if used_llm else ""),
            masked=masked,
        )

    # -------------------------------------------------------------- 事实构造

    def _focus_items(self, question: str, analysis: dict) -> list[dict]:
        """按指标名/别名定位「问题在问哪一项」。"""
        out: list[dict] = []
        for j in analysis.get("judged") or []:
            name = j.get("standard_name") or ""
            if name and name in question:
                out.append(j)
                continue
            m = self.markers.get(j.get("marker_id") or "") or {}
            for alias in m.get("aliases") or []:
                if len(alias) >= 2 and alias in question:
                    out.append(j)
                    break
        return out

    def _build_facts(self, analysis: dict,
                     focus: list[dict]) -> tuple[list[dict], list[str]]:
        """聚焦项 + 异常项带上数字；正常项只留名字。"""
        judged = analysis.get("judged") or []
        abnormal = [j for j in judged if j.get("flag") in ("high", "low")]

        picked: list[dict] = []
        seen: set[str] = set()
        for j in list(focus) + abnormal:
            name = j.get("standard_name") or ""
            if not name or name in seen:
                continue
            picked.append(j)
            seen.add(name)
        picked = picked[:_MAX_FACTS]

        normals = [j.get("standard_name") for j in judged
                   if j.get("flag") == "normal" and j.get("standard_name")]
        return picked, normals[:_MAX_NORMAL_NAMES]

    @staticmethod
    def _fact_line(j: dict) -> str:
        name = j.get("standard_name") or "未知指标"
        val = _fmt(j.get("reported_value"))
        unit = j.get("unit") or ""
        ref = j.get("ref") or []
        refs = f"{_fmt(ref[0])}-{_fmt(ref[1])}" if len(ref) == 2 else "报告未提供区间"
        return (f"- {name}：{val} {unit}".rstrip()
                + f"，参考区间 {refs}，本地判定：{_flag_word(j.get('flag'))}")

    # -------------------------------------------------------------- 饮食上下文

    @staticmethod
    def _find_foods(question: str) -> list[dict]:
        """问题里提到的菜。菜品库是日常层的资产，这里只读不写。"""
        try:
            from app.daily import service as daily       # 延迟导入，避免启动期耦合
            return daily.find_dishes_in_text(question)
        except Exception:                                # noqa: BLE001
            return []

    @staticmethod
    def _food_hits(foods: list[dict], analysis: dict) -> list[dict]:
        if not foods:
            return []
        try:
            from app.daily import service as daily
            return daily.dish_redline_hits(foods, analysis.get("redlines") or [])
        except Exception:                                # noqa: BLE001
            return []

    def _context_note(self, question: str, analysis: dict) -> str:
        """问题里那些「本地有话说」的前置说明。

        分两种，顺序不能反：
            1. 血压/血糖/体重——不在化验单上，但用户很自然会问。
               先命中它，因为「报告上没有血压」比「报告上没有收缩压」
               更贴近用户的原话。
            2. 问了一个报告里根本没做的指标（演示报告没有尿酸，
               用户却点了「尿酸高是怎么回事」）。
               这时**必须直接说没有**，不能拿别的异常项糊弄过去。
        """
        names = " ".join(j.get("standard_name") or "" for j in analysis.get("judged") or [])
        for word, note in _MEASURE_WORDS.items():
            if word in question and word not in names:
                return note
        return self._missing_marker_note(question, analysis)

    def _missing_marker_note(self, question: str, analysis: dict) -> str:
        judged = analysis.get("judged") or []
        present_ids = {j.get("marker_id") for j in judged}

        # 先把问题里提到的所有指标都找出来，再判断「是不是全都缺」。
        # 只按第一个命中的字典条目判断会误报：问题里说「转氨酶」时，
        # 报告里有谷丙转氨酶、没有谷草转氨酶，不该回答「报告里没有转氨酶」。
        mentioned: list[tuple[str, dict]] = []
        for mid, m in self.markers.items():
            names = [m.get("standard_name", "")] + list(m.get("aliases") or [])
            if any(len(n) >= 2 and n in question for n in names):
                mentioned.append((mid, m))
        if not mentioned or any(mid in present_ids for mid, _ in mentioned):
            return ""

        name = mentioned[0][1].get("standard_name", "")
        note = f"这份报告里没有「{name}」这一项，所以我没法对着你的数据回答它。"
        abnormal = [j.get("standard_name") for j in judged
                    if j.get("flag") in ("high", "low")]
        if abnormal:
            note += "报告里需要注意的有：" + "、".join(abnormal[:4]) + "。"
        return note

    # -------------------------------------------------------------- 大模型路径

    def _build_prompt(self, question: str, analysis: dict, facts: list[dict],
                      normal_names: list[str], foods: list[dict],
                      hits: list[dict], measure: str) -> str:
        lines = ["【用户问题】", question, "", "【事实清单】（数字全部来自本地判定，禁止修改）"]

        if facts:
            lines += [self._fact_line(j) for j in facts]
        else:
            lines.append("- 本次没有需要特别注意的指标")

        total = analysis.get("matched") or len(analysis.get("judged") or [])
        abnormal = analysis.get("abnormal_count")
        if abnormal is None:
            abnormal = len([j for j in analysis.get("judged") or []
                            if j.get("flag") in ("high", "low")])
        lines.append(f"- 共判定 {total} 项，其中 {abnormal} 项需注意")

        if normal_names:
            lines.append(f"- 在参考区间内的项目：{'、'.join(normal_names)}（不多赘述）")

        redlines = analysis.get("redlines") or []
        if redlines:
            lines += ["", "【当前生效的日常红线】"]
            for r in redlines:
                kind = "建议补充" if r.get("action") == "boost" else "日常限制"
                foods_txt = "、".join((r.get("trigger_foods") or [])[:5])
                lines.append(f"- {r.get('restriction')}（{kind}）：{r.get('advice', '')}"
                             + (f" 典型食物：{foods_txt}" if foods_txt else ""))

        if foods:
            lines += ["", "【问题里提到的菜】（标签来自本地菜品库，属粗筛）"]
            for d in foods[:8]:
                tags = d.get("tags") or {}
                high = [f"{_KEY_TEXT.get(k, k)}高" for k, v in tags.items() if v == "high"]
                lines.append(f"- {d['name']}（{d.get('category', '')}）："
                             + ("、".join(high) if high else "各档均不高"))

        if hits:
            lines += ["", "【本地比对结论】（这是本地算出来的，不要改）"]
            for h in hits:
                verb = "能补到" if h.get("action") == "boost" else "会撞线"
                lines.append(f"- 「{h['restriction']}」{verb}：{'、'.join(h['foods'][:5])}")

        if measure:
            lines += ["", "【补充说明】", measure]

        lines += ["", "现在用普通人能听懂的话回答【用户问题】。"]
        return "\n".join(lines)

    def _llm_answer(self, question: str, analysis: dict, facts: list[dict],
                    normal_names: list[str], foods: list[dict],
                    hits: list[dict], measure: str,
                    model: str | None = None) -> tuple[str, bool, int]:
        """调模型 → 校验 → 不过就带着"多写了哪些数字"重写一次。

        返回 (正文, 是否通过校验, 实际尝试次数)。
        两次都不过就抛异常，由调用方退回本地模板——绝不把没过校验的文本发给用户。
        """
        base = self._build_prompt(question, analysis, facts, normal_names,
                                  foods, hits, measure)
        prompt = base
        for attempt in (1, 2):
            text = self.explainer.complete([
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], max_tokens=512, model=model)
            bad = find_hallucinations(text, analysis)
            if not bad:
                return text, True, attempt
            prompt = base + (
                "\n\n【重写要求】你上次的回答里出现了这些【事实清单】中没有的数字："
                f"{'、'.join(bad)}。请重写，一个都不要保留。"
            )
        raise RuntimeError("连续两次输出含无出处数字，已退回本地模板")

    # -------------------------------------------------------------- 本地模板

    def _template_answer(self, question: str, analysis: dict, focus: list[dict],
                         foods: list[dict], hits: list[dict], measure: str) -> str:
        """无 Key / 模型不可用时的本地回答。全部由本地事实拼成，不含新数字。"""
        if focus:
            return "\n".join(self._explain_item(j, analysis) for j in focus[:2])
        if hits or foods:
            return self._explain_foods(analysis, foods, hits)
        if measure:
            return measure
        return self._explain_overview(analysis)

    def _explain_item(self, j: dict, analysis: dict) -> str:
        name = j.get("standard_name") or "这项"
        flag = j.get("flag")
        val = _fmt(j.get("reported_value"))
        unit = j.get("unit") or ""
        ref = j.get("ref") or []
        refs = f"{_fmt(ref[0])}-{_fmt(ref[1])}" if len(ref) == 2 else "报告未提供区间"

        parts = [f"你的{name}是 {val} {unit}".rstrip()
                 + f"，参考区间 {refs}，本地判定为{_flag_word(flag)}。"]

        m = self.markers.get(j.get("marker_id") or "") or {}
        key = "explain_high" if flag == "high" else "explain_low" if flag == "low" else ""
        if key and m.get(key):
            parts.append(str(m[key]))

        for r in analysis.get("redlines") or []:
            if r.get("marker") == name:
                parts.append(f"它对应的日常红线是「{r.get('restriction')}」："
                             f"{r.get('advice', '')}")
                break

        if flag == "normal":
            parts.append("这项在参考区间内，不用特别处理。")
        elif flag == "unknown":
            parts.append("这项没能自动判定，建议对着原报告核对一下数值和单位。")

        parts.append("以上数值都来自这份报告，具体怎么处理请以医生意见为准。")
        return "".join(parts)

    def _explain_foods(self, analysis: dict, foods: list[dict], hits: list[dict]) -> str:
        if not foods:
            return self._explain_overview(analysis)

        names = "、".join(d["name"] for d in foods[:4])
        if not hits:
            redlines = analysis.get("redlines") or []
            if not redlines:
                head = "你目前没有生效的红线。"
            else:
                watching = "、".join(f"「{r.get('restriction')}」" for r in redlines[:3])
                head = (f"你当前盯的是{watching}，"
                        + ("它没有被撞到。" if len(redlines) == 1 else "这几条都没被撞到。"))
            return (f"从本地菜品库看到的「{names}」，{head}"
                    "标签是粗筛，具体吃多少还得自己看着来。")

        parts: list[str] = []
        for h in hits[:2]:
            key = _KEY_TEXT.get(h.get("nutrient_key") or "", "")
            listed = "、".join(h["foods"][:4])
            if h.get("action") == "boost":
                parts.append(f"你的红线里有一条是「{h['restriction']}」，是要补充的方向，"
                             f"{listed} 这类正好能补上。")
            else:
                parts.append(f"你问的这类菜里，{listed} 的{key}档是「高」，"
                             f"会撞上你的「{h['restriction']}」红线。")
        parts.append("不是一口都不能吃，换个做法就行——清汤、少喝汤底、多点青菜；"
                     "这顿吃完在「今日记录」里记一下，就能看到今天碰了几次线。")
        return "".join(parts)

    def _explain_overview(self, analysis: dict) -> str:
        judged = analysis.get("judged") or []
        abnormal = [j for j in judged if j.get("flag") in ("high", "low")]
        total = analysis.get("matched") or len(judged)
        if not judged:
            return "还没有解读结果，先上传一份报告我才能回答。"
        if not abnormal:
            return f"这次共判定 {total} 项，都在参考区间内。"
        names = "、".join(j.get("standard_name", "该项") for j in abnormal[:3])
        tail = "等" if len(abnormal) > 3 else ""
        return (f"这次共判定 {total} 项，有 {len(abnormal)} 项需要注意：{names}{tail}。"
                "想细问哪一项，直接打指标名就行。")

    # ------------------------------------------------------------------ 组装

    def _reply(self, answer: str, validated: bool, attempts: int,
               facts: list[str], foods: list[str], analysis: dict,
               mode: str, model: str, notice: str = "",
               masked: Optional[list[str]] = None) -> dict:
        allowed = allowed_numbers(analysis)
        used = [t for t in dict.fromkeys(_NUM_RE.findall(answer or "")) if t in allowed]
        return {
            "answer": answer,
            "mode": mode,
            "model": model,
            "model_notice": notice,
            "validated": bool(validated),
            "attempts": int(attempts),
            "facts_used": facts,
            "numbers_used": used,
            "foods": foods,
            "disclaimer": DISCLAIMER,
            # 脱敏审计：前端不展示，但接口里保留，便于排查「为什么问题变短了」
            "masked_fields": masked or [],
        }
