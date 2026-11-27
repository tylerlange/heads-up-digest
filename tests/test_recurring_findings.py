"""D37. A check finding is a state, not an event: once the check stops reporting
it, the next occurrence of the same headline must alert again.

The failure this guards against was silent in production: a poller stopped for
12 days and the digest said nothing, because the identical headline had been
delivered weeks earlier for a different outage and R12 suppressed it.
"""
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class RecurringCheckFindings(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        root = Path(self.tmp.name)
        self._env = {"CURATED_DIGEST_STATE_DIR": str(root / "state"),
                     "CURATED_DIGEST_HOME": str(root)}
        for k, v in self._env.items():
            os.environ[k] = v
        from curated_digest import dispatch, ledger, store, task
        self.dispatch, self.store, self.task = dispatch, store, task
        self.ctx = ledger.connect()
        self.c = self.ctx.__enter__()
        store.ensure_schema(self.c)
        # The check reports a finding while this file exists.
        self.flag = root / "broken"
        self.src = task.Source(
            "probe/poller", "shell",
            f"test -e '{self.flag}' && {{ echo 'Poller not syncing'; echo detail; exit 1; }}; exit 0")

    def tearDown(self):
        self.ctx.__exit__(None, None, None)
        self.tmp.cleanup()
        for k in self._env:
            os.environ.pop(k, None)

    def _poll(self):
        out = {"new": 0}
        status = self.dispatch._fetch_source(self.c, self.src, out)
        self.assertEqual(status, "ok")
        return self.store.candidates(self.c, "probe", [self.src.id])

    def _deliver(self, rows):
        self.store.mark_reported(self.c, "probe", rows)

    def test_a_finding_that_persists_is_reported_once(self):
        self.store.candidates(self.c, "probe", [self.src.id])   # past the first-run watermark
        self.flag.touch()
        rows = self._poll()
        self.assertEqual([r["title"] for r in rows], ["Poller not syncing"])
        self._deliver(rows)
        self.assertEqual(self._poll(), [])

    def test_a_finding_that_clears_and_recurs_is_reported_again(self):
        self.store.candidates(self.c, "probe", [self.src.id])
        self.flag.touch()
        self._deliver(self._poll())
        self.flag.unlink()
        self.assertEqual(self._poll(), [])        # healthy: silence (R26)
        self.flag.touch()
        rows = self._poll()
        self.assertEqual([r["title"] for r in rows], ["Poller not syncing"])

    def test_a_broken_check_does_not_clear_anything(self):
        """R36: a check that cannot run says nothing about whether the fault
        is over, so it must not reset the finding into a fresh alert."""
        self.store.candidates(self.c, "probe", [self.src.id])
        self.flag.touch()
        self._deliver(self._poll())
        broken = self.task.Source("probe/poller", "shell", "exit 3")
        self.assertEqual(self.dispatch._fetch_source(self.c, broken, {"new": 0}), "failed")
        self.assertEqual(self._poll(), [])        # still reported, not re-alerted

    def test_only_the_finding_that_cleared_is_forgotten(self):
        self.store.candidates(self.c, "probe", [self.src.id])
        two = self.task.Source("probe/poller", "shell",
                               "printf 'Disk full\\n'; exit 1")
        self.dispatch._fetch_source(self.c, two, {"new": 0})
        self._deliver(self.store.candidates(self.c, "probe", [self.src.id]))
        self.dispatch._fetch_source(self.c, two, {"new": 0})
        self.assertEqual(self.store.candidates(self.c, "probe", [self.src.id]), [])
        # A different headline from the same check replaces the old one.
        self.flag.touch()
        rows = self._poll()
        self.assertEqual([r["title"] for r in rows], ["Poller not syncing"])
        self.dispatch._fetch_source(self.c, two, {"new": 0})
        self.assertEqual([r["title"] for r in
                          self.store.candidates(self.c, "probe", [self.src.id])],
                         ["Disk full"])

    def test_feed_items_are_still_reported_only_once(self):
        """R12 is unchanged for events: an RSS item that drops off the feed
        and reappears is the same item, already reported."""
        from curated_digest.sources import rss
        self.assertFalse(getattr(rss, "FINDINGS_ARE_CURRENT", False))


if __name__ == "__main__":
    unittest.main()
