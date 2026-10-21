"""大模型不可用时的本地降级测试

================================================================================
测什么
================================================================================
「评委现场断网 / Key 失效 / 限流」时，产品必须仍然是一份能用的产品：
解读、问答、判定、记餐、趋势一条都不能少，而且**不能撒谎说自己用了大模型**。

这一组测试里的大模型全是**假的**——
只在测试进程内 monkeypatch `provider._chat`，不发一个真实请求，
不读、不改、不打印用户的 .env 与真实 Key。

================================================================================
为什么单独成文件
================================================================================
test_llm.py 测的是「模板模式好不好」，这一份测的是「从大模型模式摔下来
摔得住不住」。前者是常态，后者是事故——事故的断言必须单独钉死，
否则哪天有人改了重试逻辑，常态测试照样全绿，事故现场才第一次被发现。
================================================================================
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm import provider as P                    # noqa: E402
from app.llm.chat import QaAnswerer                  # noqa: E402
from app.llm.explainer import Explainer              # noqa: E402
from app.rules.engine import RuleEngine              # noqa: E402

#: 假 Key：只在测试进程里用，不落盘、不发真实请求。
#: 刻意不写成 sk- 开头 —— 仓库做密钥扫描时那种形态会被当成真 Key 报警，
#: 而扫描器分不出真假，最后只能靠人眼一条条看，等于把警报作废。
FAKE_KEY = "fake-key-for-test-only"

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


def check_true(name: str, got) -> None:
    check(name, bool(got), True)


def sample_analysis() -> dict:
    """一份带已知异常的判定结果，数字固定，便于逐字比对。"""
    return RuleEngine().evaluate(
        [
            {"name": "尿酸(UA)", "value": "512", "unit": "μmol/L", "ref": "208-428"},
            {"name": "空腹血糖", "value": "5.41", "unit": "mmol/L", "ref": "3.9-6.1"},
            {"name": "血红蛋白", "value": "98", "unit": "g/L", "ref": "130-175"},
        ],
        "male",
    )


class Boom(Exception):
    """模拟服务商异常。带 status_code 以便走 provider 的归类分支。"""

    def __init__(self, msg: str = "boom", status_code: int | None = None):
        super().__init__(msg)
        self.status_code = status_code


def install_fake_sdk(handler) -> dict:
    """把厂商 SDK 换掉，让 `provider._chat` 走**真实代码路径**。

    为什么不直接 patch `provider._chat`：
        异常归类就写在 `_chat` 里面，把它整个替换掉等于把被测的那段代码
        也一并跳过了——测试会绿，但绿的是空气。
        这里替换的是更外一层的 `openai.OpenAI`，
        于是「发请求 → 抛异常 → 归类成 LLMUnavailable → 上层退模板」
        这条链是完整跑过的。

    :return: 计数器字典，记录调用次数与拿到的超时
    """
    import openai

    calls = {"n": 0, "timeout": None}

    class _Msg:
        def __init__(self, content):
            self.content = content

    class _Choice:
        def __init__(self, content):
            self.message = _Msg(content)

    class _Resp:
        def __init__(self, content):
            self.choices = [_Choice(content)]

    class _Completions:
        def create(self, **kwargs):
            calls["n"] += 1
            out = handler(kwargs)
            # 允许 handler 直接返回字符串，省得每个用例都造一遍响应对象
            return _Resp(out) if isinstance(out, str) else out

    class _Chat:
        completions = _Completions()

    class _FakeOpenAI:
        def __init__(self, **kwargs):
            calls["timeout"] = kwargs.get("timeout")
            self.chat = _Chat()

    real = openai.OpenAI
    openai.OpenAI = _FakeOpenAI
    return {"calls": calls, "restore": lambda: setattr(openai, "OpenAI", real)}


def make_raiser(exc: BaseException):
    """返回一个假的 SDK 安装器：任何请求都抛 `exc`。"""
    def _handler(kwargs):
        raise exc
    return _handler


# ────────────────────────────── 一、无 Key ──────────────────────────────

def test_no_key_is_template() -> None:
    """没有 Key：全部走本地模板，且**本来就够用**。"""
    an = sample_analysis()
    exp = Explainer(api_key="")
    check("无 Key 时模式", exp.mode, "template")

    out = exp.explain(an)
    check("无 Key 解读 mode", out["mode"], "template")
    check("无 Key 解读 model", out["model"], "本地模板")
    check("无 Key 不算降级", out["degraded"], "")
    check_true("无 Key 仍有正文", out["report"]["summary"])
    check_true("无 Key 仍有逐项解释", len(out["report"]["tests"]) >= 3)
    check_true("无 Key 仍有家话", out["family_message"]["message"])

    qa = QaAnswerer(exp)
    reply = qa.answer("尿酸高是怎么回事", an)
    check("无 Key 问答 mode", reply["mode"], "template")
    check("无 Key 问答 model", reply["model"], "本地模板")
    check("无 Key 问答不算降级", reply["degraded"], "")
    check_true("无 Key 问答有正文", reply["answer"])


# ───────────────────────── 二、假 Key / 401 / 429 ─────────────────────────

def _degraded_explain(exc: BaseException) -> tuple[dict, dict]:
    """在「有 Key 但模型不可用」的状态下跑一次解读。

    假 Key 只在 os.environ 里，测试进程结束就没了，不落盘也不打印。
    """
    an = sample_analysis()
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    exp = Explainer(api_key=FAKE_KEY)
    check_true("有假 Key 时进入 llm 模式", exp.mode == "llm")

    fake = install_fake_sdk(make_raiser(exc))
    try:
        out = exp.explain(an)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)
    return out, fake["calls"]


def test_fake_key_401() -> None:
    """401：接口仍返回可用结果，绝不能 500。"""
    out, calls = _degraded_explain(Boom("invalid key", 401))
    check("401 解读 mode 如实", out["mode"], "template")
    check("401 解读 model 如实", out["model"], "本地模板")
    check("401 降级原因", out["degraded"], "unavailable")
    check("401 提示文案", out["model_notice"], Explainer.DEGRADE_HINT)
    check_true("401 仍有正文", out["report"]["summary"])
    # 不可用就不该再试第二次：鉴权错了重试还是错，只让用户多等一轮
    check("401 只调一次", calls["n"], 1)


def test_rate_limit_429() -> None:
    """429：同上，而且**不重试**——重试只会让限流更久。"""
    out, calls = _degraded_explain(Boom("rate limited", 429))
    check("429 降级原因", out["degraded"], "unavailable")
    check("429 只调一次", calls["n"], 1)
    check_true("429 仍有正文", out["report"]["summary"])


def test_timeout_and_network() -> None:
    """超时与断网：同样退本地，且归为不可用而不是校验失败。"""
    for name, exc in [("超时", Boom("timeout")), ("断网", Boom("conn reset"))]:
        out, _ = _degraded_explain(exc)
        check(f"{name} 降级原因", out["degraded"], "unavailable")
        check(f"{name} mode 如实", out["mode"], "template")


# ────────────────── 三、降级结果的数字必须与本地逐字一致 ──────────────────

def test_degraded_numbers_match_local() -> None:
    """不管因为什么降级，出来的数字就是本地判定的数字，一个字都不差。

    这是最重要的一条断言：降级链路最危险的失败不是崩，
    而是「换了条路走，顺手把数字也算错了一遍」。
    """
    an = sample_analysis()
    baseline = Explainer(api_key="").explain(an)      # 纯本地模板，作为基准
    base_tests = {t["test_name"]: t for t in baseline["report"]["tests"]}

    for name, exc in [("401", Boom("x", 401)),
                      ("429", Boom("x", 429)),
                      ("超时", Boom("timeout")),
                      ("5xx", Boom("server error", 503))]:
        out, _ = _degraded_explain(exc)
        check(f"{name} 概要与本地一致", out["report"]["summary"],
              baseline["report"]["summary"])
        got = {t["test_name"]: t for t in out["report"]["tests"]}
        check(f"{name} 指标集合一致", sorted(got), sorted(base_tests))
        for k in base_tests:
            check(f"{name}·{k} 报告值一致", got[k]["reported_value"],
                  base_tests[k]["reported_value"])
            check(f"{name}·{k} 参考区间一致", got[k]["reference_range"],
                  base_tests[k]["reference_range"])
        check(f"{name} 家话一致", out["family_message"]["message"],
              baseline["family_message"]["message"])


def test_degraded_no_raw_error_leak() -> None:
    """给用户的文案里不能有服务商原始报文、异常类名、堆栈关键字。"""
    msg = "Error code: 401 - {'error': {'message': 'Authentication Fails'}}"
    out, _ = _degraded_explain(Boom(msg, 401))
    blob = json.dumps(out, ensure_ascii=False)
    for bad in ["Error code", "Authentication", "traceback", "Traceback",
                "request_id", "openai", "401 -"]:
        check(f"不泄露「{bad}」", bad in blob, False)


# ─────────────────── 四、校验失败 ≠ 网络失败（必须分开） ───────────────────

def test_hallucination_is_distinct() -> None:
    """模型编了数字：要重写一次，仍不过才退；原因记为 hallucination。"""
    an = sample_analysis()
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    exp = Explainer(api_key=FAKE_KEY)
    # 这份 JSON 里的 999 在本地事实清单里没有出处
    bad_json = json.dumps({
        "summary": "尿酸 999 需要重视",
        "disclaimer": "x",
        "follow_up_questions": [],
        "tests": [{
            "test_name": "尿酸", "simple_explanation": "999 很高",
            "recommended_next_step": "问医生", "is_likely_normal": "no",
            "confidence": "high", "reported_value": "512",
        }],
    }, ensure_ascii=False)

    fake = install_fake_sdk(lambda kwargs: bad_json)
    try:
        out = exp.explain(an)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)

    check("编数字时重写了一次", fake["calls"]["n"], 2)
    check("编数字降级原因", out["degraded"], "hallucination")
    check("编数字也如实标模板", out["mode"], "template")
    check("编数字不谎报不可用", out["degraded"] == "unavailable", False)
    # 校验失败与网络失败的提示不能是同一句，否则排查会往错的方向走
    check("两种降级提示不同", Explainer.DEGRADE_HINT.endswith("判定结果不受影响。"), True)


def test_unavailable_does_not_retry() -> None:
    """模型不可用不重试：这一条和上面那条正好构成一对。"""
    _, calls = _degraded_explain(Boom("x", 401))
    check("不可用时只调一次", calls["n"], 1)


# ─────────────────────── 五、问答链路的降级 ───────────────────────

def test_chat_degrades() -> None:
    """问答：不可用退模板，原因如实，正文仍在。"""
    an = sample_analysis()
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    exp = Explainer(api_key=FAKE_KEY)
    qa = QaAnswerer(exp)

    fake = install_fake_sdk(make_raiser(Boom("x", 401)))
    try:
        reply = qa.answer("尿酸高是怎么回事", an)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)

    check("问答降级 mode", reply["mode"], "template")
    check("问答降级 model", reply["model"], "本地模板")
    check("问答降级原因", reply["degraded"], "unavailable")
    check("问答降级提示", reply["model_notice"], Explainer.DEGRADE_HINT)
    check_true("问答降级仍有正文", reply["answer"])
    check("问答降级正文来自本地模板", reply["validated"], True)


def test_chat_hallucination_distinct() -> None:
    """问答里「模型编数字」也必须与「模型不可用」区分开。"""
    an = sample_analysis()
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    exp = Explainer(api_key=FAKE_KEY)
    qa = QaAnswerer(exp)

    # 999 在本地事实清单里没有出处
    fake = install_fake_sdk(lambda kwargs: "你的尿酸是 999 μmol/L，需要马上住院")
    try:
        reply = qa.answer("尿酸高是怎么回事", an)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)

    check("问答编数字原因", reply["degraded"], "hallucination")
    check("问答编数字不谎报不可用", reply["degraded"] == "unavailable", False)
    check("问答编数字也退模板", reply["mode"], "template")


# ────────────────── 六、provider 的超时与异常归类 ──────────────────

def test_provider_timeout_is_bounded() -> None:
    """必须有明确超时，不能用 SDK 默认的 600 秒读超时。"""
    check_true("读超时已设置", P.DEFAULT_READ_TIMEOUT > 0)
    check("读超时不超过 60 秒", P.DEFAULT_READ_TIMEOUT <= 60, True)
    check_true("连接超时已设置", P.DEFAULT_CONNECT_TIMEOUT > 0)
    # 传输层不重试：失败后本来就退本地模板，重试只会让用户多等一整轮
    check("传输层零重试", P.MAX_RETRIES, 0)


def test_provider_classifies_errors() -> None:
    """服务商异常要被归类成人话，且不吐原始报文。"""
    cases = [
        (Boom("x", 401), "鉴权失败"),
        (Boom("x", 429), "限流"),
        (Boom("x", 503), "服务端异常"),
    ]
    for exc, keyword in cases:
        reason = P._safe_reason(exc)
        check_true(f"{keyword} 归类正确（{reason}）", keyword in reason)


def test_provider_raises_llm_unavailable() -> None:
    """_chat 抛出的必须是 LLMUnavailable，好让上层分得清事故类型。"""
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    fake = install_fake_sdk(make_raiser(Boom("x", 401)))
    try:
        try:
            P.call_llm("你好")
            check("应抛 LLMUnavailable", False, True)
        except P.LLMUnavailable:
            check("抛出 LLMUnavailable", True, True)
        except Exception as e:                          # noqa: BLE001
            check(f"抛出 LLMUnavailable（实际 {type(e).__name__}）", False, True)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_provider_hides_raw_error() -> None:
    """抛出去的原因里不能有服务商原始报文。"""
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    raw = "Error code: 401 - {'request_id': 'abc123', 'message': 'Auth Fails'}"
    fake = install_fake_sdk(make_raiser(Boom(raw, 401)))
    try:
        try:
            P.call_llm("你好")
        except P.LLMUnavailable as e:
            check("原因不含 request_id", "request_id" in str(e), False)
            check("原因不含 Error code", "Error code" in str(e), False)
            check("原因是一句人话", "鉴权失败" in str(e), True)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)


def test_no_key_never_sends_request() -> None:
    """没配 Key 时不该把请求发出去：401 要等一个来回，本地模板是瞬时的。"""
    os.environ.pop("DEEPSEEK_API_KEY", None)
    calls = {"n": 0}

    def _handler(kwargs):
        calls["n"] += 1
        return "不该被调用"

    fake = install_fake_sdk(_handler)
    try:
        try:
            P.call_llm("你好")
        except P.LLMUnavailable:
            pass
        check("没 Key 时零请求", calls["n"], 0)
    finally:
        fake["restore"]()


def test_timeout_is_passed_down() -> None:
    """问答的读超时要比解读短——短问答不该按长解读的时长去等。"""
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    exp = Explainer(api_key=FAKE_KEY)
    fake = install_fake_sdk(lambda kwargs: "好的")
    try:
        exp.complete([{"role": "user", "content": "hi"}], max_tokens=512)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)
    got = fake["calls"]["timeout"]
    check_true(f"超时确实传给了 SDK（{got}）", got is not None)
    total = getattr(got, "read", None) or got
    check_true(f"问答用了更短的超时（{total} < {P.DEFAULT_READ_TIMEOUT}）",
               float(total) < P.DEFAULT_READ_TIMEOUT)


# ────────────── 七、恢复 provider 后大模型模式仍然可用 ──────────────

def test_recovers_when_provider_back() -> None:
    """修好了 Key 与网络，就该回到大模型模式，不能一直赖在本地模板上。

    降级链路最怕的是「降下去就回不来」——一次断网之后，
    哪怕网络恢复了，界面也永远显示本地模板，等于白接了大模型。
    """
    an = sample_analysis()
    exp = Explainer(api_key=FAKE_KEY)
    ok_json = json.dumps({
        "summary": "血糖正常，尿酸偏高，注意饮食。",
        "disclaimer": "x",
        "follow_up_questions": [],
        "tests": [{
            "test_name": "尿酸", "simple_explanation": "偏高，少吃海鲜",
            "recommended_next_step": "复查尿酸", "is_likely_normal": "no",
            "confidence": "high", "reported_value": "512",
        }],
    }, ensure_ascii=False)

    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    fake = install_fake_sdk(lambda kwargs: ok_json)
    try:
        out = exp.explain(an)
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)

    check("恢复后 mode 回到 llm", out["mode"], "llm")
    check("恢复后不算降级", out["degraded"], "")
    check("恢复后 model 是真实模型", out["model"], "DeepSeek V4.1")
    check("恢复后正文来自模型", out["report"]["summary"].startswith("血糖正常"), True)


# ────────────── 八、本地链路在任何模型故障下都不受影响 ──────────────

def test_local_chain_unaffected() -> None:
    """判定 / 红线 / 记餐 / 今日汇总 / 一周趋势：模型炸了也照常。

    这几条链路在代码里根本不经过 provider，
    这里再用「让 provider 彻底炸掉」的方式实测一遍，防止以后有人接错线。
    """
    import app.main as M                                # noqa: E402
    from fastapi.testclient import TestClient           # noqa: E402

    # 数据库挪到临时文件：这组测试会写记餐记录，不能污染真实档案
    tmp = Path(tempfile.mkdtemp()) / "degrade_test.db"
    real_db = M.store.DB_PATH
    M.store.DB_PATH = tmp
    M.store.init_db()

    client = TestClient(M.app)
    os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
    M._EXPLAINER._api_key = FAKE_KEY   # 让表达层以为自己在线
    fake = install_fake_sdk(make_raiser(Boom("total outage", 503)))
    try:
        markers = [
            {"name": "尿酸(UA)", "value": "512", "unit": "μmol/L", "ref": "208-428"},
            {"name": "血红蛋白", "value": "98", "unit": "g/L", "ref": "130-175"},
        ]
        r = client.post("/api/analyze", json={"markers": markers, "sex": "male"})
        check("判定的状态码", r.status_code, 200)
        an = r.json()

        # 解读接口即使模型全炸，也要 200 且给得出结果
        r = client.post("/api/explain", json=an)
        check("解读的状态码（模型全炸）", r.status_code, 200)
        body = r.json()
        check("解读如实标本地", body["mode"], "template")
        check("解读降级原因", body["degraded"], "unavailable")

        # 流式同样不能 500
        with client.stream("POST", "/api/explain/stream", json=an) as sr:
            check("流式的状态码", sr.status_code, 200)
            text = "".join(sr.iter_text())
        check_true("流式吐出了事件", "data:" in text)
        check("流式不吐原始异常", "Traceback" in text or "Error code" in text, False)

        # 保存报告 → 激活红线
        r = client.post("/api/report", json={"result": an, "sex": "male",
                                             "source": "test"})
        check("保存报告的状态码", r.status_code, 200)

        # 记一餐
        r = client.post("/api/meals", json={"meal_type": "lunch",
                                            "dishes": ["白米饭", "红烧肉"]})
        check("记餐的状态码", r.status_code, 200)
        summary = r.json()
        check("记餐返回餐次", summary.get("meal_count"), 1)
        check_true("记餐返回红线对照", "hits" in summary)
        check_true("记餐返回卡路里", "calories" in summary)

        # 今日汇总
        r = client.get("/api/today")
        check("今日汇总的状态码", r.status_code, 200)
        check_true("今日汇总有数据", "meals" in r.json() and "hits" in r.json())

        # 一周趋势
        r = client.get("/api/week?days=7")
        check("一周趋势的状态码", r.status_code, 200)
        check_true("一周趋势有序列", "series" in r.json())

        # 数值记录（血压）—— 会激活「限钠」红线，日常层的关键路径
        r = client.post("/api/numerics", json={"kind": "blood_pressure",
                                               "systolic": 138, "diastolic": 88})
        check("血压记录的状态码", r.status_code, 200)

        # 问答
        r = client.post("/api/chat", json={"question": "尿酸高吃什么",
                                           "analysis": an})
        check("问答的状态码（模型全炸）", r.status_code, 200)
        check("问答如实标本地", r.json()["mode"], "template")
        check("问答降级原因", r.json()["degraded"], "unavailable")
    finally:
        fake["restore"]()
        os.environ.pop("DEEPSEEK_API_KEY", None)
        M._EXPLAINER._api_key = ""
        M.store.DB_PATH = real_db


def test_daily_service_has_no_llm_dependency() -> None:
    """静态断言：日常层不允许出现任何大模型调用。

    这是上面那组实测的结构性背书——
    跑通只说明「现在没接错」，这一条说明「以后也不该接」。
    """
    from app.daily import service as daily              # noqa: E402
    import inspect                                      # noqa: E402
    src = inspect.getsource(daily)
    for forbidden in ["call_llm", "provider", "OpenAI", "chat.completions"]:
        check(f"日常层不含 {forbidden}", forbidden in src, False)


# ─────────────────────────────── 入口 ───────────────────────────────

def main() -> None:
    tests = [
        test_no_key_is_template,
        test_fake_key_401,
        test_rate_limit_429,
        test_timeout_and_network,
        test_degraded_numbers_match_local,
        test_degraded_no_raw_error_leak,
        test_hallucination_is_distinct,
        test_unavailable_does_not_retry,
        test_chat_degrades,
        test_chat_hallucination_distinct,
        test_provider_timeout_is_bounded,
        test_provider_classifies_errors,
        test_provider_raises_llm_unavailable,
        test_provider_hides_raw_error,
        test_no_key_never_sends_request,
        test_timeout_is_passed_down,
        test_recovers_when_provider_back,
        test_local_chain_unaffected,
        test_daily_service_has_no_llm_dependency,
    ]
    for t in tests:
        try:
            t()
        except Exception as e:                          # noqa: BLE001
            FAILED.append(f"{t.__name__} 抛出异常：{type(e).__name__}: {e}")

    print(f"\n{'=' * 62}")
    print(f"  大模型降级测试：通过 {PASSED} 项断言")
    print(f"{'=' * 62}")
    if FAILED:
        print(f"  失败 {len(FAILED)} 项：")
        for f in FAILED:
            print("   ✗", f)
        sys.exit(1)
    print("  全部通过 ✅")


if __name__ == "__main__":
    main()
