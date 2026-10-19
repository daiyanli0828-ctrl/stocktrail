# -*- coding: utf-8 -*-
"""
LLM 客户端（CHG-009；企划 v1.0 C13 溯源三约束）
- 调用 DeepSeek（OpenAI 兼容）做【可溯源的复盘总结】——用途限定：文本压缩/新闻归因/复盘总结
- C13 三约束在此落地：
  1. source_id：分析仅基于调用方传入的带 id 的记录，prompt 要求逐条引用记录 id（挂不上来源即丢弃）
  2. hypothesis：复盘为事后分析、不产生前瞻表述；prompt 明确禁止预测，输出前瞻性语句时后处理标 hypothesis
  3. 段标记：LLM 输出由调用方（run_daily）以「🤖 LLM 分析」段呈现，与规则段区分
- 无指令式买卖用语（C7）：prompt 强制，输出后处理兜底过滤
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LLM_CONFIG = PROJECT_ROOT / "config" / "llm.yaml"
SECRETS = PROJECT_ROOT / "config" / "secrets.yaml"

logger = logging.getLogger(__name__)

# C7 兜底：仅剔除「指令式」短语（建议/应当/应该/可以… + 买卖动作）；
# 保留对已发生操作的事实描述（如「卖出 000636」），避免误伤复盘事实
INSTRUCTION_RE = re.compile(
    r"(建议|应当|应该|可以|请|务必|最好|推荐|适合)\s*(你|您|我)?\s*"
    r"(买入|卖出|加仓|减仓|清仓|满仓|建仓|补仓|抄底|追高|追涨)"
)

SYSTEM_PROMPT = (
    "你是个人 A 股投资者的交易复盘助手。只做【可溯源的事实复盘总结】，严格遵守：\n"
    "1. 仅基于用户给出的交易记录与不操作理由（认知感受）分析，每条结论用「(记录ID)」标注交易来源、「(感受ID)」标注不操作理由来源；无法对应来源的表述不要输出；\n"
    "2. 只归纳：操作回顾（买了什么/卖了什么）、理由要点、纪律观察（是否设止损、是否按计划、盈亏）；"
    "若给出「今日不操作理由（认知感受）」，结合其与交易行为做认知偏差观察（如情绪化割肉 vs 计划内操作），仍不给任何买卖建议；\n"
    "3. 禁止买卖建议与指令式用语（不得出现'建议买入/应当卖出/可以加仓'等）；描述已发生交易可用'买入/卖出'等事实动词；\n"
    "4. 禁止预测涨跌、禁止前瞻性判断（复盘是回顾过去，不是预测未来）；\n"
    "5. 简洁分点，≤200 字；若记录与感受均为空则输出'今日无交易记录'。"
)

STRATEGY_SYSTEM_PROMPT = (
    "你是个人 A 股投资者的复盘分析助手。请严格按下方【交易策略框架】对当日数据做【可溯源的事实复盘】，"
    "只描述数据所处的状态类别，不给出任何买卖操作指令。\n\n"
    "【交易策略框架】{strategy_name}\n{strategy_content}\n\n"
    "严格遵守：\n"
    "1. 逐条对照策略框架，用数据说明当前状态（如'站上/跌破 MA20''放量/缩量''超跌/超涨'），"
    "交易记录用「(记录ID)」标注来源，市场数据用「市场/指数/自选」标注来源；无法对应来源的表述不要输出；\n"
    "2. 只做事实复盘与纪律观察（是否按计划、是否触达策略定义的信号、盈亏）；"
    "若给出「今日不操作理由（认知感受）」，结合其做认知偏差观察（如情绪化 vs 计划内），"
    "禁止'建议买入/卖出/加仓/减仓'等指令式用语；\n"
    "3. 禁止预测涨跌、禁止前瞻性判断；\n"
    "4. 简洁分点，≤300 字；若当日无交易记录、无市场数据且无认知感受则输出'今日无数据'。"
)


def load_llm_config() -> dict:
    cfg = {}
    if LLM_CONFIG.exists():
        c = yaml.safe_load(LLM_CONFIG.read_text(encoding="utf-8")) or {}
        cfg = c.get("llm", {}) or {}
    if SECRETS.exists():
        s = yaml.safe_load(SECRETS.read_text(encoding="utf-8")) or {}
        sec = s.get("llm", {}) or {}
        # 仅 api_key 属凭证（secrets）；enabled/model/base_url 以 config/llm.yaml 为准。
        # 否则 secrets 里残留的旧 enabled=false 会覆盖网页设置页写入的 enabled=true。
        if sec.get("api_key"):
            cfg["api_key"] = sec["api_key"]
    return cfg


def llm_enabled() -> bool:
    cfg = load_llm_config()
    return bool(cfg.get("enabled") and cfg.get("api_key"))


def chat(messages: list[dict], model: str | None = None) -> str:
    """OpenAI 兼容调用，返回 assistant 文本。未配置/失败时返回空串。"""
    cfg = load_llm_config()
    api_key = cfg.get("api_key")
    base_url = (cfg.get("base_url") or "https://api.deepseek.com").rstrip("/")
    m = model or cfg.get("model") or "deepseek-chat"
    if not api_key:
        return ""
    body = json.dumps({"model": m, "messages": messages, "temperature": 0.3,
                       "max_tokens": 800}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            resp = json.loads(r.read().decode("utf-8"))
        return resp["choices"][0]["message"]["content"].strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM 调用失败：%s", exc)
        return ""


def _sanitize(text: str) -> str:
    """C7 兜底：剔除指令式短语（如「建议买入」），保留复盘事实（如「卖出 000636」）。"""
    return INSTRUCTION_RE.sub("【已过滤】", text)


def _format_trades(trades: list[dict]) -> str:
    return "\n".join(
        f"(记录ID {t['id']}) {t['side']} {t['code']} @{t['price']:g}×{t['shares']:g}"
        + (f"；理由：{t['reason']}" if t.get("reason") else "")
        + (f"；持股 {t['holding_days']:g} 天" if t.get("holding_days") else "")
        + (f"；盈亏 {t['pnl']:+,g} 元" if t.get("pnl") is not None else "")
        for t in trades
    )


def analyze_trades(trades: list[dict], model: str | None = None,
                   cognitions: str | None = None) -> str:
    """交易复盘分析（C13 可溯源）：输入带 id 的交易记录 + 可选今日不操作理由（认知感受），
    输出复盘文本（无指令、无预测）。无交易但有认知感受时也做认知复盘。"""
    if not trades and not cognitions:
        return "今日无交易记录"
    user = "今日交易记录：\n" + (_format_trades(trades) if trades else "（今日无交易记录）")
    if cognitions:
        user += f"\n\n今日不操作理由（认知感受）：\n{cognitions}"
    content = chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ], model=model)
    return _sanitize(content) if content else "（LLM 未配置或调用失败，仅展示原始记录）"


def analyze_strategy(trades: list[dict], market_summary: str,
                     strategy_name: str, strategy_content: str,
                     model: str | None = None, cognitions: str | None = None) -> str:
    """按交易策略框架做数据分析（CHG-010；C13 可溯源 + C7 无指令）：
    输入交易记录 + 市场摘要 + 策略名/内容 + 可选认知感受，输出按策略对照的事实复盘文本。"""
    if not trades and not market_summary.strip() and not cognitions:
        return "今日无数据"
    records = _format_trades(trades) if trades else "（今日无交易记录）"
    user = f"当日市场概况：\n{market_summary or '（无市场数据）'}\n\n当日交易记录：\n{records}"
    if cognitions:
        user += f"\n\n今日不操作理由（认知感受）：\n{cognitions}"
    content = chat([
        {"role": "system", "content": STRATEGY_SYSTEM_PROMPT.format(
            strategy_name=strategy_name, strategy_content=strategy_content)},
        {"role": "user", "content": user},
    ], model=model)
    return _sanitize(content) if content else "（LLM 未配置或调用失败，仅展示原始数据）"
