# -*- coding: utf-8 -*-
"""
持仓推算与合并（CHG-012）
- compute_positions_from_trades：默认来源——从 trades 表（买入/卖出记录）推算净持仓
- effective_positions：手动持仓（positions.yaml）覆盖/补充自动持仓（按 code）
持仓信息（positions.yaml）与自选信息（watchlist.yaml）后端分文件记录，互不混用。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path


def compute_positions_from_trades(db) -> list[dict]:
    """从 trades 表推算持仓：净股数 = 买入 - 卖出；avg_cost = 买入加权均价；
    opened_at = 该代码最早买入交易日。只保留净股数 > 0 的代码。
    db 可为路径或 sqlite3.Connection。"""
    own_conn = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db) if own_conn else db
    try:
        rows = conn.execute(
            "SELECT code, side, SUM(shares), SUM(price*shares), MIN(substr(ts,1,10))"
            " FROM trades GROUP BY code, side"
        ).fetchall()
    finally:
        if own_conn:
            conn.close()
    agg: dict[str, dict] = {}
    for code, side, s, amt, first_day in rows:
        d = agg.setdefault(code, {"buy_shares": 0.0, "buy_amt": 0.0, "sell_shares": 0.0, "opened_at": None})
        if side == "buy":
            d["buy_shares"] += float(s or 0)
            d["buy_amt"] += float(amt or 0)
            if d["opened_at"] is None or (first_day and first_day < d["opened_at"]):
                d["opened_at"] = first_day
        elif side == "sell":
            d["sell_shares"] += float(s or 0)
    out = []
    for code, d in agg.items():
        net = d["buy_shares"] - d["sell_shares"]
        if net > 0:
            avg_cost = round(d["buy_amt"] / d["buy_shares"], 3) if d["buy_shares"] else 0.0
            out.append({
                "code": str(code).zfill(6),
                "name": "",
                "shares": net,
                "avg_cost": avg_cost,
                "opened_at": d["opened_at"],   # 无买入记录时 None，compute_health 回退 today
                "stop_loss": None,
                "plan_target": None,
            })
    out.sort(key=lambda x: x["code"])
    return out


def effective_positions(db, manual_positions: list[dict]) -> list[dict]:
    """合并：手动持仓（positions.yaml）覆盖/补充自动持仓（按 code）。"""
    merged: dict[str, dict] = {}
    for p in compute_positions_from_trades(db):
        merged[str(p["code"]).zfill(6)] = p
    for p in manual_positions or []:
        code = str(p.get("code", "")).zfill(6)
        if not code or code == "000000":
            continue
        merged[code] = {
            "code": code,
            "name": str(p.get("name") or ""),
            "shares": float(p.get("shares", 0) or 0),
            "avg_cost": float(p.get("avg_cost", 0) or 0),
            "opened_at": p.get("opened_at") or None,
            "stop_loss": float(p["stop_loss"]) if p.get("stop_loss") else None,
            "plan_target": float(p["plan_target"]) if p.get("plan_target") else None,
        }
    return list(merged.values())
