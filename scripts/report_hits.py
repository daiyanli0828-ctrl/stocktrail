#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_hits.py：预测命中率报告（P-02 完整验证流水线）
从 notes(kind='hypothesis') 统计指定窗口（周/月）的判定结果：
  命中率 = 命中 /（命中 + 未命中）；无法判定单独列出（不计入命中率分母）。
输出对比基线（--baseline 0.0~1.0，默认取窗口内自身为基线；20 日基线由 M0 退出评审建立后传入）。

用法：python scripts/report_hits.py [--days 7|30] [--end YYYY-MM-DD] [--baseline 0.5] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DB = PROJECT_ROOT / "data" / "processed" / "app.db"


def load_verdicts(conn: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = conn.execute(
        "SELECT content FROM notes WHERE kind='hypothesis' AND substr(ts,1,10) BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    out = []
    for (content,) in rows:
        try:
            rec = json.loads(content)
        except json.JSONDecodeError:
            continue
        out.append({
            "date": rec.get("date"),
            "conclusion": rec.get("conclusion", ""),
            "verdict": rec.get("verdict", ""),
            "criterion": rec.get("criterion", ""),
            "source": rec.get("source", "manual"),
            "rule_version": rec.get("rule_version"),
        })
    return out


def report(conn: sqlite3.Connection, start: str, end: str, baseline: float | None) -> dict:
    recs = load_verdicts(conn, start, end)
    judged = [r for r in recs if r["verdict"] in ("命中", "未命中")]
    unjudged = [r for r in recs if r["verdict"] not in ("命中", "未命中")]
    hits = sum(1 for r in judged if r["verdict"] == "命中")
    hit_rate = hits / len(judged) if judged else 0.0
    # 按来源分组（手动结论 vs 热点规则结论）
    by_source: dict[str, dict] = {}
    for r in recs:
        s = by_source.setdefault(r["source"], {"hit": 0, "miss": 0, "unjudged": 0, "total": 0})
        s["total"] += 1
        if r["verdict"] == "命中":
            s["hit"] += 1
        elif r["verdict"] == "未命中":
            s["miss"] += 1
        else:
            s["unjudged"] += 1
    return {
        "window": (start, end), "total": len(recs), "hits": hits,
        "misses": len(judged) - hits, "unjudged": len(unjudged),
        "hit_rate": round(hit_rate * 100, 1), "baseline": baseline,
        "by_source": by_source,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 预测命中率报告（P-02）")
    parser.add_argument("--days", type=int, default=7, help="窗口天数（7=周报，30=月报）")
    parser.add_argument("--end", type=str, default="", help="结束日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--baseline", type=float, default=None, help="基线命中率 0~1（M0 20 日基线）")
    parser.add_argument("--db", default=str(DB))
    args = parser.parse_args()

    end = date.fromisoformat(args.end) if args.end else date.today()
    start = end - timedelta(days=args.days - 1)
    conn = sqlite3.connect(args.db)
    try:
        r = report(conn, start.isoformat(), end.isoformat(), args.baseline)
    finally:
        conn.close()

    print(f"# 预测命中率报告（{r['window'][0]} ~ {r['window'][1]}，{args.days} 天）\n")
    print(f"- 已判定 {r['hits'] + r['misses']} 条：命中 {r['hits']} / 未命中 {r['misses']} / "
          f"无法判定 {r['unjudged']}")
    print(f"- 命中率 {r['hit_rate']}%", end="")
    if r["baseline"] is not None:
        delta = r["hit_rate"] - r["baseline"] * 100
        print(f"（基线 {r['baseline']*100:.0f}%，{'+' if delta >= 0 else ''}{delta:.1f}pp）")
    else:
        print("（基线未设：M0 20 日基线建立后以 --baseline 传入对比）")
    print("\n- 按来源：")
    for src, s in r["by_source"].items():
        rate = s["hit"] / (s["hit"] + s["miss"]) * 100 if (s["hit"] + s["miss"]) else 0.0
        print(f"  · {src}: 命中 {s['hit']} / 未命中 {s['miss']} / 无法判定 {s['unjudged']}"
              f"（判定内命中率 {rate:.0f}%）")


if __name__ == "__main__":
    main()
