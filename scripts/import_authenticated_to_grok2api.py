#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Import local authenticated xAI JSON files into Grok2API admin API.

Reads auth JSON files from auth-local/authenticated/ (xai-*.json),
reformats them for Grok2API's /api/admin/v1/accounts/web/import endpoint,
and POSTs them as multipart form data.

The Web import endpoint expects an SSO token per account (stored in
the "sso" field of our auth JSON). Grok2API handles the OAuth token
exchange internally.

Usage:
  .venv/bin/python scripts/import_authenticated_to_grok2api.py
  .venv/bin/python scripts/import_authenticated_to_grok2api.py --auth-dir auth-local/authenticated
  .venv/bin/python scripts/import_authenticated_to_grok2api.py --email user@example.com
  .venv/bin/python scripts/import_authenticated_to_grok2api.py --limit 5

Env:
  GROK2API_ADMIN_BASE   default http://127.0.0.1:8000/api/admin/v1
  GROK2API_ADMIN_USER   default admin
  GROK2API_ADMIN_PASS   (required)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Grok2API admin auth
# ---------------------------------------------------------------------------

ADMIN_BASE = env("GROK2API_ADMIN_BASE", "http://127.0.0.1:8000/api/admin/v1").rstrip("/")
ADMIN_USER = env("GROK2API_ADMIN_USER", "admin")
ADMIN_PASS = env("GROK2API_ADMIN_PASS", "")


def admin_login(base: str = ADMIN_BASE, user: str = ADMIN_USER, passwd: str = ADMIN_PASS) -> str:
    """POST /auth/login → returns accessToken."""
    if not passwd:
        raise SystemExit("GROK2API_ADMIN_PASS not set")
    r = httpx.post(
        f"{base}/auth/login",
        json={"username": user, "password": passwd},
        timeout=30,
        trust_env=False,  # 本机/局域网 admin 服务必须直连；env 的 HTTP(S)_PROXY 会经 Clash 导致 502
    )
    if r.status_code // 100 != 2:
        raise RuntimeError(f"admin login failed: {r.status_code} {r.text[:300]}")
    data = r.json()
    # Grok2API wraps payloads as {"data": {...}}
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    tokens = payload.get("tokens") if isinstance(payload.get("tokens"), dict) else {}
    token = (
        tokens.get("accessToken")
        or payload.get("accessToken")
        or data.get("accessToken")
        or payload.get("token")
        or ""
    )
    if not token:
        raise RuntimeError(f"admin login response missing accessToken: {list(data.keys())}")
    return token


# ---------------------------------------------------------------------------
# Read local auth files
# ---------------------------------------------------------------------------

AUTH_FIELDS = {
    # field in our JSON → field we send for Grok2API
    "sso": "sso_token",
    "email": "email",
    "sub": "user_id",
}


