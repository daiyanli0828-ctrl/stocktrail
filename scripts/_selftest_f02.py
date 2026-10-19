# -*- coding: utf-8 -*-
"""F-02 自测：3 只股票日线 + 除权日不跳变 + 指数快照 + 市场广度 + 跨源核对（验证后删除）"""
import logging
import os
import sys
from pathlib import Path

os.environ["TQDM_DISABLE"] = "1"  # akshare 新浪接口 tqdm 进度条关闭
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
import pandas as pd  # noqa: E402
import akshare as ak  # noqa: E402

from app.collectors.akshare_src import AkshareSource  # noqa: E402

pd.set_option("display.width", 220)
pd.set_option("display.max_columns", 50)

src = AkshareSource(rate_limit_sec=0.5, max_retries=2, backoff_base_sec=1.5)

print("========== 1. 日线：3 只股票 × 近 10 个交易日 ==========")
codes = ["600519", "000001", "300750"]
df = src.get_daily_quotes(codes, "2026-08-17", "2026-08-31")
print("shape:", df.shape)
print(df.head(12).to_string())
print("\n列:", list(df.columns))
print("close/adj_close 缺失:", int(df["close"].isna().sum()), int(df["adj_close"].isna().sum()))
print("每只股票行数:")
print(df.groupby("code").size().to_string())

print("\n========== 2. 除权日不跳变检测（600519 长窗口 2026-05-01 ~ 2026-08-31） ==========")
w = src.get_daily_quotes(["600519"], "2026-05-01", "2026-08-31").sort_values("trade_date").reset_index(drop=True)
w["raw_pct"] = w["pct_chg"]
w["adj_pct"] = w["adj_close"].pct_change() * 100
# 除权日特征：不复权涨跌幅与后复权涨跌幅分歧 > 0.2%
w["diverg"] = (w["raw_pct"] - w["adj_pct"]).abs()
ex_dates = w[w["diverg"] > 0.2]
print(f"检测到疑似除权日 {len(ex_dates)} 天：")
print(ex_dates[["trade_date", "close", "adj_close", "raw_pct", "adj_pct", "diverg"]].to_string())
for _, r in ex_dates.iterrows():
    # 除权日 adj_close 不得跳变（|adj_pct| 应显著小于 |raw_pct| 且非异常大）
    ok = abs(r["adj_pct"]) < abs(r["raw_pct"]) + 1.0 and abs(r["adj_pct"]) < 11.0
    print(f"  {r['trade_date']}: adj 变动 {r['adj_pct']:+.2f}% (raw {r['raw_pct']:+.2f}%) -> {'OK 不跳变' if ok else 'FAIL 跳变!'}")
if len(ex_dates) == 0:
    print("（窗口内无除权日，连续性检查退化为全序列无异常跳变检查）")
    jumps = w[w["adj_pct"].abs() > 11.0]
    print(f"adj_close 单日跳变 >11% 的天数: {len(jumps)}", "-> OK" if len(jumps) == 0 else "-> FAIL")

print("\n========== 3. 指数快照 ==========")
idx = src.get_index_snapshot()
print(idx.to_string())

print("\n========== 4. 市场广度 ==========")
try:
    b = src.get_market_breadth()
    for k, v in b.items():
        print(f"  {k}: {v}")
except Exception as e:  # noqa: BLE001
    print("市场广度拉取失败:", type(e).__name__, str(e)[:200])

print("\n========== 5. 跨源核对（东财 vs 新浪；故障期则验证适配器归一化） ==========")
for code, sina_code in [("600519", "sh600519"), ("000001", "sz000001"), ("300750", "sz300750")]:
    try:
        s = ak.stock_zh_a_daily(symbol=sina_code, start_date="20260824", end_date="20260828", adjust="")
        em = df[df["code"] == code][["trade_date", "close", "pct_chg"]]
        s["ds"] = s["date"].astype(str).str[:10]
        m = s.merge(em, left_on="ds", right_on="trade_date", suffixes=("_sina", "_adapter"))
        for _, r in m.iterrows():
            dc = abs(r["close_sina"] - r["close_adapter"])
            ok = dc < 0.01
            print(f"  {code} {r['trade_date']}: sina close={r['close_sina']} adapter close={r['close_adapter']} diff={dc:.4f} -> {'OK' if ok else 'FAIL'}")
    except Exception as e:  # noqa: BLE001
        print(f"  {code} 跨源核对失败: {type(e).__name__} {str(e)[:150]}")
