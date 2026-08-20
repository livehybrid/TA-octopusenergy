"""
Real-API canary — pulls genuine data from api.octopus.energy.

Complements the deterministic mock suite (test_live_data.py): this proves the
collectors work against the *real* Octopus (Kraken) API and catches upstream
drift a mock can't (schema changes, product-code retirement, auth changes).

Two tiers:
  - agile_rates through the REAL collector: the tariff endpoint is public, so
    this runs wherever OCTOPUS_LIVE_CANARY=1 is set — no secrets required.
    Rates are future-dated, so searches span into the future.
  - credential validity: the exact obtainKrakenToken login the collectors run,
    with the credential from 1Password (in CI). Catches password/key rotation
    — the failure mode that silently killed live meter ingestion — without
    depending on Intelligent Octopus features the account may not have.

Wired as a NON-gating CI job so real-API flakiness never blocks a release.
"""
from __future__ import annotations


import json
import os
import time
import urllib.request

import pytest

APP = "TA-octopusenergy"
NS = f"/servicesNS/nobody/{APP}"
ACCOUNT = "realcanary"
INDEX = "main"
RATE_CODE = os.environ.get("OCTOPUS_AGILE_PRODUCT", "AGILE-24-10-01")

pytestmark = pytest.mark.skipif(
    os.environ.get("OCTOPUS_LIVE_CANARY", "") != "1",
    reason="OCTOPUS_LIVE_CANARY!=1 — real-API canary skipped",
)


@pytest.fixture(scope="module")
def configured(splunk):
    # agile_rates never authenticates (public tariff endpoint), but the input
    # schema requires an account reference — a placeholder satisfies it.
    st, body = splunk.request(
        "POST", f"{NS}/ta_octopusenergy_account",
        data={"name": ACCOUNT, "account_password": "unused-for-agile"},
    )
    assert st in (200, 201, 409), f"create account -> {st}: {body[:300]}"

    data = {"name": "realagile", "account": ACCOUNT, "rate_code": RATE_CODE,
            "index": INDEX, "interval": "60"}
    st, body = splunk.request("POST", f"{NS}/data/inputs/agile_rates", data=data)
    assert st in (200, 201, 409), f"create agile_rates -> {st}: {body[:300]}"
    splunk.request("POST", f"{NS}/data/inputs/agile_rates/realagile/enable")

    yield
    splunk.request("DELETE", f"{NS}/data/inputs/agile_rates/realagile")
    splunk.request("DELETE", f"{NS}/ta_octopusenergy_account/{ACCOUNT}")


def test_real_agile_rates_ingests(splunk, configured):
    # Agile rates are future-dated (tomorrow's half-hour slots), so the search
    # window must extend into the future.
    spl = f'index={INDEX} sourcetype="octopusenergy:agile_rates" | spath | head 50'
    deadline = time.time() + 180
    hits = []
    while time.time() < deadline:
        hits = splunk.search(spl, earliest="-2d", latest="+3d")
        if hits:
            break
        time.sleep(10)
    assert hits, (
        f"agile_rates produced no events from the REAL API (product {RATE_CODE}). "
        "If the product code was retired, update OCTOPUS_AGILE_PRODUCT."
    )
    assert any(h.get("value_inc_vat") is not None for h in hits), f"unexpected payload: {hits[0]}"


EMAIL = os.environ.get("OCTOPUS_EMAIL", "").strip()
SECRET = os.environ.get("OCTOPUS_SECRET", "").strip()
GRAPHQL = "https://api.octopus.energy/v1/graphql/"


def _kraken_login(input_obj):
    payload = {
        "operationName": "Login",
        "variables": {"input": input_obj},
        "query": "mutation Login($input: ObtainJSONWebTokenInput!) "
                 "{ obtainKrakenToken(input: $input) { refreshExpiresIn token } }",
    }
    req = urllib.request.Request(
        GRAPHQL, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        obj = json.load(r)
    token = (obj.get("data") or {}).get("obtainKrakenToken")
    errors = [e.get("message", "") for e in (obj.get("errors") or [])]
    return token, errors


@pytest.mark.skipif(not SECRET, reason="OCTOPUS_SECRET not set")
def test_real_credentials_valid():
    """The credential in 1Password still authenticates against Kraken.

    This is the exact mutation the collectors run (get_new_access_token); a
    stale credential here is exactly what silently killed live meter ingestion
    (data: null -> the pre-fix NoneType crash). Accepts either credential
    form: an API key, or the account password paired with OCTOPUS_EMAIL.
    """
    attempts = [({"APIKey": SECRET}, "APIKey")]
    if EMAIL:
        attempts.insert(0, ({"email": EMAIL, "password": SECRET}, "email+password"))
    failures = []
    for input_obj, label in attempts:
        token, errors = _kraken_login(input_obj)
        if token and token.get("token"):
            return
        failures.append(f"{label}: {errors or 'no token returned'}")
    pytest.fail("Kraken rejected the stored credential in every form — rotate it. " + " | ".join(failures))
