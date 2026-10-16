"""SQLite 落库层（stdlib sqlite3，零 ORM 依赖）

================================================================================
为什么不用 SQLAlchemy
================================================================================
本项目的数据形态就是「一份 JSON 快照 + 一组流水记录」，
引入 ORM 带来的迁移成本高于它省下的 SQL。
sqlite3 是标准库，WAL 模式下单文件够用，
而且评委复现时不需要额外装任何东西。

================================================================================
表设计
================================================================================
    reports         档案层快照：一次完整解读的判定结果原样存 JSON
    active_redlines 当前生效的红线：来自最近一次报告（日常对照的依据）
    meals           日常层流水：一次饮食记录（点选菜品）
    numerics        日常层流水：血压 / 血糖 / 体重

member_id 目前固定为 "me"（单用户）。家人档案是 P2 功能，
届时加 members 表即可，现有表都预留了 member_id 列，不用迁移。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

# 本文件在 app/data/ 下，深两层：parent.parent 只到 app/，
# 必须再往上一层才是仓库根。这个 bug 曾让库文件悄悄建在 app/data/ 里——
# 表面上"删库重启"了，实际数据一直都在，排查了一圈进程句柄才找到。
REPO = Path(__file__).resolve().parents[2]
DATA_DIR = REPO / "data"
DB_PATH = DATA_DIR / "tjzh.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    source      TEXT,
    sex         TEXT,
    result_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active_redlines (
    member_id     TEXT NOT NULL,
    rule_id       TEXT NOT NULL,
    restriction   TEXT NOT NULL,
    marker        TEXT,
    nutrient_key  TEXT NOT NULL,
    action        TEXT NOT NULL,
    trigger_foods TEXT NOT NULL DEFAULT '[]',
    advice        TEXT,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (member_id, rule_id)
);
CREATE TABLE IF NOT EXISTS meals (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  TEXT NOT NULL,
    date       TEXT NOT NULL,
    meal_type  TEXT NOT NULL,
    dishes     TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS numerics (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id  TEXT NOT NULL,
    date       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    systolic   INTEGER,
    diastolic  INTEGER,
    glucose    REAL,
    weight     REAL,
    note       TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meals_date ON meals (member_id, date);
CREATE INDEX IF NOT EXISTS idx_numerics_date ON numerics (member_id, date, kind);
"""


def connect() -> sqlite3.Connection:
    """打开连接。WAL 模式：读写不互斥，演示时界面轮询不会卡住写入。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ──────────────────────────── 档案层 ────────────────────────────

def save_report(result: dict, source: str = "", sex: str = "unknown") -> int:
    """保存一次完整解读，并把其中触发的红线设为当前生效。"""
    # 归一化：规则引擎的裸输出没有 counts 键（/api/analyze 直接透传），
    # 不补齐的话历史列表里的"识别 X 项 / 异常 Y 项"会永远是 0。
    if "counts" not in result:
        judged = result.get("judged", [])
        result = {
            **result,
            "counts": {
                "total": result.get("total", len(judged)),
                "matched": result.get("matched", len(judged)),
                "abnormal_count": result.get(
                    "abnormal_count",
                    len([j for j in judged if j.get("flag") in ("high", "low")])),
            },
        }
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO reports (created_at, source, sex, result_json) VALUES (?,?,?,?)",
            (_now(), source, sex, json.dumps(result, ensure_ascii=False)),
        )
        # 红线档案 = 最近一次报告触发的红线。新报告生效即整组替换：
        # 体检是低频事件，一个人同一时刻只有一份「当前红线」。
        conn.execute("DELETE FROM active_redlines WHERE member_id='me'")
        for r in result.get("redlines", []):
            conn.execute(
                "INSERT INTO active_redlines VALUES (?,?,?,?,?,?,?,?,?)",
                ("me", r["rule_id"], r["restriction"], r.get("marker", ""),
                 r["nutrient_key"], r.get("action", "limit"),
                 json.dumps(r.get("trigger_foods", []), ensure_ascii=False),
                 r.get("advice", ""), _now()),
            )
        return cur.lastrowid


def latest_report() -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reports ORDER BY id DESC LIMIT 1").fetchone()
        return json.loads(row["result_json"]) if row else None


def list_reports(limit: int = 20) -> list[dict]:
    """历史报告列表（新→旧），供趋势对比与档案页使用。"""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, created_at, source, sex, result_json "
            "FROM reports ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
        out = []
        for r in rows:
            result = json.loads(r["result_json"])
            out.append({
                "id": r["id"],
                "created_at": r["created_at"],
                "source": r["source"],
                "sex": r["sex"],
                "total": result.get("counts", {}).get("matched", 0),
                "abnormal_count": result.get("counts", {}).get("abnormal_count", 0),
                "redlines": len(result.get("redlines", [])),
            })
        return out


# ──────────────────────────── 日常层 ────────────────────────────

def add_meal(member_id: str, date: str, meal_type: str, dishes: list[str]) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO meals (member_id, date, meal_type, dishes, created_at) "
            "VALUES (?,?,?,?,?)",
            (member_id, date, meal_type, json.dumps(dishes, ensure_ascii=False), _now()),
        )
        return cur.lastrowid


def meals_for_date(member_id: str, date: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM meals WHERE member_id=? AND date=? ORDER BY id",
            (member_id, date),
        ).fetchall()
        return [
            {"id": r["id"], "meal_type": r["meal_type"],
             "dishes": json.loads(r["dishes"]), "created_at": r["created_at"]}
            for r in rows
        ]


def add_numeric(member_id: str, date: str, kind: str, **vals) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO numerics (member_id, date, kind, systolic, diastolic, "
            "glucose, weight, note, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (member_id, date, kind, vals.get("systolic"), vals.get("diastolic"),
             vals.get("glucose"), vals.get("weight"), vals.get("note"), _now()),
        )
        return cur.lastrowid


def numerics_for_date(member_id: str, date: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM numerics WHERE member_id=? AND date=? ORDER BY id",
            (member_id, date),
        ).fetchall()
        return [dict(r) for r in rows]


def get_active_redlines(member_id: str = "me") -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM active_redlines WHERE member_id=? ORDER BY rule_id",
            (member_id,),
        ).fetchall()
        return [
            {
                "rule_id": r["rule_id"],
                "restriction": r["restriction"],
                "marker": r["marker"],
                "nutrient_key": r["nutrient_key"],
                "action": r["action"],
                "trigger_foods": json.loads(r["trigger_foods"]),
                "advice": r["advice"],
            }
            for r in rows
        ]
