#!/usr/bin/env python3
"""Check how extract_structured classifies a safeguard block. No network, no CLI.

The 2026-11-13 run died on:
    API Error: Opus 5's safeguards flagged this message
    (https://www.anthropic.com/legal/aup). ...
on stdout with exit code 1. Read as a generic exit_code failure it was retried
twice per chunk, for every chunk of every document, until the circuit breaker
stopped the run with 632 documents left. These cases pin the new behaviour:
classified, never retried, and still distinct from a refusal or a limit.

Usage: python3 scripts/check_safeguard_block.py
Exit status is 0 only if every case passes.
"""
import json
import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import transforms.extract_structured as ex  # noqa: E402

AUP = ("API Error: Opus 5's safeguards flagged this message "
       "(https://www.anthropic.com/legal/aup). This sometimes happens with "
       "safe, normal conversations.")

CALLS = []


class FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def fake_run(responses):
    """Replace subprocess.run with a canned sequence; records every call."""
    seq = list(responses)

    def run(cmd, **kwargs):
        CALLS.append(kwargs.get("input", ""))
        return seq.pop(0) if len(seq) > 1 else seq[0]
    ex.subprocess = types.SimpleNamespace(
        run=run, TimeoutExpired=TimeoutError,
        CalledProcessError=Exception)
    ex.time = types.SimpleNamespace(sleep=lambda *_: None)
    CALLS.clear()


RECORD = json.dumps({"meeting_id": "m1", "date": "2026-11-04", "body": "City Council",
                     "agency": "x", "doc_type": "agenda", "votes": [],
                     "housing_items": [], "fiscal_items": [], "legal_flags": [],
                     "council_positions": [], "public_comments": [],
                     "key_quotes": [], "procedural_only": True})

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


@case("AUP block on stdout with rc 1 is classified safeguard_flagged")
def _():
    fake_run([FakeCompleted(1, stdout=AUP)])
    rec, err = ex._call_claude("prompt")
    return rec is None and err == "safeguard_flagged"


@case("a safeguard block is not retried (one CLI call, not three)")
def _():
    fake_run([FakeCompleted(1, stdout=AUP)])
    ex._call_claude("prompt")
    return len(CALLS) == 1


@case("a plain exit-code failure is still retried")
def _():
    fake_run([FakeCompleted(1, stderr="boom")])
    rec, err = ex._call_claude("prompt")
    return rec is None and err == "exit_code" and len(CALLS) == 3


@case("a per-document refusal is still content_refused, not safeguard_flagged")
def _():
    fake_run([FakeCompleted(1, stdout="I can't help with this.")])
    rec, err = ex._call_claude("prompt")
    return err == "content_refused"


@case("a session limit still raises RateLimitHit")
def _():
    fake_run([FakeCompleted(1, stdout="You've hit your session limit · resets 6am")])
    try:
        ex._call_claude("prompt")
    except ex.RateLimitHit:
        return True
    return False


@case("the CLI's 5-hour limit wording raises RateLimitHit, not exit_code")
def _():
    fake_run([FakeCompleted(1, stdout="You've hit your 5-hour limit \u00b7 resets 5am (America/Los_Angeles)")])
    try:
        ex._call_claude("prompt")
    except ex.RateLimitHit as e:
        return "resets 5am" in str(e) and len(CALLS) == 1
    return False


@case("a valid record that quotes the AUP text is not flagged")
def _():
    body = json.loads(RECORD)
    body["key_quotes"] = ["see https://www.anthropic.com/legal/aup"]
    fake_run([FakeCompleted(0, stdout=json.dumps(body))])
    rec, err = ex._call_claude("prompt")
    return err is None and rec is not None


@case("chunked extraction stops at the first blocked chunk instead of burning the rest")
def _():
    # 3 chunks' worth of text; first call succeeds, every later call is blocked.
    fake_run([FakeCompleted(0, stdout=RECORD),
              FakeCompleted(1, stdout=AUP)])
    text = ("line of document text\n" * 200 + "\n") * 40
    assert len(text) > 2 * ex.CHUNK_SIZE
    rec, err = ex.extract_structured(text, None)
    # one good chunk + one blocked chunk, then stop: not one call per chunk
    return err == "safeguard_flagged" and rec is not None and rec["_partial"] and len(CALLS) == 2


@case("probe_claude calls the CLI once and reports a healthy session")
def _():
    fake_run([FakeCompleted(0, stdout="OK\n")])
    ok, detail = ex.probe_claude()
    return ok and detail == "OK" and len(CALLS) == 1


@case("a probe that is itself flagged reports a session-wide block")
def _():
    fake_run([FakeCompleted(1, stdout=AUP)])
    ok, detail = ex.probe_claude()
    return ok is False and "safeguard-flagged" in detail


