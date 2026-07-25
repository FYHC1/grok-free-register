#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键授权：授权.py [数量] [起始index] [间隔秒]

用法:
  python 授权.py              # 处理全部未授权；已授权自动跳过
  python 授权.py 0            # 同上
  python 授权.py 50           # 最多新授 50 个
  python 授权.py 50 0 3       # 从 index0 起最多50，间隔3秒
  python 授权.py --force      # 强制重授（不跳过已成功）
  python 授权.py force        # 同上

台账: keys/cpa-enroll-ledger.jsonl
本地: auth-local/authenticated/xai-*.json
CPA:  --upload-cpa（需 .env 配置）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCE = ROOT / "keys" / "auth-sessions.jsonl"
DEFAULT_LEDGER = ROOT / "keys" / "cpa-enroll-ledger.jsonl"
DEFAULT_JSON_OUT = ROOT / "keys" / "oauth_enroll_last.json"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="授权.py",
        description="SSO → OAuth auth-code(grok-build) → 本地 xai-*.json + 上传 CPA",
    )
    p.add_argument(
        "count",
        nargs="?",
        default="0",
        help="处理数量；0=全部剩余未授权；也可写 force",
    )
    p.add_argument(
        "index",
        nargs="?",
        default="0",
        help="起始 index（默认 0）；也可写 force",
    )
    p.add_argument(
        "interval",
        nargs="?",
        type=float,
        default=3.0,
        help="每个账号间隔秒（默认 3）",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="强制重授，不跳过已授权/台账 ok",
    )
    p.add_argument(
        "--source-file",
        default=str(DEFAULT_SOURCE),
        help="SSO 源文件，默认 keys/auth-sessions.jsonl",
    )
    p.add_argument(
        "--no-upload",
        action="store_true",
        help="只落本地，不上传 CPA",
    )
    p.add_argument(
        "--proxy",
        default="",
        help="可选代理，如 http://127.0.0.1:7897",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    force = bool(args.force)
    count_raw = str(args.count or "0").strip()
    index_raw = str(args.index or "0").strip()
    if count_raw.lower() == "force":
        force = True
        count_raw = "0"
    if index_raw.lower() == "force":
        force = True
        index_raw = "0"

    try:
        count = int(count_raw)
    except ValueError:
        raise SystemExit(f"无效 count: {count_raw}")
    try:
        index = int(index_raw)
    except ValueError:
        raise SystemExit(f"无效 index: {index_raw}")
    if count < 0:
        count = 0
    if index < 0:
        index = 0

    py = ROOT / ".venv" / "Scripts" / "python.exe"
    if not py.exists():
        py = ROOT / ".venv" / "bin" / "python"
    if not py.exists():
        py = Path(sys.executable)

    source = Path(args.source_file)
    if not source.is_absolute():
        source = ROOT / source
    if not source.exists():
        print(f"[!] 没有 {source}，请先运行: python 注册.py", flush=True)
        return 2

    script = ROOT / "scripts" / "sso_auth_code_enroll.py"
    if not script.exists():
        print(f"[!] 缺少 {script}", flush=True)
        return 2

    cmd = [
        str(py),
        "-u",
        str(script),
        "--source-file",
        str(source),
        "--index",
        str(index),
        "--count",
        str(count),
        "--interval",
        str(float(args.interval)),
        "--allow-no-proxy",
        "--ledger",
        str(DEFAULT_LEDGER),
        "--json-out",
        str(DEFAULT_JSON_OUT),
    ]
    if args.no_upload:
        cmd.append("--no-upload")
    else:
        cmd.append("--upload-cpa")
    if args.proxy:
        cmd.extend(["--proxy", args.proxy])
    if force:
        cmd.extend(["--force-reauth", "--retry-failed"])
    else:
        cmd.extend(["--skip-existing", "--skip-failed", "--retry-legacy-fails"])

    print("=" * 40, flush=True)
    print("  授权脚本  auth-code / grok-build", flush=True)
    print(
        f"  count={count if count else '全部剩余'}  index={index}  "
        f"interval={args.interval}  force={int(force)}  upload={int(not args.no_upload)}",
        flush=True,
    )
    print(f"  源={source}", flush=True)
    print(f"  台账={DEFAULT_LEDGER}  （已 ok 下次自动跳过）", flush=True)
    print(f"  本地={ROOT / 'auth-local' / 'authenticated'}", flush=True)
    print("=" * 40, flush=True)
    print(flush=True)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    code = int(subprocess.call(cmd, cwd=str(ROOT), env=env))
    print(flush=True)
    print(f"[*] 授权结束 code={code}", flush=True)
    print(f"    本地: {ROOT / 'auth-local' / 'authenticated'}", flush=True)
    print(f"    台账: {DEFAULT_LEDGER}", flush=True)
    print(f"    摘要: {DEFAULT_JSON_OUT}", flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
