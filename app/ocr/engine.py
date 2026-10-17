"""
OCR 适配层

================================================================================
职责
================================================================================
把「一张体检报告图片」变成「一段接近报告行结构的文本」。

本层之后交由解析器（tests/ocr_bench/bench.py 的 parse_markers）
切成四元组，再交给规则引擎（app/rules/engine.py）做本地判定。

================================================================================
为什么这些函数放在正式代码里，而不是测试脚本里
================================================================================
它们最初写在评测脚本 bench.py 里，但产品链路同样需要它们。
若让正式代码反过来 import 测试脚本，依赖方向就反了，
而且评测脚本里的临时调整会直接影响线上行为。
所以把「引擎无关」的公共部分抽到这里，评测与产品共用同一份实现。

================================================================================
引擎现状
================================================================================
- rapidocr：默认本地引擎。PaddleOCR 同系列 PP-OCR 模型，ONNX 部署，
  安装约 17MB、纯 CPU，无需申请任何 key。
- paddle / baidu / tencent：可选对比引擎，见 tests/ocr_bench/README.md。

基线数据见 tests/ocr_bench/README.md「当前基线结论」一节。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from PIL import Image, ImageFilter


def normalize_ocr_text(text: str) -> str:
    """OCR 输出的通用后处理：修掉字符之间被插入的多余空格。

    ============================================================================
    实测依据
    ============================================================================
    本地引擎实测输出里大量出现这类情况：

        5.41      →  "5. 41"
        mmol/L    →  "mmol / L"
        1.04-1.55 →  "1. 04-1. 55"

    这不是识别错误，而是 OCR 把紧密排版的字符在间隔处切开了。
    若不修正，解析器会把 "5. 41" 的 value 截成 "5"，
    unit 变成 ". 41 mmol /L"——**格式问题被误记成识别失败**。
    实测中这一步让 ref 召回从 56% 回到 81%。

    这三条规则是所有 provider 的通病，对所有引擎一视同仁地应用，
    不破坏横向对比的公平性。

    注意：这里只修「空格」，**不修真正的识别错误**
    （如上标 ⁹ 被认成 °、字母 l 被认成 1）。那些留给指标字典的
    unit_ocr_variants 去纠正——也正是评测要暴露出来的东西。
    """
    text = re.sub(r"(\d)\s*\.\s*(\d)", r"\1.\2", text)      # 小数点
    text = re.sub(r"(\d)\s*-\s*(\d)", r"\1-\2", text)       # 区间连字符
    text = re.sub(r"\s*/\s*", "/", text)                     # 单位斜杠 mmol / L
    return text


def boxes_to_lines(items: list[tuple[float, float, str]], tol: float = 16.0) -> str:
    """把 OCR 返回的零散文本框合并成「行」。

    ============================================================================
    为什么需要
    ============================================================================
    OCR 输出的是一个个带坐标的文本框，而解析器是按**行**工作的。
    不做行合并，解析器拿到的就是一堆碎片，结果会严重失真。

    ============================================================================
    已知限制：双栏（twocol）不在处理能力内
    ============================================================================
    曾尝试按 x 间隙自动「分栏」，结果**全部样本归零**：
    单栏表格里「项目/结果/单位/参考范围」的列间距约 260px，
    真实双栏的栏间距约 400px，差距太小，任何固定阈值都会误伤
    （threshold=140 时「项目」「结果」「单位」被拆成了三行）。

    根因是结构不同：单栏是「跨列才构成一条记录」，
    双栏是「栏内就是一串完整记录」，不是调参能解决的。

    取舍：真实体检报告绝大多数是单栏，故诚实记录此限制，
    待有真实双栏样本后再单独处理。
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
    """本地 RapidOCR（默认引擎）。

    选它的理由：
      1. 用 PaddleOCR 同系列 PP-OCR 模型，识别质量接近
      2. 无需 paddlepaddle 那套几百 MB 重依赖，纯 CPU 可跑
      3. 安装约 17MB，最终产品若要内置本地 OCR，这一套更现实
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
    return normalize_ocr_text(boxes_to_lines(items))


def ocr_paddle(path: Path) -> str:
    """完整 PaddleOCR。识别质量略高，但依赖重（几百 MB）。"""
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
    return normalize_ocr_text("\n".join(lines))


def ocr_baidu(path: Path) -> str:
    """百度 OCR（高精度版）。需要 .env 里的 BAIDU_OCR_* 三个值。"""
    import base64

    import requests  # type: ignore

    app_id = os.getenv("BAIDU_OCR_APP_ID")
    api_key = os.getenv("BAIDU_OCR_API_KEY")
    secret = os.getenv("BAIDU_OCR_SECRET_KEY")
    if not all([app_id, api_key, secret]):
        raise RuntimeError("缺少百度 OCR 环境变量，见 .env.example")
    token = requests.post(
        "https://aip.baidubce.com/oauth/2.0/token",
        params={"grant_type": "client_credentials",
                "client_id": api_key, "client_secret": secret},
        timeout=20,
    ).json()["access_token"]
    img = base64.b64encode(path.read_bytes()).decode()
    data = requests.post(
        "https://aip.baidubce.com/rest/2.0/ocr/v1/accurate_basic",
        params={"access_token": token},
        data={"image": img},
        timeout=30,
    ).json()
    return normalize_ocr_text(
        "\n".join(w.get("words", "") for w in data.get("words_result", []))
    )


def ocr_tencent(path: Path) -> str:
    """腾讯 OCR。待接入。"""
    raise RuntimeError(
        "腾讯 OCR 适配器待接入。先用 rapidocr 或 baidu 得到基线后再补。"
    )


PROVIDERS = {
    "rapidocr": ocr_rapidocr,
    "paddle": ocr_paddle,
    "baidu": ocr_baidu,
    "tencent": ocr_tencent,
}

DEFAULT_PROVIDER = "rapidocr"


#: OCR 产出低于这个字符数就认为"这张图没读出来"，值得预处理后重试。
#: 一份正常体检报告至少有十几行指标，不可能只有几十个字符。
_MIN_TEXT_LEN = 200

#: 预处理放大倍数。实测对比过 1.5x 与 2.0x：
#: 1.5x 在三类弱图上都是最优（拉得太大会让笔画发虚，反而更难认）。
_PREP_SCALE = 1.5


def preprocess_for_ocr(path: Path) -> Path:
    """弱图预处理：灰度 → 自动对比度 → 放大 1.5 倍。

    依据（真实报告实测，tests/real_bench.py）：
        报告在照片里占比小、或整体偏暗时，RapidOCR 几乎读不出字：
            github_bloodtest_6  原图 17 字符 → 预处理后 307 字符
            github_bloodtest_3  原图 81 字符 → 预处理后 234 字符
        而清晰件预处理前后几乎没有变化（842 → 843），
        所以只在产出不足时才用，不做无差别预处理。
    """
    import tempfile
    from PIL import ImageOps

    im = Image.open(path).convert("L")
    im = ImageOps.autocontrast(im)
    w, h = im.size
    im = im.resize((int(w * _PREP_SCALE), int(h * _PREP_SCALE)), Image.LANCZOS)
    tmp = Path(tempfile.mkdtemp(prefix="tjzh_ocr_")) / ("prep_" + path.name + ".png")
    im.save(tmp)
    return tmp


def extract_text(path: Path, provider: str = DEFAULT_PROVIDER,
                 retry: bool = True) -> str:
    """统一入口：图片 → 文本。

    retry=True（默认）时启用自适应重试：
        先用原图 OCR；若产出字符数过少，预处理后再跑一次，取更好的那份。
        清晰件因此没有任何额外开销——预处理只在"快失败"时才发生。
    """
    if provider not in PROVIDERS:
        raise ValueError(f"未知 OCR provider: {provider}")

    text = PROVIDERS[provider](path)
    if not retry or len(text.strip()) >= _MIN_TEXT_LEN:
        return text

    tmp: Path | None = None
    try:
        tmp = preprocess_for_ocr(path)
        retry_text = PROVIDERS[provider](tmp)
        if len(retry_text.strip()) > len(text.strip()):
            return retry_text
    except Exception:                          # noqa: BLE001
        pass                                   # 预处理失败就用原图结果，不中断链路
    finally:
        if tmp:
            for f in tmp.parent.iterdir():
                f.unlink(missing_ok=True)
            tmp.parent.rmdir()

    return text
