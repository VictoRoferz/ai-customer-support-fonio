"""FastAPI webhook server for Fonio AI agent.

Endpoints:
  POST /lookup-caller   { "phone_number": "+49..." }
  POST /lookup-by-name  { "name": "Max Mustermann" }
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv

load_dotenv()

from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from conn_business_central import (
    BCAuthError,
    BCConfigError,
    check_order_eligibility,
    lookup_by_name,
    lookup_by_phone,
    warmup,
)
from conn_zammad import (
    ZammadConfigError,
    ZammadError,
    create_call_ticket,
)
from conn_zammad import warmup as zammad_warmup

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


def _check_auth(authorization: str | None) -> None:
    if not WEBHOOK_SECRET:
        return
    expected = f"Bearer {WEBHOOK_SECRET}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


class PhoneLookupIn(BaseModel):
    phone_number: str = Field(..., description="Caller's phone number in any format")


class NameLookupIn(BaseModel):
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
    prompt's {{system variables}} — DO NOT rename without updating the prompt.

    Maps to prompt section 5: customer_found, name, customer_number,
    date_of_birth, phone_number, permission_to_order_again, last_ordered_items.
    Extra keys (contact_no, health_insurance_no) are for internal use / the
    Zammad protocol and are ignored by the prompt.
    """

    customer_found: bool
    name: str | None = None
    customer_number: str | None = None          # BC customerNo (for orders)
    date_of_birth: str | None = None
    phone_number: str | None = None             # echoed input
    permission_to_order_again: bool | None = None
    last_ordered_items: str = ""
    # internal / protocol only:
    contact_no: str | None = None               # BC KN-number (Zammad linkage)
    health_insurance_no: str | None = None      # reported, not a decision
    last_order_date: str | None = None


class CallLogIn(BaseModel):
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
        date_of_birth=contact["birth_date"],
        phone_number=body.phone_number,
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
