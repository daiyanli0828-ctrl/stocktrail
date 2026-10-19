# -*- coding: utf-8 -*-
"""
板块行业观察（CHG-017）：今日涨跌 TopN 板块描述 + 近 2 周板块趋势。
- 全板块当日行情 + 净流入：同花顺行业一览（akshare stock_board_industry_summary_ths，90 行业）
- 板块指数代码：同花顺官方 API（fuyao）行业目录（881xxx.TI，约 90 主行业）
- 近 2 周趋势：同花顺官方 API 行业指数历史日K（窗口≈11 个交易日）
数据为事实描述（涨跌幅/净流入/趋势），无买卖建议（C1.2/C7）；任一数据源失败则诚实降级。
"""
from __future__ import annotations


def _fetch_industry_spot() -> list[dict]:
    """同花顺行业一览 → [{name, pct_chg, net_inflow}]（akshare；按涨跌幅降序）。"""
    import akshare as ak
    df = ak.stock_board_industry_summary_ths()
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        try:
            pct = float(r.get("涨跌幅"))
        except (TypeError, ValueError):
            continue
        try:
            inflow = float(r.get("净流入")) if r.get("净流入") is not None else None
        except (TypeError, ValueError):
            inflow = None
        rows.append({"name": str(r.get("板块", "")).strip(), "pct_chg": pct, "net_inflow": inflow})
    rows.sort(key=lambda x: x["pct_chg"], reverse=True)
    return rows


def build_sector_block(top_n: int = 5) -> str | None:
    """拼「板块行业观察」markdown：领涨/领跌 TopN + 近2周趋势。失败返回 None。"""
    rows = _fetch_industry_spot()
    if not rows:
        return None
    from app.collectors.ths_src import ThsSource
    ths = ThsSource()
    try:
        catalog = ths.get_industry_catalog()   # {行业名: thscode}（881xxx）
    except Exception:  # noqa: BLE001 —— 目录失败则回退逐名检索
        catalog = {}

    up = rows[:top_n]
    down = rows[-top_n:] if len(rows) > top_n else rows[:top_n]
    down = list(reversed(down))   # 涨跌幅升序 → 跌最多在前

    def _fmt(r: dict) -> str:
        parts = [f"{r['name']} {r['pct_chg']:+.2f}%"]
        if r.get("net_inflow") is not None:
            parts.append(f"净流入{float(r['net_inflow']):+.1f}亿")
        thscode = catalog.get(r["name"])
        if not thscode:
            try:   # 目录未命中时逐名检索（881xxx.TI）
                hits = ths.search_index(r["name"])
                hit = next((h for h in hits if h["name"] == r["name"]), hits[0] if hits else None)
                thscode = hit["thscode"] if hit else None
            except Exception:  # noqa: BLE001
                thscode = None
        if thscode:
            try:
                hist = ths.get_index_history(thscode, days=18)
                if len(hist) >= 2:
                    pct = (float(hist[-1]["close"]) / float(hist[0]["close"]) - 1) * 100
                    parts.append(f"近2周{pct:+.1f}%")
            except Exception:  # noqa: BLE001
                pass
        return "（" + "，".join(parts) + "）"

    up_s = "、".join(_fmt(r) for r in up)
    down_s = "、".join(_fmt(r) for r in down)
    return (f"- **板块行业观察**（今日 Top{top_n} · 含近2周趋势）\n"
            f"- 领涨：{up_s}\n"
            f"- 领跌：{down_s}")
