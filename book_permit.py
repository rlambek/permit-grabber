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

AUTH_STATE = Path("auth.json")
REQUIRED_FIELDS = ("permit_name", "permit_url", "date", "group_size")

# A small rotation of recent Chrome UAs. Matching the User-Agent the website itself
# expects avoids the most obvious "automation tool" fingerprint without doing anything
# evasive — same approach camply uses for its API calls.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

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
    email = os.environ.get("RECGOV_EMAIL")
    password = os.environ.get("RECGOV_PASSWORD")
    if not email or not password:
        sys.exit("error: set RECGOV_EMAIL and RECGOV_PASSWORD environment variables")

    # If already authenticated, the header will show an account menu instead of "Log In".
    if page.get_by_role("button", name="Log In").count() == 0:
        return

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

    page.wait_for_load_state("networkidle")


def select_date(page: Page, iso_date: str):
    target = date.fromisoformat(iso_date)
    # Strategy 1: a plain date input.
    date_input = page.locator('input[type="date"], input[aria-label*="date" i]').first
    if date_input.count() > 0:
        try:
            date_input.fill(iso_date)
            return
        except PlaywrightTimeout:
            pass

    # Strategy 2: open a date-picker trigger and click the day cell.
    # TODO: verify selector — name varies by permit page ("Select Date", "Start Date", etc).
    for trigger_name in ("Start Date", "Select Date", "Date"):
        trigger = page.get_by_role("button", name=trigger_name)
        if trigger.count() > 0:
            trigger.first.click()
            break

    # Advance the calendar header until the displayed month matches our target.
    target_header = target.strftime("%B %Y")
    for _ in range(24):
        if page.get_by_text(target_header, exact=True).count() > 0:
            break
        next_btn = page.get_by_role("button", name="Next Month")
        if next_btn.count() == 0:
            break
        next_btn.first.click()

    # Day cells on recreation.gov calendars are usually buttons with an aria-label like
    # "Tuesday, July 15, 2026". Fall back to the day-of-month text if that fails.
    aria_label = target.strftime("%A, %B %-d, %Y") if sys.platform != "win32" else target.strftime("%A, %B %#d, %Y")
    cell = page.get_by_role("button", name=aria_label)
    if cell.count() == 0:
        cell = page.get_by_role("button", name=str(target.day), exact=True)
    cell.first.click()


def set_group_size(page: Page, group_size: int):
    if group_size <= 1:
        return
    # TODO: verify selector — group-size inputs vary by permit type.
    for label in ("Group Size", "Number of People", "Party Size"):
        field = page.get_by_label(label)
        if field.count() > 0:
            field.first.fill(str(group_size))
            return


def click_book(page: Page):
    for label in ("Book Now", "Reserve", "Continue", "Add to Cart"):
        btn = page.get_by_role("button", name=label)
        if btn.count() > 0 and btn.first.is_enabled():
            btn.first.click()
            page.wait_for_load_state("networkidle")
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


def run(alert: dict, headless: bool, skip_precheck: bool):
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
        browser = p.chromium.launch(headless=headless)
        context_kwargs = {"user_agent": random.choice(USER_AGENTS)}
        if AUTH_STATE.exists():
            context_kwargs["storage_state"] = str(AUTH_STATE)
        context = browser.new_context(**context_kwargs)
        page = context.new_page()

        page.goto(alert["permit_url"], wait_until="domcontentloaded")
        login_if_needed(page)

        if not AUTH_STATE.exists():
            context.storage_state(path=str(AUTH_STATE))

        # Some permits redirect to a login page after auth — re-navigate to be safe.
        if "/permits/" not in page.url:
            page.goto(alert["permit_url"], wait_until="domcontentloaded")

        select_date(page, alert["date"])
        set_group_size(page, int(alert["group_size"]))
        click_book(page)

        print(f"READY FOR HUMAN — review cart and confirm payment for {alert['permit_name']} on {alert['date']}")
        print(f"alert received: {alert.get('alert_received_at', 'unknown')}")
        print(f"current page: {page.url}")
        input("Press Enter to close the browser when done...")

        context.close()
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--alert", help="Alert payload as a JSON string")
    src.add_argument("--alert-stdin", action="store_true", help="Read JSON alert from stdin")
    parser.add_argument("--headless", action="store_true", help="Run browser hidden (default: headed)")
    parser.add_argument("--no-precheck", action="store_true", help="Skip the public-API availability check")
    args = parser.parse_args()

    alert = load_alert(args)
    run(alert, headless=args.headless, skip_precheck=args.no_precheck)


if __name__ == "__main__":
    main()
