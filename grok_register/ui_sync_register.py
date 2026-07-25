"""Windows Sync Camoufox UI registration (UI-compatible).

Default on Windows: run each account in a FRESH subprocess so:
  - no shared AsyncCamoufox contention
  - no Sync API / asyncio loop pollution
  - hung Camoufox can be killed by process timeout


"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import tempfile
import sys
import time
from pathlib import Path
from typing import Callable, Optional

RefreshCodeFn = Callable[[], Optional[str]]


def _env(name: str, default: str = "") -> str:
    return str(os.environ.get(name, default) or "").strip()


def _log_fn(log):
    def _log(msg: str) -> None:
        if not log:
            return
        try:
            log(msg)
        except Exception:
            pass

    return _log


def _strong_password(seed: str = "") -> str:
    base = re.sub(r"[^A-Za-z0-9]", "", seed or "")[:8] or "Grok"
    return f"{base[:1].upper()}{base[1:].lower() or 'rok'}{random.randint(1000, 9999)}!a"


def _proxy_server() -> Optional[str]:
    if _env("UI_SYNC_NO_PROXY", "0").lower() in ("1", "true", "yes", "on"):
        return None
    for key in (
        "BROWSER_PROXY",
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "https_proxy",
        "http_proxy",
    ):
        val = _env(key)
        if val and val.lower() not in ("0", "false", "no", "off", "none", "direct"):
            return val
    if _env("UI_SYNC_AUTO_CLASH_PROXY", "0").lower() in ("1", "true", "yes", "on"):
        clash = _env("CLASH_MIXED_PORT", "7897")
        if clash.isdigit():
            try:
                import socket

                with socket.create_connection(("127.0.0.1", int(clash)), timeout=0.3):
                    return f"http://127.0.0.1:{clash}"
            except Exception:
                return None
    return None


def _read_turnstile_token(page) -> str:
    try:
        token = page.evaluate(
            """() => {
  try {
    const byInput = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
    if (byInput) return byInput;
    const ta = document.querySelector('textarea[name="cf-turnstile-response"]');
    if (ta && ta.value) return String(ta.value || '').trim();
    if (window.turnstile && typeof turnstile.getResponse === 'function') {
      return String(turnstile.getResponse() || '').trim();
    }
    return '';
  } catch (e) { return ''; }
}"""
        )
        return str(token or "").strip()
    except Exception:
        return ""


def _click_turnstile(page, log=None) -> dict:
    """Native-only Turnstile interaction (browser-native). No helper inject."""
    _log = _log_fn(log)
    detail = {
        "clicked": False,
        "frames": 0,
        "token_len": 0,
        "method": "",
        "error": "",
    }
    token = _read_turnstile_token(page)
    detail["token_len"] = len(token)
    if detail["token_len"] >= 80:
        detail["method"] = "already-solved"
        return detail

    try:
        host = page.query_selector(
            "iframe[src*='challenges.cloudflare.com'], iframe[src*='turnstile'], iframe[src*='cdn-cgi'], div.cf-turnstile, [data-sitekey]"
        )
        if host:
            try:
                host.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
            box = host.bounding_box()
            if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
                points = [
                    (box["x"] + 28, box["y"] + box["height"] * 0.5),
                    (box["x"] + 22, box["y"] + box["height"] * 0.48),
                    (box["x"] + min(36.0, box["width"] * 0.15), box["y"] + box["height"] * 0.52),
                ]
                for x, y in points:
                    try:
                        page.mouse.move(float(x), float(y), steps=8)
                        time.sleep(0.06)
                        page.mouse.click(float(x), float(y), delay=40)
                        detail["clicked"] = True
                        detail["method"] = "host-mouse-coords"
                        for _ in range(10):
                            time.sleep(0.45)
                            token = _read_turnstile_token(page)
                            detail["token_len"] = len(token)
                            if detail["token_len"] >= 80:
                                detail["method"] = "host-mouse-coords+solved"
                                return detail
                    except Exception:
                        continue
    except Exception as exc:
        detail["error"] = f"host:{exc}"

    # frame left-checkbox coords
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    detail["frames"] = len(frames)
    for frame in frames:
        try:
            url = (frame.url or "").lower()
        except Exception:
            continue
        if not any(x in url for x in ("turnstile", "challenges.cloudflare", "cdn-cgi", "cf-chl")):
            continue
        try:
            el = frame.frame_element()
            box = el.bounding_box()
            if not box or box.get("width", 0) < 20:
                continue
            x = box["x"] + 28
            y = box["y"] + box["height"] * 0.5
            page.mouse.click(float(x), float(y), delay=40)
            detail["clicked"] = True
            detail["method"] = "frame-mouse-coords"
            for _ in range(10):
                time.sleep(0.45)
                token = _read_turnstile_token(page)
                detail["token_len"] = len(token)
                if detail["token_len"] >= 80:
                    detail["method"] = "frame-mouse-coords+solved"
                    return detail
            try:
                frame.click("body", position={"x": 28, "y": max(10, int(box["height"] / 2))}, timeout=1500)
                detail["method"] = "frame-body-pos"
            except Exception:
                pass
        except Exception:
            continue

    token = _read_turnstile_token(page)
    detail["token_len"] = len(token)
    if detail["token_len"] >= 80:
        detail["method"] = detail["method"] or "solved"
    return detail


def _wait_turnstile(page, log=None, timeout_s: float = 90.0, label: str = "") -> bool:
    _log = _log_fn(log)
    deadline = time.time() + max(5.0, timeout_s)
    nudged = False
    while time.time() < deadline:
        token = _read_turnstile_token(page)
        if len(token) >= 80:
            _log(f"[C-UI-SYNC] turnstile ok label={label or '-'} len={len(token)}")
            return True
        # passive first ~6s then click
        if not nudged and (timeout_s - (deadline - time.time())) >= 6.0:
            detail = _click_turnstile(page, log=log)
            nudged = True
            _log(
                f"[C-UI-SYNC] turnstile click label={label or '-'} "
                f"method={detail.get('method') or '-'} len={detail.get('token_len') or 0}"
            )
            if (detail.get("token_len") or 0) >= 80:
                return True
        elif nudged:
            _click_turnstile(page, log=log)
        time.sleep(0.7)
    token = _read_turnstile_token(page)
    ok = len(token) >= 80
    _log(f"[C-UI-SYNC] turnstile end label={label or '-'} ok={int(ok)} len={len(token)}")
    return ok


def _set_input(page, selector: str, value: str) -> bool:
    try:
        loc = page.locator(selector).first
        if loc.count() == 0:
            return False
        loc.wait_for(state="visible", timeout=8000)
        loc.click(timeout=2000)
        loc.fill("")
        loc.type(value, delay=25)
        return True
    except Exception:
        try:
            return bool(
                page.evaluate(
                    """({sel, val}) => {
  const el = document.querySelector(sel);
  if (!el) return false;
  el.focus();
  el.value = val;
  el.dispatchEvent(new Event('input', {bubbles:true}));
  el.dispatchEvent(new Event('change', {bubbles:true}));
  return true;
}""",
                    {"sel": selector.split(",")[0].strip(), "val": value},
                )
            )
        except Exception:
            return False


def _click_by_texts(page, texts: list[str]) -> str:
    lowered = [t.lower() for t in texts]
    # role=button first
    try:
        buttons = page.locator("button, [role='button'], input[type='submit']")
        n = min(buttons.count(), 20)
        for i in range(n):
            b = buttons.nth(i)
            try:
                txt = (b.inner_text(timeout=500) or b.get_attribute("value") or "").strip().lower()
            except Exception:
                continue
            if any(t in txt for t in lowered):
                b.click(timeout=3000)
                return txt[:40]
    except Exception:
        pass
    # text selectors
    for t in texts:
        try:
            page.get_by_text(t, exact=False).first.click(timeout=1500)
            return t
        except Exception:
            continue
    return ""


def _page_has(page, kind: str) -> bool:
    kind = (kind or "").lower()
    try:
        if kind == "email":
            return page.locator('input[type="email"], input[name="email"], input[autocomplete="email"]').count() > 0
        if kind == "code":
            return page.locator(
                'input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]'
            ).count() > 0
        if kind == "profile":
            return page.locator(
                'input[name="password"], input[type="password"], input[name="givenName"], input[autocomplete="given-name"]'
            ).count() > 0
    except Exception:
        return False
    return False


def _click_email_signup(page) -> bool:
    labels = [
        "Sign up with email",
        "sign up with email",
        "Continue with email",
        "使用邮箱",
        "邮箱注册",
    ]
    clicked = _click_by_texts(page, labels)
    if clicked:
        return True
    try:
        page.locator("button:has-text('email'), a:has-text('email')").first.click(timeout=2000)
        return True
    except Exception:
        return _page_has(page, "email")


def _get_sso(page) -> str:
    try:
        cookies = page.context.cookies()
    except Exception:
        try:
            cookies = page.context.cookies(["https://grok.com", "https://accounts.x.ai", "https://x.ai"])
        except Exception:
            cookies = []
    best = ""
    for c in cookies or []:
        name = str(c.get("name") or "")
        val = str(c.get("value") or "")
        if name in ("sso", "sso-rw") and len(val) > len(best):
            best = val
    return best


def _make_page(browser, log=None):
    """Create page with direct page order + persistent-context reuse."""
    _log = _log_fn(log)
    # BrowserContext (persistent) may already have pages
    pages = []
    try:
        pages = list(getattr(browser, "pages", []) or [])
    except Exception:
        pages = []
    if pages:
        _log("[C-UI-SYNC] reuse existing context page")
        return pages[0]

    last_exc = None
    # direct page order
    try:
        page = browser.new_page(no_viewport=True)
        _log("[C-UI-SYNC] page via new_page(no_viewport=True)")
        return page
    except TypeError as exc:
        last_exc = exc
    except Exception as exc:
        last_exc = exc
        _log(f"[C-UI-SYNC] new_page(no_viewport) err={type(exc).__name__}")

    try:
        ctx = browser.new_context(no_viewport=True)
        page = ctx.new_page()
        _log("[C-UI-SYNC] page via new_context(no_viewport=True)")
        return page
    except Exception as exc:
        last_exc = exc
        _log(f"[C-UI-SYNC] new_context err={type(exc).__name__}")

    try:
        page = browser.new_page()
        _log("[C-UI-SYNC] page via bare new_page")
        return page
    except Exception as exc:
        last_exc = exc
        raise RuntimeError(f"Camoufox new page failed: {last_exc}") from exc


def _register_inprocess(
    *,
    email: str,
    password: str,
    code: str = "",
    given_name: str = "",
    family_name: str = "",
    site_url: str = "https://accounts.x.ai",
    refresh_code: RefreshCodeFn | None = None,
    log=None,
) -> Optional[str]:
    _log = _log_fn(log)
    given_name = given_name or random.choice(
        ["James", "John", "Robert", "Michael", "William", "David", "Richard", "Joseph", "Thomas", "Charles"]
    )
    family_name = family_name or random.choice(
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez"]
    )
    active_code = re.sub(r"[\s-]+", "", code or "")
    if (
        not re.search(r"[A-Z]", password or "")
        or not re.search(r"[a-z]", password or "")
        or not re.search(r"\d", password or "")
    ):
        password = _strong_password(password or (email.split("@")[0] if email else "Grok"))
        _log("[C-UI-SYNC] strengthened password for form requirements")

    try:
        import asyncio

        asyncio.set_event_loop(None)
    except Exception:
        pass

    try:
        from camoufox.sync_api import Camoufox
    except Exception as exc:
        _log(f"[C-UI-SYNC] camoufox sync import failed: {type(exc).__name__}")
        return None

    headless_raw = _env("BROWSER_HEADLESS", "1").lower()
    headless = headless_raw not in ("0", "false", "no", "off", "headed")
    proxy = _proxy_server()

    launch_kwargs = {
        "headless": headless,
        "humanize": True,
        "geoip": False,
        "locale": "en-US",
    }
    if sys.platform.startswith("win"):
        launch_kwargs["os"] = "windows"
    if proxy:
        launch_kwargs["proxy"] = {"server": proxy}
        launch_kwargs["i_know_what_im_doing"] = True

    # persistent needs user_data_dir; default OFF (upstream UI flow uses normal Browser).
    use_persistent = _env("UI_SYNC_PERSISTENT", "0").lower() in ("1", "true", "yes", "on")
    user_data_dir = ""
    if use_persistent:
        user_data_dir = _env("UI_SYNC_USER_DATA_DIR") or tempfile.mkdtemp(prefix="gfr-camoufox-")
        launch_kwargs["user_data_dir"] = user_data_dir
        launch_kwargs["persistent_context"] = True

    _log(
        f"[C-UI-SYNC] launch camoufox headless={headless} proxy={proxy or 'off'} "
        f"persistent={int(use_persistent)} email={email}"
    )

    try:
        kwargs = dict(launch_kwargs)
        with Camoufox(**kwargs) as browser:
            _log("[C-UI-SYNC] creating page")
            page = _make_page(browser, log=log)
            _log("[C-UI-SYNC] page ready")

            _log("[C-UI-SYNC] open sign-up")
            page.goto(
                f"{site_url.rstrip('/')}/sign-up?redirect=grok-com",
                timeout=45000,
                wait_until="domcontentloaded",
            )
            time.sleep(1.2)

            if not _click_email_signup(page):
                if not _page_has(page, "email"):
                    _log("[C-UI-SYNC] email signup button not found")
                    return None

            _log("[C-UI-SYNC] fill email")
            if not _set_input(
                page,
                'input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]',
                email,
            ):
                _log("[C-UI-SYNC] email input missing")
                return None
            clicked = _click_by_texts(
                page, ["sign up", "signup", "continue", "继续", "next", "下一步", "提交", "注册"]
            )
            if not clicked:
                try:
                    page.evaluate(
                        """() => {
  const form = document.querySelector('form');
  if (form && typeof form.requestSubmit === 'function') { form.requestSubmit(); return 'requestSubmit'; }
  return '';
}"""
                    )
                except Exception:
                    pass
            _log(f"[C-UI-SYNC] email submit={clicked or 'requestSubmit?'}")
            time.sleep(1.5)

            # Poll code if needed
            if refresh_code is not None and not active_code:
                _log("[C-UI-SYNC] polling mailbox for verification code")
                deadline = time.time() + 120
                attempts = 0
                while time.time() < deadline and not active_code:
                    attempts += 1
                    try:
                        new_code = refresh_code()
                    except Exception as exc:
                        _log(f"[C-UI-SYNC] refresh_code error={type(exc).__name__}")
                        new_code = None
                    new_code = re.sub(r"[\s-]+", "", str(new_code or ""))
                    if new_code:
                        active_code = new_code
                        _log(f"[C-UI-SYNC] polled code len={len(active_code)} attempts={attempts}")
                        break
                    if attempts % 4 == 0 and _page_has(page, "email"):
                        _wait_turnstile(page, log=log, timeout_s=12.0, label="email")
                        _set_input(
                            page,
                            'input[type="email"], input[name="email"], input[data-testid="email"], input[autocomplete="email"]',
                            email,
                        )
                        _click_by_texts(
                            page,
                            ["sign up", "signup", "continue", "继续", "next", "下一步", "提交", "注册"],
                        )
                    time.sleep(1.5)

            deadline = time.time() + 45
            while time.time() < deadline:
                if _page_has(page, "code") or _page_has(page, "profile"):
                    break
                time.sleep(0.5)

            if _page_has(page, "code"):
                if not active_code:
                    _log("[C-UI-SYNC] code page ready but code empty")
                    return None
                _log("[C-UI-SYNC] fill code")
                filled = False
                for _ in range(8):
                    if _set_input(
                        page,
                        'input[name="code"], input[data-testid="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"]',
                        active_code,
                    ):
                        filled = True
                        break
                    time.sleep(0.6)
                if not filled:
                    _log("[C-UI-SYNC] code input missing")
                    return None
                _wait_turnstile(page, log=log, timeout_s=15.0, label="code")
                clicked = _click_by_texts(
                    page, ["verify", "continue", "继续", "next", "确认", "验证", "submit", "确认邮箱"]
                )
                _log(f"[C-UI-SYNC] code submit={clicked or '-'}")
                time.sleep(1.5)

            deadline = time.time() + 60
            while time.time() < deadline:
                if _page_has(page, "profile"):
                    break
                time.sleep(0.7)
            if not _page_has(page, "profile"):
                _log("[C-UI-SYNC] profile form not ready")
                return None

            _log("[C-UI-SYNC] fill profile")
            ok1 = _set_input(
                page,
                'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]',
                given_name,
            )
            ok2 = _set_input(
                page,
                'input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]',
                family_name,
            )
            ok3 = _set_input(
                page,
                'input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]',
                password,
            )
            if not (ok1 and ok2 and ok3):
                _log(f"[C-UI-SYNC] profile fields incomplete given={ok1} family={ok2} pass={ok3}")
                return None

            if not _wait_turnstile(page, log=log, timeout_s=100.0, label="profile"):
                _log("[C-UI-SYNC] profile blocked by unsolved turnstile")
                return None

            submitted = ""
            try:
                submitted = page.evaluate(
                    """() => {
  const form = document.querySelector('form');
  if (form && typeof form.requestSubmit === 'function') {
    form.requestSubmit();
    return 'requestSubmit';
  }
  return '';
}"""
                )
            except Exception:
                submitted = ""
            clicked = submitted or _click_by_texts(
                page,
                [
                    "完成注册",
                    "创建账户",
                    "sign up",
                    "signup",
                    "create account",
                    "continue",
                    "继续",
                    "submit",
                    "complete",
                ],
            )
            _log(f"[C-UI-SYNC] profile submit={clicked or '-'}")

            deadline = time.time() + 70
            while time.time() < deadline:
                sso = _get_sso(page)
                if sso and len(sso) >= 20:
                    _log(f"[C-UI-SYNC] got sso len={len(sso)}")
                    return sso
                token = _read_turnstile_token(page)
                if len(token) < 80 and _page_has(page, "profile"):
                    _click_turnstile(page, log=log)
                time.sleep(1.0)
            _log("[C-UI-SYNC] sso timeout")
            return None
    except Exception as exc:
        _log(f"[C-UI-SYNC] failed: {type(exc).__name__}: {exc}")
        return None
    finally:
        # temp profile for persistent_context
        if user_data_dir and _env("UI_SYNC_KEEP_PROFILE", "0").lower() not in ("1", "true", "yes", "on"):
            try:
                import shutil
                shutil.rmtree(user_data_dir, ignore_errors=True)
            except Exception:
                pass


def _use_subprocess() -> bool:
    raw = _env("UI_SYNC_SUBPROCESS")
    if raw:
        return raw.lower() in ("1", "true", "yes", "on")
    # Windows default: subprocess isolation (killable on hang)
    return sys.platform.startswith("win")


def register_via_ui_sync(
    *,
    email: str,
    password: str,
    code: str = "",
    handle: str = "",
    given_name: str = "",
    family_name: str = "",
    site_url: str = "https://accounts.x.ai",
    refresh_code: RefreshCodeFn | None = None,
    log=None,
) -> Optional[str]:
    """Drive signup with Sync Camoufox. Return sso cookie or None."""
    _log = _log_fn(log)

    active_code = re.sub(r"[\s-]+", "", code or "")
    handle = (handle or "").strip()

    if not _use_subprocess():
        # Prefer explicit handle poll if provided.
        rc = refresh_code
        if handle and rc is None:
            def rc():
                try:
                    from grok_register.register import poll_code
                    return poll_code(handle, max_wait=8)
                except Exception:
                    return None
        return _register_inprocess(
            email=email,
            password=password,
            code=active_code or code or "",
            given_name=given_name,
            family_name=family_name,
            site_url=site_url,
            refresh_code=rc,
            log=log,
        )

    timeout_s = float(_env("UI_SYNC_SUBPROCESS_TIMEOUT", "240") or "240")
    py = sys.executable
    env = os.environ.copy()
    env["UI_SYNC_SUBPROCESS"] = "0"  # child runs in-process
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Prefer TUN/direct unless user explicitly wants clash mixed port
    if "UI_SYNC_AUTO_CLASH_PROXY" not in env:
        env["UI_SYNC_AUTO_CLASH_PROXY"] = "0"

    cmd = [
        py,
        "-u",
        "-m",
        "grok_register.ui_sync_register",
        "--email",
        email,
        "--password",
        password,
        "--code",
        active_code or code or "",
        "--handle",
        handle or "",
        "--site-url",
        site_url or "https://accounts.x.ai",
    ]
    if given_name:
        cmd.extend(["--given-name", given_name])
    if family_name:
        cmd.extend(["--family-name", family_name])

    _log(f"[C-UI-SYNC] subprocess start timeout={timeout_s:.0f}s email={email}")
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(Path(__file__).resolve().parents[1]),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:
        _log(f"[C-UI-SYNC] subprocess spawn failed: {type(exc).__name__}")
        return None

    out_lines: list[str] = []
    sso = None
    try:
        assert proc.stdout is not None
        deadline = time.time() + timeout_s
        while True:
            if proc.poll() is not None:
                rest = proc.stdout.read() or ""
                for line in rest.splitlines():
                    out_lines.append(line)
                    _log(line)
                break
            if time.time() > deadline:
                _log("[C-UI-SYNC] subprocess TIMEOUT; killing")
                try:
                    proc.kill()
                except Exception:
                    pass
                try:
                    proc.wait(timeout=5)
                except Exception:
                    pass
                break
            line = proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            line = line.rstrip("\r\n")
            out_lines.append(line)
            # forward child logs
            if line.startswith("[C-UI-SYNC]") or line.startswith("{"):
                _log(line)
            if line.startswith("SSO_JSON="):
                try:
                    payload = json.loads(line[len("SSO_JSON=") :])
                    sso = str(payload.get("sso") or "") or None
                except Exception:
                    pass
    except Exception as exc:
        _log(f"[C-UI-SYNC] subprocess read failed: {type(exc).__name__}")
        try:
            proc.kill()
        except Exception:
            pass

    if sso and len(sso) >= 20:
        _log(f"[C-UI-SYNC] subprocess sso len={len(sso)}")
        return sso

    # parse last SSO_JSON if missed
    for line in reversed(out_lines):
        if line.startswith("SSO_JSON="):
            try:
                payload = json.loads(line[len("SSO_JSON=") :])
                sso = str(payload.get("sso") or "") or None
                if sso and len(sso) >= 20:
                    return sso
            except Exception:
                pass
    _log("[C-UI-SYNC] subprocess no sso")
    return None


def _cli_main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="One-shot Sync Camoufox UI signup")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--code", default="")
    parser.add_argument("--handle", default="")
    parser.add_argument("--given-name", default="")
    parser.add_argument("--family-name", default="")
    parser.add_argument("--site-url", default="https://accounts.x.ai")
    args = parser.parse_args(argv)

    def _print(msg: str) -> None:
        print(msg, flush=True)

    # child always in-process
    os.environ["UI_SYNC_SUBPROCESS"] = "0"
    handle = (args.handle or "").strip()
    def _rc():
        if not handle:
            return None
        try:
            from grok_register.register import poll_code
            return poll_code(handle, max_wait=8)
        except Exception as exc:
            _print(f"[C-UI-SYNC] poll_code err={type(exc).__name__}")
            return None
    sso = _register_inprocess(
        email=args.email,
        password=args.password,
        code=args.code or "",
        given_name=args.given_name or "",
        family_name=args.family_name or "",
        site_url=args.site_url or "https://accounts.x.ai",
        refresh_code=_rc if handle else None,
        log=_print,
    )
    if sso:
        print("SSO_JSON=" + json.dumps({"ok": True, "sso": sso}, ensure_ascii=False), flush=True)
        return 0
    print("SSO_JSON=" + json.dumps({"ok": False, "sso": ""}, ensure_ascii=False), flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli_main())
