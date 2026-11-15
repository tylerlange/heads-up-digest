"""The Greenhouse and Ashby job-board sources.

No network: every test drives mocked HTTP at the urlopen seam, so the real
fetch path (cap, 304, error mapping) runs exactly as it does live.
"""
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from curated_digest.sources import ashby, greenhouse  # noqa: E402
from curated_digest.sources.base import SourceError  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
REMOTE_OFFICE = 1234567
GH_URL = f"https://boards-api.greenhouse.io/v1/boards/acme/jobs?office_id={REMOTE_OFFICE}"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/globex?location=US%20-%20Remote"


class FakeResponse:
    def __init__(self, body: bytes, headers=None):
        self._body, self._pos, self.headers = body, 0, headers or {}

    def read(self, n):
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(payload, headers=None):
    """Patch urlopen to answer with `payload` and record what was requested."""
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return mock.patch("urllib.request.urlopen",
                      side_effect=lambda req, timeout=None: FakeResponse(body, headers))


def gh_job(jid, title="Senior Software Engineer, Platform", offices=(REMOTE_OFFICE,),
           **kw):
    j = {"id": jid, "title": title,
         "location": {"name": "Remote, United States"},
         "first_published": "2026-11-10T09:00:00-05:00",
         "updated_at": "2026-11-12T09:00:00-05:00",
         "departments": [{"id": 1, "name": "Infrastructure"}],
         "offices": [{"id": o, "name": f"office {o}"} for o in offices],
         "metadata": [{"name": "Location Type", "value": "Remote"}],
         "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{jid}",
         "content": "&lt;h2&gt;About Acme&lt;/h2&gt;&lt;p&gt;Boilerplate mission.&lt;/p&gt;"
                    "&lt;h2&gt;About the role&lt;/h2&gt;&lt;p&gt;Run launch review "
                    "&amp;amp; governance.&lt;/p&gt;"}
    j.update(kw)
    return j


def ashby_job(jid, title="Senior Software Engineer, Build Tools", location="US - Remote",
              secondary=(), **kw):
    j = {"id": jid, "title": title, "team": "Infrastructure",
         "department": "Operations", "location": location,
         "secondaryLocations": [{"location": s} for s in secondary],
         "publishedAt": "2026-11-11T02:20:02.702+00:00", "isListed": True,
         "isRemote": True, "workplaceType": "Remote", "employmentType": "FullTime",
         "jobUrl": f"https://jobs.ashbyhq.com/globex/{jid}",
         "descriptionPlain": "ABOUT THE TEAM\n\nWe run   the build fleet."}
    j.update(kw)
    return j


U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"
U3 = "33333333-3333-4333-8333-333333333333"


def gh_items(jobs, sid="job-boards/acme", url=GH_URL):
    with serve({"jobs": jobs}):
        raw, _ = greenhouse.fetch(url)
    return greenhouse.parse(raw, sid)


def ashby_items(jobs, sid="job-boards/globex", url=ASHBY_URL):
    with serve({"jobs": jobs, "apiVersion": "1"}):
        raw, _ = ashby.fetch(url)
    return ashby.parse(raw, sid)


