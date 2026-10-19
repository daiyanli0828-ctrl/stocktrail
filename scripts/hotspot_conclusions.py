#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hotspot_conclusions.py：热点段结论生成（P-02；C6 prediction 必填 / C1.2 无标的清单）
规则初筛候选板块（涨幅 Top N）→ 生成带 prediction 的结论 → 写入 notes(kind='hypothesis')
供次日 verify_yesterday.py 验证（板块验证须在 deadline 当日盘后运行，见 verify 板块模式）。

晚报"热点结构"段由 run_daily.py 集成（market.hotspot）。

用法：python scripts/hotspot_conclusions.py [--date YYYY-MM-DD] [--top 3] [--db PATH]
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analyzer.hotspots import (  # noqa: E402
    fetch_sector_data, generate_conclusions, hotspot_block,
    market_structure, rank_candidates,
)

DB = PROJECT_ROOT / "data" / "processed" / "app.db"


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 热点段结论生成（P-02）")
    parser.add_argument("--date", type=str, default="", help="结论归属交易日（默认今天）")
    parser.add_argument("--top", type=int, default=3, help="候选板块数")
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args()

    trade_date = args.date or date.today().isoformat()
    df = fetch_sector_data()
    if df is None:
        print("热点段：板块数据源不可用（东财+新浪均失败），今日不生成结论（诚实降级，不编造）")
        return

    candidates = rank_candidates(df, top_n=args.top)
    structure = market_structure(df)
    print(hotspot_block(candidates, structure))

    records = generate_conclusions(candidates, trade_date, Path(args.db))
    print(f"\n[结论] 已生成 {len(records)} 条带 prediction 的热点结论（notes.hypothesis，次日验证）：")
    for r in records:
        print(f"  - {r['conclusion']}（target={r['prediction']['target_scope']} deadline=next_day）")


if __name__ == "__main__":
    main()
