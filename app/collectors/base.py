# -*- coding: utf-8 -*-
"""
DataSourceAdapter 统一接口（架构 v0.6 §1 数据源适配层 / §3 降级链）
M0 唯一实现：akshare（app/collectors/akshare_src.py）

统一输出 Schema 见 DAILY_QUOTE_COLUMNS（对应 quotes 表字段，F-01）：
  code, trade_date, open, high, low, close, adj_close, volume, amount, pct_chg
- close / open / high / low：不复权价（展示用，D1）
- adj_close：后复权价（指标计算用，D1）
- trade_date：A 股交易日，'YYYY-MM-DD' 文本（D4 全库 UTC + 显式 trade_date 归属）

频率控制（架构 §3）：请求间隔 >= rate_limit_sec（默认 0.5s），失败指数退避重试；
代理模式降级链：按 config/network.proxy 配置的主模式尝试，失败自动降级 direct；
重试耗尽后记录失败（source + 原因）到 data/processed/failures.log（JSONL）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]

DEFAULT_RATE_LIMIT_SEC = 0.5        # 架构 §3：请求间隔下限 0.5s
DEFAULT_MAX_RETRIES = 3             # 单模式下单次请求最大重试次数
DEFAULT_BACKOFF_BASE_SEC = 2.0      # 失败指数退避基数（秒）

FAILURE_LOG = PROJECT_ROOT / "data" / "processed" / "failures.log"

# 统一日线输出列（quotes 表字段子集；quality_flag 由质量层 F-03 补充）
DAILY_QUOTE_COLUMNS = [
    "code", "trade_date", "open", "high", "low",
    "close", "adj_close", "volume", "amount", "pct_chg",
]

logger = logging.getLogger(__name__)


def _apply_proxy_mode(mode: str) -> None:
    """按代理模式设置 requests 环境变量（每次请求前调用，生效于下一次请求）。

    mode:
      auto   —— 系统代理（Windows 读注册表；不注入/不清理用户已有 env）
      direct —— 绕过一切代理直连（NO_PROXY='*'）
      其它   —— 视为显式 HTTP 代理 URL（如 http://127.0.0.1:7897）
    """
    os.environ.pop("NO_PROXY", None)
    os.environ.pop("no_proxy", None)
    if mode == "direct":
        os.environ["NO_PROXY"] = "*"
    elif mode != "auto":
        os.environ["HTTP_PROXY"] = mode
        os.environ["HTTPS_PROXY"] = mode
    else:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("http_proxy", None)
        os.environ.pop("https_proxy", None)


class DataSourceAdapter(ABC):
    """数据源适配器基类：统一方法签名 + 频率控制 + 失败指数退避 + 代理降级 + 失败留痕。"""

    def __init__(
        self,
        rate_limit_sec: float = DEFAULT_RATE_LIMIT_SEC,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base_sec: float = DEFAULT_BACKOFF_BASE_SEC,
        proxy: str = "auto",
    ) -> None:
        self.rate_limit_sec = rate_limit_sec
        self.max_retries = max_retries
        self.backoff_base_sec = backoff_base_sec
        # 代理降级链：主模式优先，失败后尝试 direct；显式代理失败同样降级 direct
        self._proxy_modes: list[str] = [proxy] if proxy in ("auto", "direct") else [proxy, "direct"]
        if "direct" not in self._proxy_modes:
            self._proxy_modes.append("direct")
        self._last_request_ts = 0.0

    # ---------------- 统一接口 ----------------
    @abstractmethod
    def get_daily_quotes(self, codes: list[str], start: str, end: str) -> pd.DataFrame:
        """日线行情（含后复权 adj_close）。

        Args:
            codes: 股票代码列表（如 ["600519", "000001"]）
            start / end: 起始/结束日期，'YYYY-MM-DD' 或 'YYYYMMDD'

        Returns:
            DataFrame，列 = DAILY_QUOTE_COLUMNS（akshare 实现另附 data_source 列：
            eastmoney / sina，供来源追溯）；空数据返回空 DataFrame（同列）
        """
        raise NotImplementedError

    @abstractmethod
    def get_index_snapshot(self) -> pd.DataFrame:
        """主要指数快照。

        Returns:
            DataFrame：index_code, index_name, close, pct_chg, amount
        """
        raise NotImplementedError

    @abstractmethod
    def get_market_breadth(self) -> dict:
        """市场广度：上涨/下跌/平盘家数、涨停/跌停家数、两市成交额。

        Returns:
            dict：trade_date, up_count, down_count, flat_count,
                  limit_up, limit_down, total_amount, amount_unit, source
        """
        raise NotImplementedError

    # ---------------- 通用工具 ----------------
    def _rate_limit(self) -> None:
        """频率控制：距上次请求不足 rate_limit_sec 时休眠补齐（架构 §3）。"""
        elapsed = time.monotonic() - self._last_request_ts
        if elapsed < self.rate_limit_sec:
            time.sleep(self.rate_limit_sec - elapsed)
        self._last_request_ts = time.monotonic()

    def _request(self, fn, context: str):
        """执行一次带频率控制 + 指数退避 + 代理模式降级重试的请求。

        各代理模式依次尝试（每模式内指数退避重试），全部失败后把
        失败记录（source + 原因）写入 failures.log 并抛出原始异常。
        """
        last_err: Exception | None = None
        for mode in self._proxy_modes:
            _apply_proxy_mode(mode)
            for attempt in range(1, self.max_retries + 1):
                self._rate_limit()
                try:
                    return fn()
                except Exception as exc:  # noqa: BLE001 —— 网络/解析类异常统一重试
                    last_err = exc
                    wait = self.backoff_base_sec * (2 ** (attempt - 1))
                    logger.warning(
                        "[%s] 模式=%s 第 %d/%d 次失败: %s；%.1fs 后重试",
                        context, mode, attempt, self.max_retries, exc, wait,
                    )
                    if attempt < self.max_retries:
                        time.sleep(wait)
        self._record_failure(context, str(last_err))
        raise last_err

    @staticmethod
    def _record_failure(source: str, reason: str, level: str = "error") -> None:
        """失败/事件留痕：source + 原因（F-02 规格；供 F-04 数据源可用性结论）。

        level: error=采集失败；info=降级/兜底事件（如东财失败新浪兜底成功）
        """
        try:
            FAILURE_LOG.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": datetime.now(timezone.utc).isoformat(),
                "source": source,
                "reason": reason,
                "level": level,
            }
            with FAILURE_LOG.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 —— 留痕本身失败不阻断主流程
            logger.error("失败留痕写入失败: %s", exc)
        (logger.error if level == "error" else logger.info)("[%s留痕] %s: %s", level, source, reason)
