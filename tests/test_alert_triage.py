#!/usr/bin/env python3
"""The triage decides what interrupts a person, so a silent mistake in it is expensive.

Two opposite failures matter here and they pull against each other:

* Classifying a real loss as routine. The alert then exists and says nothing, which is
  worse than no alert because it looks like coverage.
* Classifying routine noise as a loss. 1,683 of the 2,251 recorded findings are
  `degraded`. Let a few of those families through and the alert fires most nights, and an
  alert that fires most nights is read once.

So the corpus is the test. `alert-triage-cases.json` holds real finding strings taken from
`data/pipeline-doctor.jsonl` (260 runs, 2026-06-24 to 2026-11-22), at most three per shape,
with the severity each was given. The severities were reviewed by hand when the file was
recorded; from here on this is a regression corpus, and any pattern edit that moves a known
family turns a test red with the string that moved.

`data/` is gitignored, so the corpus is the committed stand-in for the history and this runs
with no data directory and no dependencies beyond the standard library.

    python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))  # scripts/ is not a package

import alert_triage  # noqa: E402

CASES_FILE = Path(__file__).resolve().parent / "alert-triage-cases.json"


def load_cases() -> list[dict]:
    return json.loads(CASES_FILE.read_text())["cases"]


class TestCorpus(unittest.TestCase):
    """Every recorded finding still classifies the way it was reviewed."""

    def setUp(self):
        self.cases = load_cases()

    def test_corpus_is_not_empty(self):
        # A corpus emptied by a bad edit would make every other test here pass.
        self.assertGreater(len(self.cases), 50, "the corpus lost most of its cases")

    def test_every_case_classifies_as_recorded(self):
        moved = [
            (c["finding"], c["severity"], alert_triage.severity(c["finding"]))
            for c in self.cases
            if alert_triage.severity(c["finding"]) != c["severity"]
        ]
        if moved:
            lines = [
                f"  {was} -> {now}: {text[:110]}" for text, was, now in moved
            ]
            self.fail(
                f"{len(moved)} finding(s) changed severity:\n" + "\n".join(lines)
            )

    def test_no_recorded_case_is_unknown(self):
        # Every case here was recognised when recorded, so an unknown means a pattern was
        # deleted or narrowed. That is worth failing on separately: it would otherwise show
        # up only as a louder alert.
        orphans = [
            c["finding"] for c in self.cases
            if alert_triage.severity(c["finding"]) == "unknown"
        ]
        self.assertEqual(orphans, [], "no pattern recognises these any more")

    def test_corpus_covers_all_three_severities(self):
        got = {alert_triage.severity(c["finding"]) for c in self.cases}
        self.assertEqual(got, {"loss", "degraded", "backlog"})

    def test_loss_alerts_and_the_rest_does_not(self):
        for case in self.cases:
            with self.subTest(finding=case["finding"][:60]):
                urgent = alert_triage.alertable({"findings": [case["finding"]]})
                if case["severity"] == "loss":
                    self.assertEqual(urgent, [case["finding"]])
                else:
                    self.assertEqual(urgent, [], "this would wake somebody up")

    def test_every_case_has_a_recorded_reason(self):
        fallback = alert_triage.why("a string no pattern matches, 4b8f21")
        for case in self.cases:
            with self.subTest(finding=case["finding"][:60]):
                reason = alert_triage.why(case["finding"])
                self.assertTrue(reason.strip())
                self.assertNotEqual(
                    reason, fallback, "classified, but with no reason recorded"
                )


class TestUnknownIsLoud(unittest.TestCase):
    """A finding the triage has never seen must not be assumed routine."""

    def test_unrecognised_finding_is_unknown(self):
        self.assertEqual(
            alert_triage.severity("Flux capacitor desynchronised on 3 nodes"), "unknown"
        )

    def test_unknown_is_alertable(self):
        # The same rule as replay_failure.py refusing a fixture whose kind it cannot tell.
        # If this ever stops being true, a brand new failure mode is filed as routine.
        self.assertIn("unknown", alert_triage.ALERTABLE)
        urgent = alert_triage.alertable(
            {"findings": ["Flux capacitor desynchronised on 3 nodes"]}
        )
        self.assertEqual(urgent, ["Flux capacitor desynchronised on 3 nodes"])

    def test_unknown_says_what_to_do(self):
        reason = alert_triage.why("Flux capacitor desynchronised on 3 nodes")
        self.assertIn("alert_triage.py", reason)


class TestCoverageThreshold(unittest.TestCase):
    """The same sentence is a loss or a backlog item depending on one number."""

    def _finding(self, pct):
        return f"City of Testville: 1/2 past meetings ({pct}%) have no documents (7-90d window)"

    def test_at_the_threshold_is_loss(self):
        pct = alert_triage.COVERAGE_LOSS_PERCENT
        self.assertEqual(alert_triage.severity(self._finding(pct)), "loss")

    def test_just_below_the_threshold_is_backlog(self):
        pct = alert_triage.COVERAGE_LOSS_PERCENT - 1
        self.assertEqual(alert_triage.severity(self._finding(pct)), "backlog")

    def test_the_extremes(self):
        self.assertEqual(alert_triage.severity(self._finding(2)), "backlog")
        self.assertEqual(alert_triage.severity(self._finding(94)), "loss")

    def test_each_side_carries_a_different_reason(self):
        low = alert_triage.why(self._finding(2))
        high = alert_triage.why(self._finding(94))
        self.assertNotEqual(low, high)
        self.assertTrue(low.strip() and high.strip())


class TestTriageShape(unittest.TestCase):
    def test_recommendations_are_triaged_too(self):
        # 45 of the 260 recorded runs carry recommendations, and "needs manual
        # investigation" is the doctor's own escalation. Reading findings only would drop it.
        urgent = alert_triage.alertable(
            {
                "findings": ["Slow phase: City of Testville meetings took 30min"],
                "recommendations": [
                    "Previous automatic fixes did not help — needs manual investigation"
                ],
            }
        )
        self.assertEqual(len(urgent), 1)
        self.assertIn("manual investigation", urgent[0])

    def test_duplicates_collapse(self):
        same = "SANDAG: no dated meeting document since 2026-09-25 (scraper may be blocked)"
        urgent = alert_triage.alertable({"findings": [same, same], "recommendations": [same]})
        self.assertEqual(urgent, [same])

    def test_a_healthy_diagnosis_raises_nothing(self):
        self.assertEqual(alert_triage.alertable({"findings": [], "recommendations": []}), [])
        self.assertEqual(alert_triage.alertable({}), [])

    def test_missing_and_empty_values_do_not_raise(self):
        # The doctor builds this dict over 1,500 lines and `default=str` is used when it is
        # written, so being tolerant here is cheaper than trusting the shape.
        self.assertEqual(alert_triage.alertable({"findings": None}), [])
        self.assertEqual(alert_triage.alertable({"findings": ["", "   "]}), [])
        self.assertEqual(alert_triage.severity(""), "backlog")
        self.assertEqual(alert_triage.severity(None), "backlog")

    def test_non_string_findings_are_tolerated(self):
        grouped = alert_triage.triage({"findings": [42, None, "Slow phase: x took 1min"]})
        self.assertIn("degraded", grouped)


if __name__ == "__main__":
    unittest.main()
