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


if __name__ == "__main__":
    unittest.main()
