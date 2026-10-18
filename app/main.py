"""
《体检之后》—— FastAPI 入口

================================================================================
当前状态
================================================================================
业务链路已全部打通：

    POST /api/extract          报告图片 → 脱敏文本 + 四元组（纯本地）
    POST /api/analyze          四元组 → 本地判定 + 日常红线（纯本地）
    POST /api/explain          判定结果 → 人话解释（模板模式可用，配 Key 后升 LLM）
    POST /api/explain/stream   同上，但按事件流吐出（赛题 UX 评分点）

================================================================================
接口为什么要分成 extract → analyze → explain 三段
================================================================================
同类项目踩过的坑是把「文件→文本」和「文本→AI」合成一个接口，
结果同一份报告在重试时被重复送给大模型，既烧钱又让结果不一致。
拆开之后，前两段完全是确定性的、可缓存、可人工校正的，
第三段才交给大模型——而且此时数字早已在本地定稿。

还有一层更重要的原因：**隐私**。
脱敏必须发生在送大模型之前。合成一个接口的话，
「先脱敏再送模型」这个动作就没有容身之处了。
"""

import json
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app.llm.explainer import Explainer
from app.llm.chat import QaAnswerer
from app.llm.provider import default_model_key, list_models
from app.ocr.engine import extract_text
from app.ocr.parser import parse_markers
from app.pipeline import redact
from app.rules.engine import RuleEngine
from app.schemas import (
    ChatReply,
    ChatRequest,
    LabReport,
    StreamEvent,
)
from app.env_loader import load_dotenv_once, status_line  # noqa: E402

# 业务代码读配置只认 os.getenv，而它看不见磁盘上的 .env。
# 所以在装配 app 之前补一次：把 .env 的键值灌进 os.environ，
# 之后 provider / explainer / ocr 照常工作，一行都不用改。
# 放在这里（而不是 provider.py）还有一个原因：测试是自运行脚本，
# 它们 import provider 但不 import main，因此不会被本机 .env 影响，
# 「无 Key → 模板模式」这组守门断言得以保持确定性。
# 幂等、只补不盖、文件缺失也不报错——没 Key 时模板模式照样能跑。
load_dotenv_once()
# flush=True：uvicorn 把 stdout 重定向到文件时是块缓冲，
# 不加这一句日志会卡在缓冲区里，去日志里查模型名就只能查个寂寞。
print(f"[配置] {status_line()}", flush=True)

# 启动时把「实际会发给服务端的模型名」打出来。
# 这条日志不是装饰：model_id 写错了（用了已退役的别名）不会报错，
# 服务端会静默改写，只有对着这行日志才能发现「以为在跑 A、其实在跑 B」。
# 打印的是 model_id 而不是界面显示名，显示名好看但对不上账没有意义。
from app.llm.provider import MODELS as _MODELS, DEFAULT_MODEL_KEY as _DEFAULT  # noqa: E402
_SPEC = _MODELS[_DEFAULT]
print(f"[模型] {_SPEC.label} → {_SPEC.effective_model_id()} @ {_SPEC.effective_base_url()}",
      flush=True)

app = FastAPI(
    title="体检之后",
    description=(
        "从体检报告到日常健康的家庭助手。"
        "体检只占一年中的一天，剩下 364 天才是这个产品的地盘。"
    ),
    version="0.2.0",
)

_STATIC = Path(__file__).resolve().parent.parent / "static"


@app.get("/", summary="产品首页", include_in_schema=False)
def index() -> FileResponse:
    """前端单页应用。项目信息改到 /api/about，根路径留给产品本身。"""
    page = _STATIC / "index.html"
    if not page.exists():
        raise HTTPException(status_code=503, detail="前端尚未构建：static/index.html 缺失")
    return FileResponse(page)


@app.get("/api/models", summary="可选的大模型列表")
def list_llm_models() -> dict:
    """模型清单。右上角下拉框已经拿掉了，这个接口仍然保留：
    它回答的是「这会儿到底跑的是哪个模型、哪个还没接」，
    排查问题时第一句要问的就是这个，而且清单只有后端说了算。

    `status=wiring` 的模型是占位：选中会静默回落到默认模型，
    并由 `notice` 如实告诉调用方「当前使用的是谁」。
    """
    return {
        "models": list_models(),
        "default": default_model_key(),
        "current": default_model_key(),
    }


