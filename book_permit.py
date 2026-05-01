"""Drive recreation.gov to the cart for a permit/date, then stop for a human to pay.

Recreation.gov ToS likely prohibits automated booking. This is for personal/educational use;
the script intentionally stops before payment so a human commits the final action.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeout, sync_playwright
from playwright_stealth import Stealth

from auth_store import clear_credentials, get_credentials, store_credentials
import notify

AUTH_STATE = Path("auth.json")
REQUIRED_FIELDS = ("permit_name", "permit_url", "date", "group_size")

# A small rotation of recent Chrome UAs. Matching the User-Agent the website itself
# expects avoids the most obvious "automation tool" fingerprint without doing anything
# evasive — same approach camply uses for its API calls.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
]

# Recreation.gov sniffs `navigator.webdriver` and the `AutomationControlled` blink feature
# to flag Playwright as automation. These two patches together pass the basic checks.
LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled"]
STEALTH_INIT_SCRIPT = "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"

PERMIT_ID_RE = re.compile(r"/permits/(\d+)")


def load_alert(args) -> dict:
    if args.alert_stdin:
        raw = sys.stdin.read().strip()
    elif args.alert:
        raw = args.alert
    else:
        sys.exit("error: pass --alert '<json>' or --alert-stdin")
    try:
        alert = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"error: alert is not valid JSON ({e})")
    missing = [f for f in REQUIRED_FIELDS if not alert.get(f)]
    if missing:
        sys.exit(f"error: alert is missing required fields: {missing}")
    try:
        date.fromisoformat(alert["date"])
    except ValueError:
        sys.exit(f"error: alert.date {alert['date']!r} is not ISO YYYY-MM-DD")
    return alert


def login_if_needed(page: Page):
    # If already authenticated, the header will show an account menu instead of "Log In".
    if page.get_by_role("button", name="Log In").count() == 0:
        return

    email, password = get_credentials()
    if not email or not password:
        sys.exit("error: no recreation.gov credentials found. Run: python book_permit.py --store-creds")

    page.get_by_role("button", name="Log In").first.click()
    # Selectors confirmed via rmccrystal/recreation-gov-bot, which is a working Selenium bot.
    page.locator("#email").fill(email)
    page.locator("#rec-acct-sign-in-password").fill(password)
    page.locator("#rec-acct-sign-in-password").press("Enter")

    # Wait for either the modal to close or an MFA/CAPTCHA prompt.
    try:
        page.wait_for_selector("text=Verification", timeout=4000)
        input("MFA/CAPTCHA detected. Complete it in the browser, then press Enter here to continue...")
    except PlaywrightTimeout:
        pass

    # Login is complete when the password field is removed from the DOM. networkidle is
    # unreliable on recreation.gov — the SPA does background polling that never settles.
    try:
        page.locator("#rec-acct-sign-in-password").wait_for(state="detached", timeout=15000)
    except PlaywrightTimeout:
        pass


def select_segment(page: Page, segment: str | None):
    """Pick a permit segment/division if the page has the control.

    Recreation.gov uses a native <select id="division-selection"> for river permits.
    Some permit types may render a custom combobox instead — handled as a fallback.
    """
    if not segment:
        return
    elem = page.locator("#division-selection")
    if elem.count() == 0:
        return

    tag = elem.evaluate("el => el.tagName")
    if tag == "SELECT":
        try:
            elem.select_option(label=segment)
            return
        except Exception:
            # select_option raises Playwright's Error class (not TimeoutError) on no-match,
            # so we catch broadly and fall through to substring matching.
            pass
        options = elem.locator("option").all_inner_texts()
        for opt in options:
            if segment.lower() in opt.lower():
                elem.select_option(label=opt)
                return
        raise RuntimeError(f"No segment option matched {segment!r}. Available: {options}")

    elem.click()
    page.get_by_text(segment).first.click()


_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_MONTHS = ["January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December"]


def select_date(page: Page, iso_date: str):
    target = date.fromisoformat(iso_date)
    weekday = _WEEKDAYS[target.weekday()]
    month = _MONTHS[target.month - 1]
    # Recreation.gov permit pages use an inline calendar grid — no date <input>, no picker
    # trigger button. We advance the visible month, then click the day cell. The image
    # gallery's nav buttons use specific aria-labels ("Image N of M..."), so a plain "Next"
    # match reliably hits the calendar's nav button.
    target_header = f"{month} {target.year}"
    for _ in range(24):
        if page.get_by_text(target_header, exact=True).count() > 0:
            break
        next_btn = page.get_by_role("button", name="Next")
        if next_btn.count() == 0:
            break
        next_btn.first.click()
        page.wait_for_timeout(250)

    # Day buttons have aria-labels like "Sunday, August 16, 2026 - 5 left" or
    # "...- Unavailable" or "Today, ...". Match the date prefix and ignore the suffix.
    # Hardcoded English weekday/month avoids Windows locale surprises.
    day_pattern = re.compile(rf"^(?:Today, )?{weekday}, {month} {target.day}, {target.year}\b")
    cell = page.get_by_role("button", name=day_pattern)
    cell.first.click()


def set_group_size(page: Page, group_size: int):
    """Fill a group-size input if the page has one.

    Recreation.gov uses several patterns: a native <input>, a React-Aria NumberField
    (a role=group div wrapping a hidden/styled input), or a custom +/- spinbutton.
    We try in order: native fill, then drill into the wrapper for an inner input, then
    a spinbutton role. If none work, warn — the slot is already held; the human can
    enter the size in the form.
    """
    if group_size <= 1:
        return
    labels = ("Group Size", "Number of People", "Party Size", "Number of Participants",
              "Party size", "Group size", "Total in Party", "Number of guests",
              "Total Group Size")
    for label in labels:
        field = page.get_by_label(label)
        if field.count() == 0:
            continue
        # Strategy 1: native input that .fill() can handle directly.
        try:
            field.first.fill(str(group_size))
            return
        except Exception:
            pass
        # Strategy 2: NumberField wrapper — actual input lives inside.
        inner = field.first.locator("input").first
        if inner.count() > 0:
            try:
                inner.fill(str(group_size))
                return
            except Exception:
                pass
        # Strategy 3: spinbutton role inside the wrapper.
        spin = field.first.get_by_role("spinbutton")
        if spin.count() > 0:
            try:
                spin.first.fill(str(group_size))
                return
            except Exception:
                pass
    print(
        f"set_group_size: could not auto-fill a group-size field; group_size={group_size} "
        "must be entered manually in the reservation form."
    )


def click_book(page: Page):
    for label in ("Book Now", "Reserve", "Continue", "Add to Cart"):
        btn = page.get_by_role("button", name=label)
        if btn.count() > 0 and btn.first.is_enabled():
            btn.first.click()
            # networkidle never settles on recreation.gov. Wait for the cart/checkout URL
            # specifically so page.url is the cart link before we email it.
            try:
                page.wait_for_url(re.compile(r"/(cart|checkout|reservation)"), timeout=15000)
            except PlaywrightTimeout:
                page.wait_for_load_state("domcontentloaded")
            return
    raise RuntimeError("could not find a Book/Reserve/Continue button on the page")


def precheck_availability(permit_url: str, iso_date: str) -> bool | None:
    """Hit the public recreation.gov availability API to see if the date is still open.

    Returns True if the date appears available, False if confirmed unavailable, or None
    if we couldn't tell (unknown URL shape, network error, schema drift). Callers should
    treat None as "proceed anyway" — the precheck is a cheap fast-fail, not a gate.
    """
    match = PERMIT_ID_RE.search(permit_url)
    if not match:
        return None
    permit_id = match.group(1)
    target = date.fromisoformat(iso_date)
    month_start = target.replace(day=1).strftime("%Y-%m-%dT00:00:00.000Z")
    api_url = (
        f"https://www.recreation.gov/api/permits/{permit_id}/availability/month"
        f"?start_date={month_start}"
    )
    req = urllib.request.Request(api_url, headers={"User-Agent": random.choice(USER_AGENTS), "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError):
        return None

    # The payload shape varies by permit type. Look for any "remaining > 0" entry on the
    # target date across any division. If we can't find a date-keyed structure at all,
    # bail out (None) rather than guess.
    iso = target.isoformat()
    divisions = payload.get("payload", {}).get("availability", {}) or payload.get("availability", {})
    if not isinstance(divisions, dict) or not divisions:
        return None
    for div in divisions.values():
        date_map = (div or {}).get("date_availability") or {}
        for key, entry in date_map.items():
            if key.startswith(iso) and isinstance(entry, dict):
                remaining = entry.get("remaining")
                if isinstance(remaining, (int, float)) and remaining > 0:
                    return True
    return False


def notify_cart_ready(alert: dict, current_url: str) -> None:
    subject = f"Permit ready to book: {alert['permit_name']} - {alert['date']}"
    body = (
        f"The booking script reached the recreation.gov reservation page for:\n\n"
        f"  Permit:     {alert['permit_name']}\n"
        f"  Segment:    {alert.get('segment', '(none)')}\n"
        f"  Date:       {alert['date']}\n"
        f"  Group size: {alert['group_size']} (enter in the form)\n\n"
        f"Page URL: {current_url}\n\n"
        f"Complete the participant list, watercraft, and any other required fields, then\n"
        f"submit and pay. The browser window is open on the local machine for ~15 minutes."
    )
    try:
        notify.send_self_email(subject, body)
        print("notify: emailed you about the cart")
    except Exception as exc:
        print(f"notify: failed to send email ({exc})")


def run(alert: dict, headless: bool, skip_precheck: bool, unattended: bool):
    if not skip_precheck:
        available = precheck_availability(alert["permit_url"], alert["date"])
        if available is False:
            print(f"precheck: {alert['permit_name']} on {alert['date']} appears unavailable — aborting before launching browser")
            return
        if available is None:
            print("precheck: could not determine availability, proceeding anyway")
        else:
            print("precheck: availability confirmed, launching browser")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, args=LAUNCH_ARGS)
        # Realistic context: US English, Mountain time (Yampa is in CO), explicit
        # Accept-Language. Skip an explicit viewport — Playwright's default plays nicely
        # with the user's actual monitor and matches the working configuration we tested.
        context_kwargs = {
            "user_agent": random.choice(USER_AGENTS),
            "locale": "en-US",
            "timezone_id": "America/Denver",
            "extra_http_headers": {"Accept-Language": "en-US,en;q=0.9"},
        }
        if AUTH_STATE.exists():
            context_kwargs["storage_state"] = str(AUTH_STATE)
        context = browser.new_context(**context_kwargs)
        # playwright-stealth patches ~20 detection vectors (navigator.plugins/languages,
        # WebGL vendor, chrome.runtime, etc.) — superset of the manual init script below.
        Stealth().apply_stealth_sync(context)
        context.add_init_script(STEALTH_INIT_SCRIPT)
        page = context.new_page()

        page.goto(alert["permit_url"], wait_until="domcontentloaded")
        login_if_needed(page)

        if not AUTH_STATE.exists():
            context.storage_state(path=str(AUTH_STATE))

        # Some permits redirect to a login page after auth — re-navigate to be safe.
        if "/permits/" not in page.url:
            page.goto(alert["permit_url"], wait_until="domcontentloaded")

        select_segment(page, alert.get("segment"))
        select_date(page, alert["date"])
        # Click "Book Now" on the permit page — navigates to the reservation form. For
        # permits with a simple party-count input the next call sets it; for participant-
        # list permits (river/hunting) it warns and the human fills the form.
        click_book(page)
        page.wait_for_timeout(2000)
        set_group_size(page, int(alert["group_size"]))

        print(f"READY FOR HUMAN — complete reservation form for {alert['permit_name']} on {alert['date']}")
        print(f"alert received: {alert.get('alert_received_at', 'unknown')}")
        print(f"current page: {page.url}")
        notify_cart_ready(alert, page.url)

        if unattended:
            # Recreation.gov holds carts for ~15 minutes. Sleep just under that so the browser
            # window stays open long enough for a human to act on the email.
            print("unattended mode: holding browser for 14 minutes, then closing")
            try:
                import time
                time.sleep(14 * 60)
            except KeyboardInterrupt:
                pass
        else:
            input("Press Enter to close the browser when done...")

        context.close()
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--alert", help="Alert payload as a JSON string")
    parser.add_argument("--alert-stdin", action="store_true", help="Read JSON alert from stdin")
    parser.add_argument("--headless", action="store_true", help="Run browser hidden (default: headed)")
    parser.add_argument("--no-precheck", action="store_true", help="Skip the public-API availability check")
    parser.add_argument("--unattended", action="store_true", help="Don't wait for Enter; hold browser 14 min then exit")
    parser.add_argument("--store-creds", action="store_true", help="Prompt for and store recreation.gov credentials in the OS keyring")
    parser.add_argument("--clear-creds", action="store_true", help="Remove stored recreation.gov credentials")
    args = parser.parse_args()

    if args.store_creds:
        store_credentials()
        return
    if args.clear_creds:
        clear_credentials()
        return

    if not (args.alert or args.alert_stdin):
        parser.error("one of --alert, --alert-stdin, --store-creds, or --clear-creds is required")

    alert = load_alert(args)
    run(alert, headless=args.headless, skip_precheck=args.no_precheck, unattended=args.unattended)


if __name__ == "__main__":
    main()