@case("a probe that hits the 5-hour limit does not read as healthy")
def _():
    fake_run([FakeCompleted(1, stdout="You've hit your 5-hour limit · resets 5am")])
    ok, detail = ex.probe_claude()
    return ok is False and "limit" in detail


@case("a probe with rc 0 and no output is not a healthy session")
def _():
    fake_run([FakeCompleted(0, stdout="   \n")])
    ok, detail = ex.probe_claude()
    return ok is False and "empty" in detail


@case("a probe never raises into the run")
def _():
    def boom(cmd, **kwargs):
        raise OSError("fork failed")
    ex.subprocess = types.SimpleNamespace(
        run=boom, TimeoutExpired=TimeoutError, CalledProcessError=Exception)
    ok, detail = ex.probe_claude()
    return ok is False and "OSError" in detail


DOC_TEXT = ("CITY COUNCIL AGENDA\nItem 1. Resolution approving a density bonus "
            "for 120 residential units, parking reduction requested.\n" * 40)


def fake_extract_run(probe_ok):
    """Flag every document; answer the probe according to probe_ok.

    The probe is told apart by its prompt, which is the only thing that
    distinguishes it from an extraction call at the subprocess boundary.
    """
    def run(cmd, **kwargs):
        prompt = kwargs.get("input", "")
        CALLS.append(prompt)
        if prompt.startswith("Reply with the two characters OK"):
            return FakeCompleted(0, stdout="OK\n") if probe_ok else FakeCompleted(1, stdout=AUP)
        return FakeCompleted(1, stdout=AUP)
    ex.subprocess = types.SimpleNamespace(
        run=run, TimeoutExpired=TimeoutError, CalledProcessError=Exception)
    ex.time = types.SimpleNamespace(sleep=lambda *_: None)
    CALLS.clear()


def drive_extract(tmp, probe_ok, n_docs=6):
    """Run cmd_extract over n_docs throwaway documents, every one flagged.

    Returns (docs_attempted, probe_calls). Touches no real data: sources, the
    structured dir and the failure state all live under tmp.
    """
    docs = tmp / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    out = tmp / "structured"
    out.mkdir(parents=True, exist_ok=True)
    sources = []
    for k in range(n_docs):
        p = docs / f"doc{k:02d}.txt"
        p.write_text(DOC_TEXT)
        sources.append(("document", p))

    ex.STRUCTURED_DIR = out
    ex.FAILURE_STATE_FILE = out / "extraction-failures.json"
    ex.collect_all_sources = lambda **kw: list(sources)
    ex.presplit_oversized = lambda *a, **kw: 0
    ex.has_skip_marker = lambda *a, **kw: False
    ex.rebuild_combined = lambda *a, **kw: None
    ex.predict_relevance = lambda *a, **kw: 1.0

    fake_extract_run(probe_ok)
    args = types.SimpleNamespace(
        force=True, dry_run=False, queue="cold", hot_days=14, stop_at=None,
        meeting=None, agency=None, no_triage=True, limit=None,
        skip_forward_backlog=False, tally=False, tally_all=False)
    try:
        ex.cmd_extract(args)
    except SystemExit:
        pass
    probes = [c for c in CALLS if c.startswith("Reply with the two characters OK")]
    # One call per document: a safeguard block is never retried, so the count of
    # non-probe calls is the count of documents the run reached.
    attempted = len(CALLS) - len(probes)
    return attempted, len(probes)


@case("a healthy probe keeps the run going past three flagged documents")
def _():
    # This is the 2026-11-13..16 failure: four cold runs aborted at document 10
    # of 647 while `claude -p` answered normally, so a 609-document backlog sat
    # untouched. The run must now walk the whole queue.
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        attempted, probes = drive_extract(Path(td), probe_ok=True, n_docs=6)
    return attempted == 6 and probes >= 1


@case("a flagged probe still stops the run at three documents")
def _():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        attempted, probes = drive_extract(Path(td), probe_ok=False, n_docs=6)
    return attempted == 3 and probes == 1


@case("record_failure(count=False) logs the error without advancing auto-skip")
def _():
    state = {}
    ex.record_failure(state, "doc.txt", "safeguard_flagged", count=False)
    ex.record_failure(state, "doc.txt", "safeguard_flagged", count=False)
    return state["doc.txt"]["fail_count"] == 0 and \
        state["doc.txt"]["last_error"] == "safeguard_flagged"


def main():
    import subprocess as real_subprocess
    import time as real_time
    failures = 0
    for name, fn in CASES:
        try:
            ok = fn()
        except Exception as e:  # noqa: BLE001
            ok, name = False, f"{name} — raised {type(e).__name__}: {e}"
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        failures += not ok
    ex.subprocess, ex.time = real_subprocess, real_time
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
