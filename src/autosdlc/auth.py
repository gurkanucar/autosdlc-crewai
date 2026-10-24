"""Optional shared-password protection for the web UI.

The password comes from the APP_PASSWORD environment variable. When it is empty or unset the UI stays open.
"""

from __future__ import annotations

import hmac
import os


def configured_password() -> str:
    return os.getenv("APP_PASSWORD", "")


def auth_enabled() -> bool:
    return bool(configured_password())


def password_matches(candidate: str) -> bool:
    """Constant-time comparison; always False when no password is configured."""
    expected = configured_password()
    return bool(expected) and hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))
