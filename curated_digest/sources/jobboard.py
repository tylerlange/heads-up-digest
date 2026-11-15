"""What the two job-board sources share: a capped JSON fetch, the filter guard,
and turning a posting into an Item.

Both boards publish their whole catalogue in one response, with descriptions
inline (Greenhouse with `content=true`, Ashby always). That is 10-15MB for a
large employer, so the cap is raised well past the RSS default — but it is
still a cap, because this payload is parsed in-process before anything
validates it.

Filtering happens in `fetch`, not `parse`. The dispatcher hands `parse` only
the bytes and a source id (R2), and the filter lives in the task's url, so the
one place that knows both is `fetch`. What it returns is the board's own job
shape, cut down to the postings the url asked for.
"""
import html
import json
import re

from . import rss
from .base import Item, SourceError

MAX_BYTES = 40_000_000
TITLE_CHARS = 200
BODY_CHARS = 4000
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def get_json(url, *, etag=None, modified=None, timeout=30):
    """(decoded_json_or_None, headers). None is a 304 — unchanged, not broken."""
    raw, headers = rss.fetch(url, timeout=timeout, max_bytes=MAX_BYTES,
                             etag=etag, modified=modified)
    if raw is None:
        return None, headers
    try:
        return json.loads(raw), headers
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise SourceError(url, f"not JSON: {e}") from e


def guard(url, total, matched, what):
    """R36. An empty board, or a filter that matches nothing on a board full of
    postings, is far likelier to mean the API or the filter value changed (an
    office renumbered, a location renamed) than that every remote role closed
    on the same day. Reporting it as broken is the difference between a dead
    source and a quiet one."""
    if total == 0:
        raise SourceError(url, "board returned no postings at all")
    if matched == 0:
        raise SourceError(url, f"{what} matched 0 of {total} postings; "
                               "has the filter value changed?")


def one_line(s, limit=TITLE_CHARS) -> str:
    """Titles come from the board and end up in a Telegram message: no control
    characters, no newlines, bounded length. Escaping is the renderer's job."""
    s = _CTRL_RE.sub(" ", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= limit else s[:limit - 1].rstrip() + "…"


def html_to_text(s) -> str:
    """Greenhouse `content` is HTML that has itself been entity-escaped, so it
    is unescaped once to recover the markup, then cleaned like a feed body."""
    return rss._clean_body(html.unescape(s or ""))


def make_item(*, job_id, source_id, url, title, team, location, facts, description,
              published, fetched_at) -> Item:
    """Item.title carries title · team · location, built here from structured
    fields rather than written by the curator. It is what the digest links, so
    the reader sees exactly what the board said, not a model's paraphrase.

    The body leads with the same facts before the description: curation reads
    only the first 1200 chars (MAX_BODY_CHARS), and seniority, team and
    workplace type are most of what the judgment needs.
    """
    head = " · ".join(p for p in (one_line(title), one_line(team, 80),
                                  one_line(location, 80)) if p)
    fact_line = " | ".join(f"{k}: {one_line(v, 120)}" for k, v in facts if v)
    body = f"{fact_line}\n{description or ''}".strip()[:BODY_CHARS]
    return Item(id=str(job_id), source_id=source_id, url=url,
                title=one_line(head), body=body,
                published_at=rss._parse_rfc3339(published or ""),
                fetched_at=fetched_at)
