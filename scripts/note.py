#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
note.py：盘前预先承诺 / 交易理由录入（F-07；企划 v1.0 §2.5 M5 C3/C4/C19）

命令形态：
  note buy|sell <code> [--price X --shares Y] [--reason 文本] [--target X] [--stop Y] [--note 文本]
      · 不带 --price → 盘前预先承诺（notes.kind='plan'，10 秒录入，C3 主入口）
      · 带 --price/--shares → 记录实际成交（trades 行；理由四字段同时写入，reason_recorded_at=now）
  note reason <trade_id> --reason 文本 [--target X] [--stop Y] [--note 文本]
      · 成交后补充/修改理由四字段（C3 时间边界）：
          - 距成交 ≤24h：允许（含首次补录，post_hoc=0）
          - 距成交 >24h 且已有记录：拒绝修改（T+1 锁定）
          - 距成交 >24h 首次补录：post_hoc=1（归因样本剔除）
  note verify [--date YYYY-MM-DD]
      · 收盘后比对：note 计划 vs 实际成交 → "今日按计划执行 N 笔 / 非计划 M 笔"

约束：理由开放化（CHG-007）——开放文本存 reason_note、reason_type 留空 NULL（供 LLM 分析）；
CSV 导入不写 reason 字段（C19，见 import_csv.py）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "processed" / "app.db"

LOCK_AFTER_HOURS = 24                    # C3：T+1（24h）后锁定

_UTC = timezone.utc


def _now_iso() -> str:
    return datetime.now(_UTC).isoformat()


def _connect(db: str | None = None) -> sqlite3.Connection:
    return sqlite3.connect(db or DB)


def _validate_reason(reason: str | None) -> None:
    # CHG-007：理由开放化——不再强制三选一，开放文本存 reason_note（供 LLM 分析）
    return


# ---------------- 盘前承诺 / 成交录入 ----------------
def cmd_plan_or_trade(conn: sqlite3.Connection, side: str, code: str, args) -> None:
    _validate_reason(args.reason)
    # 测试注入：--date 时 ts 取该日 09:30 北京（01:30 UTC）；默认 now
    ts = (datetime.fromisoformat(args.date + "T01:30:00+00:00").isoformat()
          if args.date else _now_iso())
    if args.price is None or args.shares is None:
        # ---- 盘前预先承诺（notes.kind='plan'，C3） ----
        content = json.dumps({
            "side": side, "code": code, "reason": args.reason,
            "target": args.target, "stop": args.stop, "note": args.note,
        }, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO notes(ts, kind, content) VALUES(?, 'plan', ?)", (ts, content)
        )
        conn.commit()
        print(f"[ok] 盘前承诺已记录（notes#{cur.lastrowid}）：{side} {code}"
              + (f" 理由={args.reason}" if args.reason else "（未填理由）")
              + (f" 目标={args.target:g}" if args.target else "")
              + (f" 止损={args.stop:g}" if args.stop else ""))
    else:
        # ---- 成交录入（trades 行；CHG-007：理由开放文本存 reason_note，reason_type 留空） ----
        cur = conn.execute(
            "INSERT INTO trades(code, side, price, shares, ts, reason_type, reason_note,"
            " plan_target, stop_loss, reason_recorded_at, post_hoc)"
            " VALUES(?,?,?,?,?,NULL,?,?,?,?,0)",
            (code, side, args.price, args.shares, ts, args.reason or None,
             args.target, args.stop, ts if args.reason else None),
        )
        conn.commit()
        print(f"[ok] 成交已记录（trades#{cur.lastrowid}）：{side} {code} "
              f"@{args.price:g}×{args.shares:g}"
              + (f" 理由={args.reason}（reason_recorded_at={ts}）" if args.reason
                 else " 理由未填（可用 note reason <id> 补充，>24h 将标 post_hoc）"))


