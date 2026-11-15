"""R6-R10: tasks. A task is a declarative YAML file, nothing more. Adding one
means adding a file — no code change, no new unit, no restart (R6).

R9 governs the shape of this whole module: a malformed task must fail loudly
AND in isolation. One typo'd file cannot be allowed to take the others down
with it, so `load_all` never raises — it collects failures the same way it
collects successes.
"""
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .sources import SOURCE_TYPES

ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
SCHEDULE_EVERY_RE = re.compile(r"^every (\d+)(m|h)$")
SCHEDULE_DAILY_RE = re.compile(r"^daily at (\d{2}):(\d{2})$")
WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")
URGENCIES = ("digest", "prompt", "urgent")
# How a digest bullet is built. `line` is the curator's sentence alone, which
# suits news. `title+line` links the item's own title and puts the curator's
# line under it as the reason, for items whose title is the fact (a job
# posting) and must not be paraphrased by a model.
BULLETS = ("line", "title+line")

REQUIRED = ("id", "source", "criteria", "schedule", "model", "renderer",
            "channel", "urgency", "heading")
# R7 lists id/source/criteria/schedule/model/renderer/channel/urgency; heading
# is required too so a rendered message never has to invent a title.
OPTIONAL = ("link", "prefilter", "window", "bullet")
KNOWN_KEYS = set(REQUIRED) | set(OPTIONAL)


@dataclass(frozen=True)
class Source:
    """One feed. `id` is what the store keys etags, failure counts and
    already-reported records against, so it must be stable across edits to the
    task file — deriving it from list position would silently orphan state the
    moment a source is reordered or removed."""
    id: str
    type: str
    url: str
    command: str = ""           # `shell` sources only: the check to run

    @property
    def locator(self) -> str:
        """What the source implementation is pointed at. A url for anything
        fetched over the network, a command for a `shell` check.

        Exists so `_fetch_source` stays free of `if src.type ==` branches: R2
        says adding a source type must not require changes elsewhere, and a
        dispatcher that special-cases one type has already broken that.
        """
        return self.command or self.url


@dataclass(frozen=True)
class Task:
    id: str
    sources: tuple           # tuple[Source, ...] — one digest may span several
    criteria: str
    schedule: str               # raw string, as written
    model: str                  # "provider:model"
    renderer: str               # "telegram"
    channel: str                # "telegram"
    urgency: str                # digest | prompt | urgent
    heading: str
    link: str | None = None
    window: str | None = None   # "HH:MM-HH:MM" local; outside it, never runs
    max_age_days: int | None = None
    include: tuple[str, ...] = field(default_factory=tuple)
    exclude: tuple[str, ...] = field(default_factory=tuple)
    bullet: str = "line"         # see BULLETS
    path: str = ""               # file it came from


class TaskError(Exception):
    """Carries enough to name the file and the reason without a traceback —
    this is reported to the reader, not just logged."""
    def __init__(self, path, reason):
        self.path = str(path)
        self.reason = reason
        super().__init__(f"{self.path}: {reason}")


def valid_model(spec) -> bool:
    """R8: no bare-model default. A bare name is an error, not something to
    fall back on — that fallback is exactly where cost silently accumulates.

    Lives here rather than inline in `load_task` so the `--model` override
    shares one rule with the schema. Two copies would drift, and the copy that
    drifts loosest is the one on the command line, where nothing reviews it.
    """
    return (isinstance(spec, str) and ":" in spec
            and not spec.startswith(":") and not spec.endswith(":"))


def _require(d: dict, key: str, path):
    if key not in d or d[key] in (None, ""):
        raise TaskError(path, f"missing required field: {key}")
    return d[key]


