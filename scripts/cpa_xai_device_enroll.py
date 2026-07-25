#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CPA-orchestrated xAI Device Flow enrollment.

Target pipeline (community / CPA management API):

  1) Register machine already did: captcha + password/register CreateSession -> SSO
  2) CPA  GET  /v0/management/xai-auth-url
        -> {url, state, user_code, flow=device, expires_in}
        CPA keeps device_code and polls token in background
  3) Local browser: inject SSO cookies, open verification url, Allow
  4) CPA  GET  /v0/management/get-auth-status?state=...
        -> wait | ok | error
  5) On ok: CPA already saved auth-files
        Local: list/download new xai-*.json -> auth-local/authenticated (SUB/CPA compatible)
  6) Optional: import downloaded JSON into local Grok2API admin API

This is the correct split of responsibilities:
  - CPA owns device_code + token exchange + auth-file persistence
  - Local owns SSO session + browser approve of user_code
  - Local may dual-write SUB json and Grok2API

Env:
  XAI_ENROLLER_CPA_BASE_URL          e.g. https://your-cpa.example
  XAI_ENROLLER_CPA_MANAGEMENT_SECRET management key
  HTTP_PROXY / HTTPS_PROXY           optional, e.g. http://127.0.0.1:7897
  GROK2API_ADMIN_BASE                optional, default http://127.0.0.1:8000/api/admin/v1
  GROK2API_ADMIN_USER / PASS         optional for auto-import

Example:
  python scripts/cpa_xai_device_enroll.py --source-file keys/auth-sessions.jsonl --index 0 --headed
  python scripts/cpa_xai_device_enroll.py --source-file keys/auth-sessions.jsonl --count 3 --import-grok2api
  # failed emails remembered in keys/cpa-enroll-ledger.jsonl and skipped next run
  # retry failed: add --retry-failed  (or --force-reauth for one account)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from xai_enroller.executors import PlaywrightExecutor
from xai_enroller.models import DeviceFlow, SourceRecord


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()



def allowed_email_domain() -> str:
    """Only enroll these addresses. Default: mail.example.com (never bare apex)."""
    return (
        env("XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN")
        or env("EMAIL_DOMAIN")
        or "mail.example.com"
    ).strip().lower().lstrip("@")


def email_allowed(email: str, domain: str | None = None) -> bool:
    email = (email or "").strip().lower()
    domain = (domain or allowed_email_domain()).strip().lower().lstrip("@")
    if not email or "@" not in email or not domain:
        return False
    host = email.rsplit("@", 1)[-1]
    # exact subdomain match only (oc@mail.example.com OK; oc@example.com blocked)
    return host == domain

def require_cpa() -> tuple[str, str]:
    base = env("XAI_ENROLLER_CPA_BASE_URL") or env("CPA_BASE_URL") or env("CPA_BASE")
    secret = (
        env("XAI_ENROLLER_CPA_MANAGEMENT_SECRET")
        or env("CPA_MANAGEMENT_SECRET")
        or env("CPA_KEY")
        or env("MANAGEMENT_KEY")
    )
    if not base or not secret:
        raise SystemExit(
            "缺少 CPA 配置。请在 .env 设置:\n"
            "  XAI_ENROLLER_CPA_BASE_URL=https://你的CPA域名\n"
            "  XAI_ENROLLER_CPA_MANAGEMENT_SECRET=你的management_key"
        )
    return base.rstrip("/"), secret


def auth_headers(secret: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {secret}",
        "X-Management-Key": secret,
        "Accept": "application/json",
    }


def proxy_for_httpx() -> str | None:
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY"):
        value = env(key)
        if value:
            return value
    return None


