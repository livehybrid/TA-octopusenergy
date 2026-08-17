"""
Modular-input scheme execution smoke (docker/portainer backend only).

Runs each packaged input script under Splunk's own Python via `--scheme`. This
proves the vendored libraries (import_declare_test path bootstrap, requests,
solnlib, splunklib) all import INSIDE the container and the scripts emit valid
scheme XML, independent of splunkd scheduling them. It is the strongest check
that the python:3.7 AppInspect false-positive could never give you.

Needs `docker exec` into the Splunk container, so it self-skips when
SPLUNK_CONTAINER is not a reachable container (e.g. the live backend).
"""
from __future__ import annotations

import shutil

import pytest

from conftest import CONTAINER, docker_exec

APP = "TA-octopusenergy"
# scaffolder fills SCRIPTS from bin/*.py: {"carbon_intensity.py": "Carbon Intensity"}
SCRIPTS = {'agile_rates.py': 'agile_rates', 'intelligent_octopus.py': 'intelligent_octopus', 'live_readings.py': 'live_readings', 'meter_readings.py': 'meter_readings'}


def _container_reachable():
    if not shutil.which("docker"):
        return False
    p = docker_exec("true", timeout=15)
    return p[0] == 0


pytestmark = pytest.mark.skipif(
    not SCRIPTS or not _container_reachable(),
    reason=f"no scripts or container {CONTAINER!r} not reachable (non-docker backend)",
)


@pytest.mark.parametrize("script,title", list(SCRIPTS.items()))
def test_script_emits_scheme(splunk, script, title):
    rc, out, err = docker_exec(
        "/opt/splunk/bin/splunk", "cmd", "python",
        f"/opt/splunk/etc/apps/{APP}/bin/{script}", "--scheme",
    )
    assert rc == 0, f"{script} --scheme exited {rc}\nSTDOUT:\n{out}\nSTDERR:\n{err}"
    assert "<scheme>" in out, f"{script} did not emit a scheme:\n{out}\n{err}"
    if title:
        assert title in out, f"{script} scheme missing title '{title}':\n{out}"
