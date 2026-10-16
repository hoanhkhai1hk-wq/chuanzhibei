"""
合成中文体检报告生成器（用于 OCR 评测）

================================================================================
为什么不用网上的现成数据集
================================================================================
全网搜过一轮，结论是没有一个能同时满足「中文 + 免费 + 可商用 + 已脱敏」：

| 数据集 | 规模 | License | 问题 |
|---|---|---|---|
| 数据堂 6087 张体检报告 | 6087 中文 | 商用付费 | 要钱 |
| 天池/启智 中文化验单 | 238 中文 | CC-BY-**NC** | 非商业许可，且是真实单据 |
| ClinOCR-Bench | 384 | MIT | 合规但**英文** |
| MedOCR-Vision | 2462 | MIT | 英文 |

所以改为自己合成。合成的好处很实在：

1. **中文**，且排版可控（单栏 / 双栏 / 有框线 / 无线）
2. **完全合规**——没有用任何真实患者数据
3. **ground-truth 100% 精确**——图是我们画的，答案就是我们写进去的那些数
4. **能模拟拍照退化**——旋转、模糊、噪点、明暗不均，这正是真实场景最难的部分

================================================================================
用法
================================================================================
    python synth.py                      # 生成默认的 12 张（4 种排版 × 3 份）
    python synth.py --count 20           # 生成 20 张
    python synth.py --degraded 6         # 其中 6 张做拍照退化
    python synth.py --clean              # 先清空旧样本

生成物：
    samples/synth_001.png ...            图片（不入库）
    expected/synth_001.json ...          标注（入库，因为是自己造的，无隐私）

然后直接跑：
    python bench.py --provider paddle
"""

from __future__ import annotations

import argparse
import json
import random
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = Path(__file__).parent
SAMPLES = HERE / "samples"
EXPECTED = HERE / "expected"

FONT_CANDIDATES = [
    "C:/Windows/Fonts/simhei.ttf",   # 黑体，接近报告正文
    "C:/Windows/Fonts/msyh.ttc",     # 微软雅黑
    "C:/Windows/Fonts/simsun.ttc",   # 宋体
    "C:/Windows/Fonts/simkai.ttf",   # 楷体
]

#: 页面宽度。加宽到 820 是为了让「高密度脂蛋白(HDL-C)」这类长名称不被框线切断。
PAGE_W = 820
ROW_H = 34
TABLE_TOP = 150

# ==============================================================================
# 常见体检指标（参考区间取自公开科普资料，仅用于合成测试数据）
# ==============================================================================

MARKERS = [
    ("白细胞计数", "WBC", "×10⁹/L", (3.5, 9.5)),
    ("红细胞计数", "RBC", "×10¹²/L", (3.8, 5.1)),
    ("血红蛋白", "Hb", "g/L", (115, 150)),
    ("血小板计数", "PLT", "×10⁹/L", (125, 350)),
    ("谷丙转氨酶", "ALT", "U/L", (9, 50)),
    ("谷草转氨酶", "AST", "U/L", (15, 40)),
    ("总胆红素", "TBIL", "μmol/L", (3.4, 20.5)),
    ("尿素氮", "BUN", "mmol/L", (2.9, 8.2)),
    ("肌酐", "Cr", "μmol/L", (44, 133)),
    ("尿酸", "UA", "μmol/L", (208, 428)),
    ("空腹血糖", "GLU", "mmol/L", (3.9, 6.1)),
    ("总胆固醇", "TC", "mmol/L", (3.1, 5.2)),
    ("甘油三酯", "TG", "mmol/L", (0.45, 1.70)),
    ("高密度脂蛋白", "HDL-C", "mmol/L", (1.04, 1.55)),
    ("低密度脂蛋白", "LDL-C", "mmol/L", (1.50, 3.40)),
]

