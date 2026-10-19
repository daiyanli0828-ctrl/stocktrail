#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_ui.py：GRGPZS 本地网页界面（P-13；用户需求：命令行太复杂，要简单前端展示与记录）
- 展示：晚间主报 / 盘前增量页（vault 目录 daily-*.md / daily_increment-*.md → 极简 HTML）
- 记录：网页表单录入交易操作（理由开放文本，CHG-007）与 read（价值验收），写库与 CLI 同逻辑
- 策略库：/strategy 页（基础模板 + 上传/查看/启用，CHG-010），供 LLM 按策略做数据分析
- 仅监听 127.0.0.1（本地个人使用，无鉴权；云端部署时再启用令牌鉴权，CHG-006 后续）
- 行为护栏：无指令式买卖用语（C7）；录入仅承接用户输入，无任何新建议（C1/C10）

启动：python scripts/web_ui.py [--port 8000]
访问：http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import yaml  # noqa: E402

from app.summarize.render_daily import DISCLAIMER  # noqa: E402

DB = PROJECT_ROOT / "data" / "processed" / "app.db"
VAULT = PROJECT_ROOT / "data" / "processed" / "vault"
POSITIONS = PROJECT_ROOT / "data" / "inputs" / "positions.yaml"
ACCOUNT = PROJECT_ROOT / "data" / "inputs" / "account.yaml"
WATCHLIST_CFG = PROJECT_ROOT / "data" / "inputs" / "watchlist.yaml"   # 自选股池（CHG-011）
_db_path = DB   # 可被 --db 覆盖（测试注入）


def _db() -> str:
    return str(_db_path)

READ_SECTIONS = ["市场概况", "账户体检", "自选异动", "昨日验证", "明日计划", "交易频次"]

try:
    from fastapi import FastAPI, File, Form, Request, UploadFile
    from fastapi.responses import HTMLResponse, RedirectResponse
except ImportError:
    print("缺少 fastapi/uvicorn：请执行 pip install fastapi uvicorn")
    sys.exit(1)

app = FastAPI(title="GRGPZS 本地界面")

PAGE_CSS = """
body{font-family:'Microsoft YaHei',sans-serif;max-width:860px;margin:0 auto;padding:16px;background:#fafafa;color:#222}
h1{font-size:22px} h2{font-size:18px;border-bottom:1px solid #ddd;padding-bottom:4px;margin-top:24px}
table{border-collapse:collapse;width:100%;margin:8px 0}
th,td{border:1px solid #ccc;padding:5px 8px;text-align:left;font-size:14px}
th{background:#f0f0f0}
a{color:#1a73e8;text-decoration:none} a:hover{text-decoration:underline}
.card{background:#fff;border:1px solid #e0e0e0;border-radius:8px;padding:14px;margin:10px 0}
form label{display:inline-block;min-width:90px;font-size:14px}
input,select{font-size:14px;padding:4px;margin:3px 0}
button{background:#1a73e8;color:#fff;border:none;padding:7px 16px;border-radius:5px;font-size:14px;cursor:pointer}
button:hover{background:#1765c0}
.msg{background:#e8f5e9;border:1px solid #a5d6a7;padding:8px 12px;border-radius:5px;margin:8px 0}
.err{background:#fdecea;border:1px solid #ef9a9a;padding:8px 12px;border-radius:5px;margin:8px 0}
blockquote{border-left:3px solid #ccc;margin:6px 0;padding:2px 10px;color:#555}
ul{margin:4px 0}
.news-list li{margin:10px 0;line-height:1.7}
.small{font-size:12px;color:#777}
"""


# ---------------- 极简 markdown → HTML（覆盖 daily.md / 增量页格式） ----------------
def md_to_html(md: str) -> str:
    out: list[str] = []
    lines = md.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
            continue
        if ln.startswith("# "):
            out.append(f"<h1>{_fmt(ln[2:])}</h1>")
        elif ln.startswith("## "):
            out.append(f"<h2>{_fmt(ln[3:])}</h2>")
        elif ln.startswith("|") and i + 1 < len(lines) and set(lines[i + 1].replace("|", "").replace("-", "").replace(":", "").strip()) == set():
            # 表格头 + 分隔行
            header = [c.strip() for c in ln.strip("|").split("|")]
            i += 1
            rows = []
            while i + 1 < len(lines) and lines[i + 1].strip().startswith("|"):
                i += 1
                rows.append([c.strip() for c in lines[i].strip("|").split("|")])
            t = ["<table><thead><tr>"] + [f"<th>{_fmt(c)}</th>" for c in header] + ["</tr></thead><tbody>"]
            for r in rows:
                t.append("<tr>" + "".join(f"<td>{_fmt(c)}</td>" for c in r) + "</tr>")
            t.append("</tbody></table>")
            out.append("".join(t))
        elif ln.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(f"<li>{_fmt(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            i -= 1
        elif ln.startswith("> "):
            out.append(f"<blockquote>{_fmt(ln[2:])}</blockquote>")
        elif ln.strip() == "---":
            out.append("<hr>")
        else:
            out.append(f"<p>{_fmt(ln)}</p>")
        i += 1
    return "\n".join(out)


def _fmt(text: str) -> str:
    t = html.escape(text)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', t)
    return t


# ---------------- 数据读取 ----------------
def list_dailies() -> list[dict]:
    files = sorted(VAULT.glob("daily-*.md"), reverse=True)
    return [{"date": f.name.replace("daily-", "").replace(".md", ""), "path": f.name} for f in files]


def list_increments() -> list[dict]:
    files = sorted(VAULT.glob("daily_increment-*.md"), reverse=True)
    return [{"date": f.name.replace("daily_increment-", "").replace(".md", ""), "path": f.name} for f in files]


# ---------------- 页面 ----------------
def _page(title: str, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)}</title>
<style>{PAGE_CSS}</style></head><body>
<p class="small"><a href="/">← 返回首页</a></p>
{body}
<p class="small" style="margin-top:24px">{DISCLAIMER}</p>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index(msg: str = ""):
    dailies = list_dailies()
    incs = list_increments()
    daily_items = "".join(f'<li><a href="/daily/{d["date"]}">{d["date"]} 晚间主报</a></li>' for d in dailies)
    inc_items = "".join(f'<li><a href="/increment/{d["date"]}">{d["date"]} 盘前增量页</a></li>' for d in incs)
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    body = f"""{m}
<h1>GRGPZS 交易工作台</h1>
<div class="card">
  <h2>⚡ 一键生成</h2>
  <form method="post" action="/action/run_daily">
    <button type="submit">生成今日日报</button>
    <span class="small">（晚间主报：采集行情 + 账户体检 + 自选异动，约 1-2 分钟）</span>
  </form>
</div>
<div class="card">
  <h2>📄 查看日报</h2>
  <ul>{daily_items or "<li>暂无日报（点击上方按钮生成）</li>"}</ul>
  <h2>🌅 盘前增量页</h2>
  <ul>{inc_items or "<li>暂无增量页</li>"}</ul>
</div>
<div class="card">
  <h2>📰 新闻快讯</h2>
  <p><a href="/news">打开新闻快讯</a>（盘中刷新 / 盘后汇总）</p>
</div>
<div class="card">
  <h2>✏️ 交易操作记录</h2>
  <p><a href="/record">记录操作</a>　·　<a href="/record/read">记录今天哪段有用</a></p>
  <p><a href="/account">账户投入与持仓占比</a>　·　<a href="/positions">持仓股票</a>　·　<a href="/watchlist">自选股池</a></p>
</div>
<div class="card">
  <h2>📊 统计与设置</h2>
  <p><a href="/stats/read">价值验收统计（跳过率）</a>　·　<a href="/stats/hits">预测命中率</a></p>
  <p><a href="/strategy">📚 交易策略库</a>　·　<a href="/settings">⚙️ 设置</a></p>
</div>
"""
    return _page("首页", body)


