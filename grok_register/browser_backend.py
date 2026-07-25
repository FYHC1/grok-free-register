"""Browser backend for grok-free-register.

Supports:
  - camoufox: Firefox anti-detect via camoufox Async API (recommended)
  - cloakbrowser: Playwright + CloakBrowser Chromium

Independent browser backend integration.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import urllib.request
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator, Optional, Union


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()



def _ensure_virtual_display(log=None) -> Optional[str]:
    """Start a private Xvfb display for Camoufox virtual/headless stability."""
    if not sys.platform.startswith("linux"):
        return None
    if _env("DISPLAY") and _env("BROWSER_FORCE_XVFB", "0") not in ("1", "true", "yes", "on"):
        # reuse existing display unless forced
        return _env("DISPLAY")
    # pick a free display number
    for n in range(90, 120):
        lock = Path(f"/tmp/.X{n}-lock")
        if lock.exists():
            continue
        display = f":{n}"
        xvfb = shutil.which("Xvfb")
        if not xvfb:
            if log:
                log("[!] Xvfb not found; install with: sudo apt-get install -y xvfb")
            return None
        try:
            proc = subprocess.Popen(
                [xvfb, display, "-screen", "0", "1280x800x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            os.environ["DISPLAY"] = display
            # keep handle so process is not GC'd immediately
            _ensure_virtual_display._proc = proc  # type: ignore[attr-defined]
            if log:
                log(f"[*] Xvfb started display={display} pid={proc.pid}")
            return display
        except Exception as exc:
            if log:
                log(f"[!] Xvfb start failed: {type(exc).__name__}")
            return None
    return None


def browser_engine() -> str:
    raw = (_env("BROWSER_ENGINE") or _env("REGISTER_BROWSER") or "camoufox").lower()
    if raw in ("cloak", "cloakbrowser", "chrome", "chromium", "cb"):
        return "cloakbrowser"
    if raw in ("camoufox", "fox", "firefox", "cfox"):
        return "camoufox"
    if raw in ("auto", ""):
        return "camoufox" if camoufox_importable() else "cloakbrowser"
    return raw


def browser_headless_mode() -> Union[bool, str]:
    """Return True/False/'virtual'.

    virtual uses Xvfb-like path in Camoufox and often solves Turnstile better
    than pure headless on Linux/WSL.
    """
    raw = (_env("BROWSER_HEADLESS") or _env("HEADLESS") or "auto").lower()
    if raw in ("0", "false", "no", "off", "headed"):
        return False
    if raw in ("virtual", "xvfb", "x11"):
        return "virtual"
    if raw in ("1", "true", "yes", "on", "headless"):
        return True
    # auto: plain headless. Use BROWSER_HEADLESS=virtual explicitly if needed.
    return True


def browser_headless() -> bool:
    """Backward-compatible bool for callers that only care on/off. """
    mode = browser_headless_mode()
    return mode is not False


def browser_proxy_server() -> Optional[str]:
    raw = (
        _env("BROWSER_PROXY")
        or _env("CLOAK_PROXY")
        or _env("XAI_ENROLLER_PROXY")
        or _env("HTTPS_PROXY")
        or _env("HTTP_PROXY")
        or _env("https_proxy")
        or _env("http_proxy")
    )
    if not raw or raw.lower() in ("0", "false", "no", "off", "none", "direct"):
        return None
    return raw


def camoufox_importable() -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec("camoufox") is not None
    except Exception:
        return False


def find_cloakbrowser_chrome() -> str:
    paths = list(Path.home().glob(".cloakbrowser/chromium-*/chrome"))
    if not paths:
        raise RuntimeError("CloakBrowser not found under ~/.cloakbrowser")
    return str(sorted(paths)[-1])


def _resolve_camoufox_exe() -> Optional[str]:
    try:
        from camoufox import pkgman  # type: ignore

        path = pkgman.launch_path()
        if path and Path(path).exists():
            return str(path)
    except Exception:
        pass
    cands: list[Path] = []
    roots = [
        Path.home() / ".cache" / "camoufox",
        Path.home() / ".local" / "share" / "camoufox",
        Path("/root/.cache/camoufox"),
    ]
    for root in roots:
        if not root.exists():
            continue
        cands.extend(root.rglob("camoufox"))
        cands.extend(root.rglob("camoufox-bin"))
    cands = [p for p in cands if p.is_file() and os.access(p, os.X_OK)]
    if not cands:
        return None
    return str(sorted(cands, key=lambda p: p.stat().st_mtime)[-1])


def ensure_camoufox_ready(log=None) -> str:
    if not camoufox_importable():
        raise RuntimeError(
            'camoufox 未安装。请在 venv 中执行: pip install "camoufox[geoip]" && python -m camoufox fetch'
        )
    exe = _resolve_camoufox_exe()
    if exe:
        if log:
            log(f"[*] Camoufox ready: {exe}")
        return exe
    if log:
        log("[*] Camoufox binary missing; running python -m camoufox fetch ...")
    cmd = [sys.executable, "-m", "camoufox", "fetch"]
    subprocess.run(cmd, check=False)
    exe = _resolve_camoufox_exe()
    if not exe:
        raise RuntimeError("camoufox binary still missing after fetch")
    if log:
        log(f"[*] Camoufox ready: {exe}")
    return exe


def _proxy_dict(server: Optional[str]) -> Optional[dict]:
    if not server:
        return None
    return {"server": server}


def _fetch_exit_ip(proxy: Optional[str] = None, timeout: float = 6.0) -> Optional[str]:
    """Best-effort public IPv4/IPv6 detection (for geoip alignment)."""
    try:
        handlers = []
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open("https://api.ipify.org", timeout=timeout) as resp:
            ip = (resp.read() or b"").decode("utf-8", errors="ignore").strip()
        if ip and re.fullmatch(r"[0-9a-fA-F\.:]{3,45}", ip):
            return ip
    except Exception:
        return None
    return None


def _timezone_for_ip(ip: Optional[str]) -> str:
    forced = _env("BROWSER_TZ")
    if forced:
        return forced
    # Coarse mapping is enough to avoid US-TZ + JP-IP mismatch that kills Turnstile.
    # TUN users often egress Japan; keep Tokyo as safer default on Linux/WSL.
    if not ip:
        return "Asia/Tokyo" if sys.platform.startswith("linux") else "America/New_York"
    # Very rough JP datacenter heuristic from recent smoke exits.
    if ip.startswith("82.26.") or ip.startswith("103.") or ip.startswith("45."):
        return "Asia/Tokyo"
    return "Asia/Tokyo" if sys.platform.startswith("linux") else "America/New_York"


def _browser_os() -> Optional[str]:
    raw = _env("BROWSER_OS", "auto").lower()
    if raw in ("", "auto", "default"):
        # Match host OS. Forcing windows on Linux Camoufox can crash browser
        # process on some WSL builds (TargetClosedError during first goto).
        if sys.platform.startswith("win"):
            return "windows"
        if sys.platform == "darwin":
            return "macos"
        return "linux"
    if raw in ("0", "false", "no", "off", "none"):
        return None
    return raw


def cloak_launch_kwargs() -> dict:
    mode = browser_headless_mode()
    # WSL/Linux Chromium commonly needs no-sandbox; without it CloakBrowser
    # launches then dies on first context/page (TargetClosedError).
    args = [
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-blink-features=AutomationControlled",
        "--disable-features=IsolateOrigins,site-per-process",
        "--ignore-certificate-errors",
    ]
    extra = (_env("CLOAK_CHROME_ARGS") or "").strip()
    if extra:
        args.extend([a for a in extra.split() if a])
    kwargs: dict[str, Any] = {
        "executable_path": find_cloakbrowser_chrome(),
        "headless": bool(mode),
        "args": args,
        "chromium_sandbox": False,
    }
    proxy = _proxy_dict(browser_proxy_server())
    if proxy:
        kwargs["proxy"] = proxy
    return kwargs


def context_kwargs(engine: str, timezone_id: Optional[str] = None) -> dict:
    """Context options. Camoufox/Firefox prefers no_viewport."""
    locale = _env("BROWSER_LOCALE", "en-US") or "en-US"
    tz = timezone_id or _timezone_for_ip(None)
    if engine == "camoufox":
        return {
            "locale": locale,
            "timezone_id": tz,
            "no_viewport": True,
        }
    # Match CloakBrowser Chromium major when possible; mismatched UA can
    # make Turnstile stay on blank/crashed frames.
    default_ua = (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        if sys.platform.startswith("linux")
        else (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
        )
    )
    return {
        "locale": locale,
        "timezone_id": tz,
        "viewport": {"width": 1280, "height": 800},
        "user_agent": _env("BROWSER_UA", default_ua),
    }


@dataclass
class BrowserBundle:
    engine: str
    browser: Any
    playwright: Any = None
    camoufox_cm: Any = None
    timezone_id: str = "Asia/Tokyo"
    exit_ip: str = ""

    async def new_context(self, **overrides):
        kwargs = context_kwargs(self.engine, timezone_id=self.timezone_id)
        kwargs.update(overrides or {})
        try:
            return await self.browser.new_context(**kwargs)
        except TypeError:
            kwargs.pop("no_viewport", None)
            kwargs.pop("viewport", None)
            try:
                return await self.browser.new_context(**kwargs)
            except TypeError:
                return await self.browser.new_context()

    async def new_page(self, **overrides):
        # Isolated context per page keeps cookies clean and is more stable
        # under AsyncCamoufox on WSL than browser.new_page shortcuts.
        context = await self.new_context(**overrides)
        page = await context.new_page()
        try:
            page._gfr_context = context  # type: ignore[attr-defined]
        except Exception:
            pass
        return page

    async def close(self):
        try:
            await self.browser.close()
        except Exception:
            pass
        if self.camoufox_cm is not None:
            try:
                await self.camoufox_cm.__aexit__(None, None, None)
            except Exception:
                pass
        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass


@asynccontextmanager
async def launch_browser_bundle(log=None) -> AsyncIterator[BrowserBundle]:
    engine = browser_engine()
    if engine == "camoufox":
        ensure_camoufox_ready(log=log)
        from camoufox.async_api import AsyncCamoufox

        headless_mode = browser_headless_mode()
        # Only start Xvfb when explicitly requesting virtual mode.
        # Starting Xvfb for plain headless wastes RAM and can SIGKILL the
        # second Camoufox instance under WSL memory pressure.
        if str(headless_mode).lower() == "virtual":
            disp = _ensure_virtual_display(log=log)
            if disp and _env("BROWSER_HEADED_ON_XVFB", "0").lower() in ("1", "true", "yes", "on"):
                headless_mode = False
            else:
                headless_mode = "virtual"
        proxy = browser_proxy_server()
        exit_ip = _fetch_exit_ip(proxy)
        tz = _timezone_for_ip(exit_ip)
        os_name = _browser_os()

        launch_kwargs: dict[str, Any] = {
            "headless": headless_mode,
            "humanize": True,
            "geoip": False,
            "locale": _env("BROWSER_LOCALE", "en-US") or "en-US",
        }
        if os_name:
            launch_kwargs["os"] = os_name
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
            launch_kwargs["i_know_what_im_doing"] = True
        # Align geoip with real exit when possible (TUN or explicit proxy).
        geoip_mode = (_env("BROWSER_GEOIP") or "auto").lower()
        if geoip_mode in ("0", "false", "no", "off", "none"):
            launch_kwargs["geoip"] = False
        elif geoip_mode not in ("", "auto") and re.fullmatch(r"[0-9a-fA-F\.:]{3,45}", geoip_mode):
            launch_kwargs["geoip"] = geoip_mode
        elif exit_ip:
            launch_kwargs["geoip"] = exit_ip
        else:
            launch_kwargs["geoip"] = False
            if proxy:
                launch_kwargs["i_know_what_im_doing"] = True

        last_exc = None
        cm = None
        browser = None
        for _ in range(4):
            try:
                cm = AsyncCamoufox(**launch_kwargs)
                browser = await cm.__aenter__()
                last_exc = None
                break
            except TypeError as exc:
                last_exc = exc
                # Older camoufox may not accept virtual/os/i_know_what_im_doing.
                if launch_kwargs.get("headless") == "virtual":
                    launch_kwargs["headless"] = True
                launch_kwargs.pop("i_know_what_im_doing", None)
                launch_kwargs.pop("os", None)
                launch_kwargs["geoip"] = False
            except Exception as exc:
                last_exc = exc
                msg = str(exc).lower()
                if "geoip" in msg or "mmdb" in msg:
                    launch_kwargs["geoip"] = False
                    launch_kwargs["i_know_what_im_doing"] = True
                    continue
                if "virtual" in msg or "xvfb" in msg or "display" in msg:
                    launch_kwargs["headless"] = True
                    continue
                break
        if browser is None:
            raise RuntimeError(f"Camoufox launch failed: {last_exc}")
        if log:
            proxy_s = proxy or "off"
            geo = launch_kwargs.get("geoip")
            log(
                f"[*] Browser launched engine=camoufox headless={launch_kwargs.get('headless')} "
                f"proxy={proxy_s} os={launch_kwargs.get('os') or '-'} "
                f"geoip={geo if geo not in (False, None) else 'off'} "
                f"exit_ip={exit_ip or '-'} tz={tz}"
            )
        bundle = BrowserBundle(
            engine="camoufox",
            browser=browser,
            camoufox_cm=cm,
            timezone_id=tz,
            exit_ip=exit_ip or "",
        )
        try:
            yield bundle
        finally:
            await bundle.close()
        return

    from playwright.async_api import async_playwright

    pw = await async_playwright().start()
    try:
        kwargs = cloak_launch_kwargs()
        last_exc = None
        browser = None
        for _ in range(3):
            try:
                browser = await pw.chromium.launch(**kwargs)
                last_exc = None
                break
            except TypeError as exc:
                last_exc = exc
                kwargs.pop("chromium_sandbox", None)
            except Exception as exc:
                last_exc = exc
                # Retry once with minimal args if launch dies immediately.
                kwargs.setdefault("args", ["--no-sandbox", "--disable-dev-shm-usage"])
                if "Target closed" in str(exc) or "closed" in str(exc).lower():
                    continue
                break
        if browser is None:
            raise RuntimeError(f"CloakBrowser launch failed: {last_exc}")
        exit_ip = _fetch_exit_ip(browser_proxy_server())
        tz = _timezone_for_ip(exit_ip)
        if log:
            proxy = (kwargs.get("proxy") or {}).get("server") or "off"
            log(
                f"[*] Browser launched engine=cloakbrowser headless={kwargs.get('headless')} "
                f"proxy={proxy} args={len(kwargs.get('args') or [])} "
                f"exit_ip={exit_ip or '-'} tz={tz}"
            )
        bundle = BrowserBundle(
            engine="cloakbrowser",
            browser=browser,
            playwright=pw,
            timezone_id=tz,
            exit_ip=exit_ip or "",
        )
        try:
            yield bundle
        finally:
            await bundle.close()
    except Exception:
        try:
            await pw.stop()
        except Exception:
            pass
        raise



