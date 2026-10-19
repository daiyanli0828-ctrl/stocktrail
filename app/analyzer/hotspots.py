# -*- coding: utf-8 -*-
"""
热点段：规则初筛 + 带 prediction 结论（P-02；企划 v1.0 §2.5 M2 C6 / C1.2）
数据源降级链：东财概念板块（主）→ 新浪行业板块（兜底，直连可用）→ None（诚实降级，不编造）。

规则初筛：板块按当日涨幅排序取 Top N（涨幅/成交额作为资金集中度描述，无振幅/换手/连板
梯队数据源时以涨幅 + 成交额为 M0 后最小规则集，见 03 规格 P-02）。

每条候选结论强制携带 prediction（C6）：direction=up / target_scope=板块名 / deadline=next_day，
写入 notes(kind='hypothesis') 供次日 verify_yesterday.py 验证（验证端支持板块 spot 涨跌幅）。

日报呈现只描述市场结构（C1.2）：资金向哪些板块集中、集中度多高，不输出个股/可买清单。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DB = PROJECT_ROOT / "data" / "processed" / "app.db"

TOP_N = 3                       # 候选板块数（规则初筛 Top N）
CONCENTRATION_TH = 0.30         # 集中度阈值：Top1 成交额占比 > 30% 描述为"高度集中"


def fetch_sector_data() -> pd.DataFrame | None:
    """板块行情（东财概念主源 → 新浪行业兜底）。返回统一列：
    sector(板块名) / pct_chg(涨跌幅%) / amount(成交额) / count(公司家数)。"""
    import akshare as ak

    # 主源：东财概念板块
    try:
        df = ak.stock_board_concept_name_em()
        out = pd.DataFrame({
            "sector": df["板块名称"].astype(str),
            "pct_chg": pd.to_numeric(df["涨跌幅"], errors="coerce"),
            "amount": pd.to_numeric(df["成交额"], errors="coerce"),
            "count": pd.to_numeric(df["公司家数"], errors="coerce"),
        })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[hotspots] 东财概念板块失败，降级新浪行业：%s", str(exc)[:100])

    # 兜底：新浪行业板块（直连可用）
    try:
        df = ak.stock_sector_spot(indicator="新浪行业")
        out = pd.DataFrame({
            "sector": df["板块"].astype(str),
            "pct_chg": pd.to_numeric(df["涨跌幅"], errors="coerce"),
            "amount": pd.to_numeric(df["总成交额"], errors="coerce"),
            "count": pd.to_numeric(df["公司家数"], errors="coerce"),
        })
        return out
    except Exception as exc:  # noqa: BLE001
        logger.warning("[hotspots] 新浪行业板块失败：%s", str(exc)[:100])
    return None


def rank_candidates(df: pd.DataFrame, top_n: int = TOP_N) -> pd.DataFrame:
    """规则初筛：按当日涨幅排序取 Top N（NaN 涨幅剔除）。"""
    d = df.dropna(subset=["pct_chg"]).copy()
    return d.sort_values("pct_chg", ascending=False).head(top_n)


def market_structure(df: pd.DataFrame) -> str:
    """市场结构一句话（C1.2：只描述资金集中度，不输出标的）。
    CHG-017：领涨/领跌明细改由「板块行业观察」（同花顺行业）承担，此处不再重复领涨方向。"""
    total = float(df["amount"].sum()) or 1.0
    top1 = df.sort_values("amount", ascending=False).iloc[0]
    top1_share = float(top1["amount"]) / total
    level = "高度集中" if top1_share > CONCENTRATION_TH else "相对分散"
    return (
        f"资金集中度{level}：成交额最高的板块为「{top1['sector']}」"
        f"（占两市板块成交 {top1_share*100:.0f}%）"
    )


def generate_conclusions(candidates: pd.DataFrame, trade_date: str,
                         db_path: Path = DB) -> list[dict]:
    """候选板块 → 带 prediction 的结论（C6），写入 notes(kind='hypothesis')。"""
    conn = sqlite3.connect(db_path)
    records = []
    try:
        for _, r in candidates.iterrows():
            sector = str(r["sector"])
            pct = float(r["pct_chg"])
            amount = float(r["amount"])
            rec = {
                "date": trade_date,
                "conclusion": f"行业「{sector}」当日涨幅 {pct:+.2f}%，次日延续（预测）",
                "logic": f"当日涨幅 {pct:+.2f}%，成交额 {amount/1e8:.0f} 亿元，公司 {int(r['count'])} 家（规则初筛 Top{TOP_N}）",
                "prediction": {
                    "direction": "up",
                    "target_scope": sector,
                    "deadline": "next_day",
                    "criterion_id": "price_direction",
                },
                "source": "hotspot_rule",
                "verified_at": datetime.now(timezone.utc).isoformat(),
            }
            records.append(rec)
            conn.execute(
                "INSERT INTO notes(ts, kind, content) VALUES(?, 'hypothesis', ?)",
                (rec["verified_at"], json.dumps(rec, ensure_ascii=False)),
            )
        conn.commit()
    finally:
        conn.close()
    return records


def hotspot_block(candidates: pd.DataFrame, structure: str) -> str:
    """晚报"热点结构"段文本（C1.2：只描述资金集中度；领涨/领跌明细由板块行业观察承担，CHG-017）。"""
    return f"- {structure}"
