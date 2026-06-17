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

# Fallback phone matching: how many trailing digits to anchor the contains()
# probe on, and how many candidates to pull back for Python-side comparison.
PHONE_ANCHOR_LEN = int(os.environ.get("BC_PHONE_ANCHOR_LEN", "7"))
PHONE_FALLBACK_TOP = int(os.environ.get("BC_PHONE_FALLBACK_TOP", "25"))

# Latency control. Fonio drops the webhook after 5000 ms, and a no-match phone
# lookup can issue up to 2*len(PHONE_FIELDS) sequential BC requests. REQUEST_TIMEOUT
# bounds any single call; LOOKUP_BUDGET_S bounds the whole lookup_by_phone walk so
# we return found:false in time instead of timing out the call.
REQUEST_TIMEOUT = float(os.environ.get("BC_REQUEST_TIMEOUT", "4"))
LOOKUP_BUDGET_S = float(os.environ.get("BC_LOOKUP_BUDGET_S", "3.5"))

# Reuse one HTTPS connection across probes. lookup_by_phone can fire a dozen
# requests to the same BC host; without pooling each one repeats the TLS
# handshake (~hundreds of ms), which both wastes the latency budget and can make
# a short per-request timeout expire on the handshake alone.
_session = requests.Session()

# Fields we always read back. Phone fields are NOT selected wholesale: a single
# field name that doesn't exist on a tenant makes BC 400 the entire request, so
# each phone probe selects only the one field it queries (see _select_fields).
CORE_FIELDS = ["no", "name", "firstName", "middleName", "surname", "birthDate", "eMail"]

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


def _request_kwargs() -> dict[str, Any]:
    if BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET:
        return {"headers": {"Authorization": f"Bearer {_get_oauth_token()}"}}
    if BC_USERNAME and BC_ACCESS_KEY:
        return {"auth": HTTPBasicAuth(BC_USERNAME, BC_ACCESS_KEY)}
    raise BCConfigError(
        "No auth configured. Set BC_TENANT_ID/BC_CLIENT_ID/BC_CLIENT_SECRET for OAuth2 "
        "or BC_USERNAME/BC_ACCESS_KEY for Basic Auth."
    )


def warmup() -> bool:
    """Best-effort: pre-establish auth so the first real call isn't cold.

    The OAuth handshake (~2.8 s) happens lazily on the first BC request and sits
    outside lookup_by_phone's time budget, so a first call could brush against
    Fonio's 5 s limit. Calling this at server startup moves that cost off the
    critical path: it caches the OAuth token and opens the pooled HTTPS
    connection to BC with a trivial query. Never raises — returns True if BC is
    reachable and auth is ready.
    """
    try:
        kwargs = _request_kwargs()  # triggers + caches the OAuth token in OAuth mode
        if BC_CONTACTS_URL:
            _session.get(
                BC_CONTACTS_URL,
                params={"$top": "1", "$select": "no"},
                timeout=REQUEST_TIMEOUT,
                **kwargs,
            )
        return True
    except Exception:
        return False


def _digits_only(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def _to_national_digits(raw: str) -> str:
    """Reduce any phone string to its bare national-significant digits.

    Strips separators (spaces, dashes, parens), an international prefix
    (``00CC`` or ``+CC``) and a single trunk ``0`` so the same subscriber
    number compares equal regardless of how either side was formatted. This is
    the only way to match reliably: BC's OData here has no regex/replace to
    normalize the stored value server-side, so we normalize both sides in
    Python and compare.
    """
    d = _digits_only(raw)
    if not d:
        return ""
    cc = DEFAULT_COUNTRY_CODE
    if d.startswith("00" + cc):
        d = d[2 + len(cc):]
    elif d.startswith(cc) and not (raw or "").lstrip().startswith("0"):
        d = d[len(cc):]
    if d.startswith("0"):
        d = d[1:]
    return d


def _phone_matches(stored: str | None, target_national: str) -> bool:
    """True if a stored phone value is the same subscriber number as the caller.

    Compares both sides reduced to national-significant digits, so spacing,
    dashes, ``+49``/``0049``/``0`` prefixes etc. in the stored value can't
    block a real match.
    """
    if not stored or not target_national:
        return False
    return _to_national_digits(stored) == target_national


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

    return sorted(variants)


def _phone_filter_for_field(field: str, variants: list[str]) -> str:
    parts = [f"{field} eq '{_escape(v)}'" for v in variants]
    return " or ".join(parts)


def _select_fields(extra: str | None = None) -> str:
    """`$select` list: the core fields, plus one optional phone field to read.

    We never select all PHONE_FIELDS at once — an unknown field name 400s the
    whole request — so phone probes pass the single field they query as `extra`.
    """
    fields = list(CORE_FIELDS)
    if extra and extra not in fields:
        fields.append(extra)
    return ",".join(fields)


def _odata_get(
    filter_expr: str,
    top: int = 5,
    select: str | None = None,
    timeout: float | None = None,
    skip_bad_field: bool = False,
) -> list[dict[str, Any]]:
    if not BC_CONTACTS_URL:
        raise BCConfigError("BC_CONTACTS_URL is not set")
    params = {
        "$filter": filter_expr,
        "$select": select or _select_fields(),
        "$top": str(top),
    }
    resp = _session.get(
        BC_CONTACTS_URL,
        params=params,
        timeout=timeout or REQUEST_TIMEOUT,
        **_request_kwargs(),
    )
    if resp.status_code == 401:
        raise BCAuthError(f"BC rejected auth: {resp.text}")
    # A 400 on a per-field phone probe usually means the field doesn't exist on
    # this tenant (or the operator isn't supported there). Treat it as "no match
    # via this field" so one bad field can't fail the whole lookup.
    if resp.status_code == 400 and skip_bad_field:
        return []
    resp.raise_for_status()
    return resp.json().get("value", [])


def _clean_birth_date(value: Any) -> str | None:
    """Drop BC's empty/min date so it never reaches the voice agent.

    BC stores "no birth date" as `0001-01-01` (common for company contacts).
    Returning it would let Fonio read `0001-01-01` aloud as if it were a real
    date to confirm identity against.
    """
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
    }


