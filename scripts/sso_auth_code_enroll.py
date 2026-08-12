#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SSO → Authorization Code OAuth (Grok Build) enroll + CPA upload.

This is the working path proven by upstream UI flow:

  SSO cookie
    → GET /oauth2/authorize?referrer=grok-build (PKCE)
    → POST consent action=allow + referrer=grok-build
    → exchange authorization_code
    → require JWT claim referrer=grok-build
    → save local auth JSON (base_url=cli-chat-proxy.grok.com/v1)
    → optional upload to CPA /v0/management/auth-files

NOT device-flow. Device-flow often lacks grok-build and/or hits invalid_grant.

Examples:
  .venv/bin/python scripts/sso_auth_code_enroll.py --source-file keys/auth-sessions.jsonl --index 0 --count 5
  .venv/bin/python scripts/sso_auth_code_enroll.py --source-file keys/auth-sessions.jsonl --count 20 --interval 3
  .venv/bin/python scripts/sso_auth_code_enroll.py --email ocxxx@mail.example.com --force-reauth
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from xai_enroller.auth_code import (  # noqa: E402
    ConvertError,
    convert_one,
    decode_jwt_payload,
    GROK_REFERRER,
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def allowed_email_domains() -> list[str]:
    raw = (
        env("XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN")
        or env("EMAIL_DOMAIN")
        or env("CFWORKER_DOMAINS")
        or env("CF_TEMP_MAIL_DOMAINS")
        or env("CFWORKER_DOMAIN")
        or ""
    ).strip()
    if not raw:
        return []
    try:
        import json as _json
        parsed = _json.loads(raw)
        if isinstance(parsed, list):
            items = [str(x) for x in parsed]
        else:
            items = [str(parsed)]
    except Exception:
        items = [x.strip() for x in raw.replace(";", ",").split(",")]
    out: list[str] = []
    for item in items:
        d = str(item or "").strip().lower().lstrip("@")
        if d and d not in out:
            out.append(d)
    return out


def allowed_email_domain() -> str:
    domains = allowed_email_domains()
    return domains[0] if domains else ""


def email_allowed(email: str, domain: str | None = None) -> bool:
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    host = email.rsplit("@", 1)[-1].strip().lower()
    if domain:
        allow = [x.strip().lower().lstrip("@") for x in str(domain).replace(";", ",").split(",") if x.strip()]
    else:
        allow = allowed_email_domains()
    if not allow:
        return True
    return host in allow

def normalize_email(value: str) -> str:
    return str(value or "").strip().lower()


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


def proxy_url() -> str:
    """Resolve outbound proxy for OAuth convert.

    upstream UI flow always converts via Clash. Without proxy, xAI often returns
    consent access_denied even for good SSO + clean domains.
    """
    for key in (
        "XAI_ENROLLER_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "https_proxy",
        "http_proxy",
        "all_proxy",
        "GROK_PROXY",
    ):
        v = env(key)
        if v:
            return v

    # optional explicit disable
    if env("XAI_ENROLLER_PROXY_DISABLE", "0").lower() in ("1", "true", "yes", "on"):
        return ""

    candidates: list[str] = []
    # common Clash ports on Windows host
    for port in ("7897", "7890", "10809", "1080"):
        candidates.append(f"http://127.0.0.1:{port}")

    # WSL: Windows host is nameserver in /etc/resolv.conf
    try:
        resolv = Path("/etc/resolv.conf")
        if resolv.exists():
            for line in resolv.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.strip().startswith("nameserver"):
                    host = line.split()[1].strip()
                    if host:
                        for port in ("7897", "7890", "10809"):
                            candidates.append(f"http://{host}:{port}")
                    break
    except Exception:
        pass

    # probe CONNECT-ish with short TCP connect
    import socket
    from urllib.parse import urlparse

    for url in candidates:
        try:
            u = urlparse(url)
            host = u.hostname or ""
            port = int(u.port or 0)
            if not host or not port:
                continue
            with socket.create_connection((host, port), timeout=0.35):
                return url
        except Exception:
            continue
    return ""


def load_sources(path: Path, domain: str) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    records: list[dict[str, str]] = []
    skipped = 0

    def add(email: str, sso: str) -> None:
        nonlocal skipped
        email = (email or "").strip()
        sso = (sso or "").strip()
        if not sso:
            return
        if email and "@" in email and not email_allowed(email, domain):
            skipped += 1
            return
        if not email:
            email = f"source#{len(records)}"
        records.append({"email": email, "sso": sso})

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
            add(email, sso)
    else:
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(":")
            if len(parts) >= 3 and len(parts[-1].strip()) > 20:
                add(parts[0].strip(), parts[-1].strip())

    if skipped:
        log(f"[filter] skipped {skipped} sources outside allowed domains")
    return records


DEFAULT_LEDGER = ROOT / "keys" / "cpa-enroll-ledger.jsonl"


def load_ledger(path: Path) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not path.exists():
        return latest
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
        if email and status in {"ok", "fail"}:
            latest[email] = obj
    return latest


def append_ledger(path: Path, *, email: str, status: str, stage: str = "", error: str = "", index: int | None = None) -> None:
    email_l = normalize_email(email)
    if not email_l or status not in {"ok", "fail"}:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "email": email_l,
        "status": status,
        "stage": stage,
        "error": str(error or "")[:500],
        "mode": "auth_code",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if index is not None:
        row["index"] = index
    try:
        from scripts.append_locked import locked_append

        locked_append(path, json.dumps(row, ensure_ascii=False))
    except Exception:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def collect_existing(auth_dir: Path, cpa: "CPAClient | None") -> set[str]:
    found: set[str] = set()

    def add(val: str) -> None:
        val = (val or "").strip().lower()
        if not val:
            return
        if val.endswith(".json"):
            val = val[:-5]
        if val.startswith("xai-") and "@" in val:
            val = val[4:]
        if "@" in val:
            found.add(val)

    if auth_dir.exists():
        for p in auth_dir.glob("*.json"):
            add(p.name)
            try:
                doc = json.loads(p.read_text(encoding="utf-8", errors="ignore"))
                add(str(doc.get("email") or ""))
            except Exception:
                pass
    if cpa is not None:
        try:
            for f in cpa.list_auth_files():
                for key in ("email", "account", "label", "name", "id"):
                    add(str(f.get(key) or ""))
        except Exception as exc:
            log(f"[skip] list CPA auth-files warn: {exc}")
    return found


class CPAClient:
    def __init__(self, base_url: str, secret: str, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.secret = secret
        # CPA 是本机/局域网服务，必须直连。env 里可能配置了 HTTP(S)_PROXY(Clash),
        # httpx 默认 trust_env=True 会读环境变量并把 192.168.x 私有地址也发往代理,
        # Clash 对私有地址返回 502。必须显式 trust_env=False 才能直连。
        self.client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            headers={
                "Authorization": f"Bearer {secret}",
                "X-Management-Key": secret,
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self.client.close()

    def list_auth_files(self) -> list[dict[str, Any]]:
        r = self.client.get(f"{self.base_url}/v0/management/auth-files")
        if r.status_code // 100 != 2:
            raise RuntimeError(f"list auth-files failed: {r.status_code} {r.text[:300]}")
        data = r.json()
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        if isinstance(data, dict):
            for key in ("files", "items", "data", "auth_files"):
                val = data.get(key)
                if isinstance(val, list):
                    return [x for x in val if isinstance(x, dict)]
        return []

    def upload_auth_file(self, name: str, document: dict[str, Any]) -> None:
        if not name.endswith(".json"):
            name += ".json"
        r = self.client.post(
            f"{self.base_url}/v0/management/auth-files?{urlencode({'name': name})}",
            headers={"Content-Type": "application/json"},
            json=document,
        )
        if r.status_code // 100 != 2:
            raise RuntimeError(f"upload {name} failed: {r.status_code} {r.text[:300]}")


def save_local(entry: dict[str, Any], auth_dir: Path) -> Path:
    auth_dir.mkdir(parents=True, exist_ok=True)
    email = str(entry.get("email") or "").strip().lower()
    if email and "@" in email:
        safe = "".join(ch if ch.isalnum() or ch in "._-+@" else "_" for ch in email)
        name = f"xai-{safe}.json"
    else:
        sub = str(entry.get("sub") or "unknown")
        name = f"xai-{sub[:16]}.json"
    path = auth_dir / name
    path.write_text(json.dumps(entry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # also append credentials ledger
    keys = ROOT / "keys"
    keys.mkdir(exist_ok=True)
    with (keys / "oauth_credentials.jsonl").open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "email": entry.get("email"),
                    "access_token": entry.get("access_token"),
                    "refresh_token": entry.get("refresh_token"),
                    "id_token": entry.get("id_token"),
                    "sub": entry.get("sub"),
                    "base_url": entry.get("base_url"),
                    "source": "sso_auth_code_enroll",
                    "file": name,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return path


def enroll_one(email: str, sso: str, *, proxy: str, auth_dir: Path, cpa: CPAClient | None, upload: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "email": email, "stage": "convert", "error": "", "path": "", "referrer": ""}
    try:
        entry = convert_one(sso, email=email, proxy=proxy)
    except ConvertError as exc:
        out["error"] = str(exc)
        # permanent=True（4xx invalid_grant）→ 永久失败；False（5xx/网络）→ 瞬时，下次可重试
        out["stage"] = "convert_failed" if exc.permanent else "convert_temporary"
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}:{exc}"
        out["stage"] = "convert_exception"
        return out

    claims = decode_jwt_payload(entry.get("access_token") or "")
    out["referrer"] = str(claims.get("referrer") or "")
    if out["referrer"] != GROK_REFERRER:
        out["error"] = f"missing referrer={GROK_REFERRER}"
        out["stage"] = "claim_failed"
        return out

    path = save_local(entry, auth_dir)
    out["path"] = str(path)
    out["stage"] = "saved_local"

    if upload and cpa is not None:
        out["stage"] = "cpa_upload"
        try:
            cpa.upload_auth_file(path.name, entry)
        except Exception as exc:
            out["error"] = f"cpa_upload:{exc}"
            out["stage"] = "cpa_upload_failed"
            # local file still saved; treat as partial fail
            return out

    out["ok"] = True
    out["stage"] = "done"
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="SSO → Auth Code (grok-build) enroll + CPA upload")
    p.add_argument("--source-file", default=str(ROOT / "keys" / "auth-sessions.jsonl"))
    p.add_argument("--email", default="", help="only process this one email")
    p.add_argument("--index", type=int, default=0)
    p.add_argument(
        "--count",
        type=int,
        default=1,
        help="how many accounts to process; 0 = all remaining from --index",
    )
    p.add_argument("--interval", type=float, default=3.0)
    p.add_argument("--auth-dir", default=str(ROOT / "auth-local" / "authenticated"))
    p.add_argument("--email-domain", default="")
    p.add_argument("--proxy", default="", help="override HTTPS proxy for convert")
    p.add_argument("--no-upload", action="store_true", default=True, help="only save local json (default)")
    p.add_argument("--upload-cpa", dest="no_upload", action="store_false", help="also upload to CPA after local save")
    p.add_argument("--allow-no-proxy", action="store_true", help="allow convert without proxy (not recommended)")
    p.add_argument("--skip-existing", dest="skip_existing", action="store_true", default=True)
    p.add_argument("--no-skip-existing", dest="skip_existing", action="store_false")
    p.add_argument("--force-reauth", action="store_true")
    p.add_argument("--skip-failed", dest="skip_failed", action="store_true", default=True)
    p.add_argument("--retry-failed", dest="skip_failed", action="store_false")
    p.add_argument("--no-skip-failed", dest="skip_failed", action="store_false")
    p.add_argument(
        "--retry-legacy-fails",
        dest="retry_legacy_fails",
        action="store_true",
        default=True,
        help="retry ledger fails from old device-flow (browser_*/invalid_grant/cpa_status); default on",
    )
    p.add_argument(
        "--no-retry-legacy-fails",
        dest="retry_legacy_fails",
        action="store_false",
        help="strictly skip all ledger fails",
    )
    p.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    p.add_argument("--no-ledger", action="store_true")
    p.add_argument("--max-consecutive-fail", type=int, default=0)
    p.add_argument("--json-out", default="")
    p.add_argument("--import-grok2api", action="store_true", help="import authorized json into local Grok2API")
    p.add_argument(
        "--quiet-skip",
        action="store_true",
        help="不逐条输出 SKIP 日志（仅保留计数），减少刷屏",
    )
    return p


def maybe_import_grok2api(paths: list[Path]) -> dict[str, Any]:
    """Import local auth files into Grok2API.

    Tries direct import first, falls back to subprocess.
    Mirrors the same function in cpa_xai_device_enroll.py.
    """
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
            "--files",
            *[str(p) for p in paths],
        ]
        proc = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True)
        return {
            "skipped": False,
            "returncode": proc.returncode,
            "stdout": (proc.stdout or "")[-1000:],
            "stderr": (proc.stderr or "")[-1000:],
        }
    # If importable main exists, call with argv
    argv = ["--files", *[str(p) for p in paths]]
    try:
        code = g2a_main(argv)
        return {"skipped": False, "returncode": code}
    except TypeError:
        code = g2a_main()
        return {"skipped": False, "returncode": code}
    except Exception as exc:
        return {"skipped": True, "reason": f"import_failed:{exc}"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # 启动前清理上次异常退出残留的浏览器进程
    try:
        from scripts.cleanup_browsers import cleanup_stale_browsers

        n = cleanup_stale_browsers()
        if n:
            log(f"[cleanup] {n} stale browser process(es) terminated")
    except Exception:
        pass
    if args.email_domain:
        os.environ["XAI_ENROLLER_ALLOWED_EMAIL_DOMAIN"] = args.email_domain.strip()
    domain = ",".join(allowed_email_domains())
    log(f"mode=auth_code referrer={GROK_REFERRER} domains={allowed_email_domains() or ['*']}")

    source_path = Path(args.source_file)
    records = load_sources(source_path, domain)
    if args.email:
        target = normalize_email(args.email)
        records = [r for r in records if normalize_email(r.get("email")) == target]
        if not records:
            raise SystemExit(f"email not found in source: {args.email}")
        args.index = 0
        args.count = 1
        args.force_reauth = True

    if not records:
        raise SystemExit(f"no SSO sources in {source_path}")
    if args.index < 0 or args.index >= len(records):
        raise SystemExit(f"index {args.index} out of range 0..{len(records)-1}")
    # count=0 means process all remaining from index (skip-existing still applies)
    if int(args.count) <= 0:
        args.count = max(1, len(records) - int(args.index))

    auth_dir = Path(args.auth_dir)
    auth_dir.mkdir(parents=True, exist_ok=True)
    proxy = (args.proxy or proxy_url()).strip()
    # Local-first: CPA upload is opt-in only (--upload-cpa).
    upload = not bool(getattr(args, "no_upload", True))
    force = bool(args.force_reauth)

    # Drop dead proxy (common: WSL -> Windows Clash without Allow LAN).
    if proxy:
        try:
            import socket
            from urllib.parse import urlparse
            u = urlparse(proxy)
            host = u.hostname or ""
            port = int(u.port or 0)
            if host and port:
                with socket.create_connection((host, port), timeout=0.5):
                    pass
            else:
                raise OSError("bad proxy url")
        except Exception:
            log(f"[!] proxy unreachable: {proxy}; fallback to direct")
            proxy = ""

    if not proxy:
        # upstream UI flow convert often works direct; do not hard-fail when Clash is closed.
        if not bool(getattr(args, "allow_no_proxy", False)):
            log(
                "[!] proxy=off (direct). If OAuth fails more, open Clash Allow LAN and set "
                "XAI_ENROLLER_PROXY=http://<windows-host-ip>:7897"
            )
    skip_existing = bool(args.skip_existing) and not force
    use_ledger = not bool(args.no_ledger)
    skip_failed = bool(args.skip_failed) and use_ledger and not force
    retry_legacy_fails = bool(getattr(args, 'retry_legacy_fails', True))
    ledger_path = Path(args.ledger) if use_ledger else None
    max_consecutive_fail = max(0, int(args.max_consecutive_fail or 0))

    cpa = None
    if upload:
        base, secret = require_cpa()
        cpa = CPAClient(base, secret)
        log(f"CPA base={base}")
    else:
        log("CPA upload disabled (local-only; pass --upload-cpa to enable)")

    import_grok2api = bool(getattr(args, "import_grok2api", False))
    imported_paths: list[Path] = []
    quiet_skip = bool(getattr(args, "quiet_skip", False))

    ok_n = fail_n = skip_n = 0
    results: list[dict[str, Any]] = []
    consecutive_fail = 0
    try:
        existing = collect_existing(auth_dir, cpa if skip_existing else None) if skip_existing else set()
        ledger_latest: dict[str, dict[str, Any]] = {}
        failed: set[str] = set()
        if use_ledger and ledger_path is not None:
            ledger_latest = load_ledger(ledger_path)
            failed = {e for e, r in ledger_latest.items() if r.get("status") == "fail"}
            if skip_existing:
                existing |= {e for e, r in ledger_latest.items() if r.get("status") == "ok"}
        if not proxy:
            log("[!] proxy=off — upstream UI flow 授权必须走 Clash。请开 Clash 或设 XAI_ENROLLER_PROXY=http://127.0.0.1:7897")
        else:
            log(f"[+] proxy={proxy}")
        log(
            f"source={source_path} total={len(records)} index={args.index} count={args.count} "
            f"skip_existing={skip_existing} already={len(existing)} "
            f"skip_failed={skip_failed} failed_ledger={len(failed)} proxy={'on' if proxy else 'off'}"
        )

        idx = args.index
        attempted = 0
        target = max(1, int(args.count))
        store = None
        if use_ledger and ledger_path is not None:
            from scripts.ledger_store import LedgerStore

            store = LedgerStore(ledger_path)
        while attempted < target and idx < len(records):
            rec = records[idx]
            email = rec["email"]
            email_l = normalize_email(email)
            # 台账状态用锁内原子认领（替代启动快照），消除并发下重复处理
            if store is not None:
                ledger_status = store.claim(email_l, skip_existing=skip_existing, skip_failed=skip_failed)
                if ledger_status == "ok":
                    skip_n += 1
                    if not quiet_skip:
                        log(f"[SKIP] index={idx} email={email} already ok (ledger)")
                    idx += 1
                    continue
                if ledger_status == "fail":
                    prev = store.last(email_l)
                    prev_err = str(prev.get("error") or prev.get("stage") or "fail")
                    legacy_markers = (
                        "browser_",
                        "invalid_grant",
                        "access denied",
                        "cpa_status",
                        "device_",
                        "oauth_rejected",
                        "cpa_failed",
                    )
                    is_legacy = any(m in prev_err.lower() for m in legacy_markers)
                    if retry_legacy_fails and is_legacy:
                        log(f"[RETRY-LEGACY] index={idx} email={email} previous={prev_err[:120]}")
                    else:
                        skip_n += 1
                        if not quiet_skip:
                            log(f"[SKIP] index={idx} email={email} previous fail ({prev_err[:120]})")
                        idx += 1
                        continue
                if ledger_status == "processing":
                    skip_n += 1
                    if not quiet_skip:
                        log(f"[SKIP] index={idx} email={email} processing in another instance")
                    idx += 1
                    continue
            if skip_existing and email_l in existing:
                skip_n += 1
                if not quiet_skip:
                    log(f"[SKIP] index={idx} email={email} already ok/local/CPA")
                idx += 1
                continue

            log(f"\n===== enroll index={idx} email={email} =====")
            result = enroll_one(
                email,
                rec["sso"],
                proxy=proxy,
                auth_dir=auth_dir,
                cpa=cpa,
                upload=upload,
            )
            results.append(result)
            attempted += 1
            if result.get("ok"):
                ok_n += 1
                consecutive_fail = 0
                existing.add(email_l)
                failed.discard(email_l)
                if store is not None:
                    store.append(email=email_l, status="ok", stage="done", index=idx)
                elif use_ledger and ledger_path is not None:
                    append_ledger(ledger_path, email=email_l, status="ok", stage="done", index=idx)
                path_str = str(result.get("path") or "")
                if import_grok2api and path_str:
                    imported_paths.append(Path(path_str))
                log(f"[OK] {email} referrer={result.get('referrer')} path={result.get('path')}")
            else:
                fail_n += 1
                consecutive_fail += 1
                stage = str(result.get("stage") or "")
                # convert_temporary（5xx/网络等瞬时错误）不写 fail 台账，
                # 避免被 skip_failed 永久跳过；下次运行可重试
                if stage == "convert_temporary":
                    log(f"[TEMP] {email} stage={stage} error={result.get('error')} (瞬时错误，不记失败)")
                else:
                    if store is not None:
                        store.append(
                            email=email_l,
                            status="fail",
                            stage=stage,
                            error=str(result.get("error") or ""),
                            index=idx,
                        )
                    elif use_ledger and ledger_path is not None:
                        append_ledger(
                            ledger_path,
                            email=email_l,
                            status="fail",
                            stage=stage,
                            error=str(result.get("error") or ""),
                            index=idx,
                        )
                    failed.add(email_l)
                log(f"[FAIL] {email} stage={stage} error={result.get('error')}")
                if max_consecutive_fail > 0 and consecutive_fail >= max_consecutive_fail:
                    log(f"[STOP] max consecutive fail {consecutive_fail}")
                    break
            idx += 1
            if attempted < target and idx < len(records):
                time.sleep(max(0.0, float(args.interval)))
    finally:
        if cpa is not None:
            cpa.close()

    if import_grok2api:
        g2a_result: dict[str, Any] = {"skipped": True, "reason": "no_files"}
        if not imported_paths:
            # 没有新授权账号时，扫描已有文件
            for p in sorted(auth_dir.glob("xai-*.json")):
                imported_paths.append(p)
        if imported_paths:
            log(f"importing {len(imported_paths)} auth file(s) into Grok2API ...")
            g2a_result = maybe_import_grok2api(imported_paths)
        if g2a_result.get("skipped"):
            log(f"[grok2api] skipped: {g2a_result.get('reason')}")
        elif g2a_result.get("returncode", -1) == 0:
            log("[grok2api] import OK")
        else:
            log(f"[grok2api] import failed: rc={g2a_result.get('returncode')}")
            stderr = g2a_result.get("stderr", "") or ""
            stdout = g2a_result.get("stdout", "") or ""
            if stderr:
                log(f"[grok2api] stderr: {stderr[-500:]}")
            if stdout:
                log(f"[grok2api] stdout: {stdout[-500:]}")

    summary = {"ok": ok_n, "fail": fail_n, "skip": skip_n, "results": results, "mode": "auth_code"}
    print(json.dumps({"ok": ok_n, "fail": fail_n, "skip": skip_n, "mode": "auth_code"}, ensure_ascii=False), flush=True)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if ok_n > 0 else 1


if __name__ == "__main__":
    # 单实例锁：与 授权.py wrapper 共用 .lock-auth，跨入口互斥
    try:
        from scripts.single_instance import acquire_single_instance

        if not acquire_single_instance(ROOT / "keys" / ".lock-auth", "授权"):
            raise SystemExit(3)
    except SystemExit:
        raise
    except Exception as exc:
        log(f"[!] 单实例锁不可用（{exc}），继续运行")
    raise SystemExit(main())
