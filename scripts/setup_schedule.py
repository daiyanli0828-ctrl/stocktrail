#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
setup_schedule.py：Windows 计划任务注册（P-06；企划 v1.0 §2.5 C18）
不用 Docker、不用 APScheduler 常驻——用系统 schtasks。

注册任务：每个任务 = 一个 .bat（跑 python + 失败气泡通知）+ 一个 .vbs（wscript 隐藏启动
bat，避免每次触发弹出空白 cmd 黑窗闪退）；/TR 只指向单文件 .vbs，规避嵌套引号问题。
vbs 用 Run(bat, 0, True) 同步等待并转发退出码，LastTaskResult 仍反映 python 成败。
  GRGPZS_Increment  每天 07:30   daily_increment.py    （休市日脚本内跳过）
  GRGPZS_StopWatch  每 5 分钟    stop_watch.py --once  （脚本内检查交易日 + 交易时段，C10）
  GRGPZS_News       每小时       news_fetch.py        （脚本内仅在交易日+交易时段抓取，CHG-014）
  （GRGPZS_Daily 已取消：日报含 LLM 交易复盘分析，交易记录手动录入，日报改网页一键生成，CHG-009）
日志：data/processed/logs/<task>.log（追加）

用法：
  python scripts/setup_schedule.py --install [--dry-run]   # 注册（--dry-run 只打印）
  python scripts/setup_schedule.py --remove [--dry-run]    # 卸载
  python scripts/setup_schedule.py --list                  # 查看
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
SCHED_DIR = PROJECT_ROOT / "data" / "processed" / "schedule"
LOGS_DIR = PROJECT_ROOT / "data" / "processed" / "logs"

TASKS = {
    # GRGPZS_Daily 已取消（CHG-009：日报需 LLM 分析交易记录，交易记录手动录入，日报改网页一键生成/手动）
    "GRGPZS_Increment": {"schedule": ("DAILY", {"ST": "07:30"}), "script": "daily_increment.py", "args": ""},
    "GRGPZS_StopWatch": {"schedule": ("MINUTE", {"MO": "5"}), "script": "stop_watch.py", "args": "--once"},
    # 盘中新闻定时累积（CHG-014 二次修订）：每小时一次，脚本内仅在交易日+交易时段才抓取
    "GRGPZS_News": {"schedule": ("HOURLY", {"MO": "1"}), "script": "news_fetch.py", "args": ""},
}


def write_bat(name: str, task: dict) -> Path:
    """生成任务 bat 包装（内部引号由 cmd 解析，外部 /TR 只指向文件路径）。
    失败通知：exit code ≠ 0 时调用 notify_failure.ps1 弹桌面通知（P-06 评审 P1 修复；
    通知层为过渡方案，整体产品形态确定后可替换）。"""
    SCHED_DIR.mkdir(parents=True, exist_ok=True)
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    bat = SCHED_DIR / f"{name}.bat"
    log = LOGS_DIR / f"{task['script'].replace('.py', '')}.log"
    ps1 = PROJECT_ROOT / "scripts" / "notify_failure.ps1"
    title = name.replace("GRGPZS_", "GRGPZS ")
    # 备注 1/2 加固：chcp 65001 防代码页差异；set RC 保存 python 退出码，exit /b 保证
    # 任务退出码反映 python 成败（而非末尾 powershell 通知的退出码）
    bat.write_text(
        "@echo off\r\n"
        "chcp 65001 >nul\r\n"
        f'cd /d "{PROJECT_ROOT}"\r\n'
        f'"{PYTHON}" -X utf8 scripts\\{task["script"]} {task["args"]} >> "{log}" 2>&1\r\n'
        "set RC=%errorlevel%\r\n"
        "if %RC% neq 0 (\r\n"
        f'  powershell -NoProfile -ExecutionPolicy Bypass -File "{ps1}" '
        f'-Title "{title} 失败" -Message "详情见 {log}"\r\n'
        ")\r\n"
        "exit /b %RC%\r\n",
        encoding="utf-8",
    )
    return bat


def write_vbs(name: str, bat: Path) -> Path:
    """生成 wscript 隐藏启动器：无窗口运行 bat，同步等待并转发退出码。
    Run(bat, 0, True)：0=隐藏窗口；True=等待 bat 结束、返回其退出码。"""
    vbs = SCHED_DIR / f"{name}.vbs"
    vbs.write_text(
        "Set sh = CreateObject(\"WScript.Shell\")\r\n"
        f'rc = sh.Run("{bat}", 0, True)\r\n'
        "WScript.Quit rc\r\n",
        encoding="ascii",
    )
    return vbs


def task_command(name: str, action: str) -> list[str]:
    task = TASKS[name]
    if action == "Delete":
        return ["schtasks", "/Delete", "/TN", name, "/F"]
    sched_type, sched_opts = task["schedule"]
    bat = write_bat(name, task)          # python 执行 + 失败气泡通知（保持退出码语义）
    launcher = write_vbs(name, bat)      # wscript 隐藏宿主，不闪 cmd 黑窗
    cmd = ["schtasks", "/Create", "/F", "/TN", name, "/SC", sched_type]
    for k, v in sched_opts.items():
        cmd += [f"/{k}", v]
    cmd += ["/TR", str(launcher)]        # /TR 只指向单文件 .vbs（单路径，无嵌套引号）
    return cmd


def run_schtasks(cmd: list[str], dry_run: bool) -> tuple[int, str]:
    if dry_run:
        return 0, " ".join(cmd)
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="GRGPZS Windows 计划任务（C18，不用 APScheduler）")
    parser.add_argument("--install", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--task", type=str, default="", help="只操作指定任务（如 GRGPZS_News）")
    args = parser.parse_args()

    tasks = {k: v for k, v in TASKS.items() if not args.task or k == args.task}
    if args.task and args.task not in TASKS:
        print(f"未知任务：{args.task}（可选：{', '.join(TASKS)}）")
        return

    if args.list:
        for name in tasks:
            p = subprocess.run(["schtasks", "/Query", "/FO", "LIST", "/TN", name],
                               capture_output=True, text=True)
            ok = "TaskName" in (p.stdout + p.stderr) or "GRGPZS" in (p.stdout + p.stderr)
            print(f"{name}: {'已注册' if ok else '未注册'}")
            if ok and p.stdout:
                print("  " + p.stdout.strip().splitlines()[2] if len(p.stdout.splitlines()) > 2 else "")
        return

    if args.remove:
        for name in tasks:
            rc, out = run_schtasks(task_command(name, "Delete"), args.dry_run)
            print(f"[{'dry-run' if args.dry_run else 'ok'}] 卸载 {name}: {out[:120]}")
        return

    if args.install:
        for name in tasks:
            cmd = task_command(name, "Create")
            rc, out = run_schtasks(cmd, args.dry_run)
            status = "dry-run" if args.dry_run else ("ok" if rc == 0 else f"失败({rc})")
            print(f"[{status}] {name}: {out[:160]}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
