"""历史对比测试：趋势判定口径

================================================================================
对比的口径必须可解释——好转/加重/持续/反转/新增五种判定各测到位，
尤其是"同向异常=持续"与"反向异常=反转"这两个容易被顺手写错的分支。
================================================================================
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.history import compare                        # noqa: E402
from app.data import db as store                       # noqa: E402
import tempfile                                        # noqa: E402

# 必须在打临时库补丁**之前**存下真实路径——防回归断言要检查的就是它
_REAL_DB_PATH = store.DB_PATH

# 测试用独立数据库，不碰开发库
_tmp = tempfile.mkdtemp(prefix="tjzh_hist_")
store.DB_PATH = Path(_tmp) / "test.db"
store.init_db()

PASSED = 0
FAILED: list[str] = []


def check(name: str, got, want) -> None:
    global PASSED
    if got == want:
        PASSED += 1
    else:
        FAILED.append(f"{name}: 期望 {want!r}，实际 {got!r}")


def J(name, flag, value="100") -> dict:
    return {"marker_id": name, "standard_name": name, "flag": flag,
            "reported_value": value, "reason": "test"}


def test_verdicts() -> None:
    old = {"judged": [
        J("尿酸", "high", "512"),
        J("空腹血糖", "normal", "5.4"),
        J("总胆固醇", "high", "6.1"),
        J("血红蛋白", "low", "95"),
        J("白细胞", "high", "12"),
    ]}
    new = {"judged": [
        J("尿酸", "normal", "350"),        # 好转
        J("空腹血糖", "high", "7.2"),      # 加重
        J("总胆固醇", "high", "5.9"),      # 持续
        J("血红蛋白", "high", "180"),      # 反转（低→高）
        J("甘油三酯", "high", "2.8"),      # 新增（异常）
        J("新指标", "normal", "10"),       # 新增（正常，不进 rows）
    ]}
    diff = compare(old, new)

    c = diff["counts"]
    check("好转数", c["improved"], 1)
    check("加载数", c["worsened"], 1)
    check("持续数", c["persistent"], 1)
    check("反转数", c["reversed"], 1)
    check("新增数（含正常）", c["new"], 2)
    check("两次正常的项", c["normal"], 0)   # 样例里没有两次都正常的

    by_name = {r["standard_name"]: r for r in diff["rows"]}
    check("好转行存在", by_name["尿酸"]["verdict"], "improved")
    check("好转保留旧值", by_name["尿酸"]["from_value"], "512")
    check("反转行存在", by_name["血红蛋白"]["verdict"], "reversed")
    check("持续行存在", by_name["总胆固醇"]["verdict"], "persistent")
    check("新增异常行存在", by_name["甘油三酯"]["verdict"], "new")
    check("新增正常项不进 rows", "新指标" in by_name, False)
    check("消失项记录", diff["unmatched_before"], ["白细胞"])


def test_save_returns_compare() -> None:
    """保存新报告时应返回与上一份的对比。"""
    first = {"judged": [J("尿酸", "high", "512")], "redlines": [],
             "counts": {"matched": 1, "abnormal_count": 1}}
    second = {"judged": [J("尿酸", "normal", "350")], "redlines": [],
              "counts": {"matched": 1, "abnormal_count": 0}}

    r1 = store.save_report(first)
    check("首份保存返回 id", r1 > 0, True)

    # 第二次保存前先取上一份（main.py 的时序），对比应在保存前完成
    prev = store.latest_report()
    diff = compare(prev, second)
    check("对比判定为好转", diff["counts"]["improved"], 1)
    store.save_report(second)
    check("历史列表有两条", len(store.list_reports()), 2)
    check("历史按新到旧", store.list_reports()[0]["id"] > store.list_reports()[1]["id"], True)
    check("历史含异常计数", store.list_reports()[1]["abnormal_count"], 1)


def test_db_path_guard() -> None:
    """防回归：db.py 曾把库文件建到 app/data/ 里（parent 数错一层）。

    那个 bug 的表象是"删库重启后数据还在"，排查了一圈进程句柄，
    真因是路径算错。用断言把正确位置钉死。
    """
    p = Path(_REAL_DB_PATH).resolve()
    check("库文件在仓库根 data/ 下", p.parent.name, "data")
    check("data/ 直属仓库根", p.parent.parent.name, "tijian-zhihou")


def main() -> int:
    print("=== 判定口径 ===")
    test_verdicts()
    print("=== 保存时序 ===")
    test_save_returns_compare()
    print("=== 路径防回归 ===")
    test_db_path_guard()

    print(f"\n通过 {PASSED} 项断言")
    if FAILED:
        print(f"\n失败 {len(FAILED)} 项：")
        for f in FAILED:
            print(f"   ✗ {f}")
        return 1
    print("历史对比测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
