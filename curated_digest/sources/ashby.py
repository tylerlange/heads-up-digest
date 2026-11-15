"""Ashby job boards, via the public posting API.

    https://api.ashbyhq.com/posting-api/job-board/<board>?location=US%20-%20Remote

`location` is our filter, matched against a posting's primary and secondary
location names (repeat the parameter for several). The public API exposes
location *names* only. A careers page that filters by a location uuid is using
Ashby's internal id for one of these names; the task file records which, and
`jobboard.guard` turns a later rename into a broken source rather than silence.

Identity is Ashby's posting uuid.
"""
import json
import re
import time
import urllib.parse

from . import jobboard
from .base import SourceError

HOST = "api.ashbyhq.com"
PATH_RE = re.compile(r"^/posting-api/job-board/([A-Za-z0-9_.-]+)/?$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SHOWN_LOCATIONS = 3


def target(url) -> tuple[str, list[str]]:
    """(board, location names) from a task url."""
    u = urllib.parse.urlsplit(url)
    m = PATH_RE.match(u.path)
    if u.scheme != "https" or u.hostname != HOST or not m:
        raise SourceError(url, f"not an Ashby job-board url: {url!r}")
    locs = [v.strip() for v in urllib.parse.parse_qs(u.query).get("location", [])
            if v.strip()]
    return m.group(1), locs


def _locations(j) -> list[str]:
    names = [j.get("location")] + [s.get("location") for s in
                                   (j.get("secondaryLocations") or []) if isinstance(s, dict)]
    return [n for n in dict.fromkeys(names) if isinstance(n, str) and n]


def fetch(url, *, etag=None, modified=None, **_ignored):
    board, wanted = target(url)
    api = f"https://{HOST}/posting-api/job-board/{board}"
    blob, headers = jobboard.get_json(api, etag=etag, modified=modified)
    if blob is None:
        return None, headers
    jobs = blob.get("jobs") if isinstance(blob, dict) else None
    if not isinstance(jobs, list):
        raise SourceError(url, "response has no `jobs` list")

    wanted_set = set(wanted)
    kept = [j for j in jobs if isinstance(j, dict) and j.get("isListed", True)
            and (not wanted_set or wanted_set & set(_locations(j)))]
    if wanted:
        jobboard.guard(url, len(jobs), len(kept), f"location {wanted!r}")
    # Wanted locations travel with the payload so the digest can lead with the
    # one that matched rather than whichever office Ashby lists first.
    return json.dumps({"board": board, "wanted": wanted, "jobs": kept}).encode(), headers


def _display_location(j, wanted) -> str:
    names = _locations(j)
    names.sort(key=lambda n: n not in wanted)   # the matched location first
    shown = ", ".join(names[:SHOWN_LOCATIONS])
    extra = len(names) - SHOWN_LOCATIONS
    return f"{shown} +{extra}" if extra > 0 else shown


def parse(raw, source_id):
    try:
        blob = json.loads(raw)
        board, jobs, wanted = blob["board"], blob["jobs"], blob.get("wanted") or []
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise SourceError(source_id, f"not a filtered Ashby payload: {e}") from e

    now, items = int(time.time()), []
    for j in jobs:
        jid = str(j.get("id") or "")
        if not UUID_RE.match(jid):
            continue
        team = j.get("team") or j.get("department") or ""
        location = _display_location(j, wanted)
        items.append(jobboard.make_item(
            job_id=jid, source_id=source_id,
            url=f"https://jobs.ashbyhq.com/{board}/{jid}",
            title=j.get("title"), team=team, location=location,
            facts=[("Team", team), ("Department", j.get("department")),
                   ("Location", location), ("Workplace", j.get("workplaceType")),
                   ("Employment", j.get("employmentType"))],
            description=" ".join((j.get("descriptionPlain") or "").split()),
            published=j.get("publishedAt"), fetched_at=now))
    return items
