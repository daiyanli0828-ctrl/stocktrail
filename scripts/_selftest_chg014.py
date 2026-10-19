# -*- coding: utf-8 -*-
"""CHG-014 自测：异动观察（进日报）+ 新闻快讯（独立，盘中/盘后）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ok = fail = 0


def check(name, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
        print(f"OK   {name}")
    else:
        fail += 1
        print(f"FAIL {name}  {detail}")


from app.collectors.ths_src import ThsSource  # noqa: E402
from app.collectors.cls_src import fetch_telegraph_raw, accumulate_telegraphs, load_telegraphs  # noqa: E402
from app.analyzer.anomalies import emotion_overview, watchlist_anomalies, lhb_line  # noqa: E402
from app.analyzer.news import build_intraday_view, build_afterhours_view  # noqa: E402

# 1. 异动：涨停池/情绪概览
ths = ThsSource()
up_pool = ths.get_limit_up_pool()
down = ths.get_anomaly_list("LIMIT_DOWN")
check("涨停池有数据", len(up_pool) > 0)
emo = emotion_overview(up_pool, len(down))
check("情绪概览含涨停/跌停/非荐股", "涨停" in emo and "跌停" in emo and "非荐股" in emo, emo)
_lhb = lhb_line("2026-09-01")
check("龙虎榜行", _lhb is not None and "非荐股" in _lhb, str(_lhb))
wl = watchlist_anomalies([up_pool[0]["code"]], {up_pool[0]["code"]: up_pool[0]["name"]}, up_pool, down)
check("自选相关异动命中", len(wl) >= 1 and "涨停" in wl[0])

# 2. 新闻：抓取 + 累积去重
raw = fetch_telegraph_raw()
check("财联社抓取", len(raw) > 0, f"n={len(raw)}")
check("快讯字段完整", raw and "title" in raw[0] and "url" in raw[0] and "id" in raw[0])
acc1 = accumulate_telegraphs()
acc2 = accumulate_telegraphs()
check("累积去重（两次抓取无重复 id）", len(acc2) == len({t['id'] for t in acc2}),
      f"n1={len(acc1)} n2={len(acc2)}")
check("累积文件可读", len(load_telegraphs("2026-09-01")) >= 1)

# 3. 新闻视图
iv = build_intraday_view(acc2)
ah = build_afterhours_view(acc2, {"600519": "贵州茅台"}, ["600519"])
check("盘中视图", iv is not None and "cls.cn/detail" in iv)
check("盘后视图", ah is not None and "cls.cn/detail" in ah)

print(f"\n===== {ok} OK / {fail} FAIL =====")
sys.exit(1 if fail else 0)