def load_task(path) -> Task:
    path = Path(path)
    with open(path) as fh:
        try:
            d = yaml.safe_load(fh) or {}
        except yaml.YAMLError as e:
            raise TaskError(path, f"invalid YAML: {e}")

    if not isinstance(d, dict):
        raise TaskError(path, "task must be a YAML mapping")

    # R9's isolation guarantee is only as good as this check: an unrecognised
    # key (a typo'd `critera:`) must fail loudly rather than be silently
    # dropped and quietly disable whatever it was meant to configure.
    unknown = set(d) - KNOWN_KEYS
    if unknown:
        raise TaskError(path, f"unknown field(s): {', '.join(sorted(unknown))}")

    for key in REQUIRED:
        _require(d, key, path)

    task_id = d["id"]
    if not isinstance(task_id, str) or not ID_RE.match(task_id):
        raise TaskError(path, f"id {task_id!r} is not filename-safe")
    # R15/state key off id, keyed by filename elsewhere — a mismatch here
    # silently orphans state under a stale id the next time the file is
    # renamed, so id and filename stem must always agree.
    if task_id != path.stem:
        raise TaskError(
            path, f"id {task_id!r} does not match filename {path.stem!r}")

    sources = _parse_sources(path, d["source"], task_id)

    model = d["model"]
    if not valid_model(model):
        raise TaskError(path, f"model {model!r} must be 'provider:model'")

    urgency = d["urgency"]
    if urgency not in URGENCIES:
        raise TaskError(path, f"urgency {urgency!r} must be one of {URGENCIES}")

    renderer = d["renderer"]
    channel = d["channel"]

    schedule = d["schedule"]
    if not isinstance(schedule, str):
        raise TaskError(path, "schedule must be a string")
    try:
        parse_schedule(schedule)
    except TaskError:
        raise TaskError(path, f"schedule {schedule!r} is not a recognised form")

    link = d.get("link")
    if link is not None and (not isinstance(link, str) or not re.match(r"^https?://", link)):
        raise TaskError(path, f"link {link!r} is not http(s)")
    # One footer link on a multi-source digest is a claim about every bullet,
    # and it is wrong for all but one source. Bullets carry their own links.
    if link is not None and len(sources) > 1:
        raise TaskError(path, "link is single-source only: a digest spanning "
                              f"{len(sources)} sources cannot footnote one url")

    window = d.get("window")
    if window is not None:
        if not isinstance(window, str) or not WINDOW_RE.match(window):
            raise TaskError(path, f"window {window!r} must be 'HH:MM-HH:MM'")
        try:
            parse_window(window)
        except TaskError as e:
            raise TaskError(path, e.reason)

    bullet = d.get("bullet", "line")
    if bullet not in BULLETS:
        raise TaskError(path, f"bullet {bullet!r} must be one of {BULLETS}")

    prefilter = d.get("prefilter") or {}
    if not isinstance(prefilter, dict):
        raise TaskError(path, "prefilter must be a mapping")
    pf_unknown = set(prefilter) - {"max_age_days", "include", "exclude"}
    if pf_unknown:
        raise TaskError(path, f"unknown prefilter field(s): {', '.join(sorted(pf_unknown))}")
    max_age_days = prefilter.get("max_age_days")
    if max_age_days is not None and not isinstance(max_age_days, int):
        raise TaskError(path, "prefilter.max_age_days must be an integer")
    include = tuple(prefilter.get("include") or ())
    exclude = tuple(prefilter.get("exclude") or ())

    return Task(
        id=task_id,
        sources=sources,
        criteria=str(d["criteria"]),
        schedule=schedule,
        model=model,
        renderer=str(renderer),
        channel=str(channel),
        urgency=urgency,
        heading=str(d["heading"]),
        link=link,
        window=window,
        max_age_days=max_age_days,
        include=include,
        exclude=exclude,
        bullet=bullet,
        path=str(path),
    )


def load_all(directory) -> tuple[list[Task], list[TaskError]]:
    """R9: a malformed task fails loudly AND in isolation. Returns the tasks
    that loaded and the errors for those that did not — never raises, never
    lets one bad file stop the others."""
    tasks, errors = [], []
    paths = sorted(set(Path(directory).glob("*.yaml")) | set(Path(directory).glob("*.yml")))
    for path in paths:
        try:
            tasks.append(load_task(path))
        except TaskError as e:
            errors.append(e)
        except Exception as e:  # noqa: BLE001 - any surprise still isolates
            errors.append(TaskError(path, f"unexpected error: {e}"))
    return tasks, errors


def _parse_sources(path, source, task_id) -> tuple:
    """`source:` accepts a single mapping or a list of them.

    A single unnamed source keeps `source.id == task_id`. That is not cosmetic:
    the store already holds state under the bare task id for existing tasks,
    and changing the key would look exactly like a brand new source — first
    run, fresh watermark, ten items re-reported.
    """
    entries = source if isinstance(source, list) else [source]
    if not entries:
        raise TaskError(path, "source list is empty")

    out, seen = [], set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            raise TaskError(path, "each source must be a mapping")
        unknown = set(e) - {"type", "url", "id", "run"}
        if unknown:
            raise TaskError(path, f"unknown source key(s): {sorted(unknown)}")
        if e.get("type") not in SOURCE_TYPES:
            raise TaskError(path, f"source.type {e.get('type')!r} is not supported")

        # `url` and `run` are the two mutually exclusive ways to address a
        # source, and accepting both would leave `locator` silently picking a
        # winner. A shell check with a url is a copy-paste error, not a config.
        url, command = e.get("url"), ""
        if e["type"] == "shell":
            if url is not None:
                raise TaskError(path, "a shell source takes `run`, not `url`")
            command = e.get("run")
            if not isinstance(command, str) or not command.strip():
                raise TaskError(path, "shell source needs a non-empty `run`")
            command, url = command.strip(), ""
        else:
            if "run" in e:
                raise TaskError(path, f"`run` is shell-only, not {e['type']!r}")
            if not isinstance(url, str) or not re.match(r"^https?://", url):
                raise TaskError(path, f"source.url {url!r} is not http(s)")

        sid = e.get("id")
        if sid is None:
            # A shell check has no url to slug, so an unnamed one in a list has
            # nothing stable to key state on. Naming it is the cheap fix, and
            # demanding the name is better than inventing `check-2`, which
            # reorders into a different check the day one is deleted.
            if len(entries) > 1 and e["type"] == "shell":
                raise TaskError(path, "a shell source in a list needs an `id`")
            sid = task_id if len(entries) == 1 else f"{task_id}/{_slug(url)}"
        elif len(entries) > 1 or sid != task_id:
            sid = sid if sid.startswith(f"{task_id}") else f"{task_id}/{sid}"
        if not ID_RE.match(sid.split("/")[-1]):
            raise TaskError(path, f"source id {sid!r} is not filename-safe")
        if sid in seen:
            raise TaskError(path, f"duplicate source id {sid!r}")
        seen.add(sid)
        out.append(Source(sid, e["type"], url, command))
    return tuple(out)


