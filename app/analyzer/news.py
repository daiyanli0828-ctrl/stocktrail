# -*- coding: utf-8 -*-
"""
新闻视图（CHG-014 修订）：盘中快讯 + 盘后要闻。
纯规则、无 LLM；C15：只输出标题 + 链接 + 时间，不落正文。
"""
from __future__ import annotations


def build_intraday_view(telegraphs: list[dict], limit: int = 20) -> str | None:
    """盘中快讯：最新 N 条（倒序，红黄绿标记重要程度）。"""
    if not telegraphs:
        return None
    latest = sorted(telegraphs, key=lambda x: x.get("ts", 0), reverse=True)[:limit]
    return "\n".join(
        f"- {t.get('time', '')} {t.get('mark', '')} [{t['title']}]({t['url']})"
        for t in latest
    )


def build_afterhours_view(telegraphs: list[dict], names: dict[str, str],
                          related_codes: list[str], top_n: int = 15) -> str | None:
    """盘后要闻：全天累积，按红黄绿重要程度排序 + 自选/持仓相关单列。"""
    if not telegraphs:
        return None
    keywords = set(related_codes) | {n for n in names.values() if n}
    related, market = [], []
    for t in telegraphs:
        hit = any(k and (k in t.get("title", "") or k in " ".join(t.get("stocks") or []))
                  for k in keywords)
        (related if hit else market).append(t)

    lines = []
    market_sorted = sorted(market, key=lambda x: (x.get("tier", 2), x.get("ts", 0)))
    for t in market_sorted[:top_n]:
        lines.append(f"- {t.get('time', '')} {t.get('mark', '')} [{t['title']}]({t['url']})")
    if related:
        lines.append("- 自选/持仓相关：")
        for t in related:
            lines.append(f"  - {t.get('time', '')} {t.get('mark', '')} [{t['title']}]({t['url']})")
    return "\n".join(lines)