class GreenhouseFeed(unittest.TestCase):
    def test_only_postings_in_the_remote_office_survive(self):
        items = gh_items([gh_job(1), gh_job(2, offices=(7654321,)),
                          gh_job(3, offices=(7654321, REMOTE_OFFICE))])
        self.assertEqual(sorted(i.id for i in items), ["1", "3"])

    def test_the_request_goes_to_the_board_api_without_the_local_filter(self):
        with serve({"jobs": [gh_job(1)]}) as up:
            greenhouse.fetch(GH_URL)
        req = up.call_args[0][0]
        self.assertEqual(req.full_url,
                         "https://boards-api.greenhouse.io/v1/boards/acme/jobs?content=true")

    def test_item_shape(self):
        (it,) = gh_items([gh_job(1000001)])
        self.assertEqual(it.id, "1000001")
        self.assertEqual(it.url, "https://job-boards.greenhouse.io/acme/jobs/1000001")
        self.assertEqual(it.title, "Senior Software Engineer, Platform · "
                                   f"Infrastructure · office {REMOTE_OFFICE}; "
                                   "Remote, United States")
        self.assertIsNotNone(it.published_at)
        self.assertTrue(it.body.startswith("Team: Infrastructure | Location: "))

    def test_the_matched_office_leads_the_location(self):
        (it,) = gh_items([gh_job(1, offices=(7654321, REMOTE_OFFICE),
                                 location={"name": "San Francisco, CA"})])
        self.assertTrue(it.title.endswith(f"office {REMOTE_OFFICE}; San Francisco, CA"))

    def test_the_company_boilerplate_is_cut_and_entities_decoded(self):
        (it,) = gh_items([gh_job(1)])
        self.assertNotIn("Boilerplate mission", it.body)
        self.assertIn("About the role Run launch review & governance.", it.body)
        self.assertNotIn("<", it.body)

    def test_the_link_is_built_from_the_id_not_taken_from_the_payload(self):
        (it,) = gh_items([gh_job(7, absolute_url="https://evil.example/phish")])
        self.assertEqual(it.url, "https://job-boards.greenhouse.io/acme/jobs/7")

    def test_a_filter_that_matches_nothing_is_broken_not_quiet(self):
        with serve({"jobs": [gh_job(1, offices=(42,))]}):
            with self.assertRaisesRegex(SourceError, "matched 0 of 1"):
                greenhouse.fetch(GH_URL)

    def test_an_empty_board_is_broken(self):
        with serve({"jobs": []}):
            with self.assertRaises(SourceError):
                greenhouse.fetch(GH_URL)

    def test_a_payload_without_jobs_is_broken(self):
        with serve({"error": "nope"}):
            with self.assertRaises(SourceError):
                greenhouse.fetch(GH_URL)

    def test_non_json_is_broken(self):
        with serve(b"<html>maintenance</html>"):
            with self.assertRaises(SourceError):
                greenhouse.fetch(GH_URL)

    def test_http_errors_are_source_errors(self):
        err = urllib.error.HTTPError(GH_URL, 503, "down", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(SourceError):
                greenhouse.fetch(GH_URL)

    def test_304_is_unchanged_not_broken(self):
        err = urllib.error.HTTPError(GH_URL, 304, "not modified", {"ETag": "x"}, None)
        with mock.patch("urllib.request.urlopen", side_effect=err):
            raw, _ = greenhouse.fetch(GH_URL, etag="x")
        self.assertIsNone(raw)

    def test_urls_that_are_not_a_greenhouse_board_are_refused(self):
        for bad in ("http://boards-api.greenhouse.io/v1/boards/acme/jobs",
                    "https://evil.example/v1/boards/acme/jobs",
                    "https://boards-api.greenhouse.io/v1/boards/acme/jobs?office_id=abc"):
            with self.assertRaises(SourceError, msg=bad):
                greenhouse.target(bad)


class AshbyFeed(unittest.TestCase):
    def test_primary_or_secondary_location_matches_the_remote_filter(self):
        items = ashby_items([
            ashby_job(U1),
            ashby_job(U2, location="San Francisco", secondary=("US - Remote",)),
            ashby_job(U3, location="London, UK", secondary=("Ontario - Remote",)),
        ])
        self.assertEqual(sorted(i.id for i in items), [U1, U2])

    def test_unlisted_postings_are_dropped(self):
        items = ashby_items([ashby_job(U1), ashby_job(U2, isListed=False)])
        self.assertEqual([i.id for i in items], [U1])

    def test_item_shape_leads_with_the_matched_location(self):
        (it,) = ashby_items([ashby_job(U1, location="San Francisco",
                                       secondary=("Seattle", "US - Remote", "NYC", "DC"))])
        self.assertEqual(it.url, f"https://jobs.ashbyhq.com/globex/{U1}")
        self.assertEqual(it.title, "Senior Software Engineer, Build Tools · Infrastructure · "
                                   "US - Remote, San Francisco, Seattle +2")
        self.assertIn("Workplace: Remote", it.body)
        self.assertIn("ABOUT THE TEAM We run the build fleet.", it.body)

    def test_a_posting_whose_id_is_not_a_uuid_is_skipped(self):
        items = ashby_items([ashby_job(U1), ashby_job("../../etc")])
        self.assertEqual([i.id for i in items], [U1])

    def test_a_renamed_location_is_broken_not_quiet(self):
        with serve({"jobs": [ashby_job(U1, location="Remote (US)")]}):
            with self.assertRaisesRegex(SourceError, "matched 0 of 1"):
                ashby.fetch(ASHBY_URL)

    def test_the_request_does_not_carry_the_local_filter(self):
        with serve({"jobs": [ashby_job(U1)]}) as up:
            ashby.fetch(ASHBY_URL)
        self.assertEqual(up.call_args[0][0].full_url,
                         "https://api.ashbyhq.com/posting-api/job-board/globex")

    def test_titles_are_one_line(self):
        (it,) = ashby_items([ashby_job(U1, title="Senior Engineer\n\x1b[31mInjected\nline")])
        self.assertNotIn("\n", it.title)
        self.assertNotIn("\x1b", it.title)

if __name__ == "__main__":
    unittest.main()
