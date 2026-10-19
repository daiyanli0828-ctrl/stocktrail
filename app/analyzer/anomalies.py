# -*- coding: utf-8 -*-
"""
异动观察（CHG-014）：板块领涨/领跌 + 自选/持仓相关异动 + 全市场情绪概览。
纯规则、无 LLM；措辞固定「情绪观察，非荐股」（C1.2 无标的清单 / C7 无指令）。
数据源：同花顺特色数据（涨停池 / 跌停异动）+ akshare 板块（复用 P-02 的 sector_df）。
"""
from __future__ import annotations

import pandas as pd


def emotion_overview(limit_up_pool: list[dict], limit_down_count: int) -> str:
    """全市场情绪概览：涨停家数 / 跌停家数 / 最高连板。"""
    up_count = len(limit_up_pool)
    board_max = max((x.get("board_cnt", 0) for x in limit_up_pool), default=0)
    parts = [f"涨停 {up_count} 家 / 跌停 {limit_down_count} 家"]
    if board_max > 0:
        parts.append(f"最高 {board_max} 连板")
    return "，".join(parts) + "——情绪观察，非荐股"


def watchlist_anomalies(codes: list[str], names: dict[str, str],
                        limit_up_pool: list[dict], limit_down: list[dict]) -> list[str]:
    """自选/持仓相关异动：命中涨停池或跌停列表的代码，附原因（只列相关，不列全市场）。"""
    up = {x["code"]: x for x in limit_up_pool}
    down = {x["code"]: x for x in limit_down}
    lines = []
    for code in codes:
        name = names.get(code, "")
        if code in up:
            it = up[code]
            reason = f"，{it['reason']}" if it.get("reason") else ""
            board = f"（{it['board_text']}）" if it.get("board_text") else ""
            lines.append(f"  - {code} {name or ''} 涨停{board}{reason}——情绪观察，非荐股")
        if code in down:
            it = down[code]
            kw = "、".join(it.get("keywords") or []) or (it.get("reason") or "")[:40]
            reason = f"，{kw}" if kw else ""
            lines.append(f"  - {code} {name or ''} 跌停{reason}——情绪观察，非荐股")
    return lines


def lhb_line(day: str, top_n: int = 5) -> str | None:
    """龙虎榜：净买额居前 Top N（资金观察，非荐股）；东财不可达时返回 None（诚实降级）。"""
    try:
        import akshare as ak
        d8 = str(day).replace("-", "")   # akshare 需 YYYYMMDD
        df = ak.stock_lhb_detail_em(start_date=d8, end_date=d8)
    except Exception:  # noqa: BLE001
        return None
    if df is None or df.empty:
        return None
    d = (df.dropna(subset=["龙虎榜净买额"])
         .drop_duplicates(subset=["名称"])
         .sort_values("龙虎榜净买额", ascending=False)
         .head(top_n))
    parts = []
    for _, r in d.iterrows():
        amt = float(r["龙虎榜净买额"]) / 1e8
        parts.append(f"{r['名称']} {amt:+.1f}亿")
    if not parts:
        return None
    return f"龙虎榜净买额居前：{'、'.join(parts)}——情绪观察，非荐股"


def sector_anomalies(sector_df: pd.DataFrame | None, top_n: int = 5) -> str:
    """板块异动：领涨 Top N / 领跌 Top N（板块级，无个股，C1.2）。"""
    if sector_df is None or sector_df.empty:
        return ""
    d = sector_df.dropna(subset=["pct_chg"])

    def fmt(r):
        return f"{r['sector']} {float(r['pct_chg']):+.2f}%"

    up = "、".join(fmt(r) for _, r in d.sort_values("pct_chg", ascending=False).head(top_n).iterrows())
    down = "、".join(fmt(r) for _, r in d.sort_values("pct_chg", ascending=True).head(top_n).iterrows())
    return f"领涨 {up}；领跌 {down}"
