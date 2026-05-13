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


def _shape(contact: dict[str, Any]) -> dict[str, Any]:
    first = contact.get("firstName") or ""
    surname = contact.get("surname") or ""
    display = (f"{first} {surname}").strip() or contact.get("name") or ""
    return {
        "no": contact.get("no"),
        "name": display,
        "first_name": first or None,
        "surname": surname or None,
        "birth_date": contact.get("birthDate"),
        "email": contact.get("eMail"),
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
