# -*- coding: utf-8 -*-
"""
质量检查三条（M0，架构 v0.6 §7 / 企划 v1.0 §7 D1/D2）：
  1. 价格非法：close <= 0 或 adj_close <= 0 → 拦截（block）
  2. 交易日缺数据：本地交易日历判定为交易日但无行情 → 标记（mark）+ 报告标注
  3. 涨跌幅超板块分档阈值：|pct_chg| > 板块阈值 → 标记 + 拦截（block，可复核）
     · 板块：ST 5% / 主板 10% / 创业板·科创板 20% / 退市整理 10% / 新股首日豁免
     · 除权日豁免（D1）：|adj 日变动% - close 日变动%| > 阈值 → 跳过涨跌幅检查

约束：阈值只读 config/quality_rules.yaml（代码无硬编码 5%/10%/20% 常量，F-03 A 级检查）。
拦截/标记结果落 notes(kind='quality')，日报底部标注"数据质量：正常 / N 项异常已排除"。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULES_PATH = PROJECT_ROOT / "config" / "quality_rules.yaml"
DEFAULT_CALENDAR_PATH = PROJECT_ROOT / "data" / "calendar" / "trading_calendar.yaml"

# 代码前缀 → 板块（与 config boards 键对应）；其余归 main
_BOARD_BY_PREFIX = {
    "300": "chi_next", "301": "chi_next",
    "688": "star", "689": "star",
}


def load_rules(path: str | Path = DEFAULT_RULES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_calendar(path: str | Path = DEFAULT_CALENDAR_PATH) -> set[str]:
    """加载本地交易日历（D3 兜底）→ {'YYYY-MM-DD', ...}。"""
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    days = data.get("trading_days", [])
    return {str(d)[:10] for d in days}


@dataclass
class QualityIssue:
    check: str              # price_invalid / missing_on_trading_day / pct_chg_threshold
    code: str | None
    trade_date: str | None
    severity: str           # block / mark
    detail: str


@dataclass
class QualityResult:
    df: pd.DataFrame                    # 原 df + quality_flag 列（ok / mark / block）
    issues: list[QualityIssue] = field(default_factory=list)

    def summary(self) -> str:
        """日报底部标注（F-06 使用）；豁免（info）不计入异常数。"""
        anomalies = [i for i in self.issues if i.severity in ("block", "mark")]
        if not anomalies:
            return "数据质量：正常"
        blocks = sum(1 for i in anomalies if i.severity == "block")
        marks = sum(1 for i in anomalies if i.severity == "mark")
        return f"数据质量：{len(anomalies)} 项异常已排除（拦截 {blocks} / 标记 {marks}）"


def board_for_code(code: str, name: str | None = None) -> str:
    """板块判定优先级（config 注释）：ST > 新股首日 > 退市整理 > 创业板/科创板 > 主板。
    新股首日豁免单独处理（见 _is_new_stock_first_day），不在此返回。"""
    if name:
        if "ST" in name.upper():
            return "st"
        if "退" in name:
            return "delisting"
    return _BOARD_BY_PREFIX.get(code[:3], "main")


class QualityChecker:
    def __init__(
        self,
        rules: dict | None = None,
        calendar: set[str] | None = None,
        code_meta: dict | None = None,
    ) -> None:
        """code_meta: {code: {"name": str|None, "board": str|None, "listing_date": str|None}}"""
        self.rules = rules or load_rules()
        self.calendar = calendar if calendar is not None else load_calendar()
        self.code_meta = code_meta or {}

    # ---------------- 主入口 ----------------
    def check(self, df: pd.DataFrame, codes: list[str] | None = None) -> QualityResult:
        """对采集到的日线 df 执行三条检查，返回带 quality_flag 的结果与问题清单。

        df 需含：code, trade_date, close, adj_close, pct_chg（collector 统一 Schema）。
        缺少当日行情代码的"交易日缺数据"检查按 codes 参数判定。
        """
        if df.empty:
            return QualityResult(df=df.copy(), issues=[])
        out = df.copy()
        out["quality_flag"] = "ok"
        issues: list[QualityIssue] = []

        # 检查 1：价格非法（逐行拦截）
        if self.rules.get("price_invalid", {}).get("enabled", True):
            bad = out[(out["close"] <= 0) | (out["adj_close"] <= 0)]
            for _, r in bad.iterrows():
                out.loc[r.name, "quality_flag"] = "block"
                issues.append(QualityIssue(
                    check="price_invalid", code=r["code"], trade_date=r["trade_date"],
                    severity="block",
                    detail=f"close={r['close']} adj_close={r['adj_close']} 非法（<=0）",
                ))

        # 检查 3：涨跌幅超板块阈值（含除权日豁免 / 新股首日豁免）
        if self.rules.get("pct_chg_threshold", {}).get("enabled", True):
            for code, g in out.groupby("code", sort=False):
                g = g.sort_values("trade_date")
                issues += self._check_pct_chg(g, code)

        # 检查 2：交易日缺数据（标记；作用于"应有行情但无行"的代码）
        if self.rules.get("missing_on_trading_day", {}).get("enabled", True):
            issues += self._check_missing(out, codes)

        # 拦截行标记 quality_flag=block（价格非法在检查 1 已直接标记）
        for issue in issues:
            if issue.severity == "block":
                out.loc[
                    (out["code"] == issue.code) & (out["trade_date"] == issue.trade_date),
                    "quality_flag",
                ] = "block"

        result = QualityResult(df=out, issues=issues)
        return result

    # ---------------- 检查 3：涨跌幅阈值 ----------------
    def _check_pct_chg(self, g: pd.DataFrame, code: str) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        meta = self.code_meta.get(code, {})
        name = meta.get("name")
        board = meta.get("board") or board_for_code(code, name)
        rules = self.rules["pct_chg_threshold"]
        new_stock_exempt = rules.get("new_stock_first_day", {}).get("exempt", True)
        ex_div = self.rules.get("ex_dividend_exemption", {})
        diverg_th = ex_div.get("adj_vs_close_diverg_threshold", 0.005)
        ex_div_enabled = ex_div.get("enabled", True)

        prev_close: float | None = None
        prev_adj: float | None = None
        for _, r in g.iterrows():
            td = r["trade_date"]
            # 新股首日豁免
            listing_date = meta.get("listing_date")
            if new_stock_exempt and listing_date and str(td) == str(listing_date)[:10]:
                issues.append(QualityIssue(
                    check="pct_chg_threshold", code=code, trade_date=td,
                    severity="info",
                    detail=f"新股首日（{listing_date}）豁免涨跌幅检查",
                ))
                prev_close = r["close"]
                prev_adj = r["adj_close"]
                continue
            # 除权日豁免（D1）：|adj 变动% - raw 变动%| > 阈值
            if ex_div_enabled and prev_close is not None and prev_adj:
                adj_pct = (r["adj_close"] / prev_adj - 1) * 100
                raw_pct = (r["close"] / prev_close - 1) * 100
                if abs(adj_pct - raw_pct) > diverg_th * 100:
                    issues.append(QualityIssue(
                        check="pct_chg_threshold", code=code, trade_date=td,
                        severity="info",
                        detail=f"除权日豁免（adj {adj_pct:+.2f}% vs raw {raw_pct:+.2f}%）",
                    ))
                    prev_close = r["close"]
                    prev_adj = r["adj_close"]
                    continue
            # 阈值判断
            threshold = rules["boards"].get(board, rules["boards"]["main"])["max_pct_chg"]
            if abs(r["pct_chg"]) > threshold:
                issues.append(QualityIssue(
                    check="pct_chg_threshold", code=code, trade_date=td,
                    severity="block",
                    detail=f"涨跌幅 {r['pct_chg']:+.2f}% 超板块[{board}]上限 {threshold}%",
                ))
            prev_close = r["close"]
            prev_adj = r["adj_close"]
        return issues

    # ---------------- 检查 2：交易日缺数据 ----------------
    def _check_missing(self, df: pd.DataFrame, codes: list[str] | None) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        codes = codes or list(df["code"].unique())
        trading_days = sorted(self.calendar)
        for code in codes:
            rows = df[df["code"] == code]
            present = set(rows["trade_date"].astype(str))
            # 取窗口内交易日（df 中该代码最早~最晚日期之间的交易日）
            if rows.empty:
                continue
            d0, d1 = rows["trade_date"].min(), rows["trade_date"].max()
            missing = [d for d in trading_days if d0 <= d <= d1 and d not in present]
            for d in missing:
                issues.append(QualityIssue(
                    check="missing_on_trading_day", code=code, trade_date=d,
                    severity="mark", detail=f"交易日 {d} 无行情数据",
                ))
        return issues


def persist_issues(result: QualityResult, conn) -> None:
    """拦截/标记落 notes(kind='quality')（F-03：M0 并入 notes，M0 后独立 quality_logs 表）。"""
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).isoformat()
    for issue in result.issues:
        content = f"[{issue.severity}] {issue.check} code={issue.code} date={issue.trade_date} {issue.detail}"
        conn.execute(
            "INSERT INTO notes(ts, kind, content) VALUES(?, 'quality', ?)",
            (ts, content),
        )
    if result.issues:
        conn.commit()
