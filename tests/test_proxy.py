"""
The proxy on the Configuration page's Proxy tab is the route the collectors
take to the API, and only while it is enabled.

docker-compose.yml runs a Squid forward proxy (docker/proxy) beside Splunk and
the mock Kraken: port 3128 open, 3129 with basic auth. The proxy is configured
through the add-on's UCC settings handler, the endpoint the Proxy tab saves to,
so the password is stored encrypted and read back the way the collectors read
it.

Three phases, one after the other (the proxy is a global setting):
  open      proxy enabled on 3128
  auth      proxy enabled on 3129 with a user name and password
  disabled  Enable unticked, host and port left filled in
In each phase an agile_rates input (REST GET, its own rate code) and an
intelligent_octopus input (GraphQL POSTs) run once. The open phase also asks
the Meter dropdown's handler for meters.

What a request did is read from two places the add-on never consults: the
proxy's own access log (`docker logs`, PROXYLOG lines) and the address the
mock recorded the request coming from (the proxy's container, or Splunk's).

The mock is plain HTTP, so the proxy sees each request in full (absolute URL).
An HTTPS target goes through the same proxies setting as a CONNECT tunnel, and
the proxy then logs only CONNECT host:443 (the real API; not reachable in CI).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.parse
import urllib.request
import uuid

import pytest

from conftest import CONTAINER, docker_exec

_mgmt_host = urllib.parse.urlparse(
    os.environ.get("SPLUNK_MGMT_URL", "https://127.0.0.1:8089")).hostname or "127.0.0.1"
pytestmark = pytest.mark.skipif(
    _mgmt_host not in ("127.0.0.1", "localhost", "::1"),
    reason="the proxy and mock run beside a local docker Splunk (CI covers this tier)",
)

APP = "TA-octopusenergy"
NS = f"/servicesNS/nobody/{APP}"
SETTINGS = f"{NS}/ta_octopusenergy_settings/proxy"
ACCOUNT = "octoproxy"
INDEX = "main"
PROXY_CONTAINER = os.environ.get("PROXY_CONTAINER", "ta_octopusenergy_proxy")
PROXY_HOST = "proxy"  # its name on the compose network
OPEN_PORT, AUTH_PORT = "3128", "3129"
# Matches docker/proxy/e2e-passwd. '@' and ':' must survive the proxy URL.
PROXY_USER, PROXY_PASSWORD = "e2euser", "e2e-p@ss:word"
MOCK = "http://127.0.0.1:8080"
MOCK_FROM_SPLUNK = "http://mock:8080"

PHASES = {
    "open": {"proxy_enabled": "1", "proxy_type": "http", "proxy_url": PROXY_HOST,
             "proxy_port": OPEN_PORT, "proxy_username": "", "proxy_password": "", "proxy_rdns": "0"},
    "auth": {"proxy_enabled": "1", "proxy_type": "http", "proxy_url": PROXY_HOST,
             "proxy_port": AUTH_PORT, "proxy_username": PROXY_USER, "proxy_password": PROXY_PASSWORD,
             "proxy_rdns": "0"},
    "disabled": {"proxy_enabled": "0", "proxy_type": "http", "proxy_url": PROXY_HOST,
                 "proxy_port": OPEN_PORT, "proxy_username": "", "proxy_password": "", "proxy_rdns": "0"},
}


def _ip(container):
    out = subprocess.run(
        ["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", container],
        capture_output=True, text=True, timeout=30)
    ips = out.stdout.split()
    assert out.returncode == 0 and ips, f"no address for {container}: {out.stderr}"
    return ips[0]


def proxy_log():
    """Every PROXYLOG line the proxy has written, parsed to dicts, in order."""
    out = subprocess.run(["docker", "logs", PROXY_CONTAINER], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, f"docker logs {PROXY_CONTAINER}: {out.stderr[-300:]}"
    rows = []
    for line in (out.stdout + "\n" + out.stderr).splitlines():
        if line.startswith("PROXYLOG "):
            rows.append(dict(kv.split("=", 1) for kv in line.split()[1:] if "=" in kv))
    return rows


def mock_requests():
    with urllib.request.urlopen(MOCK + "/_mock/requests", timeout=30) as r:
        return json.load(r)


def _wait(cond, timeout, step=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = cond()
        if got:
            return got
        time.sleep(step)
    return cond()


def _set_proxy(splunk, values):
    st, body = splunk.request("POST", SETTINGS, data=values, params={"output_mode": "json"})
    assert st == 200, f"save Proxy tab -> {st}: {body[:400]}"


def _through_proxy(port, path, auth=None):
    """curl from the Splunk container through the proxy; returns the HTTP code."""
    proxy = f"http://{PROXY_HOST}:{port}"
    cmd = ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-x", proxy]
    if auth:
        cmd += ["--proxy-user", auth]
    rc, out, err = docker_exec(*cmd, MOCK_FROM_SPLUNK + path, timeout=60)
    return out.strip()


def _search(splunk, source, sourcetype):
    return splunk.search(f'search index={INDEX} source="{source}" sourcetype="{sourcetype}" | head 5',
                         earliest="-3d", latest="+3d")


def _run_phase(splunk, name):
    _set_proxy(splunk, PHASES[name])
    log_from = len(proxy_log())
    started = time.time()
    rate = f"AGILE-PX{name.upper()}"
    agile, io = f"px_agile_{name}", f"px_io_{name}"
    out = {"rate": rate, "agile": f"agile_rates://{agile}", "io": f"intelligent_octopus://{io}"}

    if name == "open":
        # The Meter dropdown's handler logs in and lists meters from inside
        # splunkd; nothing else is calling GraphQL yet in this phase.
        st, body = splunk.request("GET", f"{NS}/ta_octopusenergy_account_info",
                                  params={"account": ACCOUNT, "output_mode": "json"})
        out["dropdown"] = (st, body[:300])
        out["dropdown_log"] = proxy_log()[log_from:]
        out["dropdown_mock"] = [r for r in mock_requests() if r["at"] >= started and r.get("op")]

    inputs = [("agile_rates", {"name": agile, "account": ACCOUNT, "rate_code": rate,
                               "index": INDEX, "interval": "3600"}),
              ("intelligent_octopus", {"name": io, "account": ACCOUNT, "index": INDEX, "interval": "3600"})]
    try:
        for kind, data in inputs:
            st, body = splunk.request("POST", f"{NS}/data/inputs/{kind}", data=data)
            assert st in (200, 201), f"create {kind}/{data['name']} -> {st}: {body[:300]}"
            splunk.request("POST", f"{NS}/data/inputs/{kind}/{data['name']}/enable")
        out["agile_events"] = _wait(lambda: _search(splunk, out["agile"], "octopusenergy:agile_rates"), 240, 10)
        out["io_events"] = _wait(lambda: _search(splunk, out["io"], "octopusenergy:io:planneddispatch"), 180, 10)
        time.sleep(5)  # let the last page of the run land in both logs
    finally:
        for kind, data in inputs:
            splunk.request("POST", f"{NS}/data/inputs/{kind}/{data['name']}/disable")
            splunk.request("DELETE", f"{NS}/data/inputs/{kind}/{data['name']}")
    out["log"] = proxy_log()[log_from:]
    reqs = [r for r in mock_requests() if r["at"] >= started]
    out["mock_agile"] = [r for r in reqs if rate in (r.get("path") or "")]
    out["mock_graphql"] = [r for r in reqs if r.get("op")]
    return out


@pytest.fixture(scope="module")
def phases(splunk):
    st, body = splunk.request("POST", f"{NS}/ta_octopusenergy_account",
                              data={"name": ACCOUNT, "account_password": "mock-password-ignored"})
    assert st in (200, 201, 409), f"create account -> {st}: {body[:300]}"
    ips = {"proxy": _ip(PROXY_CONTAINER), "splunk": _ip(CONTAINER)}
    try:
        yield {"ips": ips, **{name: _run_phase(splunk, name) for name in PHASES}}
    finally:
        _set_proxy(splunk, {"proxy_enabled": "0", "proxy_url": "", "proxy_port": "", "proxy_username": "",
                            "proxy_password": "", "proxy_rdns": "0"})
        splunk.request("DELETE", f"{NS}/ta_octopusenergy_account/{ACCOUNT}")


def _diag(phase, splunk=None):
    lines = [f"  {r.get('port')} {r.get('status')} {r.get('method')} {r.get('url')} user={r.get('user')}"
             for r in phase["log"][-15:]]
    agile = [(r.get("client"), r.get("path", "")[:60]) for r in phase["mock_agile"][:5]]
    gql = [(r.get("client"), r.get("op")) for r in phase["mock_graphql"][:8]]
    text = (f"\nproxy log in this phase ({len(phase['log'])} lines, last 15):\n" + "\n".join(lines)
            + f"\nmock agile requests (client, path): {agile}\nmock GraphQL requests (client, op): {gql}")
    if splunk is not None:
        rows = splunk.search(
            "search index=_internal earliest=-20m (source=*agile_rates* OR source=*intelligent_octopus*) "
            "(proxy OR Proxy OR error OR ERROR OR rror) | head 20 | table _raw", earliest="-20m", count=20)
        text += "\ncollector log:\n" + "\n".join("  " + r.get("_raw", "")[:300] for r in rows)
    return text


def _relayed(log, needle, port, user="-"):
    return [r for r in log if needle in r.get("url", "") and r.get("port") == port
            and r.get("status", "").endswith("/200") and r.get("user") == user]


@pytest.mark.parametrize("name,port,user", [("open", OPEN_PORT, "-"), ("auth", AUTH_PORT, PROXY_USER)])
def test_agile_rates_go_through_the_proxy(phases, splunk, name, port, user):
    p = phases[name]
    assert p["agile_events"], f"{p['agile']} indexed nothing" + _diag(p, splunk)
    assert p["mock_agile"], "the mock never saw this input's rate code; the run did not happen" + _diag(p, splunk)
    hits = _relayed(p["log"], f"/products/{p['rate']}/", port, user)
    assert hits, f"no agile_rates request for {p['rate']} in the proxy's log on :{port} as {user}" + _diag(p, splunk)
    direct = [r for r in p["mock_agile"] if r.get("client") != phases["ips"]["proxy"]]
    assert not direct, (f"{len(direct)} of {len(p['mock_agile'])} agile requests reached the mock directly "
                        f"from {sorted({r.get('client') for r in direct})} (proxy is {phases['ips']['proxy']})"
                        + _diag(p))


@pytest.mark.parametrize("name,port,user", [("open", OPEN_PORT, "-"), ("auth", AUTH_PORT, PROXY_USER)])
def test_graphql_collector_goes_through_the_proxy(phases, splunk, name, port, user):
    p = phases[name]
    assert p["io_events"], f"{p['io']} indexed nothing" + _diag(p, splunk)
    ops = {r["op"] for r in p["mock_graphql"]}
    assert "plannedDispatches" in ops, f"the mock saw no dispatches query (ops {ops})" + _diag(p, splunk)
    hits = [r for r in _relayed(p["log"], "/v1/graphql", port, user) if r.get("method") == "POST"]
    assert hits, f"no GraphQL POST in the proxy's log on :{port} as {user}" + _diag(p, splunk)
    direct = [r for r in p["mock_graphql"] if r.get("client") != phases["ips"]["proxy"]]
    assert not direct, (f"GraphQL ops reached the mock directly: {[(r.get('client'), r['op']) for r in direct]}"
                        + _diag(p))


def test_meter_dropdown_handler_goes_through_the_proxy(phases):
    p = phases["open"]
    st, body = p["dropdown"]
    assert st == 200 and "metername" in body, f"Meter dropdown handler -> {st}: {body}"
    assert any(r["op"] == "GetAccountInfo" for r in p["dropdown_mock"]), p["dropdown_mock"]
    direct = [r for r in p["dropdown_mock"] if r.get("client") != phases["ips"]["proxy"]]
    assert not direct, f"the handler reached the mock directly: {direct}"
    assert _relayed(p["dropdown_log"], "/v1/graphql", OPEN_PORT), f"proxy log: {p['dropdown_log']}"


def test_auth_proxy_really_demands_credentials(phases):
    # Without this the auth phase could pass with no credentials sent at all.
    assert _through_proxy(AUTH_PORT, "/_mock/health") == "407"
    assert _through_proxy(AUTH_PORT, "/_mock/health", auth=f"{PROXY_USER}:{PROXY_PASSWORD}") == "200"
    denied = [r for r in phases["auth"]["log"] if r.get("status", "").endswith("/407")]
    assert not denied, f"the collectors were refused by the auth proxy: {denied[:5]}"


def test_disabled_proxy_is_not_used(phases, splunk):
    p = phases["disabled"]
    assert p["agile_events"] and p["io_events"], "the disabled phase collected nothing" + _diag(p, splunk)
    assert p["mock_agile"] and p["mock_graphql"], "the mock saw nothing in the disabled phase" + _diag(p)
    via_proxy = [r for r in p["log"] if "mock:8080" in r.get("url", "")]
    assert not via_proxy, f"requests went through a disabled proxy: {via_proxy[:5]}"
    clients = {r.get("client") for r in p["mock_agile"] + p["mock_graphql"]}
    assert clients == {phases["ips"]["splunk"]}, (
        f"with the proxy off every request should come from Splunk {phases['ips']['splunk']}, got {clients}")


def test_proxy_log_check_can_see_a_request():
    # Positive control for the disabled phase: a request that does go through
    # the proxy shows up to the same reader, so an empty result means something.
    probe = uuid.uuid4().hex
    assert _through_proxy(OPEN_PORT, f"/_mock/health?probe={probe}") == "200"
    assert _wait(lambda: [r for r in proxy_log() if probe in r.get("url", "")], 20, 2), \
        f"probe {probe} not in the proxy log"
