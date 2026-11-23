#!/usr/bin/env python3
"""The doctor's two "content is being missed" rules, which both misreported on 2026-11-22.

Both failures were silent in opposite directions, and that is why they are tested here
rather than left to the nightly run:

* `find_stale_agencies` flagged NCTD and the Coastal Commission with "scraper may be
  blocked or broken" while both scrapers were working. The 30-day document-age default
  assumes a body that files monthly or oftener; NCTD's largest healthy gap over the prior
  year is 56 days and the CCC's is 133, because that scraper stores only the items
  relevant to this jurisdiction. A per-agency `doc_stale_days` is the override.
* The empty-meetings coverage check compared body names by equality, so an agency storing
  "City of La Mesa Planning Commission" against a configured "Planning Commission" matched
  nothing. Measured 2026-11-23: zero rows matched for nine of twenty-two agencies, so that
  finding had been dead for all nine, and dead in the quiet direction.

`data/` is gitignored, so these tests supply their own agency registry and document dates
and touch no data directory.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pipeline_doctor  # noqa: E402


def days_ago(n: int) -> str:
    return (date.today() - timedelta(days=n)).isoformat()


class StaleAgencyWindow(unittest.TestCase):
    """`doc_stale_days` widens the document-age rule for one agency only."""

    def setUp(self):
        self.registry = {}
        self.doc_dates = {}
        self._orig = (pipeline_doctor.load_agencies,
                      pipeline_doctor._load_doc_dates,
                      pipeline_doctor._newest_meeting_row)
        pipeline_doctor.load_agencies = lambda: self.registry
        pipeline_doctor._load_doc_dates = lambda slug: self.doc_dates.get(slug, {})
        # No live meeting rows: the cadence rule's escape hatch stays shut, so each test
        # exercises the document-age rule it is about.
        pipeline_doctor._newest_meeting_row = lambda slug: ""

    def tearDown(self):
        (pipeline_doctor.load_agencies,
         pipeline_doctor._load_doc_dates,
         pipeline_doctor._newest_meeting_row) = self._orig

    def reasons(self):
        return {name: reason for name, _newest, reason in pipeline_doctor.find_stale_agencies()}

    def test_default_window_still_flags_a_silent_agency(self):
        self.registry = {"poway": {"name": "City of Poway"}}
        self.doc_dates = {"poway": {"a.txt": days_ago(45)}}
        self.assertEqual(self.reasons(),
                         {"City of Poway": "no dated meeting document"})

    def test_override_suppresses_a_gap_inside_the_agency_window(self):
        # NCTD's real state on 2026-11-23: newest document 32 days old, healthy.
        self.registry = {"nctd": {"name": "North County Transit District",
                                  "doc_stale_days": 75}}
        self.doc_dates = {"nctd": {"a.txt": days_ago(32)}}
        self.assertEqual(self.reasons(), {})

    def test_override_still_flags_a_gap_past_the_agency_window(self):
        self.registry = {"nctd": {"name": "North County Transit District",
                                  "doc_stale_days": 75}}
        self.doc_dates = {"nctd": {"a.txt": days_ago(90)}}
        self.assertEqual(self.reasons(),
                         {"North County Transit District": "no dated meeting document"})

    def test_override_applies_to_one_agency_not_its_neighbours(self):
        self.registry = {
            "coastal": {"name": "California Coastal Commission", "doc_stale_days": 180},
            "vista": {"name": "City of Vista"},
        }
        self.doc_dates = {"coastal": {"a.txt": days_ago(100)},
                          "vista": {"b.txt": days_ago(100)}}
        self.assertEqual(self.reasons(),
                         {"City of Vista": "no dated meeting document"})

    def test_a_junk_override_falls_back_to_the_default(self):
        # A typo in agencies.yaml must not silence an agency for ever.
        self.registry = {"nctd": {"name": "North County Transit District",
                                  "doc_stale_days": "soon"}}
        self.doc_dates = {"nctd": {"a.txt": days_ago(45)}}
        self.assertEqual(self.reasons(),
                         {"North County Transit District": "no dated meeting document"})

    def test_an_agency_with_no_documents_is_flagged_as_never(self):
        self.registry = {"santee": {"name": "City of Santee"}}
        self.doc_dates = {}
        self.assertEqual(
            pipeline_doctor.find_stale_agencies(),
            [("City of Santee", "never", "no dated meeting document")],
        )


class BodyMatching(unittest.TestCase):
    """Configured body names match the names the scrapers actually store."""

    def test_exact_name_matches(self):
        self.assertTrue(pipeline_doctor._body_matches({"city council"}, "City Council"))

    def test_city_prefixed_name_matches(self):
        # la_mesa, chula_vista, national_city, imperial_beach, sandag, sdcounty
        self.assertTrue(pipeline_doctor._body_matches(
            {"planning commission"}, "City of La Mesa Planning Commission"))

    def test_agency_prefixed_name_matches(self):
        self.assertTrue(pipeline_doctor._body_matches(
            {"regular board meeting"}, "NCTD Regular Board Meeting"))

    def test_cancelled_prefix_is_stripped_before_matching(self):
        # Coronado writes the cancellation into the body name itself.
        self.assertTrue(pipeline_doctor._body_matches(
            {"city council regular meeting"},
            "Canceled - City Council Regular Meeting"))

    def test_an_unrelated_body_does_not_match(self):
        self.assertFalse(pipeline_doctor._body_matches(
            {"planning commission"}, "City of La Mesa Civil Service Commission"))

    def test_an_empty_body_does_not_match(self):
        self.assertFalse(pipeline_doctor._body_matches({"city council"}, ""))
        self.assertFalse(pipeline_doctor._body_matches({"city council"}, None))


class AdvertisedDocuments(unittest.TestCase):
    """A row pointing at no document is the source publishing nothing, not a gap."""

    def test_a_packet_url_counts(self):
        self.assertTrue(pipeline_doctor._advertises_document({"pdf_url": "http://x/a.pdf"}))

    def test_an_agenda_url_counts(self):
        self.assertTrue(pipeline_doctor._advertises_document({"agenda_url": "http://x/a"}))

    def test_a_declared_documents_list_counts(self):
        self.assertTrue(pipeline_doctor._advertises_document(
            {"documents": [{"title": "Agenda"}]}))

    def test_nctd_row_with_no_packet_does_not_count(self):
        # gonctd.com listed the 2026-11-19 board meeting with no agenda packet and no
        # video, verified against the live page on 2026-11-23.
        self.assertFalse(pipeline_doctor._advertises_document(
            {"body": "NCTD Regular Board Meeting", "date": "11/19/2026",
             "pdf_url": None, "video_url": None}))

    def test_a_meeting_page_link_is_not_a_document(self):
        self.assertFalse(pipeline_doctor._advertises_document(
            {"escribe_meeting_url": "http://x/Meeting.aspx", "agenda_url": None,
             "minutes_url": None, "detail_url": "http://x/d", "folder_url": "http://x/f"}))

    def test_an_empty_documents_list_does_not_count(self):
        self.assertFalse(pipeline_doctor._advertises_document({"documents": []}))


if __name__ == "__main__":
    unittest.main()
