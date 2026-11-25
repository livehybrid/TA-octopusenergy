#!/usr/bin/env python3
"""
Mock Octopus Energy (Kraken) API for the integration test.

Deterministic, synthetic responses for the endpoints the collectors call, so the
four keyed inputs can be exercised end-to-end against a real Splunk with no real
account and no real energy data. The collectors are pointed here via the
app-local `local/octopus_api_base` override file (see octopus_modinput). Auth
tokens/passwords are accepted but ignored.

Surface:
  POST /v1/graphql/                                       -> dispatch on operationName
  GET  /v1/products/<code>/electricity-tariffs/.../standard-unit-rates/  -> agile rates
  GET  /v1/electricity-meter-points/<mpan>/meters/<serial>/consumption/  -> consumption
  GET  /v1/gas-meter-points/<mprn>/meters/<serial>/consumption/          -> consumption

Pure stdlib (no pip install in the mock container).
"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
import calendar
import json
import os
import threading
import time
import urllib.parse

ACCOUNT_NUMBER = "A-MOCK0001"
ELEC_MPAN = "1200012345678"
ELEC_SERIAL = "MOCKELEC01"
ELEC_DEVICE = "MOCK-DEVICE-01"


def _b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _mock_jwt():
    # A structurally-valid JWT the collector decodes with verify_signature=False;
    # only the `exp` claim is read (check_token_validity). Far-future so it never
    # looks expired during a test run.
    header = _b64url({"alg": "HS256", "typ": "JWT"})
    payload = _b64url({"sub": "mock", "exp": int(time.time()) + 315360000})
    return f"{header}.{payload}.mocksig"


def _graphql(op, variables=None):
    variables = variables or {}
    if op == "Login":
        return {"data": {"obtainKrakenToken": {
            "token": _mock_jwt(), "refreshToken": "mock-refresh-token",
            "refreshExpiresIn": int(time.time()) + 315360000}}}
    if op == "GetUser":
        return {"data": {"viewer": {"id": "U1", "preferredName": "Mock",
                                    "givenName": "Mock", "email": "mock@example.com",
                                    "accounts": [{"number": ACCOUNT_NUMBER}]}}}
    if op == "GetAccountInfo":
        return {"data": {"account": {
            "number": ACCOUNT_NUMBER, "status": "ACTIVE",
            "properties": [{
                "id": "P1", "address": "1 Mock Street", "postcode": "MO1 1CK",
                "electricityMeterPoints": [{
                    "__typename": "ElectricityMeterPointType", "mpan": ELEC_MPAN, "id": "EMP1",
                    "meters": [{
                        "id": "M1", "serialNumber": ELEC_SERIAL, "meterType": "SMETS2",
                        "smartDevices": [{"paymentMode": "CREDIT", "deviceId": ELEC_DEVICE}],
                    }],
                    "agreements": [],
                }],
                "gasMeterPoints": [],
            }],
        }}}
    if op == "GetSmartMeterTelemetry":
        return _telemetry(variables)
    if op and op.endswith("Dispatches"):  # plannedDispatches / completedDispatches
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {"data": {op: [{"startDt": now, "endDt": now}]}}
    return {"data": {}, "errors": [{"message": f"mock: unhandled operation {op}"}]}


SLOT = 1800  # half-hour slots, for both rates and consumption
MOCK_START = time.time()


def _floor(t, step):
    return int(t // step * step)


# The dataset is fixed relative to the moment the mock started, so every page
# of every request agrees on where the data begins. Only the newest end grows,
# a slot at a time, as a real meter's does.
#   consumption: a meter fitted DATA_DAYS before the mock started; a reading is
#                published once its half hour has ended.
#   agile rates: published from DATA_DAYS back up to 23:00 tomorrow (UTC); the
#                real API publishes the next day's rates after 16:00.
#   telemetry:   one reading every TELEMETRY_STEP seconds, starting
#                TELEMETRY_SINCE_HOURS before the mock started. The collector's
#                first window (48 to 45 hours back) is therefore empty, like a
#                Home Mini that was offline or only just fitted.
DATA_DAYS = 30
TELEMETRY_STEP = 600
TELEMETRY_SINCE_HOURS = 44
CONSUMPTION_START = _floor(MOCK_START - DATA_DAYS * 86400, SLOT)
AGILE_START = CONSUMPTION_START
TELEMETRY_START = _floor(MOCK_START - TELEMETRY_SINCE_HOURS * 3600, TELEMETRY_STEP)

# Real caps (measured 25 Nov 2026): standard-unit-rates silently caps page_size
# at 1500, consumption at 25000; the default page size is 100. The harness sets
# lower caps so an ordinary window spans several pages.
DEFAULT_PAGE_SIZE = 100
AGILE_MAX_PAGE_SIZE = int(os.environ.get("MOCK_AGILE_MAX_PAGE_SIZE", "1500"))
CONSUMPTION_MAX_PAGE_SIZE = int(os.environ.get("MOCK_CONSUMPTION_MAX_PAGE_SIZE", "25000"))

REQUESTS = []  # recorded for /_mock/requests (paths and op names only)
_LOCK = threading.Lock()


def _fmt(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def _parse_ts(v):
    """API-style timestamp -> epoch. Accepts Z, +HH:MM, fractions or no offset (UTC)."""
    v = v.strip().replace(" ", "T")
    offset = 0
    if v.endswith("Z"):
        v = v[:-1]
    elif len(v) > 19 and v[19] in "+-":
        sign = 1 if v[19] == "+" else -1
        hh, mm = v[20:].replace(":", "")[:2], v[20:].replace(":", "")[2:4] or "00"
        offset = sign * (int(hh) * 3600 + int(mm) * 60)
        v = v[:19]
    v = v[:19]
    return calendar.timegm(time.strptime(v, "%Y-%m-%dT%H:%M:%S")) - offset


def agile_last_end(now=None):
    now = time.time() if now is None else now
    tomorrow = _floor(now, 86400) + 86400
    return tomorrow + 23 * 3600  # the last published slot ends at 23:00 tomorrow


def consumption_last_end(now=None):
    now = time.time() if now is None else now
    return _floor(now, SLOT)


def _agile_value(t):
    return round(10 + (t // SLOT) % 37 * 0.5, 2)


def _consumption_value(t):
    return round(0.05 + (t // SLOT) % 23 * 0.011, 3)


def _paginate(handler, slots, make, max_page_size):
    """DRF page-number pagination over newest-first slots."""
    url = urllib.parse.urlsplit(handler.path)
    q = urllib.parse.parse_qs(url.query, keep_blank_values=True)
    try:
        page = int((q.get("page") or ["1"])[0])
        size = int((q.get("page_size") or [str(DEFAULT_PAGE_SIZE)])[0])
    except ValueError:
        return {"detail": "Invalid page."}, 404
    size = max(1, min(size, max_page_size))
    count = len(slots)
    pages = max(1, -(-count // size))
    if page < 1 or page > pages:
        return {"detail": "Invalid page."}, 404
    chunk = slots[(page - 1) * size: page * size]

    def link(n):
        params = {k: v[0] for k, v in q.items()}
        params["page"] = str(n)
        params["page_size"] = (q.get("page_size") or [None])[0] or None
        if params["page_size"] is None:
            del params["page_size"]
        if n == 1:
            del params["page"]
        host = handler.headers.get("Host") or "mock:8080"
        return "http://{}{}?{}".format(host, url.path, urllib.parse.urlencode(sorted(params.items())))

    return {
        "count": count,
        "next": link(page + 1) if page < pages else None,
        "previous": link(page - 1) if page > 1 else None,
        "results": [make(t) for t in chunk],
    }, 200


def _window(handler, first, last_end):
    """Slots (start times) in [period_from, period_to) within [first, last_end), newest first."""
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(handler.path).query)
    lo = _parse_ts(q["period_from"][0]) if q.get("period_from") else first
    hi = _parse_ts(q["period_to"][0]) if q.get("period_to") else last_end
    lo = max(lo, first)
    hi = min(hi, last_end)
    start = _floor(lo + SLOT - 1, SLOT)  # first slot starting at or after period_from
    return list(range(_floor(hi - SLOT, SLOT), start - 1, -SLOT)) if hi > start else []


def _agile_rates(handler):
    slots = _window(handler, AGILE_START, agile_last_end())

    def make(t):
        v = _agile_value(t)
        return {"value_exc_vat": round(v / 1.05, 4), "value_inc_vat": v,
                "valid_from": _fmt(t), "valid_to": _fmt(t + SLOT), "payment_method": None}
    return _paginate(handler, slots, make, AGILE_MAX_PAGE_SIZE)


def _consumption(handler):
    slots = _window(handler, CONSUMPTION_START, consumption_last_end())

    def make(t):
        return {"consumption": _consumption_value(t), "interval_start": _fmt(t),
                "interval_end": _fmt(t + SLOT)}
    return _paginate(handler, slots, make, CONSUMPTION_MAX_PAGE_SIZE)


def _telemetry(variables):
    if variables.get("deviceID") != ELEC_DEVICE:
        return {"data": {"smartMeterTelemetry": None},
                "errors": [{"message": "mock: unknown device"}]}
    lo = max(_parse_ts(variables["startDate"]), TELEMETRY_START)
    hi = min(_parse_ts(variables["endDate"]), time.time())
    t = _floor(lo + TELEMETRY_STEP - 1, TELEMETRY_STEP)
    rows = []
    while t <= hi:
        n = (t - TELEMETRY_START) // TELEMETRY_STEP
        rows.append({"readAt": _fmt(t), "consumption": "{:.3f}".format(1000 + n * 7.5),
                     "demand": str(300 + n % 50)})
        t += TELEMETRY_STEP
    return {"data": {"smartMeterTelemetry": rows}}


def _dataset():
    now = time.time()
    return {
        "slot_seconds": SLOT,
        "agile": {"first_valid_from": _fmt(AGILE_START), "last_valid_to": _fmt(agile_last_end(now)),
                  "max_page_size": AGILE_MAX_PAGE_SIZE},
        "consumption": {"first_interval_start": _fmt(CONSUMPTION_START),
                        "last_interval_end": _fmt(consumption_last_end(now)),
                        "max_page_size": CONSUMPTION_MAX_PAGE_SIZE},
        "telemetry": {"device": ELEC_DEVICE, "first_read_at": _fmt(TELEMETRY_START),
                      "step_seconds": TELEMETRY_STEP},
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record(self, entry):
        with _LOCK:
            REQUESTS.append(dict(entry, at=time.time()))

    def do_POST(self):  # noqa: N802
        if self.path == "/_mock/reset":
            with _LOCK:
                REQUESTS.clear()
            self._send({"ok": True})
            return
        if self.path.rstrip("/").endswith("/v1/graphql"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
                op, variables = body.get("operationName"), body.get("variables") or {}
            except Exception:
                op, variables = None, {}
            entry = {"method": "POST", "op": op}
            if op == "GetSmartMeterTelemetry":
                entry.update({k: variables.get(k) for k in ("deviceID", "startDate", "endDate")})
            self._record(entry)
            self._send(_graphql(op, variables))
            return
        self._send({"errors": [{"message": "mock: unhandled POST"}]}, status=404)

    def do_GET(self):  # noqa: N802
        p = self.path
        if p.startswith("/_mock/requests"):
            with _LOCK:
                self._send(list(REQUESTS))
            return
        if p.startswith("/_mock/dataset"):
            self._send(_dataset())
            return
        if p.startswith("/_mock/health"):
            self._send({"ok": True})
            return
        if "standard-unit-rates" in p:
            body, status = _agile_rates(self)
        elif "/consumption" in p:
            body, status = _consumption(self)
        elif p.rstrip("/") in ("/v1/graphql", ""):
            self._send({"data": {}})
            return
        else:
            self._send({"detail": "mock: not found", "path": p}, status=404)
            return
        self._record({"method": "GET", "path": p, "status": status})
        self._send(body, status=status)

    def log_message(self, fmt, *args):
        # One concise line per request so `docker compose logs mock` shows the
        # collectors reaching the mock (op names / paths, never real data).
        print(f"MOCK {self.command} {self.path}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
