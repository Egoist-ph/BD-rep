"""Text normalization, keyword matching and state detection."""

from __future__ import annotations

import html
import re
import unicodedata
from functools import lru_cache
from typing import Iterable

_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_TAG = re.compile(r"<[^>]+>")
_SAM_PREFIX = re.compile(r"^\s*[A-Z0-9]{1,5}\s*--\s*")

TITLE_STOPWORDS = {"the", "a", "an", "of", "for", "and", "at", "in", "on", "to", "project"}

ADDRESS_ABBREVIATIONS = {
    "street": "st", "avenue": "ave", "boulevard": "blvd", "road": "rd",
    "drive": "dr", "suite": "ste", "north": "n", "south": "s", "east": "e",
    "west": "w", "highway": "hwy", "parkway": "pkwy", "lane": "ln",
    "court": "ct", "place": "pl", "building": "bldg", "floor": "fl",
}

STATE_NAMES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington state": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}
STATE_CODES = set(STATE_NAMES.values()) | {"DC", "PR"}

# AP-style abbreviations, matched case-sensitively on raw text.
AP_ABBREVIATIONS = {"Calif.": "CA", "Ill.": "IL", "Fla.": "FL"}

# Places that only exist in one target state. Ambiguous names (Springfield,
# Aurora, Orange County, Naples, Bloomington, ...) are left out on purpose.
PLACE_ALIASES = {
    "CA": [
        "los angeles", "san francisco", "san jose", "san diego", "sacramento",
        "oakland", "fresno", "long beach", "bakersfield", "anaheim", "irvine",
        "santa clara", "sunnyvale", "mountain view", "palo alto", "fremont",
        "stockton", "modesto", "santa ana", "san bernardino", "silicon valley",
        "bay area", "inland empire", "central valley", "kern county",
        "alameda county", "contra costa", "san mateo", "camp pendleton",
    ],
    "IL": [
        "chicago", "peoria", "rockford", "naperville", "joliet", "elgin",
        "champaign", "evanston", "schaumburg", "des plaines", "waukegan",
        "cook county", "dupage county", "ohare",
    ],
    "FL": [
        "miami", "orlando", "tampa", "jacksonville", "tallahassee",
        "fort lauderdale", "st petersburg", "west palm beach", "boca raton",
        "sarasota", "fort myers", "pensacola", "daytona beach", "miami dade",
        "broward", "palm beach county", "cape canaveral", "kennedy space center",
    ],
}


def strip_html(text: str) -> str:
    return " ".join(html.unescape(_TAG.sub(" ", text or "")).split())


def norm_text(text: str) -> str:
    """Lowercase ASCII, punctuation to single spaces."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = text.lower().replace("'", "").replace("&", " and ")
    return _NON_ALNUM.sub(" ", text).strip()


def clean_title(title: str) -> str:
    """Drop SAM.gov product-code prefixes such as 'Z1DA--'."""
    return _SAM_PREFIX.sub("", title or "").strip()


def norm_title(title: str) -> str:
    words = norm_text(clean_title(title)).split()
    return " ".join(w for w in words if w not in TITLE_STOPWORDS)


def norm_address(*parts: str) -> str:
    words = norm_text(" ".join(p for p in parts if p)).split()
    return " ".join(ADDRESS_ABBREVIATIONS.get(w, w) for w in words)


def numbers_in(text: str) -> set[str]:
    return set(re.findall(r"\d+", text or ""))


@lru_cache(maxsize=256)
def _term_regex(terms: tuple[str, ...]) -> re.Pattern[str] | None:
    normed = sorted({norm_text(t) for t in terms if norm_text(t)}, key=len, reverse=True)
    if not normed:
        return None
    alternation = "|".join(re.escape(t) for t in normed)
    return re.compile(rf"(?<![a-z0-9])(?:{alternation})(?![a-z0-9])")


def match_terms(normalized_text: str, terms: Iterable[str]) -> list[str]:
    """Distinct terms (normalized) found as whole words in normalized text."""
    rx = _term_regex(tuple(terms))
    if rx is None:
        return []
    return sorted(set(rx.findall(normalized_text)))


def detect_states(text: str) -> list[str]:
    """State codes mentioned in text, ordered by first mention."""
    found: dict[str, int] = {}

    def note(code: str, pos: int) -> None:
        if code not in found or pos < found[code]:
            found[code] = pos

    normed = norm_text(text)
    padded = f" {normed} "
    for name, code in STATE_NAMES.items():
        idx = padded.find(f" {name} ")
        if idx >= 0:
            note(code, idx)
    for code, places in PLACE_ALIASES.items():
        for place in places:
            idx = padded.find(f" {place} ")
            if idx >= 0:
                note(code, idx)

    # ", CA" / ", Calif." style references, on the raw text. Positions are
    # approximate (raw vs normalized) but only used for ordering.
    for m in re.finditer(r",\s*([A-Z]{2})\b", text or ""):
        if m.group(1) in STATE_CODES:
            note(m.group(1), m.start())
    for abbr, code in AP_ABBREVIATIONS.items():
        idx = (text or "").find(abbr)
        if idx >= 0:
            note(code, idx)
    return sorted(found, key=found.get)


def pick_state(text: str, targets: Iterable[str]) -> str | None:
    """First target state mentioned; else first other state; else None."""
    mentioned = detect_states(text)
    targets = set(targets)
    for code in mentioned:
        if code in targets:
            return code
    return mentioned[0] if mentioned else None
