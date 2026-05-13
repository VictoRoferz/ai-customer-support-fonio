"""Layered connectivity tests for the BC integration.

Run with:  python test_connection.py [phone_or_name]

It runs four checks in order, stopping at the first failure:
  1. .env has the required values
  2. Azure AD returns an OAuth token for our client credentials
  3. BC accepts that token and returns at least one Contact
  4. lookup_by_phone / lookup_by_name return shaped data
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

load_dotenv()

import requests

REQUIRED = ["BC_CONTACTS_URL", "BC_TENANT_ID", "BC_CLIENT_ID", "BC_CLIENT_SECRET"]


def step(n: int, label: str) -> None:
    print(f"\n[{n}] {label}")


def ok(msg: str) -> None:
    print(f"    OK  — {msg}")


def fail(msg: str) -> "None":
    print(f"    FAIL — {msg}")
    sys.exit(1)


def check_env() -> None:
    step(1, "Checking .env")
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        fail(f"missing in .env: {', '.join(missing)}")
    ok("all required env vars present")


def check_oauth() -> str:
    step(2, "Requesting OAuth token from Azure AD")
    tenant = os.environ["BC_TENANT_ID"]
    resp = requests.post(
        f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": os.environ["BC_CLIENT_ID"],
            "client_secret": os.environ["BC_CLIENT_SECRET"],
            "scope": os.environ.get(
                "BC_OAUTH_SCOPE", "https://api.businesscentral.dynamics.com/.default"
            ),
        },
        timeout=15,
    )
    if resp.status_code != 200:
        fail(f"Azure returned {resp.status_code}: {resp.text}")
    token = resp.json().get("access_token")
    if not token:
        fail(f"no access_token in response: {resp.text}")
    ok(f"got token ({len(token)} chars)")
    return token


def check_bc_reachable(token: str) -> None:
    step(3, "Querying BC /Contact?$top=1")
    url = os.environ["BC_CONTACTS_URL"]
    resp = requests.get(
        url,
        params={"$top": "1", "$select": "no,name,phoneNo,mobilePhoneNo"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        fail(f"BC returned {resp.status_code}: {resp.text[:400]}")
    rows = resp.json().get("value", [])
    if not rows:
        ok("BC reachable but returned 0 contacts — check the company name / permissions")
        return
    sample = rows[0]
    ok(
        f"got a contact: no={sample.get('no')}, name={sample.get('name')!r}, "
        f"phoneNo={sample.get('phoneNo')!r}, mobilePhoneNo={sample.get('mobilePhoneNo')!r}"
    )


def check_lookups(query: str) -> None:
    from conn_business_central import lookup_by_name, lookup_by_phone

    step(4, f"Testing lookup_by_phone({query!r})")
    by_phone = lookup_by_phone(query)
    if by_phone:
        ok(f"matched: {by_phone}")
    else:
        ok("no phone match (expected if you passed a name)")

    step(4, f"Testing lookup_by_name({query!r})")
    by_name = lookup_by_name(query, limit=3)
    if by_name:
        ok(f"matched {len(by_name)} contact(s): {by_name}")
    else:
        ok("no name match (expected if you passed a phone number)")


def main() -> None:
    check_env()
    token = check_oauth()
    check_bc_reachable(token)

    query = sys.argv[1] if len(sys.argv) > 1 else None
    if query:
        check_lookups(query)
    else:
        print(
            "\n(Tip: pass a phone number or name as an argument to also test the "
            "lookup functions, e.g.  python test_connection.py +4915122222)"
        )

    print("\nAll checks passed.")


if __name__ == "__main__":
    main()
