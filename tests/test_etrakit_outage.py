#!/usr/bin/env python3
"""The eTRAKiT outage of 2026-11-24: a wrong-name certificate, and a scraper that called it done.

Two things are pinned here. The phase-failure lines civic-pipeline writes for each eTRAKiT
phase must classify as `loss`, so the next outage alerts. And the pinned-certificate fallback
must verify normally first, fall back only on an SSL error, and stay on the pin afterwards.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT))

import alert_triage  # noqa: E402

PIN = "bf" * 32

# The exact descriptions civic-pipeline passes to log_phase_error for eTRAKiT phases.
PHASE_FAILURES = [
    "City of Oceanside permits (2026) failed (rc=1)",
    "City of Oceanside projects (2025) failed (rc=1)",
    "City of Oceanside permit enrichment failed (rc=1)",
    "City of Oceanside status refresh failed (rc=1)",
    "City of Oceanside permits (2026) timed out",
]


class TestTriage(unittest.TestCase):
    def test_etrakit_phase_failures_are_loss(self):
        for desc in PHASE_FAILURES:
            finding = f"Latest pipeline run: [2026-11-28 01:07:10] [local] ERROR: {desc}"
            self.assertEqual(alert_triage.severity(finding), "loss", desc)

    def test_the_old_noise_finding_stays_degraded(self):
        # The per-request error count is still noise; the loud signal is the exit code.
        self.assertEqual(alert_triage.severity(
            "City of Oceanside permits fetch had 4 error(s) in recent log"), "degraded")


try:
    import requests  # noqa: F401
    from scrapers import etrakit
except ImportError:  # CI installs requests; a bare checkout may not
    etrakit = None


@unittest.skipIf(etrakit is None, "requests/bs4 not installed")
class TestPinFallback(unittest.TestCase):
    def _session(self):
        return etrakit._make_session(pin=PIN)

    def test_no_pin_means_plain_adapter(self):
        adapter = etrakit._make_session().get_adapter("https://x/")
        self.assertNotIsInstance(adapter, etrakit._PinFallbackAdapter)

    def test_verifies_normally_when_cert_is_good(self):
        adapter = self._session().get_adapter("https://x/")
        with mock.patch.object(etrakit.requests.adapters.HTTPAdapter, "send",
                               return_value="ok") as send:
            self.assertEqual(adapter.send(mock.Mock()), "ok")
        self.assertEqual(send.call_count, 1)
        self.assertFalse(adapter._use_pin)

    def test_falls_back_to_pin_on_ssl_error_and_stays(self):
        adapter = self._session().get_adapter("https://x/")
        calls = []

        def send(self_, request, **kw):
            calls.append(type(self_).__name__)
            if isinstance(self_, etrakit._PinnedAdapter):
                return "pinned"
            raise etrakit.requests.exceptions.SSLError("hostname mismatch")

        with mock.patch.object(etrakit.requests.adapters.HTTPAdapter, "send", send):
            self.assertEqual(adapter.send(mock.Mock()), "pinned")
            self.assertEqual(adapter.send(mock.Mock()), "pinned")
        self.assertEqual(calls, ["_PinFallbackAdapter", "_PinnedAdapter", "_PinnedAdapter"])

    def test_pinned_pool_asserts_the_fingerprint(self):
        pinned = etrakit._PinnedAdapter(PIN)
        self.assertEqual(pinned.poolmanager.connection_pool_kw["assert_fingerprint"], PIN)

    def test_other_errors_do_not_trigger_the_pin(self):
        adapter = self._session().get_adapter("https://x/")
        with mock.patch.object(etrakit.requests.adapters.HTTPAdapter, "send",
                               side_effect=etrakit.requests.exceptions.ConnectionError("reset")):
            with self.assertRaises(etrakit.requests.exceptions.ConnectionError):
                adapter.send(mock.Mock())
        self.assertFalse(adapter._use_pin)


@unittest.skipIf(etrakit is None, "requests/bs4 not installed")
class TestPersistentFailures(unittest.TestCase):
    """A partial failure exits nonzero only when the same item keeps failing."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        log = Path(self.tmp.name) / "errors.jsonl"
        patcher = mock.patch.object(etrakit.civic_utils, "ERRORS_LOG", log)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)

    def _fail_in_runs(self, n, item="BLDG26-0302", phase="enrich"):
        failed = []
        for i in range(n):
            with mock.patch.dict(etrakit.os.environ, {"WATCHDOG_RUN_ID": f"run-{i}"}):
                failed = []
                etrakit._request_error("oceanside", phase, item, TimeoutError("read"), failed)
        return failed

    def test_two_runs_is_routine(self):
        failed = self._fail_in_runs(2)
        self.assertEqual(etrakit.persistent_failures("oceanside", "enrich", failed), [])
        etrakit._finish("oceanside", "enrich", failed)  # returns, no exit

    def test_three_runs_exits_nonzero(self):
        failed = self._fail_in_runs(3)
        self.assertEqual(etrakit.persistent_failures("oceanside", "enrich", failed),
                         ["BLDG26-0302"])
        with self.assertRaises(SystemExit) as cm:
            etrakit._finish("oceanside", "enrich", failed)
        self.assertEqual(cm.exception.code, etrakit.EXIT_REQUEST_ERRORS)

    def test_same_run_counts_once(self):
        with mock.patch.dict(etrakit.os.environ, {"WATCHDOG_RUN_ID": "one"}):
            failed = []
            for _ in range(5):
                etrakit._request_error("oceanside", "enrich", "BLDG26-0302",
                                       TimeoutError("read"), failed)
        self.assertEqual(etrakit.persistent_failures("oceanside", "enrich", failed), [])

    def test_other_phase_and_old_records_do_not_count(self):
        self._fail_in_runs(3, phase="refresh")
        self.assertEqual(etrakit.persistent_failures(
            "oceanside", "enrich", ["BLDG26-0302"]), [])
        later = etrakit.datetime.now() + etrakit.timedelta(days=etrakit.PERSIST_DAYS)
        self.assertEqual(etrakit.persistent_failures(
            "oceanside", "refresh", ["BLDG26-0302"], now=later), [])

    def test_an_item_that_recovered_is_not_reported(self):
        self._fail_in_runs(3)
        # This run it succeeded, so it is not in this run's failed list.
        self.assertEqual(etrakit.persistent_failures("oceanside", "enrich", []), [])

    def test_outage_still_exits_with_no_history(self):
        with self.assertRaises(SystemExit):
            etrakit._finish("oceanside", "permits", [], outage="scan stopped")


