# -*- coding: utf-8 -*-
"""
财联社电报源（CHG-014 修订：新闻独立入口，盘中/盘后两个生成键）
C15 合规：只存标题/时间/链接/level/关联股票，不存正文全文，仅本地自用、不分发。
- fetch_telegraph_raw()：抓最新约 20 条快讯（免签名，仅 Referer 头）
- accumulate_telegraphs(day)：抓取 + 按 id 去重追加到当日累积文件（支撑「盘后全天要闻」）
- load_telegraphs(day)：读当日累积
存储：data/processed/news/cls_YYYY-MM-DD.json（仅标题/时间/链接，无正文）
"""
from __future__ import annotations

import json
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

CLS_API = "https://www.cls.cn/api/cache?app=CailianpressWeb&name=telegraph&os=web&sv=8.7.9"
CLS_REFERER = "https://www.cls.cn/telegraph"
CLS_DETAIL = "https://www.cls.cn/detail/{id}"

_BJ = timezone(timedelta(hours=8))
PROJECT_ROOT = Path(__file__).resolve().parents[2]
NEWS_DIR = PROJECT_ROOT / "data" / "processed" / "news"


def _store_path(day: str) -> Path:
    return NEWS_DIR / f"cls_{day}.json"


def news_enabled() -> bool:
    """新闻数据源是否启用（config/data_sources.yaml 的 news.sources 含 cls）。
    未配置 sources 键 → 默认启用（向后兼容）；显式空列表 → 停用。"""
    try:
        import yaml
        cfg = yaml.safe_load((PROJECT_ROOT / "config" / "data_sources.yaml").read_text(encoding="utf-8")) or {}
        news = cfg.get("news") or {}
        if "sources" not in news:
            return True
        return "cls" in (news.get("sources") or [])
    except Exception:  # noqa: BLE001 —— 配置缺失时默认启用（向后兼容）
        return True


def fetch_telegraph_raw() -> list[dict]:
    """抓最新快讯（约 20 条），返回 [{id, ts, time, title, url, level, important, stocks}]。"""
    req = urllib.request.Request(CLS_API, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Referer": CLS_REFERER,
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        body = json.loads(r.read().decode("utf-8"))
    rolls = (body.get("data") or {}).get("roll_data") or []
    out = []
    for it in rolls:
        ctime = it.get("ctime") or 0
        try:
            dt = datetime.fromtimestamp(int(ctime), tz=_BJ)
        except (ValueError, OSError, OverflowError):
            continue
        title = (it.get("title") or it.get("brief") or "").strip()
        if not title:
            continue
        level = str(it.get("level") or "")
        stocks = []
        for s in (it.get("stock_list") or []) or []:
            if isinstance(s, dict):
                code = str(s.get("ticker") or s.get("code") or s.get("thscode") or "")
                code = code.split(".")[0].zfill(6) if code else ""
                if code and code != "000000":
                    stocks.append(code)
        out.append({
            "id": str(it.get("id") or ""),
            "ts": int(ctime),
            "time": dt.strftime("%H:%M"),
            "title": title,
            "url": CLS_DETAIL.format(id=it.get("id") or ""),
            "level": level,
            # 红-黄-绿 三档重要程度：A=红（最重要）/ B=黄（重要）/ C=绿（普通）
            "mark": {"A": "🔴", "B": "🟡"}.get(level, "🟢"),
            "tier": {"A": 0, "B": 1}.get(level, 2),
            "stocks": stocks,
        })
    return out


def load_telegraphs(day: str) -> list[dict]:
    """读当日累积快讯。"""
    p = _store_path(day)
    if not p.exists():
        return []
    try:
        return (json.loads(p.read_text(encoding="utf-8")) or {}).get("telegraphs", [])
    except (json.JSONDecodeError, OSError):
        return []


def accumulate_telegraphs(day: str | None = None) -> list[dict]:
    """抓最新快讯并按 id 去重追加到当日累积文件，返回当日全部（时间升序）。"""
    day = day or datetime.now(_BJ).date().isoformat()
    merged = load_telegraphs(day)
    seen = {t.get("id") for t in merged if t.get("id")}
    for t in fetch_telegraph_raw():
        if not t.get("id") or t["id"] in seen:
            continue
        merged.append(t)
        seen.add(t["id"])
    merged.sort(key=lambda x: x.get("ts", 0))
    NEWS_DIR.mkdir(parents=True, exist_ok=True)
    _store_path(day).write_text(
        json.dumps({"day": day, "telegraphs": merged}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    return merged
