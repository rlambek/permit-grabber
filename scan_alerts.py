"""Scan Gmail for Campflare permit-found alerts and emit a JSON booking payload.

Usage:
    python scan_alerts.py                # one-shot scan, prints JSON to stdout
    python scan_alerts.py --watch 60     # poll every 60s, print JSON when found
    python scan_alerts.py | python book_permit.py --alert-stdin

First run opens a browser for Google OAuth and writes token.json next to credentials.json.
EVERY Campflare email gets the Gmail label "campflare-processed" so we never re-attempt
the same one — even if it doesn't trigger a booking. Booking fires only when the body
contains a recreation.gov permit URL listed in permit_config.json.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
PROCESSED_LABEL = "campflare-processed"

CREDENTIALS_PATH = Path(os.environ.get("GMAIL_CREDENTIALS", "credentials.json"))
TOKEN_PATH = Path(os.environ.get("GMAIL_TOKEN", "token.json"))
PERMIT_CONFIG_PATH = Path(os.environ.get("PERMIT_CONFIG", "permit_config.json"))


def get_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds)


def ensure_label(service, name: str) -> str:
    labels = service.users().labels().list(userId="me").execute().get("labels", [])
    for lbl in labels:
        if lbl["name"] == name:
            return lbl["id"]
    created = service.users().labels().create(
        userId="me",
        body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
    ).execute()
    return created["id"]


def search_alerts(service):
    # Exclude the subscription-confirmation emails and anything we've already processed.
    query = (
        f'from:no-reply@campflare.com '
        f'-subject:"Confirmation" '
        f'-label:{PROCESSED_LABEL}'
    )
    resp = service.users().messages().list(userId="me", q=query, maxResults=10).execute()
    return resp.get("messages", [])


def get_message_full(service, message_id: str) -> dict:
    return service.users().messages().get(userId="me", id=message_id, format="full").execute()


def extract_plaintext(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    for part in payload.get("parts", []) or []:
        text = extract_plaintext(part)
        if text:
            return text
    return ""


def header(payload: dict, name: str) -> str:
    for h in payload.get("headers", []):
        if h["name"].lower() == name.lower():
            return h["value"]
    return ""


# Date extraction: try ISO YYYY-MM-DD first (the format Campflare's confirmation emails
# use), then fall back to "Month DayTH" with year inferred from the email's received date.
ISO_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
VERBOSE_DATE_RE = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2})(?:st|nd|rd|th)?\b",
    re.IGNORECASE,
)


def _load_permit_config() -> dict:
    if PERMIT_CONFIG_PATH.exists():
        return json.loads(PERMIT_CONFIG_PATH.read_text())
    return {}


def extract_date(body: str, email_received_iso: str) -> str | None:
    iso_matches = ISO_DATE_RE.findall(body)
    if iso_matches:
        return iso_matches[0]

    try:
        email_date = dt.date.fromisoformat(email_received_iso[:10])
    except ValueError:
        email_date = dt.date.today()

    for month_name, day_str in VERBOSE_DATE_RE.findall(body):
        try:
            month_num = dt.datetime.strptime(month_name.title(), "%B").month
            day = int(day_str)
            candidate = dt.date(email_date.year, month_num, day)
            if candidate < email_date:
                candidate = candidate.replace(year=email_date.year + 1)
            return candidate.isoformat()
        except ValueError:
            continue
    return None


def parse_alert(subject: str, body: str, received_iso: str) -> dict | None:
    """Return a booking payload if the body matches a configured permit URL, else None."""
    config = _load_permit_config()
    matched_url = next((url for url in config if url in body), None)
    if not matched_url:
        return None

    date_str = extract_date(body, received_iso)
    if not date_str:
        # Recognised permit but no parseable date — the booker can't proceed without one.
        # Caller will still mark this email processed; user can inspect manually.
        return None

    cfg = config[matched_url]
    return {
        "permit_name": cfg.get("name", subject),
        "permit_url": matched_url,
        "segment": cfg.get("segment"),
        "date": date_str,
        "group_size": int(cfg.get("group_size", 1)),
        "alert_received_at": received_iso,
    }


def mark_processed(service, message_id: str, label_id: str):
    service.users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]}
    ).execute()


def scan_once(service, label_id: str) -> list[dict]:
    payloads = []
    for msg_meta in search_alerts(service):
        msg = get_message_full(service, msg_meta["id"])
        payload = msg["payload"]
        subject = header(payload, "Subject")
        date_hdr = header(payload, "Date")
        try:
            received_iso = datetime.strptime(date_hdr, "%a, %d %b %Y %H:%M:%S %z").astimezone(timezone.utc).isoformat()
        except ValueError:
            received_iso = datetime.now(timezone.utc).isoformat()
        body = extract_plaintext(payload)
        parsed = parse_alert(subject, body, received_iso)
        if parsed:
            payloads.append(parsed)
        # Always mark processed — even if the alert wasn't bookable. Otherwise we'd
        # re-fetch and re-attempt the same email every poll cycle indefinitely.
        mark_processed(service, msg_meta["id"], label_id)
    return payloads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", type=int, metavar="SECONDS", help="Poll every N seconds instead of one-shot")
    args = parser.parse_args()

    service = get_service()
    label_id = ensure_label(service, PROCESSED_LABEL)

    if args.watch:
        while True:
            for payload in scan_once(service, label_id):
                print(json.dumps(payload), flush=True)
            time.sleep(args.watch)
    else:
        payloads = scan_once(service, label_id)
        if not payloads:
            print("[]", file=sys.stderr)
            return 0
        for payload in payloads:
            print(json.dumps(payload))
        return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
