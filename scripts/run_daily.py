#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_daily.py：晚间主报生成（F-06；路线图第 1 周）
流程：采集（F-02）→ 质量检查（F-03）→ 账户体检（F-05）→ 自选异动/明日计划 → daily.md 渲染落盘
手动运行（C18：M0 不接调度）。

用法：python scripts/run_daily.py [--days 120] [--today YYYY-MM-DD]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
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
from app.quality.checks import QualityChecker, load_calendar, load_rules, persist_issues  # noqa: E402
from app.analyzer.portfolio_health import compute_health, health_lines  # noqa: E402
from app.analyzer.positions import effective_positions  # noqa: E402  (CHG-012 交易记录推算持仓)
from app.review.stats import freq_line, trade_frequency  # noqa: E402
from app.summarize.render_daily import render_daily, write_daily  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("run_daily")

CONFIG = PROJECT_ROOT / "config" / "data_sources.yaml"
DB = PROJECT_ROOT / "data" / "processed" / "app.db"
STRATEGY_CFG = PROJECT_ROOT / "config" / "strategy.yaml"   # 交易策略库配置（CHG-010）
STRATEGY_DIR = PROJECT_ROOT / "data" / "strategies"        # 策略 md 空间（用户读写）


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_analysis_date(calendar: set[str], now: datetime | None = None) -> date | None:
    """分析日期 = 最近一个「数据已收盘」的交易日（CHG-010 标题口径 + CHG-016 盘中拒生成）。
    - 交易时段（9:30-15:00，北京）内且今天是交易日 → 返回 None：当日实时数据未收盘，
      若继续生成会把当日的盘中半截数据（涨跌家数/异动/成交额）标成前一交易日，见 CHG-016；
    - 收盘后（≥15:00）且今天是交易日 → 今天；
    - 其它时刻（开盘前 / 隔夜 / 非交易日）→ 最近一个已收盘交易日（实时快照=其收盘）。
    例：9/1 00:30 → 8/31；9/3 10:29（盘中）→ None；9/3 22:00 → 9/3。now 仅供测试注入。"""
    bj = timezone(timedelta(hours=8))
    n = now.astimezone(bj) if now else datetime.now(bj)
    d = n.date()
    hm = n.strftime("%H:%M")
    if d.isoformat() in calendar and "09:30" <= hm < "15:00":
        return None   # 交易时段：当日数据未收盘
    start = d if n.hour >= 15 else d - timedelta(days=1)
    for _ in range(45):
        if start.isoformat() in calendar:
            return start
        start -= timedelta(days=1)
    return start


def load_active_strategy() -> dict | None:
    """读取启用的交易策略（config/strategy.yaml 的 strategy.active）→ {'name','content'}；
    未启用或策略文件缺失返回 None。"""
    if not STRATEGY_CFG.exists():
        return None
    cfg = load_yaml(STRATEGY_CFG)
    name = str((cfg.get("strategy", {}) or {}).get("active", "") or "").strip()
    if not name:
        return None
    f = STRATEGY_DIR / f"{name}.md"
    if not f.exists():
        logger.warning("策略 %s 已启用但文件缺失（%s）", name, f)
        return None
    return {"name": name, "content": f.read_text(encoding="utf-8")}


def build_market_summary(breadth: dict | None, index_df, moves: list[dict]) -> str:
    """拼装给 LLM 的当日市场摘要（事实数据，供策略框架对照）。"""
    parts: list[str] = []
    if breadth:
        parts.append(
            f"涨跌家数：上涨 {breadth.get('up_count', '—')} / 下跌 {breadth.get('down_count', '—')}"
            f" / 平盘 {breadth.get('flat_count', '—')}；涨停 {breadth.get('limit_up', '—')}"
            f" / 跌停 {breadth.get('limit_down', '—')}"
        )
        if breadth.get("total_amount") is not None:
            parts.append(f"两市成交额 {float(breadth['total_amount'])/1e8:.0f} 亿元")
    if index_df is not None and not index_df.empty:
        idx = "、".join(
            f"{r.get('index_name', '')} {float(r.get('close', 0)):g}（{float(r.get('pct_chg', 0)):g}%）"
            for r in index_df.to_dict(orient="records")
        )
        if idx:
            parts.append(f"指数：{idx}")
    if moves:
        mv = "；".join(
            f"{m.get('code', '')} {m.get('name') or ''} 收盘 {m['close']:g}"
            f"（{m['pct_chg']:g}%）{m.get('state', '')}"
            for m in moves
        )
        parts.append(f"自选异动：{mv}")
    return "\n".join(parts)


