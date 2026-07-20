from __future__ import annotations

import re
import unicodedata
from urllib.parse import urlsplit

LEGAL_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "company", "co", "llc",
    "llp", "lp", "ltd", "limited", "plc", "gmbh", "ag", "sa", "sas",
    "bv", "nv", "pte", "pty", "holdings", "group",
}
COUNTRIES = {
    "united states": "US", "usa": "US", "us": "US", "u.s.": "US",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB",
    "canada": "CA", "germany": "DE", "france": "FR", "japan": "JP",
    "australia": "AU", "netherlands": "NL", "singapore": "SG",
}
NULL_TOKENS = {"-", "n/a", "na", "none", "null", "unknown"}


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    value = unicodedata.normalize("NFKD", str(value))
    value = "".join(c for c in value if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", value).strip()


def normalize_null(value):
    if value is None:
        return None
    if isinstance(value, str) and value.strip().casefold() in NULL_TOKENS:
        return None
    return value


def normalize_name(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"\band\b", " and ", value)
    tokens = re.findall(r"[a-z0-9]+", value)
    while tokens and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens) or None


def normalize_domain(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    candidate = value if "://" in value else f"//{value}"
    host = (urlsplit(candidate).hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or "." not in host or " " in host:
        return None
    parts = host.split(".")
    # Conservative registrable-domain approximation; preserves country-code domains.
    return ".".join(parts[-3:] if len(parts) > 2 and parts[-2] in {"co", "com", "org", "net"} else parts[-2:])


def normalize_country(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    if len(value) == 2 and value.isalpha():
        return value.upper()
    return COUNTRIES.get(value.casefold(), value.upper())


def normalize_identifier(value: str | None, identifier_type: str | None = None) -> str | None:
    value = normalize_null(value)
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper()) or None
    if normalized and identifier_type == "cik" and normalized.isdigit():
        return normalized.zfill(10)
    return normalized

