# TA-octopusenergy

Octopus Energy add-on for Splunk. A UCC-based add-on that pulls energy data from
the [Octopus Energy Kraken API](https://developer.octopus.energy/) into Splunk
via four modular inputs:

- **agile_rates** — 30-minute Agile tariff unit rates.
- **meter_readings** — half-hourly meter consumption / usage.
- **live_readings** — live (OctoMini / Home Mini) readings.
- **intelligent_octopus** — Intelligent Octopus dispatch/charge slots.

## Compatibility

| Attribute | Value |
|-----------|-------|
| **Add-on version** | 1.0.x |
| **Python runtime** | 3.9, Splunk's long-term-support runtime (pinned) |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x (any release on the Python 3.9 runtime) |
| **Tested in CI** | Real-Splunk install/registration harness + AppInspect `cloud`, `future`, `private_victoria` on every push |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |

Splunk 9.3 through 10.1 default to Python 3.9, and 3.9 stays the LTS runtime on
10.2 and later, so an add-on clean on 3.9 runs unchanged across that range. This
add-on pins the runtime to 3.9 (`python.required = 3.9` on every input and REST
handler, `python.version = python3` as the Splunk ≤10.1 fallback) and pins its
vendored libraries to versions that stay 3.9-clean. It is not yet validated on
the opt-in Python 3.13 runtime introduced in Splunk 10.2.

## Testing

CI boots `splunk/splunk:10.0` in Docker on every push and runs a pytest suite
that proves the add-on installs and is enabled, all four modular inputs register
and expose their schemes on the Splunk 10 / Python 3.9 runtime, and no startup
import/init errors appear in `index=_internal`. AppInspect (`cloud`, `future`,
`private_victoria`) runs alongside.

The collectors are keyed (they authenticate to the Kraken API with account
credentials), so live end-to-end ingestion assertions are a planned follow-up
using a mock Kraken GraphQL upstream (no real account or energy data in CI).