def load_source_records(path: Path, *, domain: str | None = None) -> list[SourceRecord]:
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    domain = (domain or allowed_email_domain()).strip().lower().lstrip("@")
    records: list[SourceRecord] = []
    skipped_domain = 0

    def maybe_add(email: str, sso: str, cookies) -> None:
        nonlocal skipped_domain
        email = (email or "").strip()
        sso = (sso or "").strip()
        if not sso:
            return
        if not email:
            email = f"source#{len(records)}"
        if "@" in email and not email_allowed(email, domain):
            skipped_domain += 1
            return
        cookie_tuple = tuple(c for c in cookies if isinstance(c, dict)) if isinstance(cookies, list) else ()
        records.append(SourceRecord(source_id=email, sso_token=sso, cookies=cookie_tuple))

    # jsonl first
    if path.suffix.lower() in {".jsonl", ".json"} or text.lstrip().startswith("{"):
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            email = str(obj.get("email") or obj.get("id") or obj.get("source_id") or "").strip()
            sso = str(obj.get("sso") or obj.get("sso_token") or obj.get("token") or "").strip()
            cookies = obj.get("cookies") or []
            if not sso and isinstance(cookies, list):
                for c in cookies:
                    if isinstance(c, dict) and str(c.get("name") or "") in {"sso", "sso-rw"} and c.get("value"):
                        sso = str(c["value"])
                        break
            maybe_add(email, sso, cookies if isinstance(cookies, list) else [])
        if records or skipped_domain:
            if skipped_domain:
                log(f"[filter] skipped {skipped_domain} sources not @{domain}")
            return records

    # accounts.txt: email:password:sso
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 3:
            continue
        email = parts[0].strip()
        sso = parts[-1].strip()
        if email and sso and len(sso) > 20:
            maybe_add(email, sso, [])
    if skipped_domain:
        log(f"[filter] skipped {skipped_domain} sources not @{domain}")
    return records


def collect_existing_emails(cpa=None, auth_dir: Path | None = None) -> set[str]:
    """Emails already present in CPA auth-files and/or local authenticated json."""
    found: set[str] = set()

    def add_email(val: str) -> None:
        val = (val or "").strip().lower()
        if not val:
            return
        if val.endswith(".json"):
            val = val[:-5]
        if val.startswith("xai-") and "@" in val:
            val = val[4:]
        if "@" in val:
            found.add(val)

    if cpa is not None:
        try:
            for f in cpa.list_auth_files():
                for key in ("email", "account", "label", "name", "id"):
                    add_email(str(f.get(key) or ""))
        except Exception as exc:
            log(f"[skip] list CPA auth-files warn: {exc}")

    root = Path(auth_dir) if auth_dir else (ROOT / "auth-local" / "authenticated")
    try:
        if root.exists():
            for p in root.glob("*.json"):
                add_email(p.name)
                try:
                    doc = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                    add_email(str(doc.get("email") or ""))
                except Exception:
                    pass
    except Exception as exc:
        log(f"[skip] local auth dir warn: {exc}")

    return found



DEFAULT_LEDGER_PATH = ROOT / "keys" / "cpa-enroll-ledger.jsonl"


def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


def load_enroll_ledger(path: Path) -> dict[str, dict[str, Any]]:
    """Load last status per email from append-only jsonl ledger."""
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
    try:
        for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            email = normalize_email(obj.get("email") or "")
            status = str(obj.get("status") or "").strip().lower()
            if not email or status not in {"ok", "fail"}:
                continue
            latest[email] = obj
    except Exception as exc:
        log(f"[ledger] load warn: {exc}")
    return latest


def ledger_failed_emails(latest: dict[str, dict[str, Any]]) -> set[str]:
    return {
        email
        for email, row in latest.items()
        if str(row.get("status") or "").strip().lower() == "fail"
    }


def ledger_ok_emails(latest: dict[str, dict[str, Any]]) -> set[str]:
    return {
        email
        for email, row in latest.items()
        if str(row.get("status") or "").strip().lower() == "ok"
    }


