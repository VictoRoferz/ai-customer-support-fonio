"""Zammad helpdesk connector.

Mirrors the two-layer design of `conn_business_central.py`: pure functions, no
web framework, env-driven config read at import time (process restart required
to change). Used in Fonio's *post-processing* (after-call) path to log one
ticket per call — so there is no tight latency budget here, unlike the BC
caller lookup.

Env vars:
    ZAMMAD_URL    Base URL of the instance, e.g. https://your.zammad.com
                  (no trailing /api — that path is appended here).
    ZAMMAD_TOKEN  API token. Sent as `Authorization: Token token=<...>`.
                  Create under Profile → Token Access with ticket.agent perms.
    ZAMMAD_GROUP  Group new tickets are filed under (default "Users").
                  Must already exist in Zammad (e.g. "LUMINA").

Auth docs:  https://docs.zammad.org/en/latest/api/intro.html
Ticket API: https://docs.zammad.org/en/latest/api/ticket/index.html
"""

from __future__ import annotations

import os
from typing import Any

import requests

ZAMMAD_URL = os.environ.get("ZAMMAD_URL", "").rstrip("/")
ZAMMAD_TOKEN = os.environ.get("ZAMMAD_TOKEN", "")
ZAMMAD_GROUP = os.environ.get("ZAMMAD_GROUP", "Users")

# Post-processing path: generous but bounded so a hung Zammad never wedges us.
ZAMMAD_TIMEOUT = float(os.environ.get("ZAMMAD_TIMEOUT", "10"))

_session = requests.Session()


class ZammadConfigError(RuntimeError):
    """Required Zammad env vars are missing/empty."""


class ZammadError(RuntimeError):
    """Zammad API returned an error or was unreachable."""


def _require_config() -> None:
    if not ZAMMAD_URL:
        raise ZammadConfigError("ZAMMAD_URL is not set")
    if not ZAMMAD_TOKEN:
        raise ZammadConfigError("ZAMMAD_TOKEN is not set")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Token token={ZAMMAD_TOKEN}",
        "Content-Type": "application/json",
    }


def _api(path: str) -> str:
    return f"{ZAMMAD_URL}/api/v1/{path.lstrip('/')}"


def _request(method: str, path: str, **kwargs: Any) -> Any:
    _require_config()
    try:
        resp = _session.request(
            method,
            _api(path),
            headers=_headers(),
            timeout=ZAMMAD_TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as e:
        raise ZammadError(f"Zammad request failed: {e}") from e
    if resp.status_code == 401:
        raise ZammadError(f"Zammad rejected auth (401): {resp.text}")
    if resp.status_code >= 400:
        raise ZammadError(f"Zammad {method} {path} -> {resp.status_code}: {resp.text}")
    if not resp.content:
        return None
    return resp.json()


def find_user(phone: str | None = None, email: str | None = None) -> dict[str, Any] | None:
    """Best-effort lookup of an existing Zammad user by phone or email.

    Uses the user search endpoint, which matches across login/email/phone/name.
    Returns the first hit or None. Never raises on "not found" — only on
    transport/auth errors.
    """
    query = (email or phone or "").strip()
    if not query:
        return None
    results = _request("GET", "users/search", params={"query": query, "limit": 1})
    if isinstance(results, list) and results:
        return results[0]
    return None


def _build_body(
    *,
    phone_number: str | None,
    summary: str | None,
    contact_no: str | None,
    bc_found: bool | None,
    extra: dict[str, Any] | None,
) -> str:
    lines: list[str] = []
    if phone_number:
        lines.append(f"Anrufer / Caller: {phone_number}")
    if bc_found is not None:
        lines.append(f"Business Central match: {'yes' if bc_found else 'no'}")
    if contact_no:
        lines.append(f"BC Contact No.: {contact_no}")
    if extra:
        for k, v in extra.items():
            if v not in (None, ""):
                lines.append(f"{k}: {v}")
    if lines:
        lines.append("")  # blank line before the free-text summary
    lines.append((summary or "").strip() or "(no call summary provided)")
    return "\n".join(lines)


def create_call_ticket(
    *,
    phone_number: str | None = None,
    title: str | None = None,
    customer: str | None = None,
    name: str | None = None,
    summary: str | None = None,
    contact_no: str | None = None,
    bc_found: bool | None = None,
    article_type: str = "phone",
    internal: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create one Zammad ticket logging a call. Returns the created ticket JSON.

    Customer resolution (Zammad requires a customer on a ticket):
      1. If `customer` (an email) is given, pass it through — Zammad finds or
         auto-creates that user.
      2. Else search existing users by phone; if found, use their id.
      3. Else fall back to Zammad's "guest" customer ("guess:<phone>" syntax),
         which lets Zammad attach a placeholder customer when we only have a
         phone number — no PII user record is force-created.

    `title` defaults to a caller-derived subject. `article_type` is "phone" so
    it shows as a call in Zammad's timeline; use "note" for plain logs.
    """
    _require_config()

    payload: dict[str, Any] = {
        "title": title or f"Anruf von {name or phone_number or 'Unbekannt'}",
        "group": ZAMMAD_GROUP,
        "article": {
            "subject": "Eingehender Anruf (Fonio AI)",
            "body": _build_body(
                phone_number=phone_number,
                summary=summary,
                contact_no=contact_no,
                bc_found=bc_found,
                extra=extra,
            ),
            "type": article_type,
            "internal": internal,
        },
    }

    if customer:
        payload["customer"] = customer
    else:
        user = find_user(phone=phone_number)
        if user and user.get("id"):
            payload["customer_id"] = user["id"]
        elif phone_number:
            # Zammad accepts a "guess:<value>" customer to attach a placeholder
            # without us having to create/curate a user record up front.
            payload["customer"] = f"guess:{phone_number}"
        # else: no customer info at all -> let Zammad default to the token's user

    return _request("POST", "tickets", json=payload)


def warmup() -> bool:
    """Best-effort connectivity probe at startup. Returns True if the instance
    answers an authenticated request, else False (never raises) so startup
    never crashes on a cold/unconfigured Zammad."""
    if not (ZAMMAD_URL and ZAMMAD_TOKEN):
        return False
    try:
        _request("GET", "users/me")
        return True
    except (ZammadConfigError, ZammadError):
        return False
