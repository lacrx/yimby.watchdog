#!/usr/bin/env python3
"""The committed fixture corpus and its regression check.

data/pipeline/failures/ is scratch: whatever last night's run happened to hit,
gitignored with the rest of data/. This promotes a fixture out of that scratch
and into fixtures/, which IS committed, with the number of items the parser
gets from it recorded alongside.

That recorded number is the whole point. A replay of a single fixture only
proves the parser returned something. A corpus with expected counts proves the
parser still returns what it returned before, which is the difference between
"the agent changed the code and the page parsed" and "the agent changed the
code and did not break the other twenty".

    # after a real failure, promote it (expected count = what it parses now)
    python3 scripts/fixtures.py add data/pipeline/failures/<run>/vista-1.json

    # once the parser is fixed, re-record the expectation
    python3 scripts/fixtures.py add fixtures/granicus/vista.json --expect 41

    # seed a known-good page by hand (no URL guessing: give it the URL)
    python3 scripts/fixtures.py capture --agency vista \
        --url "https://vista.granicus.com/ViewPublisherRSS.php?view_id=2&mode=agendas"

    # the regression check: every fixture parses its expected count
    python3 scripts/fixtures.py check

check exits 1 if any fixture parses a different count than recorded, which
makes it usable as a gate, by a person or by a loop.
"""

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts/ is not a package

import civic_utils  # noqa: E402
from replay_failure import PARSERS, fixture_body, platform_for  # noqa: E402

CORPUS = REPO_ROOT / "fixtures"
# Bodies are committed, so the corpus is part of every clone and every
# worktree. Keep it readable rather than exhaustive.
MAX_BODY_CHARS = 400_000


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:60]


def _redaction_ok(fixture):
    """Refuse to commit anything still carrying a credential.

    Capture redacts already. This is the second check, at the moment the bytes
    stop being gitignored scratch and start being a committed file.
    """
    for field in ("request_headers", "response_headers"):
        for key, value in (fixture.get(field) or {}).items():
            if str(key).lower() in civic_utils.REDACT_HEADERS and value != "[redacted]":
                return False, f"{field}.{key} is not redacted"
    return True, ""


def _parse_count(fixture):
    """(count, error). count is None when the platform has no replay parser."""
    platform = platform_for(fixture)
    fn = PARSERS.get(platform)
    if fn is None:
        return None, f"no parser for platform {platform!r}"
    try:
        items = fn(fixture_body(fixture), fixture.get("url") or "")
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"
    return len(list(items or [])), ""


def cmd_add(args):
    path = Path(args.fixture)
    try:
        fixture = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        print(f"ERROR: cannot read {path}: {exc}")
        return 2

    ok, why = _redaction_ok(fixture)
    if not ok:
        print(f"ERROR: refusing to commit {path}: {why}")
        return 2

    body = fixture_body(fixture)
    if len(body) > MAX_BODY_CHARS and not args.force:
        print(f"ERROR: body is {len(body)} chars, over the {MAX_BODY_CHARS} "
              f"corpus limit. Use --force if this one is worth the bytes.")
        return 2

    platform = platform_for(fixture)
    if not platform:
        print("ERROR: fixture has no platform and its agency is not in agencies.yaml")
        return 2

    count, err = _parse_count(fixture)
    expect = args.expect if args.expect is not None else count
    if expect is None:
        print(f"ERROR: cannot determine an expected count ({err}). "
              f"Pass --expect explicitly to add it anyway.")
        return 2

    fixture["expect_items"] = expect
    fixture["note"] = args.note or fixture.get("note", "")
    name = args.name or _slugify(fixture.get("agency") or path.stem)
    dest = CORPUS / platform / f"{name}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(fixture, indent=2, default=str))

    observed = "unparseable" if count is None else count
    print(f"added {dest.relative_to(REPO_ROOT)}")
    print(f"  platform {platform}, parses {observed}, expect {expect}")
    if count is not None and count != expect:
        print(f"  NOTE: recorded expectation differs from what it parses now. "
              f"check will fail until the parser is fixed, which is the point.")
    return 0


def cmd_capture(args):
    import requests
    try:
        resp = requests.get(args.url, timeout=60,
                            headers={"User-Agent": civic_utils.BROWSER_UA})
    except Exception as exc:
        print(f"ERROR: fetch failed: {type(exc).__name__}: {exc}")
        return 2

    fixture = {
        "ts": civic_utils.datetime.now().isoformat(timespec="seconds"),
        "run_id": "corpus-capture",
        "agency": args.agency,
        "platform": args.platform,
        "url": args.url,
        "method": "GET",
        "status": resp.status_code,
        "request_headers": {"User-Agent": civic_utils.BROWSER_UA},
        "response_headers": civic_utils._redact_headers(resp.headers),
        "body": resp.text,
        "body_encoding": "utf-8",
        "body_bytes": len(resp.content),
        "body_truncated": False,
    }
    tmp = civic_utils.FAILURES_DIR / "corpus-capture"
    tmp.mkdir(parents=True, exist_ok=True)
    staged = tmp / f"{_slugify(args.agency)}.json"
    staged.write_text(json.dumps(fixture, indent=2, default=str))
    print(f"fetched HTTP {resp.status_code}, {len(resp.text)} chars")

    args.fixture = str(staged)
    return cmd_add(args)


def cmd_check(args):
    if not CORPUS.exists():
        print("fixtures/ does not exist yet. Nothing to check.")
        return 0

    paths = sorted(CORPUS.rglob("*.json"))
    if args.platform:
        paths = [p for p in paths if p.parent.name == args.platform]
    if not paths:
        print("no fixtures matched")
        return 0

    failures = 0
    for path in paths:
        rel = path.relative_to(REPO_ROOT)
        try:
            fixture = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            print(f"FAIL {rel}: unreadable ({exc})")
            failures += 1
            continue
        expect = fixture.get("expect_items")
        count, err = _parse_count(fixture)
        if count is None:
            print(f"FAIL {rel}: {err}")
            failures += 1
        elif expect is None:
            print(f"FAIL {rel}: no expect_items recorded")
            failures += 1
        elif count != expect:
            print(f"FAIL {rel}: parsed {count}, expected {expect}")
            failures += 1
        elif args.verbose:
            print(f"ok   {rel}: {count}")

    total = len(paths)
    if failures:
        print(f"\n{failures} of {total} fixtures failed")
        return 1
    print(f"{total} fixtures ok")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="promote a fixture into the committed corpus")
    add.add_argument("fixture")
    add.add_argument("--expect", type=int,
                     help="expected item count (default: what it parses now)")
    add.add_argument("--name", help="corpus filename stem (default: the agency slug)")
    add.add_argument("--note", help="why this fixture is worth keeping")
    add.add_argument("--force", action="store_true", help="accept an oversized body")

    cap = sub.add_parser("capture", help="fetch a URL now and add it to the corpus")
    cap.add_argument("--agency", required=True)
    cap.add_argument("--url", required=True)
    cap.add_argument("--platform", help="override the agency's platform")
    cap.add_argument("--expect", type=int)
    cap.add_argument("--name")
    cap.add_argument("--note")
    cap.add_argument("--force", action="store_true")

    chk = sub.add_parser("check", help="replay every fixture and compare counts")
    chk.add_argument("--platform")
    chk.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "add":
        return cmd_add(args)
    if args.cmd == "capture":
        return cmd_capture(args)
    if args.cmd == "check":
        return cmd_check(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
