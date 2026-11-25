"""
Pagination, checkpoint and live_readings tests against the paginating mock.

The mock (docker/mock/mock_kraken.py) paginates like the real REST API: Django
REST Framework page numbers, newest first, `next` as an absolute URL that keeps
every query parameter, and a silent page_size cap. The harness lowers the caps
(20 rates, 100 readings per page) so ordinary windows span many pages.

Expectations are built from /_mock/dataset (where the data starts and ends)
by arithmetic over half-hour slots, and multi-page behaviour is proved from
/_mock/requests. The collectors consult neither. Indexed events are read back
with `stats count by <slot>`, so a missing slot (a page not followed) and a
repeated slot (a window fetched twice) both show.
"""
from __future__ import annotations

import calendar
import datetime
import json
import os
import time
import urllib.parse
import urllib.request

import pytest

_mgmt_host = urllib.parse.urlparse(
    os.environ.get("SPLUNK_MGMT_URL", "https://127.0.0.1:8089")
).hostname or "127.0.0.1"
pytestmark = pytest.mark.skipif(
    _mgmt_host not in ("127.0.0.1", "localhost", "::1"),
    reason="mock upstream runs on the test runner; a remote Splunk cannot reach it",
)

APP = "TA-octopusenergy"
NS = f"/servicesNS/nobody/{APP}"
ACCOUNT = "octopage"
INDEX = "octo_paging"
MOCK = os.environ.get("OCTOPUS_MOCK_URL", "http://127.0.0.1:8080")
CKPT = f"{NS}/storage/collections/data/TA_octopusenergy_checkpointer"
SLOT = 1800

AGILE_CODE = "AGILE-PAGE"
AGILE_CODE_2 = "AGILE-PAGE2"
METER_SERIAL = "MOCKPAGE01"
DEVICE = "MOCK-DEVICE-01"

TODAY = datetime.datetime.combine(datetime.datetime.now(datetime.timezone.utc).date(), datetime.time())
AGILE_START = TODAY - datetime.timedelta(days=3)
AGILE_START_2 = TODAY - datetime.timedelta(days=1)


def _epoch(text):
    text = text.strip().replace(" ", "T").rstrip("Z")[:19]
    return calendar.timegm(time.strptime(text, "%Y-%m-%dT%H:%M:%S"))


def _fmt(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _mock(path):
    with urllib.request.urlopen(MOCK + path, timeout=10) as r:
        return json.load(r)


def _requests(match):
    return [r for r in _mock("/_mock/requests") if match(r)]


def _page_of(path):
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
    return int((q.get("page") or ["1"])[0])


def _query_of(path):
    return {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(path).query).items()}


def _checkpoint(splunk, key):
    rows = splunk.get_json(CKPT, query=json.dumps({"_key": key}))
    if not rows:
        return None
    return json.loads(rows[0]["state"])


def _slot_counts(splunk, source, field):
    rows = splunk.search(
        f'search index={INDEX} source="{source}" | spath {field} | stats count by {field}',
        earliest="-60d", latest="+7d", count=10000)
    return {r[field]: int(r["count"]) for r in rows}


def _diag(splunk, word):
    rows = splunk.search(
        f"search index=_internal earliest=-20m (source=*{word}* OR (component=ExecProcessor {word})) "
        "| head 30 | table _raw", earliest="-20m", count=30)
    return "\n".join("  " + r.get("_raw", "")[:300] for r in rows) or "  (no log lines)"


def _expected(first, last_end):
    return {_fmt(t) for t in range(first, last_end, SLOT)}


def _describe(counts, expected):
    missing = sorted(expected - set(counts))
    extra = sorted(set(counts) - expected)
    dups = sorted(k for k, v in counts.items() if v > 1)
    return (f"indexed {len(counts)} distinct (expected {len(expected)}); "
            f"missing {len(missing)} {missing[:3]}; extra {len(extra)} {extra[:3]}; "
            f"duplicated {len(dups)} {dups[:3]}")


@pytest.fixture(scope="module")
def configured(splunk):
    st, body = splunk.request("POST", "/services/data/indexes", data={"name": INDEX})
    assert st in (200, 201, 409), f"create index -> {st}: {body[:200]}"
    st, body = splunk.request(
        "POST", f"{NS}/ta_octopusenergy_account",
        data={"name": ACCOUNT, "account_password": "mock-password-ignored"})
    assert st in (200, 201, 409), f"create account -> {st}: {body[:300]}"
    inputs = [
        ("agile_rates", {"name": "agile_pg", "account": ACCOUNT, "rate_code": AGILE_CODE,
                         "start_date": AGILE_START.strftime("%Y-%m-%d %H:%M:%S"),
                         "index": INDEX, "interval": "20"}),
        ("meter_readings", {"name": "elec_pg", "account": ACCOUNT, "index": INDEX,
                            "meter": f"Electric#1200012345678#{METER_SERIAL}#", "interval": "10"}),
        ("live_readings", {"name": "live_pg", "account": ACCOUNT, "index": INDEX,
                           "meter": f"Electric#1200012345678#MOCKELEC01#{DEVICE}", "interval": "10"}),
    ]
    for kind, data in inputs:
        st, body = splunk.request("POST", f"{NS}/data/inputs/{kind}", data=data)
        assert st in (200, 201, 409), f"create {kind} -> {st}: {body[:300]}"
        splunk.request("POST", f"{NS}/data/inputs/{kind}/{data['name']}/enable")
    yield inputs
    for kind, data in inputs + [("agile_rates", {"name": "agile_pg2"})]:
        splunk.request("DELETE", f"{NS}/data/inputs/{kind}/{data['name']}")
    splunk.request("DELETE", f"{NS}/ta_octopusenergy_account/{ACCOUNT}")


