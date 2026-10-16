"""
OCR 选型评测脚本（Day 3-5）

================================================================================
为什么必须先做这件事
================================================================================
OCR 是本项目唯一的「不可绕过的技术风险」。如果报告上的数字抽不准，
后面所有功能——红线、日常对照、周报——全都是错的。

所以这里不猜哪家好用，**用数据选**。

================================================================================
评测分两层（关键设计）
================================================================================
L1 文本层：OCR 吐出的原始文本，与人工标注的标准文本比对字符准确率。
L2 字段层：用**同一个解析器**从 OCR 文本里抽四元组，与标注比对召回率。

为什么 L2 必须对三家使用同一个解析器：
    如果每家配一个专属解析器，比的就是「我给谁写的解析器更好」，
    而不是「谁的 OCR 更强」。共用解析器才能真正反映 OCR 质量
    对下游（指标字典归一化）的影响。

================================================================================
用法
================================================================================
    # 0. 先看脚本自身是否正常（无需样本、无需 API key）
    python bench.py --selftest

    # 1. 把真实报告放 samples/，把人工标注放 expected/，然后：
    python bench.py                      # 跑本地 PaddleOCR
    python bench.py --provider baidu     # 跑百度（需要 .env 里的 key）
    python bench.py --provider all       # 全部跑，出对比表

================================================================================
隐私提醒
================================================================================
samples/ 已被 .gitignore 排除，真实报告**不会**进 Public 仓库。
标注 expected/ 里也请只写指标数值，不要抄姓名、体检号、医院名。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
EXPECTED = HERE / "expected"
RESULTS = HERE / "results"

#: 我们真正关心的四个字段
FIELDS = ["name", "value", "unit", "ref"]


# ==============================================================================
# 统一解析器（所有 provider 共用）
# ==============================================================================

#: 体检报告典型行：尿酸(UA)  512  μmol/L  208-428
#:
#: ── 这一版正则踩过两个坑，都靠自检抓出来 ──
#:
#: 坑 1：name 的字符类里不能含空白，否则会跨过间隔把后面的数值一起吞掉。
#:        （血红蛋白那行 name 吃成了「血红蛋白(Hb)  135  g/L」，value 取成 130）
#:
#: 坑 2：不能用一条正则同时切出 unit 与 ref。
#:        因为 unit 里有数字（×10⁹/L），一旦允许 unit 含数字，
#:        遇到没有单位的行（如「尿酸 512 208-428」）它就会把参考区间也吞掉；
#:        而若不允许含数字，含上标的单位又切不出来。
#:        解法：正则只负责切出 name/value/箭头/剩余部分，
#:        再用 REF_TAIL_RE 从剩余部分的**尾部**反推参考区间。
LINE_RE = re.compile(
    r"^\s*(?P<name>[^\s\d][\u4e00-\u9fa5A-Za-z0-9（）()·/.\-]*)"
    r"\s+"
    r"(?P<value>[<>]?\s*[-+]?\d+(?:[.]\d+)?)"
    r"\s*(?P<arrow>[↑↓*★※]*)\s*"
    r"(?P<rest>.*)$"
)

#: 从行尾反推「参考区间」，剩下的部分算作单位。
#: 必须让 unit 非贪婪，否则它会把区间一起吃掉。
REF_TAIL_RE = re.compile(
    r"^(?P<unit>.*?)\s*"
    r"(?P<ref>\d+(?:[.]\d+)?\s*[-~至]\s*\d+(?:[.]\d+)?)\s*$"
)


def parse_markers(text: str) -> list[dict]:
    """从一段 OCR 文本里抽四元组。

    这是一个**故意做得朴素**的解析器：不依赖任何指标体系，纯靠版式规律。
    目的不是追求最高召回，而是给三家 provider 一把**同一把尺子**。
    Day 6-9 指标字典建成后，它会被字典驱动的版本取代。
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
        if not name:
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
        out.append(
            {
                "name": name,
                "value": (m.group("value") or "").replace(" ", ""),
                "unit": unit,
                "ref": ref,
            }
        )
    return out


# ==============================================================================
# 评分
# ==============================================================================


def _norm(s: str) -> str:
    """归一化：去掉空白与全角符号差异，便于比对。"""
    return re.sub(r"\s+", "", (s or "")).replace("（", "(").replace("）", ")").lower()


