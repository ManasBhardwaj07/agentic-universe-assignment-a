"""
Unit tests for data normalization and validation functions.
"""
from datetime import datetime, timezone
from solution.normalization import (
    IST,
    mask_phone,
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
    parse_timestamp,
)


def test_normalize_header():
    assert normalize_header("Mobile No") == "mobile no"
    assert normalize_header("  insured___name  ") == "insured name"
    assert normalize_header("POLICY_NUMBER") == "policy number"
    assert normalize_header("order   id") == "order id"


def test_normalize_phone_valid_formats():
    # 10 digits starting with 6, 7, 8, 9
    assert normalize_phone("9876543210") == ("+919876543210", True)
    assert normalize_phone("8123456789") == ("+918123456789", True)
    assert normalize_phone("7012345678") == ("+917012345678", True)
    assert normalize_phone("6901234567") == ("+916901234567", True)

    # 12 digits starting with 91
    assert normalize_phone("919876543210") == ("+919876543210", True)
    assert normalize_phone("+91 98765 43210") == ("+919876543210", True)
    assert normalize_phone("91-98765-43210") == ("+919876543210", True)
    assert normalize_phone("(+91) 9876543210") == ("+919876543210", True)

    # 11 digits starting with 0
    assert normalize_phone("09876543210") == ("+919876543210", True)


def test_normalize_phone_invalid():
    assert normalize_phone("")[1] is False
    assert normalize_phone("   ")[1] is False
    assert normalize_phone("987654321")[1] is False  # 9 digits
    assert normalize_phone("5876543210")[1] is False  # starts with 5
    assert normalize_phone("022-2847-1948")[1] is False  # landline
    assert normalize_phone("+1 415 555 0134")[1] is False  # US number
    assert normalize_phone("N/A")[1] is False


def test_mask_phone():
    assert mask_phone("+919876543210") == "+91******3210"
    assert mask_phone("+917123456789") == "+91******6789"


def test_parse_date():
    # YYYY-MM-DD
    assert parse_date("2026-10-15") == ("2026-10-15", True)
    # DD/MM/YYYY (day-first)
    assert parse_date("15/10/2026") == ("2026-10-15", True)
    assert parse_date("02/03/2026") == ("2026-03-02", True)
    # DD-MM-YYYY
    assert parse_date("15-10-2026") == ("2026-10-15", True)
    # DD-Mon-YYYY (case-insensitive)
    assert parse_date("15-Oct-2026") == ("2026-10-15", True)
    assert parse_date("15-OCT-2026") == ("2026-10-15", True)
    assert parse_date("15-oct-2026") == ("2026-10-15", True)

    # Impossible dates
    assert parse_date("31/02/2026")[1] is False
    assert parse_date("2026-13-01")[1] is False
    assert parse_date("soon")[1] is False
    assert parse_date("")[1] is False


def test_parse_decimal():
    # Plain
    assert parse_decimal("1234.5") == ("1234.50", True)
    assert parse_decimal("1234") == ("1234.00", True)
    # Commas
    assert parse_decimal("27,554") == ("27554.00", True)
    assert parse_decimal("1,234,567.89") == ("1234567.89", True)
    # Currency prefixes
    assert parse_decimal("₹27,554") == ("27554.00", True)
    assert parse_decimal("Rs. 31629") == ("31629.00", True)
    assert parse_decimal("Rs 31629") == ("31629.00", True)
    assert parse_decimal("INR 25301.4") == ("25301.40", True)
    assert parse_decimal("inr 100") == ("100.00", True)

    # Invalid
    assert parse_decimal("-100")[1] is False  # negative
    assert parse_decimal("N/A")[1] is False
    assert parse_decimal("twelve thousand")[1] is False
    assert parse_decimal("")[1] is False


def test_parse_enum():
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert parse_enum("en", allowed) == ("en", True)
    assert parse_enum("EN", allowed) == ("en", True)
    assert parse_enum(" Hi ", allowed) == ("hi", True)
    assert parse_enum("fr", allowed)[1] is False
    assert parse_enum("", allowed)[1] is False


def test_parse_timestamp():
    # UTC
    utc_dt, utc_iso, ist_iso = parse_timestamp("2026-10-01T05:26:10Z")
    assert utc_iso == "2026-10-01T05:26:10Z"
    assert ist_iso == "2026-10-01T10:56:10+05:30"

    # Offset IST
    _, utc_iso2, ist_iso2 = parse_timestamp("2026-10-01T10:40:19+05:30")
    assert utc_iso2 == "2026-10-01T05:10:19Z"
    assert ist_iso2 == "2026-10-01T10:40:19+05:30"

    # Naive (IST per spec)
    _, utc_iso3, ist_iso3 = parse_timestamp("2026-10-01 09:15:55")
    assert ist_iso3 == "2026-10-01T09:15:55+05:30"
    assert utc_iso3 == "2026-10-01T03:45:55Z"
