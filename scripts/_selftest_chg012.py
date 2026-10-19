# -*- coding: utf-8 -*-
"""CHG-012 自测（临时）：自选自动补全 + 持仓推算/合并。"""
import importlib.util
import sys
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


DB = ROOT / "data" / "processed" / "app.db"

# ---- 1. 持仓推算：真实 app.db（8/31 全卖出）→ 净买入为空 ----
from app.analyzer.positions import compute_positions_from_trades, effective_positions  # noqa: E402
auto = compute_positions_from_trades(DB)
print("auto positions:", auto)
check("8/31 全卖出 → 自动持仓为空", auto == [], str(auto))

# ---- 2. 合并：手动覆盖/补充 ----
eff = effective_positions(DB, [{"code": "600519", "shares": 100, "avg_cost": 1500.0,
                                "name": "贵州茅台", "stop_loss": 1380.0}])
check("手动项进入有效持仓", len(eff) == 1 and eff[0]["code"] == "600519"
      and eff[0]["shares"] == 100 and eff[0]["stop_loss"] == 1380.0, str(eff))

# ---- 3. 用临时库验证买入推算 ----
import sqlite3  # noqa: E402
tmp = ROOT / "data" / "processed" / "test" / "chg012_tmp.db"
tmp.parent.mkdir(parents=True, exist_ok=True)
tmp.unlink(missing_ok=True)
conn = sqlite3.connect(tmp)
conn.execute("CREATE TABLE trades(id INTEGER PRIMARY KEY, code TEXT, side TEXT, price REAL, shares REAL, ts TEXT,"
             " reason_type TEXT, reason_note TEXT, plan_target REAL, stop_loss REAL, reason_recorded_at TEXT,"
             " post_hoc INTEGER, holding_days REAL, pnl REAL)")
conn.execute("INSERT INTO trades(code,side,price,shares,ts) VALUES('600519','buy',1500.0,100,'2026-08-01T00:00:00+00:00')")
conn.execute("INSERT INTO trades(code,side,price,shares,ts) VALUES('600519','buy',1600.0,100,'2026-08-02T00:00:00+00:00')")
conn.execute("INSERT INTO trades(code,side,price,shares,ts) VALUES('600519','sell',1650.0,50,'2026-08-28T00:00:00+00:00')")
conn.commit(); conn.close()
auto2 = compute_positions_from_trades(tmp)
check("买入推算：净股数 150（200-50）、成本 1550（加权）",
      len(auto2) == 1 and auto2[0]["shares"] == 150 and auto2[0]["avg_cost"] == 1550.0, str(auto2))
check("opened_at = 最早买入日", auto2[0]["opened_at"] == "2026-08-01", str(auto2[0]))
tmp.unlink(missing_ok=True)

# ---- 4. search_ticker（真实同花顺）----
from app.collectors.ths_src import ThsSource  # noqa: E402
ths = ThsSource()
by_name = ths.search_ticker("贵州茅台")
by_code = ths.search_ticker("600519")
check("search 名称→600519/贵州茅台", any(i["code"] == "600519" and i["name"] == "贵州茅台" for i in by_name), str(by_name))
check("search 代码→600519/贵州茅台", any(i["code"] == "600519" and i["name"] == "贵州茅台" for i in by_code), str(by_code))

# ---- 5. web_ui 路由 ----
spec = importlib.util.spec_from_file_location("web_ui", str(ROOT / "scripts" / "web_ui.py"))
wui = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wui)
paths = {getattr(r, "path", "") for r in wui.app.routes}
for p in ["/positions", "/api/stock/lookup"]:
    check(f"web_ui 路由 {p} 已注册", p in paths, f"paths={sorted(paths)}")

print(f"\n===== {ok} OK / {fail} FAIL =====")
sys.exit(1 if fail else 0)