@app.post("/action/run_daily", response_class=HTMLResponse)
def action_run_daily():
    """一键生成今日日报（后台调 run_daily.py，仅交易日生成，休市自动提示）。"""
    import subprocess
    p = subprocess.run(
        [sys.executable, "-X", "utf8", str(PROJECT_ROOT / "scripts" / "run_daily.py")],
        capture_output=True, text=True, timeout=600,
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8", "TQDM_DISABLE": "1"},
    )
    out = (p.stdout or "") + (p.stderr or "")
    m = re.search(r"REPORT_DATE=(\d{4}-\d{2}-\d{2})", out)   # CHG-010：按分析日期跳转（非生成时刻）
    if p.returncode == 0 and m:
        return RedirectResponse(f"/daily/{m.group(1)}?msg={_q('日报已生成')}", status_code=303)
    if "未收盘" in out:   # CHG-016：盘中（9:30-15:00）数据未收盘，拒生成
        return RedirectResponse("/?msg=" + _q("市场交易中（9:30-15:00），当日数据未收盘；请 15:00 后生成今日日报"), status_code=303)
    if "非交易日" in out:
        return RedirectResponse("/?msg=" + _q("今日非交易日（休市），无需生成"), status_code=303)
    return RedirectResponse("/?msg=" + _q("生成失败，请查看 data/processed/logs 或重试"), status_code=303)


@app.get("/daily/{d}", response_class=HTMLResponse)
def daily_page(d: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):   # 评审加固 C-5：日期白名单
        return _page("日报", '<p class="err">日期格式应为 YYYY-MM-DD</p>')
    f = VAULT / f"daily-{d}.md"
    if not f.exists():
        return _page("日报", '<p class="err">未找到该日报：' + html.escape(d) + "</p>")
    return _page(f"日报 {d}", md_to_html(f.read_text(encoding="utf-8")))


@app.get("/increment/{d}", response_class=HTMLResponse)
def increment_page(d: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):   # 评审加固 C-5
        return _page("盘前增量页", '<p class="err">日期格式应为 YYYY-MM-DD</p>')
    f = VAULT / f"daily_increment-{d}.md"
    if not f.exists():
        return _page("盘前增量页", '<p class="err">未找到该增量页</p>')
    return _page(f"盘前增量页 {d}", md_to_html(f.read_text(encoding="utf-8")))


# ---------------- 新闻快讯（CHG-014 修订：盘中/盘后两个生成键，独立于日报；C15 只存标题/链接） ----------------
def _related_stocks() -> tuple[list[str], dict[str, str]]:
    """自选 + 持仓 相关代码/名称（用于新闻相关匹配）。"""
    codes: list[str] = []
    names: dict[str, str] = {}
    for w in _load_watchlist():
        c = str(w.get("code", "")).zfill(6)
        codes.append(c)
        if w.get("name"):
            names[c] = str(w["name"])
    try:
        from app.analyzer.positions import effective_positions
        for p in effective_positions(_db(), _load_manual_positions()):
            c = str(p.get("code", "")).zfill(6)
            codes.append(c)
            names.setdefault(c, str(p.get("name") or ""))
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(codes)), names


@app.get("/news", response_class=HTMLResponse)
def news_page(msg: str = "", mode: str = "afterhours"):
    from app.collectors.cls_src import load_telegraphs, news_enabled
    if not news_enabled():
        return _page("新闻快讯",
                     '<h1>新闻快讯</h1><div class="card"><p class="err">新闻数据源已停用（可在「设置 → 新闻数据源」中开启）</p></div>')
    telegraphs = load_telegraphs(date.today().isoformat())
    related_codes, names = _related_stocks()
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    intraday = mode == "intraday"
    if intraday:
        from app.analyzer.news import build_intraday_view
        content = build_intraday_view(telegraphs) or '<p class="small">暂无快讯（点「盘中刷新」抓取）</p>'
        view_title = "盘中快讯"
    else:
        from app.analyzer.news import build_afterhours_view
        content = build_afterhours_view(telegraphs, names, related_codes) or '<p class="small">今日暂无累积快讯（点「盘后汇总」抓取）</p>'
        view_title = "盘后要闻"
    body = f"""{m}
<h1>新闻快讯</h1>
<p class="small">财联社电报 · 仅标题/链接 · 本地自用　|　🔴红=最重要　🟡黄=重要　🟢绿=普通</p>
<div class="card">
  <form method="post" action="/news/intraday" style="display:inline">
    <button type="submit">盘中刷新</button>
  </form>
  <span class="small">抓最新快讯，盘中使用</span>
  <form method="post" action="/news/afterhours" style="display:inline;margin-left:12px">
    <button type="submit">盘后汇总</button>
  </form>
  <span class="small">汇总今日全天要闻，盘后使用</span>
</div>
<div class="card">
  <h2>{view_title}（今日已累积 {len(telegraphs)} 条）</h2>
  <div class="news-list">{md_to_html(content)}</div>
</div>"""
    return _page("新闻快讯", body)


