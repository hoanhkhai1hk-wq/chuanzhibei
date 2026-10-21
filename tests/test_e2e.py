"""
端到端链路测试

不依赖 pytest，直接运行：
    python tests/test_e2e.py

覆盖：
    图片 → OCR → 脱敏 → 解析 → 本地判定 → 红线

为什么单独测：
    各模块单测通过不代表串起来能跑。端到端是「Day 15 丑链路」的验收标准，
    也是演示时真正要展示的那条线。

注意：本测试需要 rapidocr，且会跑 12 份样本，耗时约 30-60 秒。

关于样本从哪来：
    12 份合成报告图放在 tests/ocr_bench/samples/，而该目录被 .gitignore 排除
    （本意是拦住真实体检报告图，合成图也被一并挡住了）。
    所以全新 clone 之后直接跑本文件**不需要任何手动前置步骤**——
    检测到 synth_*.png 缺失时，会现场调用 tests/ocr_bench/synth.py 的生成逻辑补齐。
    生成是确定性的（固定 seed），补出来的图与仓库里 expected/*.json 的标注一一对应。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from app.pipeline import Pipeline, redact  # noqa: E402

SAMPLES = ROOT / "tests" / "ocr_bench" / "samples"
EXPECTED = ROOT / "tests" / "ocr_bench" / "expected"
SYNTH_PY = ROOT / "tests" / "ocr_bench" / "synth.py"

#: 与 tests/ocr_bench/synth.py 的默认值保持一致；换 seed 会让图与标注对不上
SYNTH_SEED = 20261016
SYNTH_DEGRADED = 4

FAILED: list[str] = []
PASSED = 0


def _load_synth():
    """按文件路径加载 synth.py。

    tests/ocr_bench 不是包（没有 __init__.py），所以不能 `import tests.ocr_bench.synth`。
    用 spec_from_file_location 加载，避免为此改动目录结构。
    """
    spec = importlib.util.spec_from_file_location("ocr_bench_synth", SYNTH_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def ensure_synth_samples() -> None:
    """确保 samples/ 下的合成图齐备；缺了就现场生成。

    为什么放在测试里而不是写成文档步骤：
        "先跑一个命令再跑测试"这种前置步骤一定会被跳过——评委、CI、未来的自己都会跳。
        把补齐动作放进测试本身，才是真正的"可复现"。
    """
    expected = sorted(EXPECTED.glob("synth_*.json"))
    have = {p.stem for p in SAMPLES.glob("synth_*.png")}
    missing = [f.stem for f in expected if f.stem not in have]

    if not missing:
        return

    if not SYNTH_PY.exists():
        print(f"   [警告] 缺少 {len(missing)} 张合成样本图，且找不到生成器 {SYNTH_PY}")
        return

    print(f"   [准备] samples/ 下缺 {len(missing)} 张合成图"
          f"（该目录被 .gitignore 排除，合成图不会进仓库）")
    print(f"   [准备] 正在调用 tests/ocr_bench/synth.py 现场生成，约需数秒…")
    synth = _load_synth()
    # expected/*.json 已随仓库提供，是评测的 ground truth，这里只补图、不覆盖标注
    synth.generate(SYNTH_SEED, len(expected) or 12, SYNTH_DEGRADED,
                   clean=False, write_expected=not expected)
    print()


def check(label: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{label}: 得到 {got!r}，期望 {want!r}")


def ok(cond: bool, label: str, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
    else:
        FAILED.append(f"{label}{(' —— ' + detail) if detail else ''}")


def test_redact() -> None:
    """脱敏是隐私底线，必须单独验证。"""
    text = "\n".join([
        "姓名：张三    性别：男    年龄：45",
        "体检号：20261016001234",
        "身份证：110101199001011234",
        "联系电话：13812345678",
        "尿酸(UA)    512    μmol/L    208-428",
    ])
    clean, fields = redact(text)

    ok("姓名" not in clean, "脱敏应移除姓名行")
    ok("张三" not in clean, "脱敏应移除姓名内容")
    ok("110101199001011234" not in clean, "脱敏应移除身份证号")
    ok("13812345678" not in clean, "脱敏应移除手机号")
    ok("20261016001234" not in clean, "脱敏应移除体检号")
    # 身份证那行因为含「身份证」三字，会被整行抹除而非字段替换——
    # 整行抹除更彻底，属预期行为，所以两种记录方式都应接受。
    ok(("id_card" in fields) or ("info_line" in fields),
       "应记录身份证相关脱敏（整行抹除或字段替换均可）", str(fields))
    ok("尿酸" in clean, "脱敏不应误伤指标行")
    ok("512" in clean, "脱敏不应误伤数值")


def test_pipeline_on_samples() -> None:
    """在合成样本上跑完整链路。"""
    p = Pipeline()
    total_rows = 0
    total_matched = 0
    total_redlines = 0
    ran = 0

    for exp_f in sorted(EXPECTED.glob("synth_*.json")):
        stem = exp_f.stem
        img = SAMPLES / f"{stem}.png"
        if not img.exists():
            FAILED.append(f"缺少样本图片 {img}")
            continue
        try:
            res = p.analyze(img, sex="male")
        except Exception as e:  # 任何一份跑挂都算失败
            FAILED.append(f"{stem} 链路异常: {e}")
            continue
        ran += 1

        c = res["counts"]
        total_rows += c["total_rows"]
        total_matched += c["matched"]
        total_redlines += len(res["redlines"])

        # 结构完整性
        ok(res["privacy"]["is_clean"], f"{stem} 应已完成脱敏")
        ok("disclaimer" in res and bool(res["disclaimer"]), f"{stem} 必须带免责声明")
        ok(isinstance(res["judged"], list), f"{stem} judged 应为列表")

        # 每条判定都要有依据，否则无法向用户解释
        for j in res["judged"]:
            if j["flag"] != "unknown":
                ok(bool(j.get("reason")),
                   f"{stem}/{j.get('standard_name')} 判定必须附依据")

        # 汇总文案要能读
        s = p.summary_text(res)
        ok(len(s) > 20, f"{stem} 汇总文案不应为空")
        ok("请遵医嘱" in s, f"{stem} 汇总文案必须含遵医嘱提示")

    print(f"   跑通 {ran} 份，识别 {total_rows} 行，命中 {total_matched} 行，红线 {total_redlines} 条")

    ok(ran >= 10, "至少应跑通 10 份样本", f"实际 {ran}")

    if total_rows:
        hit = total_matched / total_rows
        ok(hit >= 0.95, "字典命中率应 ≥95%", f"实际 {hit:.0%}")

    ok(total_redlines >= 5, "12 份样本应触发至少 5 条红线", f"实际 {total_redlines}")


def main() -> int:
    print("=== 脱敏测试 ===")
    test_redact()
    print("=== 端到端链路测试（12 份合成样本）===")
    ensure_synth_samples()
    test_pipeline_on_samples()

    print()
    print(f"通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED[:20]:
            print(f"   ✗ {f}")
        return 1
    print("端到端测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
