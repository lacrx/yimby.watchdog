---
id: 2026-11-22-alert-delivery
tier: 2
status: accepted
quiet: yes
paths:
  - pipeline_doctor.py
  - scripts/alert_triage.py
  - tests/test_alert_triage.py
  - tests/alert-triage-cases.json
---

# A diagnosis nobody reads is a diagnosis that did not happen

## Problem

`pipeline_doctor.py` runs at the end of every `civic-pipeline` cron run, twice a day. It
appends its findings to `data/pipeline-doctor.jsonl` and prints them into
`data/pipeline-cron.log`. **Nothing reads either file.** There is no reader, no digest, no
notification and no exit code anybody watches: the doctor writes a correct diagnosis into a
file and the run ends.

On 2026-11-22 it found two real things. North County Transit District had served no dated
meeting document since 2026-10-22, meaning a month of that agency's meetings are simply
absent, and Del Mar had a download failure. Both were correct. The only reason either was
read is that somebody ran `pipeline_doctor.py` by hand that morning.

This is the same shape as the frontend deploy token that expired on 2026-10-30 and went
unread for 23 days: the system knew, said so in a place with no reader, and carried on. The
cost of the shape is measured in weeks, not minutes.

The channel to fix it already existed and was unused. `brain-sync-alert` writes
`~/.claude/state/brain-sync-alert.md`, and since 2026-11-14 `claude-state-hook` injects
**every** `~/.claude/state/*-alert.md` at SessionStart, so any tool can ride it without
editing the hook. Nothing but the brain sync and the weekly spec checks ever did.

### Why alerting on "the doctor found something" is not the fix

Measured over the 260 recorded diagnoses in `data/pipeline-doctor.jsonl`, 2026-06-24 to
2026-11-22:

| | |
|---|---|
| runs recorded | 260 |
| runs whose status was not `healthy` | 249 (96%) |
| runs carrying a finding also present in the run before | 244 (94%) |
| total findings and recommendations | 2,251 |

So neither "it found something" nor "it persisted" narrows anything. An alert on either
fires nearly every night, and an alert that fires nearly every night is read once. That is
worse than no alert, because it looks like coverage.

The findings are not equally urgent. 1,683 of the 2,251 are a slow phase or a handful of
per-request fetch errors inside a run that still returned documents.

## Invariants

- **S1** WHEN the doctor finishes a non-dry run, it SHALL deliver its alertable findings to
  `~/.claude/state/civic-pipeline-alert.md` via `job-alert`, so they reach a surface a
  person opens without going to look for them.
- **S2** A finding SHALL be alertable only when it means meeting content is being missed
  and nothing else is saying so (`loss`), or when no pattern recognises it (`unknown`).
  A slow phase, a per-request error count, a rate limit, or a known data-quality backlog
  item SHALL NOT raise.
- **S3** An unrecognised finding SHALL be treated as alertable, not as routine, for the
  same reason `replay_failure.py` refuses a fixture whose kind it cannot tell: a finding the
  triage has never seen is by definition new.
- **S4** WHEN a run produces no alertable finding, the alert file SHALL be removed, so its
  presence always means the current state and never a stale event.
- **S5** `--dry-run` SHALL neither raise nor clear an alert, matching its existing promise
  that it writes nothing.
- **S6** Failing to deliver an alert SHALL NOT fail the diagnosis that produced it, and the
  diagnosis SHALL be persisted before delivery is attempted.
- **S7** The same sentence reporting a coverage gap SHALL be classified by its percentage:
  at or above `COVERAGE_LOSS_PERCENT` it is `loss`, below it is `backlog`.
- **S8** The classification of every finding shape in the recorded history SHALL be
  committed as a corpus and checked by tests that need no data directory and no third-party
  package.

## Blast radius

One call at the end of `diagnose()`, after the persist, wrapped so nothing it does can
raise. A new module that imports only `re`. A test directory. `pipeline-doctor.jsonl` keeps
its exact shape, which matters because `show_history` and `build_autofix_prompt` both read
it.

The alert file is injected into every new Claude Code session on this machine until it
clears, so the body is capped at 1,800 characters by `job-alert`. `~/.claude/state/` is not
synced to neo, which is correct: this pipeline does not run there.

