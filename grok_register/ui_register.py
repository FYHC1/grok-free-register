"""UI-form signup path for accounts.x.ai (Playwright).

Why: server-action + synthetic Castle request tokens currently yield
botFlagDetails=castle_token:invalid_token. Successful UI signup drives the
real multi-step form so page-native Castle mints tokens on submit.

This module is local to grok-free-register only. Email fill/submit/Turnstile waits mirror the native Camoufox UI signup flow.
"""
from __future__ import annotations

import asyncio
import os
import random
import re
import string
from typing import Awaitable, Callable, Optional


RefreshCodeFn = Callable[[], Awaitable[Optional[str]]]


def _strong_password(seed: str = "") -> str:
    """xAI form often rejects weak lowercase-only passwords."""
    base = re.sub(r"[^A-Za-z0-9]", "", seed or "")
    if len(base) < 8:
        base = base + "".join(random.choice(string.ascii_lowercase) for _ in range(8))
    body = (base[:10] + str(random.randint(10, 99))).ljust(12, "x")
    return f"Aa1!{body}"[:20]


async def _sleep(page, ms: int = 300) -> None:
    try:
        await page.wait_for_timeout(ms)
    except Exception:
        await asyncio.sleep(ms / 1000.0)


async def _humanize(page) -> None:
    try:
        await page.mouse.move(random.randint(60, 280), random.randint(60, 220), steps=4)
        await page.mouse.move(random.randint(140, 520), random.randint(100, 360), steps=6)
        await page.mouse.wheel(0, random.randint(20, 120))
    except Exception:
        pass


