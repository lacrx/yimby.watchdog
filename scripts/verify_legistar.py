#!/usr/bin/env python3
"""
Verify Oceanside doc-index against Legistar source and extraction outputs.

Usage:
    python scripts/verify_legistar.py              # full report: missing meetings + date mismatches
    python scripts/verify_legistar.py --backfill   # also queue missing meetings in state.json for nightly scrape
    python scripts/verify_legistar.py --year 2024  # single year only
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from oceanside import fetch_calendar_page, parse_meetings, BASE_URL
from civic_utils import agency_data_dir, normalize_date

DATA_DIR = agency_data_dir("oceanside")
STATE_FILE = DATA_DIR / "state.json"
DOC_INDEX_FILE = DATA_DIR / "doc-index.json"
STRUCTURED_DIR = REPO_ROOT / "data" / "structured"


def load_state():
    return json.loads(STATE_FILE.read_text())


def load_doc_index():
    data = json.loads(DOC_INDEX_FILE.read_text())
    return data.get("documents", data)


def fetch_all_legistar_meetings(years):
    """Fetch meeting listings from Legistar for given years."""
    all_meetings = {}
    for year in years:
        print(f"  Fetching Legistar calendar {year}...")
        try:
            html = fetch_calendar_page(year)
            meetings = parse_meetings(html)
            for m in meetings:
                if m["id"]:
                    all_meetings[m["id"]] = m
            print(f"    {len(meetings)} meetings found")
            time.sleep(1)
        except Exception as e:
            print(f"    ERROR fetching {year}: {e}")
    return all_meetings


def report_missing_meetings(legistar_meetings, state):
    """Compare Legistar meetings against state.json."""
    state_mids = set(state.get("meetings", {}).keys())
    legistar_mids = set(legistar_meetings.keys())

    missing = legistar_mids - state_mids
    extra = state_mids - legistar_mids

    by_year = defaultdict(lambda: {"legistar": 0, "state": 0, "missing": 0})
    for mid, m in legistar_meetings.items():
        date = m.get("date", "")
        year = date.split("/")[-1] if "/" in date else "unknown"
        by_year[year]["legistar"] += 1
        if mid in state_mids:
            by_year[year]["state"] += 1
        else:
            by_year[year]["missing"] += 1

    print("\n=== Meeting Coverage ===")
    print(f"{'Year':<6} {'Legistar':>9} {'Scraped':>8} {'Missing':>8}")
    print("-" * 35)
    for year in sorted(by_year.keys()):
        y = by_year[year]
        print(f"{year:<6} {y['legistar']:>9} {y['state']:>8} {y['missing']:>8}")

    print(f"\nTotal: {len(legistar_mids)} on Legistar, {len(state_mids)} in state, {len(missing)} missing, {len(extra)} extra in state")

    if missing:
        print(f"\n=== Missing Meetings ({len(missing)}) ===")
        missing_list = []
        for mid in sorted(missing):
            m = legistar_meetings[mid]
            missing_list.append(m)
            print(f"  {mid}: {m['body']} — {m['date']}")
        return missing_list

    # Check for commission gap
    legistar_bodies = {m["body"] for m in legistar_meetings.values()}
    state_bodies = {m.get("body") for m in state.get("meetings", {}).values()}
    known_commissions = state_bodies - legistar_bodies
    if known_commissions:
        print(f"\n=== Commission Gap ===")
        print(f"Bodies in state.json but NOT on Legistar calendar:")
        for b in sorted(known_commissions):
            if b:
                print(f"  - {b}")
        print("These commissions may have been removed from Legistar's public calendar.")
        print("Check if Oceanside moved them to a different system.")

    return []


def report_date_mismatches(doc_index):
    """Compare doc-index meeting_date against extraction-derived date."""
    mismatches = []
    stats = {"total": 0, "extracted": 0, "match": 0, "mismatch": 0, "no_extraction": 0}

    for filename, meta in doc_index.items():
        stats["total"] += 1
        idx_date = meta.get("meeting_date", "")
        stem = filename[:-4] if filename.endswith(".txt") else filename
        json_path = STRUCTURED_DIR / (stem + ".json")

        if not json_path.exists():
            stats["no_extraction"] += 1
            continue

        stats["extracted"] += 1
        try:
            record = json.loads(json_path.read_text())
            ext_date = record.get("date") or ""
        except (json.JSONDecodeError, Exception):
            continue

        if not ext_date or not idx_date:
            continue

        if ext_date == idx_date:
            stats["match"] += 1
        else:
            stats["mismatch"] += 1
            direction = "older" if ext_date < idx_date else "newer"
            mismatches.append({
                "file": filename,
                "index_date": idx_date,
                "extraction_date": ext_date,
                "direction": direction,
            })

    print("\n=== Date Reconciliation ===")
    print(f"Total docs:        {stats['total']}")
    print(f"With extraction:   {stats['extracted']}")
    print(f"Dates match:       {stats['match']}")
    print(f"Dates mismatch:    {stats['mismatch']}")
    print(f"No extraction yet: {stats['no_extraction']}")

    if mismatches:
        print(f"\n=== Date Mismatches ({len(mismatches)}) ===")
        print(f"{'File':<60} {'Index Date':<12} {'Extract Date':<12} {'Dir'}")
        print("-" * 95)
        for m in sorted(mismatches, key=lambda x: x["extraction_date"]):
            fname = m["file"][:58]
            print(f"{fname:<60} {m['index_date']:<12} {m['extraction_date']:<12} {m['direction']}")

    return mismatches


def report_orphan_extractions(doc_index):
    """Find extraction JSONs with no corresponding source .txt on disk."""
    doc_dir = DATA_DIR / "documents"
    orphans_by_year = defaultdict(int)
    orphan_count = 0

    for json_file in sorted(STRUCTURED_DIR.glob("*.json")):
        if json_file.name in ("extraction-state.json", "meetings-state.json", "document-dates.json"):
            continue
        try:
            record = json.loads(json_file.read_text())
            if not isinstance(record, dict) or "votes" not in record:
                continue
            agency = record.get("agency", "")
            if "oceanside" not in agency.lower():
                continue
            txt_path = doc_dir / (json_file.stem + ".txt")
            if not txt_path.exists():
                orphan_count += 1
                d = record.get("date", "")
                yr = d[:4] if d else "unknown"
                orphans_by_year[yr] += 1
        except (json.JSONDecodeError, Exception):
            continue

    print(f"\n=== Orphan Extractions (no source .txt) ===")
    print(f"Total: {orphan_count}")
    if orphans_by_year:
        print("By year:")
        for yr in sorted(orphans_by_year.keys()):
            print(f"  {yr}: {orphans_by_year[yr]}")
    print("Note: these are valid extracted records from a prior scrape. Source files")
    print("no longer on disk but extraction output is preserved in all-records.jsonl.")
    return orphan_count


def report_coverage_by_body(doc_index):
    """Show document count per body per year."""
    by_body_year = defaultdict(lambda: defaultdict(int))
    for filename, meta in doc_index.items():
        date = meta.get("meeting_date", "")
        body = meta.get("body", "unknown")
        year = date[:4] if date else "unknown"
        by_body_year[body][year] += 1

    print("\n=== Coverage by Body ===")
    years = sorted({y for b in by_body_year.values() for y in b.keys()})
    header = f"{'Body':<40} " + " ".join(f"{y:>5}" for y in years)
    print(header)
    print("-" * len(header))
    for body in sorted(by_body_year.keys()):
        row = f"{body:<40} " + " ".join(f"{by_body_year[body].get(y, 0):>5}" for y in years)
        print(row)


def backfill_state(missing_meetings, state):
    """Add missing meetings to state.json with pending marker."""
    added = 0
    for m in missing_meetings:
        mid = m["id"]
        if mid not in state.get("meetings", {}):
            state.setdefault("meetings", {})[mid] = {
                "body": m["body"],
                "date": m["date"],
                "pending": True,
                "queued_at": datetime.now().isoformat(),
            }
            added += 1

    if added:
        STATE_FILE.write_text(json.dumps(state, indent=2, default=str))
        print(f"\nBackfill: added {added} meetings to state.json with pending=true")
    else:
        print("\nBackfill: nothing to add")
    return added


def main():
    parser = argparse.ArgumentParser(description="Verify Oceanside data against Legistar")
    parser.add_argument("--year", type=int, help="Check single year only")
    parser.add_argument("--backfill", action="store_true", help="Queue missing meetings in state.json")
    parser.add_argument("--no-fetch", action="store_true", help="Skip Legistar fetch, only run date reconciliation")
    args = parser.parse_args()

    doc_index = load_doc_index()
    state = load_state()

    if not args.no_fetch:
        years = [args.year] if args.year else list(range(2020, 2027))
        print(f"Fetching Legistar calendar for years: {years}")
        legistar_meetings = fetch_all_legistar_meetings(years)

        missing = report_missing_meetings(legistar_meetings, state)

        if args.backfill and missing:
            backfill_state(missing, state)

    report_date_mismatches(doc_index)
    report_orphan_extractions(doc_index)
    report_coverage_by_body(doc_index)


if __name__ == "__main__":
    main()