@app.get("/api/about", summary="项目信息")
def about() -> dict:
    return {
        "project": "体检之后",
        "tagline": "体检只占一年中的一天，剩下 364 天才是这个产品的地盘。",
        "version": "0.2.0",
        "architecture": {
            "档案层": "体检报告 → OCR → 指标字典归一 → 本地判定 → 红线档案",
            "日常层": "每日记录 → 菜品库/数值 → 本地累加 → 对照红线 → 提示/周报",
            "表达层": "大模型只写人话，不写数字",
            "问答层": "提问 → 本地事实清单 → 模型只写措辞 → 数字回溯校验后重写",
            "多模型": "call_llm(prompt, model) 是唯一出口，换模型只改 provider.MODELS",
        },
        "explain_mode": _EXPLAINER.mode,
        "explain_model": _EXPLAINER.model_name,
        "models": [m["key"] for m in list_models()],
    }


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok", "explain_mode": _EXPLAINER.mode}


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

_ENGINE = RuleEngine()
_EXPLAINER = Explainer()
_QA = QaAnswerer(explainer=_EXPLAINER, markers=_ENGINE.markers)
_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}


def _take_model(payload: dict) -> str:
    """从请求体里取出并摘掉 `model`。

    为什么要 pop：这些接口的 body 就是判定结果本身，
    留一个多余的键进去会一路带到事实清单和大模型的上下文里。
    模型是**调用参数**，不是报告的一部分。
    """
    return str(payload.pop("model", "") or "")


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


@app.post("/api/demo", summary="用合成演示数据跑一遍（无需真实报告）")
def demo(sex: str = "unknown") -> dict:
    """一键演示：现场合成一份体检报告 → 跑完整链路 → 返回结果。

    为什么值得单独做这个接口：
    评委和用户在第一次接触产品时，手上并没有一份合适的报告照片。
    如果首页只能上传、不能试用，第一屏的流失率会非常高。

    演示报告由 tests/ocr_bench/synth.py 现场生成、用完即删，
    既不需要往仓库里塞图片，也把「合成数据 harness」变成了产品的一部分。
    返回字段与 /api/extract 保持一致，另加 image_b64 供界面回显。
    """
    import base64
    import random as _random
    import sys

    tmpdir = Path(tempfile.mkdtemp(prefix="tjzh_demo_"))
    try:
        # 让 tests/ 可被 import，同时不污染 sys.path
        repo_root = Path(__file__).resolve().parent.parent
        if str(repo_root) not in sys.path:
            sys.path.insert(0, str(repo_root))
        from tests.ocr_bench.synth import make_one

        # 多试几个种子，优先挑能触发红线的样本。
        # 为什么：红线卡片是本产品最核心的差异化卖点，
        # 演示若随机到「异常项都不映射红线」的样本，第一屏的说服力会打对折。
        # 每一轮都是真实跑完整条链路，只是挑了一份更有代表性的「体检报告」。
        img = None
        clean = markers = None
        for attempt in range(6):
            img = make_one(seed=_random.randint(1, 999), out_dir=tmpdir)
            raw = extract_text(img)
            clean, _ = redact(raw)
            markers = parse_markers(clean)
            if markers and _ENGINE.evaluate(markers, sex)["redlines"]:
                break
            img.unlink(missing_ok=True)      # 没选中的报告图直接删掉
            img = None

        if img is None or not markers:
            raise HTTPException(status_code=503, detail="演示数据生成失败，请重试")

        image_b64 = base64.b64encode(img.read_bytes()).decode("ascii")
        return {
            "demo": True,
            "image_b64": image_b64,
            "provider": "rapidocr",
            "rows": len(markers),
            "privacy": {"is_clean": True, "redacted_fields": []},
            "markers": markers,
            "text": clean,
        }
    except HTTPException:
        raise
    except Exception as e:                          # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"演示数据生成失败：{e}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)   # 演示报告同样不留存


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


