# -*- coding: utf-8 -*-
"""
纪律统计（F-06 交易频次行 / F-07 计划 vs 成交比对）
依据：企划 v1.0 §2.11 C1.1（交易频次行固定显示）、§2.5 M5 C3/C19
"""
from __future__ import annotations

import sqlite3
from datetime import date


def _month_range(anchor: date, months_back: int) -> tuple[str, str]:
    """返回 (月初 YYYY-MM-01, 月末 YYYY-MM-31)（按 ts 的 YYYY-MM 前缀匹配）。"""
    y, m = anchor.year, anchor.month
    for _ in range(months_back):
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    return f"{y:04d}-{m:02d}", f"{y:04d}-{m:02d}"


def _count_month(conn: sqlite3.Connection, month_prefix: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM trades WHERE substr(ts,1,7) = ?", (month_prefix,)
    ).fetchone()
    return int(row[0] or 0)


def _count_unplanned(conn: sqlite3.Connection, month_prefix: str) -> int:
    """非计划笔数：本月成交中无对应盘前计划（notes.kind='plan'，同日同代码同方向）的交易。"""
    import json

    rows = conn.execute(
        "SELECT id, code, side, substr(ts,1,10) AS d FROM trades WHERE substr(ts,1,7) = ?",
        (month_prefix,),
    ).fetchall()
    unplanned = 0
    for tid, code, side, d in rows:
        plans = conn.execute(
            "SELECT content FROM notes WHERE kind='plan' AND substr(ts,1,10) = ?",
            (d,),
        ).fetchall()
        matched = False
        for (content,) in plans:
            try:
                plan = json.loads(content)
                if plan.get("code") == code and plan.get("side") == side:
                    matched = True
                    break
            except json.JSONDecodeError:
                # 兼容旧格式（纯文本含代码）计划
                if content and code in content:
                    matched = True
                    break
        if not matched:
            unplanned += 1
    return unplanned


def trade_frequency(conn: sqlite3.Connection, today: date | None = None,
                    total_shares: float = 0.0) -> dict:
    """交易频次行（C1.1）：本月/上月笔数、换手率、非计划笔数。

    换手率定义（M0）：本月成交股数合计 / 期末总持仓股数（positions.yaml 汇总）* 100%。
    """
    today = today or date.today()
    this_m = _month_range(today, 0)[0]
    last_m = _month_range(today, 1)[0]
    this_count = _count_month(conn, this_m)
    last_count = _count_month(conn, last_m)
    unplanned = _count_unplanned(conn, this_m)
    shares_row = conn.execute(
        "SELECT COALESCE(SUM(shares),0) FROM trades WHERE substr(ts,1,7) = ? AND side='buy'",
        (this_m,),
    ).fetchone()
    this_buy_shares = float(shares_row[0] or 0)
    turnover = this_buy_shares / total_shares * 100 if total_shares else 0.0
    return {
        "this_month": this_count,
        "last_month": last_count,
        "unplanned": unplanned,
        "turnover_pct": round(turnover, 1),
        "month_label": this_m,
        "last_month_label": last_m,
    }


def freq_line(stats: dict) -> str:
    """交易频次行固定文案（C1.1）。"""
    return (
        f"交易频次：本月已交易 {stats['this_month']} 笔（上月 {stats['last_month']} 笔）"
        f" · 本月换手率 {stats['turnover_pct']}% · 非计划交易 {stats['unplanned']} 笔"
    )