def _should_inject_turnstile(token: str = "") -> bool:
    """Synthetic Turnstile inject often breaks Camoufox/Firefox form submit.

    Default: inject only when UI_INJECT_TURNSTILE=1 and a token is present.
    """
    raw = (os.environ.get("UI_INJECT_TURNSTILE") or "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return bool(token)
    engine = (
        os.environ.get("BROWSER_ENGINE")
        or os.environ.get("REGISTER_BROWSER")
        or "camoufox"
    ).strip().lower()
    if engine in ("camoufox", "fox", "firefox", "cfox", "auto", ""):
        return False
    return bool(token)


async def click_email_signup(page) -> bool:
    js = r"""() => {
      function isVisible(node) {
        if (!node) return false;
        const style = window.getComputedStyle(node);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
        const rect = node.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      }
      function nodeText(node) {
        return [
          node.innerText, node.textContent, node.getAttribute('aria-label'),
          node.getAttribute('title'), node.getAttribute('href'),
        ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
      }
      function scoreEntry(node) {
        const compact = nodeText(node).replace(/\s+/g, '');
        const lower = compact.toLowerCase();
        if (compact.includes('使用邮箱注册')) return 100;
        if (lower.includes('signupwithemail')) return 95;
        if (lower.includes('continuewithemail')) return 90;
        if (lower.includes('email') && (lower.includes('sign') || lower.includes('continue') || lower.includes('use') || lower.includes('with') || lower.includes('regist'))) return 80;
        if (lower.includes('邮箱') || lower === 'email') return 70;
        return 0;
      }
      const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]'))
        .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
        .map((node) => ({ node, score: scoreEntry(node), text: nodeText(node) }))
        .filter((item) => item.score > 0)
        .sort((a, b) => b.score - a.score);
      const target = candidates[0]?.node || null;
      if (!target) return '';
      target.click();
      return candidates[0].text || 'clicked';
    }"""
    for _ in range(12):
        try:
            text = await page.evaluate(js)
            if text:
                await _sleep(page, 800)
                return True
        except Exception:
            pass
        ready = await page.evaluate(
            """() => !!document.querySelector('input[type="email"], input[name="email"], input[autocomplete="email"], input[data-testid="email"]')"""
        )
        if ready:
            return True
        await _sleep(page, 500)
    return False

# browser-native React-compatible input setter.
_SET_INPUT_JS = r"""
({selectors, value}) => {
  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function textOf(node) {
    return [
      node.innerText, node.textContent, node.getAttribute('aria-label'),
      node.getAttribute('title'), node.getAttribute('placeholder'),
      node.getAttribute('data-testid'), node.getAttribute('name'),
      node.getAttribute('id'), node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  }
  const list = String(selectors || '').split(',').map(s => s.trim()).filter(Boolean);
  let input = null;
  for (const sel of list) {
    try {
      const nodes = Array.from(document.querySelectorAll(sel));
      input = nodes.find(n => isVisible(n) && !n.disabled && !n.readOnly) || nodes.find(isVisible) || null;
      if (input) break;
    } catch (e) {}
  }
  if (!input) {
    const keys = list.join(' ').toLowerCase();
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
      if (!isVisible(node) || node.disabled || node.readOnly) continue;
      const type = (node.getAttribute('type') || '').toLowerCase();
      if (['hidden','submit','button','checkbox','radio','file'].includes(type)) continue;
      const meta = textOf(node).toLowerCase();
      if (keys.includes('email') && (meta.includes('email') || meta.includes('mail') || meta.includes('邮箱'))) {
        input = node; break;
      }
    }
  }
  if (!input) return false;
  input.focus();
  try { input.click(); } catch (e) {}
  const proto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker && typeof tracker.setValue === 'function') tracker.setValue('');
  if (setter) setter.call(input, value); else input.value = value;
  try {
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
  } catch (e) {}
  try {
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
  } catch (e) {
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  input.dispatchEvent(new Event('change', { bubbles: true }));
  try { input.blur(); } catch (e) {}
  return String(input.value || '').trim() === String(value || '').trim();
}
"""


async def _set_input(page, selectors: str, value: str) -> bool:
    try:
        return bool(
            await page.evaluate(
                _SET_INPUT_JS,
                {"selectors": selectors, "value": value},
            )
        )
    except Exception:
        return False


_FILL_EMAIL_JS = r"""
(email) => {
  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function textOf(node) {
    return [
      node.innerText, node.textContent, node.getAttribute('aria-label'),
      node.getAttribute('title'), node.getAttribute('placeholder'),
      node.getAttribute('data-testid'), node.getAttribute('name'),
      node.getAttribute('id'), node.getAttribute('autocomplete'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  }
  function describeInput(node) {
    return [
      `type=${node.getAttribute('type') || ''}`,
      `name=${node.getAttribute('name') || ''}`,
      `id=${node.getAttribute('id') || ''}`,
      `placeholder=${node.getAttribute('placeholder') || ''}`,
      `aria=${node.getAttribute('aria-label') || ''}`,
      `testid=${node.getAttribute('data-testid') || ''}`,
    ].join(' ').replace(/\s+/g, ' ').trim().slice(0, 160);
  }
  function emailCandidates() {
    const direct = Array.from(document.querySelectorAll(
      'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'
    ));
    const all = Array.from(document.querySelectorAll('input, textarea'));
    for (const node of all) {
      const type = (node.getAttribute('type') || '').toLowerCase();
      if (['hidden', 'submit', 'button', 'checkbox', 'radio', 'file', 'search'].includes(type)) continue;
      const meta = textOf(node).toLowerCase();
      if (meta.includes('email') || meta.includes('e-mail') || meta.includes('mail') || meta.includes('邮箱') || meta.includes('电子邮件')) {
        direct.push(node);
      }
    }
    return Array.from(new Set(direct));
  }
  const visibleInputs = Array.from(document.querySelectorAll('input, textarea'))
    .filter((node) => isVisible(node) && !node.disabled && !node.readOnly)
    .map(describeInput)
    .slice(0, 8);
  const visibleActions = Array.from(document.querySelectorAll('button, a, [role="button"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true')
    .map((n) => textOf(n).slice(0, 80))
    .filter(Boolean)
    .slice(0, 10);
  const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
  if (!input) {
    return { state: 'not-ready', url: location.href, inputs: visibleInputs, buttons: visibleActions };
  }
  input.focus();
  try { input.click(); } catch (e) {}
  const valueProto = input instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const valueSetter = Object.getOwnPropertyDescriptor(valueProto, 'value')?.set;
  const tracker = input._valueTracker;
  if (tracker && typeof tracker.setValue === 'function') tracker.setValue('');
  if (valueSetter) valueSetter.call(input, email); else input.value = email;
  try {
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
  } catch (e) {}
  try {
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
  } catch (e) {
    input.dispatchEvent(new Event('input', { bubbles: true }));
  }
  input.dispatchEvent(new Event('change', { bubbles: true }));
  const inputType = (input.getAttribute('type') || '').toLowerCase();
  const isValid = inputType !== 'email' || input.checkValidity();
  if ((input.value || '').trim() !== email || !isValid) {
    return {
      state: 'fill-failed',
      value: input.value || '',
      valid: isValid,
      input: describeInput(input),
      url: location.href,
    };
  }
  try { input.blur(); } catch (e) {}
  return { state: 'filled', input: describeInput(input), url: location.href };
}
"""

_SUBMIT_EMAIL_JS = r"""
() => {
  function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  }
  function textOf(node) {
    return [
      node.innerText, node.textContent, node.getAttribute('aria-label'),
      node.getAttribute('title'), node.getAttribute('placeholder'),
      node.getAttribute('data-testid'), node.getAttribute('name'),
      node.getAttribute('id'), node.getAttribute('autocomplete'), node.getAttribute('value'),
    ].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
  }
  function emailCandidates() {
    const direct = Array.from(document.querySelectorAll(
      'input[data-testid="email"], input[name="email"], input[type="email"], input[autocomplete="email"], input[placeholder*="mail" i], input[aria-label*="mail" i]'
    ));
    return Array.from(new Set(direct));
  }
  const input = emailCandidates().find((node) => isVisible(node) && !node.disabled && !node.readOnly) || null;
  if (!input || !(input.value || '').trim()) return '';
  const inputType = (input.getAttribute('type') || '').toLowerCase();
  if (inputType === 'email' && !input.checkValidity()) return '';
  const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"], input[type="submit"]'))
    .filter((node) => isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true');
  const submitButton = buttons.find((node) => {
    const text = textOf(node).replace(/\s+/g, '');
    const lower = text.toLowerCase();
    return (
      text === '注册' ||
      text.includes('注册') ||
      text.includes('继续') ||
      text.includes('下一步') ||
      text.includes('确认') ||
      lower.includes('signup') ||
      lower.includes('sign up') ||
      lower.includes('continue') ||
      lower.includes('next') ||
      lower.includes('createaccount') ||
      lower.includes('submit')
    );
  });
  if (submitButton) {
    try { submitButton.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
    try { submitButton.focus(); } catch (e) {}
    submitButton.click();
    return textOf(submitButton) || 'clicked';
  }
  const form = input.closest('form');
  if (form) {
    if (form.requestSubmit) form.requestSubmit();
    else form.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true }));
    return 'form-submit';
  }
  input.focus();
  input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
  input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true, cancelable: true }));
  return 'enter';
}
"""


async def _click_submit(page, prefer: list[str] | None = None) -> str:
    prefer = prefer or []
    try:
        text = await page.evaluate(
            """(prefer) => {
              function isVisible(node) {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return false;
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              }
              function buttonText(node) {
                return [node.innerText, node.textContent, node.getAttribute('value'),
                        node.getAttribute('aria-label'), node.getAttribute('title')]
                  .filter(Boolean).join(' ').replace(/\\s+/g, ' ').trim();
              }
              const buttons = Array.from(document.querySelectorAll(
                'button[type="submit"], button, [role="button"], input[type="submit"]'
              )).filter(n => isVisible(n) && !n.disabled && n.getAttribute('aria-disabled') !== 'true');
              const scored = buttons.map(node => {
                const t = buttonText(node);
                const lower = t.replace(/\\s+/g, '').toLowerCase();
                let score = 0;
                for (const p of (prefer || [])) {
                  const pl = String(p).replace(/\\s+/g,'').toLowerCase();
                  if (pl && lower.includes(pl)) score += 50;
                }
                if (lower.includes('signup') || lower.includes('createaccount') || lower.includes('创建账户') || lower.includes('完成注册')) score += 40;
                if (lower.includes('continue') || lower.includes('继续') || lower.includes('next') || lower.includes('下一步')) score += 30;
                if (lower.includes('submit') || lower.includes('确认') || lower.includes('verify') || lower.includes('验证')) score += 25;
                if (lower.includes('goback') || lower.includes('返回') || lower.includes('back')) score -= 100;
                return {node, score, text: t};
              }).filter(x => x.score > 0).sort((a,b)=>b.score-a.score);
              const hit = scored[0];
              if (!hit) return '';
              try { hit.node.scrollIntoView({block:'center', inline:'center'}); } catch (e) {}
              try { hit.node.focus(); } catch (e) {}
              hit.node.click();
              return hit.text || 'clicked';
            }""",
            prefer,
        )
        return str(text or "")
    except Exception:
        return ""


async def _inject_turnstile(page, token: str) -> bool:
    if not token:
        return False
    return bool(
        await page.evaluate(
            """(token) => {
              let input = document.querySelector('input[name="cf-turnstile-response"]');
              if (!input) {
                input = document.createElement('input');
                input.type = 'hidden';
                input.name = 'cf-turnstile-response';
                (document.forms[0] || document.body).appendChild(input);
              }
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
              if (setter) setter.call(input, token); else input.value = token;
              input.dispatchEvent(new Event('input', {bubbles:true}));
              input.dispatchEvent(new Event('change', {bubbles:true}));
              return String(input.value || '').length > 20;
            }""",
            token,
        )
    )



async def _turnstile_info(page) -> dict:
    try:
        info = await page.evaluate(
            """() => {
              try {
                let token = String((document.querySelector('input[name="cf-turnstile-response"]') || {}).value || '').trim();
                if (!token) {
                  const ta = document.querySelector('textarea[name="cf-turnstile-response"]');
                  if (ta) token = String(ta.value || '').trim();
                }
                if (!token && window.turnstile && typeof turnstile.getResponse === 'function') {
                  token = String(turnstile.getResponse() || '').trim();
                }
                const iframes = Array.from(document.querySelectorAll('iframe'))
                  .map((n) => String(n.src || n.getAttribute('src') || ''))
                  .filter(Boolean)
                  .slice(0, 8);
                const sitekeyNode = document.querySelector('[data-sitekey]');
                const sitekey = sitekeyNode ? String(sitekeyNode.getAttribute('data-sitekey') || '') : '';
                const widget = !!(
                  document.querySelector('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], iframe[src*="cdn-cgi"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]')
                  || iframes.some((u) => /turnstile|challenges.cloudflare|cdn-cgi|cf-chl/i.test(u))
                );
                return {
                  present: !!(token || widget),
                  token_len: token.length,
                  solved: token.length >= 80,
                  sitekey: sitekey.slice(0, 40),
                  iframes,
                };
              } catch (e) {
                return { present: false, token_len: 0, solved: false, sitekey: '', iframes: [] };
              }
            }"""
        )
        if isinstance(info, dict):
            try:
                frame_urls = []
                for fr in list(page.frames):
                    try:
                        u = fr.url or ""
                    except Exception:
                        u = ""
                    if u:
                        frame_urls.append(u[:140])
                if frame_urls:
                    info["frame_urls"] = frame_urls[:10]
                    if not info.get("present"):
                        low = " ".join(frame_urls).lower()
                        if any(x in low for x in ("turnstile", "challenges.cloudflare", "cdn-cgi", "cf-chl")):
                            info["present"] = True
            except Exception:
                pass
            return info
    except Exception:
        pass
    return {"present": False, "token_len": 0, "solved": False, "sitekey": "", "iframes": []}


async def _nudge_turnstile(page, log=None) -> bool:
    """browser-native Turnstile interaction for headless Camoufox."""
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    info = await _turnstile_info(page)
    if info.get("solved"):
        return True

    detail = {"clicked": False, "method": "", "token_len": int(info.get("token_len") or 0)}

    # 0) Playwright locator pierces open shadow roots better than query_selector.
    try:
        loc = page.locator(
            'iframe[src*="challenges.cloudflare.com"], iframe[src*="turnstile"], iframe[src*="cdn-cgi"], div.cf-turnstile, [data-sitekey]'
        ).first
        try:
            await loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        box = await loc.bounding_box()
        if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
            points = [
                (box["x"] + 28, box["y"] + box["height"] * 0.5),
                (box["x"] + 22, box["y"] + box["height"] * 0.48),
                (box["x"] + min(36.0, box["width"] * 0.15), box["y"] + box["height"] * 0.52),
                (box["x"] + box["width"] * 0.5, box["y"] + box["height"] * 0.5),
            ]
            for x, y in points:
                try:
                    await page.mouse.move(float(x), float(y), steps=12)
                    await _sleep(page, 120)
                    await page.mouse.click(float(x), float(y), delay=60)
                    detail["clicked"] = True
                    detail["method"] = "locator-host-coords"
                    for _ in range(10):
                        await _sleep(page, 450)
                        info2 = await _turnstile_info(page)
                        detail["token_len"] = int(info2.get("token_len") or 0)
                        if info2.get("solved"):
                            _log(f"[C-UI] turnstile solved via locator-host len={detail['token_len']}")
                            return True
                    break
                except Exception:
                    continue
    except Exception:
        pass

    # 1) Main-page host box (checkbox is left side).
    try:
        host = await page.query_selector(
            'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], iframe[src*="cdn-cgi"], div.cf-turnstile, [data-sitekey]'
        )
        if host:
            try:
                await host.scroll_into_view_if_needed()
            except Exception:
                pass
            box = await host.bounding_box()
            if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
                points = [
                    (box["x"] + 28, box["y"] + box["height"] * 0.5),
                    (box["x"] + 22, box["y"] + box["height"] * 0.5),
                    (box["x"] + min(36, box["width"] * 0.18), box["y"] + box["height"] * 0.52),
                ]
                for x, y in points:
                    try:
                        await page.mouse.move(float(x), float(y), steps=10)
                        await _sleep(page, 90)
                        await page.mouse.click(float(x), float(y), delay=50)
                        detail["clicked"] = True
                        detail["method"] = "main-host-coords"
                        for _ in range(8):
                            await _sleep(page, 450)
                            info2 = await _turnstile_info(page)
                            detail["token_len"] = int(info2.get("token_len") or 0)
                            if info2.get("solved"):
                                _log(f"[C-UI] turnstile solved via main-host len={detail['token_len']}")
                                return True
                        break
                    except Exception:
                        continue
    except Exception:
        pass

    try:
        frames = list(page.frames)
    except Exception:
        frames = []

    def _is_cf_url(url: str) -> bool:
        u = (url or "").lower()
        return any(
            x in u
            for x in (
                "challenges.cloudflare.com",
                "turnstile",
                "cdn-cgi/challenge-platform",
                "cf-chl",
            )
        )

    frame_meta = []
    for fr in frames:
        try:
            frame_meta.append({"frame": fr, "url": fr.url or ""})
        except Exception:
            continue
    turnstile_frames = [m for m in frame_meta if _is_cf_url(m.get("url") or "")]
    ordered = turnstile_frames + (
        [m for m in frame_meta if m not in turnstile_frames] if not turnstile_frames else []
    )

    click_selectors = [
        "input[type='checkbox']",
        "label.ctp-checkbox-label",
        ".ctp-checkbox-label",
        "#challenge-stage input",
        "#challenge-stage",
        "[role='checkbox']",
        "label",
        "body",
    ]

    for meta in ordered:
        fr = meta["frame"]
        prefer = _is_cf_url(meta.get("url") or "")
        if not prefer and turnstile_frames:
            continue
        try:
            el = await fr.frame_element()
            try:
                await el.scroll_into_view_if_needed()
            except Exception:
                pass
            box = await el.bounding_box()
            if box and box.get("width", 0) >= 20 and box.get("height", 0) >= 20:
                points = [
                    (box["x"] + 28, box["y"] + box["height"] * 0.5),
                    (box["x"] + 22, box["y"] + box["height"] * 0.48),
                    (box["x"] + box["width"] * 0.12, box["y"] + box["height"] * 0.5),
                ]
                for x, y in points:
                    try:
                        await page.mouse.move(float(x), float(y), steps=8)
                        await _sleep(page, 80)
                        await page.mouse.click(float(x), float(y), delay=40)
                        detail["clicked"] = True
                        detail["method"] = "mouse-checkbox-coords"
                        for _ in range(8):
                            await _sleep(page, 400)
                            info = await _turnstile_info(page)
                            detail["token_len"] = int(info.get("token_len") or 0)
                            if info.get("solved"):
                                _log(
                                    f"[C-UI] turnstile solved via mouse-coords len={detail['token_len']}"
                                )
                                return True
                        break
                    except Exception:
                        continue
        except Exception:
            pass

        for sel in click_selectors:
            try:
                loc = fr.locator(sel).first
                try:
                    cnt = await loc.count()
                except Exception:
                    cnt = 1
                if cnt == 0:
                    continue
                try:
                    visible = await loc.is_visible(timeout=700)
                except Exception:
                    visible = True
                if not visible and sel != "body":
                    continue
                try:
                    await loc.hover(timeout=1000)
                except Exception:
                    pass
                await loc.click(timeout=2500, force=True)
                detail["clicked"] = True
                detail["method"] = f"frame-click:{sel}"
                break
            except Exception:
                continue

        if detail["clicked"]:
            for _ in range(8):
                await _sleep(page, 400)
                info = await _turnstile_info(page)
                detail["token_len"] = int(info.get("token_len") or 0)
                if info.get("solved"):
                    _log(f"[C-UI] turnstile solved via {detail['method']} len={detail['token_len']}")
                    return True
            break

    if not detail["clicked"]:
        for sel in (
            "iframe[src*='turnstile']",
            "iframe[src*='challenges.cloudflare']",
            "iframe[src*='cdn-cgi']",
        ):
            try:
                fl = page.frame_locator(sel)
                for inner in ("input[type='checkbox']", "label", ".ctp-checkbox-label", "body"):
                    try:
                        await fl.locator(inner).first.click(timeout=1500, force=True)
                        detail["clicked"] = True
                        detail["method"] = f"frame_locator:{inner}"
                        break
                    except Exception:
                        continue
                if detail["clicked"]:
                    break
            except Exception:
                continue

    if detail["clicked"]:
        for _ in range(10):
            await _sleep(page, 450)
            info = await _turnstile_info(page)
            detail["token_len"] = int(info.get("token_len") or 0)
            if info.get("solved"):
                _log(f"[C-UI] turnstile solved via {detail.get('method')} len={detail['token_len']}")
                return True
        _log(
            f"[C-UI] turnstile click method={detail.get('method') or '-'} "
            f"len={detail.get('token_len') or 0}"
        )
        return True
    _log("[C-UI] turnstile click missed")
    return False


async def _turnstile_crashed(page) -> bool:
    info = await _turnstile_info(page)
    urls = " ".join((info.get("frame_urls") or info.get("iframes") or [])).lower()
    return "crashed" in urls or "crashed_retry" in urls


async def _recover_crashed_turnstile(page, log=None) -> bool:
    """Reload/reset crashed Turnstile widget (common on Linux headless Camoufox)."""
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    if not await _turnstile_crashed(page):
        return False
    _log("[C-UI] turnstile crashed_retry detected; attempting reset")
    try:
        await page.evaluate(
            """() => {
              try {
                if (window.turnstile && typeof turnstile.reset === 'function') {
                  turnstile.reset();
                  return 'reset';
                }
              } catch (e) {}
              return 'no-reset';
            }"""
        )
    except Exception:
        pass
    await _sleep(page, 1200)
    try:
        await page.evaluate(
            """() => {
              const nodes = Array.from(document.querySelectorAll('iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], div.cf-turnstile'));
              for (const n of nodes) {
                try {
                  n.style.display = 'none';
                  n.offsetHeight;
                  n.style.display = '';
                } catch (e) {}
              }
              try {
                if (window.turnstile && typeof turnstile.reset === 'function') turnstile.reset();
              } catch (e) {}
              return nodes.length;
            }"""
        )
    except Exception:
        pass
    await _sleep(page, 1500)
    info = await _turnstile_info(page)
    _log(
        f"[C-UI] after crash-recover present={info.get('present')} "
        f"len={info.get('token_len') or 0} crashed={int(await _turnstile_crashed(page))}"
    )
    return True


async def _extract_turnstile_sitekey(page) -> str:
    try:
        key = await page.evaluate(
            """() => {
              const n = document.querySelector('[data-sitekey]');
              if (n) return String(n.getAttribute('data-sitekey') || '').trim();
              const iframes = Array.from(document.querySelectorAll('iframe'))
                .map((x) => String(x.src || ''))
                .filter(Boolean);
              for (const u of iframes) {
                const m = u.match(/(0x[0-9A-Za-z_-]{8,})/);
                if (m) return m[1];
              }
              return '';
            }"""
        )
        if key:
            return str(key).strip()
    except Exception:
        pass
    info = await _turnstile_info(page)
    for u in (info.get("frame_urls") or info.get("iframes") or []):
        m = re.search(r"(0x[0-9A-Za-z_-]{8,})", str(u or ""))
        if m:
            return m.group(1)
    return (
        (os.environ.get("TURNSTILE_SITEKEY") or os.environ.get("SITE_KEY") or "").strip()
        or "0x4AAAAAAAhr9JGVDZbrZOo0"
    )


async def _solve_turnstile_helper_widget(page, log=None, timeout_s: float = 40.0) -> bool:
    """Last-resort: inject a standalone Turnstile widget (same idea as S-worker).

    Uses api.js without render=explicit (matches register.py solver inject).
    """
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    info = await _turnstile_info(page)
    if info.get("solved"):
        return True
    sitekey = await _extract_turnstile_sitekey(page)
    if not sitekey:
        sitekey = (os.environ.get("TURNSTILE_SITEKEY") or "0x4AAAAAAAhr9JGVDZbrZOo0").strip()
    _log(f"[C-UI] turnstile helper-widget start sitekey={sitekey[:24]}")

    try:
        await page.evaluate(
            """(sitekey) => {
              try { document.querySelectorAll('#gfr-ts-helper').forEach((n) => n.remove()); } catch (e) {}
              window.__gfr_ts_token = '';
              window.__gfr_ts_err = '';
              const d = document.createElement('div');
              d.id = 'gfr-ts-helper';
              d.className = 'cf-turnstile';
              d.setAttribute('data-sitekey', sitekey);
              d.style.cssText = 'position:fixed;top:10px;left:10px;z-index:2147483647;background:white;padding:12px;border:2px solid red;border-radius:6px;width:300px;height:70px;';
              document.body.appendChild(d);
              function writeToken(t) {
                window.__gfr_ts_token = t || '';
                let input = document.querySelector('input[name="cf-turnstile-response"]');
                if (!input) {
                  input = document.createElement('input');
                  input.type = 'hidden';
                  input.name = 'cf-turnstile-response';
                  document.body.appendChild(input);
                }
                input.value = t || '';
                try { input.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}
                try { input.dispatchEvent(new Event('change', { bubbles: true })); } catch (e) {}
              }
              function render() {
                try {
                  if (!window.turnstile || typeof window.turnstile.render !== 'function') return;
                  window.turnstile.render(d, { sitekey: sitekey, callback: writeToken });
                } catch (e) {
                  window.__gfr_ts_err = e && e.message ? e.message : String(e);
                }
              }
              if (window.turnstile) {
                render();
              } else {
                const s = document.createElement('script');
                // Match S-worker inject: plain api.js (no render=explicit).
                s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js';
                s.async = true;
                s.onload = () => setTimeout(render, 1000);
                s.onerror = () => { window.__gfr_ts_err = 'script_load_failed'; };
                document.head.appendChild(s);
              }
            }""",
            sitekey,
        )
    except Exception as exc:
        _log(f"[C-UI] helper-widget inject err={type(exc).__name__}")
        return False

    await _sleep(page, 1500)
    deadline = asyncio.get_event_loop().time() + max(12.0, float(timeout_s or 40.0))
    last_click = 0.0
    while asyncio.get_event_loop().time() < deadline:
        info = await _turnstile_info(page)
        if info.get("solved"):
            _log(f"[C-UI] helper-widget solved len={info.get('token_len')}")
            return True
        try:
            token = await page.evaluate("() => String(window.__gfr_ts_token || '').trim()")
            if token and len(token) >= 80:
                await _inject_turnstile(page, token)
                _log(f"[C-UI] helper-widget token copied len={len(token)}")
                return True
        except Exception:
            pass

        now = asyncio.get_event_loop().time()
        if now - last_click >= 2.5:
            last_click = now
            clicked = False
            # Prefer coordinate click on helper box / challenge iframe left checkbox.
            try:
                box = await page.evaluate(
                    """() => {
                      const e = document.querySelector('#gfr-ts-helper, #gfr-ts-helper iframe, iframe[src*="challenges.cloudflare"], .cf-turnstile');
                      if (!e) return null;
                      const r = e.getBoundingClientRect();
                      return {x:r.left, y:r.top, w:r.width, h:r.height};
                    }"""
                )
                if box and float(box.get("w") or 0) >= 20 and float(box.get("h") or 0) >= 20:
                    points = [
                        (float(box["x"]) + 28.0, float(box["y"]) + float(box["h"]) * 0.5),
                        (float(box["x"]) + 22.0, float(box["y"]) + float(box["h"]) * 0.48),
                        (float(box["x"]) + float(box["w"]) * 0.12, float(box["y"]) + float(box["h"]) * 0.5),
                    ]
                    for x, y in points:
                        try:
                            await page.mouse.move(x, y, steps=8)
                            await _sleep(page, 80)
                            await page.mouse.click(x, y, delay=40)
                            clicked = True
                        except Exception:
                            continue
            except Exception:
                pass
            if not clicked:
                await _nudge_turnstile(page, log=log)
            else:
                _log("[C-UI] helper-widget clicked checkbox-coords")
        await _sleep(page, 700)

    info = await _turnstile_info(page)
    err = ""
    try:
        err = await page.evaluate("() => String(window.__gfr_ts_err || '')")
    except Exception:
        err = ""
    frames = info.get("frame_urls") or info.get("iframes") or []
    _log(
        f"[C-UI] helper-widget timeout len={info.get('token_len') or 0} "
        f"err={err or '-'} frames={frames[:3]}"
    )
    return bool(info.get("solved"))

async def _wait_turnstile(page, log=None, timeout_s: float = 25.0, label: str = "step") -> bool:
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    info = await _turnstile_info(page)
    if not info.get("present"):
        return True
    if info.get("solved"):
        _log(f"[C-UI] {label} turnstile already solved len={info.get('token_len')}")
        return True

    frames = info.get("frame_urls") or info.get("iframes") or []
    _log(
        f"[C-UI] {label} waiting native turnstile present=1 "
        f"len={info.get('token_len') or 0} frames={len(frames)}"
    )
    if frames:
        _log(f"[C-UI] {label} turnstile frames={frames[:4]}")

    deadline = asyncio.get_event_loop().time() + max(8.0, float(timeout_s or 25.0))
    started = asyncio.get_event_loop().time()
    last_nudge = 0.0
    last_log = 0.0
    helper_tried = False
    passive_s = float(os.environ.get("UI_TURNSTILE_PASSIVE_S") or "6")
    nudge_every_s = float(os.environ.get("UI_TURNSTILE_NUDGE_EVERY_S") or "3")
    helper_after_s = float(os.environ.get("UI_TURNSTILE_HELPER_AFTER_S") or "45")
    # Default OFF: helper inject often kills page-native CF frames (upstream UI flow does native click only).
    helper_enabled = (os.environ.get("UI_TURNSTILE_HELPER") or "0").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )
    while asyncio.get_event_loop().time() < deadline:
        try:
            info = await _turnstile_info(page)
        except Exception as exc:
            if "TargetClosed" in type(exc).__name__ or "closed" in str(exc).lower():
                _log(f"[C-UI] {label} turnstile aborted: {type(exc).__name__}")
                return False
            raise
        if info.get("solved"):
            _log(f"[C-UI] {label} turnstile solved len={info.get('token_len')}")
            return True
        now = asyncio.get_event_loop().time()
        waited = now - started
        if await _turnstile_crashed(page) and waited >= 2.0:
            await _recover_crashed_turnstile(page, log=log)
            started = asyncio.get_event_loop().time()
            last_nudge = 0.0
            await _sleep(page, 1000)
            continue
        if (
            helper_enabled
            and not helper_tried
            and waited >= helper_after_s
            and int(info.get("token_len") or 0) < 80
        ):
            helper_tried = True
            if await _solve_turnstile_helper_widget(
                page,
                log=log,
                timeout_s=min(35.0, max(12.0, deadline - now)),
            ):
                return True
            continue
        if waited >= passive_s and (now - last_nudge) >= nudge_every_s:
            # Avoid random mouse moves right before CF checkbox click.
            await _nudge_turnstile(page, log=log)
            last_nudge = now
            info = await _turnstile_info(page)
            _log(
                f"[C-UI] {label} turnstile after-nudge len={info.get('token_len') or 0} "
                f"waited={waited:.1f}s"
            )
            if info.get("solved"):
                return True
        elif (now - last_log) >= 4.0:
            last_log = now
            _log(
                f"[C-UI] {label} turnstile passive wait len={info.get('token_len') or 0} "
                f"waited={waited:.1f}s"
            )
        await _sleep(page, 800)

    if helper_enabled and not helper_tried:
        if await _solve_turnstile_helper_widget(page, log=log, timeout_s=25.0):
            return True

    info = await _turnstile_info(page)
    frames = info.get("frame_urls") or info.get("iframes") or []
    _log(
        f"[C-UI] {label} turnstile timeout present={info.get('present')} "
        f"len={info.get('token_len') or 0} frames={frames[:3]}"
    )
    return bool(info.get("solved"))


