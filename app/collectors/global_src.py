# -*- coding: utf-8 -*-
"""
全球指数源（F-10 盘前增量附页；企划 v1.0 §2.5 M1 C14 降级链）
降级链：东财 index_global_spot_em（主）→ 新浪 index_us_stock_sina（美股三大指数，兜底）
        → yfinance（仅代理可用时）→ 本地缓存 T-1 + 报头显式标注。
日期归属（D4）：美东交易日 → 北京次日归属；结果携带 us_date 与 beijing_date。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CACHE = PROJECT_ROOT / "data" / "processed" / "global_indices_cache.json"

logger = logging.getLogger(__name__)

# 期望输出的全球指标（名称与新浪代码）
EXPECTED = [
    {"name": "道琼斯", "sina": ".DJI"},
    {"name": "纳斯达克", "sina": ".IXIC"},
    {"name": "标普500", "sina": ".INX"},
    {"name": "日经225", "sina": None},
    {"name": "富时中国A50", "sina": None},
]


def _prev_us_trading_day(d: date) -> date:
    """美东最近一个已收盘交易日（北京凌晨归属）：输入为北京日期，美东当日未开盘，
    取前一日并跳过周末（美东节假日 M0 已知局限，D4 夏令时周人工核对）。"""
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


class GlobalIndicesSource:
    def __init__(self, cache_path: Path = DEFAULT_CACHE,
                 max_retries: int = 2, backoff_base_sec: float = 1.5) -> None:
        self.cache_path = cache_path
        self.max_retries = max_retries
        self.backoff_base_sec = backoff_base_sec

    # ---------------- 主入口 ----------------
    def fetch(self) -> dict:
        """按降级链获取全球指数。返回：
        {beijing_date, us_date, indices:[{name, close, pct_chg, source}],
         source, degraded:[...], t1_note: str|None}
        """
        beijing_date = date.today().isoformat()
        degraded: list[str] = []

        # 1. 东财主源
        try:
            result = self._fetch_em()
            self._save_cache(result)
            return result
        except Exception as exc:  # noqa: BLE001
            degraded.append(f"eastmoney 失败：{str(exc)[:100]}")
            logger.warning("[global] 东财全球指数失败：%s", exc)

        # 2. 新浪兜底（美股三大指数）
        try:
            result = self._fetch_sina()
            missing = [e["name"] for e in EXPECTED
                       if not any(i["name"] == e["name"] for i in result["indices"])]
            if missing:
                degraded.append(f"sina 兜底缺少：{'、'.join(missing)}")
            result["degraded"] = degraded + ["eastmoney 失败，新浪兜底"]
            self._save_cache(result)
            return result
        except Exception as exc:  # noqa: BLE001
            degraded.append(f"sina 失败：{str(exc)[:100]}")
            logger.warning("[global] 新浪全球指数失败：%s", exc)

        # 3. yfinance（仅代理可用时）
        if self._proxy_available():
            try:
                result = self._fetch_yfinance()
                result["degraded"] = degraded + ["eastmoney/sina 失败，yfinance 兜底"]
                self._save_cache(result)
                return result
            except Exception as exc:  # noqa: BLE001
                degraded.append(f"yfinance 失败：{str(exc)[:100]}")
        else:
            degraded.append("yfinance 未启用（无代理配置，C14）")

        # 4. 缓存 T-1 兜底
        cached = self._load_cache()
        if cached:
            cached["degraded"] = degraded
            failed_sources = "、".join(d.split("：")[0] for d in degraded[:2])
            cached["t1_note"] = (
                f"全球数据为 T-1（美东 {cached.get('us_date', '?')}），来源：{failed_sources}"
            )
            cached["beijing_date"] = beijing_date
            return cached
        return {
            "beijing_date": beijing_date, "us_date": None,
            "indices": [], "source": "none", "degraded": degraded,
            "t1_note": "全球数据不可用（所有来源失败且无缓存）",
        }

    # ---------------- 东财主源 ----------------
    def _fetch_em(self) -> dict:
        import akshare as ak

        df = self._retry(lambda: ak.index_global_spot_em())
        names = [e["name"] for e in EXPECTED if e["name"] != "富时中国A50"]
        sub = df[df["名称"].astype(str).isin(names)]
        indices = [
            {"name": str(r["名称"]), "close": float(r["最新价"]),
             "pct_chg": float(r["涨跌幅"]), "source": "eastmoney"}
            for _, r in sub.iterrows()
        ]
        # 富时中国A50：外盘期货接口（东财系，独立端点）
        a50 = self._try_a50()
        if a50:
            indices.append(a50)
        # 美东交易日：最近一个非周末交易日（周一盘前 → 上周五，D4）
        us_date = _prev_us_trading_day(date.today()).isoformat()
        return {"beijing_date": date.today().isoformat(), "us_date": us_date,
                "indices": indices, "source": "eastmoney", "degraded": []}

    # ---------------- 新浪兜底 ----------------
    def _fetch_sina(self) -> dict:
        import akshare as ak

        indices = []
        us_date = None
        for e in EXPECTED:
            if not e["sina"]:
                continue
            try:
                df = self._retry(lambda s=e["sina"]: ak.index_us_stock_sina(symbol=s))
                df = df.sort_values("date")
                last, prev = df.iloc[-1], df.iloc[-2]
                close = float(last["close"])
                pct = (close / float(prev["close"]) - 1) * 100
                us_date = str(last["date"])[:10]
                indices.append({"name": e["name"], "close": round(close, 2),
                                "pct_chg": round(pct, 2), "source": "sina"})
            except Exception as exc:  # noqa: BLE001
                logger.warning("[global] sina %s 失败：%s", e["name"], exc)
        # 富时中国A50：外盘期货接口兜底
        a50 = self._try_a50()
        if a50:
            a50["source"] = "eastmoney_futures"
            indices.append(a50)
        return {"beijing_date": date.today().isoformat(), "us_date": us_date,
                "indices": indices, "source": "sina", "degraded": []}

    def _try_a50(self) -> dict | None:
        """富时中国A50 当月连续（akshare futures_global_spot_em，独立端点）。"""
        import akshare as ak

        try:
            df = self._retry(lambda: ak.futures_global_spot_em())
            row = df[df["名称"].astype(str).str.contains("A50期指当月连续")]
            if row.empty:
                return None
            r = row.iloc[0]
            return {"name": "富时中国A50", "close": float(r["最新价"]),
                    "pct_chg": float(r["涨跌幅"]), "source": "eastmoney_futures"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("[global] A50 拉取失败：%s", exc)
            return None

    # ---------------- yfinance（代理可用时） ----------------
    @staticmethod
    def _proxy_available() -> bool:
        """C14：yfinance 仅在代理配置存在时启用（无 yfinance 默认直连）。"""
        cfg_path = PROJECT_ROOT / "config" / "data_sources.yaml"
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            proxy = cfg.get("network", {}).get("proxy", "auto")
            if proxy not in ("auto", "direct"):
                return True
        except Exception:  # noqa: BLE001
            pass
        return bool(os.environ.get("HTTPS_PROXY") or os.environ.get("http_proxy"))

    def _fetch_yfinance(self) -> dict:
        import yfinance as yf

        symbols = {".DJI": "^DJI", ".IXIC": "^IXIC", ".INX": "^GSPC", "日经": "^N225"}
        indices = []
        for e in EXPECTED:
            sym = symbols.get(e["sina"]) if e["sina"] else symbols.get(e["name"])
            if not sym:
                continue
            hist = yf.Ticker(sym).history(period="5d")
            if len(hist) < 2:
                continue
            last, prev = hist.iloc[-1], hist.iloc[-2]
            close = float(last["Close"])
            indices.append({"name": e["name"], "close": round(close, 2),
                            "pct_chg": round((close / float(prev["Close"]) - 1) * 100, 2),
                            "source": "yfinance"})
        us_date = str(hist.index[-1].date()) if indices else None
        return {"beijing_date": date.today().isoformat(), "us_date": us_date,
                "indices": indices, "source": "yfinance", "degraded": []}

    # ---------------- 缓存 ----------------
    def _save_cache(self, result: dict) -> None:
        if self.cache_path is None:   # 防御：显式 None 时不写缓存
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({k: result.get(k) for k in
                            ("beijing_date", "us_date", "indices", "source")},
                           ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[global] 缓存写入失败：%s", exc)

    def _load_cache(self) -> dict | None:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[global] 缓存读取失败：%s", exc)
        return None

    # ---------------- 工具 ----------------
    def _retry(self, fn):
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                return fn()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < self.max_retries:
                    import time
                    time.sleep(self.backoff_base_sec * attempt)
        raise last_err
