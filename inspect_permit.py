"""Dump button labels + segment-related markup on a permit page so we can write real selectors."""

import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

PERMIT_URL = sys.argv[1] if len(sys.argv) > 1 else "https://www.recreation.gov/permits/250014"
AUTH_STATE = Path("auth.json")
KEYWORDS = ("Segment", "Deerlodge", "Division", "Zone", "Where", "Select", "Choose")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
    ctx_kwargs = {"user_agent": UA}
    if AUTH_STATE.exists():
        ctx_kwargs["storage_state"] = str(AUTH_STATE)
    ctx = browser.new_context(**ctx_kwargs)
    ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    page = ctx.new_page()
    page.goto(PERMIT_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(6000)

    page.screenshot(path="permit_page.png", full_page=True)
    print("Saved screenshot to permit_page.png\n")

    print("=== ALL VISIBLE BUTTONS ===")
    seen = set()
    for handle in page.locator("button, [role='button']").element_handles():
        try:
            if not handle.is_visible():
                continue
            text = (handle.inner_text() or "").strip()
            aria = handle.get_attribute("aria-label") or ""
            key = (text, aria)
            if key in seen or (not text and not aria):
                continue
            seen.add(key)
            print(f"  text={text!r}  aria-label={aria!r}")
        except Exception:
            pass

    print("\n=== KEYWORD MATCHES ===")
    for kw in KEYWORDS:
        for handle in page.get_by_text(kw, exact=False).element_handles()[:3]:
            try:
                if not handle.is_visible():
                    continue
                tag = handle.evaluate("el => el.tagName")
                outer = handle.evaluate("el => el.outerHTML")[:250]
                print(f"  [{kw}] <{tag}> {outer}")
            except Exception:
                pass

    print("\nBrowser stays open. Look at the page, find the segment control, then press Enter here.")
    input()
    browser.close()
