# -*- coding: utf-8 -*-
"""CHG-010 自测（临时）：标题按分析日期 + 交易策略库。"""
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ok = fail = 0

def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"OK   {name}")
    else:
        fail += 1
        print(f"FAIL {name}  {detail}")

# ---- 1. resolve_analysis_date（注入时刻） ----
from scripts.run_daily import resolve_analysis_date, load_active_strategy, build_market_summary  # noqa: E402

bj = timezone(timedelta(hours=8))
cal = {"2026-08-28", "2026-08-31", "2026-09-01", "2026-09-02"}

# 9/1 00:30（未收盘）→ 8/31
check("resolve 9/1 00:30 → 8/31",
      resolve_analysis_date(cal, datetime(2026, 9, 1, 0, 30, tzinfo=bj)).isoformat() == "2026-08-31")
# 9/1 16:00（已收盘）→ 9/1
check("resolve 9/1 16:00 → 9/1",
      resolve_analysis_date(cal, datetime(2026, 9, 1, 16, 0, tzinfo=bj)).isoformat() == "2026-09-01")
# 9/1 15:00 整 → 9/1（已收盘）
check("resolve 9/1 15:00 → 9/1",
      resolve_analysis_date(cal, datetime(2026, 9, 1, 15, 0, tzinfo=bj)).isoformat() == "2026-09-01")
# 周末（9/5 周六，非交易日）→ 9/4？ cal 里最新交易日为 9/2
check("resolve 9/5(周六) → 最近交易日",
      resolve_analysis_date(cal, datetime(2026, 9, 5, 10, 0, tzinfo=bj)).isoformat() == "2026-09-02")

# ---- 2. load_active_strategy ----
from scripts.run_daily import STRATEGY_CFG, STRATEGY_DIR, load_yaml  # noqa: E402
orig = load_yaml(STRATEGY_CFG)
orig_active = (orig.get("strategy", {}) or {}).get("active", "")
check("未启用策略时 load_active_strategy=None", load_active_strategy() is None,
      f"got {load_active_strategy()!r}")

# 临时启用「趋势跟踪」验证读取
try:
    import yaml
    cfg = load_yaml(STRATEGY_CFG)
    cfg.setdefault("strategy", {})["active"] = "趋势跟踪"
    yaml.safe_dump(cfg, STRATEGY_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    s = load_active_strategy()
    check("启用「趋势跟踪」读取 name+content", s is not None and s["name"] == "趋势跟踪"
          and "MA20" in s["content"], f"got {s['name'] if s else None}")
finally:
    cfg = load_yaml(STRATEGY_CFG)
    cfg.setdefault("strategy", {})["active"] = orig_active
    yaml.safe_dump(cfg, STRATEGY_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)

# ---- 3. build_market_summary ----
breadth = {"up_count": 3000, "down_count": 2000, "flat_count": 100,
           "limit_up": 50, "limit_down": 10, "total_amount": 1.2e12}
ms = build_market_summary(breadth, None, [{"code": "600519", "name": "贵州茅台", "close": 1500.0, "pct_chg": 1.2, "state": "站上 MA20"}])
check("build_market_summary 含涨跌家数", "上涨 3000" in ms and "涨停 50" in ms)
check("build_market_summary 含成交额亿元", "12000 亿元" in ms)
check("build_market_summary 含自选异动", "贵州茅台" in ms and "站上 MA20" in ms)

# ---- 4. LLM client ----
from app.llm.client import analyze_strategy, analyze_trades, _format_trades  # noqa: E402
check("analyze_strategy 空输入 → 今日无数据",
      analyze_strategy([], "", "趋势跟踪", "框架内容") == "今日无数据")
check("analyze_trades 空 → 今日无交易记录", analyze_trades([]) == "今日无交易记录")
fmt = _format_trades([{"id": 1, "code": "000636", "side": "sell", "price": 8.2, "shares": 1000,
                       "reason": "割肉", "holding_days": 3, "pnl": -1200}])
check("_format_trades 含记录ID/理由/持股/盈亏",
      "(记录ID 1)" in fmt and "理由：割肉" in fmt and "持股 3 天" in fmt and "盈亏 -1,200 元" in fmt,
      fmt)

# ---- 5. web_ui 策略路由 ----
spec = importlib.util.spec_from_file_location("web_ui", str(ROOT / "scripts" / "web_ui.py"))
wui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wui)
paths = {getattr(r, "path", "") for r in wui.app.routes}
for p in ["/strategy", "/strategy/view/{name}"]:
    check(f"web_ui 路由 {p} 已注册", p in paths, f"paths={sorted(paths)}")
check("_valid_strategy_name 拒绝穿越", not wui._valid_strategy_name("../x") and not wui._valid_strategy_name("a/b")
      and not wui._valid_strategy_name("") and wui._valid_strategy_name("趋势跟踪"))
check("策略库目录存在且含 4 个基础模板",
      STRATEGY_DIR.exists() and len(list(STRATEGY_DIR.glob("*.md"))) >= 4,
      f"files={[f.name for f in STRATEGY_DIR.glob('*.md')]}")

print(f"\n===== {ok} OK / {fail} FAIL =====")
sys.exit(1 if fail else 0)