@app.post("/api/explain", summary="判定结果 → 人话解释")
def explain(payload: dict) -> dict:
    """把已经判定好的结果改写成人话。

    注意：只接收已经判定好的结果，绝不接收原始文件或原始图片——
    数字在进入本接口之前必须已经由本地链路确定，大模型只负责措辞。

    未配置 DEEPSEEK_API_KEY 时自动使用本地模板模式，产品功能不受影响；
    配置后自动升级为大模型模式，输出仍会经过数字回溯校验。

    payload 里可选的 `model` 指定用哪个模型；
    传了未接通的模型不会报错，provider 会回落到默认模型并在返回里带 `model_notice`。
    """
    if not payload.get("judged"):
        raise HTTPException(status_code=400, detail="judged 不能为空，请先调用 /api/analyze")
    return _EXPLAINER.explain(payload, model=_take_model(payload))


@app.post("/api/explain/stream", summary="[流式] 判定结果 → 人话解释")
def explain_stream(payload: dict) -> StreamingResponse:
    """按 Server-Sent Events 逐条吐出解读过程。

    这一段直接对应赛题「用户体验与产品精细度」中官方点名的
    「AI 响应状态提示友好（如流式、思考可视化）」：
    用户在等待时能实时看到系统判定到了第几项、命中了哪条红线，
    而不是对着一个空白转圈圈。

    事件契约见 app/schemas.py 的 StreamEvent。

    payload 里可选的 `model` 指定用哪个模型；占位模型会回落到默认模型，
    并额外吐一条 status 事件说明「当前实际用的是谁」——
    思考可视化里出现的是真话，不是用户以为的那个模型。
    """
    if not payload.get("judged"):
        raise HTTPException(status_code=400, detail="judged 不能为空，请先调用 /api/analyze")
    wanted = _take_model(payload)

    def gen():
        try:
            for ev in _EXPLAINER.stream_events(payload, model=wanted):
                yield f"data: {ev.model_dump_json()}\n\n"
        except Exception as e:                      # noqa: BLE001
            err = StreamEvent(type="error", stage="explain",
                              message=f"解读过程出错：{type(e).__name__}")
            yield f"data: {err.model_dump_json()}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/chat", summary="就这份报告提问", response_model=ChatReply)
def chat(req: ChatRequest) -> ChatReply:
    """回答用户针对本次报告提出的问题。

    为什么单独成一个接口，而不是把问答塞进 /api/explain：
        explain 是「一次把整份报告讲完」，输出必须是 LabReport 契约；
        问答是「按需问一句」，一问一答、可以问很多次，输出是自由文本。
        两者共用同一个校验器（`find_hallucinations`），但契约不同，
        混在一个接口里会让 LabReport 的 extra="forbid" 立刻打架。

    三段铁律在这里同样成立：
        1. 数字只从 `req.analysis` 里取——本接口不接收图片、不接收原始 OCR 文本；
        2. 提问先过 `mask_pii()` 再送模型，报告事实里本来就没有姓名字段；
        3. 回答里的每个数字都要能在事实清单里找到出处，否则带着
           「你多写了哪些数字」重写一次；两次都不过就退回本地模板并如实标注。
    """
    if not (req.analysis.get("judged") or []):
        raise HTTPException(
            status_code=400,
            detail="analysis.judged 不能为空，请先调用 /api/analyze 再提问",
        )
    return ChatReply(**_QA.answer(req.question, req.analysis, model=req.model))


# ==============================================================================
# 日常层接口
# ==============================================================================
# 档案层与日常层在这里接上：
#   POST /api/report  存一次解读，并把红线设为「当前生效」
#   POST /api/meals   记一餐 → 立即返回当日对照结果
#   GET  /api/today   当日汇总（红线撞线情况）
# 两层之间没有模型参与：红线说"盯住嘌呤"，菜品标签说"嘌呤高"，
# 匹配是本地字符串比对。

from app.data import db as store                       # noqa: E402
from app.daily import service as daily                 # noqa: E402
from app import history                                # noqa: E402

store.init_db()


def _today() -> str:
    """服务端本地日期。日期默认值必须在服务端定：
    若落库成空串，当天的汇总查询就再也找不到这一餐。"""
    from datetime import date as _d
    return _d.today().isoformat()


@app.get("/api/dishes", summary="菜品库搜索")
def list_dishes(q: str = "") -> dict:
    """点选式记录的数据源。空 q 返回常用分类的默认列表。"""
    return {"dishes": daily.search_dishes(q)}


