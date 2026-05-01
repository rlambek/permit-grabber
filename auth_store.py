"""Recreation.gov credential storage backed by the OS-native credential store.

On Windows this writes to Windows Credential Manager (the same store `gh` uses).
On macOS it writes to Keychain. On Linux it uses the Secret Service API.
"""

from __future__ import annotations

import getpass
import os

import keyring
import keyring.errors

SERVICE = "permit-grabber"
EMAIL_KEY = "recgov-email"
PASSWORD_KEY = "recgov-password"


def get_credentials() -> tuple[str, str]:
    """Return (email, password). Env vars take precedence; falls back to the OS keyring."""
    email = os.environ.get("RECGOV_EMAIL") or keyring.get_password(SERVICE, EMAIL_KEY) or ""
    password = os.environ.get("RECGOV_PASSWORD") or keyring.get_password(SERVICE, PASSWORD_KEY) or ""
    return email, password


def store_credentials() -> None:
    email = input("recreation.gov email: ").strip()
    if not email:
        raise SystemExit("error: email is required")
    password = getpass.getpass("recreation.gov password: ")
    if not password:
        raise SystemExit("error: password is required")
    keyring.set_password(SERVICE, EMAIL_KEY, email)
    keyring.set_password(SERVICE, PASSWORD_KEY, password)
    print(f"Stored credentials for {email} in OS keyring (service={SERVICE!r}).")


def clear_credentials() -> None:
    for key in (EMAIL_KEY, PASSWORD_KEY):
        try:
            keyring.delete_password(SERVICE, key)
        except keyring.errors.PasswordDeleteError:
            pass
    print("Cleared stored credentials.")
