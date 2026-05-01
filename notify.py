"""Send a notification email through the Gmail OAuth token created by scan_alerts.py.

The `gmail.modify` scope already includes sending, so no re-auth is needed.
"""

from __future__ import annotations

import base64
from email.mime.text import MIMEText
from pathlib import Path

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
    service = build("gmail", "v1", credentials=creds)
    profile = service.users().getProfile(userId="me").execute()
    addr = profile["emailAddress"]
    msg = MIMEText(body)
    msg["to"] = addr
    msg["from"] = addr
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