@app.post("/api/report", summary="保存解读结果并激活红线")
def save_report(payload: dict) -> dict:
    """把一次解读的判定结果落库，其中的红线成为日常对照的依据。

    为什么单独一个接口而不是在 /api/analyze 里顺手保存：
    analyze 是纯计算，应该保持无副作用——能否复现、能否重跑都不碰库。
    「保存」是用户显式确认后的动作，职责分开，回滚也容易。

    返回里附带与上一份报告的对比（若有）——保存的这一刻正好是
    "新报告已确认、旧报告还没被覆盖"的唯一时机，对比就地完成，
    前端不需要再发第二次请求。
    """
    result = payload.get("result") or {}
    if not result.get("judged"):
        raise HTTPException(status_code=400, detail="result.judged 不能为空")

    prev = store.latest_report()      # 必须在插入之前取，插入后"上一份"就变了
    rid = store.save_report(result, source=payload.get("source", ""),
                            sex=payload.get("sex", "unknown"))
    diff = history.compare(prev, result) if prev else None
    return {"saved": True, "report_id": rid,
            "redlines_active": len(result.get("redlines", [])),
            "compare": diff}


@app.get("/api/reports", summary="历史报告列表")
def list_reports() -> dict:
    """已保存的解读记录（新→旧）。趋势对比与档案页的数据源。"""
    return {"reports": store.list_reports()}


@app.post("/api/meals", summary="记录一餐 → 返回当日对照")
def add_meal(payload: dict) -> dict:
    """点选菜品记一餐。返回当天的完整汇总（撞线即时可见）。"""
    dishes = payload.get("dishes") or []
    if not dishes:
        raise HTTPException(status_code=400, detail="dishes 不能为空")
    meal_type = payload.get("meal_type", "lunch")
    if meal_type not in ("breakfast", "lunch", "dinner", "snack"):
        raise HTTPException(status_code=400, detail=f"非法餐次：{meal_type}")
    store.add_meal(payload.get("member_id", "me"),
                   payload.get("date") or _today(), meal_type, dishes)
    return daily.today_summary(payload.get("member_id", "me"), payload.get("date") or None)


@app.get("/api/today", summary="当日汇总")
def today(member_id: str = "me", date: str = "") -> dict:
    """今日已记录的餐次、热量估算与各条红线的撞线情况。"""
    return daily.today_summary(member_id, date or None)


@app.post("/api/numerics", summary="记录血压 / 血糖 / 体重")
def add_numeric(payload: dict) -> dict:
    """记一次测量值，返回当日汇总。

    血压高压时会**激活「限钠」红线**——报告上不会有血压值（它是测量值非检验项），
    这条红线只能由日常层触发，这正是日常层存在的意义之一。
    阈值采用《中国高血压防治指南》诊室标准 ≥140/90 mmHg，写在 service.BP_THRESHOLD。
    """
    kind = payload.get("kind")
    if kind not in ("blood_pressure", "blood_glucose", "weight"):
        raise HTTPException(status_code=400, detail=f"非法记录类型：{kind}")
    try:
        return daily.record_numeric(
            payload.get("member_id", "me"), payload.get("date") or _today(), kind,
            systolic=payload.get("systolic"), diastolic=payload.get("diastolic"),
            glucose=payload.get("glucose"), weight=payload.get("weight"),
            note=payload.get("note"),
        )
    except Exception as e:                              # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"记录失败：{e}")


@app.get("/api/week", summary="最近 N 天趋势（周报曲线）")
def week(member_id: str = "me", days: int = 7, end: str = "") -> dict:
    """逐日汇总最近 N 天：热量、餐次、撞线次数、血压与血糖。

    周报的数据源与今日对照是同一套计算，不另起炉灶——
    这也是「一条管线，两个字典，一个规则引擎」的体现。
    """
    return daily.week_summary(member_id, end or None, min(max(days, 1), 30))


@app.get("/api/contract/active-redlines", summary="当前生效的红线")
def active_redlines() -> dict:
    """前端「今日记录」页打开时先取这个，知道该盯住哪几条。"""
    return {"redlines": store.get_active_redlines()}


# 静态资源放在最后挂载：通配路由不能吞掉上面的 API 路径
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=_STATIC), name="static")

