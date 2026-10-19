# -*- coding: utf-8 -*-
"""P-13 web_ui 自测：页面/录入/护栏（验证后删除）"""
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parents[1]
TDB = ROOT / "data" / "processed" / "test" / "ui_test.db"
PORT = 8123
BASE = f"http://127.0.0.1:{PORT}"

PASS, FAIL = "OK", "FAIL"
results = []


def expect(label, cond, detail=""):
    results.append((label, bool(cond), detail))
    return bool(cond)


if TDB.exists():
    TDB.unlink()
subprocess.run([sys.executable, "-X", "utf8", str(ROOT / "scripts" / "init_db.py"), "--db", str(TDB)],
               capture_output=True, check=True)

# 启动 web_ui（后台）
proc = subprocess.Popen(
    [sys.executable, "-X", "utf8", str(ROOT / "scripts" / "web_ui.py"), "--port", str(PORT), "--db", str(TDB)],
    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
)
try:
    # 等待端口就绪
    ready = False
    for _ in range(30):
        try:
            requests.get(BASE, timeout=1)
            ready = True
            break
        except Exception:
            time.sleep(0.5)
    expect("服务启动", ready, "")

    # ========== 展示页 ==========
    r = requests.get(BASE + "/", timeout=5)
    expect("首页 200 + 日报列表", r.status_code == 200 and "晚间主报" in r.text, "")
    r = requests.get(BASE + "/daily/2026-08-31", timeout=5)
    expect("日报页 200 + 表格渲染", r.status_code == 200 and "<table>" in r.text and "市场概况" in r.text, "")
    expect("日报页含免责声明", "不构成任何投资建议" in r.text, "")
    r = requests.get(BASE + "/daily/1999-01-01", timeout=5)
    expect("不存在日报提示", r.status_code == 200 and "未找到" in r.text, "")

    # ========== 录入页 ==========
    r = requests.get(BASE + "/record/note", timeout=5)
    expect("note 表单页（记录交易）", r.status_code == 200 and "记录交易操作" in r.text and "reason" in r.text, "")
    r = requests.get(BASE + "/record/read", timeout=5)
    expect("read 表单页", r.status_code == 200 and "价值验收" in r.text and "市场概况" in r.text, "")

    # ========== POST 录入（allow_redirects=False 检查 303） ==========
    import urllib.parse
    # 缺价格 → 拒绝（记录实际交易，价格数量必填）
    r = requests.post(BASE + "/record/note", data={"side": "buy", "code": "600519",
                       "reason": "趋势"}, timeout=5, allow_redirects=False)
    expect("缺价格被拒（msg 提示）", r.status_code == 303 and "成交价与数量必填" in urllib.parse.unquote(r.headers.get("location", "")),
           r.headers.get("location", ""))
    # 成交 + 开放理由
    r = requests.post(BASE + "/record/note", data={"side": "buy", "code": "000001",
                       "price": "11.5", "shares": "1000",
                       "reason": "突破平台回调，看好中期业绩，分批建仓"},
                      timeout=5, allow_redirects=False)
    expect("成交 POST 303", r.status_code == 303, str(r.status_code))
    # 卖出 + 持股天数/盈亏（CHG-008）
    r = requests.post(BASE + "/record/note", data={"side": "sell", "code": "600519",
                       "price": "1600", "shares": "100", "reason": "到达目标价止盈",
                       "holding_days": "45", "pnl": "10000"},
                      timeout=5, allow_redirects=False)
    expect("卖出 POST 303（含持股天数/盈亏）", r.status_code == 303, str(r.status_code))
    # read
    r = requests.post(BASE + "/record/read", data={"section": "市场概况", "verdict": "有用",
                       "d": "2026-08-31"}, timeout=5, allow_redirects=False)
    expect("read POST 303", r.status_code == 303, str(r.status_code))

    conn = sqlite3.connect(TDB)
    n_trade = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    rn = conn.execute("SELECT reason_note, reason_type FROM trades WHERE code='000001'").fetchone()
    sell = conn.execute("SELECT holding_days, pnl FROM trades WHERE side='sell'").fetchone()
    n_read = conn.execute("SELECT COUNT(*) FROM notes WHERE kind='read'").fetchone()[0]
    n_plan = conn.execute("SELECT COUNT(*) FROM notes WHERE kind='plan'").fetchone()[0]
    conn.close()
    expect("交易落库", n_trade == 2, str(n_trade))
    expect("开放理由存 reason_note（reason_type 留空）",
           rn[0] == "突破平台回调，看好中期业绩，分批建仓" and rn[1] is None, str(rn))
    expect("卖出持股天数/盈亏落库（CHG-008）", sell[0] == 45.0 and sell[1] == 10000.0, str(sell))
    expect("read 落库", n_read == 1, str(n_read))
    expect("网页不再生成盘前承诺 plan", n_plan == 0, str(n_plan))

    # ========== 统计页 ==========
    r = requests.get(BASE + "/stats/read?days=5", timeout=10)
    expect("价值验收统计页", r.status_code == 200 and "跳过率" in r.text, "")

    # ========== 账户模块（投入 + 持仓占比） ==========
    acc = ROOT / "data" / "inputs" / "account.yaml"
    acc_bak = acc.read_text(encoding="utf-8") if acc.exists() else None
    try:
        r = requests.get(BASE + "/account", timeout=5)
        expect("账户页 200 + 持仓占比", r.status_code == 200 and "持仓占比" in r.text and "累计投入" in r.text, "")
        r = requests.post(BASE + "/account", data={"invested": "200000"}, timeout=5, allow_redirects=False)
        expect("投入保存 303", r.status_code == 303, str(r.status_code))
        acc_data = yaml.safe_load(acc.read_text(encoding="utf-8"))
        expect("account.yaml 写入 invested", acc_data["account"]["invested"] == 200000.0, str(acc_data["account"]))
        r = requests.get(BASE + "/account", timeout=5)
        expect("账户页回显投入", "200000" in r.text, "")
    finally:
        if acc_bak is not None:
            acc.write_text(acc_bak, encoding="utf-8")
        else:
            acc.write_text("account:\n  invested: 0\n  updated_at: \"\"\n", encoding="utf-8")

    # ========== 行为护栏 ==========
    text = requests.get(BASE + "/", timeout=5).text + requests.get(BASE + "/daily/2026-08-31", timeout=5).text
    for banned in ["建议买入", "建议卖出", "建议加仓", "建议减仓", "推荐买入", "可买"]:
        expect(f"页面无[{banned}]", banned not in text, "")
finally:
    proc.terminate()
    proc.wait(timeout=10)

print()
all_ok = all(ok for _, ok, _ in results)
for label, ok, detail in results:
    print(f"  [{PASS if ok else FAIL}] {label}（{detail}）")
print("P-13 web_ui 自测结论:", "全部通过" if all_ok else "存在失败")
sys.exit(0 if all_ok else 1)