async def _page_has_profile(page) -> bool:
    return bool(
        await page.evaluate(
            """() => {
              const g = document.querySelector('input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]');
              const f = document.querySelector('input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]');
              const p = document.querySelector('input[data-testid="password"], input[name="password"], input[type="password"]');
              return !!(g && f && p);
            }"""
        )
    )


async def _page_has_code(page) -> bool:
    return bool(
        await page.evaluate(
            """() => {
              function visible(n){
                if(!n) return false;
                const s=getComputedStyle(n);
                if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
                const r=n.getBoundingClientRect();
                return r.width>0 && r.height>0;
              }
              const nodes = Array.from(document.querySelectorAll('input, textarea'));
              for (const n of nodes) {
                if (!visible(n) || n.disabled) continue;
                const meta = [
                  n.getAttribute('autocomplete')||'',
                  n.getAttribute('name')||'',
                  n.getAttribute('id')||'',
                  n.getAttribute('data-testid')||'',
                  n.getAttribute('placeholder')||'',
                  n.getAttribute('aria-label')||'',
                  n.getAttribute('data-input-otp')||'',
                ].join(' ').toLowerCase();
                if (
                  meta.includes('one-time-code') || meta.includes('otp') ||
                  meta.includes('verification') || meta === 'code' ||
                  meta.includes('验证码') ||
                  (meta.includes('code') && (meta.includes('email') || meta.includes('verify')))
                ) {
                  return true;
                }
              }
              const emailStill = document.querySelector(
                'input[type="email"], input[name="email"], input[autocomplete="email"], input[data-testid="email"]'
              );
              if (emailStill && visible(emailStill)) return false;
              return !!nodes.find(n =>
                visible(n) && (
                  (n.getAttribute('inputmode')||'').toLowerCase()==='numeric' ||
                  (n.getAttribute('maxlength')||'')==='6' ||
                  n.getAttribute('data-input-otp') === 'true'
                )
              );
            }"""
        )
    )