def _escape(value: str) -> str:
    return value.replace("'", "''")


def lookup_by_phone(phone: str) -> dict[str, Any] | None:
    """Find a contact whose stored phone matches `phone` in any phone field.

    BC's OData here rejects OR across different fields, so we query one field at
    a time and stop at the first hit. Two passes:

      1. Fast path — exact `eq` OR'ing all plausible format variants of the
         number on a single field. Indexed and cheap; matches cleanly-stored
         numbers regardless of how the caller formatted theirs.
      2. Fallback — if nothing matched exactly, pull candidates whose value
         *contains* the trailing digits of the number, then compare both sides
         reduced to national-significant digits. This is what catches numbers
         BC stored with embedded spaces/dashes (e.g. ``0172 893-4185``), which
         no exact `eq` variant can equal.

    Every hit is re-verified in Python with `_phone_matches`, so a loose
    candidate can never be returned as a false positive.
    """
    target = _to_national_digits(phone)
    variants = phone_variants(phone)
    if not target and not variants:
        return None

    # Bound total wall-clock: we may walk every phone field twice, and Fonio
    # drops the call after 5 s. Stop probing once the budget is spent (and cap
    # each request to whatever time is left) so we still answer found:false.
    deadline = time.monotonic() + LOOKUP_BUDGET_S
    # Don't start a request with so little time left that it would just time out
    # on the round-trip itself.
    min_slice = 0.5

    def _budget_left() -> float:
        return deadline - time.monotonic()

    def _probe(filter_expr: str, top: int, field: str) -> list[dict[str, Any]]:
        """One budgeted, fault-tolerant BC query for a single phone field.

        Returns [] (never raises) on a bad field (400) or a slow/failed call, so
        one unreachable field can't fail the whole lookup or turn into a 502.
        """
        try:
            return _odata_get(
                filter_expr,
                top=top,
                select=_select_fields(field),
                timeout=min(REQUEST_TIMEOUT, _budget_left()),
                skip_bad_field=True,
            )
        except requests.RequestException:
            return []

    # Pass 1 — exact equality on indexed fields.
    for field in PHONE_FIELDS:
        if _budget_left() <= min_slice:
            return None
        rows = _probe(_phone_filter_for_field(field, variants), 1, field)
        if rows and _phone_matches(rows[0].get(field), target):
            return _shape(rows[0])

    # Pass 2 — digits-only fallback for separator-formatted stored values.
    if target:
        anchor = target[-PHONE_ANCHOR_LEN:] if len(target) > PHONE_ANCHOR_LEN else target
        for field in PHONE_FIELDS:
            if _budget_left() <= min_slice:
                return None
            rows = _probe(
                f"contains({field}, '{_escape(anchor)}')", PHONE_FALLBACK_TOP, field
            )
            for row in rows:
                if _phone_matches(row.get(field), target):
                    return _shape(row)
    return None


def lookup_by_name(name: str, limit: int = 5) -> list[dict[str, Any]]:
    """Find contacts by name using contains() on the `name` field.

    contains() is supported on a single field; only OR across distinct fields
    fails. If the full string returns nothing, retry with the last token alone
    (handles cases like "Max Mustermann" where only "Mustermann" is stored).
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return []

    filt = f"contains(name, '{_escape(cleaned)}')"
    rows = _odata_get(filt, top=limit)
    if rows:
        return [_shape(r) for r in rows]

    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    if len(tokens) >= 2:
        filt = f"contains(name, '{_escape(tokens[-1])}')"
        rows = _odata_get(filt, top=limit)
        return [_shape(r) for r in rows]

    return []
