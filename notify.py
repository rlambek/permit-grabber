"""Send a notification email through the Gmail OAuth token created by scan_alerts.py.

The `gmail.modify` scope already includes sending, so no re-auth is needed.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

TOKEN_PATH = Path("token.json")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def send_self_email(subject: str, body: str) -> None:
    """Send `subject`/`body` to the authenticated Gmail user's own address.

    Raises if token.json is missing or the API call fails — caller decides whether to
    surface or swallow the error.
    """
    if not TOKEN_PATH.exists():
        raise RuntimeError("token.json not found — run scan_alerts.py once to authorize Gmail.")
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    # google-auth refreshes in-memory inside .execute(), but doesn't write the new token
    # back to disk. Persist it so future runs don't drift out of sync.
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    addr = profile["emailAddress"]
    msg = MIMEText(body)
    msg["to"] = addr
    msg["from"] = addr
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
