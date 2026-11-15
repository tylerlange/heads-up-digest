"""`bullet: title+line`: the item's own title is the link, the curator's line
is the reason under it. Default bullets must not change."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest.curate import Judgment  # noqa: E402
from curated_digest.renderers import telegram as renderer  # noqa: E402
from curated_digest.task import TaskError, load_task  # noqa: E402


class TitledBullets(unittest.TestCase):
    class It:
        def __init__(self, title, url="https://jobs.ashbyhq.com/globex/x", sid="t/globex"):
            self.title, self.url, self.source_id = title, url, sid

    def test_the_title_is_the_link_and_the_reason_sits_under_it(self):
        j = Judgment("a", True, "act", "why", False, "Fits <launch> review")
        body = renderer.render([j], {"a": self.It("Engineer · Ops · US - Remote")},
                               heading="Jobs", bullet="title+line").body
        self.assertIn('">Engineer · Ops · US - Remote</a>\n   Fits &lt;launch&gt; review', body)

    def test_default_bullets_are_unchanged(self):
        j = Judgment("a", True, "act", "why", False, "the line")
        body = renderer.render([j], {"a": self.It("Title")}, heading="H").body
        self.assertIn(">the line</a>", body)
        self.assertNotIn("Title", body)

    def test_an_unknown_bullet_style_is_a_task_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.yaml"
            p.write_text("id: t\nsource: {type: rss, url: 'https://e.com/f.xml'}\n"
                         "criteria: x\nschedule: every 1h\nmodel: gemini:x\n"
                         "renderer: telegram\nchannel: telegram\nurgency: digest\n"
                         "heading: T\nbullet: fancy\n")
            with self.assertRaises(TaskError):
                load_task(p)

if __name__ == "__main__":
    unittest.main()
