#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F-04 data_test.py：一键执行 F-02 采集 + F-03 质量检查，输出测试数据与字段稳定性清单。

输出：
  - data/processed/test/market.json   指数快照 + 涨跌家数 + 成交额 + 涨停/跌停 + 质量摘要 + 源可用性
  - data/processed/test/stocks.json   自选股（读 watchlist.yaml）日线 + quality_flag + code_meta
  - data/processed/test/field_stability.md  每次运行追加各接口字段观测（字段名/单位/空值/状态）
  - 数据源可用性结论（成功/失败 + 原因，含降级事件）

用法：python scripts/data_test.py [--days 120] [--out data/processed/test]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("TQDM_DISABLE", "1")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from app.collectors.akshare_src import AkshareSource  # noqa: E402
from app.quality.checks import QualityChecker, load_calendar, load_rules  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("data_test")

CONFIG = PROJECT_ROOT / "config" / "data_sources.yaml"
WATCHLIST = PROJECT_ROOT / "data" / "inputs" / "watchlist.yaml"
FAILURES_LOG = PROJECT_ROOT / "data" / "processed" / "failures.log"
OUT_DIR = PROJECT_ROOT / "data" / "processed" / "test"


def load_config() -> dict:
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_watchlist() -> list[str]:
    with open(WATCHLIST, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [str(item["code"]) for item in data.get("watchlist", [])]


def safe_fetch(name: str, fn):
    """执行采集，返回 (result, status, reason)。失败原因截断 200 字符。"""
    try:
        result = fn()
        return result, "ok", ""
    except Exception as exc:  # noqa: BLE001
        return None, "fail", str(exc)[:200]


def read_new_failures(since_ts: str) -> list[dict]:
    """读取 failures.log 中 ts >= since_ts 的记录（降级/失败事件）。"""
    out = []
    if not FAILURES_LOG.exists():
        return out
    for line in FAILURES_LOG.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            if rec.get("ts", "") >= since_ts:
                out.append(rec)
        except json.JSONDecodeError:
            continue
    return out


def append_field_observations(run_ts: str, obs: list[dict]) -> None:
    """追加本次运行的接口字段观测到 field_stability.md。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "field_stability.md"
    lines = [f"\n## 运行观测 {run_ts}", ""]
    for o in obs:
        lines.append(f"### {o['interface']} — {o['status']}")
        if o.get("columns"):
            lines.append(f"- 字段: {', '.join(o['columns'])}")
        lines.append(f"- 行数: {o.get('rows', 'n/a')}；空值: {o.get('nulls', 'n/a')}")
        if o.get("note"):
            lines.append(f"- 备注: {o['note']}")
        lines.append("")
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 数据测试：采集 + 质量检查 + 字段稳定性")
    parser.add_argument("--days", type=int, default=120, help="回看窗口（自然日），默认 120")
    parser.add_argument("--out", type=str, default=str(OUT_DIR), help="输出目录")
    args = parser.parse_args()

    cfg = load_config()
    codes = load_watchlist()
    run_ts = datetime.now(timezone.utc).isoformat()
    end = date.today()
    start = end - timedelta(days=args.days)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    src = AkshareSource(
        config=cfg.get("fetch", {}).get("index_snapshot", {}),
        rate_limit_sec=cfg["sources"]["akshare"].get("rate_limit_sec", 0.5),
        max_retries=cfg["sources"]["akshare"].get("max_retries", 3),
        backoff_base_sec=cfg["sources"]["akshare"].get("backoff_base_sec", 2.0),
        proxy=cfg.get("network", {}).get("proxy", "auto"),
    )

    sources_status: dict[str, dict] = {}

    # ---- F-02 采集 ----
    quotes_df, st, reason = safe_fetch("daily_quotes", lambda: src.get_daily_quotes(codes, str(start), str(end)))
    sources_status["daily_quotes"] = {"status": st, "reason": reason}
    if st == "fail":
        logger.error("日线采集失败: %s", reason)

    index_df, st2, reason2 = safe_fetch("index_snapshot", src.get_index_snapshot)
    sources_status["index_snapshot"] = {"status": st2, "reason": reason2}

    breadth, st3, reason3 = safe_fetch("market_breadth", src.get_market_breadth)
    sources_status["market_breadth"] = {"status": st3, "reason": reason3}

    # ---- 名称解析（ST/退市判定用；失败则降级为代码判定）----
    name_map: dict[str, str] = {}
    try:
        spot = src._request(lambda: __import__("akshare").stock_zh_a_spot_em(), "akshare.spot_name_map")
        name_map = dict(zip(spot["代码"].astype(str).str.zfill(6), spot["名称"].astype(str)))
        logger.info("名称映射加载 %d 条", len(name_map))
    except Exception:  # noqa: BLE001
        logger.warning("名称映射不可用（ST/退市判定降级为代码判定）")

    # ---- F-03 质量检查 ----
    quality_result = None
    if quotes_df is not None and not quotes_df.empty:
        code_meta = {
            c: {"name": name_map.get(c)} for c in codes
        }
        checker = QualityChecker(rules=load_rules(), calendar=load_calendar(), code_meta=code_meta)
        quality_result = checker.check(quotes_df, codes=codes)
    elif st == "ok":
        logger.warning("日线数据为空（窗口内无行情？）")

    # ---- 源可用性结论（含降级事件） ----
    events = read_new_failures(run_ts[:19])
    degraded = [e for e in events if e.get("level") == "info"]
    failed = [e for e in events if e.get("level") == "error"]

    # ---- 输出 market.json ----
    market = {
        "generated_at": run_ts,
        "fetch_window": {"start": str(start), "end": str(end)},
        "index": index_df.to_dict(orient="records") if index_df is not None else None,
        "breadth": breadth,
        "quality": {
            "summary": quality_result.summary() if quality_result else "数据质量：无数据",
            "issues": [vars(i) for i in quality_result.issues] if quality_result else [],
        },
        "sources": sources_status,
        "degradations": degraded,
        "failures": failed,
    }
    (out_dir / "market.json").write_text(
        json.dumps(market, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    logger.info("已写出 %s", out_dir / "market.json")

    # ---- 输出 stocks.json ----
    stocks = {
        "generated_at": run_ts,
        "fetch_window": {"start": str(start), "end": str(end)},
        "codes": codes,
        "quotes": (
            quality_result.df.to_dict(orient="records") if quality_result is not None else []
        ),
        "code_meta": {
            c: {"name": name_map.get(c), "board": None} for c in codes
        },
        "quality": market["quality"],
    }
    (out_dir / "stocks.json").write_text(
        json.dumps(stocks, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    logger.info("已写出 %s", out_dir / "stocks.json")

    # ---- 字段稳定性追加 ----
    obs = []
    if quotes_df is not None:
        obs.append({
            "interface": "get_daily_quotes", "status": st,
            "columns": list(quotes_df.columns), "rows": len(quotes_df),
            "nulls": int(quotes_df.isna().sum().sum()),
            "note": f"codes={codes} source={sorted(quotes_df['data_source'].unique().tolist()) if 'data_source' in quotes_df else 'n/a'}",
        })
    if index_df is not None:
        obs.append({
            "interface": "get_index_snapshot", "status": st2,
            "columns": list(index_df.columns), "rows": len(index_df),
            "nulls": int(index_df.isna().sum().sum()), "note": "",
        })
    if breadth:
        obs.append({
            "interface": "get_market_breadth", "status": st3,
            "columns": list(breadth.keys()), "rows": 1,
            "nulls": "n/a", "note": breadth.get("amount_note", ""),
        })
    append_field_observations(run_ts[:19], obs)

    # ---- 数据源可用性结论 ----
    print("\n===== 数据源可用性结论 =====")
    for name, s in sources_status.items():
        mark = "✅" if s["status"] == "ok" else "❌"
        print(f"  {mark} {name}: {s['status']}" + (f" 原因: {s['reason']}" if s["reason"] else ""))
    if degraded:
        print(f"  ⚠ 降级事件 {len(degraded)} 条：")
        for e in degraded:
            print(f"    - {e['source']}: {e['reason']}")
    if failed:
        print(f"  ❌ 失败事件 {len(failed)} 条：")
        for e in failed:
            print(f"    - {e['source']}: {e['reason'][:120]}")
    if all(s["status"] == "ok" for s in sources_status.values()):
        print("  结论：全部源可用")
    else:
        print("  结论：存在源不可用（详见上）")


if __name__ == "__main__":
    main()