async def _page_has_email(page) -> bool:
    return bool(
        await page.evaluate(
            """() => {
              function visible(n){
                if(!n) return false;
                const s=getComputedStyle(n);
                if(s.display==='none'||s.visibility==='hidden'||s.opacity==='0') return false;
                const r=n.getBoundingClientRect();
                return r.width>0 && r.height>0;
              }
              const n = document.querySelector(
                'input[type="email"], input[name="email"], input[autocomplete="email"], input[data-testid="email"]'
              );
              return !!(n && visible(n));
            }"""
        )
    )



async def _page_diag(page) -> str:
    try:
        d = await page.evaluate(
            """() => {
              function isVisible(node) {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              }
              const inputs = Array.from(document.querySelectorAll('input, textarea'))
                .filter(isVisible)
                .map(n => `${n.getAttribute('type')||''}:${n.getAttribute('name')||n.getAttribute('data-testid')||n.getAttribute('autocomplete')||''}`)
                .slice(0, 8);
              const buttons = Array.from(document.querySelectorAll('button, [role="button"]'))
                .filter(isVisible)
                .map(n => (n.innerText||n.textContent||'').replace(/\\s+/g,' ').trim().slice(0,40))
                .filter(Boolean)
                .slice(0, 6);
              let token = String((document.querySelector('input[name="cf-turnstile-response"]')||{}).value||'').trim();
              if (!token) {
                const ta = document.querySelector('textarea[name="cf-turnstile-response"]');
                if (ta) token = String(ta.value || '').trim();
              }
              const widget = !!document.querySelector(
                'iframe[src*="turnstile"], iframe[src*="challenges.cloudflare"], iframe[src*="cdn-cgi"], div.cf-turnstile, [data-sitekey], script[src*="turnstile"]'
              );
              const iframes = Array.from(document.querySelectorAll('iframe'))
                .map((n) => String(n.src || '').slice(0, 100))
                .filter(Boolean)
                .slice(0, 4);
              return {url: location.href.slice(0,160), inputs, buttons, cf: token.length, widget, iframes};
            }"""
        )
        info = await _turnstile_info(page)
        if isinstance(d, dict):
            frames = info.get("frame_urls") or d.get("iframes") or []
            return (
                f"url={d.get('url')} inputs={d.get('inputs')} "
                f"buttons={d.get('buttons')} cf_len={d.get('cf')} "
                f"cf_widget={int(bool(d.get('widget') or info.get('present')))} "
                f"frames={frames[:3]}"
            )
    except Exception as exc:
        return f"diag_err={type(exc).__name__}"
    return ""


