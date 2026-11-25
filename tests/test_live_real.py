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


KEY_ACCOUNT = "realapikey"


@pytest.mark.skipif(not SECRET.startswith("sk_"), reason="OCTOPUS_SECRET is not an API key")
def test_add_on_logs_in_with_an_api_key(splunk):
    """The add-on itself logs in with an API key and reaches an authenticated query.

    An account holding only the key, plus the simplest authenticated input,
    makes the collector run obtainKrakenToken and then fill in account_number
    from the viewer query. account_number is read back over REST, which the
    collector never uses to read its own config. Before API-key support the
    collector sent the key as a password and Kraken rejected it.
    """
    st, body = splunk.request(
        "POST", f"{NS}/ta_octopusenergy_account",
        data={"name": KEY_ACCOUNT, "account_password": SECRET},
    )
    assert st in (200, 201), f"create account -> {st}: {body[:200]}"
    try:
        data = {"name": "realio", "account": KEY_ACCOUNT, "index": INDEX, "interval": "60"}
        st, body = splunk.request("POST", f"{NS}/data/inputs/intelligent_octopus", data=data)
        assert st in (200, 201, 409), f"create intelligent_octopus -> {st}: {body[:200]}"
        splunk.request("POST", f"{NS}/data/inputs/intelligent_octopus/realio/enable")

        number = ""
        deadline = time.time() + 240
        while time.time() < deadline:
            st, body = splunk.request(
                "GET", f"{NS}/ta_octopusenergy_account/{KEY_ACCOUNT}?output_mode=json")
            if st == 200:
                number = json.loads(body)["entry"][0]["content"].get("account_number", "")
                if number:
                    break
            time.sleep(10)
        assert number.startswith("A-"), (
            "the add-on never logged in with the API key: account_number was not "
            f"filled in within 240s (got {number!r})")
    finally:
        splunk.request("DELETE", f"{NS}/data/inputs/intelligent_octopus/realio")
        splunk.request("DELETE", f"{NS}/ta_octopusenergy_account/{KEY_ACCOUNT}")


# ------------------------------------------------------- real pagination
#
# The collectors follow `next` through octopus_paging.iter_pages. These checks
# drive that same helper against the real API with a small page_size, so a
# change in how Octopus paginates (link shape, caps, ordering) shows here.
# Structure and counts only: nothing from the account is printed or asserted
# by value.

import pathlib  # noqa: E402
import sys  # noqa: E402
import urllib.parse  # noqa: E402

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "package" / "bin"))
from octopus_paging import iter_pages  # noqa: E402

API = "https://api.octopus.energy"


def _fetch(auth=None):
    def fetch(url):
        req = urllib.request.Request(url, headers={"Authorization": auth} if auth else {})
        with urllib.request.urlopen(req, timeout=60) as r:
            body = json.load(r)
        fetch.bodies.append(body)
        return body
    fetch.bodies = []
    return fetch


def _check_pages(fetch, rows, field, step=1800):
    bodies = fetch.bodies
    assert len(bodies) > 1, f"expected several pages, got {len(bodies)}"
    count = bodies[0]["count"]
    assert len(rows) == count, f"followed pages yielded {len(rows)} rows, count says {count}"
    assert bodies[-1]["next"] is None, "last page still has a next link"
    links_keep_window = all(
        {"period_from", "period_to", "page", "page_size"} <= set(urllib.parse.parse_qs(urllib.parse.urlsplit(b["next"]).query))
        for b in bodies[:-1])
    assert links_keep_window, "a next link dropped the period or page_size parameters"
    starts = [r[field] for _, r in rows]
    assert len(set(starts)) == len(starts), "a slot appeared on two pages"
    from octopus_paging import parse_api_time
    times = sorted(parse_api_time(s).timestamp() for s in starts)
    gaps = sum(1 for a, b in zip(times, times[1:]) if b - a != step)
    return gaps


def test_real_agile_rates_paginate():
    """A four-day agile range at 50 per page spans four pages and is complete."""
    url = (f"{API}/v1/products/{RATE_CODE}/electricity-tariffs/E-1R-{RATE_CODE}-M/standard-unit-rates/"
           "?period_from=2026-11-01T00:00:00Z&period_to=2026-11-05T00:00:00Z&page_size=50")
    fetch = _fetch()
    rows = list(iter_pages(fetch, url, api_base=API))
    gaps = _check_pages(fetch, rows, "valid_from")
    assert len(rows) == 4 * 48 and gaps == 0, f"{len(rows)} slots with {gaps} gaps"


def _real_meter(token):
    q = ("query A($n: String!) { account(accountNumber: $n) { properties { "
         "electricityMeterPoints { mpan meters { serialNumber } } } } }")

    def gql(query, variables):
        req = urllib.request.Request(GRAPHQL, data=json.dumps({"query": query, "variables": variables}).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": token})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["data"]
    number = gql("query { viewer { accounts { number } } }", {})["viewer"]["accounts"][0]["number"]
    for prop in gql(q, {"n": number})["account"]["properties"]:
        for point in prop["electricityMeterPoints"]:
            for meter in point["meters"]:
                return point["mpan"], meter["serialNumber"]
    return None


@pytest.mark.skipif(not SECRET.startswith("sk_"), reason="OCTOPUS_SECRET is not an API key")
def test_real_consumption_paginates():
    """Real consumption, shaped like the collector's request but 100 per page."""
    token, errors = _kraken_login({"APIKey": SECRET})
    assert token, "login failed"
    meter = _real_meter(token["token"])
    if not meter:
        pytest.skip("account has no electricity meter")
    url = (f"{API}/v1/electricity-meter-points/{meter[0]}/meters/{meter[1]}/consumption/"
           "?period_from=2026-10-01T00:00:00&period_to=2026-10-08T00:00:00&page_size=100")
    fetch = _fetch(auth=token["token"])
    rows = list(iter_pages(fetch, url, api_base=API))
    if len(fetch.bodies) < 2:
        pytest.skip("meter has fewer than 100 readings in the week checked")
    # Real meters have outages, so gaps are allowed here; duplicates are not.
    _check_pages(fetch, rows, "interval_start")
