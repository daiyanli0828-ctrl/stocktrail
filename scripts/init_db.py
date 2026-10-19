#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
F-01 三张表初始化（quotes / trades / notes）
依据：企划 v1.0 §2.5 M5、架构 v0.6 §5（存储 Schema，字段严格一致）
用法：python scripts/init_db.py [--db data/processed/app.db]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# 项目根目录引导（脚本从任意 cwd 运行均可导入 app）
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DDL = """
-- 行情表（M0）：后复权价 adj_close 用于指标计算，close 用于展示（D1）
-- trade_date：A 股交易日（北京时间归属），存 TEXT 'YYYY-MM-DD'（D4 显式 trade_date）
CREATE TABLE IF NOT EXISTS quotes (
    code         TEXT    NOT NULL,          -- 股票代码，如 600519
    trade_date   TEXT    NOT NULL,          -- 交易日 YYYY-MM-DD（北京时间归属）
    open         REAL,                      -- 开盘价（不复权）
    high         REAL,                      -- 最高价（不复权）
    low          REAL,                      -- 最低价（不复权）
    close        REAL    NOT NULL,          -- 收盘价（不复权，展示用）
    adj_close    REAL    NOT NULL,          -- 后复权收盘价（指标计算用，D1）
    volume       REAL,                      -- 成交量（手）
    amount       REAL,                      -- 成交额（元）
    pct_chg      REAL,                      -- 涨跌幅（%，不复权）
    quality_flag TEXT    NOT NULL DEFAULT 'ok',  -- F-03 质量检查结果：ok / mark / block
    PRIMARY KEY (code, trade_date)
);

-- 交易表（M0）：reason 四字段 T+1 后锁定（C3）
-- ts / reason_recorded_at 存 ISO-8601 UTC（D4）
CREATE TABLE IF NOT EXISTS trades (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    code               TEXT    NOT NULL,    -- 股票代码
    side               TEXT    NOT NULL,    -- buy / sell
    price              REAL    NOT NULL,    -- 成交价
    shares             REAL    NOT NULL,    -- 成交数量（股）
    ts                 TEXT    NOT NULL,    -- 成交时间 ISO-8601 UTC
    reason_type        TEXT,                -- 理由分类（CHG-007 开放化后留空 NULL；开放文本存 reason_note）
    reason_note        TEXT,                -- 理由说明
    plan_target        REAL,                -- 计划目标价
    stop_loss          REAL,                -- 计划止损价
    reason_recorded_at TEXT,                -- 理由四字段填写时间（C3 锁定依据）
    post_hoc           INTEGER NOT NULL DEFAULT 0,  -- 晚于成交 24h 补录=1，归因样本剔除（C3）
    holding_days       REAL,                -- 持股天数（卖出选填，CHG-008）
    pnl                REAL,                -- 盈亏金额（元，卖出选填，CHG-008）
    note               TEXT                 -- 备注（选填，CHG-013 统一操作记录）
);

-- 备注表（M0）：kind = plan(盘前承诺) / trade_reason(交易理由) / read(价值验收) / quality(质量日志, F-03)
--                    / hypothesis(前瞻结论与验证结果, F-09，C6) / stop_alert(止损触发记录, P-01，C10 去重)
--                    / cognition(认知感受，CHG-013：无交易也记录，供 LLM 纠偏)
CREATE TABLE IF NOT EXISTS notes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT    NOT NULL,          -- ISO-8601 UTC
    kind         TEXT    NOT NULL CHECK (kind IN ('plan','trade_reason','read','quality','hypothesis','stop_alert','cognition')),
    content      TEXT    NOT NULL,
    rel_trade_id INTEGER                   -- 关联 trades.id（可空）
);

-- 常用查询索引（非第 4 张表，D5 只约束表数量）
CREATE INDEX IF NOT EXISTS idx_trades_code ON trades(code);
CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes(ts);
"""


def _migrate_notes_kind(conn: sqlite3.Connection) -> bool:
    """迁移：notes.kind CHECK 增加 'cognition'（CHG-013）。SQLite 不能改 CHECK，需重建表。"""
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='notes'").fetchone()
    if row and "cognition" in (row[0] or ""):
        return False
    conn.execute("""
        CREATE TABLE notes_new (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT    NOT NULL,
            kind         TEXT    NOT NULL CHECK (kind IN ('plan','trade_reason','read','quality','hypothesis','stop_alert','cognition')),
            content      TEXT    NOT NULL,
            rel_trade_id INTEGER
        )
    """)
    conn.execute("INSERT INTO notes_new (id, ts, kind, content, rel_trade_id) "
                 "SELECT id, ts, kind, content, rel_trade_id FROM notes")
    conn.execute("DROP TABLE notes")
    conn.execute("ALTER TABLE notes_new RENAME TO notes")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_ts ON notes(ts)")
    conn.commit()
    return True


def _migrate_trades_note(conn: sqlite3.Connection) -> bool:
    """迁移：trades 表增加 note 备注列（CHG-013 统一操作记录）。"""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()]
    if "note" in cols:
        return False
    conn.execute("ALTER TABLE trades ADD COLUMN note TEXT")
    conn.commit()
    return True


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(DDL)
        conn.commit()
        if _migrate_notes_kind(conn):
            print("[ok] notes.kind 已迁移（增加 cognition）")
        if _migrate_trades_note(conn):
            print("[ok] trades 表已迁移（增加 note 备注列）")
        print(f"[ok] 数据库初始化完成: {db_path}")
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ):
            print(f"  - 表 {name}")
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS M0 三张表初始化")
    parser.add_argument("--db", default=str(PROJECT_ROOT / "data" / "processed" / "app.db"),
                        help="SQLite 数据库路径（默认 data/processed/app.db）")
    args = parser.parse_args()
    init_db(Path(args.db))


if __name__ == "__main__":
    main()