async def _fill_code(page, code: str) -> bool:
    code = re.sub(r"[\s-]+", "", code or "")
    if not code:
        return False
    try:
        result = await page.evaluate(
            """(code) => {
              function isVisible(node) {
                if (!node) return false;
                const style = window.getComputedStyle(node);
                if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
                const rect = node.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0;
              }
              function setInputValue(input, value) {
                const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                const tracker = input._valueTracker;
                if (tracker && typeof tracker.setValue === 'function') tracker.setValue('');
                if (nativeSetter) nativeSetter.call(input, value); else input.value = value;
                try {
                  input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
                } catch (e) {}
                try {
                  input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
                } catch (e) {
                  input.dispatchEvent(new Event('input', { bubbles: true }));
                }
                input.dispatchEvent(new Event('change', { bubbles: true }));
              }
              const aggregate = Array.from(document.querySelectorAll(
                'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
              )).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);
              if (aggregate) {
                aggregate.focus();
                try { aggregate.click(); } catch (e) {}
                setInputValue(aggregate, code);
                return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
              }
              const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
                if (!isVisible(node) || node.disabled || node.readOnly) return false;
                const maxLength = Number(node.maxLength || 0);
                const ac = String(node.autocomplete || '').toLowerCase();
                return maxLength === 1 || ac === 'one-time-code';
              });
              if (otpBoxes.length >= Math.min(code.length, 4)) {
                for (let i = 0; i < code.length && i < otpBoxes.length; i++) {
                  const ch = code[i] || '';
                  const box = otpBoxes[i];
                  box.focus();
                  try { box.click(); } catch (e) {}
                  setInputValue(box, ch);
                }
                const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
                return merged.length ? 'filled-boxes' : 'boxes-failed';
              }
              return 'not-ready';
            }""",
            code,
        )
        return str(result or "").startswith("filled")
    except Exception:
        return False


