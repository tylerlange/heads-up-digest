"""Source layer: fetch + normalize into Item (R1). Nothing downstream should
ever need to know a feed was RSS vs Atom vs anything else.

The registry lives here rather than in the dispatcher because two modules need
it — the dispatcher to run a source, the task schema to reject a typo'd type —
and a second copy in the validator would eventually accept a type the runtime
cannot run.
"""
from . import ashby, greenhouse, reddit, rss, shell

# R2: a source type is a registry entry. Adding one must not reach into
# curation, rendering, or delivery — they only ever see the normalized Item.
SOURCE_TYPES = {"rss": rss, "reddit": reddit, "shell": shell,
                "greenhouse": greenhouse, "ashby": ashby}
