#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mark already-imported auth-service accounts as not-imported so old path can retry.

Default ledger (auth-service local sink):
  ~/path/to/claimed/enrollment-ledger.db

Examples:
  .venv/bin/python scripts/auth_service_force_requeue.py ocxxxx@mail.example.com
  .venv/bin/python scripts/auth_service_force_requeue.py --all-imported
  .venv/bin/python scripts/auth_service_force_requeue.py --list 20
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DIR = Path.home() / "Downloads" / "grok-free-register-auth"


def load_salt(auth_dir: Path) -> bytes:
    salt_file = auth_dir / ".ledger-salt"
    if not salt_file.exists():
        raise SystemExit(f"missing salt file: {salt_file}")
    salt = salt_file.read_text(encoding="utf-8").strip()
    if not salt:
        raise SystemExit(f"empty salt file: {salt_file}")
    return salt.encode("utf-8")


def fingerprint(salt: bytes, source_id: str) -> str:
    return hmac.new(salt, source_id.encode("utf-8"), hashlib.sha256).hexdigest()


def connect(db: Path) -> sqlite3.Connection:
    if not db.exists():
        raise SystemExit(f"ledger not found: {db}")
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def list_imported(conn: sqlite3.Connection, limit: int = 20) -> None:
    rows = conn.execute(
        """
        SELECT source_fingerprint, COUNT(*) AS n, MAX(finished_at) AS last_at
        FROM jobs
        WHERE status='imported'
        GROUP BY source_fingerprint
        ORDER BY last_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    total = conn.execute(
        "SELECT COUNT(DISTINCT source_fingerprint) AS c FROM jobs WHERE status='imported'"
    ).fetchone()["c"]
    print(f"imported_unique={total} showing={len(rows)}")
    for row in rows:
        print(f"{row['last_at']}  n={row['n']}  fp={row['source_fingerprint'][:16]}...")


def clear_imported(conn: sqlite3.Connection, fps: list[str]) -> int:
    if not fps:
        return 0
    now = datetime.now(timezone.utc).isoformat()
    changed = 0
    with conn:
        for fp in fps:
            cur = conn.execute(
                """
                UPDATE jobs
                SET status='cancelled',
                    finished_at=?,
                    reason_code='force_reauth_requeue',
                    sink_receipt_fingerprint=NULL
                WHERE source_fingerprint=? AND status='imported'
                """,
                (now, fp),
            )
            changed += cur.rowcount
    return changed


def main() -> int:
    p = argparse.ArgumentParser(description="Re-queue imported accounts for old auth-service")
    p.add_argument("emails", nargs="*", help="emails / source_id to re-queue")
    p.add_argument(
        "--auth-dir",
        default=str(DEFAULT_DIR),
        help="auth-service local dir (default: ~/path/to/claimed)",
    )
    p.add_argument("--all-imported", action="store_true", help="re-queue ALL imported fingerprints")
    p.add_argument("--list", type=int, default=0, metavar="N", help="list N recent imported fps")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    auth_dir = Path(args.auth_dir).expanduser()
    db = auth_dir / "enrollment-ledger.db"
    salt = load_salt(auth_dir)
    conn = connect(db)

    if args.list:
        list_imported(conn, args.list)
        return 0

    fps: list[str] = []
    if args.all_imported:
        rows = conn.execute(
            "SELECT DISTINCT source_fingerprint FROM jobs WHERE status='imported'"
        ).fetchall()
        fps = [r["source_fingerprint"] for r in rows]
        print(f"selected all imported: {len(fps)}")
    else:
        if not args.emails:
            raise SystemExit("provide emails, or --all-imported, or --list N")
        for email in args.emails:
            email = email.strip().lower()
            fp = fingerprint(salt, email)
            has = conn.execute(
                "SELECT 1 FROM jobs WHERE source_fingerprint=? AND status='imported' LIMIT 1",
                (fp,),
            ).fetchone()
            print(f"{email} fp={fp[:16]}... imported={'yes' if has else 'no'}")
            if has:
                fps.append(fp)

    fps = list(dict.fromkeys(fps))
    if not fps:
        print("nothing to re-queue")
        return 1

    if args.dry_run:
        print(f"dry-run: would clear imported rows for {len(fps)} fingerprint(s)")
        return 0

    changed = clear_imported(conn, fps)
    print(f"cleared_imported_rows={changed} fingerprints={len(fps)}")
    print("Next: stop auth-service if running, then:")
    print("  bash auth-service.sh --debug")
    print("Note: old path still does local token poll; may hit invalid_grant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
