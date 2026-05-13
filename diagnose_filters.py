"""Probe what filter expressions this BC OData endpoint actually supports.

Sends a series of small, isolated $filter queries and reports the HTTP status
plus a short slice of the response body for each one. The output tells us
which operators/fields work so we can write a filter strategy that matches
this specific BC deployment.
"""

from __future__ import annotations

import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv()


def get_token() -> str:
    resp = requests.post(
        f"https://login.microsoftonline.com/{os.environ['BC_TENANT_ID']}/oauth2/v2.0/token",
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
    resp.raise_for_status()
    return resp.json()["access_token"]


def probe(label: str, token: str, params: dict[str, str]) -> None:
    url = os.environ["BC_CONTACTS_URL"]
    resp = requests.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    status = resp.status_code
    if status == 200:
        body = resp.json()
        n = len(body.get("value", []))
        sample = body["value"][0] if n else {}
        keys = ",".join(list(sample.keys())[:5])
        print(f"  [{status}] {label:50}  → {n} row(s)  {keys}")
    else:
        snippet = resp.text[:200].replace("\n", " ")
        print(f"  [{status}] {label:50}  → {snippet}")


def main() -> None:
    phone = sys.argv[1] if len(sys.argv) > 1 else "+4915122222"
    name = sys.argv[2] if len(sys.argv) > 2 else "Mutanen"

    token = get_token()
    print(f"Probing BC OData endpoint with phone={phone!r} name={name!r}\n")

    print("# Field reachability (does $select on this field 200?)")
    for field in [
        "phoneNo",
        "mobilePhoneNo",
        "phoneNo2",
        "mobilePhoneNo2",
        "privatPhoneNo",
        "privatMobilePhoneNo",
        "searchName",
        "firstName",
        "surname",
        "birthDate",
    ]:
        probe(f"$select={field}", token, {"$select": f"no,{field}", "$top": "1"})

    print("\n# Equality filters (one field at a time)")
    probe("phoneNo eq '<phone>'", token, {"$filter": f"phoneNo eq '{phone}'", "$top": "1"})
    probe("mobilePhoneNo eq '<phone>'", token, {"$filter": f"mobilePhoneNo eq '{phone}'", "$top": "1"})
    probe("phoneNo eq '<no-plus>'", token, {"$filter": f"phoneNo eq '{phone.lstrip('+')}'", "$top": "1"})

    print("\n# Equality with no quotes (matches the URL pattern you shared)")
    probe("phoneNo eq <phone>", token, {"$filter": f"phoneNo eq {phone}", "$top": "1"})

    print("\n# OR across two fields")
    probe(
        "phoneNo or mobilePhoneNo eq '<phone>'",
        token,
        {"$filter": f"phoneNo eq '{phone}' or mobilePhoneNo eq '{phone}'", "$top": "1"},
    )

    print("\n# Name filters")
    probe(f"searchName eq '{name.upper()}'", token, {"$filter": f"searchName eq '{name.upper()}'", "$top": "1"})
    probe(f"startswith(searchName, '{name.upper()[:4]}')", token, {"$filter": f"startswith(searchName, '{name.upper()[:4]}')", "$top": "1"})
    probe(f"name eq '{name}'", token, {"$filter": f"name eq '{name}'", "$top": "1"})
    probe(f"surname eq '{name}'", token, {"$filter": f"surname eq '{name}'", "$top": "1"})
    probe(f"contains(name, '{name}')", token, {"$filter": f"contains(name, '{name}')", "$top": "1"})


if __name__ == "__main__":
    main()