# ---------------------------------------------------------------- agile rates

def _agile_settled(splunk, name, first, timeout=300):
    """Wait until the input has checkpointed at the mock's newest slot and indexed up to it."""
    key = f"agile_rates__{name}_start_time"
    deadline = time.time() + timeout
    counts, expected, ckpt = {}, set(), None
    while time.time() < deadline:
        ckpt = _checkpoint(splunk, key)
        last = _epoch(_mock("/_mock/dataset")["agile"]["last_valid_to"])
        if ckpt and _epoch(ckpt) == last:
            expected = _expected(first, last)
            counts = _slot_counts(splunk, f"agile_rates://{name}", "valid_from")
            if sum(counts.values()) >= len(expected):
                break
        time.sleep(10)
    return ckpt, counts, expected


def test_agile_multi_page_range_is_fully_ingested(splunk, configured):
    first = calendar.timegm(AGILE_START.timetuple())
    ckpt, counts, expected = _agile_settled(splunk, "agile_pg", first)
    assert ckpt, "agile_pg never saved a checkpoint\n" + _diag(splunk, "agile_rates")
    pages = sorted({_page_of(r["path"]) for r in _requests(
        lambda r: AGILE_CODE + "/" in r.get("path", "") and "E-1R-" + AGILE_CODE + "-M" in r["path"])})
    assert max(pages) > 1, f"the range fitted one page, so pagination was not exercised: pages {pages}"
    assert pages == list(range(1, max(pages) + 1)), f"pages requested: {pages}"
    assert set(counts) == expected and set(counts.values()) == {1}, _describe(counts, expected)


def test_agile_checkpoint_lands_after_the_last_page(splunk, configured):
    first = calendar.timegm(AGILE_START.timetuple())
    ckpt, counts, expected = _agile_settled(splunk, "agile_pg", first)
    newest = _mock("/_mock/dataset")["agile"]["last_valid_to"]
    assert ckpt and _epoch(ckpt) == _epoch(newest), f"checkpoint {ckpt!r}, newest published slot ends {newest}"


def test_agile_next_run_starts_at_the_checkpoint_and_adds_nothing(splunk, configured):
    first = calendar.timegm(AGILE_START.timetuple())
    ckpt, _, expected = _agile_settled(splunk, "agile_pg", first)
    assert ckpt, "no checkpoint"
    deadline = time.time() + 120
    later = []
    while time.time() < deadline:
        later = [r for r in _requests(lambda r: "E-1R-" + AGILE_CODE + "-M" in r.get("path", ""))
                 if _page_of(r["path"]) == 1 and _epoch(_query_of(r["path"])["period_from"]) == _epoch(ckpt)]
        if later:
            break
        time.sleep(10)
    starts = [_query_of(r["path"]).get("period_from") for r in _requests(
        lambda r: "E-1R-" + AGILE_CODE + "-M" in r.get("path", "") and _page_of(r["path"]) == 1)]
    assert later, f"no run started at the checkpoint {ckpt}; first-page period_from seen: {starts}"
    time.sleep(25)  # let one more run land
    counts = _slot_counts(splunk, "agile_rates://agile_pg", "valid_from")
    assert set(counts) == expected and set(counts.values()) == {1}, _describe(counts, expected)


def test_agile_inputs_keep_their_own_checkpoints(splunk, configured):
    # A second input created once the first has a checkpoint must start from its
    # own start_date, not from the first input's position.
    first = calendar.timegm(AGILE_START.timetuple())
    assert _agile_settled(splunk, "agile_pg", first)[0], "agile_pg never checkpointed"
    data = {"name": "agile_pg2", "account": ACCOUNT, "rate_code": AGILE_CODE_2,
            "start_date": AGILE_START_2.strftime("%Y-%m-%d %H:%M:%S"), "index": INDEX, "interval": "20"}
    st, body = splunk.request("POST", f"{NS}/data/inputs/agile_rates", data=data)
    assert st in (200, 201, 409), f"create agile_pg2 -> {st}: {body[:200]}"
    splunk.request("POST", f"{NS}/data/inputs/agile_rates/agile_pg2/enable")
    first2 = calendar.timegm(AGILE_START_2.timetuple())
    ckpt, counts, expected = _agile_settled(splunk, "agile_pg2", first2, timeout=180)
    starts = sorted({_query_of(r["path"]).get("period_from") for r in _requests(
        lambda r: "E-1R-" + AGILE_CODE_2 + "-M" in r.get("path", ""))})
    assert set(counts) == expected and set(counts.values()) == {1}, (
        _describe(counts, expected) + f"; agile_pg2 asked from {starts}")


