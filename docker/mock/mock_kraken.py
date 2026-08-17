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
import json
import time

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


def _graphql(op):
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
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {"data": {"smartMeterTelemetry": [
            {"readAt": now, "consumption": "12.345", "demand": "450"},
            {"readAt": now, "consumption": "12.400", "demand": "455"},
        ]}}
    if op and op.endswith("Dispatches"):  # plannedDispatches / completedDispatches
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return {"data": {op: [{"startDt": now, "endDt": now}]}}
    return {"data": {}, "errors": [{"message": f"mock: unhandled operation {op}"}]}


def _agile_rates():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"count": 2, "next": None, "previous": None, "results": [
        {"value_exc_vat": 14.5, "value_inc_vat": 15.225, "valid_from": now, "valid_to": now,
         "payment_method": None},
        {"value_exc_vat": 9.1, "value_inc_vat": 9.555, "valid_from": now, "valid_to": now,
         "payment_method": None},
    ]}


def _consumption():
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return {"count": 2, "next": None, "previous": None, "results": [
        {"consumption": 0.123, "interval_start": now, "interval_end": now},
        {"consumption": 0.456, "interval_start": now, "interval_end": now},
    ]}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        if self.path.rstrip("/").endswith("/v1/graphql"):
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                op = json.loads(raw).get("operationName")
            except Exception:
                op = None
            self._send(_graphql(op))
            return
        self._send({"errors": [{"message": "mock: unhandled POST"}]}, status=404)

    def do_GET(self):  # noqa: N802
        p = self.path
        if "standard-unit-rates" in p:
            self._send(_agile_rates())
        elif "/consumption" in p:
            self._send(_consumption())
        elif p.rstrip("/") in ("/v1/graphql", ""):
            self._send({"data": {}})
        else:
            self._send({"detail": "mock: not found", "path": p}, status=404)

    def log_message(self, fmt, *args):
        # One concise line per request so `docker compose logs mock` shows the
        # collectors reaching the mock (op names / paths, never real data).
        print(f"MOCK {self.command} {self.path}", flush=True)


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
