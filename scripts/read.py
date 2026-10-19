#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
read.py：价值验收记录（F-08；企划 v1.0 §2.10 C5）
用法：read <段落名> <有用|没用> [--date YYYY-MM-DD]
段落名与 daily.md 固定段名对齐（C5）：市场概况 / 账户体检 / 自选异动 / 昨日验证 / 明日计划 / 交易频次
落库：notes(kind='read', content=JSON {"section","verdict","date"})
M0 退出评审的数据来源（C5：跳过率 > 50% 的段落直接删除）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "processed" / "app.db"

# 与 daily.md 六部分固定段名对齐（C5）
SECTION_NAMES = ["市场概况", "账户体检", "自选异动", "昨日验证", "明日计划", "交易频次"]
VERDICTS = ("有用", "没用")


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 价值验收记录（今天哪段有用）")
    parser.add_argument("section", choices=SECTION_NAMES, help="段落名（六部分之一）")
    parser.add_argument("verdict", choices=VERDICTS, help="有用 / 没用")
    parser.add_argument("--date", type=str, default="", help="YYYY-MM-DD（默认今天；测试注入用）")
    parser.add_argument("--db", default=str(DB), help="SQLite 数据库路径")
    args = parser.parse_args()

    day = args.date or date.today().isoformat()
    # 记录时刻：当日 20:00 北京时间（盘后阅读场景）≈ 12:00 UTC
    ts = f"{day}T12:00:00+00:00"
    content = json.dumps({"section": args.section, "verdict": args.verdict, "date": day},
                         ensure_ascii=False)
    conn = sqlite3.connect(args.db)
    try:
        conn.execute("INSERT INTO notes(ts, kind, content) VALUES(?, 'read', ?)", (ts, content))
        conn.commit()
        print(f"[ok] 已记录：{day} {args.section} → {args.verdict}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