def persist_quotes(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """quotes 表 upsert（INSERT OR REPLACE，含 quality_flag）。返回写入行数。"""
    if df is None or df.empty:
        return 0
    cols = ["code", "trade_date", "open", "high", "low", "close",
            "adj_close", "volume", "amount", "pct_chg", "quality_flag"]
    rows = [tuple(r[c] for c in cols) for _, r in df.iterrows()]
    conn.executemany(
        f"INSERT OR REPLACE INTO quotes({','.join(cols)}) VALUES({','.join('?' * len(cols))})",
        rows,
    )
    conn.commit()
    return len(rows)


def latest_closes(df: pd.DataFrame) -> dict[str, float]:
    """每代码最新收盘价。"""
    out = {}
    for code, g in df.groupby("code"):
        g = g.sort_values("trade_date")
        out[code] = float(g.iloc[-1]["close"])
    return out


def watchlist_moves(df: pd.DataFrame, names: dict[str, str]) -> list[dict]:
    """自选异动：状态/风险的事实描述（C1.2：不含可买标的清单，无指令式用语）。
    MA20 计算用 adj_close（D1），展示与比较换算回不复权刻度（× close/adj_close）。"""
    moves = []
    for code, g in df.groupby("code"):
        g = g.sort_values("trade_date").reset_index(drop=True)
        if len(g) < 20:
            continue
        last = g.iloc[-1]
        close = float(last["close"])
        adj = float(last["adj_close"])
        pct = float(last["pct_chg"])
        ma20_adj = float(g["adj_close"].tail(20).mean())
        # 换算回不复权刻度：adj 与 raw 同刻度的比例因子 = adj_close/close（当日内近似）
        ma20 = ma20_adj * (close / adj) if adj else ma20_adj
        vol5 = float(g["volume"].tail(5).mean()) if len(g) >= 5 else float(g["volume"].mean())
        vol_ratio = float(last["volume"]) / vol5 if vol5 else 0.0
        deviation = (close - ma20) / ma20 * 100 if ma20 else 0.0
        state = f"站上 MA20（{ma20:.2f}）" if close >= ma20 else f"跌破 MA20（{ma20:.2f}）"
        if vol_ratio >= 1.5:
            state += "，放量"
        elif vol_ratio <= 0.7:
            state += "，缩量"
        if deviation > 10:
            risk = f"偏离 MA20 达 +{deviation:.1f}%，短期乖离较大"
        elif deviation < -5:
            risk = f"运行于 MA20 下方 {abs(deviation):.1f}%，弱势状态"
        else:
            risk = f"偏离 MA20 {deviation:+.1f}%（正常范围）"
        moves.append({
            "code": code, "name": names.get(code, ""), "close": close,
            "pct_chg": pct, "state": state, "risk": risk,
        })
    return moves


def tomorrow_plan(positions_cfg: dict, closes: dict[str, float], ma20: dict[str, float]) -> dict:
    """条件式明日计划：持仓收盘跌破 MA20（不复权刻度）→ 风险关注；无触发 → 今日无需操作（C1.3）。"""
    lines = []
    for p in positions_cfg.get("positions", []) or []:
        code = str(p["code"])
        if code in closes and code in ma20 and closes[code] < ma20[code]:
            lines.append(
                f"持仓 {code}：收盘 {closes[code]:.2f} 低于 20 日线（MA20 {ma20[code]:.2f}）"
                "→ 风险关注（观察点：MA20 是否收复 / 量能是否萎缩）"
            )
    return {"lines": lines, "no_action": not lines}


def yesterday_verification(conn: sqlite3.Connection, today: date, calendar: set[str]) -> dict | None:
    """昨日验证（F-09）：前一交易日的 hypothesis 记录 → 日报"昨日验证"段。"""
    prev = today
    for _ in range(10):
        prev -= timedelta(days=1)
        if prev.isoformat() in calendar:
            break
    rows = conn.execute("SELECT content FROM notes WHERE kind='hypothesis'").fetchall()
    for (content,) in rows:
        try:
            rec = json.loads(content)
        except json.JSONDecodeError:
            continue
        if str(rec.get("date"))[:10] == prev.isoformat():
            return {
                "conclusion": rec.get("conclusion", ""),
                "verdict": rec.get("verdict", ""),
                "criterion": rec.get("criterion", ""),
                "rule_version": rec.get("rule_version"),
            }
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 晚间主报生成")
    parser.add_argument("--days", type=int, default=120, help="回看窗口（自然日）")
    parser.add_argument("--today", type=str, default="", help="指定交易日 YYYY-MM-DD（默认今天）")
    parser.add_argument("--positions", type=str, default="", help="持仓文件路径（默认 data/inputs/positions.yaml）")
    parser.add_argument("--source", choices=["ths", "akshare"], default=None,
                        help="日线/指数/广度数据源（默认读 config fetch.sources 首个；失败自动降级 akshare）")
    parser.add_argument("--force", action="store_true", help="跳过休市日检查（P-06 计划任务/补跑用）")
    args = parser.parse_args()

    calendar = load_calendar()
    if args.today:
        today = date.fromisoformat(args.today)   # 显式指定交易日（补跑/测试）
        # P-06：休市日跳过（本地交易日历，D3）——显式日期落在非交易日时直接退出
        if not args.force and today.isoformat() not in calendar:
            print(f"{today} 非交易日（休市），跳过日报生成")
            return
    else:
        # CHG-010/CHG-016：分析日期 = 最近已收盘交易日；盘中（9:30-15:00 交易日）当日数据未收盘 → 拒生成
        today = resolve_analysis_date(calendar)
        if today is None:
            print("市场交易中（9:30-15:00），当日数据未收盘；日报请于收盘后（15:00 后）生成")
            return
    start = today - timedelta(days=args.days)

    cfg = load_yaml(CONFIG)
    if args.source is None:   # 默认源从 config 读（CHG-014 修订：多选优先级列表，首个为主源）
        srcs = (cfg.get("fetch", {}) or {}).get("sources") or ["ths", "akshare"]
        args.source = srcs[0] if srcs else "ths"
    watchlist = load_yaml(PROJECT_ROOT / "data" / "inputs" / "watchlist.yaml").get("watchlist", [])
    codes = [str(w["code"]) for w in watchlist]
    names = {str(w["code"]): w.get("name", "") for w in watchlist}
    positions_path = Path(args.positions) if args.positions else PROJECT_ROOT / "data" / "inputs" / "positions.yaml"
    positions_cfg = load_yaml(positions_path)
    # CHG-012：有效持仓 = 交易记录自动推算（净买入）+ positions.yaml 手动覆盖（按 code）
    positions_cfg["positions"] = effective_positions(DB, positions_cfg.get("positions", []) or [])
    industries = load_yaml(PROJECT_ROOT / "data" / "inputs" / "industries.yaml").get("industries", {}) \
        if (PROJECT_ROOT / "data" / "inputs" / "industries.yaml").exists() else {}

    src = AkshareSource(
        config=cfg.get("fetch", {}).get("index_snapshot", {}),
        rate_limit_sec=cfg["sources"]["akshare"].get("rate_limit_sec", 0.5),
        max_retries=cfg["sources"]["akshare"].get("max_retries", 3),
        backoff_base_sec=cfg["sources"]["akshare"].get("backoff_base_sec", 2.0),
        proxy=cfg.get("network", {}).get("proxy", "auto"),
    )

    # ---- 采集 ----
    # 默认 ths（同花顺官方源，全链路日线+指数+广度）；失败降级 akshare（东财→新浪）
    use_ths = args.source == "ths"
    if use_ths:
        try:
            from app.collectors.ths_src import ThsSource
            ths_src = ThsSource(proxy=cfg.get("network", {}).get("proxy", "auto"))   # 与 akshare 同源 proxy 配置
            df = ths_src.get_daily_quotes(codes, str(start), str(today))
            index_df = ths_src.get_index_snapshot()
            breadth = ths_src.get_market_breadth()
            if df is None or df.empty:
                raise RuntimeError("ths 日线为空")
        except Exception as exc:  # noqa: BLE001 —— ths 失败降级 akshare
            logger.warning("ths 源失败（%s），降级 akshare", str(exc)[:120])
            use_ths = False
    if not use_ths:
        df = src.get_daily_quotes(codes, str(start), str(today))
        index_df = src.get_index_snapshot()
        breadth = src.get_market_breadth()

    # ---- 热点结构（P-02，C1.2：只描述市场结构，无标的清单） ----
    hotspot_text = None
    sector_df = None
    try:
        from app.analyzer.hotspots import fetch_sector_data, hotspot_block, market_structure, rank_candidates
        sector_df = fetch_sector_data()
        if sector_df is not None:
            hotspot_text = hotspot_block(rank_candidates(sector_df), market_structure(sector_df))
    except Exception as exc:  # noqa: BLE001
        logger.warning("热点段生成失败（不影响主报）: %s", str(exc)[:100])

    # ---- 异动观察（CHG-014：情绪概览 + 板块领涨/领跌 + 自选/持仓相关异动；纯规则，非荐股） ----
    anomaly_text = None
    try:
        from app.analyzer.anomalies import emotion_overview, watchlist_anomalies, lhb_line
        from app.analyzer.sectors import build_sector_block
        from app.collectors.ths_src import ThsSource
        ths_anom = ThsSource(proxy=cfg.get("network", {}).get("proxy", "auto"))
        limit_up_pool = ths_anom.get_limit_up_pool()
        limit_down = ths_anom.get_anomaly_list("LIMIT_DOWN")
        related_codes = list(dict.fromkeys(
            codes + [str(p["code"]) for p in positions_cfg.get("positions", []) or []]))
        related_names = dict(names)
        for p in positions_cfg.get("positions", []) or []:
            related_names.setdefault(str(p["code"]), str(p.get("name") or ""))
        alines = ["**异动观察**"]
        alines.append("- " + emotion_overview(limit_up_pool, len(limit_down)))
        sb = build_sector_block()   # CHG-017：板块行业观察（Top5 + 近2周趋势）
        if sb:
            alines.extend(sb.splitlines())
        lhb = lhb_line(today.isoformat())
        if lhb:
            alines.append("- " + lhb)
        wl = watchlist_anomalies(related_codes, related_names, limit_up_pool, limit_down)
        if wl:
            alines.append("- 自选/持仓相关异动：")
            alines.extend(wl)
        anomaly_text = "\n".join(alines)
    except Exception as exc:  # noqa: BLE001
        logger.warning("异动观察生成失败（不影响主报）: %s", str(exc)[:100])

    # ---- 质量检查 ----
    quality_summary = "数据质量：正常"
    if df is not None and not df.empty:
        checker = QualityChecker(rules=load_rules(), calendar=load_calendar())
        qr = checker.check(df, codes=codes)
        df = qr.df
        quality_summary = qr.summary()
        conn = sqlite3.connect(DB)
        persist_issues(qr, conn)
        conn.close()

    # ---- 落库 quotes ----
    conn = sqlite3.connect(DB)
    n = persist_quotes(conn, df)
    logger.info("quotes 落库 %d 行", n)

    # ---- 账户体检 ----
    closes = latest_closes(df) if df is not None else {}
    health = compute_health(positions_cfg, closes, industries, today=today)
    # 预格式化为 markdown 列表（嵌套项缩进；模板 join 渲染）
    h_lines = ["  " + ln if ln.startswith("  ") else "- " + ln for ln in health_lines(health)]

    # ---- 自选异动 / 明日计划 ----
    moves = watchlist_moves(df, names) if df is not None else []
    ma20 = {}
    if df is not None:
        for code, g in df.groupby("code"):
            g = g.sort_values("trade_date")
            if len(g) >= 20:
                adj = float(g.iloc[-1]["adj_close"])
                close = float(g.iloc[-1]["close"])
                ma20_adj = float(g["adj_close"].tail(20).mean())
                ma20[code] = ma20_adj * (close / adj) if adj else ma20_adj
    plan = tomorrow_plan(positions_cfg, closes, ma20)

    # ---- 交易频次行（C1.1 固定显示） + 昨日验证（F-09） ----
    total_shares = sum(float(p.get("shares", 0)) for p in positions_cfg.get("positions", []) or [])
    stats = trade_frequency(conn, today=today, total_shares=total_shares)
    stats["line"] = freq_line(stats)
    yesterday = yesterday_verification(conn, today, load_calendar())

    # ---- 交易复盘 LLM 分析（CHG-009；C13：LLM 段标记区分，复盘总结，无指令用语） ----
    # CHG-010：启用交易策略时，按策略框架对市场数据 + 交易记录做数据分析
    # CHG-013：并入当日不操作理由（认知感受；无交易也复盘，供纠偏）
    llm_analysis = None
    strategy_name = None
    try:
        from app.llm.client import analyze_trades, analyze_strategy, llm_enabled
        if llm_enabled():
            trades_rows = conn.execute(
                "SELECT id, code, side, price, shares, reason_note, holding_days, pnl"
                " FROM trades WHERE substr(ts,1,10)=? ORDER BY id", (today.isoformat(),)
            ).fetchall()
            trades_list = [{
                "id": r[0], "code": r[1], "side": r[2], "price": r[3], "shares": r[4],
                "reason": r[5], "holding_days": r[6], "pnl": r[7],
            } for r in trades_rows]
            cog_rows = conn.execute(
                "SELECT id, content FROM notes WHERE kind='cognition' AND substr(ts,1,10)=? ORDER BY id",
                (today.isoformat(),)
            ).fetchall()
            cognitions = "\n".join(f"(感受ID {r[0]}) {r[1]}" for r in cog_rows) if cog_rows else None
            strat = load_active_strategy()
            if strat:
                market_summary = build_market_summary(breadth, index_df, moves)
                llm_analysis = analyze_strategy(trades_list, market_summary, strat["name"], strat["content"],
                                                cognitions=cognitions)
                strategy_name = strat["name"]
            elif trades_list or cognitions:
                llm_analysis = analyze_trades(trades_list, cognitions=cognitions)
    except Exception as exc:  # noqa: BLE001 —— LLM 失败不影响主报
        logger.warning("LLM 交易复盘分析失败（不影响主报）: %s", str(exc)[:100])
    conn.close()

    # ---- 渲染落盘 ----
    bj = timezone(timedelta(hours=8))  # 北京时间（中国无夏令时）
    # 数据来源标注（P-12 评审 P-2：报头动态标注，不硬编码 akshare）
    data_source_label = "同花顺官方（fuyao.aicubes.cn）" if use_ths else "akshare"
    context = {
        "date": today.isoformat(),
        "weekday": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][today.weekday()],
        "generated_at": datetime.now(bj).strftime("%Y-%m-%d %H:%M（北京时间）"),
        "data_source": data_source_label,
        "market": {
            "breadth": breadth,
            "index": index_df.to_dict(orient="records") if index_df is not None else [],
            "hotspot": hotspot_text,
        },
        "anomaly": anomaly_text,
        "health_lines": h_lines,
        "watchlist": moves,
        "yesterday": yesterday,
        "llm_analysis": llm_analysis,
        "strategy_name": strategy_name,
        "tomorrow_plan": plan,
        "trade_stats": stats,
        "quality_summary": quality_summary,
    }
    markdown = render_daily(context)
    vault_dir = Path(cfg.get("output", {}).get("vault_dir", "data/processed/vault"))
    out_path = write_daily(markdown, vault_dir, trade_date=today)
    logger.info("daily.md 已生成: %s", out_path)
    print(f"REPORT_DATE={today.isoformat()} REPORT_FILE={out_path.name}")   # 供 web 一键生成解析跳转
    print(markdown)


if __name__ == "__main__":
    main()
