"""Utility functions for Tracepoint."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def mask_email(email: str) -> str:
    """Mask email for privacy-safe logs (e.g., j***e@domain.com)."""
    if not email or "@" not in email:
        return "[MASKED_EMAIL]"
    parts = email.split("@", 1)
    user, domain = parts[0], parts[1]
    if len(user) <= 2:
        masked_user = user[0] + "***"
    else:
        masked_user = user[0] + "***" + user[-1]
    return f"{masked_user}@{domain}"


def mask_phone(phone: str) -> str:
    """Mask phone number for privacy-safe logs."""
    if not phone or len(phone) < 4:
        return "[MASKED_PHONE]"
    return phone[:3] + " ***-*** " + phone[-2:]


def mask_name(name: str) -> str:
    """Mask personal name for privacy-safe logs (e.g. J*** D***)."""
    if not name:
        return "[MASKED_NAME]"
    tokens = name.split()
    masked_tokens = [f"{t[0]}***" for t in tokens if t]
    return " ".join(masked_tokens)


def parse_iso8601(timestamp_str: str) -> datetime:
    """Parse ISO-8601 string into timezone-aware datetime."""
    # Replace 'Z' with '+00:00' if needed
    cleaned = timestamp_str.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
