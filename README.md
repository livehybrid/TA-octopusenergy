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
| **Python runtime** | 3.13 on Splunk 10.2 and later, 3.9 on Splunk 10.1 and earlier |
| **Expected compatible** | Splunk Enterprise and Cloud 9.3+ and 10.x |
| **Tested in CI** | Real-Splunk install/registration harness + AppInspect `cloud`, `future`, `private_victoria` on every push |
| **Deployment roles** | Standalone, Distributed, Search Head Clustering |

Splunk 10.2 and later ship Python 3.9 and 3.13 side by side; this add-on sets
`python.required = 3.13`, so it runs on 3.13 there. Splunk 10.1 and earlier
ignore `python.required` and run it on 3.9 (`python.version = python3`), so the
code and vendored libraries stay 3.9-compatible (`requests < 2.33`,
`splunk-sdk < 3`). CI runs the integration tests on Splunk 10.0 (Python 3.9)
and 10.6 (Python 3.13) to hold both.

## Testing

CI boots `splunk/splunk:10.0` in Docker on every push and runs a pytest suite
that proves the add-on installs and is enabled, all four modular inputs register
and expose their schemes on the Splunk 10.0 (Python 3.9) and 10.6 (Python 3.13) runtimes, and no startup
import/init errors appear in `index=_internal`. AppInspect (`cloud`, `future`,
`private_victoria`) runs alongside.

The collectors are keyed (they authenticate to the Kraken API with account
credentials), so live end-to-end ingestion assertions are a planned follow-up
using a mock Kraken GraphQL upstream (no real account or energy data in CI).
