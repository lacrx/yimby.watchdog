#!/usr/bin/env python3
"""One-off backfill for meetings lost to the Swagit same-day meeting ID collision.

Before the fix in scrapers/swagit.py, meeting IDs were {prefix}-{body}-{YYYYMMDD}
with no time component, and _body_from_title() strips a trailing "Special
Meeting". A body that met twice in one day — Carlsbad routinely holds a short
closed-session special before the regular meeting — therefore minted one ID for
both, so the second listing row was either skipped as already-seen or clobbered
the first row's agenda PDF. Either way one real meeting vanished with no error.

The fixed scraper only looks back as far as --years, so it repairs recent dates
and leaves older collisions buried. This walks the full listing history for a
Swagit agency, keeps only rows that share a body and date with another row, and
fetches the ones still missing or misfiled.

Idempotent: a meeting already recorded with the right video and a non-empty
agenda on disk is left alone.

    python scripts/backfill_swagit_same_day.py --agency carlsbad [--years 20]
    python scripts/backfill_swagit_same_day.py --agency carlsbad --dry-run
"""

import argparse
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import (  # noqa: E402
    agency_data_dir, extract_text, load_json, log_discovery,
    meeting_needs_recheck, rebuild_doc_index, save_json,
)
from scrapers.swagit import (  # noqa: E402
    _assign_meeting_ids, _body_from_title, _supersede_stale_docs,
    download_agenda_pdf, get_agency_config, paginate_body,
)


def gather_rows(cfg, cutoff):
    """Return every listing row for the agency, keyed by body, deduped by video."""
    base_url = cfg["base_url"]
    view_id = cfg.get("swagit_view_id", "295")
    bodies = cfg.get("swagit_bodies", {})
    archives = cfg.get("swagit_archive_slugs", {})

    rows = []
    seen = set()
    for body_name, body_slug in bodies.items():
        for slug in [body_slug] + list(archives.get(body_name, [])):
            try:
                for m in paginate_body(base_url, view_id, slug, cutoff):
                    if m["video_id"] in seen:
                        continue
                    seen.add(m["video_id"])
                    m["effective_body"] = _body_from_title(m["title"]) or body_name
                    rows.append(m)
            except Exception as e:
                print(f"  ERROR: listing {slug} failed: {e}")
                return None
        print(f"  {body_name}: {len(rows)} rows so far")
    return rows


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--agency", required=True)
    ap.add_argument("--years", type=float, default=20)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = get_agency_config(args.agency)
    if cfg.get("platform") != "swagit":
        print(f"ERROR: {args.agency} is not a swagit agency")
        return 3

    prefix = cfg.get("swagit_prefix", args.agency[:2])
    base_url = cfg["base_url"]
    data_dir = agency_data_dir(args.agency)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    for d in (docs_dir, meetings_dir):
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(data_dir / "state.json") or {"last_fetch": None, "meetings": {}}
    cutoff = datetime.now() - timedelta(days=365 * args.years)

    print(f"Walking {cfg.get('name', args.agency)} listings back to {cutoff:%Y-%m-%d}...")
    rows = gather_rows(cfg, cutoff)
    if rows is None:
        print("ERROR: aborting, listings incomplete")
        return 3
    if not rows:
        print(f"ERROR: {args.agency}: listing parsed 0 meetings")
        return 3

    # Keep only rows that collided with another meeting of the same body that day.
    contested = [(m, mid, sib) for m, mid, sib in _assign_meeting_ids(prefix, rows) if sib]
    print(f"\n{len(rows)} listing rows, {len(contested)} in a same-day group")

    todo = []
    for m, mid, sib in contested:
        existing = load_json(meetings_dir / mid / "meeting.json") or {}
        in_state = mid in state["meetings"]
        right_video = existing.get("video_id") == m["video_id"]
        has_docs = not meeting_needs_recheck(mid, docs_dir, m["date"].strftime("%m/%d/%Y"))
        if in_state and right_video and has_docs:
            continue
        todo.append((m, mid, sib, existing))

    print(f"{len(todo)} need fetching or refiling\n")
    if args.dry_run:
        for m, mid, _, existing in todo:
            why = ("new" if not existing
                   else f"was video {existing.get('video_id')}" if existing.get("video_id") != m["video_id"]
                   else "no agenda on disk")
            print(f"  {m['date']:%Y-%m-%d}  {m['duration']:>8}  {mid}  ({why})")
        return 0

    new_count = doc_count = repaired = failed = 0
    for m, mid, sib, existing in todo:
        dt = m["date"]
        body = m["effective_body"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meta = {
            "body": body,
            "date": dt.strftime("%m/%d/%Y"),
            "title": f"{body} — {dt.strftime('%b %d, %Y')}",
            "id": mid,
            "source": "swagit",
            "agency": args.agency,
            "video_id": m["video_id"],
            "video_url": f"{base_url}/videos/{m['video_id']}",
            "duration": m["duration"],
            "same_day_meetings": sib,
            "same_day_role": "primary" if m.get("_same_day_primary") else "additional",
            "backfilled": "swagit same-day ID collision",
        }

        if not existing:
            save_json(mdir / "meeting.json", meta)
            new_count += 1
            print(f"  NEW:    {dt:%Y-%m-%d} {m['duration']:>8}  {mid}")
        elif existing.get("video_id") != m["video_id"]:
            moved = _supersede_stale_docs(docs_dir, mid)
            save_json(mdir / "meeting.json", meta)
            state["meetings"].pop(mid, None)
            repaired += 1
            print(f"  REPAIR: {dt:%Y-%m-%d} {m['duration']:>8}  {mid} "
                  f"(was video {existing.get('video_id')}, {moved} stale agenda files superseded)")
        else:
            save_json(mdir / "meeting.json", {**existing, **meta})

        pdf_path = docs_dir / f"{mid}-agenda.pdf"
        if download_agenda_pdf(base_url, m["video_id"], pdf_path):
            if extract_text(pdf_path):
                doc_count += 1
            time.sleep(0.3)
        else:
            failed += 1
            print(f"          no agenda PDF available for video {m['video_id']}")

        state["meetings"][mid] = {
            "fetched": datetime.now().isoformat(),
            "body": body,
            "video_id": m["video_id"],
            "date": dt.strftime("%m/%d/%Y"),
        }

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(args.agency, state, docs_dir)
    save_json(data_dir / "state.json", state)
    log_discovery(args.agency, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {repaired} refiled, "
          f"{doc_count} agendas extracted, {failed} with no agenda posted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
