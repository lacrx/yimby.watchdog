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


PROBE_PREFIX = "Reply with the two characters OK"
BIG_TEXT = ("line of document text\n" * 200 + "\n") * 40


def scripted_run(outcomes, probe_ok=None):
    """Replace subprocess.run; answer extraction calls from `outcomes` in order.

    `outcomes` is a list of FakeCompleted, the last one repeating. A probe is
    told apart by its prompt and answered from `probe_ok`, so a document's
    chunks and the probes between them can be scripted independently. That
    separation is the whole point: before the probe existed, a test could not
    say "this chunk is blocked but the session is fine".
    """
    seq = list(outcomes)
    state = {"i": 0}

    def run(cmd, **kwargs):
        prompt = kwargs.get("input", "")
        CALLS.append(prompt)
        if prompt.startswith(PROBE_PREFIX):
            if probe_ok is None:
                raise AssertionError("probe called but no probe outcome scripted")
            return FakeCompleted(0, stdout="OK\n") if probe_ok else FakeCompleted(1, stdout=AUP)
        out = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return out

    ex.subprocess = types.SimpleNamespace(
        run=run, TimeoutExpired=TimeoutError, CalledProcessError=Exception)
    ex.time = types.SimpleNamespace(sleep=lambda *_: None)
    CALLS.clear()


def chunk_calls():
    return [c for c in CALLS if not c.startswith(PROBE_PREFIX)]


def probe_calls():
    return [c for c in CALLS if c.startswith(PROBE_PREFIX)]


@case("a blocked chunk stops the document when the probe says session-wide")
def _():
    # The pre-2026-11-17 behaviour, now conditional on evidence: one good chunk,
    # one blocked chunk, one probe that is itself blocked, then stop. Still not
    # one call per chunk.
    assert len(BIG_TEXT) > 2 * ex.CHUNK_SIZE
    scripted_run([FakeCompleted(0, stdout=RECORD), FakeCompleted(1, stdout=AUP)],
                 probe_ok=False)
    rec, err = ex.extract_structured(BIG_TEXT, None)
    return (err == "safeguard_flagged" and rec is not None and rec["_partial"]
            and len(chunk_calls()) == 2 and len(probe_calls()) == 1)


@case("a healthy probe retires the blocked chunk and reads the rest of the document")
def _():
    # part05 of cb-city-council-20210309-agenda sat at 10 of 33 chunks from
    # 2026-11-13 to 11-17 because chunk 0 was flagged and stopped the document
    # every night. One flagged chunk must cost one chunk, not the document.
    chunks = ex._split_chunks(BIG_TEXT)
    assert len(chunks) >= 3
    scripted_run([FakeCompleted(1, stdout=AUP), FakeCompleted(0, stdout=RECORD)],
                 probe_ok=True)
    rec, err = ex.extract_structured(BIG_TEXT, None)
    return (err is None and rec is not None and not rec.get("_partial")
            and rec.get("_retired_chunks") == 1
            and len(chunk_calls()) == len(chunks)
            and len(probe_calls()) == 1)


@case("a retired chunk is not re-run on the next pass")
def _():
    import tempfile
    chunks = ex._split_chunks(BIG_TEXT)
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "doc.chunks"
        scripted_run([FakeCompleted(1, stdout=AUP), FakeCompleted(0, stdout=RECORD)],
                     probe_ok=True)
        ex.extract_structured(BIG_TEXT, None, cache_path=cache)
        if not cache.exists():
            return False  # deleting the sidecar would forget the retirement
        # Second pass: every chunk is cached, the retired one must not be asked
        # again, and no probe is needed because nothing flags.
        scripted_run([FakeCompleted(0, stdout=RECORD)])
        rec, err = ex.extract_structured(BIG_TEXT, None, cache_path=cache)
        return (err is None and rec is not None and len(chunk_calls()) == 0
                and len(probe_calls()) == 0 and rec.get("_retired_chunks") == 1)


@case("a chunk that has failed three runs is retired instead of re-run for ever")
def _():
    import tempfile
    chunks = ex._split_chunks(BIG_TEXT)
    fp = ex._chunk_fingerprint(BIG_TEXT, chunks)
    # The real cache this was built for: part05 held 22 chunks marked exit_code
    # from the 2026-11-13 rate limit that was misread as a chunk failure. Each
    # one costs three CLI calls inside _call_claude every time it is retried.
    results = {0: {"record": json.loads(RECORD)}}
    for ci in range(1, len(chunks)):
        results[ci] = {"error": "exit_code", "attempts": 3}
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "doc.chunks"
        ex._save_chunk_cache(cache, fp, results)
        scripted_run([FakeCompleted(0, stdout=RECORD)])
        rec, err = ex.extract_structured(BIG_TEXT, None, cache_path=cache)
    return (err is None and rec is not None and len(chunk_calls()) == 0
            and rec.get("_retired_chunks") == len(chunks) - 1)


@case("a cache with no attempt count gets a fresh budget, not instant retirement")
def _():
    # Sidecars written before attempt counting have no "attempts" key. Reading
    # a missing count as "already exhausted" would drop good chunks on sight.
    import tempfile
    chunks = ex._split_chunks(BIG_TEXT)
    fp = ex._chunk_fingerprint(BIG_TEXT, chunks)
    results = {ci: {"error": "exit_code"} for ci in range(len(chunks))}
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "doc.chunks"
        ex._save_chunk_cache(cache, fp, results)
        scripted_run([FakeCompleted(0, stdout=RECORD)])
        rec, err = ex.extract_structured(BIG_TEXT, None, cache_path=cache)
    return (err is None and rec is not None and not rec.get("_retired_chunks")
            and len(chunk_calls()) == len(chunks))


@case("a document whose every chunk is retired reports chunks_exhausted")
def _():
    import tempfile
    chunks = ex._split_chunks(BIG_TEXT)
    fp = ex._chunk_fingerprint(BIG_TEXT, chunks)
    results = {ci: {"error": "exit_code", "attempts": 3} for ci in range(len(chunks))}
    with tempfile.TemporaryDirectory() as td:
        cache = Path(td) / "doc.chunks"
        ex._save_chunk_cache(cache, fp, results)
        scripted_run([FakeCompleted(0, stdout=RECORD)])
        rec, err = ex.extract_structured(BIG_TEXT, None, cache_path=cache)
    return rec is None and err == "chunks_exhausted" and len(chunk_calls()) == 0


@case("chunk probes are bounded, so a session-wide block cannot be probed per chunk")
def _():
    # Every chunk flags and every probe passes: a liar of a probe must not buy
    # one call per chunk on a long document.
    scripted_run([FakeCompleted(1, stdout=AUP)], probe_ok=True)
    rec, err = ex.extract_structured(BIG_TEXT, None)
    return len(probe_calls()) == ex.MAX_CHUNK_PROBES


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
