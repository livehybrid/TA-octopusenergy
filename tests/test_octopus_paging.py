"""Unit tests for the REST pagination helper (no Splunk needed)."""
import datetime
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "package" / "bin"))

from octopus_paging import iter_pages, parse_api_time, rebase_next, utc_iso  # noqa: E402

REAL = "https://api.octopus.energy"


def _pages(n, per=2):
    """A fake upstream of n pages, keyed by URL, newest first like the real API."""
    urls = [f"{REAL}/v1/x/?period_from=a"] + [f"{REAL}/v1/x/?page={i}&period_from=a" for i in range(2, n + 1)]
    bodies = {}
    for i, url in enumerate(urls):
        bodies[url] = {"count": n * per, "next": urls[i + 1] if i + 1 < n else None,
                       "results": [{"id": i * per + j} for j in range(per)]}
    return urls[0], bodies


def test_follows_next_until_null():
    first, bodies = _pages(4)
    fetched = []

    def fetch(url):
        fetched.append(url)
        return bodies[url]
    got = [r["id"] for _, r in iter_pages(fetch, first)]
    assert got == list(range(8))
    assert fetched == list(bodies)  # each page once, in order


def test_reports_page_numbers():
    first, bodies = _pages(3)
    assert [p for p, _ in iter_pages(bodies.get, first)] == [1, 1, 2, 2, 3, 3]


def test_next_is_rebased_onto_the_configured_base():
    first, bodies = _pages(2)
    mock = "http://mock:8080"
    seen = []

    def fetch(url):
        seen.append(url)
        return bodies[url.replace(mock, REAL)]
    list(iter_pages(fetch, first.replace(REAL, mock), api_base=mock))
    assert seen == [first.replace(REAL, mock), f"{mock}/v1/x/?page=2&period_from=a"]


def test_rebase_keeps_a_gateway_path_prefix():
    assert rebase_next(f"{REAL}/v1/a/?page=2", "https://gw.example/octopus/") == \
        "https://gw.example/octopus/v1/a/?page=2"
    assert rebase_next(None, REAL) is None


def test_a_page_loop_raises_instead_of_spinning():
    loop = {"u": {"results": [{"id": 1}], "next": "u"}}
    with pytest.raises(RuntimeError, match="loop"):
        list(iter_pages(loop.get, "u"))


def test_page_limit():
    def fetch(url):
        return {"results": [], "next": url + "x"}
    with pytest.raises(RuntimeError, match="pages"):
        list(iter_pages(fetch, "u", max_pages=5))


@pytest.mark.parametrize("text", ["2026-11-25T10:30:00Z", "2026-11-25T11:30:00+01:00",
                                  "2026-11-25T10:30:00", "2026-11-25 10:30:00"])
def test_parse_api_time_is_utc(text):
    assert parse_api_time(text) == datetime.datetime(2026, 11, 25, 10, 30, tzinfo=datetime.timezone.utc)


def test_utc_iso():
    aware = datetime.datetime(2026, 6, 1, 1, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=1)))
    assert utc_iso(aware) == "2026-06-01T00:00:00Z"
    assert utc_iso(datetime.datetime(2026, 6, 1)) == "2026-06-01T00:00:00Z"