async def _get_sso(page):
    try:
        cookies = await page.context.cookies()
    except Exception:
        return "", []
    sso = ""
    for c in cookies:
        if c.get("name") == "sso" and c.get("value"):
            if "grok.com" in str(c.get("domain") or ""):
                return str(c.get("value") or ""), cookies
            if not sso:
                sso = str(c.get("value") or "")
    return sso, cookies


async def _fill_and_submit_email(page, email: str, log=None) -> bool:
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    deadline = asyncio.get_event_loop().time() + 45
    last_state = ""
    while asyncio.get_event_loop().time() < deadline:
        try:
            filled = await page.evaluate(_FILL_EMAIL_JS, email)
        except Exception as exc:
            _log(f"[C-UI] fill email js err={type(exc).__name__}")
            filled = {"state": "error"}
        state = filled.get("state") if isinstance(filled, dict) else str(filled or "")
        if state != last_state:
            last_state = state
            if state == "not-ready":
                _log("[C-UI] email input not ready")
            elif state == "fill-failed":
                _log(f"[C-UI] email fill-failed value_len={len(str((filled or {}).get('value') or ''))}")
            elif state == "filled":
                _log(f"[C-UI] email filled input={(filled or {}).get('input') or '-'}")
        if state == "not-ready":
            await _sleep(page, 500)
            continue
        if state != "filled":
            await _sleep(page, 500)
            continue

        await _humanize(page)
        await _wait_turnstile(page, log=log, timeout_s=6.0, label="email-pre")
        try:
            clicked = await page.evaluate(_SUBMIT_EMAIL_JS)
        except Exception:
            clicked = ""
        if not clicked:
            clicked = await _click_submit(
                page,
                ["sign up", "signup", "continue", "继续", "next", "下一步", "提交", "注册"],
            )
        _log(f"[C-UI] email submit={clicked or '-'}")
        if clicked:
            return True
        await _sleep(page, 600)
    return False



