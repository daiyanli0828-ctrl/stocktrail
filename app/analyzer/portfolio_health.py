# -*- coding: utf-8 -*-
"""
账户体检计算（F-05；企划 v1.0 §2.5 M3 C2 / C10）
输入：positions.yaml 结构（account.total_assets / account.tier / tiers 三档上限 / positions 列表）
     + 最新收盘价 {code: close} + 可选行业映射 {code: 行业}
输出六要素（C2）：
  1. 总仓位 % 与档位上限对比
  2. 单票占比超限项
  3. 第一大行业集中度（行业映射简版；未标注时提示）
  4. 已跌破自设止损位的持仓（措辞：执行用户过去的决定，不含新买卖建议，C10 唯一例外）
  5. 浮盈浮亏分布（按仓位加权的盈利/亏损数量与幅度分桶）
  6. 持仓平均天数
纯函数、可单测；无指令式用语（C2/C10）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 浮盈亏幅度分桶（盈利/亏损两方向）
PnlBucket = dict[str, int]      # {"label": count}
PNL_BUCKET_LABELS = {
    "盈利": [">+20%", "+10%~+20%", "0~+10%"],
    "亏损": ["0~-10%", "-10%~-20%", "<-20%"],
}


@dataclass
class Position:
    code: str
    shares: float
    avg_cost: float
    opened_at: date
    stop_loss: float | None = None
    plan_target: float | None = None


@dataclass
class PortfolioHealth:
    tier: str
    total_assets: float
    total_position_pct: float            # 总仓位 %
    position_limit_pct: float            # 档位仓位上限 %
    over_position: bool
    single_stock_items: list[dict] = field(default_factory=list)   # {code, pct, limit, over}
    single_stock_limit_pct: float = 0.0
    top_industry: dict | None = None     # {name, pct, limit, over}
    industry_limit_pct: float = 0.0
    below_stop_loss: list[dict] = field(default_factory=list)      # {code, stop_loss, price, wording}
    pnl: dict = field(default_factory=dict)                        # {盈利: {buckets, count, weighted_avg}, 亏损: {...}}
    pnl_detail: list[dict] = field(default_factory=list)           # [{code, avg_cost, price, ret}] 每持仓一行
    avg_holding_days: float = 0.0
    missing_closes: list[str] = field(default_factory=list)        # 无最新收盘价的代码
    industries_unlabeled: bool = False


def compute_health(
    positions_cfg: dict,
    closes: dict[str, float],
    industries: dict[str, str] | None = None,
    today: date | None = None,
) -> PortfolioHealth:
    """纯函数：由 positions.yaml 结构 + 收盘价计算账户体检六要素。"""
    today = today or date.today()
    account = positions_cfg.get("account", {})
    total_assets = float(account.get("total_assets", 0) or 0)
    tier = account.get("tier", "稳健")
    tiers = positions_cfg.get("tiers", {})
    limits = tiers.get(tier, tiers.get("稳健", {}))
    pos_limit_pct = float(limits.get("position_limit", 0.5) or 0) * 100
    single_limit_pct = float(limits.get("single_stock_limit", 0.2) or 0) * 100
    ind_limit_pct = float(limits.get("industry_limit", 0.4) or 0) * 100

    pos_rows = positions_cfg.get("positions", []) or []
    positions: list[Position] = []
    for p in pos_rows:
        try:
            opened = date.fromisoformat(str(p["opened_at"]))
        except (ValueError, KeyError):
            opened = today
        positions.append(Position(
            code=str(p["code"]), shares=float(p.get("shares", 0)),
            avg_cost=float(p.get("avg_cost", 0)), opened_at=opened,
            stop_loss=float(p["stop_loss"]) if p.get("stop_loss") else None,
            plan_target=float(p["plan_target"]) if p.get("plan_target") else None,
        ))

    missing = [p.code for p in positions if p.code not in closes]
    market_value = {p.code: p.shares * closes.get(p.code, 0.0) for p in positions}
    total_mv = sum(market_value.values())
    total_pct = total_mv / total_assets * 100 if total_assets else 0.0

    # 2. 单票占比
    single_items = []
    for p in positions:
        pct = market_value[p.code] / total_assets * 100 if total_assets else 0.0
        single_items.append({
            "code": p.code, "pct": round(pct, 2),
            "limit": single_limit_pct, "over": pct > single_limit_pct,
        })

    # 3. 第一大行业集中度
    top_industry = None
    unlabeled = bool(positions) and not (industries or {})
    if industries:
        ind_mv: dict[str, float] = {}
        for p in positions:
            ind = industries.get(p.code)
            if ind:
                ind_mv[ind] = ind_mv.get(ind, 0.0) + market_value[p.code]
        if ind_mv:
            top_name = max(ind_mv, key=ind_mv.get)
            top_pct = ind_mv[top_name] / total_assets * 100 if total_assets else 0.0
            top_industry = {
                "name": top_name, "pct": round(top_pct, 2),
                "limit": ind_limit_pct, "over": top_pct > ind_limit_pct,
            }

    # 4. 破止损位持仓（措辞：执行用户过去的决定，无新买卖建议，C10）
    below = []
    for p in positions:
        if p.stop_loss and closes.get(p.code) is not None and closes[p.code] < p.stop_loss:
            price = closes[p.code]
            below.append({
                "code": p.code, "stop_loss": p.stop_loss, "price": price,
                "wording": (
                    f"⚠ {p.code} 已跌破你设定的止损位 {p.stop_loss:g} 元"
                    f"（现价 {price:g}），请按你的计划处理"
                ),
            })

    # 5. 浮盈浮亏分布（按仓位加权的数量与幅度分桶）+ 每持仓明细
    pnl = _pnl_distribution(positions, market_value, total_mv or 1.0, closes)
    pnl_detail = []
    for p in positions:
        price = closes.get(p.code)
        if p.avg_cost > 0 and price is not None and price > 0:
            pnl_detail.append({
                "code": p.code,
                "avg_cost": round(p.avg_cost, 3),
                "price": round(price, 3),
                "ret": round((price - p.avg_cost) / p.avg_cost * 100, 1),
            })

    # 6. 平均持股天数
    days = [max((today - p.opened_at).days, 0) for p in positions]
    avg_days = sum(days) / len(days) if days else 0.0

    return PortfolioHealth(
        tier=tier,
        total_assets=total_assets,
        total_position_pct=round(total_pct, 2),
        position_limit_pct=pos_limit_pct,
        over_position=total_pct > pos_limit_pct,
        single_stock_items=single_items,
        single_stock_limit_pct=single_limit_pct,
        top_industry=top_industry,
        industry_limit_pct=ind_limit_pct,
        below_stop_loss=below,
        pnl=pnl,
        pnl_detail=pnl_detail,
        avg_holding_days=round(avg_days, 1),
        missing_closes=missing,
        industries_unlabeled=unlabeled,
    )


def _pnl_distribution(
    positions: list[Position],
    market_value: dict[str, float],
    total_mv: float,
    closes: dict[str, float],
) -> dict:
    """浮盈亏分布：盈利/亏损两方向的幅度分桶（数量）+ 按仓位加权平均幅度。"""
    result = {}
    for direction, labels in PNL_BUCKET_LABELS.items():
        buckets = {label: 0 for label in labels}
        weighted_sum = 0.0
        weight_total = 0.0
        count = 0
        for p in positions:
            if p.avg_cost <= 0:
                continue
            price = closes.get(p.code)
            if price is None or price <= 0:   # 缺收盘价 → 不参与浮盈亏统计（与 missing_closes 提示一致）
                continue
            ret = (price - p.avg_cost) / p.avg_cost * 100
            weight = market_value.get(p.code, 0.0) / total_mv
            if direction == "盈利" and ret > 0:
                label = ">+20%" if ret > 20 else ("+10%~+20%" if ret >= 10 else "0~+10%")
            elif direction == "亏损" and ret < 0:
                label = "<-20%" if ret < -20 else ("-10%~-20%" if ret <= -10 else "0~-10%")
            else:
                continue
            buckets[label] += 1
            weighted_sum += ret * weight
            weight_total += weight
            count += 1
        result[direction] = {
            "buckets": buckets,
            "count": count,
            "weighted_avg": round(weighted_sum / weight_total, 2) if weight_total else 0.0,
        }
    return result


def health_lines(h: PortfolioHealth) -> list[str]:
    """账户体检段落文本行（F-06 日报渲染用；无指令式用语；人类可读优先）。"""
    lines: list[str] = []
    # 1. 总仓位
    pos_mark = "，⚠ 超出上限" if h.over_position else ""
    lines.append(
        f"总仓位 {h.total_position_pct:.0f}%（{h.tier}档上限 {h.position_limit_pct:.0f}%）{pos_mark}"
    )
    # 2. 单票占比
    over_single = [i for i in h.single_stock_items if i["over"]]
    if over_single:
        for i in over_single:
            lines.append(f"单票 {i['code']} 占 {i['pct']:.0f}%，超出单票上限 {i['limit']:.0f}%")
    elif h.single_stock_items:
        lines.append(f"单票占比均未超上限（{h.single_stock_limit_pct:.0f}%）")
    # 3. 行业集中度
    if h.top_industry:
        ind = h.top_industry
        mark = "，⚠ 超出上限" if ind["over"] else ""
        lines.append(f"第一大行业 {ind['name']} 集中度 {ind['pct']:.0f}%（上限 {ind['limit']:.0f}%）{mark}")
    elif h.industries_unlabeled:
        lines.append("行业集中度：未标注（可在 data/inputs/industries.yaml 补充）")
    # 4. 破止损（措辞按 F-05 规格固定）
    if h.below_stop_loss:
        for b in h.below_stop_loss:
            lines.append(b["wording"])
    else:
        lines.append("无跌破自设止损位的持仓")
    # 5. 浮盈浮亏：摘要行 + 每只持仓一行（成本 → 现价 → 收益率）
    pnl_summary_parts = []
    for direction in ("盈利", "亏损"):
        d = h.pnl.get(direction, {})
        if d.get("count"):
            wa = f"{d['weighted_avg']:+.1f}%" if d.get("weighted_avg") else "—"
            pnl_summary_parts.append(f"{direction} {d['count']} 笔（加权 {wa}）")
        else:
            pnl_summary_parts.append(f"{direction} 0 笔")
    lines.append("浮盈亏：" + " · ".join(pnl_summary_parts))
    for d in h.pnl_detail:
        lines.append(f"  - {d['code']}：成本 {d['avg_cost']:g} → 现价 {d['price']:g}（{d['ret']:+.1f}%）")
    # 6. 平均持股天数
    lines.append(f"平均持股 {h.avg_holding_days:.0f} 天")
    if h.missing_closes:
        lines.append(f"⚠ 以下持仓缺最新收盘价，相关计算未含：{', '.join(h.missing_closes)}")
    return lines
