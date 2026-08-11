"""诊断 fetch_config：打开 x.ai sign-up 页面，检查页面结构与提取结果。"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")


async def main() -> None:
    from grok_register.browser_backend import launch_browser_bundle  # type: ignore

    proxy = os.environ.get("BROWSER_PROXY") or "http://127.0.0.1:7890"
    os.environ["BROWSER_PROXY"] = proxy

    async with launch_browser_bundle() as bundle:
        browser = bundle.browser
        page = await browser.new_page()
        await page.goto("https://accounts.x.ai/sign-up?redirect=grok-com", timeout=60000)
        await page.wait_for_timeout(8000)
        html = await page.content()
        title = await page.title()
        url = page.url

        print(f"url={url}")
        print(f"title={title!r}")
        print(f"html_len={len(html)}")
        for kw in ("turnstile", "challenge", "captcha", "sign-up", "email", "verify", "0x4"):
            print(f"contains {kw!r}: {kw in html.lower()}")

        m = re.search(r'0x4AAAAAAA[a-zA-Z0-9_-]+', html)
        print(f"SITE_KEY match: {m.group(0) if m else None}")

        state_tree = None
        for chunk in re.findall(r'self\.__next_f\.push\(\[1,"(.*?)"\]\)', html, re.DOTALL):
            if 'sign-up' not in chunk:
                continue
            decoded = chunk.replace('\\"', '"')
            f_match = re.search(r'"f":\[\[\[', decoded)
            if not f_match:
                continue
            f_start = f_match.start() + 5
            end_idx = decoded.find('"$undefined"', f_start)
            if end_idx < 0:
                continue
            state_tree = decoded[f_start:end_idx].replace('\\\\"', '"').replace('\\', '')[:60]
            break
        print(f"STATE_TREE match: {state_tree}")

        js_urls = re.findall(r'src="(/_next/static/[^"]+\.js[^"]*)"', html)
        print(f"js_urls count: {len(js_urls)}")
        action_id = None
        for js_url in js_urls[:50]:
            try:
                js = await page.evaluate(
                    f"(async()=>{{return await fetch('{js_url}').then(r=>r.text()).catch(()=>\"\")}})()"
                )
                if not js:
                    continue
                if not any(kw in js for kw in ['createUser', 'registerUser', 'emailValidation']):
                    continue
                hexes = re.findall(r'[a-fA-F0-9]{40,50}', js)
                if hexes:
                    action_id = hexes[0]
                    print(f"ACTION_ID from {js_url}")
                    break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"js fetch error {js_url}: {exc}")
        print(f"ACTION_ID match: {action_id}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
