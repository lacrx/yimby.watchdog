#!/usr/bin/env python3
"""Decide which of the doctor's findings are worth interrupting a person for.

Why this is not "alert when the doctor finds something"
-------------------------------------------------------
Measured across the 260 recorded diagnoses in `data/pipeline-doctor.jsonl`
(2026-06-24 to 2026-11-22): 249 of them were not `healthy`. Alerting on status
would fire on 96% of runs, and 94% of runs carry a finding that was also present
in the run before, so "it persisted" does not narrow it either. An alert that
fires almost every night is an alert nobody reads, which is worse than none: it
trains the reader to dismiss the one night that mattered.

The findings are not equally urgent, and the useful split is not by count but by
consequence:

* **loss** — meeting content is being missed and nothing else says so. A scraper
  that has stopped returning dated documents, a run that never finished, a
  crashed extractor, an agency whose past meetings have no documents at all.
  This is the class the expired frontend token belonged to: silent, cheap to
  miss, expensive to miss for three weeks.
* **degraded** — it works and it works worse. A slow phase, a handful of fetch
  errors in the log, a rate limit that resets by itself. Visible in the log and
  in the doctor's own output; interrupting somebody for it buys nothing.
* **backlog** — a queue of known data-quality work: records with no date, docs
  too big to extract, a file that has failed extraction several times. These
  stand for weeks by design and are worked through deliberately, not tonight.
* **unknown** — a finding no pattern here recognises. Treated as alertable, for
  the same reason `replay_failure.py` refuses a fixture whose kind it cannot
  tell: a thing the triage has never seen is by definition new, and guessing it
  is harmless is how a new failure mode gets silently filed under routine.

So an unrecognised finding is a loud failure of this file, not a quiet one, and
the fix is to add a pattern with a reason.

The patterns match the doctor's own strings, which makes this coupled to its
wording. That is deliberate and it is the cheaper half of the trade: labelling
severity at each of the 46 `findings.append` sites would change the shape of
`pipeline-doctor.jsonl`, which `show_history` and `build_autofix_prompt` both
read, and could not be checked against five months of recorded history. This can
be, and is: see `tests/test_alert_triage.py`.
"""

from __future__ import annotations

import re

# (severity, compiled pattern, why it is that severity).
#
# First match wins, so order within a severity does not matter but order between
# them does: a more specific loss pattern must come before a broader degraded one.
_RULES: list[tuple[str, re.Pattern, str]] = [
    # ── loss: content is being missed and nothing else is saying so ──
    (
        "loss",
        re.compile(r"no dated meeting document", re.I),
        "The scraper is returning nothing dated for an agency, so its meetings "
        "are simply absent. This is the shape that goes unnoticed longest.",
    ),
    (
        "loss",
        re.compile(r"silent >\d+d|latest scrape parsed 0 meetings", re.I),
        "An agency that meets on a cadence has gone quiet past it, or its "
        "listing parsed nothing at all. Both mean the scraper, not the city.",
    ),
    (
        "loss",
        re.compile(r"^Latest pipeline run:.*\bERROR\b.*fetch failed", re.I),
        "A named agency's fetch failed in the most recent run, so that "
        "agency contributed nothing to it.",
    ),
    (
        "loss",
        re.compile(r"produced zero extractions", re.I),
        "Documents are arriving and nothing is being extracted from them.",
    ),
    (
        "loss",
        re.compile(r"Extraction script crashed", re.I),
        "A crash, not a degradation: whatever was in that window is lost until "
        "someone looks.",
    ),
    (
        "loss",
        re.compile(r"was terminated/killed", re.I),
        "The run did not finish, so its scrape is partial and nothing retries "
        "it before the next scheduled run.",
    ),
    (
        "loss",
        re.compile(r"^Overlapping run:", re.I),
        "A previous run never completed. Two scrapes writing the same data dirs "
        "is how a half-written record happens.",
    ),
    (
        "loss",
        re.compile(r"needs manual investigation|did not help", re.I),
        "The doctor's own escalation: it applied fixes, they changed nothing, "
        "and it is explicitly asking for a person.",
    ),
    # ── degraded: working, and working worse ──
    (
        "degraded",
        re.compile(r"^Slow phase:", re.I),
        "Slow is not broken. 452 of the recorded findings are slow phases and "
        "none of them cost a document.",
    ),
    (
        "degraded",
        re.compile(r"fetch had \d+ error\(s\) in recent log", re.I),
        "The single largest family in the history and the least actionable: a "
        "few per-request errors inside a run that still returned documents.",
    ),
    (
        "degraded",
        re.compile(r"rate-limited|Session limit hit", re.I),
        "Self-healing. The window is wasted and the next one is not, and the "
        "backlog is picked up by the cold extraction pass.",
    ),
    (
        "degraded",
        re.compile(r"failed \d+x in run \S+", re.I),
        "A per-document download or parse failure, already recorded in "
        "errors.jsonl and replayable from a fixture. Counting these on "
        "2026-11-22 found 24 of 26 non-PDF responses came from two URLs, so "
        "most are a known unsupported type rather than a break.",
    ),
    (
        "degraded",
        re.compile(r"^Auto-fix (failed|timed out)", re.I),
        "The self-healer failing does not itself lose content. Whatever it was "
        "trying to fix reports itself under its own finding.",
    ),
    (
        "degraded",
        re.compile(r"claude CLI not (available|in PATH)", re.I),
        "Auto-fix is off for this run. The diagnosis still happened and still "
        "persisted.",
    ),
    (
        "degraded",
        re.compile(r"JSON parse error rate|--json-schema", re.I),
        "A flag to check, with the extraction log naming the records. Worth "
        "doing, not worth waking for.",
    ),
    # ── backlog: a known queue, worked through deliberately ──
    (
        "backlog",
        re.compile(r"null date", re.I),
        "Records needing a date assigned. 96 of the recorded recommendations "
        "are this and they stand for weeks by design.",
    ),
    (
        "backlog",
        re.compile(r"Oversized meeting doc needs split|needs split:", re.I),
        "A document too big for one extraction pass. Queued work with a known "
        "procedure.",
    ),
    (
        "backlog",
        re.compile(r"^Extraction failing:", re.I),
        "A named file failing repeatedly. It is counted, it is capped by the "
        "skip rules, and it does not spread.",
    ),
    (
        "backlog",
        re.compile(r"No obvious blocker found|check structured-extraction\.log", re.I),
        "The doctor saying it could not tell. A pointer at a log, not an event.",
    ),
    (
        "backlog",
        re.compile(r"closed session|undocumented closed", re.I),
        "Closed sessions have no public documents to find, so an absence here "
        "is usually correct.",
    ),
    (
        "backlog",
        re.compile(r"no parseable date|excluded from coverage check", re.I),
        "Rows the coverage check cannot judge. A counting problem, not a "
        "fetching one.",
    ),
    (
        "backlog",
        re.compile(r"blocked extraction \d+ times", re.I),
        "A single file defeating the chunker. The skip rules cap it at three "
        "attempts and it does not spread to other files.",
    ),
    (
        "degraded",
        re.compile(r"eScribe downloads paused|cf_bypass failed|Cloudflare challenge", re.I),
        "Cloudflare, which escribe-sweep retries five times a day. Paused is "
        "the designed response to it, not a fault.",
    ),
    (
        "degraded",
        re.compile(r"firecrawl CLI not installed", re.I),
        "A fallback is unavailable while the primary path still works. Worth "
        "fixing, and nothing is lost while it waits.",
    ),
]

