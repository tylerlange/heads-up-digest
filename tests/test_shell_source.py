"""The exit-code contract is the whole value of this source, so every row of
it gets a test. The one that matters most is `test_a_check_that_cannot_run_is
_an_error_not_a_clean_bill_of_health` — that is the R36 failure this source
exists to avoid, and it is the failure nobody would notice in production.
"""
import os
import unittest
import unittest.mock

from curated_digest.sources import shell
from curated_digest.sources.base import SourceError


_HOME = None
_ENV = None


def setUpModule():
    # Checks run from the config home, so give them one that exists.
    global _HOME, _ENV
    import tempfile
    _HOME = tempfile.TemporaryDirectory()
    _ENV = unittest.mock.patch.dict(os.environ, {"CURATED_DIGEST_HOME": _HOME.name})
    _ENV.start()


def tearDownModule():
    _ENV.stop()
    _HOME.cleanup()


def run(command, **kw):
    raw, _ = shell.fetch(command, **kw)
    return shell.parse(raw, "t/check", fetched_at=1_700_000_000)


class HealthyChecksSaySilent(unittest.TestCase):
    def test_exit_zero_produces_no_item_at_all(self):
        self.assertEqual(run("echo everything fine; exit 0"), [])

    def test_exit_zero_stays_silent_even_when_it_printed_nothing(self):
        self.assertEqual(run("true"), [])


class FindingsBecomeItems(unittest.TestCase):
    def test_exit_one_turns_stdout_into_an_item(self):
        (item,) = run("echo 'Backups are stale'; echo 'newest is 5 days old'; exit 1")
        self.assertEqual(item.title, "Backups are stale")
        self.assertIn("newest is 5 days old", item.body)
        self.assertEqual(item.source_id, "t/check")

    def test_the_first_line_is_the_title_and_the_rest_is_detail(self):
        (item,) = run("printf 'headline\\ndetail one\\ndetail two\\n'; exit 1")
        self.assertEqual(item.title, "headline")
        self.assertIn("detail two", item.body)

    def test_stderr_is_captured_so_a_check_can_report_on_either_stream(self):
        (item,) = run("echo 'disk nearly full' >&2; exit 1")
        self.assertEqual(item.title, "disk nearly full")

    def test_an_item_carries_no_url_because_a_check_has_nothing_to_link_to(self):
        (item,) = run("echo problem; exit 1")
        self.assertEqual(item.url, "")


class IdentityIsTheFirstLine(unittest.TestCase):
    """Dedup is what keeps a standing problem from re-notifying every six
    hours, and it keys off the title alone."""

    def test_the_same_headline_twice_is_the_same_item(self):
        (a,) = run("echo 'Backups are stale'; echo 'run at 01:00'; exit 1")
        (b,) = run("echo 'Backups are stale'; echo 'run at 07:00'; exit 1")
        self.assertEqual(a.id, b.id)

    def test_a_changed_headline_is_a_new_item(self):
        (a,) = run("echo 'Backups are stale'; exit 1")
        (b,) = run("echo 'Backups are missing'; exit 1")
        self.assertNotEqual(a.id, b.id)

    def test_two_checks_reporting_the_same_words_stay_separate_items(self):
        raw, _ = shell.fetch("echo 'disk nearly full'; exit 1")
        (a,) = shell.parse(raw, "t/disk-root")
        (b,) = shell.parse(raw, "t/disk-media")
        self.assertNotEqual(a.id, b.id)


class BrokenChecksAreLoud(unittest.TestCase):
    def test_a_check_that_cannot_run_is_an_error_not_a_clean_bill_of_health(self):
        with self.assertRaises(SourceError):
            run("/nonexistent/check.sh")

    def test_an_unexpected_exit_code_is_an_error(self):
        with self.assertRaises(SourceError) as cm:
            run("echo oops; exit 3")
        self.assertIn("exited 3", str(cm.exception))

    def test_a_check_killed_by_a_signal_is_an_error(self):
        with self.assertRaises(SourceError):
            run("kill -9 $$")

    def test_a_hanging_check_times_out_rather_than_blocking_the_digest(self):
        with self.assertRaises(SourceError) as cm:
            run("sleep 5", timeout=1)
        self.assertIn("timed out", str(cm.exception))

    def test_claiming_a_finding_with_no_output_is_an_error_not_an_empty_bullet(self):
        with self.assertRaises(SourceError):
            run("exit 1")

    def test_an_unreadable_result_is_an_error(self):
        with self.assertRaises(SourceError):
            shell.parse(b"not json", "t/check")


class Limits(unittest.TestCase):
    def test_a_runaway_check_is_capped_rather_than_buffered_forever(self):
        (item,) = run("yes 'x' | head -c 200000; exit 1")
        self.assertLessEqual(len(item.body), shell.BODY_CHARS)

    def test_a_long_headline_is_truncated(self):
        (item,) = run(f"printf '%.0sA' $(seq 1 500); echo; exit 1")
        self.assertEqual(len(item.title), shell.TITLE_CHARS)


class EnvironmentIsScrubbed(unittest.TestCase):
    """R32: the checks are the most privileged thing in the pipeline. An
    allowlist is used rather than a denylist, so this test is really asserting
    that a newly added secret is excluded by default."""

    def test_a_secret_in_the_parent_environment_does_not_reach_a_check(self):
        os.environ["CURATED_DIGEST_TEST_FAKE_API_KEY"] = "sk-should-not-leak"
        self.addCleanup(os.environ.pop, "CURATED_DIGEST_TEST_FAKE_API_KEY", None)
        (item,) = run("echo \"seen=[${CURATED_DIGEST_TEST_FAKE_API_KEY:-}]\"; exit 1")
        self.assertIn("seen=[]", item.title)

    def test_path_survives_so_ordinary_commands_still_resolve(self):
        self.assertEqual(run("command -v echo >/dev/null; exit 0"), [])


class RunsFromTheConfigHome(unittest.TestCase):
    def test_a_relative_command_resolves_against_the_instance_not_the_caller(self):
        import tempfile
        with tempfile.TemporaryDirectory() as home, \
                unittest.mock.patch.dict(os.environ, {"CURATED_DIGEST_HOME": home}):
            (item,) = run("pwd; exit 1")
        self.assertEqual(os.path.realpath(item.title), os.path.realpath(home))


if __name__ == "__main__":
    unittest.main()