def char_accuracy(ocr_text: str, std_text: str) -> float:
    """L1：字符级准确率（用最长公共子序列近似，避免依赖第三方库）。"""
    a, b = _norm(ocr_text), _norm(std_text)
    if not b:
        return 1.0 if not a else 0.0
    # 简易 LCS 长度
    prev = [0] * (len(b) + 1)
    for ca in a:
        cur = [0]
        for j, cb in enumerate(b, 1):
            cur.append(prev[j - 1] + 1 if ca == cb else max(prev[j], cur[j - 1]))
        prev = cur
    return prev[len(b)] / len(b)


def field_recall(parsed: list[dict], expect: list[dict]) -> dict[str, float]:
    """L2：四项字段的召回率。

    匹配逻辑：以标注里的指标名为锚，在 OCR 结果里找最相近的那条，
    再逐字段比对。这样即便 OCR 把「尿酸」写成「尿酸 」，也能对上。
    """
    hit = {f: 0 for f in FIELDS}
    total = {f: 0 for f in FIELDS}

    for exp in expect:
        ename = _norm(exp.get("name", ""))
        if not ename:
            continue
        best = None
        best_score = 0.0
        for p in parsed:
            pname = _norm(p.get("name", ""))
            if not pname:
                continue
            common = len(set(ename) & set(pname)) / max(len(set(ename) | set(pname)), 1)
            if ename in pname or pname in ename:
                common = max(common, 0.9)
            if common > best_score:
                best, best_score = p, common
        if best is None or best_score < 0.5:
            # 指标名都没认出来，四个字段全部记未召回
            for f in FIELDS:
                total[f] += 1
            continue
        for f in FIELDS:
            total[f] += 1
            want = _norm(str(exp.get(f, "")))
            got = _norm(str(best.get(f, "")))
            if want and got and (want == got or want in got or got in want):
                hit[f] += 1
            elif not want and not got:
                hit[f] += 1  # 两边都为空也算对齐
    return {f: (hit[f] / total[f] if total[f] else 0.0) for f in FIELDS}


# ==============================================================================
# Provider 适配层
# ==============================================================================


def normalize_ocr_text(text: str) -> str:
    """OCR 输出的通用后处理：修掉字符之间被插入的多余空格。

    ============================================================================
    为什么要这一步（实测依据）
    ============================================================================
    本地引擎实测输出里大量出现这类情况：

        5.41        →  "5. 41"
        mmol/L      →  "mmol / L"
        1.04-1.55   →  "1. 04-1. 55"

    这不是识别错误，而是 OCR 把紧密排版的字符在间隔处切开了。
    若不修正，解析器会把 "5. 41" 的 value 截成 "5"，
    unit 变成 ". 41 mmol /L"——于是**格式问题被误记成识别失败**。

    这三条规则是**所有 provider 通病**，对所有引擎一视同仁地应用，
    因此不会破坏横向对比的公平性。

    注意：这里只修「空格」，**不修真正的识别错误**
    （如上标 ⁹ 被认成 °、字母 l 被认成 1）——那些要留给
    Day 6-9 的指标字典去纠正，也正是评测要暴露出来的东西。
    """
    text = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text)      # 小数点
    text = re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", text)       # 区间连字符
    text = re.sub(r"\s*/\s*", "/", text)                     # 单位斜杠 mmol / L
    return text


def _boxes_to_lines(items: list[tuple[float, float, str]], tol: float = 16.0) -> str:
    """把 OCR 返回的零散文本框合并成「行」。

    ============================================================================
    为什么要这一步
    ============================================================================
    OCR 输出的是一个个带坐标的文本框，而本项目的解析器是按**行**工作的。
    不做行合并，解析器拿到的就是一堆碎片，评测结果会严重失真——
    而且这个失真对所有 provider 一视同仁，最终只会误导选型。

    ============================================================================
    已知限制：双栏排版（twocol）不在此函数的处理能力内
    ============================================================================
    曾尝试按 x 间隙自动「分栏」，结果**全线崩到 0%**：
    单栏表格里「项目 / 结果 / 单位 / 参考范围」的列间距约 260px，
    而真实双栏的栏间距约 400px——两者只差一百多像素，
    任何固定阈值都会误伤。实测 threshold=140 时，
    「项目」「结果」「单位」被拆成了三行，全部样本归零。

    根本原因：单栏表格里，**跨列才构成一条记录**；
    而双栏里，**每栏内部就是一串完整记录**。两者结构不同，
    不是调阈值能解决的，需要真正的版面分析。

    取舍：真实体检报告绝大多数是单栏（或分节单栏），双栏少见。
    与其引入一个脆弱的启发式，不如**诚实记录这个限制**。
    若后续真实样本里遇到双栏，再单独处理——那时会有真实数据支撑判断。
    """
    items.sort(key=lambda t: (t[0], t[1]))
    lines: list[str] = []
    cur: list[tuple[float, str]] = []
    cur_y: float | None = None
    for y, x, txt in items:
        if cur_y is None or abs(y - cur_y) < tol:
            cur.append((x, txt))
            cur_y = y if cur_y is None else cur_y
        else:
            cur.sort()
            lines.append("  ".join(t for _, t in cur))
            cur, cur_y = [(x, txt)], y
    if cur:
        cur.sort()
        lines.append("  ".join(t for _, t in cur))
    return "\n".join(lines)


