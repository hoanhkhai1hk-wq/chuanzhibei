"""
《体检之后》—— 大模型调用的唯一出口

================================================================================
为什么要有这一层
================================================================================
业务代码里一旦出现 `OpenAI(api_key=...)` 或 `if model == "deepseek":`，
多模型就会从「配置问题」退化成「改业务代码」：每接一个新模型，
解读、问答、流式三处都要跟着改一遍，而且总会漏掉一处。

这里把所有差异收进一张表 + 一个函数：

    MODELS       模型注册表：新增模型只往这张表里加一行
    call_llm()   唯一出口：业务代码只认这一个函数

    call_llm(prompt, model="deepseek", system=..., json_mode=...) -> LLMReply

================================================================================
为什么另两个模型是「占位」而不是直接接上
================================================================================
本项目最有价值的部分不是调了哪个模型，而是**数字不来自模型**。
模型只是把已经定稿的事实改写成大白话，换谁来写都一样。
所以模型位次是可替换的：先接 DeepSeek，通义千问与 Kimi 在注册表里标记为
`wiring`，选中时**静默回落到默认模型**并回一个 `notice`，
由界面如实告诉用户「该模型正在接入中，当前使用 DeepSeek」——
不报错、不中断，也不假装已经接上了。

接新模型时只要做两件事：
    1. 在 MODELS 里加一行（写清读哪个环境变量、base_url 是什么）；
    2. 把 status 从 "wiring" 改成 "ready"。
业务代码一行都不用动。

================================================================================
写 model_id 之前先去官网对清单
================================================================================
这个文件里的三个 model_id 都曾经是退役名，而且**退役名不会报错**：
DeepSeek 的 `deepseek-chat` 至今仍能调通，服务端会静默改写到你没要求的模型上，
于是「配置里写的」和「实际跑的」变成两件事，日志、成本、效果全都对不上账。
Kimi 的 `moonshot-v1` 系列已于 2026-08-31 下线，通义 `qwen-plus` 也已让位给
`qwen3.7-plus`。

所以定下一条规矩：改 model_id 前先 `GET /v1/models` 对一遍清单，
或去厂商的「模型列表 / 下线公告」页核一次，不要凭记忆写。
下面每个 model_id 后面都留了核查日期与出处。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

#: 默认模型。所有回落都指向它，不指向"第一个 ready 的"——
#: 回落目标必须是固定的，否则今天选 Kimi 落到 DeepSeek、明天接了通义就落到通义，
#: 同一个操作产生不同结果，这种不确定性比功能缺失更难排查。
DEFAULT_MODEL_KEY = "deepseek"


@dataclass(frozen=True)
class ModelSpec:
    """一个模型的全部接入信息。"""

    key: str                    # 前端与接口里用的标识
    label: str                  # 界面显示名
    vendor: str                 # 厂商，用于日志与溯源
    model_id: str               # 实际送进 API 的模型名
    base_url: str               # OpenAI 兼容端点
    env_key: str                # 读哪个环境变量拿 API Key
    env_base: str               # 覆盖 base_url 的环境变量
    env_model: str              # 覆盖 model_id 的环境变量
    status: str = "wiring"      # ready=已接通；wiring=占位，选中时回落
    notice: str = ""            # 占位时给用户的提示

    @property
    def is_ready(self) -> bool:
        return self.status == "ready"

    def has_key(self) -> bool:
        """是否配置了 Key。没有 Key 时上层走模板模式，而不是报错。"""
        return bool(os.getenv(self.env_key, ""))

    def effective_model_id(self) -> str:
        return os.getenv(self.env_model, "") or self.model_id

    def effective_base_url(self) -> str:
        return os.getenv(self.env_base, "") or self.base_url


#: ============================== 模型注册表 ==============================
#: 新增模型 = 加一行。三家的 SDK 都兼容 OpenAI 协议，
#: 所以不需要为每个厂商写分支——差异只是 base_url、模型名与环境变量名。
MODELS: dict[str, ModelSpec] = {
    "deepseek": ModelSpec(
        key="deepseek",
        label="DeepSeek V4.1",
        vendor="DeepSeek",
        # 2026-10-19 核对 GET /v1/models：在售 deepseek-flash / deepseek-v4-pro。
        # 旧名 deepseek-chat 已退役，调用不报错但会被服务端静默改写到 flash，
        # 配置与实际不符，别再写回去。
        model_id="deepseek-flash",
        base_url="https://api.deepseek.com/v1",
        env_key="DEEPSEEK_API_KEY",
        env_base="DEEPSEEK_BASE_URL",
        env_model="DEEPSEEK_MODEL",
        status="ready",
        notice="",
    ),
    "qwen": ModelSpec(
        key="qwen",
        label="通义千问",
        vendor="阿里云百炼",
        # 2026-10-19 核对百炼模型列表：qwen-plus 已被列为旧版并进入下线流程，
        # 现推荐 qwen3.7-plus。这一项仍是 wiring，真正接通时再验证一次。
        model_id="qwen3.7-plus",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        env_key="DASHSCOPE_API_KEY",
        env_base="QWEN_BASE_URL",
        env_model="QWEN_MODEL",
        status="wiring",
        notice="该模型正在接入中，当前使用 DeepSeek",
    ),
    "kimi": ModelSpec(
        key="kimi",
        label="Kimi",
        vendor="月之暗面 Moonshot",
        # 2026-10-19 核对 Kimi 模型列表：moonshot-v1 全系已于 2026-08-31 下线，
        # 现役为 kimi-k3（旗舰）/ kimi-k2.7-code / kimi-k2.6。
        # 启用时注意 K3 用 reasoning_effort（默认 max），会先烧推理再出正文，
        # max_tokens 给太小会拿到空 content。
        model_id="kimi-k3",
        base_url="https://api.moonshot.cn/v1",
        env_key="MOONSHOT_API_KEY",
        env_base="KIMI_BASE_URL",
        env_model="KIMI_MODEL",
        status="wiring",
        notice="该模型正在接入中，当前使用 DeepSeek",
    ),
}


@dataclass
class LLMReply:
    """一次调用的完整结果。

    `notice` 非空表示**这次不是用你选的模型跑的**——
    界面必须把它显示出来，否则用户会以为切换生效了。
    """

    text: str
    model_key: str
    model_label: str
    model_id: str
    vendor: str
    notice: str = ""


def resolve_model(key: Optional[str]) -> tuple[ModelSpec, str]:
    """把「用户选的模型」解析成「实际要用的模型 + 给用户的提示」。

    两种回落，都记在 notice 里，绝不静默：
        1. 未登记的 key          → 用默认模型，提示会说明
        2. 登记了但 status≠ready → 用默认模型，提示用该模型自己的 notice

    "没指定"和"指定了一个不存在的"必须分开：前者是正常调用（前端已经不传 model 了），
    后者才是需要告诉用户的事。以前这里把 None 也当成"未登记"，
    于是每一次正常调用都会带一句「未登记的模型「None」」——
    提示一旦天天出现，就等于没有提示。
    """
    raw = (key or "").strip()
    spec = MODELS.get(raw.lower())
    if spec is None:
        fallback = MODELS[DEFAULT_MODEL_KEY]
        if not raw:
            return fallback, ""
        return fallback, f"未登记的模型「{key}」，当前使用 {fallback.label}"
    if not spec.is_ready:
        fallback = MODELS[DEFAULT_MODEL_KEY]
        return fallback, spec.notice or f"{spec.label}正在接入中，当前使用 {fallback.label}"
    return spec, ""


def list_models() -> list[dict]:
    """给接口用。同时暴露 status 与 has_key，
    让调用方能区分「没接」和「接了但没配 Key」这两种不同的状态。"""
    return [
        {
            "key": m.key,
            "label": m.label,
            "vendor": m.vendor,
            "status": m.status,
            "ready": m.is_ready,
            "has_key": m.has_key(),
            "notice": m.notice,
            "is_default": m.key == DEFAULT_MODEL_KEY,
        }
        for m in MODELS.values()
    ]


def default_model_key() -> str:
    return DEFAULT_MODEL_KEY


def call_llm(prompt: str, model: Optional[str] = None, *,
             system: str = "",
             max_tokens: int = 2048,
             temperature: float = 0.3,
             json_mode: bool = False) -> LLMReply:
    """调大模型。全项目**只有这一个函数**真正发起请求。

    参数
        prompt       用户消息（已经拼好事实清单的那种）
        model        用户选的模型 key；缺省或不可用时回落到默认模型
        system       系统提示
        json_mode    要求结构化输出（解读用的 LabReport 契约需要它）

    返回
        LLMReply，其中 notice 非空表示实际用的不是你选的那个模型。

    为什么不在函数里 catch 异常：
        调用方（解读 / 问答）都已经有「退回本地模板」的兜底路径，
        在这里吞掉异常反而会让上层分不清「模型不可用」和「模型编了数字」。
    """
    spec, notice = resolve_model(model)

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    text = _chat(spec, messages, max_tokens=max_tokens,
                 temperature=temperature, json_mode=json_mode)
    return LLMReply(
        text=text,
        model_key=spec.key,
        model_label=spec.label,
        model_id=spec.effective_model_id(),
        vendor=spec.vendor,
        notice=notice,
    )


def _chat(spec: ModelSpec, messages: list[dict], *, max_tokens: int,
          temperature: float, json_mode: bool) -> str:
    """真正发请求的地方。三家都走 OpenAI 兼容协议，所以只有一份实现。"""
    try:
        from openai import OpenAI          # 延迟导入：没装依赖也能起服务
    except ImportError as e:               # pragma: no cover
        raise RuntimeError(
            f"未安装 openai 依赖，无法调用 {spec.label}。"
            "执行 pip install openai 后重启，或清空 API Key 使用模板模式。"
        ) from e

    client = OpenAI(api_key=os.getenv(spec.env_key, ""),
                    base_url=spec.effective_base_url())

    # 每次真发请求都记一行实际模型名。退役名不会报错、服务端会静默改写，
    # 不留下这一行，事后就没法证明"当时发的到底是哪个"。
    # 用 print 而不是 logger：uvicorn 只配了自己的 logger，
    # 模块级 logger 的 INFO 会被丢掉，去日志里查反而查不到。
    print(f"[调用] {spec.label} → {spec.effective_model_id()}", flush=True)

    kwargs: dict = {
        "model": spec.effective_model_id(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,          # 显式截断，避免无限生成
    }
    if json_mode:
        # 不是所有模型都支持 json_object；交给服务端报错，不在这里猜
        kwargs["response_format"] = {"type": "json_object"}

    resp = client.chat.completions.create(**kwargs)
    return (resp.choices[0].message.content or "").strip()
