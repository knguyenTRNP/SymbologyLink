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
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "britain": "GB",
    "england": "GB", "scotland": "GB", "wales": "GB", "northern ireland": "GB",
    "canada": "CA", "germany": "DE", "france": "FR", "japan": "JP",
    "australia": "AU", "netherlands": "NL", "singapore": "SG",
}
ISO_ALPHA2_CODES = frozenset("""
AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO JP
KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR MS MT
MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO RS RU RW
SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV TW TZ UA UG
UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
""".split())
EXCHANGE_ALIASES = {
    "NASDAQ": "XNAS", "NASDAQGS": "XNAS", "NASDAQGLOBALSELECT": "XNAS",
    "NASDAQGM": "XNAS", "NASDAQGLOBALMARKET": "XNAS", "NASDAQCM": "XNAS",
    "NASDAQCAPITALMARKET": "XNAS", "XNAS": "XNAS",
    "NYSE": "XNYS", "NEWYORKSTOCKEXCHANGE": "XNYS", "XNYS": "XNYS",
    "NYSEARCA": "ARCX", "ARCA": "ARCX", "ARCX": "ARCX",
    "NYSEAMERICAN": "XASE", "AMEX": "XASE", "XASE": "XASE",
    "LSE": "XLON", "LONDON": "XLON", "XLON": "XLON",
    "TSX": "XTSE", "XTSE": "XTSE", "TSXV": "XTSX", "XTSX": "XTSX",
    "EURONEXTPARIS": "XPAR", "XPAR": "XPAR", "EURONEXTAMSTERDAM": "XAMS", "XAMS": "XAMS",
    "XETRA": "XETR", "XETR": "XETR", "TOKYO": "XTKS", "XTKS": "XTKS",
    "ASX": "XASX", "XASX": "XASX", "SINGAPORE": "XSES", "XSES": "XSES",
    "HONGKONG": "XHKG", "XHKG": "XHKG",
}
STREET_NUMBER_WORDS = {
    "zero": "0", "one": "1", "two": "2", "three": "3", "four": "4",
    "five": "5", "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10",
}
STREET_SUFFIXES = {
    "st": "street", "str": "street", "street": "street",
    "rd": "road", "road": "road", "ave": "avenue", "av": "avenue", "avenue": "avenue",
    "blvd": "boulevard", "boulevard": "boulevard", "dr": "drive", "drive": "drive",
    "ln": "lane", "lane": "lane", "ct": "court", "court": "court",
    "pkwy": "parkway", "parkway": "parkway", "hwy": "highway", "highway": "highway",
    "wy": "way", "way": "way", "pl": "place", "place": "place",
}
SUBDIVISION_ALIASES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar", "california": "ca",
    "colorado": "co", "connecticut": "ct", "delaware": "de", "district of columbia": "dc",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id", "illinois": "il",
    "indiana": "in", "iowa": "ia", "kansas": "ks", "kentucky": "ky", "louisiana": "la",
    "maine": "me", "maryland": "md", "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne", "nevada": "nv",
    "new hampshire": "nh", "new jersey": "nj", "new mexico": "nm", "new york": "ny",
    "north carolina": "nc", "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc", "south dakota": "sd",
    "tennessee": "tn", "texas": "tx", "utah": "ut", "vermont": "vt", "virginia": "va",
    "washington": "wa", "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "alberta": "ab", "british columbia": "bc", "manitoba": "mb", "new brunswick": "nb",
    "newfoundland and labrador": "nl", "nova scotia": "ns", "ontario": "on",
    "prince edward island": "pe", "quebec": "qc", "saskatchewan": "sk",
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
    alias = COUNTRIES.get(value.casefold())
    if alias:
        return alias
    code = value.upper()
    return code if code in ISO_ALPHA2_CODES else None


def normalize_exchange(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    compact = re.sub(r"[^A-Z0-9]", "", value.upper())
    return EXCHANGE_ALIASES.get(compact, compact or None)


def normalize_address(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    tokens = re.findall(r"[a-z0-9]+", value.casefold())
    if tokens and tokens[0] in STREET_NUMBER_WORDS:
        tokens[0] = STREET_NUMBER_WORDS[tokens[0]]
    tokens = [STREET_SUFFIXES.get(token, token) for token in tokens]
    return " ".join(tokens) or None


def normalize_locality(value: str | None) -> str | None:
    value = clean_text(value)
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold())) if value else None


def normalize_subdivision(value: str | None) -> str | None:
    normalized = normalize_locality(value)
    return SUBDIVISION_ALIASES.get(normalized, normalized) if normalized else None


def normalize_postal_code(value: str | None) -> str | None:
    value = clean_text(value)
    if not value:
        return None
    return re.sub(r"[^A-Z0-9]", "", value.upper()) or None


def normalize_identifier(value: str | None, identifier_type: str | None = None) -> str | None:
    value = normalize_null(value)
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]", "", str(value).upper()) or None
    if identifier_type == "exchange":
        return normalize_exchange(normalized)
    if normalized and identifier_type == "cik" and normalized.isdigit():
        return normalized.zfill(10)
    return normalized