def load_auth_documents(
    auth_dir: Path,
    *,
    email_filter: str = "",
    limit: int = 0,
    files: list[Path] | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Read xai-*.json files from auth_dir.

    If `files` is given, only those exact files are considered (in the given
    order); otherwise all xai-*.json in auth_dir (sorted) are used.  The
    `limit` applies to the final document count.

    Returns list of (filename, parsed_doc).
    """
    if files:
        paths = list(files)
    else:
        if not auth_dir.is_dir():
            raise SystemExit(f"auth-dir not found: {auth_dir}")
        paths = sorted(auth_dir.glob("xai-*.json"))

    docs: list[tuple[str, dict[str, Any]]] = []
    for p in paths:
        try:
            doc = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
        except json.JSONDecodeError as exc:
            log(f"[skip] invalid JSON {p.name}: {exc}")
            continue
        if not isinstance(doc, dict):
            continue
        email = str(doc.get("email") or "").strip().lower()
        if email_filter and email_filter.lower() not in email:
            continue
        sso = str(doc.get("sso") or doc.get("sso_token") or "").strip()
        if not sso:
            log(f"[skip] {p.name}: missing sso field")
            continue
        docs.append((p.name, doc))
        if limit > 0 and len(docs) >= limit:
            break

    return docs


def build_import_payload(docs: list[tuple[str, dict[str, Any]]], tier: str = "auto") -> bytes:
    """Build a Grok2API-compatible JSON document for web import.

    Format expected by Grok2API web/import.go:
      {
        "provider": "grok_web",
        "accounts": [
          {
            "name": "...",
            "email": "...",
            "user_id": "...",
            "sso_token": "...",
            "tier": "auto"
          }
        ]
      }
    """
    accounts: list[dict[str, Any]] = []
    for fname, doc in docs:
        email = str(doc.get("email") or "").strip()
        sso = str(doc.get("sso") or doc.get("sso_token") or "").strip()
        sub = str(doc.get("sub") or "").strip()
        name = email or f"xai-{sub[:16]}" if sub else fname.replace(".json", "")
        accounts.append({
            "name": name,
            "email": email,
            "user_id": sub,
            "sso_token": sso,
            "tier": tier,
        })

    payload = {"provider": "grok_web", "accounts": accounts}
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


# ---------------------------------------------------------------------------
# POST import (multipart form → SSE stream response)
# ---------------------------------------------------------------------------


def post_import(token: str, payload_bytes: bytes, base: str = ADMIN_BASE) -> dict[str, Any]:
    """POST multipart form with a single file to /accounts/web/import.

    The endpoint returns SSE events. We buffer them and return the final
    "complete" event payload.

    Returns dict with keys: created, updated, skipped, synced, syncFailed
    """
    url = f"{base}/accounts/web/import"
    boundary = "----grok2api-import-boundary-146b9e"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="accounts.json"\r\n'
        f"Content-Type: application/json\r\n\r\n"
    ).encode("utf-8") + payload_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "text/event-stream",
    }

    with httpx.Client(timeout=300, follow_redirects=False, trust_env=False) as client:
        r = client.post(url, headers=headers, content=body)
        if r.status_code // 100 != 2:
            raise RuntimeError(f"import failed: {r.status_code} {r.text[:500]}")

        # Parse SSE events from response text
        result: dict[str, Any] = {}
        for raw in r.text.split("\n\n"):
            raw = raw.strip()
            if not raw:
                continue
            event_type = ""
            data_str = ""
            for line in raw.split("\n"):
                line = line.strip()
                if line.startswith("event: "):
                    event_type = line[len("event: "):].strip()
                elif line.startswith("data: "):
                    data_str = line[len("data: "):].strip()
            if event_type == "complete" and data_str:
                try:
                    result = json.loads(data_str)
                except json.JSONDecodeError:
                    log(f"[warn] unparseable SSE complete data: {data_str[:200]}")
            elif event_type == "error" and data_str:
                try:
                    err_data = json.loads(data_str)
                    err_msg = err_data.get("message") or err_data.get("code") or data_str
                except json.JSONDecodeError:
                    err_msg = data_str
                log(f"[error] import SSE error: {err_msg}")

    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Import authenticated xAI JSON files into Grok2API")
    p.add_argument(
        "--auth-dir",
        default=str(ROOT / "auth-local" / "authenticated"),
        help="directory with xai-*.json auth files",
    )
    p.add_argument(
        "--files",
        nargs="*",
        default=[],
        help="exact xai-*.json paths to import (overrides directory scan)",
    )
    p.add_argument("--email", default="", help="only import files matching this email substring")
    p.add_argument("--limit", type=int, default=0, help="max accounts to import (0=all)")
    p.add_argument(
        "--tier",
        default="auto",
        choices=("auto", "basic", "super", "heavy"),
        help="Grok2API Web account tier (default: auto)",
    )
    p.add_argument("--dry-run", action="store_true", help="print what would be imported without sending")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    auth_dir = Path(args.auth_dir)
    files = [Path(f) for f in args.files] if args.files else None
    docs = load_auth_documents(auth_dir, email_filter=args.email, limit=args.limit, files=files)
    if not docs:
        log(f"no auth files found (with sso token) in {auth_dir}")
        return 0

    payload_bytes = build_import_payload(docs, tier=args.tier)
    parsed = json.loads(payload_bytes.decode("utf-8"))
    n = len(parsed["accounts"])
    log(f"found {n} account(s) to import")

    if args.dry_run:
        log("--- dry-run: would send these accounts ---")
        for acct in parsed["accounts"]:
            sso_preview = (acct["sso_token"][:40] + "...") if len(acct["sso_token"]) > 40 else acct["sso_token"]
            log(f"  email={acct['email']} user_id={acct['user_id']} sso={sso_preview}")
        log(f"payload size={len(payload_bytes)} bytes")
        return 0

    token = admin_login()
    log(f"admin login OK (token {token[:16]}...)")

    result = post_import(token, payload_bytes)
    created = result.get("created", 0)
    updated = result.get("updated", 0)
    skipped = result.get("skipped", 0)
    synced = result.get("synced", 0)
    sync_failed = result.get("syncFailed", 0)
    log(
        f"import complete: created={created} updated={updated} skipped={skipped} "
        f"synced={synced} syncFailed={sync_failed}"
    )
    return 0 if (created + updated) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
