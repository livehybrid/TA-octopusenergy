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
  - meter_readings    (consumption from a meter fitted 30 days ago: empty years first)
live_readings additionally needs meter discovery config; adding it is a follow-up.
"""
from __future__ import annotations

import json
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
        # The mock meter only has the last 30 days of readings, so this input
        # starts several empty years back and must walk forward to reach them.
        ("meter_readings", {"name": "elec1", "account": ACCOUNT, "index": INDEX,
                            "meter": "Electric#1200012345678#MOCKELEC01#", "interval": "10"}),
    ]
    for kind, data in inputs:
        st, body = splunk.request("POST", f"{NS}/data/inputs/{kind}", data=data)
        assert st in (200, 201, 409), f"create {kind} -> {st}: {body[:300]}"
        splunk.request("POST", f"{NS}/data/inputs/{kind}/{data['name']}/enable")

    yield
    for kind, data in inputs:
        splunk.request("DELETE", f"{NS}/data/inputs/{kind}/{data['name']}")
    splunk.request("DELETE", f"{NS}/ta_octopusenergy_account/{ACCOUNT}")


def _wait_for(splunk, spl, timeout=120, earliest="-15m"):
    deadline = time.time() + timeout
    hits = []
    while time.time() < deadline:
        hits = splunk.search(spl, earliest=earliest)
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


def test_meter_readings_reach_data_after_empty_years(splunk, configured):
    # A fresh input starts at 2020-01-01 and asks a year at a time. Years with no
    # readings must move the checkpoint on; otherwise it re-asks for 2020 forever.
    hits = _wait_for(splunk, f'index={INDEX} sourcetype="octopusenergy:meter_reading:Electric" source="meter_readings://elec1" | spath',
                     timeout=300, earliest="-1d")  # readings are timestamped up to 90 min ago
    if not hits:
        rows = splunk.search(
            "search index=_internal earliest=-15m (source=*meter_readings* OR (component=ExecProcessor meter_readings)) "
            "| head 40 | table _raw", earliest="-15m", count=40)
        diag = "\n".join("  " + r.get("_raw", "")[:300] for r in rows) or "  (no meter_readings lines at all)"
        st, inp = splunk.request("GET", f"{NS}/data/inputs/meter_readings/elec1", params={"output_mode": "json"})
        pytest.fail("meter_readings never reached the meter's readings (stuck on an empty window?).\n"
                    f"input elec1 -> {st}: {inp[:400]}\nmeter_readings log lines:\n{diag}")
    assert any(h.get("consumption") is not None for h in hits), f"unexpected reading payload: {hits[0]}"


CKPT = f"{NS}/storage/collections/data/TA_octopusenergy_checkpointer"


def test_meter_checkpoint_key_has_no_slashes(splunk, configured):
    # Splunk 10.6.0.5 404s a by-id read of a KV _key containing '://', so a key
    # like 'meter_readings://elec1_start_time' is written but never read back.
    deadline = time.time() + 180
    keys = []
    while time.time() < deadline:
        rows = splunk.get_json(CKPT)
        keys = [r["_key"] for r in rows if "elec1" in r.get("_key", "")]
        if keys:
            break
        time.sleep(10)
    assert keys, "meter_readings never saved a checkpoint for elec1"
    assert all("/" not in k for k in keys), f"checkpoint keys contain '/': {keys}"


def test_meter_carries_over_an_old_style_checkpoint(splunk, configured):
    # A position saved under the old 'meter_readings://<name>_start_time' key
    # must be picked up, or every existing input restarts from 2020.
    seed = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 2 * 86400))
    old_key = "meter_readings://elec2_start_time"
    req_body = json.dumps({"_key": old_key, "state": json.dumps(seed)})
    st, body = splunk.request_json("POST", CKPT, req_body)
    assert st in (200, 201), f"seed old checkpoint -> {st}: {body[:200]}"
    data = {"name": "elec2", "account": ACCOUNT, "index": INDEX,
            "meter": "Electric#1200012345678#MOCKELEC01#", "interval": "10"}
    st, body = splunk.request("POST", f"{NS}/data/inputs/meter_readings", data=data)
    assert st in (200, 201, 409), f"create elec2 -> {st}: {body[:200]}"
    try:
        splunk.request("POST", f"{NS}/data/inputs/meter_readings/elec2/enable")
        deadline = time.time() + 180
        starts = []
        while time.time() < deadline:
            rows = splunk.search(
                'search index=_internal source=*meter_readings* "start_time=" '
                '| rex "start_time=(?<st>\\S+)" | search st=* | table _raw st', earliest="-15m", count=200)
            starts = [r["st"] for r in rows]  # the seed is unique to elec2
            if seed in starts:
                break
            time.sleep(10)
        assert seed in starts, f"elec2 did not start from the old checkpoint {seed}; starts seen: {sorted(set(starts))[:8]}"
    finally:
        splunk.request("DELETE", f"{NS}/data/inputs/meter_readings/elec2")
        for k in (old_key, "meter_readings__elec2_start_time"):
            splunk.request("DELETE", CKPT + "/" + urllib.parse.quote(k, safe=""))
