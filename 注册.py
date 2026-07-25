#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键注册：注册.py [并发] [数量]

用法:
  python 注册.py 2 10     # 并发2，注册10个后停
  python 注册.py 1 0      # 并发1，无限注册（Ctrl+C 停）
  python 注册.py 0 0      # 并发默认1，无限注册
  python 注册.py          # 默认 并发1 数量1
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="注册.py",
        description="UI Sync 注册（Camoufox 子进程）",
    )
    p.add_argument(
        "concurrency",
        nargs="?",
        type=int,
        default=1,
        help="并发数；0 视为 1",
    )
    p.add_argument(
        "count",
        nargs="?",
        type=int,
        default=1,
        help="注册数量；0=无限一直注册",
    )
    p.add_argument("--debug", action="store_true", default=True, help="debug 日志（默认开）")
    p.add_argument("--no-debug", dest="debug", action="store_false")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    conc = int(args.concurrency or 0)
    if conc <= 0:
        conc = 1
    count = int(args.count or 0)
    if count < 0:
        count = 0

    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)

    env = os.environ.copy()
    env.update(
        {
            "PYTHONUNBUFFERED": "1",
            "UI_SYNC_CAMOUFOX": "1",
            "UI_SYNC_SUBPROCESS": "1",
            "UI_SYNC_PERSISTENT": "0",
            "UI_SYNC_AUTO_CLASH_PROXY": "0",
            "UI_SYNC_NO_PROXY": "1",
            "REGISTER_MODE": "ui",
            "BROWSER_ENGINE": "camoufox",
            "REGISTER_BROWSER": "camoufox",
            "BROWSER_OS": "windows",
            "BROWSER_HEADLESS": "1",
            "REGISTER_LOG_MODE": "debug" if args.debug else "info",
            "C_CONSUME_TIMEOUT": env.get("C_CONSUME_TIMEOUT") or "360",
            "PHYSICAL_CAP": str(conc),
            "S_WORKERS": str(conc),
            "P_WORKERS": str(conc),
            "C_WORKERS": str(conc),
            "P_BATCH_MAX": str(conc),
            "Q_PENDING_CAP": str(max(2, conc * 2)),
            "TARGET": str(count),
        }
    )

    print("=" * 40, flush=True)
    print("  注册脚本", flush=True)
    print(f"  并发={conc}  目标={count if count else '无限'}  python={py}", flush=True)
    print(f"  目录={ROOT}", flush=True)
    print("=" * 40, flush=True)
    print(f"[*] SSO 输出: {ROOT / 'keys' / 'auth-sessions.jsonl'}", flush=True)
    print("[*] 完成后可运行: python 授权.py", flush=True)
    print(flush=True)

    cmd = [str(py), "-u", "-m", "grok_register.register", "--target", str(count)]
    if args.debug:
        cmd.append("--debug")
    return int(subprocess.call(cmd, cwd=str(ROOT), env=env))


if __name__ == "__main__":
    raise SystemExit(main())
