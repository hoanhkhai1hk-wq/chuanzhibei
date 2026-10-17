"""
本地规则引擎：指标归一 → 高低判定 → 红线匹配

================================================================================
架构铁律的落地点
================================================================================
本文件里的每一步都在本地完成，**不调用任何大模型**：

    指标名归一    → 查字典（别名表）
    高低判定      → 与字典里的参考区间做数值比较
    红线匹配      → 查红线规则表

大模型在这个链路里的位置是**下游**：拿到这里产出的确定性结果之后，
才负责把「尿酸 512，参考 208-428，偏高」改写成一句人话。
它不参与任何数值的产生或判断。

================================================================================
为什么要单独成一个模块
================================================================================
把判定逻辑集中在这里，好处有三个：
  1. 判定规则可解释、可复现——每一条结论都能追到具体的区间数字
  2. 便于单独测试——不需要 OCR、不需要网络、不需要 API key
  3. 演示时可直接展示「本地判定 vs 大模型写话」的分界，这是方法创新性的实证
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from app.schemas import Flag

#: 本文件位于 app/rules/，因此：
#:   HERE      = app/rules/
#:   APP       = app/
#:   字典在 app/dict/，规则在 app/rules/ —— 两者不同目录，别写错
HERE = Path(__file__).parent
APP = HERE.parent
MARKERS_F = APP / "dict" / "markers.json"
REDLINES_F = HERE / "redlines.json"

#: 定性指标的正常表述（用于尿常规的阴性/阳性判定）
NEGATIVE_WORDS = {"阴性", "-", "–", "—", "neg", "negative", "未见", "无"}
POSITIVE_WORDS = {"阳性", "+", "++", "+++", "++++", "pos", "positive", "微量", "trace"}

#: 定性结果里带括号的符号，如「阴性（-）」「阳性（+）」「弱阳性（±）」。
#: 报告上最可靠的定性标记就是它——比文字更不容易被 OCR 认错。
_QUAL_SIGN_RE = re.compile(r"[（(]\s*(?P<sign>[+\-±–—]+)\s*[)）]")

_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _to_float(v) -> float | None:
    """从 OCR 可能带噪的字符串里取出数值。

    只做「抽取」，不做「修正」——OCR 若把它认错了，这里如实返回，
    由上层决定是否交给字典兜底或提示人工校正。
    """
    if isinstance(v, (int, float)):
        return float(v)
    if v is None:
        return None
    m = _NUM_RE.search(str(v).replace(",", ""))
    return float(m.group()) if m else None


class RuleEngine:
    """指标字典 + 红线规则的本地判定引擎。"""

    def __init__(self) -> None:
        self.markers_doc = json.loads(MARKERS_F.read_text(encoding="utf-8"))
        self.rules_doc = json.loads(REDLINES_F.read_text(encoding="utf-8"))
        self.markers = {m["id"]: m for m in self.markers_doc["markers"]}
        self.rules = self.rules_doc["rules"]

        #: 别名索引：归一化后的别名 → 指标 id
        #: 冲突时**不覆盖**先登记者，而是记录下来交给 validate.py 报错。
        #: （曾经踩过的坑：尿常规的「WBC(尿)」归一化后等于「WBC」，
        #:   若允许覆盖，血常规的白细胞计数就会被误判成尿白细胞——
        #:   方向完全错了，而且不会有任何异常提示。）
        self._alias: dict[str, str] = {}
        self.alias_conflicts: list[tuple[str, str, str]] = []
        for mid, m in self.markers.items():
            for a in m.get("aliases", []):
                k = self._norm(a)
                if k in self._alias and self._alias[k] != mid:
                    self.alias_conflicts.append((a, self._alias[k], mid))
                    continue
                self._alias[k] = mid
            sk = self._norm(m["standard_name"])
            if sk in self._alias and self._alias[sk] != mid:
                self.alias_conflicts.append((m["standard_name"], self._alias[sk], mid))
            else:
                self._alias.setdefault(sk, mid)

    # ---------------------------------------------------------------- 归一化

    @staticmethod
    def _norm(s: str) -> str:
        """归一化：去空白、去括号内容、转小写。"""
        s = re.sub(r"\s+", "", str(s or ""))
        s = re.sub(r"[（(].*?[)）]", "", s)      # 去掉 (UA) 这类后缀
        return s.lower()

    def normalize_name(self, raw: str) -> str | None:
        """把报告上的原始指标名归一到标准 id，找不到返回 None。

        找不到是正常情况——真实报告里有大量字典尚未收录的指标，
        此时上层应走「未识别」分支，而不是乱猜。
        """
        key = self._norm(raw)
        if key in self._alias:
            return self._alias[key]
        # 退一步：用包含关系匹配（应对「白细胞计数WBC」这类连写）
        for alias, mid in self._alias.items():
            if len(alias) >= 2 and (alias in key or key in alias):
                return mid
        return None

    # ------------------------------------------------------------------ 判定

    def ref_range(self, marker_id: str, sex: str = "unknown") -> tuple[float, float] | None:
        """取参考区间。性别未知时用 default。"""
        m = self.markers.get(marker_id)
        if not m:
            return None
        ref = m.get("ref", {})
        lo_hi = ref.get(sex) if sex in ("male", "female") else None
        if not lo_hi:
            lo_hi = ref.get("default")
        return tuple(lo_hi) if lo_hi else None

    def judge(self, marker_id: str, value, sex: str = "unknown") -> tuple[Flag, dict]:
        """本地判定高低。返回 (flag, 判定依据)。

        判定依据里带上实际用于比较的区间，便于向用户解释「凭什么这么判断」。
        """
        m = self.markers.get(marker_id)
        if not m:
            return "unknown", {"reason": "字典中无此指标"}

        raw = str(value or "").strip()

        # 定性指标（尿常规、便检、乙肝定性等）：按阴性/阳性判定
        if m.get("unit") == "定性" or not m.get("ref", {}).get("default"):
            # 括号里的符号优先：报告上最可靠的定性标记就是它。
            # 修复前，「阴性（-）」——中国检验报告最常见的写法——会被判成 unknown，
            # 而「阳性（+）」却能被正确识别，两边不对称。
            bracket = _QUAL_SIGN_RE.search(raw)
            if bracket:
                sign = bracket.group("sign")
                # ± 视为弱阳性/临界，按「需关注」处理并如实说明
                if "+" in sign or "±" in sign:
                    return "high", {
                        "reason": f"定性结果阳性（标记 {sign}，正常应为{m.get('ref_text', '阴性')}）",
                        "ref_text": m.get("ref_text")}
                return "normal", {
                    "reason": f"定性结果阴性（标记 {sign}，{m.get('ref_text', '阴性')}）",
                    "ref_text": m.get("ref_text")}

            # 半定量写法（尿蛋白/尿糖常见的 "2+"、"+++"）：带 + 号即阳性，
            # 程度差异交给医生解读，产品不替用户分级
            if "+" in raw or "±" in raw:
                return "high", {"reason": f"定性结果阳性（{raw}，正常应为{m.get('ref_text', '阴性')}）",
                                "ref_text": m.get("ref_text")}

            if raw in POSITIVE_WORDS or any(w in raw for w in POSITIVE_WORDS if len(w) > 1):
                return "high", {"reason": f"定性结果阳性（正常应为{m.get('ref_text', '阴性')}）",
                                "ref_text": m.get("ref_text")}
            if raw in NEGATIVE_WORDS or raw == "":
                return "normal", {"reason": f"定性结果阴性（{m.get('ref_text', '阴性')}）",
                                  "ref_text": m.get("ref_text")}
            return "unknown", {"reason": f"无法识别的定性值：{raw}"}

        # 定量指标
        rng = self.ref_range(marker_id, sex)
        val = _to_float(value)
        if rng is None or val is None:
            return "unknown", {"reason": "缺少参考区间或数值无法解析"}

        lo, hi = rng
        if val > hi:
            flag: Flag = "high"
        elif val < lo:
            flag = "low"
        else:
            flag = "normal"
        return flag, {"value": val, "ref": [lo, hi], "unit": m.get("unit"),
                      "reason": f"{val} 与参考区间 {lo}-{hi} 比较"}

    # -------------------------------------------------------------- 红线匹配

    def match_redlines(self, judged: list[dict]) -> list[dict]:
        """根据判定结果匹配红线规则。

        judged 元素需含 marker_id 与 flag。
        """
        hits: list[dict] = []
        seen: set[str] = set()
        for item in judged:
            mid, flag = item.get("marker_id"), item.get("flag")
            if not mid or flag not in ("high", "low"):
                continue
            for r in self.rules:
                if not r.get("enabled", True):
                    continue
                if r.get("marker_id") == mid and r.get("flag") == flag:
                    if r["rule_id"] in seen:
                        continue
                    seen.add(r["rule_id"])
                    foods = r.get("trigger_foods", [])
                    hits.append({
                        "rule_id": r["rule_id"],
                        "marker": r["marker"],
                        "restriction": r["restriction"],
                        "nutrient_key": r["nutrient_key"],
                        "action": r.get("action", "limit"),
                        "trigger_foods": foods,
                        "advice": r["advice_template"].replace(
                            "{trigger_foods}", "、".join(foods[:3]) if foods else "相关食物"),
                    })
        return hits

    # ------------------------------------------------------------------ 主入口

    def evaluate(self, extracted: list[dict], sex: str = "unknown") -> dict:
        """端到端本地评估：抽取结果 → 归一 → 判定 → 红线。

        extracted 元素形如 {"name": "尿酸(UA)", "value": "512", "unit": "μmol/L"}
        """
        judged, unmatched = [], []
        for ex in extracted:
            mid = self.normalize_name(ex.get("name", ""))
            if mid is None:
                unmatched.append(ex.get("name", ""))
                continue
            flag, why = self.judge(mid, ex.get("value"), sex)
            judged.append({
                "marker_id": mid,
                "standard_name": self.markers[mid]["standard_name"],
                "reported_value": ex.get("value"),
                **why,
                "flag": flag,
            })

        # 把命中的红线信息回填到对应指标上。
        # 为什么必须回填：表达层生成「给爸妈说的话」时需要知道这条异常
        # 到底是「要少吃」还是「要多吃」。补铁是唯一一条 boost 规则，
        # 若这里不带出来，前端只能一刀切写成「限制」，方向就完全反了。
        rule_by_marker: dict[str, dict] = {}
        for r in self.rules:
            if r.get("enabled", True):
                rule_by_marker[(r.get("marker_id"), r.get("flag"))] = r

        redlines = self.match_redlines(judged)
        for item in judged:
            rule = rule_by_marker.get((item.get("marker_id"), item.get("flag")))
            if rule:
                item["action"] = rule.get("action", "limit")
                item["restriction"] = rule.get("restriction")
                item["trigger_foods"] = rule.get("trigger_foods", [])
                item["rule_id"] = rule.get("rule_id")

        abnormal = [j for j in judged if j["flag"] in ("high", "low")]
        return {
            "total": len(extracted),
            "matched": len(judged),
            "unmatched": unmatched,
            "abnormal_count": len(abnormal),
            "judged": judged,
            "redlines": redlines,
        }


def make_engine() -> RuleEngine:
    return RuleEngine()
