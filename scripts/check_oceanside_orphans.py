#!/usr/bin/env python3
"""Check oceanside.mark_stale_orphans against fabricated rows. No network.

Marking a row stale hides it from the doctor's coverage check, so the expensive
direction is marking a real gap. Most cases here assert a row is left alone.

Usage: python3 scripts/check_oceanside_orphans.py
Exit status is 0 only if every case passes.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scrapers import oceanside  # noqa: E402

CC = "City Council"
PC = "Planning Commission"


def build(rows, docs=()):
    """rows: {id: meeting dict}. docs: filenames. Returns the temp root."""
    root = Path(tempfile.mkdtemp(prefix="oceanside-orphans-"))
    meetings, documents = root / "meetings", root / "documents"
    meetings.mkdir()
    documents.mkdir()
    for mid, meta in rows.items():
        (meetings / mid).mkdir()
        (meetings / mid / "meeting.json").write_text(json.dumps(meta))
    for name in docs:
        (documents / name).write_text("x")
    oceanside.MEETINGS_DIR = meetings
    oceanside.DOCS_DIR = documents
    return root


def row(body, date, **extra):
    m = {"body": body, "date": date, "time": "3:30 PM", "detail_url": None,
         "agenda_url": None, "minutes_url": None}
    m.update(extra)
    return m


def listed(*pairs):
    return [{"body": b, "date": d} for b, d in pairs]


CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn
    return deco


def is_stale(root, mid):
    meta = json.loads((root / "meetings" / mid / "meeting.json").read_text())
    return bool(meta.get("stale"))


@case("bare row absent from the calendar is marked stale")
def _():
    root = build({"ghost": row(CC, "11/5/2026")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return is_stale(root, "ghost")


@case("a listed row is left alone")
def _():
    root = build({"real": row(CC, "11/4/2026")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return not is_stale(root, "real")


@case("a row whose body never appeared in the listing is left alone")
def _():
    root = build({"pc": row(PC, "11/5/2026")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return not is_stale(root, "pc")


@case("a row older than the listing's range is left alone")
def _():
    root = build({"old": row(CC, "3/4/2025")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return not is_stale(root, "old")


@case("a row with documents on disk is left alone")
def _():
    root = build({"docd": row(CC, "11/5/2026")}, docs=["docd-agenda.txt"])
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return not is_stale(root, "docd")


@case("a row holding a detail link is left alone (a later run can still reach it)")
def _():
    root = build({"linked": row(CC, "11/5/2026",
                                detail_url="https://oceanside.legistar.com/x")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return not is_stale(root, "linked")


@case("an empty listing (failed parse) condemns nothing")
def _():
    root = build({"ghost": row(CC, "11/5/2026")})
    oceanside.mark_stale_orphans([])
    return not is_stale(root, "ghost")


@case("a superseded row is left as a duplicate, not restated as stale")
def _():
    root = build({"dup": row(CC, "11/5/2026", superseded_by="1355287")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return not is_stale(root, "dup")


@case("idempotent: a second run marks nothing new")
def _():
    build({"ghost": row(CC, "11/5/2026")})
    oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026")))
    return oceanside.mark_stale_orphans(listed((CC, "11/4/2026"), (CC, "10/21/2026"))) == 0


def main():
    failures = 0
    for name, fn in CASES:
        try:
            ok = fn()
        except Exception as e:  # noqa: BLE001
            ok, name = False, f"{name} — raised {type(e).__name__}: {e}"
        print(f"{'PASS' if ok else 'FAIL'}  {name}")
        failures += not ok
    for d in Path(tempfile.gettempdir()).glob("oceanside-orphans-*"):
        shutil.rmtree(d, ignore_errors=True)
    print(f"\n{len(CASES) - failures}/{len(CASES)} passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