# ---------------- 理由补录/修改（C3 时间边界） ----------------
def cmd_reason(conn: sqlite3.Connection, trade_id: int, args) -> None:
    _validate_reason(args.reason)
    row = conn.execute(
        "SELECT id, ts, reason_type, reason_recorded_at, post_hoc FROM trades WHERE id=?",
        (trade_id,),
    ).fetchone()
    if not row:
        sys.exit(f"错误：trades#{trade_id} 不存在")
    tid, trade_ts, _, recorded_at, post_hoc = row
    trade_dt = datetime.fromisoformat(trade_ts)
    age = (datetime.now(_UTC) - trade_dt).total_seconds() / 3600

    if recorded_at is None:
        # 首次补录：>24h → post_hoc=1（归因样本剔除）
        late = age > LOCK_AFTER_HOURS
        conn.execute(
            "UPDATE trades SET reason_type=NULL, reason_note=?, plan_target=?, stop_loss=?,"
            " reason_recorded_at=?, post_hoc=? WHERE id=?",
            (args.reason, args.target, args.stop, _now_iso(), 1 if late else 0, tid),
        )
        conn.commit()
        print(f"[ok] 理由已补录 trades#{tid}"
              + ("（⚠ 晚于成交 24h，post_hoc=1，将从归因样本剔除）" if late else "（post_hoc=0）"))
    else:
        # 已有记录：>24h 锁定，拒绝修改（C3 T+1）
        if age > LOCK_AFTER_HOURS:
            sys.exit(f"拒绝：trades#{tid} 理由已锁定（距成交 {age:.1f}h > {LOCK_AFTER_HOURS}h，T+1 后不可修改）")
        conn.execute(
            "UPDATE trades SET reason_type=NULL, reason_note=?, plan_target=?, stop_loss=? WHERE id=?",
            (args.reason, args.target, args.stop, tid),
        )
        conn.commit()
        print(f"[ok] 理由已修改 trades#{tid}（距成交 {age:.1f}h，T+0 窗口内；"
              f"原始 reason_recorded_at 保留 {recorded_at}）")


# ---------------- 计划 vs 成交比对 ----------------
def _plan_of_trade(conn: sqlite3.Connection, code: str, side: str, trade_date: str) -> bool:
    """同日同代码同方向是否有盘前承诺（notes.kind='plan'，content 为 JSON）。"""
    rows = conn.execute(
        "SELECT content FROM notes WHERE kind='plan' AND substr(ts,1,10)=? ORDER BY ts",
        (trade_date,),
    ).fetchall()
    for (content,) in rows:
        try:
            plan = json.loads(content)
        except json.JSONDecodeError:
            continue
        if plan.get("code") == code and plan.get("side") == side:
            return True
    return False


def cmd_verify(conn: sqlite3.Connection, day: str) -> None:
    trades = conn.execute(
        "SELECT id, code, side FROM trades WHERE substr(ts,1,10)=?", (day,)
    ).fetchall()
    planned = sum(1 for tid, code, side in trades if _plan_of_trade(conn, code, side, day))
    total = len(trades)
    print(f"{day} 计划 vs 成交：今日按计划执行 {planned} 笔 / 非计划 {total - planned} 笔"
          f"（当日成交 {total} 笔）")


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 盘前预先承诺 / 交易理由录入")
    parser.add_argument("--db", default=str(DB), help="SQLite 数据库路径（默认 data/processed/app.db）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_trade = sub.add_parser("buy", help="买入：盘前承诺（不带 --price）或成交录入（带 --price/--shares）")
    p_trade.add_argument("code", help="6 位股票代码")
    p_trade.add_argument("--price", type=float, help="成交价（给价则视为成交录入）")
    p_trade.add_argument("--shares", type=float, help="成交数量（股）")
    p_trade.add_argument("--reason", help="理由（开放文本，CHG-007：供 LLM 分析）")
    p_trade.add_argument("--target", type=float, help="计划目标价")
    p_trade.add_argument("--stop", type=float, help="计划止损价")
    p_trade.add_argument("--note", help="备注")
    p_trade.add_argument("--date", type=str, default="", help="指定交易日（默认今天；仅供测试注入）")

    p_sell = sub.add_parser("sell", help="卖出（同 buy）")
    p_sell.add_argument("code")
    p_sell.add_argument("--price", type=float)
    p_sell.add_argument("--shares", type=float)
    p_sell.add_argument("--reason", help="理由（开放文本）")
    p_sell.add_argument("--target", type=float)
    p_sell.add_argument("--stop", type=float)
    p_sell.add_argument("--note")
    p_sell.add_argument("--date", type=str, default="")

    p_reason = sub.add_parser("reason", help="补录/修改成交理由（T+1 锁定）")
    p_reason.add_argument("trade_id", type=int)
    p_reason.add_argument("--reason", required=True, help="理由（开放文本）")
    p_reason.add_argument("--target", type=float)
    p_reason.add_argument("--stop", type=float)
    p_reason.add_argument("--note")

    p_verify = sub.add_parser("verify", help="计划 vs 成交比对")
    p_verify.add_argument("--date", type=str, default="", help="YYYY-MM-DD（默认今天）")

    args = parser.parse_args()
    conn = _connect(args.db)
    try:
        if args.cmd in ("buy", "sell"):
            cmd_plan_or_trade(conn, args.cmd, args.code, args)
        elif args.cmd == "reason":
            cmd_reason(conn, args.trade_id, args)
        elif args.cmd == "verify":
            day = args.date or date.today().isoformat()
            cmd_verify(conn, day)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
