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


_me_cache: dict[str, Any] = {}


def _token_user_id() -> int | None:
    """Id of the API token's own user (cached). Best-effort: None on failure."""
    if "id" not in _me_cache:
        try:
            me = _request("GET", "users/me")
            _me_cache["id"] = (me or {}).get("id")
        except ZammadError:
            return None
    return _me_cache.get("id")


def _resolve_customer(
    payload: dict[str, Any], *, customer: str | None, phone_number: str | None
) -> None:
    """Attach a customer to a ticket payload (Zammad hard-requires one).

    1. Explicit `customer` email -> pass through (Zammad finds/auto-creates).
    2. Else search existing users by phone -> use their id.
    3. Else file under the service account's own user. Zammad's "guess:"
       syntax only accepts EMAILS — "guess:<phone>" 422s (verified live
       2026-07-13), and omitting the customer 422s too. The real caller
       number stays in the ticket body, and no PII user record is created.
    """
    if customer:
        payload["customer"] = customer
        return
    user = find_user(phone=phone_number)
    if user and user.get("id"):
        payload["customer_id"] = user["id"]
        return
    me_id = _token_user_id()
    if me_id:
        payload["customer_id"] = me_id
    # else: leave unset and let Zammad's 422 surface — nothing sane to attach.


def create_ticket(
    *,
    title: str,
    body: str,
    customer: str | None = None,
    phone_number: str | None = None,
    priority_id: int | None = None,
    tags: str | None = None,
    article_type: str = "note",
    internal: bool = False,
) -> dict[str, Any]:
    """Create one Zammad ticket with an arbitrary title/body. Returns the raw
    ticket JSON (callers read `id` and `number` — the ticket number doubles as
    the request number read to the caller, per prompt §11.4).

    `priority_id` maps to Zammad's ticket_priorities (default install:
    1 low / 2 normal / 3 high) — used for VIGILANCE / URGENT_MEDICAL.
    `tags` is a comma-separated string; best-effort (older Zammad versions
    ignore it on create — the [TYPE] title prefix is the durable encoding).
    """
    _require_config()
    payload: dict[str, Any] = {
        "title": title,
        "group": ZAMMAD_GROUP,
        "article": {
            "subject": title,
            "body": body,
            "type": article_type,
            "internal": internal,
        },
    }
    if priority_id is not None:
        payload["priority_id"] = priority_id
    if tags:
        payload["tags"] = tags
    _resolve_customer(payload, customer=customer, phone_number=phone_number)
    return _request("POST", "tickets", json=payload)


# Ticket states that count as "still open" for duplicate detection.
# NB: the two-word state MUST be quoted — an unquoted space breaks the whole
# Lucene query (0 hits for everything; found the hard way, 2026-07-13).
_OPEN_STATES = '(new OR open OR "pending reminder")'


def search_open_tickets(
    *,
    phone_number: str | None = None,
    contact_no: str | None = None,
    title_contains: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Find open tickets for a caller, for duplicate-request prevention (§11.2).

    Strategy: resolve the caller to a Zammad user by phone and query their open
    tickets; else fall back to a full-text phrase search on the KN-number
    (every ticket we create carries it in title/body). `title_contains` is
    applied client-side — don't bet on Lucene tokenizing "[SPARE_PARTS]".

    Returns [] when nothing matches; raises ZammadError only on transport/auth
    problems (callers decide how to degrade).
    """
    query = None
    user = find_user(phone=phone_number) if phone_number else None
    if user and user.get("id"):
        query = f"state.name:{_OPEN_STATES} AND customer_id:{user['id']}"
    elif contact_no:
        query = f'state.name:{_OPEN_STATES} AND "{contact_no}"'
    if not query:
        return []

    results = _request(
        "GET", "tickets/search", params={"query": query, "limit": limit, "expand": "true"}
    )
    # Normalize the two response shapes: expanded list vs {tickets, assets}.
    if isinstance(results, dict):
        assets = (results.get("assets") or {}).get("Ticket") or {}
        tickets = [assets[str(tid)] for tid in results.get("tickets") or [] if str(tid) in assets]
    elif isinstance(results, list):
        tickets = results
    else:
        tickets = []

    if title_contains:
        tickets = [t for t in tickets if title_contains in (t.get("title") or "")]
    return tickets


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
      3. Else file under the service account's user (see _resolve_customer —
         "guess:<phone>" does NOT work, the syntax only accepts emails).

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

    _resolve_customer(payload, customer=customer, phone_number=phone_number)
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
