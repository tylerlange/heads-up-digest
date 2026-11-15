"""Greenhouse job boards, via the public board API.

    https://boards-api.greenhouse.io/v1/boards/<board>/jobs?office_id=<id>

`office_id` is our filter, not Greenhouse's: the API ignores it, so it is
read off the url and applied here (comma-separate several). It is the same id
the employer's own careers page puts in its `?office=` parameter, so a filtered
careers url translates directly. With no `office_id` every posting is kept.

Identity is Greenhouse's numeric job id, which survives title edits, so an
edited posting is not re-announced as new.
"""
import json
import re
import time
import urllib.parse

from . import jobboard
from .base import SourceError

HOST = "boards-api.greenhouse.io"
PATH_RE = re.compile(r"^/v1/boards/([A-Za-z0-9_-]+)/jobs/?$")
ROLE_START_RE = re.compile(
    r"\b(About (the|this) (role|team|program|position)|The role|Responsibilities)\b", re.I)


def target(url) -> tuple[str, set[str]]:
    """(board, office ids) from a task url."""
    u = urllib.parse.urlsplit(url)
    m = PATH_RE.match(u.path)
    if u.scheme != "https" or u.hostname != HOST or not m:
        raise SourceError(url, f"not a Greenhouse board url: {url!r}")
    q = urllib.parse.parse_qs(u.query)
    offices = {o.strip() for v in q.get("office_id", []) for o in v.split(",")
               if o.strip()}
    if any(not o.isdigit() for o in offices):
        raise SourceError(url, f"office_id must be numeric: {sorted(offices)}")
    return m.group(1), offices


def fetch(url, *, etag=None, modified=None, **_ignored):
    board, offices = target(url)
    api = f"https://{HOST}/v1/boards/{board}/jobs?content=true"
    blob, headers = jobboard.get_json(api, etag=etag, modified=modified)
    if blob is None:
        return None, headers
    jobs = blob.get("jobs") if isinstance(blob, dict) else None
    if not isinstance(jobs, list):
        raise SourceError(url, "response has no `jobs` list")

    def in_office(j):
        return any(str(o.get("id")) in offices for o in (j.get("offices") or [])
                   if isinstance(o, dict))

    kept = [j for j in jobs if isinstance(j, dict) and (not offices or in_office(j))]
    if offices:
        jobboard.guard(url, len(jobs), len(kept), f"office_id {','.join(sorted(offices))}")
    # The matched offices travel with the payload: a posting in the remote
    # office can still carry "San Francisco, CA" as its location string, and
    # the digest should say why it is there.
    return json.dumps({"board": board, "offices": sorted(offices),
                       "jobs": kept}).encode(), headers


def parse(raw, source_id):
    try:
        blob = json.loads(raw)
        board, jobs, offices = blob["board"], blob["jobs"], set(blob.get("offices") or [])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise SourceError(source_id, f"not a filtered Greenhouse payload: {e}") from e

    now, items = int(time.time()), []
    for j in jobs:
        jid = j.get("id")
        if not isinstance(jid, int):
            continue
        depts = [d.get("name") for d in (j.get("departments") or [])
                 if isinstance(d, dict) and d.get("name")]
        matched = [o.get("name") for o in (j.get("offices") or [])
                   if isinstance(o, dict) and str(o.get("id")) in offices and o.get("name")]
        listed = (j.get("location") or {}).get("name", "")
        location = "; ".join(dict.fromkeys(matched + ([listed] if listed else [])))
        workplace = next((m.get("value") for m in (j.get("metadata") or [])
                          if isinstance(m, dict) and m.get("name") == "Location Type"), "")
        text = jobboard.html_to_text(j.get("content"))
        # Every posting on a large employer's board opens with the same company
        # paragraph, which would spend a third of curation's 1200-char window
        # on nothing.
        m = ROLE_START_RE.search(text) if text.startswith("About ") else None
        if m and 0 < m.start() < 1500:
            text = text[m.start():]
        items.append(jobboard.make_item(
            job_id=jid, source_id=source_id,
            # Built from the id, never taken from the payload: the link is the
            # one thing in the bullet a reader acts on without reading.
            url=f"https://job-boards.greenhouse.io/{board}/jobs/{jid}",
            title=j.get("title"), team=", ".join(depts), location=location,
            facts=[("Team", ", ".join(depts)), ("Location", location),
                   ("Workplace", workplace)],
            description=text,
            published=j.get("first_published") or j.get("updated_at"),
            fetched_at=now))
    return items
