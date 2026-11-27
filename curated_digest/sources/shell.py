"""A source that asks the host about itself.

Every other source pulls from the open internet, which means the digest could
report what strangers said about a product and nothing about the machine it
runs on. This closes that: a check is a command, and its exit code is the
finding.

The exit-code contract is the whole design, and it exists to preserve R36 —
broken must never look like quiet:

    0            healthy. No item. Silence is correct (R26).
    1            a finding. stdout becomes the item the digest reports.
    anything else, a signal, a timeout, or a missing interpreter
                 the CHECK is broken, not the thing it checks. Raises
                 SourceError, so it lands in `source_state` as a failure and
                 gets reported by R36/R38 rather than read as "all clear".

That third row is the reason this file is not just `subprocess.run`. A check
script that gets deleted, renamed, or made non-executable would otherwise
report success forever, and the alarm the owner installed would be the thing that
fails silently.

Identity: `item_id(None, source_id, title)`, so the FIRST LINE of stdout is the
dedup key. A check that restates the same problem every six hours produces one
item and is reported once. Volatile detail (counts, ages, byte sizes) belongs
in the body, never in that first line — put "5 days" in the title and every run
mints a new item and re-notifies.

That identity lasts only as long as the condition does (D37). A feed item is
an event: once reported, it is done. A check finding is a state: when the
check stops reporting it, the problem is over, and the same headline next
month is a new occurrence that must alert again. FINDINGS_ARE_CURRENT tells
the dispatcher to forget findings this check no longer reports.
"""
import json
import os
import subprocess

from .. import config
from .base import Item, SourceError, item_id

# Enough for a check that dumps `docker ps` or a failed-unit list; small enough
# that a runaway loop printing forever is killed rather than buffered into RAM.
MAX_OUTPUT_BYTES = 64_000
BODY_CHARS = 4000
TITLE_CHARS = 200
TIMEOUT_SECONDS = 60
FINDINGS_ARE_CURRENT = True

# R32: no process handling content destined for a prompt carries secrets. These
# checks are the most privileged thing in the pipeline — they run shell on the
# host — so the environment is built up from nothing rather than filtered down
# from `os.environ`. A filter needs updating every time a new key is added to
# the environment; an allowlist does not.
ENV_ALLOW = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TZ")


def _env() -> dict:
    env = {k: os.environ[k] for k in ENV_ALLOW if k in os.environ}
    env.setdefault("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")
    return env


def fetch(command, *, timeout=TIMEOUT_SECONDS, cwd=None, **_ignored):
    """Runs one check. Returns (raw, headers) to match the source contract; the
    etag/modified arguments every caller passes are meaningless here and are
    swallowed by **_ignored.

    `shell=True` is deliberate. The command comes from a task file, which is
    owner-authored config that already decides what model to spend money on and
    what criteria to send it — a task file is trusted input, and if it were not,
    the command string would be the least of the problem. Fetched content never
    reaches this function.
    """
    # Relative to the config home, the operator's instance directory, so a
    # task can say `checks/disk-space.sh` and mean the same thing whether it
    # was started by systemd, by the harness, or by hand from another
    # directory. The checks belong to the instance, not to the engine, and a
    # task file should never have to name an absolute path.
    try:
        p = subprocess.run(
            command, shell=True, cwd=str(cwd or config.config_home()), env=_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise SourceError(command, f"check timed out after {timeout}s") from e
    except OSError as e:
        raise SourceError(command, f"check could not run: {e}") from e

    out = (p.stdout or b"")[:MAX_OUTPUT_BYTES]

    if p.returncode not in (0, 1):
        # Includes 127 (not found), 126 (not executable) and negative returns
        # (killed by signal) — every way a check can fail to be a check.
        detail = out.decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1] if detail else "no output"
        raise SourceError(command, f"check exited {p.returncode}: {reason[:200]}")

    return json.dumps({"rc": p.returncode,
                       "out": out.decode("utf-8", "replace")}).encode(), {}


def parse(raw, source_id, *, fetched_at=None):
    """rc 0 yields no items at all: a healthy check has nothing to say, and
    manufacturing an "all clear" item would put a bullet in the digest every
    single run, which is how a channel gets muted."""
    import time
    fetched_at = int(fetched_at or time.time())
    try:
        d = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise SourceError(source_id, f"unreadable check result: {e}") from e

    if d.get("rc") != 1:
        return []

    text = (d.get("out") or "").strip()
    if not text:
        # Exit 1 with nothing to say is a broken check, not a finding. Reporting
        # an empty bullet would be worse than reporting nothing, and silently
        # dropping it would hide a check that thinks it is working.
        raise SourceError(source_id, "check reported a finding with no output")

    lines = text.splitlines()
    title = lines[0].strip()[:TITLE_CHARS]
    body = text[:BODY_CHARS]
    return [Item(
        id=item_id(None, source_id, title), source_id=source_id, url="",
        title=title, body=body, published_at=None, fetched_at=fetched_at,
    )]