HOSPITALS = ["示例医院", "演示体检中心", "测试医疗机构"]
DEPARTMENTS = ["检验科", "体检中心"]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for p in FONT_CANDIDATES:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ------------------------------------------------------------------------------
# 上标单位：必须手动绘制，不能直接写 Unicode 上标字符
# ------------------------------------------------------------------------------
# 踩过的坑：×10⁹/L 里的上标 ⁹，本机所有中文字体都缺这个字形——
#   simhei 渲染成方框 □，simsun 渲染成空格。
# 如果放着不管，所有 OCR 引擎都会在这里失败，评测结果就失去意义。
# 所以手动把上标缩小字号、上移绘制，这也正是真实报告的排版方式。

_SUP = {"⁰": "0", "¹": "1", "²": "2", "³": "3", "⁴": "4",
        "⁵": "5", "⁶": "6", "⁷": "7", "⁸": "8", "⁹": "9"}
_SUP_RE = re.compile(r"^(.*?10)([⁰¹²³⁴⁵⁶⁷⁸⁹]+)(.*)$")

#: 字体池，由 generate() 填充，避免每个绘制函数都传一堆字体参数
F: dict[str, ImageFont.FreeTypeFont] = {}


def split_unit(unit: str) -> tuple[str, str, str]:
    """把 "×10⁹/L" 拆成 ("×10", "9", "/L")；不含上标的原样返回。"""
    m = _SUP_RE.match(unit)
    if not m:
        return unit, "", ""
    pre, sup, post = m.groups()
    return pre, "".join(_SUP.get(c, c) for c in sup), post


def draw_unit(d: ImageDraw.ImageDraw, xy: tuple[int, int], unit: str,
              fill: str = "black") -> None:
    """带正确上标的单位文字。"""
    pre, sup, post = split_unit(unit)
    x, y = xy
    if not sup:
        d.text((x, y), unit, fill=fill, font=F["body"])
        return
    d.text((x, y), pre, fill=fill, font=F["body"])
    x += d.textlength(pre, font=F["body"])
    d.text((x, y - 7), sup, fill=fill, font=F["sup"])
    x += d.textlength(sup, font=F["sup"])
    d.text((x, y), post, fill=fill, font=F["body"])


def unit_width(d: ImageDraw.ImageDraw, unit: str) -> float:
    pre, sup, post = split_unit(unit)
    if not sup:
        return d.textlength(unit, font=F["body"])
    return (d.textlength(pre, font=F["body"]) + d.textlength(sup, font=F["sup"])
            + d.textlength(post, font=F["body"]))


def _fmt(v: float, span: tuple[float, float]) -> str:
    """按指标量级决定小数位，尽量贴近真实报告的写法。"""
    hi = span[1]
    if hi >= 100:
        return str(int(round(v)))
    if hi >= 10:
        return f"{v:.1f}"
    return f"{v:.2f}"


def pick_value(span: tuple[float, float], rng: random.Random) -> tuple[str, str]:
    """随机取值。约 30% 概率造一个异常值——评测要覆盖异常项与箭头。"""
    lo, hi = span
    if rng.random() < 0.3:
        if rng.random() < 0.6:
            v, flag = hi * rng.uniform(1.05, 1.45), "↑"
        else:
            v, flag = lo * rng.uniform(0.55, 0.94), "↓"
    else:
        v, flag = rng.uniform(lo + (hi - lo) * 0.15, lo + (hi - lo) * 0.85), ""
    return _fmt(v, span), flag


def build_rows(rng: random.Random, n: int) -> list[dict]:
    rows = []
    for name, abbr, unit, span in rng.sample(MARKERS, k=min(n, len(MARKERS))):
        val, flag = pick_value(span, rng)
        rows.append({
            "name": f"{name}({abbr})",
            "value": val,
            "unit": unit,
            "ref": f"{_fmt(span[0], span)}-{_fmt(span[1], span)}",
            "flag": flag,
        })
    return rows


# ==============================================================================
# 四种排版
# ==============================================================================


