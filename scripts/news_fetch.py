#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
news_fetch.py：盘中新闻定时累积（CHG-014 二次修订；C18 Windows 计划任务，每小时一次）
- 仅在 A 股交易日 + 交易时段（9:30-11:30 / 13:00-15:00，北京时间）内抓取并累积财联社快讯
- 非交易日 / 非交易时段：静默跳过（不累积）
- 累积到 data/processed/news/cls_YYYY-MM-DD.json（按 id 去重，只存标题/时间/链接，C15）

用法：python scripts/news_fetch.py [--force]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.quality.checks import load_calendar  # noqa: E402
from app.collectors.cls_src import accumulate_telegraphs, news_enabled  # noqa: E402

TRADE_SESSIONS = [("09:30", "11:30"), ("13:00", "15:00")]
BJ = timezone(timedelta(hours=8))


def in_trade_session(now: datetime) -> bool:
    hm = now.strftime("%H:%M")
    return any(s <= hm <= e for s, e in TRADE_SESSIONS)


def main() -> None:
    parser = argparse.ArgumentParser(description="盘中新闻定时累积（每小时）")
    parser.add_argument("--force", action="store_true", help="跳过交易日/时段检查（测试用）")
    args = parser.parse_args()

    now = datetime.now(BJ)
    if not news_enabled():
        print("新闻数据源已禁用，跳过新闻累积")
        return
    if not args.force:
        if now.date().isoformat() not in load_calendar():
            print(f"{now.date()} 非交易日，跳过新闻累积")
            return
        if not in_trade_session(now):
            print(f"{now.strftime('%H:%M')} 非交易时段，跳过新闻累积")
            return
    items = accumulate_telegraphs()
    print(f"[ok] 盘中新闻已累积：今日共 {len(items)} 条（{now.strftime('%H:%M')}）")


if __name__ == "__main__":
    main()
