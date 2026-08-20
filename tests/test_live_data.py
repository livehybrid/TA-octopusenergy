"""
Live behaviour test — keyed collectors ingest end-to-end against a mock Kraken.

The docker harness runs a mock Octopus (Kraken) API (docker/mock) and points the
collectors at it via the app-local override file. This proves the fetch -> parse
-> write_event path (including the JWT auth flow) on the Splunk 10 / Python 3.9
runtime, with no real account and no real energy data. Closes the E3a keyed-
collector gap for this add-on.

Coverage here:
  - agile_rates       (public REST tariff endpoint -> octopusenergy:agile_rates)
  - intelligent_octopus (full Login -> account -> dispatches auth flow ->
                         octopusenergy:io:planneddispatch)
Meter-based collectors (meter_readings / live_readings) additionally need meter
discovery config; adding them is a follow-up.
"""
from __future__ import annotations

import os
import time
import urllib.parse

import pytest

# The mock Kraken runs on the TEST RUNNER. A remote Splunk (e.g. a live dev
# instance reached over the network) cannot connect back to it, so the mock
# tier only makes sense when Splunk is local to the runner (the CI docker
# harness). Real-API coverage for remote runs lives in test_live_real.py.
_mgmt_host = urllib.parse.urlparse(
    os.environ.get("SPLUNK_MGMT_URL", "https://127.0.0.1:8089")
).hostname or "127.0.0.1"
pytestmark = pytest.mark.skipif(
    _mgmt_host not in ("127.0.0.1", "localhost", "::1"),
    reason="mock upstream runs on the test runner; a remote Splunk cannot reach it (CI covers this tier)",
)

APP = "TA-octopusenergy"
NS = f"/servicesNS/nobody/{APP}"
ACCOUNT = "octomock"
INDEX = "main"


@pytest.fixture(scope="module")
def configured(splunk):
    # Account: the mock ignores the credentials; the collector Logs in (mutation)
    # and auto-populates account_number/token from the mock on first run.
    st, body = splunk.request(
        "POST", f"{NS}/ta_octopusenergy_account",
        data={"name": ACCOUNT, "account_password": "mock-password-ignored"},
    )
    assert st in (200, 201, 409), f"create account -> {st}: {body[:300]}"

    inputs = [
        ("agile_rates", {"name": "agile1", "account": ACCOUNT, "rate_code": "AGILE-TEST",
                         "index": INDEX, "interval": "30"}),
        ("intelligent_octopus", {"name": "io1", "account": ACCOUNT, "index": INDEX,
                                 "interval": "30"}),
    ]
    for kind, data in inputs:
        st, body = splunk.request("POST", f"{NS}/data/inputs/{kind}", data=data)
        assert st in (200, 201, 409), f"create {kind} -> {st}: {body[:300]}"
        splunk.request("POST", f"{NS}/data/inputs/{kind}/{data['name']}/enable")

    yield
    for kind, data in inputs:
        splunk.request("DELETE", f"{NS}/data/inputs/{kind}/{data['name']}")
    splunk.request("DELETE", f"{NS}/ta_octopusenergy_account/{ACCOUNT}")


def _wait_for(splunk, spl, timeout=120):
    deadline = time.time() + timeout
    hits = []
    while time.time() < deadline:
        hits = splunk.search(spl, earliest="-15m")
        if hits:
            return hits
        time.sleep(10)
    return hits


def _collector_diag(splunk):
    rows = splunk.search(
        "search index=_internal earliest=-15m "
        "(source=*ta-octopusenergy* OR source=*ta_octopusenergy* OR component=ExecProcessor) "
        "(octopus OR Octopus OR agile OR dispatch OR Kraken OR api.octopus) "
        "| head 25 | table _time component log_level _raw",
        earliest="-15m", count=25,
    )
    if not rows:
        return "(no ta-octopusenergy lines in index=_internal — the inputs likely never executed)"
    return "\n".join(f"  {r.get('component','')}/{r.get('log_level','')}: {r.get('_raw','')[:280]}" for r in rows)


def test_agile_rates_ingests(splunk, configured):
    hits = _wait_for(splunk, f'index={INDEX} sourcetype="octopusenergy:agile_rates" | spath')
    assert hits, "agile_rates produced no events. Collector log lines:\n" + _collector_diag(splunk)
    assert any(h.get("value_inc_vat") is not None for h in hits), f"unexpected agile payload: {hits[0]}"


def test_intelligent_octopus_ingests(splunk, configured):
    # Exercises the full auth flow: Login mutation -> account_number -> dispatches.
    hits = _wait_for(splunk, f'index={INDEX} sourcetype="octopusenergy:io:planneddispatch" | spath')
    assert hits, "intelligent_octopus produced no events. Collector log lines:\n" + _collector_diag(splunk)
    assert any(h.get("startDt") for h in hits), f"unexpected dispatch payload: {hits[0]}"
