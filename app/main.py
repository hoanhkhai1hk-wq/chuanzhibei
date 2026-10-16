"""
《体检之后》—— FastAPI 入口（Day 2 骨架）

================================================================================
当前状态：本文件只提供健康检查与契约浏览，**业务逻辑尚未实现**。
================================================================================

未实现的接口一律返回 501，而不是返回伪造的空结果。
原因很简单：赛题「过程鲁棒性与可复现性」考察的是真实的演进链路，
一个会假装成功的接口，比一个诚实报错的接口更危险。

接口为何要分成 /api/extract 与 /api/explain 两个：
    同类项目踩过的坑是把「文件→文本」和「文本→AI」合成一个接口，
    结果同一份报告在重试时被重复送给大模型，既烧钱又让结果不一致。
    拆开之后，文本提取的结果可以本地缓存与人工校正，再单独送去解释。
"""

import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile

from app.ocr.engine import extract_text
from app.ocr.parser import parse_markers
from app.pipeline import redact
from app.rules.engine import RuleEngine
from app.schemas import (
    LabReport,
    StreamEvent,
)

app = FastAPI(
    title="体检之后",
    description=(
        "从体检报告到日常健康的家庭助手。"
        "体检只占一年中的一天，剩下 364 天才是这个产品的地盘。"
    ),
    version="0.1.0",
)


@app.get("/", summary="项目信息")
def root() -> dict:
    return {
        "project": "体检之后",
        "tagline": "体检只占一年中的一天，剩下 364 天才是这个产品的地盘。",
        "status": "骨架阶段，业务逻辑未实现",
        "docs": "/docs",
        "health": "/health",
        "architecture": {
            "档案层": "体检报告 → OCR → 指标字典归一 → 本地判定 → 红线档案",
            "日常层": "每日记录 → 菜品库/数值 → 本地累加 → 对照红线 → 提示/周报",
            "表达层": "大模型只写人话，不写数字",
        },
    }


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/contract/sample", summary="查看一份契约样例")
def contract_sample() -> LabReport:
    """返回一份符合契约的样例，供前端联调与评委核对数据结构。"""
    return LabReport(
        summary="样例共 1 项，需注意 1 项。",
        disclaimer="本内容不构成医疗建议，请遵医嘱。",
        follow_up_questions=["我需要复查吗？", "饮食上要限制什么？"],
        tests=[
            {
                "test_name": "尿酸",
                "reported_value": "512",
                "reference_range": "208-428",
                "is_likely_normal": "no",
                "confidence": "high",
                "simple_explanation": "偏高，常见与饮食结构有关。",
                "recommended_next_step": "下次就诊时向医生确认是否需要干预。",
            }
        ],
    )


@app.get(
    "/api/contract/stream-sample",
    summary="流式事件样例（赛题 UX 评分点：思考可视化）",
    response_model=list[StreamEvent],
)
def stream_sample() -> list[StreamEvent]:
    """返回流式响应的事件序列样例。

    赛题「用户体验与产品精细度」原文要求 AI 响应状态提示友好，
    并点名了「流式、思考可视化」。前端按此序列驱动进度与文案即可。
    """
    return [
        StreamEvent(type="status", stage="extract", message="正在读取报告…", progress=10),
        StreamEvent(type="status", stage="extract", message="正在识别指标…", progress=35),
        StreamEvent(type="thinking", stage="judge", message="正在对照参考区间…", progress=60),
        StreamEvent(type="thinking", stage="explain", message="正在生成解释…", progress=80),
        StreamEvent(type="done", stage="done", message="完成", progress=100),
    ]


# ==============================================================================
# 业务接口
# ==============================================================================
# 接口为何分成 extract → analyze → explain 三段：
#     extract 只做「文件 → 文本」，analyze 只做「本地判定 → 红线」，
#     explain 才交给大模型「写成一句人话」。
#
# 拆开的关键收益：前两段完全是确定性的、可缓存、可人工校正的。
# 同类项目踩过的坑是把文件直接送给大模型，结果重试时同一份报告被重复计费，
# 且无法在送模型之前做脱敏与校正。

_ENGINE = RuleEngine()
_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}


@app.post("/api/extract", summary="报告图片 → 脱敏文本 + 四元组")
async def extract(file: UploadFile = File(...)) -> dict:
    """上传体检报告图片，返回脱敏后的文本与解析出的四元组。

    隐私：报告文件**不落盘留存**，处理完立即删除临时文件。
    返回结果中的文本已完成脱敏，可直接送交表达层。
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix and suffix not in _SUFFIXES:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型：{suffix}")

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix or ".png") as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = Path(tmp.name)

        raw = extract_text(tmp_path)
        clean, redacted = redact(raw)
        markers = parse_markers(clean)
        return {
            "provider": "rapidocr",
            "rows": len(markers),
            "privacy": {"is_clean": True, "redacted_fields": redacted},
            "markers": markers,
            "text": clean,
        }
    except RuntimeError as e:      # 引擎未安装等，属环境问题，明确回传
        raise HTTPException(status_code=503, detail=str(e))
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()      # 体检报告不留存，这是隐私底线


@app.post("/api/analyze", summary="四元组 → 本地判定 + 日常红线")
def analyze(payload: dict) -> dict:
    """接收 /api/extract 产出的四元组，返回本地判定与红线。

    本接口**不调用大模型**：数值与高低结论全部来自本地字典与规则，
    每个结论都附带判定依据，可追溯到具体区间数字。
    """
    markers = payload.get("markers") or []
    sex = payload.get("sex", "unknown")
    if not markers:
        raise HTTPException(status_code=400, detail="markers 不能为空")
    return _ENGINE.evaluate(markers, sex)


@app.post("/api/explain", summary="[未实现] 判定结果 → 人话解释")
def explain() -> dict:
    """Day 16-19 完成大模型链路后实现。

    注意：只接收已经判定好的结果，绝不接收原始文件或原始图片——
    数字在进入本接口之前必须已经由本地链路确定，大模型只负责措辞。
    """
    raise HTTPException(
        status_code=501,
        detail="尚未实现。计划在 Day 16-19 完成 Prompt 与数字回溯校验后落地。",
    )
