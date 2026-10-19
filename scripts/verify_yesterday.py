#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_yesterday.py：昨日验证流水线（F-09；企划 v1.0 §2.5 M2 C6）
读 data/inputs/conclusions.yaml 中指定交易日的结论 + 今日实际行情 → 计算 verdict
（命中/未命中/无法判定）→ 写回 notes(kind='hypothesis')（含 verdict/verified_at/判据原文/判据版本）。

约束（C6）：
  - prediction 缺失的结论拒绝判定（不写入日报）；
  - 判据只从 config/verify_rules.yaml 读取（代码无硬编码判据常量）；
  - 判据版本号随结论记录，判据修改必须 version+1 并留 changelog。

用法：python scripts/verify_yesterday.py [--date YYYY-MM-DD] [--db PATH] [--rules PATH] [--conclusions PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from app.quality.checks import load_calendar  # noqa: E402

DB = PROJECT_ROOT / "data" / "processed" / "app.db"
DEFAULT_CONCLUSIONS = PROJECT_ROOT / "data" / "inputs" / "conclusions.yaml"

# 指数名 → 新浪代码（判据计算用，非判据数值常量）
INDEX_SYMBOLS = {
    "上证指数": "sh000001", "深证成指": "sz399001",
    "创业板指": "sz399006", "沪深300": "sh000300",
}


def _next_trading_day(day: str, calendar: set[str]) -> str | None:
    d = date.fromisoformat(day)
    for _ in range(10):
        d += timedelta(days=1)
        if d.isoformat() in calendar:
            return d.isoformat()
    return None


def _target_series(target: str, deadline: str, db_path: str) -> pd.DataFrame | None:
    """标的日线（deadline 日及前后）：股票代码 → quotes 表；指数名 → 新浪指数日线。"""
    if target in INDEX_SYMBOLS:
        import akshare as ak
        try:
            df = ak.stock_zh_index_daily(symbol=INDEX_SYMBOLS[target])
            df = df[df["date"].astype(str).str[:10] <= deadline].tail(5)
            return df.rename(columns={"date": "trade_date"})
        except Exception:  # noqa: BLE001
            return None
    # 板块名（P-02 热点结论）：新浪行业板块 spot，单行（trade_date=当日, pct_chg=当日涨跌幅；
    # 板块无历史序列，验证须在 deadline 当日盘后运行）
    try:
        import akshare as ak
        df = ak.stock_sector_spot(indicator="新浪行业")
        row = df[df["板块"].astype(str) == target]
        if row.empty:
            return None
        return pd.DataFrame([{
            "trade_date": date.today().isoformat(),
            "pct_chg": float(row.iloc[0]["涨跌幅"]),
        }])
    except Exception:  # noqa: BLE001
        return None
    # 股票代码：读 quotes 表（run_daily 已落库）
    conn = sqlite3.connect(db_path)
    try:
        df = pd.read_sql_query(
            "SELECT trade_date, close FROM quotes WHERE code=? AND trade_date<=? ORDER BY trade_date DESC LIMIT 5",
            conn, params=(target, deadline),
        )
    finally:
        conn.close()
    return df if not df.empty else None


def _rule_verdict(rule: dict | None, prediction: dict, deadline: str, db_path: str) -> tuple[str, str]:
    """按 rule.type 计算判定。返回 (verdict, criterion 原文)。
    verdict ∈ {命中, 未命中, 部分命中, 无法判定}（M0 规则产出 命中/未命中；
    部分命中预留给多目标判据，P3 对齐）。"""
    if rule is None:
        return "无法判定", f"判据未注册：{prediction.get('criterion_id', 'price_direction')}"
    rtype = rule.get("type")
    target = prediction.get("target_scope")
    direction = prediction.get("direction")

    def _series_ready(df) -> tuple[bool, str]:
        """最新 bar 必须等于判定日（F-09 评审 P1：防止用旧 bar 静默错判）。"""
        if df is None or df.empty:
            return False, "无数据"
        latest = str(df.sort_values("trade_date").iloc[-1]["trade_date"])[:10]
        if latest != deadline:
            return False, f"数据未就绪（最新 {latest}，判定日 {deadline}）"
        return True, ""

    df = _target_series(target, deadline, db_path)
    ok, note = _series_ready(df)
    if not ok:
        return "无法判定", f"{rule.get('id')}：{target} {note}"
    is_sector = "pct_chg" in df.columns and len(df) == 1   # P-02 板块模式（spot 单行）
    if not is_sector and len(df) < 2:   # F-09 第 2 轮评审 P5：仅 1 根 bar 时 iloc[-2] 崩溃（新股首日等场景）
        return "无法判定", f"{rule.get('id')}：{target} 数据不足（仅 1 根 bar，需 ≥2 根计算变动）"

    if rtype == "price_direction":
        if is_sector:
            pct = float(df.iloc[0]["pct_chg"])
            hit = (direction == "up" and pct > 0) or (direction == "down" and pct < 0) \
                or (direction == "flat" and abs(pct) < 1e-9)
            verdict = "命中" if hit else "未命中"
            criterion = (f"方向命中：{target} {df.iloc[0]['trade_date']} 涨跌幅 "
                         f"{pct:+.2f}%（预测 {direction}）")
            return verdict, criterion
        last = df.sort_values("trade_date").iloc[-1]
        prev = df.sort_values("trade_date").iloc[-2]
        chg = last["close"] - prev["close"]
        hit = (direction == "up" and chg > 0) or (direction == "down" and chg < 0) \
            or (direction == "flat" and abs(chg) < 1e-9)
        verdict = "命中" if hit else "未命中"
        criterion = (f"方向命中：{target} {deadline} 收盘 {last['close']:g} "
                     f"vs 前收 {prev['close']:g}（预测 {direction}）")
        return verdict, criterion

    if rtype == "excess_return_over_benchmark":
        bench = rule.get("params", {}).get("benchmark", "沪深300")
        min_excess = rule.get("params", {}).get("min_excess")
        if min_excess is None:   # C6：判据参数只从 config 读，无代码默认值
            return "无法判定", f"{rule.get('id')}：config 缺少 params.min_excess"
        bdf = _target_series(bench, deadline, db_path)
        ok_b, note_b = _series_ready(bdf)
        if not ok_b:
            return "无法判定", f"{rule.get('id')}：基准 {bench} {note_b}"
        if len(bdf) < 2:
            return "无法判定", f"{rule.get('id')}：基准 {bench} 数据不足（仅 1 根 bar）"
        t = df.sort_values("trade_date")
        b = bdf.sort_values("trade_date")
        t_ret = t.iloc[-1]["close"] / t.iloc[-2]["close"] - 1
        b_ret = b.iloc[-1]["close"] / b.iloc[-2]["close"] - 1
        excess = t_ret - b_ret
        hit = excess > min_excess
        verdict = "命中" if hit else "未命中"
        criterion = (f"超额收益：{target} {t_ret*100:+.2f}% − {bench} {b_ret*100:+.2f}% "
                     f"= {excess*100:+.2f}%（阈值 >{min_excess*100:+.1f}%）")
        return verdict, criterion

    return "无法判定", f"{rule.get('id')}：未实现的判据类型 {rtype}"


def _judge_record(rec: dict, day: str, rules: dict, version: int,
                  deadline: str | None, db_path: str) -> tuple[dict, str | None]:
    """对单条结论记录判定。返回 (record 或 None, rejected 原因或 None)。"""
    pred = rec.get("prediction")
    if not pred or not all(pred.get(k) for k in ("direction", "target_scope", "deadline")):
        return None, f"{rec.get('conclusion', '?')}（prediction 缺失或字段不全，拒绝判定）"
    if pred.get("deadline") != "next_day":
        return None, f"{rec.get('conclusion', '?')}（不支持的 deadline：{pred.get('deadline')}，仅 next_day）"
    criterion_id = pred.get("criterion_id") or "price_direction"
    rule = rules.get(criterion_id)
    verdict, criterion = _rule_verdict(rule, pred, deadline or day, db_path)
    record = {
        "date": day,
        "conclusion": rec.get("conclusion", ""),
        "logic": rec.get("logic", ""),
        "prediction": pred,
        "criterion_id": criterion_id,
        "verdict": verdict,
        "criterion": criterion,
        "rule_version": version,
        "deadline": deadline,
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    return record, None


def verify(conclusions_cfg: dict, rules_cfg: dict, day: str, db_path: str,
           calendar: set[str]) -> tuple[list[dict], list[str]]:
    """判定两种来源的结论：conclusions.yaml（手动）+ notes(kind='hypothesis') 中待验证记录
    （P-02 热点规则结论，写回原记录 verdict/verified_at）。"""
    version = rules_cfg.get("version", 1)
    rules = {r["id"]: r for r in rules_cfg.get("rules", [])}
    deadline = _next_trading_day(day, calendar)
    results: list[dict] = []       # 新记录（conclusions.yaml → 插入 notes）
    updated: list[dict] = []       # 已写回的热点结论（notes 更新，不重复插入）
    rejected: list[str] = []

    # 来源 1：conclusions.yaml（手动，F-09）
    for c in conclusions_cfg.get("conclusions", []) or []:
        if str(c.get("date"))[:10] != day:
            continue
        record, rej = _judge_record(c, day, rules, version, deadline, db_path)
        if rej:
            rejected.append(rej)
        elif record:
            results.append(record)

    # 来源 2：notes(kind='hypothesis') 待验证热点结论（P-02，写回原记录）
    conn = sqlite3.connect(db_path)
    try:
        for nid, content in conn.execute(
            "SELECT id, content FROM notes WHERE kind='hypothesis'"
        ).fetchall():
            try:
                rec = json.loads(content)
            except json.JSONDecodeError:
                continue
            if rec.get("verdict") or str(rec.get("date"))[:10] != day:
                continue
            record, rej = _judge_record(rec, day, rules, version, deadline, db_path)
            if rej:
                rejected.append(rej)
                continue
            rec.update({k: record[k] for k in ("verdict", "criterion", "rule_version",
                                               "deadline", "verified_at")})
            conn.execute("UPDATE notes SET content=? WHERE id=?", (json.dumps(rec, ensure_ascii=False), nid))
            updated.append(record)
        conn.commit()
    finally:
        conn.close()
    return results, updated, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 昨日验证流水线（C6）")
    parser.add_argument("--date", type=str, default="", help="结论归属交易日 YYYY-MM-DD（默认前一交易日）")
    parser.add_argument("--db", default=str(DB), help="SQLite 数据库路径")
    parser.add_argument("--rules", type=str, default="", help="verify_rules.yaml 路径")
    parser.add_argument("--conclusions", type=str, default=str(DEFAULT_CONCLUSIONS))
    args = parser.parse_args()

    rules_path = Path(args.rules) if args.rules else PROJECT_ROOT / "config" / "verify_rules.yaml"
    with open(rules_path, encoding="utf-8") as f:
        rules_cfg = yaml.safe_load(f)
    with open(args.conclusions, encoding="utf-8") as f:
        conclusions_cfg = yaml.safe_load(f) or {"conclusions": []}
    calendar = load_calendar()

    day = args.date
    if not day:
        # 默认：最近一个"已有结论但未验证"的日期；否则前一交易日
        today = date.today().isoformat()
        day = _prev_trading_day(today, calendar)

    results, updated, rejected = verify(conclusions_cfg, rules_cfg, day, args.db, calendar)
    if rejected:
        print(f"[拒绝] {len(rejected)} 条结论被拒绝（C6：prediction 缺失/字段不全/时限不支持）：")
        for r in rejected:
            print(f"  - {r}")
    if not results and not updated:
        print(f"{day}：无待验证结论" + ("" if not rejected else "（其余均已拒绝）"))
        sys.exit(0 if rejected else 0)

    conn = sqlite3.connect(args.db)
    for rec in results:
        conn.execute(
            "INSERT INTO notes(ts, kind, content) VALUES(?, 'hypothesis', ?)",
            (rec["verified_at"], json.dumps(rec, ensure_ascii=False)),
        )
        print(f"[判定] {rec['date']} {rec['conclusion']} → {rec['verdict']}（{rec['criterion']}；判据版本 v{rec['rule_version']}）")
    for rec in updated:
        print(f"[判定·热点] {rec['date']} {rec['conclusion']} → {rec['verdict']}（{rec['criterion']}；判据版本 v{rec['rule_version']}）")
    conn.commit()
    conn.close()


def _prev_trading_day(day: str, calendar: set[str]) -> str:
    d = date.fromisoformat(day)
    for _ in range(10):
        d -= timedelta(days=1)
        if d.isoformat() in calendar:
            return d.isoformat()
    return day


if __name__ == "__main__":
    main()
