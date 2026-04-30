"""Scan Gmail for Campflare permit-found alerts and emit a JSON booking payload.

Usage:
    python scan_alerts.py                # one-shot scan, prints JSON to stdout
    python scan_alerts.py --watch 60     # poll every 60s, print JSON when found
    python scan_alerts.py | python book_permit.py --alert-stdin

First run opens a browser for Google OAuth and writes token.json next to credentials.json.
Processed alerts get the Gmail label "campflare-processed" so they aren't re-emitted.
"""

from __future__ import annotations

import argparse
import base64
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


# Format inferred from the observed Campflare confirmation email body, which uses
# "Permit: <name>" and "Start Date(s): <iso list>". We assume the actual "found" alert
# follows the same convention and includes a recreation.gov URL — the URL bit is a guess.
PERMIT_NAME_RE = re.compile(r"^\s*Permit:\s*(.+?)\s*$", re.MULTILINE)
DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
RECGOV_URL_RE = re.compile(r"https?://(?:www\.)?recreation\.gov/[^\s)>\]]+")


def parse_alert(subject: str, body: str, received_iso: str) -> dict | None:
    permit_match = PERMIT_NAME_RE.search(body)
    permit_name = permit_match.group(1).strip() if permit_match else subject.replace("Campflare Permit Alert", "").strip(" -:")
    if not permit_name:
        return None

    dates = DATE_RE.findall(body)
    if not dates:
        return None

    url_match = RECGOV_URL_RE.search(body)
    permit_url = url_match.group(0) if url_match else lookup_permit_url(permit_name)
    if not permit_url:
        # Without a permit URL we can't drive the booking script. Surface a clear payload anyway
        # so the operator can see what was missing.
        permit_url = ""

    group_size = lookup_group_size(permit_name)

    return {
        "permit_name": permit_name,
        "permit_url": permit_url,
        "date": dates[0],
        "candidate_dates": dates,
        "group_size": group_size,
        "alert_received_at": received_iso,
    }


def _load_permit_config() -> dict:
    if PERMIT_CONFIG_PATH.exists():
        return json.loads(PERMIT_CONFIG_PATH.read_text())
    return {}


def lookup_permit_url(permit_name: str) -> str:
    cfg = _load_permit_config().get(permit_name, {})
    return cfg.get("permit_url", "")


def lookup_group_size(permit_name: str) -> int:
    cfg = _load_permit_config().get(permit_name, {})
    return int(cfg.get("group_size", 1))


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