def ocr_rapidocr(path: Path) -> str:
    """本地 RapidOCR（PaddleOCR 的 PP-OCR 模型，ONNX 轻量部署）。

    选它做本地基线的理由：
      1. 用的就是 PaddleOCR 同系列模型，识别质量接近
      2. 无需 paddlepaddle 那套几百 MB 的重依赖，纯 CPU 即可
      3. 体积小，最终产品若要内置本地 OCR，这一套更现实
    """
    try:
        from rapidocr_onnxruntime import RapidOCR  # type: ignore
    except ImportError:
        raise RuntimeError(
            "未安装 rapidocr-onnxruntime。执行: "
            "pip install rapidocr-onnxruntime -i https://pypi.tuna.tsinghua.edu.cn/simple"
        )
    engine = RapidOCR()
    result, _ = engine(str(path))
    if not result:
        return ""
    items = []
    for box, text, _score in result:
        ys = [p[1] for p in box]
        xs = [p[0] for p in box]
        items.append((sum(ys) / len(ys), sum(xs) / len(xs), str(text)))
    return _boxes_to_lines(items)


def ocr_paddle(path: Path) -> str:
    """本地 PaddleOCR。零门槛不用申请 key，但对环境要求高。"""
    try:
        from paddleocr import PaddleOCR  # type: ignore
    except ImportError:
        raise RuntimeError("未安装 paddleocr。执行: pip install paddleocr paddlepaddle")
    engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
    res = engine.ocr(str(path), cls=True)
    lines = []
    for block in res or []:
        for item in block:
            txt = item[1][0] if isinstance(item[1], (list, tuple)) else str(item[1])
            lines.append(txt)
    return "\n".join(lines)


def ocr_baidu(path: Path) -> str:
    """百度 OCR。需要 .env 里的 BAIDU_OCR_* 三个 key。"""
    import base64
    import os

    import requests  # type: ignore

    app_id = os.getenv("BAIDU_OCR_APP_ID")
    api_key = os.getenv("BAIDU_OCR_API_KEY")
    secret = os.getenv("BAIDU_OCR_SECRET_KEY")
    if not all([app_id, api_key, secret]):
        raise RuntimeError("缺少百度 OCR 环境变量，见 .env.example")
    token = requests.post(
        "https://aip.baidubce.com/oauth/2.0/token",
        params={
            "grant_type": "client_credentials",
            "client_id": api_key,
            "client_secret": secret,
        },
        timeout=20,
    ).json()["access_token"]
    img = base64.b64encode(path.read_bytes()).decode()
    data = requests.post(
        "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
        params={"access_token": token},
        data={"image": img},
        timeout=30,
    ).json()
    return "\n".join(w.get("words", "") for w in data.get("words_result", []))


def ocr_tencent(path: Path) -> str:
    """腾讯 OCR。需要 SecretId / SecretKey，走腾讯云 SDK。"""
    raise RuntimeError(
        "腾讯 OCR 适配器待接入。先用 --provider paddle 或 baidu 得到基线后再补。"
    )


PROVIDERS = {
    "rapidocr": ocr_rapidocr,
    "paddle": ocr_paddle,
    "baidu": ocr_baidu,
    "tencent": ocr_tencent,
}

#: --provider all 时默认只跑已就绪的本地引擎，
#: 云端引擎需要 key，未配置时会明确报错而不是静默跳过
DEFAULT_ALL = ["rapidocr"]


# ==============================================================================
# 自检（无需样本、无需 key，用来确认脚本本身没写错）
# ==============================================================================

#: 自检用例。必须覆盖已知的两类「难行」：
#:   含上标的单位（×10⁹/L）、带箭头的异常值（0.90↓）
#: 否则解析器悄悄漏行都不会被发现——这类缺陷会把「解析器的错」
#: 误判成「OCR 很差」，进而选错引擎。
_SELF_TEXT = """项目               结果        单位        参考范围
尿酸(UA)            512         μmol/L      208-428
血红蛋白(Hb)        135         g/L         130-175
甘油三酯(TG)        2.8         mmol/L      0.45-1.70
白细胞计数(WBC)     5.37        ×10⁹/L      3.50-9.50
高密度脂蛋白(HDL-C) 0.90↓       mmol/L      1.04-1.55
总胆红素(TBIL)      2.5↓        μmol/L      3.4-20.5
"""