async def _advance_past_email(page, email: str, log=None, timeout_s: float = 45.0) -> bool:
    """After first email submit, wait for CF/page transition; resubmit only when needed."""
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    deadline = asyncio.get_event_loop().time() + max(15.0, float(timeout_s or 45.0))
    started = asyncio.get_event_loop().time()
    last_submit = 0.0
    submits = 0
    while asyncio.get_event_loop().time() < deadline:
        if await _page_has_code(page) or await _page_has_profile(page):
            _log("[C-UI] advanced past email")
            return True
        still_email = await _page_has_email(page)
        info = await _turnstile_info(page)
        waited = asyncio.get_event_loop().time() - started
        if info.get("present") and not info.get("solved"):
            remain = max(5.0, min(18.0, deadline - asyncio.get_event_loop().time()))
            await _wait_turnstile(page, log=log, timeout_s=remain, label="email-post")
            info = await _turnstile_info(page)
        now = asyncio.get_event_loop().time()
        if still_email and info.get("solved") and (now - last_submit) >= 3.0 and submits < 4:
            try:
                clicked = await page.evaluate(_SUBMIT_EMAIL_JS)
            except Exception:
                clicked = ""
            if not clicked:
                clicked = await _click_submit(
                    page,
                    ["sign up", "signup", "continue", "继续", "next", "下一步", "提交", "注册"],
                )
            last_submit = now
            submits += 1
            _log(
                f"[C-UI] email resubmit-after-cf click={clicked or '-'} "
                f"cf_len={info.get('token_len') or 0} n={submits}"
            )
            await _sleep(page, 1200)
            continue
        if still_email and (not info.get("present")) and waited >= 8 and (now - last_submit) >= 8 and submits < 3:
            try:
                await page.evaluate(_FILL_EMAIL_JS, email)
            except Exception:
                pass
            try:
                clicked = await page.evaluate(_SUBMIT_EMAIL_JS)
            except Exception:
                clicked = ""
            if not clicked:
                clicked = await _click_submit(
                    page,
                    ["sign up", "signup", "continue", "继续", "next", "下一步", "提交", "注册"],
                )
            last_submit = now
            submits += 1
            _log(f"[C-UI] email soft-resubmit click={clicked or '-'} n={submits}")
        if int(waited) in (5, 12, 20, 30):
            try:
                _log(f"[C-UI] email-advance diag={await _page_diag(page)}")
            except Exception:
                pass
        await _sleep(page, 1000)
    try:
        _log(f"[C-UI] email-advance timeout diag={await _page_diag(page)}")
    except Exception:
        pass
    return bool(await _page_has_code(page) or await _page_has_profile(page))


async def register_via_ui(
    page,
    *,
    email: str,
    password: str,
    code: str = "",
    turnstile_token: str = "",
    given_name: str = "",
    family_name: str = "",
    site_url: str = "https://accounts.x.ai",
    refresh_code: RefreshCodeFn | None = None,
    log=None,
) -> str | None:
    """Drive real signup UI; return sso cookie value or None.

    Important: do NOT inject synthetic Castle tokens here. Invalid injected
    tokens mark the account bot-flagged. Let page-native Castle mint on submit.
    """

    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

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
        password = _strong_password(password or email.split("@")[0])
        _log("[C-UI] strengthened password for form requirements")

    try:
        return await _register_via_ui_body(
            page,
            email=email,
            password=password,
            active_code=active_code,
            turnstile_token=turnstile_token,
            given_name=given_name,
            family_name=family_name,
            site_url=site_url,
            refresh_code=refresh_code,
            log=log,
        )
    except Exception as exc:
        name = type(exc).__name__
        msg = str(exc or "")
        if "TargetClosed" in name or "Target closed" in msg or "has been closed" in msg.lower():
            _log(f"[C-UI] browser/page closed: {name}")
        else:
            _log(f"[C-UI] unexpected error: {name}")
        raise


