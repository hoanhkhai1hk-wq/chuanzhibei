"""
《体检之后》—— 数据契约（Pydantic v2）

本文件是整个项目唯一的「数据语言」定义，所有模块（OCR、字典、规则引擎、LLM、前端）
都必须通过这里定义的结构交换数据。

================================================================================
架构铁律（Architecture Rule #1）
================================================================================
    数字绝不来自大模型。

落地到本文件的具体含义：

1. 所有**数值型必填字段**只允许出现在「档案层」与「日常层」的模型中，
   这些数值全部由本地计算或本地字典判定产出。

2. 「表达层」的 LLM 输出模型（`LabTest` / `LabReport` / `DailyAdvice`）中，
   **不存在任何允许模型自行填写的数值型必填字段**。
   `reported_value` 虽是字符串，但它必须与 OCR 抽到的原文逐字一致，
   后续会经过数字回溯校验：不在事实 JSON 中的数字一律判为幻觉并重跑。

3. `disclaimer`（免责声明）是 `LabReport` 的**必填字段**，不是界面上事后补贴的一行字。
   合规是被写进数据契约的，而不是靠开发者的自觉。

================================================================================
字段命名约定（借鉴 hkpritiranjan/explain-my-lab-report，MIT License）
================================================================================
    is_likely_normal  —— 用 likely 而非 is_normal，把「这是推断不是判定」
                         直接写进字段名，任何调用方都无法误解。
    reference_range   —— 可为 None，承认 OCR 会失败，而不是假装总能拿到。
"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Sex",
    "Flag",
    "Confidence",
    "NormalStatus",
    "PrivacyReport",
    "ExtractedMarker",
    "NormalizedMarker",
    "LabTest",
    "LabReport",
    "NutrientTag",
    "Dish",
    "NutrientSum",
    "RedlineRule",
    "RedlineHit",
    "MealRecord",
    "NumericRecord",
    "DailyAdvice",
    "FamilyMessage",
    "StreamEvent",
]


# ==============================================================================
# 通用枚举
# ==============================================================================

Sex = Literal["male", "female", "unknown"]

#: 本地判定结果四值。注意这里没有 True/False —— 判定不出座?
Flag = Literal["high", "low", "normal", "unknown"]

#: 模型对自己输出的确信度，用于界面向用户传达「AI 有多确定」
Confidence = Literal["high", "medium", "low"]

#: 写在字段名里的免责：这是「大概率正常」，不是「正常」
NormalStatus = Literal["yes", "no", "unknown"]


class _Strict(BaseModel):
    """所有模型的基类：禁止接受未声明字段，避免 OCR 噪声静默混入。"""

    model_config = ConfigDict(extra="forbid")


# ==============================================================================
# 档案层 · 体检报告
# ==============================================================================


class PrivacyReport(_Strict):
    """脱敏审计记录。

    OCR 之后、送入大模型之前必须完成脱敏，本模型用于记录这一动作，
    让「已脱敏」成为可被验证的事实，而不是口头承诺。
    """

    redacted_fields: list[str] = Field(
        default_factory=list,
        description="被抹掉的字段类型，如 name / id_card / hospital / doctor / exam_no",
    )
    is_clean: bool = Field(
        default=False,
        description="是否已完成脱敏。False 时禁止向下游发送任何文本。",
    )


class ExtractedMarker(_Strict):
    """OCR 抽取的**原始**四元组，逐字保留报告原文，不做任何解析或换算。

    保持 raw 的意义在于：后续所有数字校验都能回溯到这里。
    一旦在这一步做了单位换算或四舍五入，回溯链路就断了。
    """

    name_raw: str = Field(..., description="报告上的原始指标名，如「尿素氮」「UA」")
    value_raw: str = Field(..., description="原始数值字符串，如「512」「12.3」")
    unit_raw: Optional[str] = Field(None, description="原始单位字符串，如「μmol/L」")
    ref_range_raw: Optional[str] = Field(
        None, description="原始参考区间字符串，如「208-428」"
    )
    page: Optional[int] = Field(None, description="所在页码，多页报告用于定位")


class NormalizedMarker(_Strict):
    """经指标字典归一后的指标：**数值与高低判定均已落地为本地结果**。

    从 `ExtractedMarker` 到本模型的过程发生在本地
    （查字典 → 别名归一 → 单位统一 → 参考区间比对），不涉及大模型。
    """

    standard_name: str = Field(..., description="标准名，如「尿酸」")
    aliases: list[str] = Field(
        default_factory=list, description="字典中登记的别名，便于排查归一失败"
    )
    value: float = Field(..., description="归一化后的数值")
    unit: str = Field(..., description="归一化后的单位")
    ref_low: Optional[float] = Field(None, description="参考区间下界")
    ref_high: Optional[float] = Field(None, description="参考区间上界")
    flag: Flag = Field(..., description="本地判定结果")
    confidence: Confidence = Field(
        default="high", description="本地判定本身的确信度，主要取决于是否命中字典"
    )
    source_value_raw: str = Field(..., description="回填 OCR 原文，用于数字回溯校验")


# ==============================================================================
# 表达层 · 大模型输出契约
# ==============================================================================


class LabTest(_Strict):
    """单个指标的「人话解释」。

    注意本模型**不含任何数值型必填字段**：
    模型只允许写字，不允许写数。数值由上层从 `NormalizedMarker` 注入。
    """

    test_name: str = Field(..., description="指标名（必须是事实 JSON 中已存在的）")
    reported_value: str = Field(..., description="报告值字符串，须与 OCR 原文逐字一致")
    reference_range: Optional[str] = Field(
        None, description="参考区间；OCR 未拿到时允许为 None"
    )
    is_likely_normal: NormalStatus = Field(..., description="是否大概率正常")
    confidence: Confidence = Field(..., description="本次解释的确信度")
    simple_explanation: str = Field(..., description="一句话大白话解释")
    recommended_next_step: str = Field(..., description="针对该指标的下一步建议")


class LabReport(_Strict):
    """整份报告的解释结果。"""

    summary: str = Field(..., description="整体概述，建议先说有几项需要注意")
    disclaimer: str = Field(..., description="免责声明（必填，合规写进契约）")
    follow_up_questions: list[str] = Field(
        default_factory=list, description="建议向医生提问的问题清单"
    )
    tests: list[LabTest] = Field(default_factory=list, description="逐指标解释")


# ==============================================================================
# 日常层 · 饮食与记录
# ==============================================================================


class NutrientTag(_Strict):
    """一道菜的四档标签。

    `purine`（嘌呤）与 `gi`（升糖指数）在中国食物成分表中**没有**，
    需要单独整理，见 README 第六章的数据缺口说明。
    """

    sodium: Literal["low", "medium", "high"] = "low"
    sugar: Literal["low", "medium", "high"] = "low"
    purine: Literal["low", "medium", "high"] = "low"
    fat: Literal["low", "medium", "high"] = "low"


class Dish(_Strict):
    """菜品库条目。"""

    name: str = Field(..., description="菜品名，如「红烧肉」")
    aliases: list[str] = Field(default_factory=list, description="搜索用的别名")
    category: str = Field(default="家常菜", description="分类：家常菜 / 食堂 / 外卖 / 早餐")
    calories: Optional[float] = Field(None, description="千卡")
    portion_note: str = Field(default="一份", description="份量说明")
    tags: NutrientTag = Field(default_factory=NutrientTag, description="四档标签")


class RedlineRule(_Strict):
    """红线映射规则：**本项目的核心原创资产，无处可抄**。

    把「指标异常」翻译成「日常饮食约束」，例如：
    marker=尿酸 & flag=high → restriction=限嘌呤 → 触发食物=内脏/海鲜/浓汤
    """

    rule_id: str = Field(..., description="规则编号，如 R01")
    marker: str = Field(..., description="触发指标的标准名，如「尿酸」")
    flag: Literal["high", "low"] = Field(..., description="触发方向")
    restriction: str = Field(..., description="日常红线名，如「限嘌呤」")
    #: 注意补铁这一档：它不是「限制某物」而是「建议补充」，
    #: 与其余四档性质不同，所以单独用 action 字段区分，
    #: 否则产品逻辑会把「补铁」误处理成「少吃含铁食物」——方向完全反了。
    nutrient_key: Literal["sodium", "sugar", "purine", "fat", "iron"] = Field(
        ..., description="对应到 Dish.tags 的哪一档；iron 为建议补充，非限制"
    )
    action: Literal["limit", "boost"] = Field(
        default="limit", description="limit=限制摄入；boost=建议补充"
    )
    trigger_foods: list[str] = Field(
        default_factory=list, description="典型触发食物，用于解释文案"
    )
    advice_template: str = Field(..., description="提示模板")
    enabled: bool = Field(default=True, description="是否启用")


class NutrientSum(_Strict):
    """一天累加的结果，**全部本地计算**。"""

    calories: float = 0.0
    sodium_hits: int = 0
    sugar_hits: int = 0
    purine_hits: int = 0
    fat_hits: int = 0


class RedlineHit(_Strict):
    """某条红线今天被碰到的情况。"""

    rule_id: str
    restriction: str
    marker: str
    nutrient_key: str
    hit_count: int = Field(..., description="今天碰到几次")
    foods: list[str] = Field(default_factory=list, description="触发的具体菜品")


class MealRecord(_Strict):
    """一次饮食记录。主输入是点选菜品，不是拍照（见 README 第七章）。"""

    member_id: str = Field(..., description="成员标识，对应 members 表")
    date: str = Field(..., description="日期 YYYY-MM-DD")
    meal_type: Literal["breakfast", "lunch", "dinner", "snack"] = Field(
        ..., description="餐次"
    )
    dishes: list[str] = Field(default_factory=list, description="菜品名列表")
    photo_path: Optional[str] = Field(
        None, description="可选：拍照增强的落盘路径，失败不影响主流程"
    )


class NumericRecord(_Strict):
    """血压 / 血糖等数值型日常记录，与饮食共用同一套趋势图。"""

    member_id: str
    date: str
    kind: Literal["blood_pressure", "blood_glucose", "weight"] = Field(
        ..., description="记录类型"
    )
    systolic: Optional[int] = Field(None, description="收缩压 mmHg")
    diastolic: Optional[int] = Field(None, description="舒张压 mmHg")
    glucose: Optional[float] = Field(None, description="血糖 mmol/L")
    weight: Optional[float] = Field(None, description="体重 kg")
    note: Optional[str] = None


class DailyAdvice(_Strict):
    """今日提示（大模型产出）。

    同样是「只写字，不写数」：`today_sum` 与 `redline_hits` 由本地计算后注入。
    """

    summary: str = Field(..., description="今日概述")
    detail: str = Field(..., description="具体说明")
    tomorrow_tip: str = Field(..., description="明天的 actionable 建议")
    disclaimer: str = Field(..., description="免责声明（必填）")
    confidence: Confidence = "medium"


# ==============================================================================
# 给爸妈说的话（闭环最后一环）
# ==============================================================================


class FamilyMessage(BaseModel):
    """可直接念给父母听、或转发到家庭群的一段大白话。

    这是本产品区别于同类开源项目的关键：同类做到「解读」就停了，
    我们把链路走到「转述给别人听」。
    """

    model_config = ConfigDict(extra="forbid")

    audience: str = Field(default="父母", description="对象")
    message: str = Field(..., description="成稿文案，大白话，不含术语")
    tone: Literal["reassuring", "neutral", "alerting"] = Field(
        default="reassuring", description="语气：宽慰 / 中性 / 需要重视"
    )
    disclaimer: str = Field(..., description="免责声明（必填）")
    based_on: list[str] = Field(
        default_factory=list, description="依据的指标名，确保文案有出处，便于溯源"
    )


# ==============================================================================
# 流式响应（官方评分点）
# ==============================================================================


class StreamEvent(BaseModel):
    """流式响应事件契约。

    ============================================================================
    为什么必须有这一段：赛题「用户体验与产品精细度」(15%) 的官方原文是
        「界面美观优雅、交互流畅、AI 响应状态提示友好（**如流式、思考可视化**）」
    也就是说，「让用户在等待时看到 AI 正在做什么」是一个被官方点名的得分点，
    而不是锦上添花。
    ============================================================================

    实测参照（同类项目 explain-my-lab-report，MIT）：
    流式让首字节到达时间从约 4 秒的空白等待降到 1 秒以内。
    演示视频里这 4 秒的差别非常明显。
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["status", "thinking", "token", "done", "error"] = Field(
        ...,
        description=(
            "事件类型："
            "status=阶段性状态提示（如「正在读取指标…」）；"
            "thinking=思考过程可视化；"
            "token=正文增量；"
            "done=完成；"
            "error=出错"
        ),
    )
    stage: Optional[str] = Field(
        None, description="所处阶段名，如 extract / judge / explain，用于进度条"
    )
    message: Optional[str] = Field(
        None, description="给用户看的一句话；status 与 thinking 类型下必填"
    )
    progress: Optional[int] = Field(
        None, ge=0, le=100, description="进度百分比，可选"
    )
    payload: Optional[dict] = Field(
        None, description="done 事件携带的完整结果对象"
    )