def _slug(url: str) -> str:
    """A readable, stable key from a URL. Reddit's /r/<name>/.rss and a blog's
    /atom.xml both need to produce something a human can recognise in a broken
    source report."""
    parts = [p for p in re.split(r"[/.]", url.split("://", 1)[-1])
             if p and p not in ("www", "rss", "xml", "atom", "feed", "com",
                                "org", "net", "io", "en", "docs")]
    return re.sub(r"[^a-z0-9._-]", "-", "-".join(parts[-2:]).lower()) or "src"


def parse_schedule(s: str) -> dict:
    m = SCHEDULE_EVERY_RE.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        seconds = n * 60 if unit == "m" else n * 3600
        return {"kind": "every", "seconds": seconds}
    m = SCHEDULE_DAILY_RE.match(s)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if hour > 23 or minute > 59:
            raise TaskError("<schedule>", f"invalid time in schedule: {s!r}")
        return {"kind": "daily", "hour": hour, "minute": minute}
    raise TaskError("<schedule>", f"unrecognised schedule: {s!r}")


def parse_window(s: str) -> tuple[int, int]:
    """"HH:MM-HH:MM" as minutes past midnight. A start later than the end means
    the window wraps midnight (22:00-06:00), which is a legitimate thing to
    want and would otherwise parse as a window that is never open."""
    m = WINDOW_RE.match(s or "")
    if not m:
        raise TaskError("<window>", f"unrecognised window: {s!r}")
    sh, sm, eh, em = (int(g) for g in m.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        raise TaskError("<window>", f"invalid time in window: {s!r}")
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        raise TaskError("<window>", f"window {s!r} is zero-length")
    return start, end


def in_window(window: str | None, now: float, tz_offset_hours=None) -> bool:
    """No window declared means always open — the existing tasks must keep
    behaving exactly as they did."""
    if not window:
        return True
    start, end = parse_window(window)
    struct = (time.localtime(now) if tz_offset_hours is None
              else time.gmtime(now + tz_offset_hours * 3600))
    minute = struct.tm_hour * 60 + struct.tm_min
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end       # wraps midnight


def is_due(schedule: str, last_run: int | None, now: float, tz_offset_hours=None,
           window: str | None = None) -> bool:
    """A new task (last_run is None) is due on its next tick — waiting for a
    second tick before ever firing would make a freshly added task look dead.

    The window is checked FIRST, and it gates running rather than sending. A
    task held outside its window never fetches, never pays a model and never
    records a run, so it simply becomes due again the moment the window opens.
    Suppressing at the delivery seam instead would have spent the money at
    03:00 to produce a digest nobody could be shown.

    This deliberately does not touch the health alerter (R25/R42b): the host being
    broken still interrupts at any hour. Only content digests keep hours.
    """
    if not in_window(window, now, tz_offset_hours):
        return False

    if last_run is None:
        return True

    parsed = parse_schedule(schedule)
    if parsed["kind"] == "every":
        return now - last_run >= parsed["seconds"]

    # "daily at HH:MM": find the most recent occurrence of that wall-clock
    # time at or before `now`, then fire if last_run predates it. Anchoring
    # to the *most recent* occurrence — rather than requiring an exact-minute
    # match — is what makes a late tick (07:03 for a 07:00 schedule) still
    # fire instead of silently skipping the whole day.
    if tz_offset_hours is None:
        now_struct = time.localtime(now)
    else:
        now_struct = time.gmtime(now + tz_offset_hours * 3600)

    occurrence = _combine(now_struct, parsed["hour"], parsed["minute"], tz_offset_hours)
    if occurrence > now:
        occurrence -= 86400  # today's time hasn't happened yet; use yesterday's

    return last_run < occurrence


def _combine(struct, hour, minute, tz_offset_hours):
    """Rebuilds a timestamp for `struct`'s calendar date at HH:MM, in the same
    clock (local or the tz_offset test seam) that `now` was read in."""
    if tz_offset_hours is None:
        return time.mktime((struct.tm_year, struct.tm_mon, struct.tm_mday,
                             hour, minute, 0, 0, 0, -1))
    # Test seam: treat tz_offset_hours as a fixed UTC offset so the daily
    # logic is verifiable without depending on the machine's timezone or DST.
    import calendar
    ts = calendar.timegm((struct.tm_year, struct.tm_mon, struct.tm_mday,
                          hour, minute, 0, 0, 0, -1))
    return ts - tz_offset_hours * 3600
