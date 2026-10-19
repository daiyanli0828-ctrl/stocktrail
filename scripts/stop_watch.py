#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
stop_watch.py：盘中止损位提醒（P-01；企划 v1.0 §2.3 C10 唯一例外 / docs/dev/03 P-01）

行为约束（C10）：
  - 仅对 positions 中**设有 stop_loss** 的持仓生效；
  - 盘中低频轮询（--once 供 schtasks 每 ≥5 分钟触发；--loop 常驻，间隔 ≥300s）；
  - 跌破止损位 → 桌面通知一次 + notes(kind='stop_alert') 去重（同日同代码不重复提醒）；
  - 措辞执行用户过去的决定："你设定的止损位已触发，请按你的计划处理"，**不含任何新买卖建议**；
  - 不输出任何其他市场信息（无盘中异动/公告/全市场提醒）。

用法：
  python scripts/stop_watch.py --once           # 单次检查（计划任务每 5 分钟）
  python scripts/stop_watch.py --loop [--interval 300]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from app.quality.checks import load_calendar  # noqa: E402

DB = PROJECT_ROOT / "data" / "processed" / "app.db"
POSITIONS = PROJECT_ROOT / "data" / "inputs" / "positions.yaml"
MIN_INTERVAL = 300          # 低频轮询下限（秒），P-01 规格 ≥5 分钟
TRADE_SESSIONS = [("09:30", "11:30"), ("13:00", "15:00")]
BJ = timezone(timedelta(hours=8))   # 北京时间（中国无夏令时，D4 显式归属）

ALERT_WORDING = "你设定的止损位 {stop} 元已触发（现价 {price}），请按你的计划处理"


# ---------------- 实时价（可注入供测试） ----------------
def get_realtime_prices(codes: list[str]) -> dict[str, float]:
    """新浪全市场实时行情 → {6位代码: 最新价}（东财故障时的实时源，直连可用）。"""
    import akshare as ak

    df = ak.stock_zh_a_spot()
    df["code6"] = df["代码"].astype(str).str[-6:]
    df = df[df["code6"].isin(codes)]
    return {str(r["code6"]): float(r["最新价"]) for _, r in df.iterrows()}


# ---------------- 交易日 / 交易时段检查 ----------------
def is_trading_day(day: date, calendar: set[str] | None = None) -> bool:
    return (calendar or load_calendar()).__contains__(day.isoformat())


def in_trade_session(now: datetime) -> bool:
    """A 股交易时段（9:30-11:30 / 13:00-15:00，北京时间）。"""
    hm = now.strftime("%H:%M")
    return any(s <= hm <= e for s, e in TRADE_SESSIONS)


# ---------------- 通知 ----------------
def notify_desktop(title: str, message: str) -> bool:
    """Windows 桌面通知（系统 WinForms NotifyIcon balloon，无第三方依赖）。"""
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Warning;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(8000, '{title}', '{message}', "
        "[System.Windows.Forms.ToolTipIcon]::Warning);"
        "Start-Sleep -Seconds 9;"
        "$n.Dispose()"
    )
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps],
            capture_output=True, timeout=15,
        )
        return True
    except Exception:  # noqa: BLE001 —— 通知失败不阻断记录
        return False


# ---------------- 触发检查（纯逻辑，可单测） ----------------
def check_breaks(
    positions_cfg: dict, prices: dict[str, float], trade_date: str,
    conn: sqlite3.Connection,
) -> list[dict]:
    """返回本次新触发的破止损记录（去重后）。"""
    triggered: list[dict] = []
    for p in positions_cfg.get("positions", []) or []:
        stop = p.get("stop_loss")
        if not stop:
            continue                       # C10：仅设有止损位的持仓
        code = str(p["code"])
        price = prices.get(code)
        if price is None or price <= 0:
            continue                       # 无实时价不误判
        if price >= float(stop):
            continue
        # 去重：同日同代码已提醒过则跳过
        dup = conn.execute(
            "SELECT 1 FROM notes WHERE kind='stop_alert' AND substr(ts,1,10)=? "
            "AND content LIKE ? LIMIT 1",
            (trade_date, f"%{code}%"),
        ).fetchone()
        if dup:
            continue
        triggered.append({
            "code": code, "stop_loss": float(stop), "price": price,
            "wording": f"⚠ {code} " + ALERT_WORDING.format(stop=float(stop), price=price),
            "trade_date": trade_date,
        })
    return triggered


def record_alerts(conn: sqlite3.Connection, alerts: list[dict]) -> None:
    ts = datetime.now(timezone.utc).isoformat()   # D4：全库 UTC
    for a in alerts:
        conn.execute(
            "INSERT INTO notes(ts, kind, content) VALUES(?, 'stop_alert', ?)",
            (ts, json.dumps(a, ensure_ascii=False)),
        )
    if alerts:
        conn.commit()


def run_once(positions_path: Path, db_path: Path, force: bool = False) -> list[dict]:
    """单次检查。返回新触发列表（已通知 + 已记录）。"""
    now = datetime.now(BJ)   # 交易时段/交易日判定基于北京时间（D4 显式归属）
    if not force:
        if not is_trading_day(now.date()):
            print("休市日，跳过止损检查")
            return []
        if not in_trade_session(now):
            print("非交易时段，跳过止损检查")
            return []

    with open(positions_path, encoding="utf-8") as f:
        positions_cfg = yaml.safe_load(f) or {}
    codes = [str(p["code"]) for p in positions_cfg.get("positions", []) or []
             if p.get("stop_loss")]
    if not codes:
        print("无设止损位的持仓，无需检查")
        return []

    prices = get_realtime_prices(codes)
    conn = sqlite3.connect(db_path)
    try:
        alerts = check_breaks(positions_cfg, prices, now.date().isoformat(), conn)
        record_alerts(conn, alerts)
    finally:
        conn.close()

    for a in alerts:
        ok = notify_desktop("GRGPZS 止损提醒", a["wording"])
        print(f"[触发] {a['wording']}" + ("（桌面通知已发）" if ok else "（桌面通知失败，已记录）"))
    if not alerts:
        print(f"检查 {len(codes)} 只设止损持仓：均未跌破")
    return alerts


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS 盘中止损位提醒（C10 唯一例外）")
    parser.add_argument("--once", action="store_true", help="单次检查（计划任务用）")
    parser.add_argument("--loop", action="store_true", help="常驻轮询")
    parser.add_argument("--interval", type=int, default=MIN_INTERVAL, help="轮询间隔秒（≥300）")
    parser.add_argument("--positions", type=str, default="", help="持仓文件路径")
    parser.add_argument("--db", type=str, default=str(DB))
    parser.add_argument("--force", action="store_true", help="跳过交易日/时段检查（测试用）")
    args = parser.parse_args()

    positions_path = Path(args.positions) if args.positions else POSITIONS
    interval = max(args.interval, MIN_INTERVAL)

    if args.loop:
        print(f"止损守护启动：每 {interval}s 检查一次（仅设止损持仓，C10）")
        while True:
            try:
                run_once(positions_path, Path(args.db), force=args.force)
            except Exception as exc:  # noqa: BLE001 —— 单轮失败不退出守护
                print(f"[轮询失败] {exc}")
            time.sleep(interval)
    else:
        run_once(positions_path, Path(args.db), force=args.force)


if __name__ == "__main__":
    main()