# ── the coverage gap, which needs a number rather than a pattern ──
#
# `<Agency>: 1/60 past meetings (2%) have no documents` and
# `<Agency>: 7/9 past meetings (78%) have no documents` are the same sentence and
# not the same event. One agency hiccupped on one meeting; the other is listing
# meetings and fetching none of their documents. Across the recorded history the
# percentage runs the whole range from 2 to 94, so the sentence alone cannot say
# which. Half is the line: at or above it the scraper is finding listings and not
# documents, which is the silent-loss shape. Below it, it is patchy coverage and
# goes on the backlog.
_COVERAGE = re.compile(r"past meetings \((\d+)%\) have no documents", re.I)
COVERAGE_LOSS_PERCENT = 50

_COVERAGE_WHY_LOSS = (
    "At or above half the window, the agency's listings are being found and its "
    "documents are not, which is the scraper rather than the city."
)
_COVERAGE_WHY_BACKLOG = (
    "Patchy coverage below half the window. Individual meetings missing a "
    "document is ordinary; it is the share that distinguishes a broken fetch."
)

# What is worth writing an alert file for. `unknown` is in here on purpose.
ALERTABLE = ("loss", "unknown")


def severity(text: str) -> str:
    """Classify one finding. Returns `loss`, `degraded`, `backlog` or `unknown`."""
    text = (text or "").strip()
    if not text:
        return "backlog"
    hit = _COVERAGE.search(text)
    if hit:
        return "loss" if int(hit.group(1)) >= COVERAGE_LOSS_PERCENT else "backlog"
    for sev, pattern, _why in _RULES:
        if pattern.search(text):
            return sev
    return "unknown"


def triage(diagnosis: dict) -> dict[str, list[str]]:
    """Group one diagnosis's findings and recommendations by severity.

    Takes the diagnosis dict the doctor builds, or any mapping with `findings`
    and `recommendations` lists, so a recorded line from `pipeline-doctor.jsonl`
    works unchanged.
    """
    out: dict[str, list[str]] = {}
    seen: set[str] = set()
    items = list(diagnosis.get("findings") or []) + list(
        diagnosis.get("recommendations") or []
    )
    for item in items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.setdefault(severity(text), []).append(text)
    return out


def alertable(diagnosis: dict) -> list[str]:
    """The findings that justify an alert file, in severity order."""
    grouped = triage(diagnosis)
    out: list[str] = []
    for sev in ALERTABLE:
        out.extend(grouped.get(sev, []))
    return out


def why(text: str) -> str:
    """The recorded reason a finding is classified the way it is.

    Carried into the alert body so the file says why it is interrupting, rather
    than only what it found.
    """
    hit = _COVERAGE.search(text or "")
    if hit:
        return (
            _COVERAGE_WHY_LOSS
            if int(hit.group(1)) >= COVERAGE_LOSS_PERCENT
            else _COVERAGE_WHY_BACKLOG
        )
    for _sev, pattern, reason in _RULES:
        if pattern.search(text or ""):
            return reason
    return (
        "No pattern in scripts/alert_triage.py recognises this finding, so it is "
        "treated as new rather than assumed routine. If it is routine, add a "
        "pattern there with the reason."
    )