@unittest.skipIf(etrakit is None, "requests/bs4 not installed")
class TestMissOrError(unittest.TestCase):
    """Only eTRAKiT's own not-found page is a miss; any other empty answer is an error."""

    # Recorded from Oceanside on 2026-11-27 for BLDG26-1855. Kept beside this file:
    # .gitignore's `data/` would hide a tests/data/ directory.
    NOT_FOUND = (Path(__file__).parent / "etrakit-permit-not-found.html").read_text()
    PERMIT = ('<span id="ctl00_cplMain_lblPermitType">BLD ACCESSORY DWELLING</span>'
              '<span id="ctl00_cplMain_lblPermitStatus">RECEIVED</span>')

    def _fetch(self, status, body):
        session = mock.Mock()
        session.get.return_value = mock.Mock(status_code=status, text=body)
        return etrakit.fetch_permit(session, "https://x/etrakit3", "BLDG26-1849")

    def test_recorded_not_found_page_is_a_miss(self):
        self.assertIsNone(self._fetch(500, self.NOT_FOUND))

    def test_a_permit_page_parses(self):
        p = self._fetch(200, self.PERMIT)
        self.assertEqual((p["type"], p["status"]), ("BLD ACCESSORY DWELLING", "RECEIVED"))

    def test_a_different_500_is_an_error(self):
        with self.assertRaises(etrakit.PermitPageError):
            self._fetch(500, "<h1>Service Unavailable</h1>")

    def test_a_200_with_no_permit_is_an_error(self):
        # A maintenance or login page answering 200 used to count as a miss, and
        # fifty of those in a row read as "no new permits".
        with self.assertRaises(etrakit.PermitPageError):
            self._fetch(200, "<html><title>Scheduled maintenance</title></html>")

    def test_the_marker_on_a_200_is_not_a_miss(self):
        with self.assertRaises(etrakit.PermitPageError):
            self._fetch(200, "<p>Search_Permit.PermitDetails</p>")


class TestDoctorSeparatesRequestErrors(unittest.TestCase):
    """Per-request eTRAKiT records must not read as an agency failure."""

    def test_request_records_are_counted_apart(self):
        import json
        import tempfile
        import pipeline_doctor
        recs = [
            {"ts": "2026-11-28T01:05:00", "run_id": "r1", "agency": "oceanside",
             "phase": "etrakit enrich", "item": "BLDG26-0302", "error": "ReadTimeout"},
            {"ts": "2026-11-28T01:06:00", "run_id": "r1", "agency": "oceanside",
             "phase": "City of Oceanside permit enrichment", "rc": 1},
            {"ts": "2026-11-28T01:07:00", "run_id": "r1", "agency": "del_mar",
             "phase": "download", "msg": "non-PDF response"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "errors.jsonl"
            log.write_text("".join(json.dumps(r) + "\n" for r in recs))
            with mock.patch.object(pipeline_doctor, "ERRORS_LOG", log), \
                    mock.patch.object(pipeline_doctor, "PREFLIGHT_HEALTH", Path(tmp) / "none"):
                out = pipeline_doctor.get_structured_failures()
        # The phase failure still counts against the agency; the request does not.
        self.assertEqual([r["phase"] for r in out["agencies"]["oceanside"]],
                         ["City of Oceanside permit enrichment"])
        self.assertEqual(out["agencies"]["del_mar"][0]["phase"], "download")
        self.assertEqual([r["item"] for r in out["requests"]["oceanside"]], ["BLDG26-0302"])
        self.assertEqual(out["total"], 2)

    def test_a_run_with_only_request_errors_has_no_failed_agency(self):
        import json
        import tempfile
        import pipeline_doctor
        rec = {"ts": "2026-11-28T01:05:00", "run_id": "r1", "agency": "oceanside",
               "phase": "etrakit permits", "item": "BLDG26-1849", "error": "ReadTimeout"}
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "errors.jsonl"
            log.write_text(json.dumps(rec) + "\n")
            with mock.patch.object(pipeline_doctor, "ERRORS_LOG", log), \
                    mock.patch.object(pipeline_doctor, "PREFLIGHT_HEALTH", Path(tmp) / "none"):
                out = pipeline_doctor.get_structured_failures()
        self.assertEqual(out["agencies"], {})


if __name__ == "__main__":
    unittest.main()
