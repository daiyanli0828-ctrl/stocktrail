#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_increment.py：盘前增量附页（F-10；企划 v1.0 §2.5 M3 / C14 降级链）
输出 3–5 行附页 daily_increment.md 到 Obsidian vault：隔夜美股三大指数 / 富时中国A50 / 日经225
+ 一句"昨晚的计划是否需要调整"。全球数据为 T-1 时报头显式标注（C14）。

用法：python scripts/daily_increment.py [--db PATH] [--vault PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import yaml  # noqa: E402

from app.collectors.global_src import GlobalIndicesSource  # noqa: E402

DB = PROJECT_ROOT / "data" / "processed" / "app.db"


def _yesterday_plans(conn: sqlite3.Connection, beijing_date: str) -> list[str]:
    """昨晚盘前计划摘要（"昨晚" = 前一 A 股交易日，周末/节假日跳过，F-10 评审 P3 修复）。"""
    from app.quality.checks import load_calendar

    calendar = load_calendar()
    prev = date.fromisoformat(beijing_date) - timedelta(days=1)
    while prev.isoformat() not in calendar:
        prev -= timedelta(days=1)
        if (date.fromisoformat(beijing_date) - prev).days > 14:   # 防御：不应回退超过 14 天
            break
    y = prev.isoformat()
    rows = conn.execute("SELECT content FROM notes WHERE kind='plan' AND substr(ts,1,10)=?", (y,)).fetchall()
    plans = []
    for (content,) in rows:
        try:
            rec = json.loads(content)
            plans.append(f"{rec.get('side', '')} {rec.get('code', '')}")
        except json.JSONDecodeError:
            continue
    return plans


def build_increment(result: dict, plans: list[str]) -> str:
    lines: list[str] = []
    by_name = {i["name"]: i for i in result.get("indices", [])}

    def fmt(name: str) -> str:
        i = by_name.get(name)
        if not i:
            return f"{name}：—（来源缺失）"
        return f"{name} {i['close']:g}（{i['pct_chg']:+.2f}%）"

    # 美股三大指数一行
    us = " · ".join(fmt(n) for n in ("道琼斯", "纳斯达克", "标普500"))
    lines.append(f"- 隔夜美股（美东 {result.get('us_date') or '?'}）：{us}")
    lines.append(f"- {fmt('富时中国A50')}")
    lines.append(f"- {fmt('日经225')}")

    # 一句"昨晚的计划是否需要调整"
    us_up = any(i["name"] in ("道琼斯", "纳斯达克", "标普500") and i["pct_chg"] > 0
                for i in result.get("indices", []))
    direction = "上涨" if us_up else "下跌"
    if plans:
        lines.append(f"- 昨晚计划 {len(plans)} 条（{', '.join(plans)}）：隔夜美股{direction}，"
                     "请结合隔夜走势自行复核计划是否需要调整（仅供参考）")
    else:
        lines.append(f"- 昨晚无盘前计划：隔夜美股{direction}，今日开盘前如需调整请自行判断")

    if result.get("t1_note"):
        lines.append(f"*{result['t1_note']}*")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 盘前增量附页（全球数据 + T-1 降级）")
    parser.add_argument("--db", default=str(DB))
    parser.add_argument("--vault", type=str, default="",
                        help="Obsidian vault 目录（默认 config output.vault_dir）")
    parser.add_argument("--cache", type=str, default="", help="全球指数缓存路径（测试注入）")
    parser.add_argument("--date", type=str, default="", help="北京日期 YYYY-MM-DD（默认今天；测试/补跑注入）")
    parser.add_argument("--force", action="store_true", help="跳过休市日检查（P-06 计划任务/补跑用）")
    args = parser.parse_args()

    beijing_date = args.date or date.today().isoformat()
    # P-06：休市日跳过（本地交易日历，D3）——盘前附页仅交易日生成
    if not args.force:
        from app.quality.checks import load_calendar
        if beijing_date not in load_calendar():
            print(f"{beijing_date} 非交易日（休市），跳过盘前增量附页")
            return

    cfg_path = PROJECT_ROOT / "config" / "data_sources.yaml"
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    vault_dir = Path(args.vault) if args.vault else Path(cfg.get("output", {}).get("vault_dir", "data/processed/vault"))

    kwargs = {}
    if args.cache:   # 测试注入缓存路径；生产走默认 DEFAULT_CACHE（F-10 评审 P1 修复）
        kwargs["cache_path"] = Path(args.cache)
    src = GlobalIndicesSource(**kwargs)
    result = src.fetch()

    conn = sqlite3.connect(args.db)
    plans = _yesterday_plans(conn, result.get("beijing_date", date.today().isoformat()))
    conn.close()

    increment = build_increment(result, plans)
    vault_dir.mkdir(parents=True, exist_ok=True)
    beijing = result.get("beijing_date", date.today().isoformat())
    out = vault_dir / f"daily_increment-{beijing}.md"
    out.write_text(f"# 盘前增量附页 · {beijing}（北京）\n\n{increment}\n", encoding="utf-8")
    print(out)
    print(increment)
    if result.get("t1_note"):
        print(f"[降级] {result['t1_note']}")


if __name__ == "__main__":
    main()