def draw_boxed(d: ImageDraw.ImageDraw, rows: list[dict], y0: int) -> None:
    """模板 A：标准有框线表格。"""
    cols = [40, 300, 460, 580, PAGE_W - 40]
    d.rectangle([40, y0, PAGE_W - 40, y0 + ROW_H], outline="black", width=2)
    for i, h in enumerate(["项目", "结果", "单位", "参考范围"]):
        d.text((cols[i] + 8, y0 + 6), h, fill="black", font=F["body"])
    y = y0 + ROW_H
    for r in rows:
        d.rectangle([40, y, PAGE_W - 40, y + ROW_H], outline="black", width=1)
        for c in cols[1:4]:
            d.line([c, y, c, y + ROW_H], fill="black")
        d.text((cols[0] + 8, y + 6), r["name"], fill="black", font=F["body"])
        d.text((cols[1] + 8, y + 6), r["value"] + r["flag"], fill="black", font=F["body"])
        draw_unit(d, (cols[2] + 8, y + 6), r["unit"])
        d.text((cols[3] + 8, y + 6), r["ref"], fill="black", font=F["body"])
        y += ROW_H


def draw_no_line(d: ImageDraw.ImageDraw, rows: list[dict], y0: int) -> None:
    """模板 B：无线表，靠空格对齐——这类最容易让 OCR 串行。"""
    d.text((40, y0), "项目                结果          单位        参考范围",
           fill="black", font=F["body"])
    y = y0 + ROW_H + 2
    for r in rows:
        d.text((40, y), r["name"], fill="black", font=F["body"])
        d.text((300, y), r["value"] + r["flag"], fill="black", font=F["body"])
        draw_unit(d, (440, y), r["unit"])
        d.text((600, y), r["ref"], fill="black", font=F["body"])
        y += ROW_H - 2


def draw_two_col(d: ImageDraw.ImageDraw, rows: list[dict], y0: int) -> None:
    """模板 C：双栏，一页放两组——考验 OCR 的版面分析能力。"""
    for side in (0, 1):
        x0 = 40 + side * 400
        d.text((x0, y0), "项目            结果        参考范围", fill="black", font=F["body"])
        y = y0 + ROW_H
        for r in rows[side::2]:
            d.text((x0, y), r["name"], fill="black", font=F["body"])
            d.text((x0 + 250, y), r["value"] + r["flag"], fill="black", font=F["body"])
            d.text((x0 + 320, y), r["ref"], fill="black", font=F["body"])
            y += ROW_H - 2


def draw_with_header(d: ImageDraw.ImageDraw, rows: list[dict], y0: int,
                     rng: random.Random) -> None:
    """模板 D：带机构抬头与患者信息栏——最贴近真实报告。"""
    d.text((40, 40), rng.choice(HOSPITALS) + " 体检报告", fill="black", font=F["title"])
    d.text((40, 82), f"科室：{rng.choice(DEPARTMENTS)}    样本类型：血清",
           fill="black", font=F["body"])
    d.line([40, 116, PAGE_W - 40, 116], fill="black", width=2)
    draw_boxed(d, rows, y0)


TEMPLATES = {
    "boxed": draw_boxed,
    "noline": draw_no_line,
    "twocol": draw_two_col,
    "header": draw_with_header,
}


# ==============================================================================
# 拍照退化
# ==============================================================================


def degrade(img: Image.Image, rng: random.Random) -> Image.Image:
    """模拟手机拍照：倾斜、失焦、噪点、明暗不均。"""
    img = img.rotate(rng.uniform(-4.0, 4.0), resample=Image.BICUBIC,
                     expand=False, fillcolor="white")
    if rng.random() < 0.7:
        img = img.filter(ImageFilter.GaussianBlur(radius=rng.uniform(0.4, 1.1)))
    px = img.load()
    w, h = img.size
    for _ in range(int(w * h * 0.012)):
        x, y = rng.randrange(w), rng.randrange(h)
        n = rng.randint(-26, 26)
        px[x, y] = tuple(max(0, min(255, c + n)) for c in px[x, y])
    shade = Image.linear_gradient("L").resize((w, h))
    shade = shade.point(lambda p: int(255 - (255 - p) * rng.uniform(0.05, 0.22)))
    img = Image.merge("RGB", [Image.composite(ch, Image.new("L", (w, h), 255), shade)
                              for ch in img.split()])
    return img


# ==============================================================================
# 主流程
# ==============================================================================


