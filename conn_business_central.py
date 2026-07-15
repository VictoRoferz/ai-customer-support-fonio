"""Business Central OData connector.

Supports two auth modes — configured via env vars:

  OAuth2 (BC Online/SaaS, recommended):
    BC_TENANT_ID, BC_CLIENT_ID, BC_CLIENT_SECRET

  Basic Auth (BC On-Premises with Web Service Access Key):
    BC_USERNAME, BC_ACCESS_KEY

OData endpoint:
    BC_CONTACTS_URL — full URL to the contact entity set.
    e.g. https://api.businesscentral.dynamics.com/v2.0/{tenant}/{env}/ODataV4/Company('MyCo')/ContactCard
"""

from __future__ import annotations

import os
import re
import time
from datetime import date, timedelta
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

BC_CONTACTS_URL = os.environ.get("BC_CONTACTS_URL", "").rstrip("/")

BC_TENANT_ID = os.environ.get("BC_TENANT_ID")
BC_CLIENT_ID = os.environ.get("BC_CLIENT_ID")
BC_CLIENT_SECRET = os.environ.get("BC_CLIENT_SECRET")
BC_OAUTH_SCOPE = os.environ.get(
    "BC_OAUTH_SCOPE", "https://api.businesscentral.dynamics.com/.default"
)

BC_USERNAME = os.environ.get("BC_USERNAME")
BC_ACCESS_KEY = os.environ.get("BC_ACCESS_KEY")

PHONE_FIELDS = [
    f.strip()
    for f in os.environ.get(
        "BC_PHONE_FIELDS",
        "phoneNo,mobilePhoneNo,phoneNo2,mobilePhoneNo2,privatPhoneNo,privatMobilePhoneNo",
    ).split(",")
    if f.strip()
]

DEFAULT_COUNTRY_CODE = os.environ.get("BC_DEFAULT_COUNTRY_CODE", "49")

# Contact field holding the postal code (verification factor). Confirmed live on
# DE-TEST: `postCode` (top-level, populated; the privat* block is empty there).
# Empty value disables the field entirely — important because a wrong name here
# would 400 EVERY lookup (there is no bad-field recovery in _odata_get).
BC_POSTAL_FIELD = os.environ.get("BC_POSTAL_FIELD", "postCode").strip()

# "Ordered recently?" window for spare-part eligibility. 3 months ≈ 90 days.
ORDER_WINDOW_DAYS = int(os.environ.get("BC_ORDER_WINDOW_DAYS", "90"))

# Sales document entities that count as "an order", with their date field.
# Linkage: Contact.customerNo -> sellToCustomerNo on each header (see CLAUDE.md).
SALES_DOC_ENTITIES = {
    "SalesInvHeader": "postingDate",   # posted invoices
    "SalesShipHeader": "postingDate",  # shipments
    "SalesHeader": "orderDate",        # open orders (not yet posted orders)
}

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


class BCConfigError(RuntimeError):
    pass


class BCAuthError(RuntimeError):
    pass


def _get_oauth_token() -> str:
    if not (BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET):
        raise BCConfigError("OAuth2 env vars missing")

    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    url = f"https://login.microsoftonline.com/{BC_TENANT_ID}/oauth2/v2.0/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": BC_CLIENT_ID,
            "client_secret": BC_CLIENT_SECRET,
            "scope": BC_OAUTH_SCOPE,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise BCAuthError(f"OAuth token request failed: {resp.status_code} {resp.text}")
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + float(payload.get("expires_in", 3600))
    return _token_cache["access_token"]


def warmup() -> bool:
    """Pre-fetch the OAuth token at startup so the first Fonio call doesn't pay
    the ~2.8 s handshake on the critical path (Fonio drops the webhook after
    5000 ms). Best-effort: returns False (instead of raising) if auth isn't
    configured or the handshake fails, so startup never crashes on a cold BC.
    Basic-auth deployments have no token to prefetch, so this is a no-op (True).
    """
    if not (BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET):
        return False
    try:
        _get_oauth_token()
        return True
    except (BCConfigError, BCAuthError, requests.RequestException):
        return False


def _request_kwargs() -> dict[str, Any]:
    if BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET:
        return {"headers": {"Authorization": f"Bearer {_get_oauth_token()}"}}
    if BC_USERNAME and BC_ACCESS_KEY:
        return {"auth": HTTPBasicAuth(BC_USERNAME, BC_ACCESS_KEY)}
    raise BCConfigError(
        "No auth configured. Set BC_TENANT_ID/BC_CLIENT_ID/BC_CLIENT_SECRET for OAuth2 "
        "or BC_USERNAME/BC_ACCESS_KEY for Basic Auth."
    )


