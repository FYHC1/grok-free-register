#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Split auth-sessions.jsonl into old/new by estimated register time.

created_est = max(cookie.expires) - 180 days
Default cutoff: 2026-07-24 00:00 Asia/Shanghai
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

CST = timezone(timedelta(hours=8))
TTL = 180 * 86400


def main() -> int:
    p = argparse.ArgumentParser(description="Split SSO sessions by estimated age")
    p.add_argument("--source-file", default=str(ROOT / "keys" / "auth-sessions.jsonl"))
    p.add_argument("--cutoff", default="2026-07-24", help="YYYY-MM-DD (Asia/Shanghai)")
    p.add_argument("--domain", default="")
    p.add_argument("--out-dir", default=str(ROOT / "keys"))
    args = p.parse_args()

    domain = (
        args.domain
        or os.environ.get("XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN")
        or os.environ.get("EMAIL_DOMAIN")
        or "mail.example.com"
    ).strip().lower().lstrip("@")
    y, m, d = [int(x) for x in args.cutoff.split("-")]
    cutoff = datetime(y, m, d, 0, 0, tzinfo=CST)
    cutoff_ts = cutoff.timestamp()

    src = Path(args.source_file)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    for line in src.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        email = str(o.get("email") or "").strip().lower()
        if not email.endswith("@" + domain):
            continue
        exps = [
            float(c.get("expires"))
            for c in (o.get("cookies") or [])
            if isinstance(c, dict) and isinstance(c.get("expires"), (int, float)) and c.get("expires") > 0
        ]
        max_exp = max(exps) if exps else None
        created = (max_exp - TTL) if max_exp else None
        rows.append({"email": email, "obj": o, "created": created})

    rows.sort(key=lambda r: (r["created"] is None, r["created"] or 0, r["email"]))
    old = [r for r in rows if r["created"] is not None and r["created"] < cutoff_ts]
    new = [r for r in rows if r["created"] is not None and r["created"] >= cutoff_ts]
    unknown = [r for r in rows if r["created"] is None]

    def write_list(path: Path, items):
        with path.open("w", encoding="utf-8") as f:
            for r in items:
                f.write(json.dumps(r["obj"], ensure_ascii=False) + "\n")

    stamp = args.cutoff.replace("-", "")
    old_path = out_dir / f"auth-sessions-old-before-{stamp}.jsonl"
    new_path = out_dir / f"auth-sessions-new-from-{stamp}.jsonl"
    all_path = out_dir / "auth-sessions-by-age-asc.jsonl"
    write_list(all_path, rows)
    write_list(old_path, old)
    write_list(new_path, new)

    meta = {
        "domain": domain,
        "cutoff": cutoff.isoformat(),
        "total": len(rows),
        "old": len(old),
        "new": len(new),
        "unknown": len(unknown),
        "old_file": str(old_path),
        "new_file": str(new_path),
    }
    if old and old[0]["created"] and old[-1]["created"]:
        meta["old_range"] = [
            datetime.fromtimestamp(old[0]["created"], CST).isoformat(),
            datetime.fromtimestamp(old[-1]["created"], CST).isoformat(),
        ]
    if new and new[0]["created"] and new[-1]["created"]:
        meta["new_range"] = [
            datetime.fromtimestamp(new[0]["created"], CST).isoformat(),
            datetime.fromtimestamp(new[-1]["created"], CST).isoformat(),
        ]
    summary = out_dir / "auth-age-split-summary.json"
    summary.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
