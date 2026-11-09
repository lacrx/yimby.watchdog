#!/usr/bin/env python3
"""Check municode reconcile_orphans against fabricated rows. No network.

reconcile_orphans both hides rows and leaves rows alone, and getting the second
part wrong is the expensive direction: a real coverage gap marked stale stops
being reported. So every guard gets a case that proves it still holds.

Usage: python3 scripts/check_municode_reconcile.py
Exit status is 0 only if every case passes.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scrapers.municode import reconcile_orphans  # noqa: E402

PAC = "Public Art Commission"
PC = "Planning Commission"


def build(rows, docs):
    """rows: {id: meeting dict}. docs: {id: filename}. Returns the temp root."""
    root = Path(tempfile.mkdtemp(prefix="municode-reconcile-"))
    meetings, documents = root / "meetings", root / "documents"
    meetings.mkdir()
    documents.mkdir()
    for mid, meta in rows.items():
        (meetings / mid).mkdir()
        (meetings / mid / "meeting.json").write_text(json.dumps(meta))
    for mid, name in docs.items():
        (documents / f"{mid}-{name}").write_text("x" * 64)
    return root


def read(root, mid):
    return json.loads((root / "meetings" / mid / "meeting.json").read_text())


results = []


def case(label, rows, docs, listed, expect):
    """expect: {id: (flag_name_or_None, superseded_target_or_None)}"""
    root = build(rows, docs)
    try:
        captured = []
        try:
            reconcile_orphans("escondido", root / "meetings", root / "documents",
                              listed=listed)
        except Exception as e:
            results.append((label, "FAIL", f"raised {type(e).__name__}: {e}"))
            return
        for mid, (want_flag, want_sup) in expect.items():
            got = read(root, mid)
            if want_sup is not None and got.get("superseded_by") != want_sup:
                captured.append(f"{mid}: superseded_by={got.get('superseded_by')!r},"
                                f" wanted {want_sup!r}")
            if want_flag == "stale" and not got.get("stale"):
                captured.append(f"{mid}: not marked stale")
            if want_flag == "stale" and got.get("superseded_by"):
                captured.append(f"{mid}: marked stale but also superseded_by="
                                f"{got.get('superseded_by')!r}")
            if want_flag is None and want_sup is None and (
                    got.get("stale") or got.get("superseded_by")):
                captured.append(f"{mid}: should have been left alone, got "
                                f"stale={got.get('stale')!r} "
                                f"superseded_by={got.get('superseded_by')!r}")
        results.append((label, "PASS" if not captured else "FAIL",
                        "as expected" if not captured else "; ".join(captured)))
    finally:
        shutil.rmtree(root, ignore_errors=True)


ORPHAN = {"body": PAC, "date": "2026-09-16", "id": "orph", "source": "municode",
          "documents": []}
LISTED_PAC = [{"body": PAC, "date": "2026-09-21"},
              {"body": PAC, "date": "2026-08-10"},
              {"body": PC, "date": "2026-09-22"}]

# The Escondido case this was written for: two bare rows on a date the portal
# no longer carries, five days from the meeting that did happen.
case("orphan absent from listing -> stale",
     {"orph": dict(ORPHAN), "real": {"body": PAC, "date": "2026-09-21",
                                     "url": "https://x/meeting-27", "documents": [1]}},
     {"real": "Agenda_Packet.pdf"}, LISTED_PAC, {"orph": ("stale", None)})

# Guards. Each of these would hide a real gap if it stopped holding.
case("orphan still in listing -> left alone",
     {"orph": dict(ORPHAN)}, {},
     LISTED_PAC + [{"body": PAC, "date": "2026-09-16"}], {"orph": (None, None)})

case("body missing from listing -> left alone",
     {"orph": dict(ORPHAN)}, {},
     [{"body": PC, "date": "2026-09-22"}], {"orph": (None, None)})

case("no listing passed -> left alone",
     {"orph": dict(ORPHAN)}, {}, None, {"orph": (None, None)})

case("older than listing range -> left alone",
     {"orph": {"body": PAC, "date": "2025-02-11", "documents": []}}, {},
     LISTED_PAC, {"orph": (None, None)})

case("row with a url -> left alone",
     {"orph": dict(ORPHAN, url="https://x/public-art-commission-meeting-9")}, {},
     LISTED_PAC, {"orph": (None, None)})

case("orphan holding its own docs -> left alone",
     {"orph": dict(ORPHAN)}, {"orph": "Agenda.pdf"},
     LISTED_PAC, {"orph": (None, None)})

# Superseding still wins over staling: a documented twin one day later is the
# renumbering case, and it should report as a duplicate, not as a vanished row.
case("documented twin at +1 day -> superseded, not stale",
     {"orph": {"body": PAC, "date": "2026-09-20", "documents": []},
      "real": {"body": PAC, "date": "2026-09-21", "url": "https://x/meeting-27",
               "documents": [1]}},
     {"real": "Agenda_Packet.pdf"}, LISTED_PAC, {"orph": (None, "real")})

print()
for label, verdict, detail in results:
    print(f"  {verdict:4}  {label:46}  {detail}")
failures = [r for r in results if r[1] != "PASS"]
print()
print(f"{len(results) - len(failures)}/{len(results)} passed")
sys.exit(1 if failures else 0)
