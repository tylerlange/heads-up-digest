"""The archive of what was actually sent.

Before this existed, `reported` recorded which items went out and the sentences
the curator wrote about them were discarded on send — so the one artifact worth
reviewing was the only thing not kept. These tests pin the two properties that
make the archive trustworthy: it holds the body verbatim, and it only ever
holds messages that were really delivered.
"""
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest import store  # noqa: E402
from curated_digest.renderers import Artifact  # noqa: E402


def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    store.ensure_schema(c)
    return c


class RecordDelivery(unittest.TestCase):
    def setUp(self):
        self.c = conn()

    def test_the_body_is_stored_verbatim(self):
        art = Artifact("Reddit · Sep 16\n\nAct\n• a thing", kind="html",
                       title="Reddit · Sep 16", rendered_ids=())
        store.record_delivery(self.c, "reddit-digest", art)
        (row,) = store.deliveries(self.c)
        self.assertEqual(row["body"], art.body)
        self.assertEqual(row["heading"], "Reddit · Sep 16")
        self.assertEqual(row["task_id"], "reddit-digest")
        self.assertEqual(row["kind"], "digest")

    def test_broken_source_reports_are_distinguishable_from_digests(self):
        """They are different messages with different reasons for existing, and
        reviewing digest quality should not be polluted by outage alarms."""
        store.record_delivery(self.c, "t", Artifact("x", "html", "h", ()))
        store.record_delivery(self.c, "t", Artifact("y", "html", "h", ()),
                              kind="broken-sources")
        kinds = [r["kind"] for r in store.deliveries(self.c)]
        self.assertEqual(sorted(kinds), ["broken-sources", "digest"])

    def test_deliveries_come_back_newest_first(self):
        for i in range(3):
            store.record_delivery(self.c, "t", Artifact(f"m{i}", "html", "h", ()))
        self.assertEqual([r["body"] for r in store.deliveries(self.c)],
                         ["m2", "m1", "m0"])

    def test_deliveries_can_be_filtered_to_one_task(self):
        store.record_delivery(self.c, "a", Artifact("from-a", "html", "h", ()))
        store.record_delivery(self.c, "b", Artifact("from-b", "html", "h", ()))
        rows = store.deliveries(self.c, task_id="b")
        self.assertEqual([r["body"] for r in rows], ["from-b"])

    def test_the_limit_is_respected(self):
        for i in range(5):
            store.record_delivery(self.c, "t", Artifact(f"m{i}", "html", "h", ()))
        self.assertEqual(len(store.deliveries(self.c, limit=2)), 2)

    def test_an_archived_message_survives_the_items_it_described(self):
        """R13 sweeps items after six months. The archive is denormalized so a
        swept item cannot take the message that reported it with it."""
        store.record_delivery(self.c, "t", Artifact("still here", "html", "h", ()))
        self.c.execute("DELETE FROM items")
        self.assertEqual(len(store.deliveries(self.c)), 1)


class OnlyConfirmedSendsAreArchived(unittest.TestCase):
    """R14's rule applied to the archive: if it can contain a message that was
    never delivered, it is not a record of what the reader saw."""

    def _task(self, tmp):
        from curated_digest.task import load_task
        p = Path(tmp) / "t.yaml"
        p.write_text(
            "id: t\n"
            "source: {type: rss, url: 'https://example.com/f.xml'}\n"
            "criteria: anything\nschedule: every 1h\n"
            "model: gemini:x\nrenderer: telegram\nchannel: telegram\n"
            "urgency: digest\nheading: 'T'\n")
        return load_task(p)

    def _run(self, delivered):
        import tempfile
        from curated_digest import dispatch
        from curated_digest.curate import Judgment
        from curated_digest.sources.base import Item

        with tempfile.TemporaryDirectory() as tmp:
            task = self._task(tmp)
            c = conn()
            item = Item(id="i1", source_id="t", url="https://example.com/1",
                        title="A thing", body="body", published_at=None,
                        fetched_at=1)
            with mock.patch("curated_digest.sources.rss.fetch",
                            return_value=(b"<rss/>", {})), \
                 mock.patch("curated_digest.sources.rss.parse", return_value=[item]), \
                 mock.patch("curated_digest.dispatch.curate",
                            return_value=([Judgment("i1", True, "act", "why",
                                                    False, "the line")],
                                          [Judgment("i1", True, "act", "why",
                                                    False, "the line")], 0.0)), \
                 mock.patch("curated_digest.channels.telegram.send",
                            return_value=delivered):
                dispatch.run_task(c, task)
            return store.deliveries(c)

    def test_a_confirmed_send_is_archived(self):
        rows = self._run(delivered=True)
        self.assertEqual(len(rows), 1)
        self.assertIn("the line", rows[0]["body"])

    def test_a_failed_send_archives_nothing(self):
        self.assertEqual(self._run(delivered=False), [])


if __name__ == "__main__":
    unittest.main()
