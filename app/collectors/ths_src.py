# -*- coding: utf-8 -*-
"""
同花顺金融数据源（fuyao.aicubes.cn，官方 A 股数据服务）
实现 DataSourceAdapter 统一接口（get_daily_quotes 完整；指数/广度按需补）。

数据源：https://fuyao.aicubes.cn  ·  认证：X-api-key header（config/secrets.yaml，.gitignore 忽略）
文档：/docs/api-reference/prices/

adj_close 说明（D1）：adjust=backward（后复权）的 close_price → adj_close，用于指标计算；
adjust=none 的 close/open/high/low 用于展示；pct_chg 由不复权 close 序列 pct_change 计算
（首行 0；除权日含跳空由 F-03 除权豁免兜底，与新浪兜底同口径）。
volume 单位为股（服务文档；东财为手、新浪为股——本适配器 /100 归一为手，
与 quotes 表 schema（init_db.py）及 akshare 适配器口径一致，见 field_stability.md）。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

from app.collectors.base import DAILY_QUOTE_COLUMNS, DataSourceAdapter

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SECRETS = PROJECT_ROOT / "config" / "secrets.yaml"


def _to_thscode(code: str) -> str:
    """6 位代码 → 带交易所后缀的 thscode（服务要求）。
    900xxx 为沪 B 股（上交所 .SH），非北交所（P-12 评审备注 4）。"""
    if code.startswith("6"):
        return f"{code}.SH"
    if code.startswith(("0", "3")):
        return f"{code}.SZ"
    if code.startswith("900"):
        return f"{code}.SH"      # 沪 B 股
    if code.startswith(("4", "8", "9")):
        return f"{code}.BJ"      # 北交所（43/83/87/92 段）
    return f"{code}.SH"


class ThsSource(DataSourceAdapter):
    """同花顺数据源（历史 K 线含后复权，单只请求，≤10 年窗口）。"""

    def __init__(self, secrets_path: Path = DEFAULT_SECRETS, proxy: str = "auto", **kwargs) -> None:
        super().__init__(proxy=proxy, **kwargs)   # proxy 与 AkshareSource 一致（备注 2）
        with open(secrets_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        ths = cfg.get("ths", {})
        self.base_url = ths.get("base_url", "https://fuyao.aicubes.cn").rstrip("/")
        self.api_key = ths.get("api_key", "")
        self.auth_header = ths.get("auth_header", "X-api-key")

    # ---------------- 统一接口 ----------------
    def get_daily_quotes(self, codes: list[str], start: str, end: str) -> pd.DataFrame:
        frames: list[pd.DataFrame] = []
        for code in codes:
            try:
                df = self._fetch_code(code, start, end)
            except Exception as exc:  # noqa: BLE001 —— 单只失败不阻断整批（留痕后跳过）
                logger.error("[ths] %s 日线拉取失败（已跳过）: %s", code, exc)
                continue
            if df is not None and not df.empty:
                frames.append(df)
        if not frames:
            return pd.DataFrame(columns=DAILY_QUOTE_COLUMNS)
        return pd.concat(frames, ignore_index=True)[DAILY_QUOTE_COLUMNS + ["data_source"]]

    def get_index_snapshot(self) -> pd.DataFrame:
        """主要指数快照（指数快照接口，批量；名称用固定映射，指数端点不返回 name）。"""
        index_codes = {
            "000001.SH": "上证指数", "399001.SZ": "深证成指",
            "399006.SZ": "创业板指", "000300.SH": "沪深300",
        }
        thscodes = ",".join(index_codes.keys())
        body = self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/a-share-index/prices/snapshot?thscodes={thscodes}"
            ),
            "ths.index_snapshot",
        )
        items = body.get("data", {}).get("item", []) or []
        rows = [{
            "index_code": (i.get("thscode") or i.get("ticker") or "").split(".")[0],
            "index_name": index_codes.get(i.get("thscode"), i.get("thscode", "")),
            "close": i.get("last_price"),
            "pct_chg": round(i.get("price_change_ratio_pct") or 0.0, 3),   # 展示 3 位小数（备注 1）
            "amount": i.get("turnover"),
        } for i in items]
        return pd.DataFrame(rows, columns=["index_code", "index_name", "close", "pct_chg", "amount"])

    def get_market_breadth(self) -> dict:
        """市场广度：全市场快照分页累计涨跌家数/成交额 + 涨停池/跌停异动计数。"""
        up = down = flat = 0
        total_amount = 0.0
        offset = 0
        page_size = 500
        while True:
            body = self._request(
                lambda o=offset: self._http_get(
                    f"{self.base_url}/api/a-share/prices/snapshot?limit={page_size}&offset={o}"
                ),
                f"ths.snapshot_page:{offset}",
            )
            items = body.get("data", {}).get("item", []) or []
            if not items:
                break
            for it in items:
                pct = it.get("price_change_ratio_pct") or 0
                if pct > 0:
                    up += 1
                elif pct < 0:
                    down += 1
                else:
                    flat += 1
                total_amount += it.get("turnover") or 0
            if len(items) < page_size:
                break
            offset += page_size

        limit_up = self._request(
            lambda: self._http_get(f"{self.base_url}/api/a-share/special-data/limit-up-pool?size=1"),
            "ths.limit_up_pool",
        ).get("data", {}).get("pagination", {}).get("total", 0)

        limit_down = len(self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/a-share/special-data/anomaly-analysis-list?tag_codes=LIMIT_DOWN"
            ),
            "ths.limit_down",
        ).get("data", {}).get("item", []) or [])

        return {
            "trade_date": date.today().isoformat(),
            "up_count": up, "down_count": down, "flat_count": flat,
            "limit_up": limit_up, "limit_down": limit_down,
            "total_amount": total_amount,
            "amount_unit": "元",
            "amount_note": "ths 全市场快照求和（沪深京 A 股）",
            "source": "ths",
        }

    def search_ticker(self, q: str) -> list[dict]:
        """标的检索（消歧，CHG-012）：按代码或名称搜索 → [{code, name, thscode}]（仅 A 股）。
        用于前端自选/持仓表单的「代码↔名称」自动补全。"""
        body = self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/meta/tickers/search?q={urllib.parse.quote(q)}"
            ),
            f"ths.search:{q}",
        )
        items = body.get("data", {}).get("item", []) or []
        out = []
        for it in items:
            if it.get("asset_type") != "a-share":
                continue
            ticker = str(it.get("ticker") or "")
            out.append({
                "code": ticker.zfill(6),
                "name": it.get("name") or "",
                "thscode": it.get("thscode") or "",
            })
        return out

    def search_index(self, q: str) -> list[dict]:
        """按名称检索指数/板块指数（CHG-017；asset_type=a-share-index，thscode 形如 881xxx.TI）
        → [{code, name, thscode}]。用于板块近 2 周趋势。"""
        body = self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/meta/tickers/search?q={urllib.parse.quote(q)}"
            ),
            f"ths.search_index:{q}",
        )
        items = body.get("data", {}).get("item", []) or []
        out = []
        for it in items:
            if it.get("asset_type") != "a-share-index":
                continue
            out.append({
                "code": str(it.get("ticker") or ""),
                "name": it.get("name") or "",
                "thscode": it.get("thscode") or "",
            })
        return out

    def get_index_history(self, thscode: str, days: int = 20) -> list[dict]:
        """指数/板块指数历史日K（CHG-017；近 days 自然日）→ [{date, close}] 升序。
        指数无复权语义，历史端点不带 adjust 参数。"""
        end_ms = self._to_ms(date.today().isoformat(), end_of_day=True)
        start_ms = end_ms - days * 86400 * 1000
        body = self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/a-share-index/prices/historical"
                f"?thscode={urllib.parse.quote(thscode)}&interval=1d&start={start_ms}&end={end_ms}"
            ),
            f"ths.index_hist:{thscode}",
        )
        items = body.get("data", {}).get("item", []) or []
        out = []
        for it in items:
            d = (datetime.fromtimestamp(it["date_ms"] / 1000, tz=timezone(timedelta(hours=8)))
                 .strftime("%Y-%m-%d"))
            out.append({"date": d, "close": it.get("close_price")})
        out.sort(key=lambda x: x["date"])
        return out

    def get_industry_catalog(self) -> dict[str, str]:
        """同花顺行业指数目录（CHG-017）：/catalog/ths-index-list?tag=industry
        过滤出主行业（881xxx，约 90 个）→ {name: thscode}。"""
        body = self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/a-share-index/catalog/ths-index-list?tag=industry"
            ),
            "ths.industry_catalog",
        )
        items = body.get("data", {}).get("item", []) or []
        out = {}
        for it in items:
            thscode = str(it.get("thscode") or "")
            name = str(it.get("name") or "")
            if thscode.startswith("881") and name:
                out[name] = thscode
        return out

    def get_limit_up_pool(self) -> list[dict]:
        """涨停池明细（CHG-014）：{code, name, pct_chg, reason, board_text, board_cnt}。
        分页（size 上限 200）：累计全部涨停股票。"""
        out: list[dict] = []
        page = 1
        while True:
            body = self._request(
                lambda p=page: self._http_get(
                    f"{self.base_url}/api/a-share/special-data/limit-up-pool?size=200&page={p}"
                ),
                f"ths.limit_up_pool_full:p{page}",
            )
            data = body.get("data") or {}
            items = data.get("item") or []
            for it in items:
                out.append({
                    "code": str(it.get("ticker") or "").zfill(6),
                    "name": it.get("name") or "",
                    "pct_chg": round(it.get("price_change_ratio_pct") or 0.0, 2),
                    "reason": it.get("limit_up_reason") or "",
                    "board_text": it.get("continue_day_text") or "",
                    "board_cnt": int(it.get("continue_day_cnt") or 0),
                })
            total = (data.get("pagination") or {}).get("total", 0)
            if not items or len(out) >= total or page > 20:
                break
            page += 1
        return out

    def get_anomaly_list(self, tag_code: str) -> list[dict]:
        """异动列表（CHG-014；tag_codes=LIMIT_UP / LIMIT_DOWN）：{code, name, reason, keywords}。"""
        body = self._request(
            lambda: self._http_get(
                f"{self.base_url}/api/a-share/special-data/anomaly-analysis-list?tag_codes={tag_code}"
            ),
            f"ths.anomaly:{tag_code}",
        )
        items = body.get("data", {}).get("item", []) or []
        out = []
        for it in items:
            thscode = str(it.get("thscode") or "")
            code = thscode.split(".")[0] if "." in thscode else thscode
            out.append({
                "code": code.zfill(6),
                "name": it.get("stock_name") or it.get("name") or "",
                "reason": it.get("analysis_content") or "",
                "keywords": it.get("keyword_list") or [],
            })
        return out

    # ---------------- 内部 ----------------
    def _fetch_code(self, code: str, start: str, end: str) -> pd.DataFrame:
        thscode = _to_thscode(code)
        start_ms, end_ms = self._to_ms(start), self._to_ms(end, end_of_day=True)
        none_df = self._request(
            lambda: self._historical(thscode, start_ms, end_ms, "none"),
            f"ths.historical_none:{code}",
        )
        backward_df = self._request(
            lambda: self._historical(thscode, start_ms, end_ms, "backward"),
            f"ths.historical_backward:{code}",
        )
        if none_df is None or none_df.empty:
            return pd.DataFrame(columns=DAILY_QUOTE_COLUMNS)
        df = none_df.rename(columns={
            "trade_date": "trade_date", "open_price": "open", "high_price": "high",
            "low_price": "low", "close_price": "close", "volume": "volume",
            "turnover": "amount",
        }).copy()
        df["code"] = code
        # ths volume 单位为股，quotes 表/akshare 统一为手（P-12 评审 P-1）→ /100 归一
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce") / 100.0
        df["pct_chg"] = df["close"].pct_change() * 100.0
        # 后复权收盘（adj_close）
        if backward_df is not None and not backward_df.empty:
            b = backward_df[["trade_date", "close_price"]].rename(columns={"close_price": "adj_close"})
            df = df.merge(b, on="trade_date", how="left")
        else:
            df["adj_close"] = df["close"]
        df["pct_chg"] = df["pct_chg"].fillna(0.0)
        df["adj_close"] = df["adj_close"].fillna(df["close"])
        df["data_source"] = "ths"
        return df[DAILY_QUOTE_COLUMNS + ["data_source"]]

    def _historical(self, thscode: str, start_ms: int, end_ms: int, adjust: str) -> pd.DataFrame:
        url = (
            f"{self.base_url}/api/a-share/prices/historical"
            f"?thscode={urllib.parse.quote(thscode)}&interval=1d"
            f"&start={start_ms}&end={end_ms}&adjust={adjust}"
        )
        body = self._http_get(url)
        data = body.get("data") or {}
        items = data.get("item") or []
        rows = [{
            "trade_date": datetime.fromtimestamp(r["date_ms"] / 1000, tz=timezone.utc)
            .astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
            "open_price": r.get("open_price"),
            "high_price": r.get("high_price"),
            "low_price": r.get("low_price"),
            "close_price": r.get("close_price"),
            "volume": r.get("volume"),
            "turnover": r.get("turnover"),
        } for r in items]
        return pd.DataFrame(rows)

    def _http_get(self, url: str) -> dict:
        req = urllib.request.Request(url, headers={self.auth_header: self.api_key,
                                                   "User-Agent": "GRGPZS/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            body = json.loads(r.read().decode("utf-8"))
        if body.get("code") not in (0, None):
            raise RuntimeError(f"ths API code={body.get('code')} msg={body.get('message')}")
        return body

    @staticmethod
    def _to_ms(day: str, end_of_day: bool = False) -> int:
        d = date.fromisoformat(day)
        dt = datetime(d.year, d.month, d.day, 23, 59, 59) if end_of_day else datetime(d.year, d.month, d.day)
        return int(dt.timestamp() * 1000)
