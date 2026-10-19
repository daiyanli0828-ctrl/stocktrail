# -*- coding: utf-8 -*-
"""
akshare 数据源（M0 唯一数据源，架构 §1/§3；企划 §2.5 M1）
实现 DataSourceAdapter 统一接口。akshare 为接口层，底层主备：
  - 日线：东财 stock_zh_a_hist（主）→ 新浪 stock_zh_a_daily（兜底，同属 akshare）
  - 指数快照：东财 stock_zh_index_spot_em（主）→ 新浪 stock_zh_index_spot_sina（兜底）
  - 市场广度：乐咕乐股 stock_market_activity_legu + 两市成交额（东财 spot 求和，主；
    新浪 上证指数+深证综指 成交额估算，兜底）

adj_close 说明（D1）：
    adj_close = 后复权收盘价（akshare adjust="hfq"）。后复权价以早期为锚点整体上推，
    除权日**不跳变**，用于全部指标计算；close / open / high / low / volume / amount 取
    不复权数据（展示用）。新浪 hfq 与东财 hfq 绝对刻度不同：新浪兜底窗口内自洽，
    与东财历史混用时刻度可能不连续（M0 已知局限，见 field_stability.md）。

C10 约束：本模块仅日频/收盘后接口，无实时/盘中接口。
"""
from __future__ import annotations

import logging

import akshare as ak
import pandas as pd

from app.collectors.base import DAILY_QUOTE_COLUMNS, DataSourceAdapter

logger = logging.getLogger(__name__)

# 东财不复权列名映射
_EM_RAW_COLS = {
    "日期": "trade_date", "开盘": "open", "收盘": "close", "最高": "high",
    "最低": "low", "成交量": "volume", "成交额": "amount", "涨跌幅": "pct_chg",
}
# 新浪日线列名映射（volume 单位为股，需 /100 转为手与东财一致）
_SINA_RAW_COLS = {
    "date": "trade_date", "open": "open", "close": "close", "high": "high",
    "low": "low", "volume": "volume", "amount": "amount",
}


def _to_sina_symbol(code: str) -> str:
    """6 位代码 → 新浪前缀代码（sh/sz/bj）。"""
    if code.startswith("6"):
        return f"sh{code}"
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    return f"sz{code}"


