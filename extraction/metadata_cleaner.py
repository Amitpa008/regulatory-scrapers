from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional

from dateutil import parser as date_parser


WHITESPACE_RE = re.compile(r"\s+")
REFERENCE_RE = re.compile(r"[^A-Za-z0-9/\-._()]+")


def normalize_text(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = WHITESPACE_RE.sub(" ", value).strip()
    return cleaned or None


def clean_reference_no(value: Optional[str]) -> Optional[str]:
    normalized = normalize_text(value)
    if normalized is None:
        return None
    return REFERENCE_RE.sub("", normalized) or None


def parse_indian_date(value: Optional[str]) -> Optional[date]:
    normalized = normalize_text(value)
    if not normalized:
        return None
    parsed = date_parser.parse(normalized, dayfirst=True, fuzzy=True)
    return parsed.date()


def first_non_empty(values: Iterable[Optional[str]]) -> Optional[str]:
    for value in values:
        normalized = normalize_text(value)
        if normalized:
            return normalized
    return None