def _digits_only(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def phone_variants(raw: str) -> list[str]:
    """Generate plausible storage formats of the same phone number.

    For DE: +491512222, 00491512222, 491512222, 01512222, 1512222.
    Used to OR exact-match variants on a single field (BC's OData here
    forbids OR across distinct fields, but OR'ing values on one field works).
    """
    digits = _digits_only(raw)
    if not digits:
        return []

    cc = DEFAULT_COUNTRY_CODE
    variants: set[str] = set()

    if digits.startswith("00" + cc):
        national = digits[2 + len(cc):]
    elif digits.startswith(cc) and not raw.lstrip().startswith("0"):
        national = digits[len(cc):]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    if national:
        variants.add("+" + cc + national)
        variants.add("00" + cc + national)
        variants.add(cc + national)
        variants.add("0" + national)
        variants.add(national)

    variants.add(digits)
    raw_trim = (raw or "").strip()
    if raw_trim:
        variants.add(raw_trim)

    # International twins for foreign callers (e.g. Austria): +43... and
    # 0043... denote the same number; BC may store either form.
    if digits.startswith("00"):
        variants.add("+" + digits[2:])
    if raw_trim.startswith("+"):
        variants.add("00" + digits)

    return sorted(variants)


def _phone_filter_for_field(field: str, variants: list[str]) -> str:
    parts = [f"{field} eq '{_escape(v)}'" for v in variants]
    return " or ".join(parts)


def _select_fields() -> str:
    return ",".join(
        [
            "no",
            "name",
            "firstName",
            "middleName",
            "surname",
            "birthDate",
            "eMail",
            "customerNo",         # -> sales/repair documents (order history)
            "healthInsuranceNo",  # Krankenkasse identifier (reported, not a decision)
            *([BC_POSTAL_FIELD] if BC_POSTAL_FIELD else []),
            *PHONE_FIELDS,
        ]
    )


def _odata_get(filter_expr: str, top: int = 5) -> list[dict[str, Any]]:
    if not BC_CONTACTS_URL:
        raise BCConfigError("BC_CONTACTS_URL is not set")
    params = {
        "$filter": filter_expr,
        "$select": _select_fields(),
        "$top": str(top),
    }
    resp = requests.get(BC_CONTACTS_URL, params=params, timeout=20, **_request_kwargs())
    if resp.status_code == 401:
        raise BCAuthError(f"BC rejected auth: {resp.text}")
    resp.raise_for_status()
    return resp.json().get("value", [])


def _clean_birth_date(value: Any) -> str | None:
    """BC stores an empty birth date as the min-date 0001-01-01 (and some
    company contacts have no real DOB). Treat those as None so the agent never
    reads a bogus date aloud during identity verification."""
    if not value:
        return None
    if str(value).startswith("0001-01-01"):
        return None
    return value


def _shape(contact: dict[str, Any]) -> dict[str, Any]:
    first = contact.get("firstName") or ""
    surname = contact.get("surname") or ""
    display = (f"{first} {surname}").strip() or contact.get("name") or ""
    return {
        "no": contact.get("no"),
        "name": display,
        "first_name": first or None,
        "surname": surname or None,
        "birth_date": _clean_birth_date(contact.get("birthDate")),
        "email": contact.get("eMail"),
        "customer_no": contact.get("customerNo") or None,
        "health_insurance_no": contact.get("healthInsuranceNo") or None,
        "postal_code": (contact.get(BC_POSTAL_FIELD) or None) if BC_POSTAL_FIELD else None,
        # All stored numbers, for identity verification (phone factor) without
        # a second BC round-trip — the fields are already in the $select.
        "phones": [contact.get(f) for f in PHONE_FIELDS if contact.get(f)],
    }


def _escape(value: str) -> str:
    return value.replace("'", "''")


def lookup_by_phone(phone: str) -> dict[str, Any] | None:
    """Find a contact whose stored phone matches `phone` in any phone field.

    BC's OData here rejects OR across different fields, so we query one field
    at a time and stop at the first hit. Within a single field we OR all
    plausible format variants so callers can pass any common format.
    """
    variants = phone_variants(phone)
    if not variants:
        return None

    for field in PHONE_FIELDS:
        filt = _phone_filter_for_field(field, variants)
        rows = _odata_get(filt, top=1)
        if rows:
            return _shape(rows[0])
    return None


def _name_variants(name: str) -> list[str]:
    """German spelling variants for STT robustness: ß↔ss↔s, ä↔ae, ö↔oe, ü↔ue.

    A caller saying "Haußmann" may be transcribed "Haussmann" OR "Hausmann"
    (ß sounds like a plain s); contains() is exact, so we probe the alternates
    too. Some generated forms are nonsense ("ßtraße") — harmless, they just
    return no rows. Returns only variants that differ from the input."""
    to_ascii = (
        name.replace("ß", "ss")
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    )
    to_ascii_single = name.replace("ß", "s")
    to_special = (
        name.replace("ss", "ß")
        .replace("ae", "ä").replace("oe", "ö").replace("ue", "ü")
        .replace("Ae", "Ä").replace("Oe", "Ö").replace("Ue", "Ü")
    )
    # single s -> ß (after ss -> ß so "Haussmann" doesn't become "Haußßmann")
    to_special_single = name.replace("ss", "ß").replace("s", "ß")
    out = []
    for v in (to_ascii, to_ascii_single, to_special, to_special_single):
        if v != name and v not in out:
            out.append(v)
    return out


def _name_probes(cleaned: str) -> list[str]:
    """Probe ladder for a spoken name: full string + spelling variants, then
    the last token (surname) + its variants. Deduplicated, order preserved."""
    probes = [cleaned, *_name_variants(cleaned)]
    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    if len(tokens) >= 2:
        probes += [tokens[-1], *_name_variants(tokens[-1])]
    seen: set[str] = set()
    out = []
    for p in probes:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def lookup_by_name(name: str, limit: int = 5) -> list[dict[str, Any]]:
    """Find contacts by name using contains() on the `name` field.

    contains() is supported on a single field; only OR across distinct fields
    fails. Retry ladder: full string -> German spelling variants (ß/ss,
    umlauts; STT robustness) -> last token alone (handles "Max Mustermann"
    where only "Mustermann" is stored) -> last-token variants.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return []

    for probe in _name_probes(cleaned):
        rows = _odata_get(f"contains(name, '{_escape(probe)}')", top=limit)
        if rows:
            return [_shape(r) for r in rows]
    return []


def lookup_by_name_all(name: str, limit: int = 10) -> list[dict[str, Any]]:
    """Union of matches across ALL spelling probes (no first-hit shortcut).

    lookup_by_name stops at the first probe with rows — fine for candidate
    lists, but fatal for verification when a literal spelling ("Hausmann")
    matches real contacts while the caller is the ß-spelled one ("Haußmann"):
    the right record is never fetched. This variant merges every probe's rows,
    deduped by KN-number, so the factor check sees all plausible spellings.
    Costs up to ~8 sequential queries — use only when the first pass failed."""
    cleaned = (name or "").strip()
    if not cleaned:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for probe in _name_probes(cleaned):
        for row in _odata_get(f"contains(name, '{_escape(probe)}')", top=limit):
            shaped = _shape(row)
            if shaped["no"] not in seen:
                seen.add(shaped["no"])
                out.append(shaped)
        if len(out) >= limit * 3:  # safety cap
            break
    return out


def get_contact_by_no(contact_no: str) -> dict[str, Any] | None:
    """Fetch one contact by its KN-number (the `no` field). Exact eq filter.

    Used by /create-request to re-verify the caller's identity factors against
    BC before creating an order ticket (zero-trust toward the voice agent)."""
    cleaned = (contact_no or "").strip()
    if not cleaned:
        return None
    rows = _odata_get(f"no eq '{_escape(cleaned)}'", top=1)
    return _shape(rows[0]) if rows else None


def _to_national_digits(raw: str) -> str:
    """Reduce a phone number to its national-significant digits.

    Strips separators, then the international/trunk prefixes (`00<cc>`, `<cc>`,
    `0`), so '+49 172 893-4185', '0172 8934185' and '491728934185' all compare
    equal. `<cc>` is only stripped when the raw form isn't trunk-prefixed
    (mirrors phone_variants' handling of e.g. '0491...' Aachen-style numbers).
    """
    digits = _digits_only(raw)
    cc = DEFAULT_COUNTRY_CODE
    if digits.startswith("00" + cc):
        return digits[2 + len(cc):]
    if digits.startswith("00"):
        # Foreign 00-prefixed number (e.g. 0043... for Austria): strip only the
        # international prefix so it compares equal to its +43... twin. The
        # foreign country code stays in — cross-country numbers must not
        # collide with German nationals.
        return digits[2:]
    if digits.startswith(cc) and not (raw or "").lstrip().startswith("0"):
        return digits[len(cc):]
    if digits.startswith("0"):
        return digits[1:]
    return digits


def phone_matches(a: str, b: str) -> bool:
    """True when two phone strings denote the same national number.

    Both sides reduced via _to_national_digits; short strings (<6 digits) never
    match so fragments/extensions can't produce false positives."""
    na, nb = _to_national_digits(a or ""), _to_national_digits(b or "")
    return len(na) >= 6 and na == nb


# --- Spare-part / order eligibility -----------------------------------------
# Linkage runs off Contact.customerNo (NOT the KN-number). See CLAUDE.md.

def _entity_url(entity: str) -> str:
    """Sibling entity set under the same Company('…') OData root as Contact."""
    base = BC_CONTACTS_URL.rsplit("/Contact", 1)[0]
    return f"{base}/{entity}"


def _odata_get_entity(
    entity: str,
    filter_expr: str,
    select: str,
    top: int = 5,
    orderby: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "$filter": filter_expr,
        "$select": select,
        "$top": str(top),
    }
    if orderby:
        params["$orderby"] = orderby
    resp = requests.get(_entity_url(entity), params=params, timeout=20, **_request_kwargs())
    if resp.status_code == 401:
        raise BCAuthError(f"BC rejected auth: {resp.text}")
    resp.raise_for_status()
    return resp.json().get("value", [])


def _has_recent_order(customer_no: str, cutoff: str) -> bool:
    """True if the customer has any sales document on/after `cutoff` (ISO date)."""
    for entity, date_field in SALES_DOC_ENTITIES.items():
        filt = f"sellToCustomerNo eq '{_escape(customer_no)}' and {date_field} ge {cutoff}"
        rows = _odata_get_entity(entity, filt, select=f"no,{date_field}", top=1)
        if rows:
            return True
    return False


def _last_invoice_items(customer_no: str) -> tuple[list[str], str | None]:
    """Most recent posted invoice's line descriptions + its posting date."""
    heads = _odata_get_entity(
        "SalesInvHeader",
        f"sellToCustomerNo eq '{_escape(customer_no)}'",
        select="no,postingDate",
        top=1,
        orderby="postingDate desc",
    )
    if not heads:
        return [], None
    doc_no = heads[0].get("no")
    posting_date = heads[0].get("postingDate")
    lines = _odata_get_entity(
        "SalesInvLine",
        f"documentNo eq '{_escape(doc_no)}'",
        select="documentNo,type,no,description,quantity",
        top=20,
    )
    items = [
        (ln.get("description") or ln.get("no") or "").strip()
        for ln in lines
        if (ln.get("description") or ln.get("no"))
    ]
    return [i for i in items if i], posting_date


def has_recent_order(
    customer_no: str, window_days: int = ORDER_WINDOW_DAYS
) -> bool | None:
    """Recency check only (no invoice-line queries) for /create-request.

    Best-effort like check_order_eligibility: returns True/False, or None when
    BC errored / customer_no is empty — the caller decides how to degrade."""
    if not customer_no:
        return None
    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    try:
        return _has_recent_order(customer_no, cutoff)
    except (requests.RequestException, BCAuthError):
        return None


def check_order_eligibility(
    customer_no: str, window_days: int = ORDER_WINDOW_DAYS
) -> dict[str, Any]:
    """Decide whether a customer may order a spare part again.

    RULE (current): 3-month *recency only* — eligible iff no sales document
    (invoice / shipment / open order) exists within `window_days`. The
    Krankenkasse is NOT decided here: BC stores only the insurance number, so
    that half is reported elsewhere and a human makes the coverage call.

    Best-effort: any BC error degrades to permission=None ("unknown") rather
    than raising, so the on-ring lookup never fails the whole call over this.
    Returns: {permission_to_order_again, last_ordered_items, last_order_date}.
    """
    result: dict[str, Any] = {
        "permission_to_order_again": None,
        "last_ordered_items": "",
        "last_order_date": None,
    }
    if not customer_no:
        return result

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    try:
        recent = _has_recent_order(customer_no, cutoff)
        result["permission_to_order_again"] = not recent
    except (requests.RequestException, BCAuthError):
        pass  # leave permission as None -> agent treats as "unknown"

    try:
        items, last_date = _last_invoice_items(customer_no)
        result["last_ordered_items"] = ", ".join(items)
        result["last_order_date"] = last_date
    except (requests.RequestException, BCAuthError):
        pass

    return result
