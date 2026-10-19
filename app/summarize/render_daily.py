# -*- coding: utf-8 -*-
"""
daily.md 渲染（F-06；企划 v1.0 §2.5 M3、§2.11 C1 行为护栏）
模板层硬性三要素：
  1. 交易频次行固定显示（C1.1）——模板中为固定区块，数据由 run_daily 传入
  2. 任何段落不含可买标的清单（C1.2）——自选异动/市场概况只描述结构与状态
  3. "今日无需操作"是一等结论，正常渲染（C1.3）——模板默认分支
底部固定：数据质量标注 + 免责声明。全文无指令式买卖用语（C7）。
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_DIR = PROJECT_ROOT / "app" / "summarize" / "templates"
TEMPLATE_NAME = "daily.md.j2"

DISCLAIMER = (
    "> 本日报所有内容仅供个人研究与参考，不构成任何投资建议；"
    "信号与建议均为\"条件 + 观察点\"，决策权在你本人。股市有风险，决策需自行负责。"
)


def render_daily(context: dict, template_name: str = TEMPLATE_NAME) -> str:
    """由上下文渲染 daily.md 文本。context 键：
    date / weekday / market / health_lines / watchlist / trade_stats /
    yesterday(可选) / tomorrow_plan(可选) / quality_summary
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["g"] = lambda v: f"{v:g}"  # 去除浮点尾零
    template = env.get_template(template_name)
    ctx = dict(context)
    ctx.setdefault("disclaimer", DISCLAIMER)
    return template.render(**ctx)


def write_daily(markdown: str, vault_dir: Path, trade_date: date | None = None) -> Path:
    """落盘 Obsidian vault 目录（vault 路径可配置）。返回写入文件路径。"""
    vault_dir.mkdir(parents=True, exist_ok=True)
    trade_date = trade_date or date.today()
    path = vault_dir / f"daily-{trade_date.isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
