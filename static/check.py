"""前端静态自查：HTML 标签闭合 + JS 选择器是否都真的存在于 HTML。

不启动浏览器也能抓到两类低级错误：
    1. 标签写错闭合（把 </button> 写成 </div> 这种）
    2. JS 里 $('#xxx') 引用的 id 在 HTML 里根本不存在
"""

from __future__ import annotations

import re
import sys
from html.parser import HTMLParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
HTML = (HERE / "index.html").read_text(encoding="utf-8")
JS = (HERE / "app.js").read_text(encoding="utf-8")

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "source", "track", "wbr"}


class Checker(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.stack: list[tuple[str, tuple[int, int]]] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag not in VOID:
            self.stack.append((tag, self.getpos()))

    def handle_endtag(self, tag: str) -> None:
        if not self.stack:
            self.errors.append(f"多余的 </{tag}> @ {self.getpos()}")
            return
        if self.stack[-1][0] == tag:
            self.stack.pop()
            return
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                unclosed = [t for t, _ in self.stack[i + 1:]]
                self.errors.append(
                    f"</{tag}> @ {self.getpos()} 关闭时内层未闭合: {unclosed}")
                del self.stack[i:]
                return
        self.errors.append(f"意外的 </{tag}> @ {self.getpos()}")


def main() -> int:
    checker = Checker()
    checker.feed(HTML)
    if checker.stack:
        checker.errors.append("文件末尾仍未闭合: "
                             + str([t for t, _ in checker.stack]))

    print("── HTML 标签闭合 ──")
    if checker.errors:
        for e in checker.errors:
            print("   ✗", e)
    else:
        print("   通过 ✅")

    # JS 选择器 → HTML id 交叉核对
    ids = set(re.findall(r'id="([^"]+)"', HTML))
    used_in_js = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)", JS))
    # 由 JS 模板动态生成、或由字符串拼接得到的 id，HTML 里没有静态节点，不算缺失
    js_generated = {"raw-extra", "view-"}
    missing = used_in_js - ids - js_generated

    print("\n── JS 选择器核对 ──")
    print(f"   HTML id {len(ids)} 个 | JS 引用 {len(used_in_js)} 个")
    if missing:
        for m in sorted(missing):
            print(f"   ✗ JS 引用了不存在的 id: #{m}")
    else:
        print("   全部存在 ✅")

    # 检查 JS 里依赖的静态 class（dish-chip 由 JS 模板生成，不在此列）
    for cls in ("stage", "seg", "num-row"):
        if cls in JS and cls not in HTML:
            print(f"   ✗ JS 引用 class .{cls}，但 HTML 中没有")
            return 1

    return 1 if (checker.errors or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
