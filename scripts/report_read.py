#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_read.py：价值验收统计（F-08；企划 v1.0 §2.10 C5）
按段落统计：有用次数 / 没用次数 / 跳过率（跳过 = 当日无该段 read 记录）。
跳过率 > 50% 的段落列入"删除候选"（C5 第 8 条：直接删除，不留开关）——M0 退出评审数据来源。

用法：python scripts/report_read.py [--days 20] [--end YYYY-MM-DD] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "processed" / "app.db"
SECTION_NAMES = ["市场概况", "账户体检", "自选异动", "昨日验证", "明日计划", "交易频次"]


def load_records(conn: sqlite3.Connection, start: str, end: str) -> dict[str, list[dict]]:
    """{day: [{section, verdict}, ...]}（统计窗口内的 read 记录）。"""
    rows = conn.execute(
        "SELECT content FROM notes WHERE kind='read' AND substr(ts,1,10) BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    by_day: dict[str, list[dict]] = {}
    for (content,) in rows:
        try:
            rec = json.loads(content)
            day = rec.get("date") or content[:10]
            by_day.setdefault(day, []).append({
                "section": rec["section"], "verdict": rec.get("verdict", ""),
            })
        except (json.JSONDecodeError, KeyError):
            continue
    return by_day


def report(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    by_day = load_records(conn, start, end)
    start_d, end_d = date.fromisoformat(start), date.fromisoformat(end)
    total_days = (end_d - start_d).days + 1   # M0 以自然日计；交易日历细化留给 M0 后

    out = []
    for section in SECTION_NAMES:
        useful = sum(1 for recs in by_day.values() for r in recs
                     if r["section"] == section and r["verdict"] == "有用")
        useless = sum(1 for recs in by_day.values() for r in recs
                      if r["section"] == section and r["verdict"] == "没用")
        recorded_days = sum(1 for recs in by_day.values() if any(r["section"] == section for r in recs))
        skip_rate = 1 - recorded_days / total_days if total_days else 0.0
        out.append({
            "section": section, "useful": useful, "useless": useless,
            "recorded_days": recorded_days, "total_days": total_days,
            "skip_rate": round(skip_rate * 100, 1),
            "delete_candidate": skip_rate > 0.5,
        })
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 价值验收统计（跳过率报告）")
    parser.add_argument("--days", type=int, default=20, help="统计窗口（自然日），默认 20")
    parser.add_argument("--end", type=str, default="", help="结束日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--db", default=str(DB), help="SQLite 数据库路径")
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = end - timedelta(days=args.days - 1)
    conn = sqlite3.connect(args.db)
    try:
        rows = report(conn, start.isoformat(), end.isoformat())
    finally:
        conn.close()

    print(f"# read 价值验收报告（{start} ~ {end}，{args.days} 天）\n")
    print("| 段落 | 有用 | 没用 | 有记录天数 | 跳过率 | 删除候选 |")
    print("| --- | --- | --- | --- | --- | --- |")
    for r in rows:
        mark = "⚠ >50%" if r["delete_candidate"] else ""
        print(f"| {r['section']} | {r['useful']} | {r['useless']} | {r['recorded_days']}/{r['total_days']} "
              f"| {r['skip_rate']}% | {mark} |")


if __name__ == "__main__":
    main()