@app.post("/news/intraday", response_class=HTMLResponse)
def news_intraday():
    from app.collectors.cls_src import accumulate_telegraphs
    try:
        accumulate_telegraphs()
        return RedirectResponse("/news?mode=intraday&msg=" + _q("盘中快讯已刷新"), status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/news?mode=intraday&msg=" + _q(f"抓取失败：{str(exc)[:80]}"), status_code=303)


@app.post("/news/afterhours", response_class=HTMLResponse)
def news_afterhours():
    from app.collectors.cls_src import accumulate_telegraphs
    try:
        accumulate_telegraphs()
        return RedirectResponse("/news?mode=afterhours&msg=" + _q("盘后要闻已汇总"), status_code=303)
    except Exception as exc:  # noqa: BLE001
        return RedirectResponse("/news?mode=afterhours&msg=" + _q(f"抓取失败：{str(exc)[:80]}"), status_code=303)


# ---------------- 交易操作记录（CHG-013 重构：复盘信息 + 股票信息 + 其他 + 历史记录） ----------------
def _record_history() -> list[dict]:
    """合并 trades（买入/卖出）与 notes.cognition（未买卖）为统一操作记录，按时间倒序。"""
    conn = sqlite3.connect(_db())
    try:
        t = conn.execute(
            "SELECT id, ts, side, code, price, shares, reason_note, holding_days, pnl, note FROM trades"
        ).fetchall()
        c = conn.execute("SELECT id, ts, content FROM notes WHERE kind='cognition'").fetchall()
    finally:
        conn.close()
    items = []
    for tid, ts, side, code, price, shares, reason, hd, pnl, note in t:
        items.append({"id": tid, "src": "trade", "ts": ts, "date": str(ts)[:10], "type": side, "code": code,
                      "price": price, "shares": shares, "reason": reason,
                      "holding_days": hd, "pnl": pnl, "note": note})
    for cid, ts, content in c:
        items.append({"id": cid, "src": "cog", "ts": ts, "date": str(ts)[:10], "type": "none", "code": "",
                      "price": None, "shares": None, "reason": content,
                      "holding_days": None, "pnl": None, "note": None})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items


def _history_html(items: list[dict]) -> str:
    if not items:
        return '<p class="small">暂无记录</p>'

    def row(it: dict) -> str:
        if it["type"] == "none":
            head = f"{html.escape(it['date'])} · 未买卖"
        else:
            head = (f"{html.escape(it['date'])} · {'买入' if it['type'] == 'buy' else '卖出'} "
                    f"{html.escape(str(it['code']))} @{it['price']:g}×{it['shares']:g}")
        parts = []
        if it.get("holding_days"):
            parts.append(f"持股 {it['holding_days']:g} 天")
        if it.get("pnl") is not None:
            parts.append(f"盈亏 {it['pnl']:+,g} 元")
        if it.get("note"):
            parts.append(f"备注 {it['note']}")
        extra = f"<span class='small'>{' · '.join(parts)}</span>" if parts else ""
        reason = f"<p style='white-space:pre-wrap;margin:6px 0 0'>{html.escape(it['reason'] or '')}</p>" if it.get("reason") else ""
        edit = (f" <a href='/record/edit?src={it['src']}&id={it['id']}' "
                f"style='margin-left:10px;font-size:12px'>编辑</a>")
        return (f"<div class='card'><span class='small' style='color:#1a73e8'>{head}</span>{edit}"
                f"{extra}{reason}</div>")

    visible = items[:10]
    hidden = items[10:]
    out = "".join(row(x) for x in visible)
    if hidden:
        out += "<div id='hist_more' style='display:none'>" + "".join(row(x) for x in hidden) + "</div>"
        out += (f"<p><button type='button' onclick='toggleHist()' id='hist_btn' "
                f"data-n='{len(hidden)}'>展开更多（{len(hidden)} 条）</button></p>")
    return out


@app.get("/record", response_class=HTMLResponse)
def record_page(msg: str = ""):
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    history = _history_html(_record_history())
    body = f"""{m}
<h1>交易操作记录</h1>
<p class="small">每天记录一次：复盘信息 + 股票信息（可未买卖）</p>
<form method="post" action="/record">
  <div class="card">
    <h2>复盘信息</h2>
    <label>日期</label><input name="d" value="{date.today().isoformat()}" type="date"><br>
    <label>操作原因</label><br>
    <textarea name="reason" rows="3" style="width:100%;min-height:60px"
      placeholder="买卖或不买卖的原因"></textarea>
  </div>
  <div class="card">
    <h2>股票信息</h2>
    <label>方向</label>
    <select name="side" id="side" onchange="toggleTrade()">
      <option value="buy">买入</option><option value="sell">卖出</option><option value="none">未买卖</option>
    </select><br>
    <label>代码</label><input name="code" id="code" placeholder="600519"><br>
    <label>成交价</label><input name="price" id="price" type="number" step="0.01"><br>
    <label>股数</label><input name="shares" id="shares" type="number" step="1"><br>
    <p class="small">选择「未买卖」时，代码、成交价、股数可不填</p>
  </div>
  <div class="card">
    <h2>其他信息</h2>
    <div id="sell_fields" style="display:none">
      <label>持股天数</label><input name="holding_days" type="number" step="1"><br>
      <label>盈亏金额</label><input name="pnl" type="number" step="0.01" placeholder="正=盈利 负=亏损"><br>
    </div>
    <label>目标价</label><input name="target" type="number" step="0.01"><br>
    <label>止损价</label><input name="stop" type="number" step="0.01"><br>
    <label>备注</label><input name="note" style="min-width:300px"><br>
  </div>
  <button type="submit">保存</button>
</form>
<script>
function toggleTrade() {{
  var side = document.getElementById('side').value;
  var isTrade = side === 'buy' || side === 'sell';
  document.getElementById('code').required = isTrade;
  document.getElementById('price').required = isTrade;
  document.getElementById('shares').required = isTrade;
  document.getElementById('sell_fields').style.display = side === 'sell' ? 'block' : 'none';
}}
function toggleHist() {{
  var m = document.getElementById('hist_more');
  var b = document.getElementById('hist_btn');
  if (m.style.display === 'none') {{ m.style.display = 'block'; b.textContent = '收起'; }}
  else {{ m.style.display = 'none'; b.textContent = '展开更多（' + b.getAttribute('data-n') + ' 条）'; }}
}}
</script>
<h2>历史记录</h2>
{history}
"""
    return _page("交易操作记录", body)


@app.post("/record", response_class=HTMLResponse)
def record_submit(side: str = Form(""), d: str = Form(""),
                  code: str = Form(""), price: str = Form(""), shares: str = Form(""),
                  reason: str = Form(""), target: str = Form(""), stop: str = Form(""),
                  note: str = Form(""), holding_days: str = Form(""), pnl: str = Form("")):
    if side not in ("buy", "sell", "none"):
        return RedirectResponse("/record?msg=" + _q("请选择方向"), status_code=303)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
        d = date.today().isoformat()
    reason_text = reason.strip() or None
    ts = f"{d}T12:00:00+00:00"
    # 未买卖：只记录操作原因（认知感受/不操作理由），股票信息忽略
    if side == "none":
        if not reason_text:
            return RedirectResponse("/record?msg=" + _q("请填写操作原因"), status_code=303)
        conn = sqlite3.connect(_db())
        try:
            conn.execute("INSERT INTO notes(ts, kind, content) VALUES(?, 'cognition', ?)", (ts, reason_text))
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/record?msg=" + _q(f"已记录 {d} 未买卖原因"), status_code=303)
    # 买入/卖出：需代码/成交价/股数
    code = code.strip()
    if not re.fullmatch(r"\d{6}", code):
        return RedirectResponse("/record?msg=" + _q("代码须为 6 位数字"), status_code=303)
    if not price or not shares:
        return RedirectResponse("/record?msg=" + _q("成交价与股数必填"), status_code=303)
    f_price = float(price)
    f_shares = float(shares)
    f_target = float(target) if target else None
    f_stop = float(stop) if stop else None
    f_holding_days = float(holding_days) if holding_days else None
    f_pnl = float(pnl) if pnl else None
    note_text = note.strip() or None
    recorded_at = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(_db())
    try:
        conn.execute(
            "INSERT INTO trades(code, side, price, shares, ts, reason_type, reason_note,"
            " plan_target, stop_loss, reason_recorded_at, post_hoc, holding_days, pnl, note)"
            " VALUES(?,?,?,?,?,NULL,?,?,?,?,0,?,?,?)",
            (code, side, f_price, f_shares, ts, reason_text, f_target, f_stop,
             recorded_at if reason_text else None, f_holding_days, f_pnl, note_text),
        )
        conn.commit()
        extra = f" 持股 {f_holding_days:g} 天" if f_holding_days else ""
        extra += f" 盈亏 {f_pnl:+,g} 元" if f_pnl is not None else ""
        msg = f"已保存 {('买入' if side == 'buy' else '卖出')} {code} @{f_price:g}×{f_shares:g}" + extra
    finally:
        conn.close()
    return RedirectResponse("/record?msg=" + _q(msg), status_code=303)


# ---------------- 历史记录编辑（CHG-015：覆盖存储，后续分析以修改后为准） ----------------
def _fmt_num(v) -> str:
    return f"{v:g}" if isinstance(v, (int, float)) else ""


@app.get("/record/edit", response_class=HTMLResponse)
def record_edit_page(src: str = "trade", id: int = 0, msg: str = ""):
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    conn = sqlite3.connect(_db())
    conn.row_factory = sqlite3.Row
    try:
        if src == "cog":
            row = conn.execute("SELECT id, ts, content FROM notes WHERE id=? AND kind='cognition'", (id,)).fetchone()
        else:
            row = conn.execute("SELECT * FROM trades WHERE id=?", (id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return _page("编辑记录", '<p class="err">未找到该记录</p>')
    if src == "cog":
        body = f"""{m}
<h1>编辑不操作理由</h1>
<div class="card">
  <form method="post" action="/record/edit">
    <input type="hidden" name="src" value="cog"><input type="hidden" name="id" value="{id}">
    <label>日期</label><input name="d" value="{html.escape(str(row['ts'])[:10])}" type="date"><br>
    <label>理由</label><br>
    <textarea name="reason" rows="4" style="width:100%;min-height:90px">{html.escape(row['content'] or '')}</textarea><br>
    <button type="submit">保存修改</button>
  </form>
  <p class="small">保存后覆盖原记录，后续分析（日报 / LLM）以修改后为准</p>
</div>"""
        return _page("编辑记录", body)

    r = row
    d0 = str(r["ts"])[:10]
    sell_show = "block" if r["side"] == "sell" else "none"
    body = f"""{m}
<h1>编辑交易记录</h1>
<div class="card">
<form method="post" action="/record/edit" id="ef">
  <input type="hidden" name="src" value="trade"><input type="hidden" name="id" value="{id}">
  <h3 style="margin:6px 0">复盘信息</h3>
  <label>日期</label><input name="d" value="{html.escape(d0)}" type="date"><br>
  <label>操作原因</label><br>
  <textarea name="reason" rows="3" style="width:100%;min-height:60px">{html.escape(r['reason_note'] or '')}</textarea>
  <h3 style="margin:6px 0">股票信息</h3>
  <label>方向</label>
  <select name="side" id="side" onchange="toggleSell()">
    <option value="buy" {'selected' if r['side']=='buy' else ''}>买入</option>
    <option value="sell" {'selected' if r['side']=='sell' else ''}>卖出</option>
  </select><br>
  <label>代码</label><input name="code" value="{html.escape(str(r['code']))}" required><br>
  <label>成交价</label><input name="price" type="number" step="0.01" value="{_fmt_num(r['price'])}" required><br>
  <label>股数</label><input name="shares" type="number" step="1" value="{_fmt_num(r['shares'])}" required><br>
  <h3 style="margin:6px 0">其他信息</h3>
  <div id="sell_fields" style="display:{sell_show}">
    <label>持股天数</label><input name="holding_days" type="number" step="1" value="{_fmt_num(r['holding_days'])}"><br>
    <label>盈亏金额</label><input name="pnl" type="number" step="0.01" value="{_fmt_num(r['pnl'])}" placeholder="正=盈利 负=亏损"><br>
  </div>
  <label>目标价</label><input name="target" type="number" step="0.01" value="{_fmt_num(r['plan_target'])}"><br>
  <label>止损价</label><input name="stop" type="number" step="0.01" value="{_fmt_num(r['stop_loss'])}"><br>
  <label>备注</label><input name="note" value="{html.escape(r['note'] or '')}" style="min-width:300px"><br>
  <button type="submit">保存修改</button>
</form>
<p class="small">保存后覆盖原记录，后续分析（持仓推算 / 日报 / LLM）以修改后为准</p>
<script>
function toggleSell(){{document.getElementById('sell_fields').style.display=document.getElementById('side').value==='sell'?'block':'none';}}
</script>
</div>"""
    return _page("编辑记录", body)


@app.post("/record/edit", response_class=HTMLResponse)
def record_edit_submit(src: str = Form("trade"), id: int = Form(0), d: str = Form(""),
                       side: str = Form(""), code: str = Form(""), price: str = Form(""),
                       shares: str = Form(""), reason: str = Form(""), target: str = Form(""),
                       stop: str = Form(""), note: str = Form(""),
                       holding_days: str = Form(""), pnl: str = Form("")):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d or ""):
        d = date.today().isoformat()
    if src == "cog":
        content = reason.strip()
        if not content:
            return RedirectResponse(f"/record/edit?src=cog&id={id}&msg={_q('内容不能为空')}", status_code=303)
        conn = sqlite3.connect(_db())
        try:
            conn.execute("UPDATE notes SET ts=?, content=? WHERE id=? AND kind='cognition'",
                         (f"{d}T12:00:00+00:00", content, id))
            conn.commit()
        finally:
            conn.close()
        return RedirectResponse("/record?msg=" + _q(f"已更新 {d} 不操作理由"), status_code=303)
    # ---- 交易覆盖修改 ----
    if side not in ("buy", "sell"):
        return RedirectResponse(f"/record/edit?src=trade&id={id}&msg={_q('方向无效')}", status_code=303)
    code = code.strip()
    try:
        f_price = float(price)
        f_shares = float(shares)
        if not re.fullmatch(r"\d{6}", code) or f_price <= 0 or f_shares <= 0:
            raise ValueError
    except ValueError:
        return RedirectResponse(f"/record/edit?src=trade&id={id}&msg={_q('代码须 6 位数字，成交价/股数须为正数')}", status_code=303)
    f_target = float(target) if target else None
    f_stop = float(stop) if stop else None
    f_holding_days = float(holding_days) if holding_days else None
    f_pnl = float(pnl) if pnl else None
    reason_text = reason.strip() or None
    note_text = note.strip() or None
    ts = f"{d}T12:00:00+00:00"
    conn = sqlite3.connect(_db())
    try:
        orig = conn.execute("SELECT reason_note, reason_recorded_at, post_hoc, ts FROM trades WHERE id=?",
                            (id,)).fetchone()
        if not orig:
            return RedirectResponse("/record?msg=" + _q("记录不存在"), status_code=303)
        orig_reason, orig_recorded_at, orig_post_hoc, orig_ts = orig
        reason_changed = reason_text != (orig_reason or None)
        if reason_changed:   # C3 信号保留：理由被修改且晚于成交 24h → post_hoc=1（归因样本剔除）
            now_utc = datetime.now(timezone.utc)
            try:
                age_h = (now_utc - datetime.fromisoformat(orig_ts)).total_seconds() / 3600
            except ValueError:
                age_h = 9999.0
            recorded_at = now_utc.isoformat()
            post_hoc = 1 if age_h > 24 else int(orig_post_hoc or 0)
        else:
            recorded_at = orig_recorded_at
            post_hoc = int(orig_post_hoc or 0)
        conn.execute(
            "UPDATE trades SET code=?, side=?, price=?, shares=?, ts=?, reason_note=?, plan_target=?,"
            " stop_loss=?, reason_recorded_at=?, post_hoc=?, holding_days=?, pnl=?, note=? WHERE id=?",
            (code, side, f_price, f_shares, ts, reason_text, f_target, f_stop,
             recorded_at, post_hoc, f_holding_days, f_pnl, note_text, id),
        )
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/record?msg=" + _q(f"已覆盖更新 {('买入' if side == 'buy' else '卖出')} {code}"), status_code=303)


# ---------------- 账户：总投入 + 持仓占比（最小输入：只填累计投入金额） ----------------
def _load_invested() -> tuple[float, str]:
    if not ACCOUNT.exists():
        return 0.0, ""
    a = yaml.safe_load(ACCOUNT.read_text(encoding="utf-8")) or {}
    acc = a.get("account", {})
    return float(acc.get("invested", 0) or 0), str(acc.get("updated_at", ""))


def _position_analysis() -> tuple[list[dict], float, float, float]:
    """持仓占比分析：有效持仓（交易记录自动推算 + positions.yaml 手动覆盖，CHG-012）+ quotes 最新价 → 市值/成本/占比。"""
    from app.analyzer.positions import effective_positions
    manual = (_read_yaml(POSITIONS).get("positions", []) or []) if POSITIONS.exists() else []
    positions = effective_positions(_db(), manual)
    closes: dict[str, float] = {}
    if positions:
        conn = sqlite3.connect(_db())
        try:
            for code in [str(x["code"]) for x in positions]:
                row = conn.execute(
                    "SELECT close FROM quotes WHERE code=? ORDER BY trade_date DESC LIMIT 1", (code,)
                ).fetchone()
                if row:
                    closes[code] = float(row[0])
        finally:
            conn.close()
    rows: list[dict] = []
    total_mv = total_cost = 0.0
    for x in positions:
        code = str(x["code"])
        shares = float(x.get("shares", 0))
        cost = float(x.get("avg_cost", 0))
        close = closes.get(code, 0.0)
        mv = shares * close
        cv = shares * cost
        total_mv += mv
        total_cost += cv
        rows.append({"code": code, "shares": shares, "close": close, "mv": mv, "cost": cv})
    return rows, total_mv, total_cost, (total_mv - total_cost)


@app.get("/account", response_class=HTMLResponse)
def account_page(msg: str = ""):
    invested, updated = _load_invested()
    rows, total_mv, total_cost, pnl = _position_analysis()
    base = invested or total_mv or 0.0   # 未录入投入且无持仓时 base=0（防除零）
    def pct(v: float) -> str:
        return f"{v/base*100:.1f}%" if base else "—"
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    detail = "".join(
        f"<tr><td>{r['code']}</td><td>{r['shares']:g}</td><td>{r['close']:g}</td>"
        f"<td>{r['mv']:,.0f}</td><td>{pct(r['mv'])}</td>"
        f"<td>{r['mv']-r['cost']:+,.0f}</td></tr>"
        for r in rows
    ) if rows else "<tr><td colspan=6>暂无持仓（编辑 data/inputs/positions.yaml）</td></tr>"
    cash = base - total_mv
    body = f"""{m}
<h1>账户投入与持仓占比</h1>
<div class="card">
  <h2>💰 总投入（最小输入：只填累计投入金额，定期更新）</h2>
  <form method="post" action="/account">
    <label>累计投入(元)</label><input name="invested" type="number" step="0.01" value="{invested:g}" required><br>
    <button type="submit">保存投入</button>
    <span class="small">{'上次更新：' + updated if updated else '尚未录入'}</span>
  </form>
</div>
<div class="card">
  <h2>📊 持仓占比分析（市值 ÷ 总投入）</h2>
  <table>
    <thead><tr><th>代码</th><th>数量</th><th>最新价</th><th>市值</th><th>占比</th><th>盈亏</th></tr></thead>
    <tbody>{detail}</tbody>
  </table>
  <p>
    总投入 <b>{base:,.0f}</b> 元 · 持仓总市值 <b>{total_mv:,.0f}</b> 元（占比 {pct(total_mv)}）·
    现金估算 <b>{cash:,.0f}</b> 元 · 持仓盈亏 <b style="color:{'#c62828' if pnl>=0 else '#2e7d32'}">{pnl:+,.0f}</b> 元
  </p>
  <p class="small">持仓来自 data/inputs/positions.yaml，最新价来自数据库（run_daily 落库）；投入金额定期手动更新</p>
</div>"""
    return _page("账户投入", body)


@app.post("/account", response_class=HTMLResponse)
def account_submit(invested: str = Form(...)):
    try:
        v = float(invested)
        if v < 0:
            raise ValueError
    except ValueError:
        return RedirectResponse("/account?msg=" + _q("金额须为非负数字"), status_code=303)
    ACCOUNT.parent.mkdir(parents=True, exist_ok=True)
    yaml.safe_dump({"account": {"invested": v, "updated_at": date.today().isoformat()}},
                   ACCOUNT.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    return RedirectResponse("/account?msg=" + _q(f"已更新总投入 {v:g} 元"), status_code=303)


# ---------------- 股票代码↔名称 自动补全（CHG-012） ----------------
AUTOFILL_JS = """
<script>
function autofillName() {
  var c = document.getElementById('code').value.trim();
  if (c.length !== 6 || !/^[0-9]{6}$/.test(c)) return;
  fetch('/api/stock/lookup?q=' + encodeURIComponent(c)).then(function(r){return r.json();}).then(function(j){
    if (j.name) document.getElementById('name').value = j.name;
  });
}
function autofillCode() {
  var n = document.getElementById('name').value.trim();
  if (!n) return;
  fetch('/api/stock/lookup?q=' + encodeURIComponent(n)).then(function(r){return r.json();}).then(function(j){
    if (j.code) document.getElementById('code').value = j.code;
  });
}
</script>
"""


@app.get("/api/stock/lookup")
def stock_lookup(q: str = ""):
    """代码/名称互查（同花顺标的检索）：返回 {code, name} 或 {error}。"""
    q = q.strip()
    if not q:
        return {"error": "空查询"}
    try:
        from app.collectors.ths_src import ThsSource
        items = ThsSource().search_ticker(q)
    except Exception as exc:  # noqa: BLE001
        return {"error": f"查询失败：{str(exc)[:100]}"}
    if not items:
        return {"error": "未找到匹配"}
    if q.isdigit():
        code = q.zfill(6)
        hit = next((i for i in items if i["code"] == code), None)
        if hit:
            return {"code": hit["code"], "name": hit["name"]}
    return {"code": items[0]["code"], "name": items[0]["name"]}


# ---------------- 自选股池（CHG-011：前端编辑；软件自选优先，空时回退同花顺自选） ----------------
def _load_watchlist() -> list[dict]:
    if not WATCHLIST_CFG.exists():
        return []
    return (_read_yaml(WATCHLIST_CFG).get("watchlist", []) or [])


def _save_watchlist(items: list[dict]) -> None:
    WATCHLIST_CFG.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_CFG.write_text(
        "# 自选股池（网页 /watchlist 维护；软件自选优先，清空后回退同花顺自选）\n"
        "# code: 6 位股票代码（字符串）；name: 备注名（可选）\n"
        + yaml.safe_dump({"watchlist": items}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist_page(msg: str = ""):
    items = _load_watchlist()
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    rows = "".join(
        f"<tr><td>{html.escape(str(w.get('code', '')))}</td><td>{html.escape(str(w.get('name') or ''))}</td>"
        f"<td><form method='post' action='/watchlist/delete' style='display:inline'>"
        f"<input type='hidden' name='code' value='{html.escape(str(w.get('code', '')))}'>"
        f"<button type='submit'>删除</button></form></td></tr>"
        for w in items
    )
    body = f"""{m}
<h1>自选股池</h1>
<div class="card">
  <h2>➕ 添加 / 更新</h2>
  <form method="post" action="/watchlist/add">
    <label>代码</label><input id="code" name="code" placeholder="600519" pattern="[0-9]{{6}}" onchange="autofillName()" required><br>
    <label>名称</label><input id="name" name="name" placeholder="可选，如：贵州茅台（填名称自动补代码）" onchange="autofillCode()"><br>
    <button type="submit">保存</button>
    <span class="small">（同代码会更新名称；填代码自动补名称，填名称自动补代码）</span>
  </form>
</div>
{AUTOFILL_JS}
<div class="card">
  <h2>📋 当前自选（{len(items)} 只）</h2>
  <table><thead><tr><th>代码</th><th>名称</th><th>操作</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=3>暂无自选（添加后生效；清空后回退同花顺自选）</td></tr>'}</tbody></table>
  <p class="small">软件自选优先于同花顺自选：列表非空时用这里的清单；清空后才回退同花顺自选（若已接入）</p>
</div>"""
    return _page("自选股池", body)


@app.post("/watchlist/add", response_class=HTMLResponse)
def watchlist_add(code: str = Form(""), name: str = Form("")):
    code = code.strip()
    if not re.fullmatch(r"\d{6}", code):
        return RedirectResponse("/watchlist?msg=" + _q("代码须为 6 位数字"), status_code=303)
    name = name.strip()
    items = [w for w in _load_watchlist() if str(w.get("code", "")).zfill(6) != code]   # 去重
    entry: dict = {"code": code}
    if name:
        entry["name"] = name
    items.append(entry)
    _save_watchlist(items)
    return RedirectResponse("/watchlist?msg=" + _q(f"已保存自选 {code}" + (f"（{name}）" if name else "")), status_code=303)


@app.post("/watchlist/delete", response_class=HTMLResponse)
def watchlist_delete(code: str = Form("")):
    code = code.strip()
    items = [w for w in _load_watchlist() if str(w.get("code", "")).zfill(6) != code]
    _save_watchlist(items)
    return RedirectResponse("/watchlist?msg=" + _q(f"已删除 {code}"), status_code=303)


# ---------------- 持仓（CHG-012：交易记录自动推算 + 手动补充；与自选分文件记录） ----------------
def _load_manual_positions() -> list[dict]:
    if not POSITIONS.exists():
        return []
    return (_read_yaml(POSITIONS).get("positions", []) or [])


def _save_manual_positions(items: list[dict]) -> None:
    cfg = _read_yaml(POSITIONS) if POSITIONS.exists() else {}
    cfg["positions"] = items
    POSITIONS.parent.mkdir(parents=True, exist_ok=True)
    POSITIONS.write_text(
        "# 持仓快照（网页 /positions 维护；自动持仓由交易记录推算，手动项覆盖/补充同代码）\n"
        "# 字段：code / name(可选) / shares / avg_cost / opened_at / stop_loss / plan_target\n"
        + yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _stock_names() -> dict[str, str]:
    return {str(w.get("code", "")).zfill(6): str(w.get("name") or "") for w in _load_watchlist()}


@app.get("/positions", response_class=HTMLResponse)
def positions_page(msg: str = ""):
    from app.analyzer.positions import compute_positions_from_trades, effective_positions
    names = _stock_names()
    auto = compute_positions_from_trades(_db())
    manual = _load_manual_positions()
    eff = effective_positions(_db(), manual)
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""

    def nm(c: str, n: str = "") -> str:
        return html.escape(n or names.get(c, ""))

    auto_rows = "".join(
        f"<tr><td>{html.escape(a['code'])}</td><td>{nm(a['code'])}</td>"
        f"<td>{a['shares']:g}</td><td>{a['avg_cost']:g}</td></tr>"
        for a in auto
    ) or '<tr><td colspan=4>暂无（交易记录无净买入）</td></tr>'

    man_rows = "".join(
        f"<tr><td>{html.escape(str(x.get('code','')))}</td><td>{nm(str(x.get('code','')), str(x.get('name') or ''))}</td>"
        f"<td>{x.get('shares', 0):g}</td><td>{x.get('avg_cost', 0):g}</td>"
        f"<td>{x.get('stop_loss') or '—'}</td><td>{x.get('plan_target') or '—'}</td>"
        f"<td>{x.get('opened_at') or '—'}</td>"
        f"<td><form method='post' action='/positions/delete' style='display:inline'>"
        f"<input type='hidden' name='code' value='{html.escape(str(x.get('code','')))}'>"
        f"<button type='submit'>删除</button></form></td></tr>"
        for x in manual
    ) or '<tr><td colspan=8>暂无手动补充</td></tr>'

    eff_rows = "".join(
        f"<tr><td>{html.escape(e['code'])}</td><td>{nm(e['code'], e.get('name',''))}</td>"
        f"<td>{e['shares']:g}</td><td>{e['avg_cost']:g}</td></tr>"
        for e in eff
    ) or '<tr><td colspan=4>暂无有效持仓</td></tr>'

    body = f"""{m}
<h1>持仓股票</h1>
<div class="card">
  <h2>➕ 手动补充 / 覆盖持仓</h2>
  <form method="post" action="/positions/add">
    <label>代码</label><input id="code" name="code" placeholder="600519" pattern="[0-9]{{6}}" onchange="autofillName()" required><br>
    <label>名称</label><input id="name" name="name" placeholder="填名称自动补代码" onchange="autofillCode()"><br>
    <label>股数</label><input name="shares" type="number" step="1" min="0" required><br>
    <label>成本(元)</label><input name="avg_cost" type="number" step="0.01" min="0" placeholder="可选"><br>
    <label>止损价</label><input name="stop_loss" type="number" step="0.01" placeholder="可选"><br>
    <label>目标价</label><input name="plan_target" type="number" step="0.01" placeholder="可选"><br>
    <label>开仓日</label><input name="opened_at" type="date" placeholder="可选"><br>
    <button type="submit">保存</button>
    <span class="small">（同代码会覆盖手动项）</span>
  </form>
</div>
{AUTOFILL_JS}
<div class="card">
  <h2>📈 自动持仓（交易记录推算）</h2>
  <table><thead><tr><th>代码</th><th>名称</th><th>净持股数</th><th>推算成本</th></tr></thead><tbody>{auto_rows}</tbody></table>
  <p class="small">净股数 = 买入 − 卖出（trades 表）；成本 = 买入加权均价。仅展示，不可在此改</p>
</div>
<div class="card">
  <h2>✏️ 手动持仓（补充 / 覆盖）</h2>
  <table><thead><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th><th>止损</th><th>目标</th><th>开仓日</th><th>操作</th></tr></thead><tbody>{man_rows}</tbody></table>
  <p class="small">手动项覆盖同代码的自动持仓；用于记录工具启用前的初始持仓、或补充止损/目标价</p>
</div>
<div class="card">
  <h2>✅ 有效持仓（日报实际采用 = 手动 > 自动）</h2>
  <table><thead><tr><th>代码</th><th>名称</th><th>股数</th><th>成本</th></tr></thead><tbody>{eff_rows}</tbody></table>
</div>"""
    return _page("持仓股票", body)


@app.post("/positions/add", response_class=HTMLResponse)
def positions_add(code: str = Form(""), name: str = Form(""), shares: str = Form(""),
                  avg_cost: str = Form(""), stop_loss: str = Form(""),
                  plan_target: str = Form(""), opened_at: str = Form("")):
    code = code.strip()
    if not re.fullmatch(r"\d{6}", code):
        return RedirectResponse("/positions?msg=" + _q("代码须为 6 位数字"), status_code=303)
    try:
        f_shares = float(shares)
        if f_shares < 0:
            raise ValueError
    except ValueError:
        return RedirectResponse("/positions?msg=" + _q("股数须为非负数字"), status_code=303)
    entry: dict = {"code": code, "shares": f_shares}
    if name.strip():
        entry["name"] = name.strip()
    if avg_cost.strip():
        entry["avg_cost"] = float(avg_cost)
    if stop_loss.strip():
        entry["stop_loss"] = float(stop_loss)
    if plan_target.strip():
        entry["plan_target"] = float(plan_target)
    if opened_at.strip():
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", opened_at):
            return RedirectResponse("/positions?msg=" + _q("开仓日格式应为 YYYY-MM-DD"), status_code=303)
        entry["opened_at"] = opened_at
    items = [x for x in _load_manual_positions() if str(x.get("code", "")).zfill(6) != code]
    items.append(entry)
    _save_manual_positions(items)
    return RedirectResponse("/positions?msg=" + _q(f"已保存持仓 {code}" + (f"（{name.strip()}）" if name.strip() else "")), status_code=303)


@app.post("/positions/delete", response_class=HTMLResponse)
def positions_delete(code: str = Form("")):
    code = code.strip()
    items = [x for x in _load_manual_positions() if str(x.get("code", "")).zfill(6) != code]
    _save_manual_positions(items)
    return RedirectResponse("/positions?msg=" + _q(f"已删除手动持仓 {code}"), status_code=303)


# ---------------- 设置：LLM 模型/API + 信息源（CHG-009） ----------------
LLM_CFG = PROJECT_ROOT / "config" / "llm.yaml"
SECRETS_CFG = PROJECT_ROOT / "config" / "secrets.yaml"
DS_CFG = PROJECT_ROOT / "config" / "data_sources.yaml"
STRATEGY_CFG = PROJECT_ROOT / "config" / "strategy.yaml"   # 交易策略库配置（CHG-010）
STRATEGY_DIR = PROJECT_ROOT / "data" / "strategies"        # 策略 md 空间


def _read_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {} if path.exists() else {}


def _valid_strategy_name(name: str) -> bool:
    """策略名校验：非空、≤40 字符、无路径分隔符/点号（防路径穿越）。"""
    return bool(name) and len(name) <= 40 and "/" not in name and "\\" not in name \
        and name not in (".", "..") and not name.startswith(".")


@app.get("/settings", response_class=HTMLResponse)
def settings_page(msg: str = ""):
    llm = _read_yaml(LLM_CFG).get("llm", {}) or {}
    sec = _read_yaml(SECRETS_CFG)
    llm_sec = sec.get("llm", {}) or {}
    ds = _read_yaml(DS_CFG)
    a_share_sources = (ds.get("fetch", {}) or {}).get("sources") or ["ths", "akshare"]
    news_sources = (ds.get("news", {}) or {}).get("sources") or ["cls"]
    ths_sec = sec.get("ths", {}) or {}
    has_key = bool(llm_sec.get("api_key"))
    has_ths = bool(ths_sec.get("api_key"))
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    models = ["deepseek-chat", "deepseek-reasoner"]
    model_opts = "".join(f"<option {'selected' if llm.get('model','deepseek-chat')==x else ''}>{x}</option>" for x in models)
    body = f"""{m}
<h1>设置</h1>
<div class="card">
  <h2>🤖 LLM 模型（日报交易复盘分析）</h2>
  <form method="post" action="/settings/llm">
    <label>启用</label>
    <select name="enabled"><option value="1" {'selected' if llm.get('enabled') else ''}>开</option><option value="0" {'selected' if not llm.get('enabled') else ''}>关</option></select><br>
    <label>模型</label><select name="model">{model_opts}</select><br>
    <label>API 地址</label><input name="base_url" value="{html.escape(llm.get('base_url','https://api.deepseek.com'))}" style="min-width:300px"><br>
    <label>API Key</label><input name="api_key" type="password" placeholder="{'已配置，留空不修改' if has_key else '填入 API Key'}" style="min-width:300px"><br>
    <button type="submit">保存 LLM 配置</button>
  </form>
  <p class="small">模型名需与 API 服务匹配（DeepSeek 官方为 deepseek-chat / deepseek-reasoner；别名 v4-flash 会返回 400，请勿选用）</p>
</div>
<div class="card">
  <h2>📈 A股交易数据源</h2>
  <form method="post" action="/settings/source">
    <label style="min-width:0"><input type="checkbox" name="a_share_sources" value="ths" {'checked' if 'ths' in a_share_sources else ''}> 同花顺官方（需 Key）</label><br>
    <label style="min-width:0"><input type="checkbox" name="a_share_sources" value="akshare" {'checked' if 'akshare' in a_share_sources else ''}> akshare（免费开源）</label><br>
    <label>同花顺 Key</label><input name="ths_key" type="password" placeholder="{'已配置，留空不修改' if has_ths else '填入同花顺 API Key'}" style="min-width:300px"><br>
    <button type="submit">保存 A股数据源</button>
  </form>
  <p class="small">可多选：勾选多个时按顺序降级（同花顺优先，失败自动用 akshare）</p>
</div>
<div class="card">
  <h2>📰 新闻数据源</h2>
  <form method="post" action="/settings/news">
    <label style="min-width:0"><input type="checkbox" name="news_sources" value="cls" {'checked' if 'cls' in news_sources else ''}> 财联社电报</label><br>
    <button type="submit">保存新闻数据源</button>
  </form>
  <p class="small">可多选：当前仅财联社；取消勾选则停用新闻快讯</p>
</div>"""
    return _page("设置", body)


@app.post("/settings/llm", response_class=HTMLResponse)
def settings_llm(enabled: str = Form("0"), model: str = Form("deepseek-chat"),
                 base_url: str = Form(""), api_key: str = Form("")):
    llm = _read_yaml(LLM_CFG)
    llm["llm"] = {**llm.get("llm", {}),
                  "enabled": enabled == "1", "model": model.strip(),
                  "base_url": base_url.strip() or "https://api.deepseek.com"}
    yaml.safe_dump(llm, LLM_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    if api_key.strip():   # 仅当填写时更新 key（留空不改）
        sec = _read_yaml(SECRETS_CFG)
        sec.setdefault("llm", {})["api_key"] = api_key.strip()
        yaml.safe_dump(sec, SECRETS_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    return RedirectResponse("/settings?msg=" + _q("LLM 配置已保存"), status_code=303)


@app.post("/settings/source", response_class=HTMLResponse)
def settings_source(a_share_sources: list[str] = Form([]), ths_key: str = Form("")):
    a_share_sources = [s for s in a_share_sources if s in ("ths", "akshare")]
    if not a_share_sources:
        a_share_sources = ["akshare"]   # 至少保留免费兜底源
    ds = _read_yaml(DS_CFG)
    ds.setdefault("fetch", {})["sources"] = a_share_sources
    yaml.safe_dump(ds, DS_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    if ths_key.strip():
        sec = _read_yaml(SECRETS_CFG)
        sec.setdefault("ths", {})["api_key"] = ths_key.strip()
        yaml.safe_dump(sec, SECRETS_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    return RedirectResponse("/settings?msg=" + _q("A股数据源已保存"), status_code=303)


@app.post("/settings/news", response_class=HTMLResponse)
def settings_news(news_sources: list[str] = Form([])):
    news_sources = [s for s in news_sources if s == "cls"]
    ds = _read_yaml(DS_CFG)
    ds.setdefault("news", {})["sources"] = news_sources   # 空 = 停用新闻
    yaml.safe_dump(ds, DS_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    msg = "新闻数据源已停用" if not news_sources else "新闻数据源已保存"
    return RedirectResponse("/settings?msg=" + _q(msg), status_code=303)


# ---------------- 交易策略库（CHG-010：基础模板 + 上传/查看/启用） ----------------
@app.get("/strategy", response_class=HTMLResponse)
def strategy_page(msg: str = ""):
    cfg = _read_yaml(STRATEGY_CFG)
    active = str((cfg.get("strategy", {}) or {}).get("active", "") or "")
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    files = sorted(STRATEGY_DIR.glob("*.md"))
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    opts = '<option value="">（不启用策略分析）</option>' + "".join(
        f"<option value='{html.escape(f.stem)}' {'selected' if f.stem == active else ''}>{html.escape(f.stem)}</option>"
        for f in files
    )
    rows = "".join(
        f"<tr><td>{html.escape(f.stem)}</td>"
        f"<td>{'✅ 启用中' if f.stem == active else '—'}</td>"
        f"<td><a href='/strategy/view/{_q(f.stem)}'>查看内容</a></td></tr>"
        for f in files
    )
    body = f"""{m}
<h1>📚 交易策略库</h1>
<div class="card">
  <h2>🎯 启用策略</h2>
  <form method="post" action="/strategy/select">
    <label>策略</label><select name="name">{opts}</select>
    <button type="submit">保存选择</button>
  </form>
  <p class="small">启用后，日报的 LLM 分析会按该策略框架对照市场数据 + 交易记录输出（「🤖 交易复盘分析」段标注策略名）</p>
</div>
<div class="card">
  <h2>📚 策略列表</h2>
  <table><thead><tr><th>策略名</th><th>状态</th><th>操作</th></tr></thead>
  <tbody>{rows or '<tr><td colspan=3>暂无策略（用下方上传或新建）</td></tr>'}</tbody></table>
</div>
<div class="card">
  <h2>⬆️ 上传策略（.md 文件）</h2>
  <form method="post" action="/strategy/upload" enctype="multipart/form-data">
    <input type="file" name="file" accept=".md,.markdown,text/markdown" required>
    <button type="submit">上传</button>
  </form>
  <p class="small">文件名（去 .md 后缀）即策略名；同名文件会覆盖</p>
</div>
<div class="card">
  <h2>✏️ 新建策略</h2>
  <form method="post" action="/strategy/new">
    <label>策略名</label><input name="name" placeholder="如：波段操作" required><br>
    <label>内容</label><br><textarea name="content" rows="8" style="width:100%" placeholder="用 Markdown 描述策略框架 / 信号定义 / 关注点"></textarea><br>
    <button type="submit">保存</button>
  </form>
</div>"""
    return _page("交易策略库", body)


@app.post("/strategy/select", response_class=HTMLResponse)
def strategy_select(name: str = Form("")):
    name = name.strip()
    cfg = _read_yaml(STRATEGY_CFG)
    if name and not (STRATEGY_DIR / f"{name}.md").exists():
        return RedirectResponse("/strategy?msg=" + _q("策略不存在"), status_code=303)
    cfg.setdefault("strategy", {})["active"] = name
    yaml.safe_dump(cfg, STRATEGY_CFG.open("w", encoding="utf-8"), allow_unicode=True, sort_keys=False)
    msg = "已停用策略分析（LLM 仅做交易复盘）" if not name else f"已启用策略：{name}"
    return RedirectResponse("/strategy?msg=" + _q(msg), status_code=303)


@app.post("/strategy/upload", response_class=HTMLResponse)
async def strategy_upload(file: UploadFile = File(...)):
    raw = (file.filename or "").strip()
    name = raw[:-3] if raw.lower().endswith(".md") else (raw[:-9] if raw.lower().endswith(".markdown") else raw)
    name = name.strip()
    if not _valid_strategy_name(name):
        return RedirectResponse("/strategy?msg=" + _q("文件名无效（须为 .md/.markdown，策略名 1-40 字符、无路径分隔符）"), status_code=303)
    data = await file.read()
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / f"{name}.md").write_text(data.decode("utf-8", errors="replace"), encoding="utf-8")
    return RedirectResponse("/strategy?msg=" + _q(f"已上传策略：{name}"), status_code=303)


@app.post("/strategy/new", response_class=HTMLResponse)
def strategy_new(name: str = Form(""), content: str = Form("")):
    name = name.strip()
    if not _valid_strategy_name(name):
        return RedirectResponse("/strategy?msg=" + _q("策略名无效（1-40 字符、无路径分隔符）"), status_code=303)
    if not content.strip():
        return RedirectResponse("/strategy?msg=" + _q("策略内容不能为空"), status_code=303)
    STRATEGY_DIR.mkdir(parents=True, exist_ok=True)
    (STRATEGY_DIR / f"{name}.md").write_text(content.strip() + "\n", encoding="utf-8")
    return RedirectResponse("/strategy?msg=" + _q(f"已新建策略：{name}"), status_code=303)


@app.get("/strategy/view/{name}", response_class=HTMLResponse)
def strategy_view(name: str):
    if not _valid_strategy_name(name):
        return _page("策略", '<p class="err">策略名无效</p>')
    f = STRATEGY_DIR / f"{name}.md"
    if not f.exists():
        return _page("策略", '<p class="err">未找到该策略</p>')
    return _page(f"策略 {name}",
                 f"<h1>策略：{html.escape(name)}</h1><div class='card'>{md_to_html(f.read_text(encoding='utf-8'))}</div>")


# ---------------- 录入：read（价值验收） ----------------
@app.get("/record/read", response_class=HTMLResponse)
def record_read_page(msg: str = ""):
    opts = "".join(f"<option>{s}</option>" for s in READ_SECTIONS)
    m = f'<div class="msg">{html.escape(msg)}</div>' if msg else ""
    body = f"""{m}
<h1>记录价值验收（今天哪段有用）</h1>
<div class="card">
<form method="post" action="/record/read">
  <label>段落</label><select name="section">{opts}</select><br>
  <label>评价</label>
  <select name="verdict"><option>有用</option><option>没用</option></select><br>
  <label>日期</label><input name="d" value="{date.today().isoformat()}" type="date"><br>
  <button type="submit">保存</button>
</form>
<p class="small">每天读完后记一段；跳过率 &gt; 50% 的段落将自动删除（C5）</p>
</div>"""
    return _page("记录 read", body)


@app.post("/record/read", response_class=HTMLResponse)
def record_read_submit(section: str = Form(...), verdict: str = Form(...), d: str = Form(...)):
    if section not in READ_SECTIONS or verdict not in ("有用", "没用"):
        return RedirectResponse("/record/read?msg=" + _q("段落或评价无效"), status_code=303)
    content = json.dumps({"section": section, "verdict": verdict, "date": d}, ensure_ascii=False)
    ts = f"{d}T12:00:00+00:00"
    conn = sqlite3.connect(_db())
    try:
        conn.execute("INSERT INTO notes(ts, kind, content) VALUES(?, 'read', ?)", (ts, content))
        conn.commit()
    finally:
        conn.close()
    return RedirectResponse("/record/read?msg=" + _q(f"已记录 {d} {section} → {verdict}"), status_code=303)


# ---------------- 统计 ----------------
@app.get("/stats/read", response_class=HTMLResponse)
def stats_read(days: int = 20):
    import subprocess
    p = subprocess.run([sys.executable, "-X", "utf8", str(PROJECT_ROOT / "scripts" / "report_read.py"),
                        "--days", str(days)], capture_output=True, text=True)
    return _page("价值验收统计", f"<h1>价值验收统计（近 {days} 天）</h1><pre style='font-size:14px'>{html.escape(p.stdout)}</pre>")


@app.get("/stats/hits", response_class=HTMLResponse)
def stats_hits(days: int = 30):
    import subprocess
    p = subprocess.run([sys.executable, "-X", "utf8", str(PROJECT_ROOT / "scripts" / "report_hits.py"),
                        "--days", str(days)], capture_output=True, text=True)
    return _page("预测命中率", f"<h1>预测命中率（近 {days} 天）</h1><pre style='font-size:14px'>{html.escape(p.stdout)}</pre>")


def _q(s: str) -> str:
    import urllib.parse
    return urllib.parse.quote(s)


def main() -> None:
    global _db_path
    parser = argparse.ArgumentParser(description="GRGPZS 本地网页界面（仅监听 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", type=str, default="", help="数据库路径（默认 data/processed/app.db；测试注入用）")
    args = parser.parse_args()
    if args.db:
        _db_path = Path(args.db)
    import uvicorn
    print(f"GRGPZS 本地界面启动：http://127.0.0.1:{args.port}（Ctrl+C 停止）")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
