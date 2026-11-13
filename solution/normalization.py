"""
Type normalization and validation routines for leads and events.
Implements exact business rules for headers, phones, dates, decimals, enums, and timestamps.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Optional, Tuple

IST_OFFSET = timedelta(hours=5, minutes=30)
IST = timezone(IST_OFFSET)


def normalize_header(header: str) -> str:
    """
    Lower-case, trim, and replace every run of '_' or whitespace with a single space.
    """
    cleaned = header.strip().lower()
    return re.sub(r"[\s_]+", " ", cleaned)


def normalize_phone(raw: str) -> Tuple[Optional[str], bool]:
    """
    Indian mobile number normalization per specification:
    1. Strip all non-digit characters.
    2. If 12 digits starting with '91', drop '91'.
    3. If 11 digits starting with '0', drop '0'.
    4. Valid only if exactly 10 digits starting with 6, 7, 8, or 9.
    5. Canonical format: +91XXXXXXXXXX.

    Returns: (canonical_phone, is_valid)
    If empty or whitespace: returns (None, False)
    """
    if not raw or not raw.strip():
        return None, False

    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]

    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}", True

    return None, False


def mask_phone(canonical_phone: str) -> str:
    """
    Mask canonical phone +91XXXXXXXXXX to +91******XXXX (last 4 digits visible).
    """
    if not canonical_phone or len(canonical_phone) < 13:
        return canonical_phone
    return f"+91******{canonical_phone[-4:]}"


# Supported date patterns (day-first)
DATE_PATTERNS = [
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%d-%m-%Y",
    "%d-%b-%Y",
]


def parse_date(raw: str) -> Tuple[Optional[str], bool]:
    """
    Parses date in YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, or DD-Mon-YYYY (case-insensitive).
    Dates are day-first. Rejects impossible dates (e.g. 31/02).
    Returns (iso_date_string YYYY-MM-DD, is_valid).
    """
    if not raw or not raw.strip():
        return None, False

    cleaned = raw.strip()

    # Try standard patterns
    for fmt in DATE_PATTERNS:
        try:
            parsed = datetime.strptime(cleaned, fmt)
            # Valid date: format as canonical YYYY-MM-DD
            return parsed.strftime("%Y-%m-%d"), True
        except ValueError:
            continue

    # Try case-insensitive month names for DD-Mon-YYYY (e.g. 29-dec-2026 or 29-DEC-2026)
    match_mon = re.match(r"^(\d{1,2})-([A-Za-z]{3})-(\d{4})$", cleaned)
    if match_mon:
        day_str, mon_str, year_str = match_mon.groups()
        mon_title = mon_str.capitalize()
        reconstructed = f"{day_str.zfill(2)}-{mon_title}-{year_str}"
        try:
            parsed = datetime.strptime(reconstructed, "%d-%b-%Y")
            return parsed.strftime("%Y-%m-%d"), True
        except ValueError:
            pass

    return None, False


def parse_decimal(raw: str) -> Tuple[Optional[str], bool]:
    """
    Parses currency/decimal values with optional prefixes (₹, Rs, Rs., INR),
    optional commas as thousands separators, non-negative, rounded to 2 decimal places.
    Returns (string_2_decimals, is_valid).
    """
    if not raw or not raw.strip():
        return None, False

    cleaned = raw.strip()

    # Strip prefixes: ₹, Rs, Rs., INR (case-insensitive, optional space)
    # Note: ₹ is unicode \u20b9
    pattern = r"^(?:₹|rs\.?|inr)\s*"
    cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    # Remove thousands separator commas
    cleaned = cleaned.replace(",", "")

    try:
        val = Decimal(cleaned)
        if val < 0:
            return None, False
        rounded = val.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return str(rounded), True
    except (InvalidOperation, ValueError):
        return None, False


def parse_enum(raw: str, allowed_values: list[str]) -> Tuple[Optional[str], bool]:
    """
    Case-insensitive enum validation against allowed values.
    Returns (normalized_lowercase_value, is_valid).
    """
    if not raw or not raw.strip():
        return None, False

    val = raw.strip().lower()
    allowed_lower = [v.lower() for v in allowed_values]
    if val in allowed_lower:
        return val, True
    return None, False


def parse_timestamp(raw: str) -> Tuple[datetime, str, str]:
    """
    Parses event timestamp (UTC 'Z', offset '+05:30', or naive meaning IST).
    Returns (aware_utc_datetime, occurred_at_utc_iso, occurred_at_ist_iso).
    """
    s = raw.strip()
    if s.endswith("Z"):
        dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    elif "T" in s:
        # Handles offsets like +05:30 or +00:00
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
    else:
        # Naive format 'YYYY-MM-DD HH:MM:SS' => IST per specification
        dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=IST)

    utc_dt = dt.astimezone(timezone.utc)
    ist_dt = dt.astimezone(IST)

    utc_iso = utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    # ISO-8601 in IST, e.g. 2026-10-14T17:55:24+05:30
    ist_iso = ist_dt.strftime("%Y-%m-%dT%H:%M:%S+05:30")

    return utc_dt, utc_iso, ist_iso