class AkshareSource(DataSourceAdapter):
    """akshare 实现（统一 Schema 见 base.DAILY_QUOTE_COLUMNS）。"""

    def __init__(self, config: dict | None = None, **kwargs) -> None:
        super().__init__(**kwargs)
        cfg = config or {}
        self.index_names: list[str] = cfg.get("index_names", ["上证指数", "深证成指", "创业板指", "沪深300"])

    # ================= 日线（含后复权 adj_close） =================
    def get_daily_quotes(self, codes: list[str], start: str, end: str) -> pd.DataFrame:
        start_em = start.replace("-", "")
        end_em = end.replace("-", "")
        frames: list[pd.DataFrame] = []
        for code in codes:
            try:
                raw, hfq, src = self._fetch_code(code, start_em, end_em)
            except Exception as exc:  # noqa: BLE001 —— 单只失败不阻断整批（留痕后跳过）
                logger.error("[akshare] %s 日线拉取失败（已跳过）: %s", code, exc)
                continue
            if raw is None or raw.empty:
                logger.warning("[akshare] %s 无日线数据 %s~%s", code, start, end)
                continue
            df = self._normalize(raw, hfq, code, src)
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=DAILY_QUOTE_COLUMNS)
        # data_source 附加列（eastmoney/sina），供数据来源追溯与稳定性清单
        return pd.concat(frames, ignore_index=True)[DAILY_QUOTE_COLUMNS + ["data_source"]]

    def _fetch_code(self, code: str, start_em: str, end_em: str):
        """单只股票日线：东财主路径，失败降级新浪（akshare 内部兜底）。"""
        try:
            raw = self._request(
                lambda c=code: ak.stock_zh_a_hist(
                    symbol=c, period="daily", start_date=start_em, end_date=end_em, adjust="",
                ),
                f"akshare.daily_raw:{code}",
            )
            hfq = self._request(
                lambda c=code: ak.stock_zh_a_hist(
                    symbol=c, period="daily", start_date=start_em, end_date=end_em,
                    adjust="hfq",  # 后复权（D1）
                ),
                f"akshare.daily_hfq:{code}",
            )
            return raw, hfq, "eastmoney"
        except Exception:  # noqa: BLE001 —— 东财失败 → 新浪兜底
            logger.warning("[akshare] %s 东财日线失败，降级新浪", code)
            symbol = _to_sina_symbol(code)
            raw = self._request(
                lambda s=symbol: ak.stock_zh_a_daily(
                    symbol=s, start_date=start_em, end_date=end_em, adjust="",
                ),
                f"akshare.daily_raw_sina:{code}",
            )
            hfq = self._request(
                lambda s=symbol: ak.stock_zh_a_daily(
                    symbol=s, start_date=start_em, end_date=end_em, adjust="hfq",
                ),
                f"akshare.daily_hfq_sina:{code}",
            )
            self._record_failure(f"akshare.daily_fallback:{code}", "东财失败，新浪兜底成功", level="info")
            return raw, hfq, "sina"

    @staticmethod
    def _normalize(raw: pd.DataFrame, hfq: pd.DataFrame, code: str, src: str) -> pd.DataFrame:
        """统一列名/单位 → DAILY_QUOTE_COLUMNS。"""
        is_sina = src == "sina"
        raw_cols = _SINA_RAW_COLS if is_sina else _EM_RAW_COLS
        df = raw.rename(columns=raw_cols).copy()
        if is_sina:
            df["trade_date"] = df["trade_date"].astype(str).str[:10]
            df["volume"] = pd.to_numeric(df["volume"], errors="coerce") / 100.0  # 股 → 手
            df["pct_chg"] = df["close"].pct_change() * 100.0
        else:
            df["trade_date"] = df["trade_date"].astype(str).str[:10]
        df["code"] = code
        df["data_source"] = src
        # 后复权收盘（adj_close）
        hfq_close = hfq.rename(columns={"日期": "trade_date", "收盘": "adj_close"})[
            ["trade_date", "adj_close"]
        ] if not is_sina else hfq.rename(columns={"date": "trade_date", "close": "adj_close"})[
            ["trade_date", "adj_close"]
        ]
        hfq_close["trade_date"] = hfq_close["trade_date"].astype(str).str[:10]
        if not hfq_close.empty:
            df = df.merge(hfq_close, on="trade_date", how="left")
        else:
            df["adj_close"] = df["close"]  # 兜底：无后复权时退化不复权
        # 新浪首行 pct_chg 因 pct_change 为 NaN → 置 0（无前收参考，质量层另行处理）
        df["pct_chg"] = df["pct_chg"].fillna(0.0)
        df["adj_close"] = df["adj_close"].fillna(df["close"])
        df = df[DAILY_QUOTE_COLUMNS + ["data_source"]]
        return df

    # ================= 指数快照 =================
    def get_index_snapshot(self) -> pd.DataFrame:
        try:
            df = self._request(lambda: ak.stock_zh_index_spot_em(), "akshare.index_spot")
            sub = df[df["名称"].isin(self.index_names)].copy()
            if sub.empty:
                raise ValueError(f"未匹配指数: {self.index_names}")
            sub["index_code"] = sub["代码"].astype(str).str.zfill(6)
            return pd.DataFrame({
                "index_code": sub["index_code"],
                "index_name": sub["名称"],
                "close": sub["最新价"].astype(float),
                "pct_chg": sub["涨跌幅"].astype(float),
                "amount": sub["成交额"].astype(float),
            })
        except Exception:  # noqa: BLE001 —— 东财失败 → 新浪兜底
            logger.warning("[akshare] 东财指数快照失败，降级新浪")
            sdf = self._request(lambda: ak.stock_zh_index_spot_sina(), "akshare.index_spot_sina")
            sub = sdf[sdf["名称"].isin(self.index_names)].copy()
            if sub.empty:
                raise ValueError(f"新浪未匹配指数: {self.index_names}")
            # 新浪存在重复指数（如沪深300 同时有 sh000300 / sz399300），按名称去重取首行
            sub = sub.drop_duplicates(subset=["名称"], keep="first")
            sub["index_code"] = sub["代码"].str[2:].str.zfill(6)
            self._record_failure("akshare.index_fallback", "东财指数失败，新浪兜底成功", level="info")
            return pd.DataFrame({
                "index_code": sub["index_code"],
                "index_name": sub["名称"],
                "close": sub["最新价"].astype(float),
                "pct_chg": sub["涨跌幅"].astype(float),
                "amount": sub["成交额"].astype(float),
            })

    # ================= 市场广度 =================
    def get_market_breadth(self) -> dict:
        # 乐咕乐股：上涨/下跌/涨停/跌停/平盘/停牌家数 + 统计日期（item/value 长表）
        act = self._request(lambda: ak.stock_market_activity_legu(), "akshare.market_activity")
        d = dict(zip(act["item"].astype(str), act["value"]))
        total_amount, amount_note = self._fetch_turnover()
        return {
            "trade_date": str(d.get("统计日期", ""))[:10],
            "up_count": int(d.get("上涨", 0)),
            "down_count": int(d.get("下跌", 0)),
            "flat_count": int(d.get("平盘", 0)),
            "limit_up": int(d.get("涨停", 0)),        # 含 ST
            "limit_down": int(d.get("跌停", 0)),      # 含 ST
            "limit_up_ex_st": int(d.get("真实涨停", 0)),    # 剔除 ST
            "limit_down_ex_st": int(d.get("真实跌停", 0)),  # 剔除 ST
            "total_amount": total_amount,
            "amount_unit": "元",
            "amount_note": amount_note,
            "source": "akshare",
        }

    def _fetch_turnover(self) -> tuple[float | None, str]:
        """两市成交额：东财全市场求和（主）→ 新浪 上证指数+深证综指 估算（兜底）。"""
        try:
            spot = self._request(lambda: ak.stock_zh_a_spot_em(), "akshare.spot_em")
            return float(pd.to_numeric(spot["成交额"], errors="coerce").sum()), "eastmoney 全市场求和"
        except Exception:  # noqa: BLE001
            logger.warning("[akshare] 东财成交额失败，降级新浪估算")
            sdf = self._request(lambda: ak.stock_zh_index_spot_sina(), "akshare.turnover_sina")
            sub = sdf[sdf["名称"].isin(["上证指数", "深证综指"])]
            if len(sub) < 2:
                return None, "成交额源失败（东财+新浪均不可用），今日缺"
            total = float(pd.to_numeric(sub["成交额"], errors="coerce").sum())
            self._record_failure("akshare.turnover_fallback", "东财成交额失败，新浪估算", level="info")
            return total, "新浪估算（上证指数+深证综指成交额）"