def make_one(seed: int = 7, degraded: bool = False,
             out_dir: Path | None = None) -> Path:
    """生成**一份**合成报告到指定目录，返回图片路径。

    为什么单独提供这个入口：产品需要一个「一键演示」按钮，
    让没有真实报告照片的人也能立刻看到完整链路的效果。
    演示数据由本生成器现场产出、用完即删，不占用仓库体积，
    同时把「合成数据 harness」本身变成产品可展示的一部分。

    out_dir 默认为 SAMPLES，与 generate() 的行为保持一致。
    """
    global F
    target = Path(out_dir) if out_dir else SAMPLES
    target.mkdir(parents=True, exist_ok=True)

    rng = random.Random(seed)
    F["body"] = load_font(22)
    F["sup"] = load_font(15)
    F["title"] = load_font(30)

    # 演示只用三种已验证版式。twocol（双栏）是评测报告里如实记录的已知短板，
    # 演示入口不碰它——短板归评测报告，不归产品体验。
    allowed = [name for name in TEMPLATES if name != "twocol"] or list(TEMPLATES)
    tpl = allowed[seed % len(allowed)]
    rows = build_rows(rng, rng.randint(10, 14))
    H = TABLE_TOP + ROW_H * (len(rows) + 1) + 80
    img = Image.new("RGB", (PAGE_W, H), "white")
    d = ImageDraw.Draw(img)
    if tpl == "header":
        TEMPLATES[tpl](d, rows, TABLE_TOP, rng)
    else:
        TEMPLATES[tpl](d, rows, TABLE_TOP)
    if degraded:
        img = degrade(img, rng)

    path = target / f"demo_{seed}.png"
    img.save(path)
    return path


def generate(seed: int, count: int, degraded: int, clean: bool) -> int:
    rng = random.Random(seed)
    SAMPLES.mkdir(parents=True, exist_ok=True)
    EXPECTED.mkdir(parents=True, exist_ok=True)

    if clean:
        for p in list(SAMPLES.glob("synth_*")) + list(EXPECTED.glob("synth_*.json")):
            p.unlink()

    F["body"] = load_font(22)
    F["sup"] = load_font(15)
    F["title"] = load_font(30)

    names = list(TEMPLATES)
    made = []
    for i in range(count):
        tpl = names[i % len(names)]
        rows = build_rows(rng, rng.randint(10, 15))
        H = TABLE_TOP + ROW_H * (len(rows) + 1) + 80
        img = Image.new("RGB", (PAGE_W, H), "white")
        d = ImageDraw.Draw(img)
        if tpl == "header":
            TEMPLATES[tpl](d, rows, TABLE_TOP, rng)
        else:
            TEMPLATES[tpl](d, rows, TABLE_TOP)

        is_deg = i >= count - degraded
        if is_deg:
            img = degrade(img, rng)

        stem = f"synth_{i + 1:03d}"
        img.save(SAMPLES / f"{stem}.png")
        (EXPECTED / f"{stem}.json").write_text(
            json.dumps({
                "source": f"合成样本，排版={tpl}，{'已做拍照退化' if is_deg else '清晰件'}",
                "markers": [{"name": r["name"], "value": r["value"],
                             "unit": r["unit"], "ref": r["ref"]} for r in rows],
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        made.append((stem, tpl, is_deg))

    print(f"已生成 {len(made)} 份合成报告：")
    for stem, tpl, deg in made:
        print(f"   {stem}  排版={tpl:8s} {'拍照退化' if deg else '清晰'}")
    print(f"\n图片 -> {SAMPLES}")
    print(f"标注 -> {EXPECTED}")
    print(f"其中退化样本 {degraded} 份（编号最大的那批）")
    print("\n下一步：python bench.py --provider paddle")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="合成中文体检报告")
    ap.add_argument("--count", type=int, default=12, help="生成份数")
    ap.add_argument("--degraded", type=int, default=4, help="其中做拍照退化的份数")
    ap.add_argument("--seed", type=int, default=20261016)
    ap.add_argument("--clean", action="store_true", help="先清空旧的合成样本")
    a = ap.parse_args()
    return generate(a.seed, a.count, a.degraded, a.clean)


if __name__ == "__main__":
    raise SystemExit(main())
