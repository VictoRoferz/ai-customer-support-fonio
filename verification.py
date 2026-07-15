"""Pure identity-verification and order-control logic for /create-request.

No I/O and no web framework on purpose: these rules are security-critical
(§10/§11.2/§21 of the MED-EL Fonio prompt), so they must be testable by direct
function call — the repo has no HTTP test client (see CLAUDE.md re: httpx).

The three public pieces:
  normalize_dob(raw)            spoken/LLM-relayed birth date -> ISO or None
  match_identity_factors(...)   >=2 matching factors, DOB-gated -> verified?
  classify_item(raw)            spare-part whitelist -> canonical key or None
"""

from __future__ import annotations

import re
from datetime import date

# --- Date of birth -----------------------------------------------------------

# German + English month names -> month number. Keys are lowercase, umlauts
# also in ASCII fallback form, plus 3-letter abbreviations of both languages.
_MONTHS = {
    "januar": 1, "jaenner": 1, "january": 1, "jan": 1,
    "februar": 2, "february": 2, "feb": 2,
    "märz": 3, "maerz": 3, "mrz": 3, "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "mai": 5, "may": 5,
    "juni": 6, "june": 6, "jun": 6,
    "juli": 7, "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "oktober": 10, "october": 10, "okt": 10, "oct": 10,
    "november": 11, "nov": 11,
    "dezember": 12, "december": 12, "dez": 12, "dec": 12,
}

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$")
_NUMERIC_RE = re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{2}|\d{4})$")
# "24. Dezember 1979" / "24 Dez 1979" — day first (German style)
_DAY_MONTHNAME_RE = re.compile(r"^(\d{1,2})\.?\s+([A-Za-zÄÖÜäöü]+),?\s+(\d{4})$")
# "December 24, 1979" — month first (English style)
_MONTHNAME_DAY_RE = re.compile(r"^([A-Za-zÄÖÜäöü]+)\s+(\d{1,2}),?\s+(\d{4})$")


def _valid(year: int, month: int, day: int) -> str | None:
    """ISO string if a real, plausible birth date; else None."""
    try:
        d = date(year, month, day)
    except ValueError:
        return None
    if year < 1900 or d > date.today():
        return None
    return d.isoformat()


def _expand_two_digit_year(yy: int) -> int:
    """Pivot on the current 2-digit year: <=now -> 2000s, else 1900s.
    (A future date is rejected by _valid afterwards anyway.)"""
    return (2000 + yy) if yy <= (date.today().year % 100) else (1900 + yy)


def normalize_dob(raw: str | None) -> str | None:
    """Normalize a caller-spoken (LLM-relayed) birth date to ISO YYYY-MM-DD.

    Accepts, in order: ISO (with optional time tail), German/day-first numeric
    (24.12.1979, 24/12/1979, 24-12-1979, 2-digit years), German month names
    (24. Dezember 1979), English month names both orders. Returns None for
    anything unparseable, impossible (31.02.), pre-1900, or in the future —
    day-first ALWAYS wins for numeric forms (this is a German patient line).
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return None

    m = _ISO_RE.match(cleaned)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # Tolerate spaces around numeric separators ("24. 12. 1979" from STT).
    m = _NUMERIC_RE.match(re.sub(r"\s*([./-])\s*", r"\1", cleaned))
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3))
        if year < 100:
            year = _expand_two_digit_year(year)
        return _valid(year, month, day)

    m = _DAY_MONTHNAME_RE.match(cleaned)
    if m:
        month = _MONTHS.get(m.group(2).lower())
        if month:
            return _valid(int(m.group(3)), month, int(m.group(1)))

    m = _MONTHNAME_DAY_RE.match(cleaned)
    if m:
        month = _MONTHS.get(m.group(1).lower())
        if month:
            return _valid(int(m.group(3)), month, int(m.group(2)))

    return None


# --- Identity factors --------------------------------------------------------

def match_identity_factors(
    contact: dict,
    *,
    date_of_birth: str | None,
    phone_number: str | None,
    customer_number: str | None,
    postal_code: str | None,
    phone_matches,
) -> tuple[bool, list[str]]:
    """Compare caller-supplied factors against the BC contact record.

    `contact` is a _shape()'d record (birth_date ISO-or-None, phones list,
    customer_no, postal_code). `phone_matches` is injected from
    conn_business_central to keep this module I/O- and import-free.

    Verified iff >=2 factors match AND dob is among them — unless BC has no
    birth date on file, in which case >=2 alone decides. The DOB gate exists
    because phone + customer_number are both echoable by the LLM straight from
    the /lookup-caller response without the caller saying anything; the DOB is
    the §10 question the agent must actually ask. A factor participates only
    when both sides are present.

    Returns (verified, matched_factor_names). Log the NAMES only, never the
    values (DOB/postal are PII), and never surface either to the caller (§10.1).
    """
    matched: list[str] = []

    stored_dob = contact.get("birth_date")
    if stored_dob and date_of_birth:
        if normalize_dob(date_of_birth) == stored_dob:
            matched.append("dob")

    stored_phones = contact.get("phones") or []
    if phone_number and any(phone_matches(phone_number, p) for p in stored_phones):
        matched.append("phone")

    stored_cust = (contact.get("customer_no") or "").strip()
    if stored_cust and (customer_number or "").strip() == stored_cust:
        matched.append("customer_no")

    stored_postal = re.sub(r"[\s-]", "", contact.get("postal_code") or "")
    given_postal = re.sub(r"[\s-]", "", postal_code or "")
    if stored_postal and given_postal and stored_postal == given_postal:
        matched.append("postal")

    verified = len(matched) >= 2 and ("dob" in matched or not stored_dob)
    return verified, matched


# --- Spare-part item whitelist (§21 compliance rule, deliberately hardcoded) --

_BATTERY_KEYWORDS = ("batter", "akku")            # Batterie(n), battery, Akkus
_MIC_KEYWORDS = ("mikrofon", "microphone", "mic")
_COVER_KEYWORDS = ("abdeckung", "cover", "schutz")


def classify_item(raw: str | None) -> str | None:
    """Map the caller's wording to a canonical supported item, else None.

    Only batteries and microphone covers may be ordered over this line (§11).
    Microphone covers need BOTH a mic keyword and a cover keyword — a bare
    "Mikrofon" is the microphone itself (a repair, not a spare part) and a bare
    "Abdeckung" is too ambiguous; both fall through to UNSUPPORTED_ITEM.
    """
    text = (raw or "").strip().lower()
    if not text:
        return None
    if text in ("batteries", "microphone_covers"):   # exact enum tokens pass
        return text.upper()
    if any(k in text for k in _BATTERY_KEYWORDS):
        return "BATTERIES"
    if any(k in text for k in _MIC_KEYWORDS) and any(k in text for k in _COVER_KEYWORDS):
        return "MICROPHONE_COVERS"
    return None
