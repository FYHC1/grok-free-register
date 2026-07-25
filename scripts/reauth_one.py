#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-auth one xAI account via Auth Code + PKCE (Grok Build).

Usage (WSL project root):
  .venv/bin/python scripts/reauth_one.py ocxxxx@mail.example.com
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    p = argparse.ArgumentParser(description="Force re-auth one email via Auth Code (grok-build)")
    p.add_argument("email", help="full email, e.g. ocxxxx@mail.example.com")
    p.add_argument(
        "--source-file",
        default=str(ROOT / "keys" / "auth-sessions.jsonl"),
        help="SSO source jsonl",
    )
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--no-upload", action="store_true")
    p.add_argument("--proxy", default="")
    # keep old flags for compatibility (ignored)
    p.add_argument("--headed", action="store_true", help="ignored (auth-code needs no browser)")
    p.add_argument("--browser-timeout", type=float, default=120.0)
    p.add_argument("--poll-timeout", type=float, default=180.0)
    args = p.parse_args()

    email = args.email.strip().lower()
    if "@" not in email:
        raise SystemExit("email must be full address like ocxxxx@mail.example.com")

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "sso_auth_code_enroll.py"),
        "--source-file",
        str(Path(args.source_file)),
        "--email",
        email,
        "--force-reauth",
        "--interval",
        str(args.interval),
    ]
    if args.no_upload:
        cmd.append("--no-upload")
    if args.proxy:
        cmd.extend(["--proxy", args.proxy])
    print("[*] " + " ".join(cmd), flush=True)
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
