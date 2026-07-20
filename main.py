"""FastAPI webhook server for Fonio AI agent.

Endpoints:
  POST /lookup-caller    { "phone_number": "+49..." }     on ring, all §9 prompt vars
  POST /lookup-by-name   { "name": "Max Mustermann" }     mid-call assistant tool
  POST /create-request   { "request_type": "...", ... }   mid-call: typed Zammad request
  POST /log-call         { "phone_number": ..., ... }     after-call protocol ticket
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager
from enum import Enum

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from conn_business_central import (
    BCAuthError,
    BCConfigError,
    check_order_eligibility,
    get_contact_by_no,
    has_recent_order,
    lookup_by_name,
    lookup_by_name_all,
    lookup_by_phone,
    phone_matches,
    warmup,
)
from conn_zammad import (
    ZammadConfigError,
    ZammadError,
    create_call_ticket,
    create_ticket,
    search_open_tickets,
)
from conn_zammad import warmup as zammad_warmup
from verification import classify_item, match_identity_factors

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fonio-bc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm the BC OAuth token so the first Fonio call doesn't pay the ~2.8 s
    # handshake on the critical path (Fonio drops the webhook after 5000 ms).
    log.info("warming BC auth: %s", "ok" if warmup() else "deferred (will retry on first call)")
    log.info("warming Zammad: %s", "ok" if zammad_warmup() else "deferred/unconfigured")
    yield


app = FastAPI(title="Fonio ↔ Business Central", lifespan=lifespan)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# Max units per spare-part request. Echoed as {{max_quantity}} in /lookup-caller
# and enforced server-side in /create-request (§11.2).
SPARE_MAX_QUANTITY = int(os.environ.get("SPARE_MAX_QUANTITY", "5"))

# Zammad priority for VIGILANCE / URGENT_MEDICAL tickets.
# Confirmed live on medelde1: 1 low / 2 normal / 3 high.
ZAMMAD_URGENT_PRIORITY_ID = int(os.environ.get("ZAMMAD_URGENT_PRIORITY_ID", "3"))


def _check_auth(authorization: str | None) -> None:
    if not WEBHOOK_SECRET:
        return
    expected = f"Bearer {WEBHOOK_SECRET}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


class _TemplateTolerantModel(BaseModel):
    """Fonio's HTTP-Request body is a template: unfilled variables may arrive as
    "" or as the literal "{{variableName}}". Treat both as absent so optional
    fields degrade gracefully instead of 422ing the tool call."""

    @field_validator("*", mode="before")
    @classmethod
    def _blank_and_unfilled_templates_to_none(cls, v):
        if isinstance(v, str):
            s = v.strip()
            if not s or (s.startswith("{{") and s.endswith("}}")):
                return None
        return v


class PhoneLookupIn(_TemplateTolerantModel):
    # Optional: anonymous callers (suppressed caller ID) and browser test calls
    # have no number — the lookup then simply finds nothing.
    phone_number: str | None = Field(default=None, description="Caller's phone number in any format")


class NameLookupIn(_TemplateTolerantModel):
    name: str = Field(..., description="Patient's full or partial name")


class ContactOut(BaseModel):
    found: bool
    name: str | None = None
    first_name: str | None = None
    surname: str | None = None
    birth_date: str | None = None
    contact_no: str | None = None
    candidates: list[dict] | None = None


class CallerOut(BaseModel):
    """Response for /lookup-caller. Top-level keys are bound 1:1 to the Fonio
    prompt's {{system variables}} (§9) — DO NOT rename without updating the prompt.

    open_requests is always null in v1 (duplicate prevention runs server-side in
    /create-request instead — the on-ring path has no latency budget for it).
    authorized_contacts is always null (no BC data source; documented as unbacked).
    emergency_number / alternative_channel are static Fonio-side variables, never
    returned here. Extra keys (contact_no, health_insurance_no, last_order_date)
    are for internal use / the Zammad protocol and are ignored by the prompt.
    """

    customer_found: bool
    name: str | None = None
    customer_number: str | None = None          # BC customerNo (for orders)
    date_of_birth: str | None = None
    phone_number: str | None = None             # echoed input
    postal_code: str | None = None              # BC postCode (verification factor)
    permission_to_order_again: bool | None = None
    last_ordered_items: str = ""
    open_requests: str | None = None            # always null in v1
    authorized_contacts: str | None = None      # always null (unbacked)
    max_quantity: int = SPARE_MAX_QUANTITY
    # internal / protocol only:
    contact_no: str | None = None               # BC KN-number (Zammad linkage)
    health_insurance_no: str | None = None      # reported, not a decision
    last_order_date: str | None = None


class RequestType(str, Enum):
    """§17 request taxonomy. SPARE_PARTS carries extra order controls;
    VIGILANCE / URGENT_MEDICAL are filed with high priority."""

    CALLBACK = "CALLBACK"
    SUPPORT = "SUPPORT"
    SPARE_PARTS = "SPARE_PARTS"
    COMPLAINT = "COMPLAINT"
    VIGILANCE = "VIGILANCE"
    URGENT_MEDICAL = "URGENT_MEDICAL"


class CreateRequestIn(_TemplateTolerantModel):
    """Mid-call request creation. Factor/order fields are optional at the model
    level on purpose: conditional requirements are enforced in the handler so the
    voice agent receives actionable reason codes instead of opaque 422s."""

    request_type: RequestType
    # Optional: browser test calls and suppressed caller IDs carry no number;
    # verification and Zammad customer resolution both degrade gracefully.
    phone_number: str | None = Field(default=None, description="Caller's number ({{fromNumber}})")
    name: str | None = Field(default=None, description="Caller-SPOKEN full name (identity resolution)")
    contact_no: str | None = Field(default=None, description="BC KN-number from /lookup-caller or verify_caller")
    customer_number: str | None = Field(default=None, description="Verification factor")
    date_of_birth: str | None = Field(default=None, description="Caller-SPOKEN birth date, any format")
    postal_code: str | None = Field(default=None, description="Verification factor")
    item: str | None = Field(default=None, description="SPARE_PARTS: requested item, free text")
    quantity: int | None = Field(default=None, description="SPARE_PARTS: requested amount")
    summary: str | None = Field(default=None, description="Caller's request in agent's words")
    callback_time: str | None = Field(default=None, description="CALLBACK: preferred time, free text")
    internal: bool = Field(default=False, description="Mark the article internal (testing)")


class CreateRequestOut(BaseModel):
    created: bool
    denied: bool = False
    # NOT_VERIFIED | NOT_ELIGIBLE | QUANTITY_EXCEEDED | DUPLICATE_OPEN |
    # UNSUPPORTED_ITEM | INVALID_REQUEST
    reason_code: str | None = None
    request_number: str | None = None           # Zammad ticket number (§11.4)
    ticket_id: int | None = None
    existing_request_number: str | None = None  # on DUPLICATE_OPEN
    message: str | None = None                  # speakable German, factor-safe


class VerifyIn(_TemplateTolerantModel):
    """Mid-call identity verification for callers not matched by phone (§10.1):
    the agent submits what the caller SPOKE; the server checks it against BC."""

    name: str = Field(..., description="Caller-spoken full name")
    date_of_birth: str = Field(..., description="Caller-spoken birth date, any format")
    phone_number: str | None = Field(default=None, description="{{fromNumber}} — extra factor if it matches")
    customer_number: str | None = Field(default=None, description="Caller-spoken Kundennummer (3rd factor)")
    postal_code: str | None = Field(default=None, description="Caller-spoken PLZ (3rd factor)")
    contact_no: str | None = Field(
        default=None,
        description="{{contact_no}} from the ring lookup, if the phone matched — "
                    "evaluated as an extra candidate so a garbled STT name can't "
                    "fail the real patient (factors still decide)",
    )


class VerifyOut(BaseModel):
    """Patient data is returned ONLY on verified=true — unlike /lookup-by-name,
    this never discloses stored values for an unverified caller."""

    verified: bool
    name: str | None = None
    contact_no: str | None = None
    customer_number: str | None = None
    permission_to_order_again: bool | None = None
    last_ordered_items: str = ""
    last_order_date: str | None = None
    max_quantity: int = SPARE_MAX_QUANTITY
    message: str | None = None


class CallLogIn(_TemplateTolerantModel):
    """Fonio post-processing (after-call) payload → one Zammad ticket."""

    phone_number: str | None = Field(default=None, description="Caller's number")
    name: str | None = Field(default=None, description="Caller/contact name, if known")
    customer: str | None = Field(
        default=None, description="Customer email; Zammad finds or auto-creates the user"
    )
    summary: str | None = Field(default=None, description="Call summary / transcript")
    contact_no: str | None = Field(default=None, description="BC contact no. (KN-number), if matched")
    bc_found: bool | None = Field(default=None, description="Whether BC identified the caller")
    title: str | None = Field(default=None, description="Override the ticket title")


class CallLogOut(BaseModel):
    created: bool
    ticket_id: int | None = None
    ticket_number: str | None = None


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/lookup-caller", response_model=CallerOut)
def lookup_caller(
    body: PhoneLookupIn,
    authorization: str | None = Header(default=None),
) -> CallerOut:
    """On call ring: identify the caller by phone in BC and, if matched, compute
    spare-part order eligibility (3-month recency rule). Returns all the prompt's
    system variables in one response. The agent still verifies identity (name +
    date_of_birth) against these values before disclosing anything."""
    _check_auth(authorization)
    try:
        contact = lookup_by_phone(body.phone_number)
    except BCConfigError as e:
        raise HTTPException(status_code=500, detail=f"config: {e}") from e
    except BCAuthError as e:
        raise HTTPException(status_code=502, detail=f"bc auth: {e}") from e
    except Exception as e:
        log.exception("lookup_by_phone failed")
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not contact:
        return CallerOut(customer_found=False, phone_number=body.phone_number)

    # Eligibility is best-effort (never raises) so a slow/failed sales query
    # degrades to permission=None rather than dropping the call.
    elig = check_order_eligibility(contact.get("customer_no") or "")

    return CallerOut(
        customer_found=True,
        name=contact["name"],
        customer_number=contact.get("customer_no"),
        # Deliberately withheld since the unified flow (2026-07-13): the DOB is
        # the verification secret and is checked server-side in /verify-caller —
        # if the LLM never receives it, no prompt injection can leak it.
        date_of_birth=None,
        phone_number=body.phone_number,
        postal_code=contact.get("postal_code"),
        permission_to_order_again=elig["permission_to_order_again"],
        last_ordered_items=elig["last_ordered_items"],
        contact_no=contact["no"],
        health_insurance_no=contact.get("health_insurance_no"),
        last_order_date=elig["last_order_date"],
    )


@app.post("/lookup-by-name", response_model=ContactOut)
def lookup_name(
    body: NameLookupIn,
    authorization: str | None = Header(default=None),
) -> ContactOut:
    _check_auth(authorization)
    try:
        matches = lookup_by_name(body.name, limit=5)
    except BCConfigError as e:
        raise HTTPException(status_code=500, detail=f"config: {e}") from e
    except BCAuthError as e:
        raise HTTPException(status_code=502, detail=f"bc auth: {e}") from e
    except Exception as e:
        log.exception("lookup_by_name failed")
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not matches:
        return ContactOut(found=False)

    if len(matches) == 1:
        c = matches[0]
        return ContactOut(
            found=True,
            name=c["name"],
            first_name=c["first_name"],
            surname=c["surname"],
            birth_date=c["birth_date"],
            contact_no=c["no"],
        )

    return ContactOut(found=True, candidates=matches)


_VERIFY_FAIL_MSG = (
    "Die Identität konnte nicht sicher bestätigt werden. Es kann ein "
    "Rückrufwunsch erfasst werden, oder fragen Sie nach Kundennummer "
    "oder Postleitzahl und versuchen Sie es noch einmal."
)


def _resolve_and_verify(
    *,
    name: str | None,
    contact_no: str | None,
    date_of_birth: str | None,
    phone_number: str | None,
    customer_number: str | None,
    postal_code: str | None,
) -> tuple[dict | None, list[str]]:
    """Locate exactly ONE BC contact that passes the §10.1 factor rules.

    Shared by /verify-caller and /create-request so ordering never depends on
    the LLM correctly copying a KN-number between tool calls — the spoken name
    plus factors are always sufficient.

    Candidate sources: the spoken name (fast ladder, then widened spelling
    union) and/or the KN-number (ring lookup / previous verification).
    Name-selected candidates need >=1 matching factor incl. DOB when on file
    (the name itself is the implicit first factor); the KN-selected contact
    gets NO name credit and needs the full >=2-factors-incl-DOB rule.
    Exactly one survivor may remain: zero or several -> (None, []) — the
    caller-facing response must be identical either way (§10.1).
    BC transport/auth errors propagate — callers map them to HTTP codes.
    """
    def evaluate(pool: list[dict]) -> list[tuple[dict, list[str]]]:
        found = []
        for c in pool:
            _, matched = match_identity_factors(
                c,
                date_of_birth=date_of_birth,
                phone_number=phone_number,
                customer_number=customer_number,
                postal_code=postal_code,
                phone_matches=phone_matches,
            )
            if matched and ("dob" in matched or not c.get("birth_date")):
                found.append((c, matched))
        return found

    candidates = lookup_by_name(name, limit=10) if name else []
    survivors = evaluate(candidates)
    evaluated_nos = {c.get("no") for c in candidates}

    if name and not survivors:
        # Widened second pass: the fast ladder stops at the first spelling with
        # rows, which loses e.g. "Haußmann" when STT wrote "Hausmann" and
        # literal Hausmanns exist. Merge ALL spelling variants and re-check.
        try:
            widened = [c for c in lookup_by_name_all(name, limit=10)
                       if c.get("no") not in evaluated_nos]
        except Exception:
            log.exception("widened name search failed; keeping first-pass result")
            widened = []
        survivors = evaluate(widened)

    ring_contact = get_contact_by_no(contact_no) if contact_no else None
    if ring_contact and ring_contact.get("no") not in {c.get("no") for c, _ in survivors}:
        ring_verified, matched = match_identity_factors(
            ring_contact,
            date_of_birth=date_of_birth,
            phone_number=phone_number,
            customer_number=customer_number,
            postal_code=postal_code,
            phone_matches=phone_matches,
        )
        if ring_verified:  # full rule: >=2 factors, DOB gated
            survivors.append((ring_contact, matched))

    # Factor NAMES only — never values (PII).
    log.info("identity resolve: name=%s kn=%s survivors=%d factors=%s",
             bool(name), bool(contact_no), len(survivors), [m for _, m in survivors])
    if len(survivors) != 1:
        return None, []  # none matched, or ambiguous — identical outcome (§10.1)
    return survivors[0]


@app.post("/verify-caller", response_model=VerifyOut)
def verify_caller(
    body: VerifyIn,
    authorization: str | None = Header(default=None),
) -> VerifyOut:
    """Verify a caller by spoken name + birth date (+ optional Kundennummer/PLZ).

    §10.1 semantics: the NAME selects candidate records (contains-match) and
    counts as the first factor; at least one more factor must match — the birth
    date whenever BC has one on file (the no-DOB carve-out mirrors
    /create-request). Exactly ONE candidate may survive; none or several yield
    the same opaque failure (never reveal whether a record exists, §10.1).

    On success the response carries the §9 variables for the rest of the call
    (customer_number, permission_to_order_again, last_ordered_items, …) —
    the name-flow equivalent of what /lookup-caller loads on ring."""
    _check_auth(authorization)
    fail = VerifyOut(verified=False, message=_VERIFY_FAIL_MSG)
    if not body.name.strip() or not body.date_of_birth.strip():
        return fail

    try:
        contact, _ = _resolve_and_verify(
            name=body.name,
            contact_no=body.contact_no,
            date_of_birth=body.date_of_birth,
            phone_number=body.phone_number,
            customer_number=body.customer_number,
            postal_code=body.postal_code,
        )
    except BCConfigError as e:
        raise HTTPException(status_code=500, detail=f"config: {e}") from e
    except BCAuthError as e:
        raise HTTPException(status_code=502, detail=f"bc auth: {e}") from e
    except Exception as e:
        log.exception("verify-caller BC lookup failed")
        raise HTTPException(status_code=502, detail=str(e)) from e

    if not contact:
        return fail  # none matched, or ambiguous — identical response either way
    elig = check_order_eligibility(contact.get("customer_no") or "")
    return VerifyOut(
        verified=True,
        name=contact["name"],
        contact_no=contact["no"],
        customer_number=contact.get("customer_no"),
        permission_to_order_again=elig["permission_to_order_again"],
        last_ordered_items=elig["last_ordered_items"],
        last_order_date=elig["last_order_date"],
        message=f"Identität bestätigt: {contact['name']}.",
    )


# --- /create-request ---------------------------------------------------------

_ITEM_LABELS = {"BATTERIES": "Batterien", "MICROPHONE_COVERS": "Mikrofonabdeckungen"}


def _verified_label(verified: bool | None) -> str:
    return "nicht geprüft" if verified is None else ("ja" if verified else "nein")


def _ticket_body(lines: list[tuple[str, object]], summary: str | None) -> str:
    """Labeled protocol lines + free-text summary (mirrors conn_zammad._build_body)."""
    out = [f"{k}: {v}" for k, v in lines if v not in (None, "")]
    if out:
        out.append("")
    out.append((summary or "").strip() or "(keine Zusammenfassung angegeben)")
    return "\n".join(out)


def _create_zammad_ticket(**kwargs) -> dict:
    """create_ticket with connector exceptions mapped to HTTP codes."""
    try:
        return create_ticket(**kwargs)
    except ZammadConfigError as e:
        raise HTTPException(status_code=500, detail=f"zammad config: {e}") from e
    except ZammadError as e:
        log.exception("create_ticket failed")
        raise HTTPException(status_code=502, detail=f"zammad: {e}") from e


def _create_spare_parts_request(body: CreateRequestIn) -> CreateRequestOut:
    """§11 order flow with server-side controls (§11.2/§18/§21), zero-trust
    toward the voice agent. Check order: cheap/leak-free first, identity
    verification before anything that could reveal account state."""
    # 1. Required fields for an order. Identity may come from the KN-number
    #    (copied from verify_caller) OR the spoken name — the server re-resolves
    #    either way, so a lost KN never blocks a legitimate order.
    if not ((body.contact_no or body.name) and body.date_of_birth and body.item and body.quantity is not None):
        return CreateRequestOut(
            created=False, denied=True, reason_code="INVALID_REQUEST",
            message="Für eine Bestellung werden Name, Geburtsdatum, "
                    "Artikel und Menge benötigt.",
        )

    # 2. Supported-item whitelist (pure check, zero I/O).
    item_key = classify_item(body.item)
    if not item_key:
        return CreateRequestOut(
            created=False, denied=True, reason_code="UNSUPPORTED_ITEM",
            message="Über diese Hotline können nur Batterien und "
                    "Mikrofonabdeckungen angefragt werden. Die Anfrage kann für "
                    "eine MED-EL Fachperson erfasst werden.",
        )

    # 3. Quantity cap (the max is public policy, safe to state).
    if not 1 <= body.quantity <= SPARE_MAX_QUANTITY:
        return CreateRequestOut(
            created=False, denied=True, reason_code="QUANTITY_EXCEEDED",
            message=f"Diese Menge kann nicht direkt bearbeitet werden (maximal "
                    f"{SPARE_MAX_QUANTITY} pro Anfrage). Die Anfrage kann als "
                    f"Rückrufwunsch für eine MED-EL Fachperson erfasst werden.",
        )

    # 4. Identity verification against BC (never trust the LLM's claimed state).
    #    Same resolution as /verify-caller: spoken name and/or KN-number, so a
    #    lost KN between tool calls never blocks a legitimate order.
    try:
        contact, matched = _resolve_and_verify(
            name=body.name,
            contact_no=body.contact_no,
            date_of_birth=body.date_of_birth,
            phone_number=body.phone_number,
            customer_number=body.customer_number,
            postal_code=body.postal_code,
        )
    except BCConfigError as e:
        raise HTTPException(status_code=500, detail=f"config: {e}") from e
    except Exception as e:
        # BC unreachable -> we cannot verify -> we cannot order safely (§18).
        log.exception("create-request identity resolution failed")
        raise HTTPException(status_code=502, detail=f"bc: {e}") from e

    if not contact:
        # Identical response whether no record matched or a factor failed
        # (§10.1: never reveal which, never confirm a record exists).
        return CreateRequestOut(
            created=False, denied=True, reason_code="NOT_VERIFIED",
            message="Die Identität konnte nicht sicher bestätigt werden. Eine "
                    "Bestellung ist so nicht möglich. Es kann ein Rückrufwunsch "
                    "erfasst werden.",
        )
    verified = True  # by construction of _resolve_and_verify

    # 5. Eligibility re-check (90-day recency) on BC's customer number.
    #    Fail-open, flagged (signed off 2026-07-13): unknown -> human verifies.
    recent = has_recent_order(contact.get("customer_no") or "")
    tags = ["fonio", "spare_parts"]
    if recent is None:
        elig_label = "UNGEPRÜFT – manuelle Prüfung erforderlich"
        tags.append("eligibility-unchecked")
    elif recent:
        # Known-ineligible: still record the request for a human decision
        # (§11.4 script promises capture), but flagged and NOT approved.
        elig_label = "nein – NICHT freigegeben, manuelle Prüfung erforderlich"
        tags.append("review-needed")
    else:
        elig_label = "ja"

    # 6. Duplicate open request? (fail-open on search errors, flagged)
    try:
        dups = search_open_tickets(
            phone_number=body.phone_number,
            contact_no=contact.get("no"),
            title_contains="[SPARE_PARTS]",
        )
    except ZammadError:
        log.exception("duplicate search failed; proceeding flagged")
        dups, tags = [], tags + ["dupcheck-failed"]
    if dups:
        return CreateRequestOut(
            created=False, denied=True, reason_code="DUPLICATE_OPEN",
            existing_request_number=dups[0].get("number"),
            message="Zu diesem Artikel liegt bereits eine offene Anfrage vor. "
                    "Es wird keine zweite Anfrage angelegt.",
        )

    # 7. Create the request ticket. Its number is the request number (§11.4).
    item_label = _ITEM_LABELS[item_key]
    ticket = _create_zammad_ticket(
        title=f"[SPARE_PARTS] {item_label} x{body.quantity} – {contact['no']}",
        body=_ticket_body(
            [
                ("Request-Typ", "SPARE_PARTS"),
                ("Anrufer / Caller", body.phone_number),
                ("BC Contact No.", contact.get("no")),
                ("BC Customer No.", contact.get("customer_no")),
                ("Identität verifiziert", _verified_label(verified)),
                ("Artikel", f'{item_key} ("{body.item}")'),
                ("Menge", f"{body.quantity} (max {SPARE_MAX_QUANTITY})"),
                ("Bestellberechtigung (90-Tage-Regel)", elig_label),
            ],
            body.summary,
        ),
        phone_number=body.phone_number,
        tags=",".join(tags),
        internal=body.internal,
    )
    number, ticket_id = ticket.get("number"), ticket.get("id")
    if recent:
        return CreateRequestOut(
            created=True, denied=True, reason_code="NOT_ELIGIBLE",
            request_number=number, ticket_id=ticket_id,
            message="Die Bestellung kann nicht direkt abgeschlossen werden. Die "
                    "Anfrage wurde zur Prüfung durch eine MED-EL Fachperson "
                    f"erfasst. Die Vorgangsnummer lautet {number}.",
        )
    return CreateRequestOut(
        created=True, request_number=number, ticket_id=ticket_id,
        message=f"Bestellung aufgenommen. Die Vorgangsnummer lautet {number}. "
                "Die Bestellung erfolgt vorbehaltlich Prüfung und Freigabe durch MED-EL.",
    )


def _create_generic_request(body: CreateRequestIn) -> CreateRequestOut:
    """CALLBACK / SUPPORT / COMPLAINT / VIGILANCE / URGENT_MEDICAL: never denied
    (a vigilance or medical report must always be filed — safety first; the
    others disclose nothing and order nothing). Verification runs best-effort
    only to annotate the ticket for the human agent."""
    verified: bool | None = None
    if body.contact_no and (body.date_of_birth or body.customer_number or body.postal_code):
        try:
            contact = get_contact_by_no(body.contact_no)
            if contact:
                verified, _ = match_identity_factors(
                    contact,
                    date_of_birth=body.date_of_birth,
                    phone_number=body.phone_number,
                    customer_number=body.customer_number,
                    postal_code=body.postal_code,
                    phone_matches=phone_matches,
                )
            else:
                verified = False
        except Exception:
            verified = None  # BC hiccup never blocks filing the request

    rt = body.request_type.value
    who = body.contact_no or body.phone_number or "Unbekannt"
    titles = {
        RequestType.CALLBACK: f"[CALLBACK] Rückruf erbeten – {body.phone_number or 'Unbekannt'}",
        RequestType.SUPPORT: f"[SUPPORT] {(body.summary or 'Supportanfrage').strip()[:60]}",
        RequestType.COMPLAINT: f"[COMPLAINT] Beschwerde – {who}",
        RequestType.VIGILANCE: f"[VIGILANCE] Vorkommnis-Meldung – {who}",
        RequestType.URGENT_MEDICAL: f"[URGENT_MEDICAL] Medizinischer Notfall-Rückruf – {who}",
    }
    urgent = body.request_type in (RequestType.VIGILANCE, RequestType.URGENT_MEDICAL)

    ticket = _create_zammad_ticket(
        title=titles[body.request_type],
        body=_ticket_body(
            [
                ("Request-Typ", rt),
                ("Anrufer / Caller", body.phone_number),
                ("BC Contact No.", body.contact_no),
                ("Identität verifiziert", _verified_label(verified)),
                ("Gewünschte Rückrufzeit", body.callback_time),
            ],
            body.summary,
        ),
        phone_number=body.phone_number,
        priority_id=ZAMMAD_URGENT_PRIORITY_ID if urgent else None,
        tags=f"fonio,{rt.lower()}",
        internal=body.internal,
    )
    number = ticket.get("number")
    prefix = "Das Anliegen wurde als dringend erfasst" if urgent else "Anliegen erfasst"
    return CreateRequestOut(
        created=True, request_number=number, ticket_id=ticket.get("id"),
        message=f"{prefix}. Die Vorgangsnummer lautet {number}.",
    )


@app.post("/create-request", response_model=CreateRequestOut)
def create_request(
    body: CreateRequestIn,
    authorization: str | None = Header(default=None),
) -> CreateRequestOut:
    """Mid-call assistant tool: file one typed request (§17) as a Zammad ticket.

    Business refusals return HTTP 200 with denied=true + reason_code (Fonio may
    hide 4xx bodies from the LLM); HTTP errors are reserved for auth/config/
    transport problems. The Zammad ticket number doubles as the request number
    the agent reads back to the caller (§11.4)."""
    _check_auth(authorization)
    if body.request_type is RequestType.SPARE_PARTS:
        return _create_spare_parts_request(body)
    return _create_generic_request(body)


@app.post("/log-call", response_model=CallLogOut)
def log_call(
    body: CallLogIn,
    authorization: str | None = Header(default=None),
) -> CallLogOut:
    """Create a Zammad ticket logging a finished call.

    Wired to Fonio's post-processing / Outbound API (Nachverarbeitung), so it
    runs after the call — no 5000 ms budget. Not on the live-call critical path.
    """
    _check_auth(authorization)
    try:
        ticket = create_call_ticket(
            phone_number=body.phone_number,
            name=body.name,
            customer=body.customer,
            summary=body.summary,
            contact_no=body.contact_no,
            bc_found=body.bc_found,
            title=body.title,
        )
    except ZammadConfigError as e:
        raise HTTPException(status_code=500, detail=f"zammad config: {e}") from e
    except ZammadError as e:
        log.exception("create_call_ticket failed")
        raise HTTPException(status_code=502, detail=f"zammad: {e}") from e

    return CallLogOut(
        created=True,
        ticket_id=ticket.get("id") if ticket else None,
        ticket_number=ticket.get("number") if ticket else None,
    )
