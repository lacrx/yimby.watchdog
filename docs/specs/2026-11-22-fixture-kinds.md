---
id: 2026-11-22-fixture-kinds
tier: 2
status: accepted
quiet: yes
paths:
  - scripts/replay_failure.py
  - scripts/fixtures.py
  - civic_utils.py
  - .github/workflows/fixtures.yml
---

# A replay that cries wolf is worse than no replay

## Problem

The pipeline produced its first real failure fixture on 2026-11-22, in run
`20261122T090001Z-4006352`. Del Mar's AgendaCenter answered a document request
with HTTP 200 and
`application/vnd.openxmlformats-officedocument.wordprocessingml.document`:
a 35,389-byte Word file. `_save_pdf` rejected it for not starting with `%PDF`,
which is correct, and `download_pdf` captured it.

Replaying that fixture printed:

```
REPLAY FAIL: listing parsed 0 items
exit 3
```

Nothing was broken. `replay_failure.py` ran the CivicPlus *listing* parser
against a *document*, that parser correctly found no meeting rows inside a
meeting-cancellation notice, and the script reported a parse regression that
did not exist. Unpacking the captured bytes by hand gives a valid .docx, 19 zip
entries, 214 characters of extractable text, reading `MEASURE Q CITIZEN
OVERSIGHT COMMITTEE Meeting Cancellation Notice`.

Every fixture the pipeline can currently produce comes from `download_pdf`, so
every fixture is a document and none of them was a listing. The script had one
code path and it was the wrong one for all of them.

`scripts/fixtures.py` shared the assumption: `_parse_count` always called a
listing parser, so a document fixture could not be promoted into the corpus at
all. It would parse zero and be recorded as a permanent expected failure.

Separately, `yimby.watchdog` had **no GitHub workflows**. `fixtures.py check`
was written on 2026-11-21 as a regression gate and nothing ran it.

This matters more than an ordinary wrong message because of what the fixtures
are for. They are the evidence an autonomous loop would act on. A loop handed
`REPLAY FAIL` for an intact .docx would spend its budget rewriting a parser
that works.

## Invariants

- **S1** WHEN a fixture is captured, `save_failure_fixture` SHALL record the
  `phase` the request was made in, so a replay never has to infer the kind.
- **S2** IF a fixture has no `phase`, the kind SHALL be taken from the captured
  response Content-Type, so fixtures captured before this change stay usable.
- **S3** IF neither a `phase` nor a usable Content-Type is present,
  `replay_failure.py` and `fixtures.py add` SHALL refuse the fixture rather
  than choose a kind.
- **S4** WHEN a document fixture is replayed, the tool SHALL report a valid
  document of an unsupported type as a distinct outcome (exit 4) from something
  broken (exit 3).
- **S5** A listing fixture SHALL keep the existing convention: parsed at least
  one item is exit 0, parsed nothing or raised is exit 3.
- **S6** `fixtures.py check` SHALL enforce an expectation for both kinds: an
  exact item count for a listing, and the recorded label and verdict for a
  document, exiting 1 on any mismatch.
- **S7** WHILE a push or pull request is open, CI SHALL run `fixtures.py check`
  and SHALL fail the run on a mismatch.

## Blast radius

Offline tooling and one capture field. Nothing here runs during a pipeline
scrape except the added `phase` key in a fixture written by `download_pdf`,
which is a new dictionary entry in a file that is already gitignored scratch.
`save_failure_fixture` still cannot raise.

The corpus is now a corpus of two kinds. An existing listing fixture is
unaffected: `check` reads `expect_items` first and only falls back to inferring
a kind for a fixture hand-written into `fixtures/` with no expectation at all.

The cost of exit 4 is that a genuinely missing feature now reads as "not a bug".
That is deliberate and it is a trade: Del Mar's Word agendas are not being
ingested, and this change makes that a decision to take rather than a parser
alarm to chase. The doctor still counts the failure, and the agency still shows
up in `errors.jsonl`.

CI installs five packages rather than `requirements.txt`, so a parser gaining a
dependency outside that five will fail CI with an ImportError rather than
silently skipping. That is the right failure: the fix is one line in the
workflow.

## Verification

| Invariant | How |
|---|---|
| S1 | `download_pdf` passes `phase="download"` at both capture sites. A fixture written by the non-PDF branch carries `"phase": "download"`. |
| S2 | The real Del Mar fixture predates this change and has no `phase`. Replaying it prints `kind: document (from content-type application/vnd.openxmlformats-officedocument.wordprocessingml.document)` and reaches the document path. |
| S3 | A synthetic fixture with neither field exits 2 with `cannot tell whether this is a document or a listing`. `fixtures.py add` refuses it with the same reasoning. |
| S4 | Twelve synthetic fixtures covering PDF, DOCX, XLSX, legacy OLE2, corrupt ZIP, Cloudflare interstitial, login wall, plain HTML, unrecognised bytes, an empty body, a listing and an ambiguous case. Exits observed: PDF 0; DOCX, XLSX and OLE2 4; corrupt ZIP, Cloudflare, login wall, plain HTML and unrecognised 3; empty body and ambiguous 2. |
| S5 | The listing case in that set routes to the parser and exits 3 on 0 items, unchanged. |
| S6 | Three tamper runs against the committed Del Mar fixture: claiming `PDF/ok` gives `classifies as DOCX/unsupported, expected PDF/ok`; removing `expect_document` gives `no expect_document recorded`; corrupting the body to `PK\x03\x04garbage` gives `classifies as ZIP/broken, expected DOCX/unsupported`. All exit 1. Restoring the file returns `2 fixtures ok`, exit 0. |
| S7 | `.github/workflows/fixtures.yml` runs `check -v` and `compileall`. Both were run locally exactly as the workflow runs them: a clean venv with only the five pinned packages passes `check` (`2 fixtures ok`), and `compileall -q . -x '\.venv\|node_modules'` exits 0. The workflow parses under `yaml.safe_load`. |

The real Del Mar fixture is promoted to `fixtures/civicplus/del-mar.json` with
`expect_document: {label: DOCX, verdict: unsupported}`, so the corpus now
permanently holds the case that found this.

## Rejected

- **Supporting .docx now.** Text extracts cleanly with `zipfile` and a regex,
  no new dependency, and it is tempting. But how often Del Mar does this is
  unknown: the corpus is one day old and holds one example, and that example is
  a cancellation notice rather than an agenda. Counting .docx across the
  existing document store comes first. Exit 4 exists precisely so this stays a
  decision with evidence rather than a reflex.
- **Treating an unsupported type as success.** It would silence the doctor,
  and Del Mar's agendas would go on being missed with nothing recording it.
  Exit 4 is a third outcome, not a pass.
- **Inferring the kind from the URL.** `ViewFile` looks like a document and an
  AgendaCenter index looks like a listing, right up to the agency that names
  things differently. The capture site already knows; recording what it knows
  beats pattern-matching a string later.
- **Keeping one code path and making the listing parsers tolerant of
  documents.** That spreads the distinction across ten parsers instead of
  holding it in one place, and each of them would have to be right.
- **Running the pipeline itself in CI.** It needs credentials, the network and
  the `claude -p` subscription token on workhorse. The committed fixtures are
  the part that is offline by construction, which is what makes them testable.
- **Installing `requirements.txt` in CI.** It carries playwright, yt-dlp and
  boto3 for the live pipeline. None is needed to replay a committed fixture,
  and a two-second gate that takes two minutes gets skipped.
