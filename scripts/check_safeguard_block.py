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