_SELF_EXPECT = [
    {"name": "尿酸", "value": "512", "unit": "μmol/L", "ref": "208-428"},
    {"name": "血红蛋白", "value": "135", "unit": "g/L", "ref": "130-175"},
    {"name": "甘油三酯", "value": "2.8", "unit": "mmol/L", "ref": "0.45-1.70"},
    {"name": "白细胞计数", "value": "5.37", "unit": "×10⁹/L", "ref": "3.50-9.50"},
    {"name": "高密度脂蛋白", "value": "0.90", "unit": "mmol/L", "ref": "1.04-1.55"},
    {"name": "总胆红素", "value": "2.5", "unit": "μmol/L", "ref": "3.4-20.5"},
]


def selftest() -> int:
    print("=== 自检：解析器与评分逻辑（不需要真实样本）===")
    parsed = parse_markers(_SELF_TEXT)
    print(f"解析出 {len(parsed)} 条：")
    for p in parsed:
        print(f"   name={p['name']!r} value={p['value']!r} unit={p['unit']!r} ref={p['ref']!r}")
    recall = field_recall(parsed, _SELF_EXPECT)
    print("\n召回率（对照人工标注）：")
    for f in FIELDS:
        print(f"   {f:6s} {recall[f]:.0%}")
    ok = len(parsed) >= 3 and all(v >= 0.99 for v in recall.values())
    print("\n结果:", "解析器工作正常 ✅" if ok else "解析器需要调整 ⚠️")
    return 0 if ok else 1


# ==============================================================================
# 主流程
# ==============================================================================


def load_expect(sample: Path) -> list[dict]:
    f = EXPECTED / f"{sample.stem}.json"
    if not f.exists():
        return []
    return json.loads(f.read_text(encoding="utf-8")).get("markers", [])


def run(provider: str) -> dict:
    fn = PROVIDERS[provider]
    rows = []
    for sample in sorted(SAMPLES.glob("*")):
        if sample.name.startswith(".") or sample.suffix.lower() not in {
            ".jpg", ".jpeg", ".png", ".pdf", ".webp"
        }:
            continue
        expect = load_expect(sample)
        try:
            text = normalize_ocr_text(fn(sample))
        except Exception as e:  # 单张失败不中断整体评测
            print(f"  [跳过] {sample.name}: {e}", file=sys.stderr)
            continue
        parsed = parse_markers(text)
        recall = field_recall(parsed, expect) if expect else {f: 0.0 for f in FIELDS}
        rows.append({"sample": sample.name, **{f"r_{f}": recall[f] for f in FIELDS}})
    return {"provider": provider, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description="OCR 选型评测")
    ap.add_argument("--provider", default="paddle", choices=list(PROVIDERS) + ["all"])
    ap.add_argument("--selftest", action="store_true", help="只跑解析器自检")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not any(
        p.suffix.lower() in {".jpg", ".jpeg", ".png", ".pdf", ".webp"}
        for p in SAMPLES.glob("*")
    ):
        print("samples/ 里还没有报告样本。")
        print("请先把脱敏后的体检报告放进去，并在 expected/ 里写好标注。")
        print("（详见 tests/ocr_bench/README.md）")
        print("\n现在可以先用 --selftest 确认脚本本身正常。")
        return 2

    targets = list(PROVIDERS) if args.provider == "all" else [args.provider]
    RESULTS.mkdir(exist_ok=True)
    summary = {}
    for pv in targets:
        print(f"\n=== {pv} ===")
        res = run(pv)
        if not res["rows"]:
            print("  没有可评测的结果")
            continue
        avg = {
            f: sum(r[f"r_{f}"] for r in res["rows"]) / len(res["rows"]) for f in FIELDS
        }
        summary[pv] = avg
        for f in FIELDS:
            print(f"   {f:6s} 平均召回 {avg[f]:.0%}")

    if summary:
        print("\n=== 对比（数值越高越好）===")
        head = "provider  " + "  ".join(f"{f:>8s}" for f in FIELDS)
        print(head)
        for pv, avg in summary.items():
            print(f"{pv:10s}" + "  ".join(f"{avg[f]:>8.0%}" for f in FIELDS))
        (RESULTS / "latest.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"\n结果已写入 {RESULTS / 'latest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