# ------------------------------------------------------------- meter readings

def _meter_settled(splunk, timeout=360):
    key = "meter_readings__elec_pg_start_time"
    ds = _mock("/_mock/dataset")["consumption"]
    first = _epoch(ds["first_interval_start"])
    deadline = time.time() + timeout
    counts, expected, ckpt = {}, set(), None
    while time.time() < deadline:
        ckpt = _checkpoint(splunk, key)
        last = _epoch(_mock("/_mock/dataset")["consumption"]["last_interval_end"])
        if ckpt and _epoch(ckpt) == last:
            expected = _expected(first, last)
            counts = _slot_counts(splunk, "meter_readings://elec_pg", "interval_start")
            if sum(counts.values()) >= len(expected):
                break
        time.sleep(10)
    return ckpt, counts, expected


def _meter_requests():
    return _requests(lambda r: f"/meters/{METER_SERIAL}/consumption" in r.get("path", ""))


def test_meter_multi_page_range_is_fully_ingested(splunk, configured):
    ckpt, counts, expected = _meter_settled(splunk)
    assert ckpt, "elec_pg never saved a checkpoint\n" + _diag(splunk, "meter_readings")
    pages = sorted({_page_of(r["path"]) for r in _meter_requests()})
    assert max(pages) > 1, f"pagination not exercised: pages {pages}"
    assert set(counts) == expected and set(counts.values()) == {1}, _describe(counts, expected)


def test_meter_checkpoint_lands_after_the_last_page(splunk, configured):
    ckpt, _, _ = _meter_settled(splunk)
    newest = _mock("/_mock/dataset")["consumption"]["last_interval_end"]
    assert ckpt and _epoch(ckpt) == _epoch(newest), f"checkpoint {ckpt!r}, newest reading ends {newest}"


def test_meter_next_run_adds_nothing(splunk, configured):
    ckpt, _, expected = _meter_settled(splunk)
    assert ckpt, "no checkpoint"
    deadline = time.time() + 90
    later = []
    while time.time() < deadline:
        later = [r for r in _meter_requests()
                 if _page_of(r["path"]) == 1 and _epoch(_query_of(r["path"])["period_from"]) == _epoch(ckpt)]
        if later:
            break
        time.sleep(10)
    assert later, f"no run resumed from the checkpoint {ckpt}"
    time.sleep(15)
    _, counts, expected = _meter_settled(splunk, timeout=60)
    assert set(counts) == expected and set(counts.values()) == {1}, _describe(counts, expected)


# ------------------------------------------------------------- live readings

def test_live_readings_ingest_from_an_empty_first_window(splunk, configured):
    # The mock's telemetry starts 44 hours back, so the collector's first window
    # (48 to 45 hours back) is empty. The input must carry on from there.
    tel = _mock("/_mock/dataset")["telemetry"]
    first, step = _epoch(tel["first_read_at"]), tel["step_seconds"]
    deadline = time.time() + 480
    counts = {}
    while time.time() < deadline:
        counts = _slot_counts(splunk, "live_readings://live_pg", "readAt")
        if counts and max(_epoch(k) for k in counts) >= time.time() - 3 * step:
            break
        time.sleep(15)
    calls = _requests(lambda r: r.get("op") == "GetSmartMeterTelemetry" and r.get("deviceID") == DEVICE)
    assert counts, (f"live_readings indexed nothing ({len(calls)} telemetry calls for {DEVICE})\n"
                    + _diag(splunk, "live_readings"))
    last = max(_epoch(k) for k in counts)
    expected = {_fmt(t) for t in range(first, last + 1, step)}
    assert last >= time.time() - 3 * step, f"live_readings did not catch up: newest {_fmt(last)}"
    assert set(counts) == expected and set(counts.values()) == {1}, _describe(counts, expected)


def test_live_readings_fields(splunk, configured):
    rows = splunk.search(
        f'search index={INDEX} source="live_readings://live_pg" '
        'sourcetype="octopusenergy:live_meter_reading:Electric" | head 20 | table _raw',
        earliest="-3d", latest="+1d", count=20)
    assert rows, "no live readings"
    for row in rows:
        ev = json.loads(row["_raw"])
        assert isinstance(ev["consumption"], int), f"consumption not converted to int: {ev}"
        assert set(ev) == {"readAt", "consumption", "demand"}, sorted(ev)
