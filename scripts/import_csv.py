#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_csv.py：券商 CSV 对账导入（F-07/C19）
职责收窄：仅【对账与补全成交价/手续费】——按 (code, side, 日期) 匹配已有 trades 更新 price/shares，
或新增成交行（理由字段一律为 NULL，不写入 reason_type/reason_note/plan_target/stop_loss）。
理由四字段只能通过 note 命令录入（T+1 锁定，C3/C19）。

CSV 列：code, side, price, shares, ts（ts 为 ISO-8601，含时区，如 2026-08-31T14:05:00+08:00）
用法：python scripts/import_csv.py data/inputs/trades.csv
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "processed" / "app.db"


def _to_utc_iso(ts: str) -> str:
    """任意含时区 ISO → UTC ISO（D4 全库 UTC）。"""
    dt = datetime.fromisoformat(ts.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def import_csv(conn: sqlite3.Connection, csv_path: Path) -> tuple[int, int]:
    updated = 0
    inserted = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = str(row["code"]).strip()
            side = str(row["side"]).strip().lower()
            price = float(row["price"])
            shares = float(row["shares"])
            ts = _to_utc_iso(row["ts"])
            day = ts[:10]
            # 对账匹配：同 (code, side, 交易日) 的已有成交行 → 仅更新 price/shares（不碰 reason 字段）
            hit = conn.execute(
                "SELECT id FROM trades WHERE code=? AND side=? AND substr(ts,1,10)=? LIMIT 1",
                (code, side, day),
            ).fetchone()
            if hit:
                conn.execute(
                    "UPDATE trades SET price=?, shares=? WHERE id=?",
                    (price, shares, hit[0]),
                )
                updated += 1
            else:
                # 新增成交行：理由四字段保持 NULL（C19：CSV 不写 reason）
                conn.execute(
                    "INSERT INTO trades(code, side, price, shares, ts, reason_type, reason_note,"
                    " plan_target, stop_loss, reason_recorded_at, post_hoc)"
                    " VALUES(?,?,?,?,?,NULL,NULL,NULL,NULL,NULL,0)",
                    (code, side, price, shares, ts),
                )
                inserted += 1
    conn.commit()
    return updated, inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 券商 CSV 对账导入（不写理由字段，C19）")
    parser.add_argument("csv", help="CSV 路径（列：code,side,price,shares,ts）")
    parser.add_argument("--db", default=str(DB), help="SQLite 数据库路径")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    try:
        updated, inserted = import_csv(conn, Path(args.csv))
        print(f"[ok] 对账完成：更新 {updated} 行 / 新增 {inserted} 行（理由字段未写入）")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