async def _register_via_ui_body(
    page,
    *,
    email: str,
    password: str,
    active_code: str = "",
    turnstile_token: str = "",
    given_name: str = "",
    family_name: str = "",
    site_url: str = "https://accounts.x.ai",
    refresh_code: RefreshCodeFn | None = None,
    log=None,
) -> str | None:
    def _log(msg: str) -> None:
        if log:
            try:
                log(msg)
            except Exception:
                pass

    _log("[C-UI] open sign-up")
    await page.goto(
        f"{site_url}/sign-up?redirect=grok-com",
        timeout=45000,
        wait_until="domcontentloaded",
    )
    await _sleep(page, 1200)
    await _humanize(page)

    if not await click_email_signup(page):
        has_email = await _page_has_email(page)
        if not has_email:
            _log("[C-UI] email signup button not found")
            return None

    _log("[C-UI] fill email")
    if not await _fill_and_submit_email(page, email, log=log):
        _log("[C-UI] email fill/submit failed")
        try:
            _log(f"[C-UI] diag={await _page_diag(page)}")
        except Exception:
            pass
        return None

    await _sleep(page, 1200)
    try:
        _log(f"[C-UI] after-email url={page.url}")
    except Exception:
        pass
    advanced = await _advance_past_email(page, email, log=log, timeout_s=50.0)
    try:
        _log(f"[C-UI] post-advance url={page.url} advanced={int(bool(advanced))}")
    except Exception:
        _log(f"[C-UI] post-advance advanced={int(bool(advanced))}")
    if not advanced:
        await _humanize(page)
        await _wait_turnstile(page, log=log, timeout_s=15.0, label="email-last")
        try:
            await page.evaluate(_FILL_EMAIL_JS, email)
            clicked = await page.evaluate(_SUBMIT_EMAIL_JS)
        except Exception:
            clicked = ""
        _log(f"[C-UI] email last-chance submit={clicked or '-'}")
        await _sleep(page, 2500)

    if refresh_code is not None and not active_code:
        _log("[C-UI] polling mailbox for verification code")
        deadline = asyncio.get_event_loop().time() + 120
        attempts = 0
        while asyncio.get_event_loop().time() < deadline and not active_code:
            attempts += 1
            if await _page_has_profile(page):
                break
            try:
                new_code = await refresh_code()
            except Exception as exc:
                _log(f"[C-UI] refresh_code error={type(exc).__name__}")
                new_code = None
            new_code = re.sub(r"[\s-]+", "", str(new_code or ""))
            if new_code:
                active_code = new_code
                _log(f"[C-UI] polled code len={len(active_code)} attempts={attempts}")
                break

            if await _page_has_code(page):
                await _sleep(page, 1500)
                continue

            if await _page_has_email(page) and attempts in (3, 8, 14, 20):
                try:
                    await _wait_turnstile(page, log=log, timeout_s=16.0, label="email-retry")
                    info = await _turnstile_info(page)
                    if _should_inject_turnstile(turnstile_token):
                        await _inject_turnstile(page, turnstile_token)
                    if info.get("solved") or not info.get("present"):
                        try:
                            await page.evaluate(_FILL_EMAIL_JS, email)
                        except Exception:
                            pass
                        try:
                            clicked = await page.evaluate(_SUBMIT_EMAIL_JS)
                        except Exception:
                            clicked = ""
                        if not clicked:
                            clicked = await _click_submit(
                                page,
                                ["sign up", "signup", "continue", "继续", "next", "下一步", "提交", "注册"],
                            )
                        _log(
                            f"[C-UI] email resubmit attempt={attempts} click={clicked or '-'} "
                            f"cf_len={info.get('token_len') or 0}"
                        )
                    else:
                        _log(
                            f"[C-UI] email still blocked by unsolved CF "
                            f"len={info.get('token_len') or 0} attempt={attempts}"
                        )
                    try:
                        _log(f"[C-UI] diag={await _page_diag(page)}")
                    except Exception:
                        pass
                except Exception as exc:
                    _log(f"[C-UI] resubmit err={type(exc).__name__}")
            await _sleep(page, 1500)

        if not active_code and not await _page_has_code(page) and not await _page_has_profile(page):
            _log("[C-UI] no verification code from mailbox")
            try:
                _log(f"[C-UI] final_diag={await _page_diag(page)}")
            except Exception:
                pass
            return None

    deadline = asyncio.get_event_loop().time() + 45
    while asyncio.get_event_loop().time() < deadline:
        if await _page_has_code(page) or await _page_has_profile(page):
            break
        await _sleep(page, 500)

    if await _page_has_code(page):
        if not active_code:
            _log("[C-UI] code page ready but code empty")
            return None
        _log("[C-UI] fill code")
        filled_ok = False
        for _ in range(8):
            if await _fill_code(page, active_code):
                filled_ok = True
                break
            await _sleep(page, 700)
        if not filled_ok:
            _log("[C-UI] code input missing")
            return None
        if _should_inject_turnstile(turnstile_token):
            await _inject_turnstile(page, turnstile_token)
        else:
            await _wait_turnstile(page, log=log, timeout_s=12.0, label="code")
        clicked = await _click_submit(
            page,
            ["verify", "continue", "继续", "next", "确认", "验证", "submit", "code", "确认邮箱"],
        )
        if not clicked:
            try:
                clicked = await page.evaluate(
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
                clicked = ""
        _log(f"[C-UI] code submit={clicked or '-'}")
        await _sleep(page, 1500)

    deadline = asyncio.get_event_loop().time() + 60
    while asyncio.get_event_loop().time() < deadline:
        if await _page_has_profile(page):
            break
        if await _page_has_code(page) and active_code:
            await _fill_code(page, active_code)
            await _click_submit(page, ["verify", "continue", "继续", "next", "确认邮箱"])
        await _sleep(page, 700)
    if not await _page_has_profile(page):
        _log("[C-UI] profile form not ready")
        try:
            _log(f"[C-UI] final_diag={await _page_diag(page)}")
        except Exception:
            pass
        return None

    _log("[C-UI] fill profile")
    ok1 = await _set_input(
        page,
        'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]',
        given_name,
    )
    ok2 = await _set_input(
        page,
        'input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]',
        family_name,
    )
    ok3 = await _set_input(
        page,
        'input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]',
        password,
    )
    if not (ok1 and ok2 and ok3):
        _log(f"[C-UI] profile fields incomplete given={ok1} family={ok2} pass={ok3}")
        return None

    await _humanize(page)
    profile_cf_ok = True
    if _should_inject_turnstile(turnstile_token):
        await _inject_turnstile(page, turnstile_token)
        _log("[C-UI] injected turnstile token (forced)")
        profile_cf_ok = True
    else:
        if await _turnstile_crashed(page):
            await _recover_crashed_turnstile(page, log=log)
            await _set_input(
                page,
                'input[data-testid="givenName"], input[name="givenName"], input[autocomplete="given-name"]',
                given_name,
            )
            await _set_input(
                page,
                'input[data-testid="familyName"], input[name="familyName"], input[autocomplete="family-name"]',
                family_name,
            )
            await _set_input(
                page,
                'input[data-testid="password"], input[name="password"], input[type="password"], input[autocomplete="new-password"]',
                password,
            )
        profile_cf_ok = await _wait_turnstile(page, log=log, timeout_s=90.0, label="profile")
        if not profile_cf_ok and (os.environ.get("UI_TURNSTILE_HELPER") or "0").strip().lower() in (
            "1", "true", "yes", "on",
        ):
            profile_cf_ok = await _solve_turnstile_helper_widget(page, log=log, timeout_s=30.0)
        if not profile_cf_ok:
            info = await _turnstile_info(page)
            if info.get("present") and not info.get("solved"):
                _log(
                    f"[C-UI] profile blocked by unsolved turnstile len={info.get('token_len') or 0}"
                )
                return None

    try:
        submitted = await page.evaluate(
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
    clicked = submitted or await _click_submit(
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
    _log(f"[C-UI] profile submit={clicked or '-'}")

    sso = ""
    wait_deadline = asyncio.get_event_loop().time() + 60
    last_url = ""
    while asyncio.get_event_loop().time() < wait_deadline:
        sso, _ = await _get_sso(page)
        if sso and len(sso) >= 20:
            break
        url = ""
        try:
            url = page.url or ""
        except Exception:
            url = ""
        if url and url != last_url:
            last_url = url
            _log(f"[C-UI] url={url[:160]}")
        if int(asyncio.get_event_loop().time()) % 8 == 0:
            try:
                err = await page.evaluate(
                    """() => {
                      const nodes = Array.from(document.querySelectorAll('[role="alert"], .error, [data-testid*="error" i], p, span, div'))
                        .slice(0, 80);
                      const texts = nodes.map(n => (n.innerText || '').trim()).filter(Boolean);
                      const hit = texts.find(t => /error|invalid|failed|unable|密码|password|turnstile|castle|already|exists|denied|required/i.test(t));
                      return (hit || '').slice(0, 180);
                    }"""
                )
                if err:
                    _log(f"[C-UI] page_error={err}")
            except Exception:
                pass
        if "grok.com" in (url or "") and "sign-up" not in (url or ""):
            await _sleep(page, 800)
            sso, _ = await _get_sso(page)
            if sso:
                break
        if "accounts.x.ai" in (url or "") and int(asyncio.get_event_loop().time()) % 9 == 0:
            if _should_inject_turnstile(turnstile_token):
                await _inject_turnstile(page, turnstile_token)
            else:
                await _wait_turnstile(page, log=log, timeout_s=25.0, label="profile-retry")
            try:
                await page.evaluate(
                    """() => {
                      const form = document.querySelector('form');
                      if (form && typeof form.requestSubmit === 'function') form.requestSubmit();
                    }"""
                )
            except Exception:
                await _click_submit(
                    page,
                    ["完成注册", "创建账户", "signup", "continue", "继续", "complete"],
                )
        await _sleep(page, 1000)

    sso, _ = await _get_sso(page)
    if sso:
        _log(f"[C-UI] sso_len={len(sso)}")
        return sso
    try:
        _log(f"[C-UI] sso missing final_url={(page.url or '')[:180]}")
    except Exception:
        _log("[C-UI] sso missing")
    return None