**What this accepts.** Patterns matching the doctor's own wording are coupled to that
wording. Reword a finding and it becomes `unknown`, which raises an alert rather than
hiding: the failure direction is loud. The alternative was labelling severity at each of the
46 `findings.append` sites, which changes the recorded format, cannot be checked against
five months of history, and would have been a much larger edit to a file the cron runs
within hours.

**What this is not.** Not a notification system. Nothing is delivered and nothing pushes;
the alert waits in a file. The household Geoff page was the other candidate and was
rejected: its notices are shown to both people, a civic-scraper diagnosis is noise on
Sarah's page, and the vault's own convention is that nothing nags.

**Expected rate.** Over the 260 recorded runs, 69 (27%) would have raised. In the current
regime the shape is better than that average: runs from 2026-11-17 to 2026-11-21 raise
nothing at all, the five before them raise the Opus safeguards crash and two failed fetches,
and 2026-11-22 raises the stale NCTD scraper. Every one of those was real.

## Verification

| Invariant | How |
|---|---|
| S1 | `deliver_alert` run against the real last line of `pipeline-doctor.jsonl` with `HOME` redirected to a scratch directory: wrote `civic-pipeline-alert.md` naming the NCTD finding and its reason. `_job_alert()` resolved to `/home/thomas/.local/bin/job-alert`. |
| S2 | Replayed all 260 recorded diagnoses through `alertable()`: 190 of 2,251 items are `loss`, 1,683 `degraded`, 378 `backlog`. 69 runs raise. `test_loss_alerts_and_the_rest_does_not` asserts it per case. |
| S3 | `test_unknown_is_alertable`. Tampering `ALERTABLE` to `("loss",)` fails 1 test. |
| S4 | Same sandbox run, second call with `{"findings": [], "recommendations": []}`: returned `False` and the file was gone. |
| S5 | Same sandbox run with `dry_run=True`: printed `Dry run: alert not raised or cleared.`, returned `None`, wrote nothing. |
| S6 | The call sits after the `DIAGNOSIS_LOG` append. Triage errors, a missing `job-alert` and a missing module each print a `WARN:` line and return `None`. |
| S7 | `test_at_the_threshold_is_loss`, `test_just_below_the_threshold_is_backlog`, `test_the_extremes`. Tampering the threshold to 10 fails 2 tests. |
| S8 | `tests/alert-triage-cases.json`: 98 real finding strings, at most three per shape, covering all 124 distinct families. `python3 -m unittest discover -s tests -v` is 18 tests, 0.008s, no data directory, standard library only. |

Four tamper runs against a throwaway copy, each failing with the string that moved:
deleting the stale-scraper loss pattern (9 failures), dropping `unknown` from `ALERTABLE`
(1), loosening the coverage threshold to 10% (2), and promoting per-request fetch errors to
`loss` (22 subtest failures). Restoring the file returns 18 passing.

`job-alert` itself was exercised under `bash:3.2` in Docker, which is neo's shell: parses,
raises, lists and clears.

## Rejected

- **Alerting on the doctor's status.** 249 of 260 runs are not `healthy`. Measured before
  writing anything, which is the only reason this file does not contain that mistake.
- **Alerting on a finding that persisted two runs.** 244 of 260 qualify. The pipeline ran
  with a standing null-date backlog for months, so persistence selects the permanent
  conditions and misses the new break.
- **Alerting on novelty: a finding absent from the previous run.** 94 of 260 raise, and it
  cannot distinguish a new agency breaking from a run id changing inside the same message.
  It also re-raises every time a standing condition briefly clears.
- **Labelling severity at the 46 `findings.append` sites.** More honest and the right
  eventual shape. It changes `pipeline-doctor.jsonl`, which two other functions read, and it
  forfeits the one strong verification available here: replaying five months of recorded
  diagnoses.
- **Posting to the household Geoff page (`app/tools/notices.py`).** It survives restarts and
  is read on a phone. It is also shown to both people, and the vault states that nothing
  nags. A scraper diagnosis is not household news.
- **A new notification service, email, or a push.** The channel that is read many times a
  day already exists. Adding delivery infrastructure to a problem whose cause was "no
  reader" is how the next thing with no reader gets built.
- **Alerting on a `.docx` download failure.** Counted on 2026-11-22: 24 of 26 non-PDF
  responses since 2026-06-09 come from two URLs. These are `degraded`, and
  `2026-11-22-fixture-kinds.md` records why supporting the type was declined.