def append_enroll_ledger(
    path: Path,
    *,
    email: str,
    status: str,
    stage: str = "",
    error: str = "",
    source_file: str = "",
    index: int | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    email_l = normalize_email(email)
    status_l = str(status or "").strip().lower()
    if not email_l or status_l not in {"ok", "fail"}:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {
        "email": email_l,
        "status": status_l,
        "stage": str(stage or ""),
        "error": str(error or "")[:500],
        "source_file": str(source_file or ""),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if index is not None:
        row["index"] = int(index)
    if extra:
        for k, v in extra.items():
            if k not in row:
                row[k] = v
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


class CPAClient:
    def __init__(self, base_url: str, secret: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        proxy = proxy_for_httpx()
        self.client = httpx.Client(
            timeout=timeout,
            proxy=proxy,
            follow_redirects=False,
            headers=auth_headers(secret),
        )

    def close(self) -> None:
        self.client.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def start_xai_device_flow(self) -> dict[str, Any]:
        # CPA: GET /v0/management/xai-auth-url
        # returns {status,url,state,flow,user_code,expires_in}
        r = self.client.get(self._url("/v0/management/xai-auth-url"))
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        if r.status_code // 100 != 2:
            raise RuntimeError(f"xai-auth-url failed status={r.status_code} body={data}")
        if not data.get("state") or not (data.get("url") or data.get("user_code")):
            raise RuntimeError(f"xai-auth-url missing state/url/user_code: {data}")
        return data

    def get_auth_status(self, state: str) -> dict[str, Any]:
        r = self.client.get(
            self._url("/v0/management/get-auth-status"),
            params={"state": state},
        )
        try:
            return r.json()
        except Exception:
            return {"status": "error", "error": f"bad json status={r.status_code} body={r.text[:300]}"}

    def poll_auth_status(self, state: str, timeout_sec: float = 180.0, interval: float = 2.0) -> dict[str, Any]:
        deadline = time.time() + timeout_sec
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.get_auth_status(state)
            status = str(last.get("status") or "").lower()
            if status == "ok":
                return last
            if status == "error":
                return last
            # wait / pending / empty
            time.sleep(interval)
        last = dict(last or {})
        last.setdefault("status", "error")
        last.setdefault("error", "poll_timeout")
        return last

    def list_auth_files(self) -> list[dict[str, Any]]:
        r = self.client.get(self._url("/v0/management/auth-files"))
        if r.status_code // 100 != 2:
            raise RuntimeError(f"list auth-files failed: {r.status_code} {r.text[:300]}")
        data = r.json()
        files = data.get("files") if isinstance(data, dict) else None
        return list(files or [])

    def download_auth_file(self, name: str) -> bytes:
        if not name.endswith(".json"):
            name = f"{name}.json"
        r = self.client.get(
            self._url("/v0/management/auth-files/download"),
            params={"name": name},
        )
        if r.status_code // 100 != 2:
            raise RuntimeError(f"download {name} failed: {r.status_code} {r.text[:300]}")
        return r.content

    def upload_auth_file(self, name: str, document: dict[str, Any]) -> None:
        if not name.endswith(".json"):
            name = f"{name}.json"
        r = self.client.post(
            self._url(f"/v0/management/auth-files?{urlencode({'name': name})}"),
            headers={**auth_headers(self.secret), "Content-Type": "application/json"},
            content=json.dumps(document, ensure_ascii=False).encode("utf-8"),
        )
        if r.status_code // 100 != 2:
            raise RuntimeError(f"upload {name} failed: {r.status_code} {r.text[:300]}")


def file_names(files: list[dict[str, Any]]) -> set[str]:
    out: set[str] = set()
    for f in files:
        name = str(f.get("name") or f.get("id") or "").strip()
        if name:
            out.add(name if name.endswith(".json") else f"{name}.json")
    return out


def normalize_auth_name(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        return ""
    return name if name.endswith(".json") else f"{name}.json"


def auth_file_stamp(entry: dict[str, Any]) -> str:
    """Best-effort change detector for in-place CPA overwrites."""
    for key in ("updated_at", "modtime", "created_at", "size", "auth_index", "status_message"):
        if entry.get(key) not in (None, ""):
            return f"{key}={entry.get(key)}"
    # fallback stable-ish dump without huge fields
    return json.dumps(
        {k: entry.get(k) for k in ("name", "id", "email", "disabled", "unavailable") if k in entry},
        ensure_ascii=False,
        sort_keys=True,
    )


def index_auth_files(files: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """name -> entry"""
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        name = normalize_auth_name(f.get("name") or f.get("id") or "")
        if name:
            out[name] = f
    return out


def pick_auth_names_for_source(
    *,
    source_email: str,
    before_files: list[dict[str, Any]],
    after_files: list[dict[str, Any]],
) -> list[str]:
    """Prefer email match / updated same-name files / brand-new names.

    Re-running the same account usually overwrites the same CPA auth filename,
    so a pure set-diff of names would miss the success.
    """
    email_l = (source_email or "").strip().lower()
    before_idx = index_auth_files(before_files)
    after_idx = index_auth_files(after_files)

    new_names = sorted(set(after_idx) - set(before_idx))
    updated_names = []
    for name, after_entry in after_idx.items():
        if name not in before_idx:
            continue
        if auth_file_stamp(after_entry) != auth_file_stamp(before_idx[name]):
            updated_names.append(name)

    email_names = []
    for name, entry in after_idx.items():
        entry_email = str(entry.get("email") or entry.get("account") or entry.get("label") or "").strip().lower()
        provider = str(entry.get("provider") or entry.get("type") or "").strip().lower()
        if email_l and email_l in entry_email:
            email_names.append(name)
            continue
        # filename sometimes embeds email
        if email_l and email_l in name.lower():
            email_names.append(name)
            continue
        if email_l and email_l.replace("@", "_") in name.lower():
            email_names.append(name)

    ranked: list[tuple[int, str]] = []
    seen: set[str] = set()

    def add(name: str, score: int) -> None:
        name = normalize_auth_name(name)
        if not name or name in seen:
            return
        seen.add(name)
        ranked.append((score, name))

    for name in email_names:
        score = 100
        if name in new_names:
            score += 20
        if name in updated_names:
            score += 15
        if name.startswith("xai-") or "xai" in name.lower():
            score += 5
        add(name, score)

    for name in new_names:
        score = 50
        meta = after_idx.get(name) or {}
        if str(meta.get("provider") or meta.get("type") or "").lower() == "xai":
            score += 10
        if name.startswith("xai-"):
            score += 5
        add(name, score)

    for name in updated_names:
        score = 40
        meta = after_idx.get(name) or {}
        if str(meta.get("provider") or meta.get("type") or "").lower() == "xai":
            score += 10
        add(name, score)

    ranked.sort(reverse=True)
    names = [n for _, n in ranked]
    # If we already have exact/near email matches, never pull unrelated new/updated files.
    if email_names:
        email_set = {normalize_auth_name(x) for x in email_names}
        only_email = [n for n in names if n in email_set]
        if only_email:
            return only_email[:1]
    return names[:1]


def normalize_verification_url(url: str | None, user_code: str | None) -> str:
    url = (url or "").strip()
    user_code = (user_code or "").strip()
    if url:
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname and (
            parsed.hostname == "accounts.x.ai" or parsed.hostname.endswith(".x.ai")
        ):
            return url
    if not user_code:
        raise ValueError("missing verification url/user_code")
    return "https://accounts.x.ai/oauth2/device?" + urlencode({"user_code": user_code})


async def browser_approve(
    source: SourceRecord,
    *,
    verification_url: str,
    user_code: str,
    headed: bool,
    timeout_sec: float,
) -> dict[str, Any]:
    # Reuse project Playwright executor (SSO cookie inject + Allow).
    # Device_code is owned by CPA; local only needs user_code/url for UI.
    flow = DeviceFlow(
        device_code="cpa-owned",
        user_code=user_code or "UNKNOWN",
        verification_url=verification_url,
        expires_in=int(timeout_sec),
        interval=5.0,
        token_endpoint="https://auth.x.ai/oauth2/token",
    )
    executor = PlaywrightExecutor(concurrency=1)
    # headed mode via env used by some cloak setups; keep API stable
    if headed:
        os.environ.setdefault("XAI_ENROLLER_HEADED", "1")
    try:
        await executor.start()
        result = await asyncio.wait_for(executor.confirm(source, flow), timeout=timeout_sec)
        return {
            "ok": result.status.value == "authorized",
            "status": result.status.value,
            "reason": result.reason_code,
        }
    finally:
        try:
            await executor.close()
        except Exception:
            pass


def save_local_auth_json(content: bytes, auth_dir: Path, preferred_name: str | None = None) -> Path:
    auth_dir.mkdir(parents=True, exist_ok=True)
    name = preferred_name or "xai-unknown.json"
    if not name.endswith(".json"):
        name += ".json"
    # Prefer email-based name when document contains email
    try:
        doc = json.loads(content.decode("utf-8"))
        email = str(doc.get("email") or "").strip().lower()
        if email and "@" in email:
            safe = "".join(ch if ch.isalnum() or ch in "._-+@" else "_" for ch in email)
            name = f"xai-{safe}.json"
    except Exception:
        doc = None
    path = auth_dir / name
    path.write_bytes(content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8"))
    # also append ledger line
    keys = ROOT / "keys"
    keys.mkdir(exist_ok=True)
    if isinstance(doc, dict):
        line = {
            "email": doc.get("email"),
            "access_token": doc.get("access_token"),
            "refresh_token": doc.get("refresh_token"),
            "id_token": doc.get("id_token"),
            "sub": doc.get("sub"),
            "source": "cpa_xai_device_enroll",
            "file": name,
        }
        with (keys / "oauth_credentials.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
        if doc.get("refresh_token") and doc.get("email"):
            with (keys / "refresh_tokens.txt").open("a", encoding="utf-8") as f:
                f.write(f"{doc.get('email')}\t{doc.get('refresh_token')}\n")
    return path


def maybe_import_grok2api(paths: list[Path]) -> dict[str, Any]:
    if not paths:
        return {"skipped": True, "reason": "no_files"}
    try:
        from scripts.import_authenticated_to_grok2api import main as g2a_main  # type: ignore
    except Exception:
        # fallback: spawn module if import path differs
        g2a_script = ROOT / "scripts" / "import_authenticated_to_grok2api.py"
        if not g2a_script.exists():
            return {"skipped": True, "reason": "import_script_missing"}
        import subprocess

        cmd = [
            sys.executable,
            str(g2a_script),
            "--auth-dir",
            str(paths[0].parent),
            "--limit",
            str(len(paths)),
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        return {
            "skipped": False,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-1000:],
            "stderr": (proc.stderr or "")[-1000:],
        }
    # If importable main exists, call with argv
    argv = ["--auth-dir", str(paths[0].parent), "--limit", str(len(paths))]
    try:
        code = g2a_main(argv)
        return {"skipped": False, "returncode": code}
    except TypeError:
        code = g2a_main()
        return {"skipped": False, "returncode": code}
    except Exception as exc:
        return {"skipped": True, "reason": f"import_failed:{exc}"}


async def enroll_one(
    cpa: CPAClient,
    source: SourceRecord,
    *,
    headed: bool,
    browser_timeout: float,
    poll_timeout: float,
    auth_dir: Path,
    import_g2a: bool,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "ok": False,
        "email": source.source_id,
        "stage": "start",
        "state": "",
        "user_code": "",
        "url": "",
        "browser": None,
        "cpa_status": None,
        "downloaded": [],
        "error": "",
    }
    before_files = cpa.list_auth_files()
    out["stage"] = "cpa_xai_auth_url"
    started = cpa.start_xai_device_flow()
    state = str(started.get("state") or "")
    user_code = str(started.get("user_code") or "")
    url = normalize_verification_url(started.get("url"), user_code)
    out["state"] = state
    out["user_code"] = user_code
    out["url"] = url
    log(f"[CPA] device started email={source.source_id} user_code={user_code} state={state[:24]}...")
    log(f"[CPA] verification_url={url}")

    out["stage"] = "browser_approve"
    browser = await browser_approve(
        source,
        verification_url=url,
        user_code=user_code,
        headed=headed,
        timeout_sec=browser_timeout,
    )
    out["browser"] = browser
    if not browser.get("ok"):
        out["error"] = f"browser_{browser.get('reason') or browser.get('status') or 'failed'}"
        out["stage"] = "browser_failed"
        # still poll briefly so CPA session can surface token error
        status = cpa.poll_auth_status(state, timeout_sec=min(30.0, poll_timeout), interval=2.0)
        out["cpa_status"] = status
        if str(status.get("status")).lower() == "error":
            out["error"] = f"{out['error']};cpa:{status.get('error')}"
        return out

    out["stage"] = "cpa_poll_status"
    log("[CPA] browser authorized; waiting CPA token save...")
    status = cpa.poll_auth_status(state, timeout_sec=poll_timeout, interval=2.0)
    out["cpa_status"] = status
    if str(status.get("status")).lower() != "ok":
        err = status.get("error") or status.get("status") or "unknown"
        out["error"] = f"cpa_status:{err}"
        out["stage"] = "cpa_failed"
        return out

    out["stage"] = "download"
    # allow CPA a moment to index/overwrite the auth file
    time.sleep(1.5)
    after_files = cpa.list_auth_files()
    chosen = pick_auth_names_for_source(
        source_email=source.source_id,
        before_files=before_files,
        after_files=after_files,
    )
    out["candidate_files"] = chosen[:5]
    if not chosen:
        # last resort: any xai file whose email matches, even if stamp identical
        email_l = source.source_id.lower()
        for f in after_files:
            name = normalize_auth_name(f.get("name") or f.get("id") or "")
            em = str(f.get("email") or f.get("account") or "").strip().lower()
            if name and email_l and (email_l == em or email_l in name.lower()):
                chosen.append(name)
        chosen = list(dict.fromkeys(chosen))
        out["candidate_files"] = chosen[:5]

    saved: list[str] = []
    for name in chosen[:1]:
        try:
            content = cpa.download_auth_file(name)
            path = save_local_auth_json(content, auth_dir, preferred_name=name)
            saved.append(str(path))
            log(f"[LOCAL] saved {path} (from CPA {name})")
        except Exception as exc:
            log(f"[LOCAL] download warn {name}: {exc}")
    out["downloaded"] = saved
    if not saved:
        out["error"] = "cpa_ok_but_download_empty"
        out["stage"] = "download_failed"
        log("[CPA] status=ok but could not resolve/download auth-file; open CPA Auth Files UI")
        return out
    out["ok"] = True
    out["stage"] = "done"
    if import_g2a and saved:
        out["stage"] = "import_grok2api"
        out["grok2api"] = maybe_import_grok2api([Path(p) for p in saved])
        out["stage"] = "done"
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="CPA-orchestrated xAI device-flow enroll (SSO approve + CPA token save)")
    p.add_argument(
        "--source-file",
        default=str(ROOT / "keys" / "auth-sessions.jsonl"),
        help="auth-sessions.jsonl / source-snapshot.jsonl / accounts.txt",
    )
    p.add_argument("--index", type=int, default=0, help="start source index")
    p.add_argument("--count", type=int, default=1, help="how many sources to process")
    p.add_argument("--headed", action="store_true", help="show browser window")
    p.add_argument("--browser-timeout", type=float, default=120.0)
    p.add_argument("--poll-timeout", type=float, default=180.0)
    p.add_argument(
        "--auth-dir",
        default=str(ROOT / "auth-local" / "authenticated"),
        help="local SUB/CPA-compatible json output dir",
    )
    p.add_argument("--import-grok2api", action="store_true", help="import downloaded json into local Grok2API")
    p.add_argument("--json-out", default="", help="write last result json path")
    p.add_argument("--interval", type=float, default=3.0, help="pause between accounts")
    p.add_argument(
        "--email-domain",
        default="",
        help="only enroll this domain (default: EMAIL_DOMAIN or mail.example.com)",
    )
    p.add_argument(
        "--skip-existing",
        dest="skip_existing",
        action="store_true",
        default=True,
        help="skip emails already in CPA/local auth (default: on)",
    )
    p.add_argument(
        "--no-skip-existing",
        dest="skip_existing",
        action="store_false",
        help="allow re-auth of same email already in CPA/local",
    )
    p.add_argument(
        "--force-reauth",
        action="store_true",
        help="same as --no-skip-existing",
    )
    p.add_argument(
        "--stop-on-invalid-grant",
        action="store_true",
        help="stop whole batch on first invalid_grant/Access denied (old behavior)",
    )
    p.add_argument(
        "--max-consecutive-fail",
        type=int,
        default=0,
        help="stop after N consecutive failures (0=never; useful guard for large --count)",
    )
    p.add_argument(
        "--ledger",
        default=str(DEFAULT_LEDGER_PATH),
        help="append-only enroll ledger jsonl (ok/fail); default keys/cpa-enroll-ledger.jsonl",
    )
    p.add_argument(
        "--skip-failed",
        dest="skip_failed",
        action="store_true",
        default=True,
        help="skip emails whose last ledger status is fail (default: on)",
    )
    p.add_argument(
        "--no-skip-failed",
        dest="skip_failed",
        action="store_false",
        help="retry emails previously marked fail in ledger",
    )
    p.add_argument(
        "--retry-failed",
        dest="skip_failed",
        action="store_false",
        help="alias of --no-skip-failed",
    )
    p.add_argument(
        "--no-ledger",
        action="store_true",
        help="do not read/write enroll ledger",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    # Default path is proven Auth Code + PKCE (referrer=grok-build).
    # Keep --mode device for the old CPA device-flow browser path.
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--mode", choices=("auth_code", "device"), default="auth_code")
    known, rest = pre.parse_known_args(argv)
    if known.mode == "auth_code":
        import importlib.util

        mod_path = ROOT / "scripts" / "sso_auth_code_enroll.py"
        spec = importlib.util.spec_from_file_location("sso_auth_code_enroll", mod_path)
        if spec is None or spec.loader is None:
            raise SystemExit(f"cannot load {mod_path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print("[*] mode=auth_code via sso_auth_code_enroll (Grok Build)", flush=True)
        return mod.main(rest)

    args = build_parser().parse_args(rest)
    base, secret = require_cpa()
    source_path = Path(args.source_file)
    if args.email_domain:
        os.environ["XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN"] = args.email_domain.strip()
    domain = allowed_email_domain()
    log(f"allowed email domain=@{domain}")
    records = load_source_records(source_path, domain=domain)
    if not records:
        raise SystemExit(f"no SSO sources in {source_path}")
    if args.index < 0 or args.index >= len(records):
        raise SystemExit(f"index {args.index} out of range 0..{len(records)-1}")

    auth_dir = Path(args.auth_dir)
    auth_dir.mkdir(parents=True, exist_ok=True)
    cpa = CPAClient(base, secret)
    ok_n = fail_n = skip_n = 0
    results: list[dict[str, Any]] = []
    force_reauth = bool(args.force_reauth)
    skip_existing = bool(args.skip_existing) and (not force_reauth)
    use_ledger = not bool(args.no_ledger)
    skip_failed = bool(args.skip_failed) and use_ledger and (not force_reauth)
    stop_on_invalid_grant = bool(args.stop_on_invalid_grant)
    max_consecutive_fail = max(0, int(args.max_consecutive_fail or 0))
    consecutive_fail = 0
    ledger_path = Path(args.ledger) if use_ledger else None
    try:
        existing = collect_existing_emails(cpa, auth_dir) if skip_existing else set()
        ledger_latest: dict[str, dict[str, Any]] = {}
        failed = set()
        ledger_ok = set()
        if use_ledger and ledger_path is not None:
            ledger_latest = load_enroll_ledger(ledger_path)
            failed = ledger_failed_emails(ledger_latest)
            ledger_ok = ledger_ok_emails(ledger_latest)
            # successes already recorded in ledger also count as existing
            if skip_existing:
                existing |= ledger_ok
        log(f"CPA base={base}")
        log(
            f"source={source_path} total={len(records)} index={args.index} count={args.count} "
            f"skip_existing={skip_existing} already={len(existing)} "
            f"skip_failed={skip_failed} failed_ledger={len(failed)} ledger={ledger_path or 'off'} "
            f"stop_on_invalid_grant={stop_on_invalid_grant} max_consecutive_fail={max_consecutive_fail}"
        )
        # Walk forward until we attempt `count` non-skipped sources (or list ends)
        idx = args.index
        attempted = 0
        target = max(1, args.count)
        while attempted < target and idx < len(records):
            source = records[idx]
            email_l = normalize_email(source.source_id)
            if skip_existing and email_l in existing:
                skip_n += 1
                log(f"[SKIP] index={idx} email={source.source_id} already in CPA/local/ledger-ok")
                idx += 1
                continue
            if skip_failed and email_l in failed:
                skip_n += 1
                prev = ledger_latest.get(email_l) or {}
                prev_err = str(prev.get("error") or prev.get("stage") or "fail")[:120]
                log(f"[SKIP] index={idx} email={source.source_id} previous fail in ledger ({prev_err})")
                idx += 1
                continue

            log(f"\n===== enroll index={idx} email={source.source_id} =====")
            try:
                result = asyncio.run(
                    enroll_one(
                        cpa,
                        source,
                        headed=bool(args.headed),
                        browser_timeout=float(args.browser_timeout),
                        poll_timeout=float(args.poll_timeout),
                        auth_dir=auth_dir,
                        import_g2a=bool(args.import_grok2api),
                    )
                )
            except Exception as exc:
                result = {
                    "ok": False,
                    "email": source.source_id,
                    "error": f"{type(exc).__name__}:{exc}",
                    "stage": "exception",
                }
            results.append(result)
            attempted += 1
            if result.get("ok"):
                ok_n += 1
                consecutive_fail = 0
                if email_l:
                    existing.add(email_l)
                    failed.discard(email_l)
                if use_ledger and ledger_path is not None and email_l:
                    append_enroll_ledger(
                        ledger_path,
                        email=email_l,
                        status="ok",
                        stage=str(result.get("stage") or "done"),
                        error="",
                        source_file=str(source_path),
                        index=idx,
                        extra={"user_code": result.get("user_code") or ""},
                    )
                    ledger_latest[email_l] = {"email": email_l, "status": "ok"}
                log(f"[OK] {source.source_id} downloaded={result.get('downloaded')}")
            else:
                fail_n += 1
                consecutive_fail += 1
                log(f"[FAIL] {source.source_id} stage={result.get('stage')} error={result.get('error')}")
                if use_ledger and ledger_path is not None and email_l:
                    append_enroll_ledger(
                        ledger_path,
                        email=email_l,
                        status="fail",
                        stage=str(result.get("stage") or ""),
                        error=str(result.get("error") or ""),
                        source_file=str(source_path),
                        index=idx,
                        extra={"user_code": result.get("user_code") or ""},
                    )
                    ledger_latest[email_l] = {
                        "email": email_l,
                        "status": "fail",
                        "stage": result.get("stage"),
                        "error": result.get("error"),
                    }
                    failed.add(email_l)
                err = str(result.get("error") or "")
                is_invalid_grant = ("invalid_grant" in err) or ("Access denied" in err)
                if is_invalid_grant:
                    if stop_on_invalid_grant:
                        log("[STOP] token still rejected by xAI (invalid_grant). Don't burn more accounts.")
                        break
                    log("[CONTINUE] invalid_grant on this account; recorded in ledger and try next")
                if max_consecutive_fail > 0 and consecutive_fail >= max_consecutive_fail:
                    log(
                        f"[STOP] reached max consecutive failures "
                        f"({consecutive_fail}>={max_consecutive_fail})"
                    )
                    break
            idx += 1
            if attempted < target and idx < len(records):
                time.sleep(max(0.0, float(args.interval)))
    finally:
        cpa.close()

    summary = {"ok": ok_n, "fail": fail_n, "skip": skip_n, "results": results}
    print(json.dumps({"ok": ok_n, "fail": fail_n, "skip": skip_n}, ensure_ascii=False), flush=True)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if ok_n > 0 and fail_n == 0 else (0 if ok_n > 0 else 1)


if __name__ == "__main__":
    raise SystemExit(main())





