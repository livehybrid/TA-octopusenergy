"""Pagination and time helpers for the Octopus REST API (stdlib only).

The REST endpoints (consumption, standard-unit-rates) are Django REST Framework
page-number paginated:

    {"count": 192, "next": "https://api.octopus.energy/v1/...?page=2&page_size=50&...",
     "previous": null, "results": [...]}

`next` is an absolute URL carrying every original query parameter plus `page`,
and is null on the last page. The server caps page_size silently (agile rates
at 1500, consumption at 25000) and keeps the requested value in `next`, so a
caller cannot tell from its own request how many pages there will be: it must
follow `next` until it is null.

Kept free of Splunk imports so the logic can be exercised directly.
"""
import datetime
import urllib.parse

MAX_PAGES = 1000


def rebase_next(next_url, api_base):
    """Point a `next` link at the configured API base.

    The real API returns links on api.octopus.energy. When the add-on is
    pointed at a gateway or mock (local/octopus_api_base), following the link
    verbatim would leave that base, so the path and query are kept and the
    scheme, host and any path prefix come from api_base.
    """
    if not next_url:
        return None
    parts = urllib.parse.urlsplit(next_url)
    path = parts.path
    idx = path.find("/v1/")
    if idx > 0:
        path = path[idx:]
    url = api_base.rstrip("/") + path
    if parts.query:
        url += "?" + parts.query
    return url


def iter_pages(fetch_json, url, api_base=None, max_pages=MAX_PAGES):
    """Yield (page_number, result) for every result on every page.

    fetch_json(url) returns the decoded JSON body of one page. Stops when
    `next` is null. A repeated URL or more than max_pages pages raises, so a
    misbehaving upstream cannot loop the input forever.
    """
    seen = set()
    page = 0
    while url:
        if url in seen:
            raise RuntimeError("pagination loop: {} was already fetched".format(url))
        seen.add(url)
        page += 1
        if page > max_pages:
            raise RuntimeError("more than {} pages; giving up".format(max_pages))
        body = fetch_json(url) or {}
        for result in body.get("results") or []:
            yield page, result
        nxt = body.get("next")
        url = rebase_next(nxt, api_base) if (nxt and api_base) else nxt


def parse_api_time(value):
    """Parse an API timestamp ('...Z' or '...+01:00') to an aware UTC datetime.

    Python 3.9's fromisoformat does not accept 'Z', so it is rewritten first.
    A value with no offset is taken as UTC.
    """
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def utc_iso(dt):
    """Format a datetime as 'YYYY-MM-DDTHH:MM:SSZ' (naive is taken as UTC)."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(datetime.timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
