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
    lookup_by_name,
    lookup_by_phone,
    warmup,
)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("fonio-bc")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Pre-warm the BC OAuth token so the first Fonio call doesn't pay the ~2.8 s
    # handshake on the critical path (Fonio drops the webhook after 5000 ms).
    log.info("warming BC auth: %s", "ok" if warmup() else "deferred (will retry on first call)")
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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/lookup-caller", response_model=ContactOut)
def lookup_caller(
    body: PhoneLookupIn,
    authorization: str | None = Header(default=None),
) -> ContactOut:
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
        return ContactOut(found=False)
    return ContactOut(
        found=True,
        name=contact["name"],
        first_name=contact["first_name"],
        surname=contact["surname"],
        birth_date=contact["birth_date"],
        contact_no=contact["no"],
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
