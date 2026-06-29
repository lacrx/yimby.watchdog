#!/usr/bin/env python3
"""
Merge per-document JSONL records into consolidated per-meeting records.

A single meeting may have sources arriving across multiple days:
  - Agenda (posted before meeting)
  - Minutes (posted days/weeks after)
  - Staff reports (posted with agenda)
  - Transcript (video posted days after, transcribed on next cron run)

This script merges all JSONL records sharing a meeting_id into one
consolidated record per meeting, using majority-vote for date and agency
selection. Tracks source counts to detect when new sources arrive.

Usage:
    python meeting_merge.py                # merge new/changed meetings
    python meeting_merge.py --force        # rebuild all merged records
    python meeting_merge.py --check        # list meetings with new sources since last merge
    python meeting_merge.py --stats        # show merge stats

Input:  data/structured/*.json (per-document records)
Output: data/structured/meetings/{meeting_id}.json (one per meeting)
        data/structured/meetings-combined.jsonl (all meetings, one per line)
        data/structured/meetings-state.json (source tracking)
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
MERGED_JSONL = STRUCTURED_DIR / "meetings-combined.jsonl"
STATE_FILE = STRUCTURED_DIR / "meetings-state.json"


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def collect_records_by_meeting():
    """Group all per-document JSONL records by meeting_id."""
    by_meeting = defaultdict(list)

    for jf in sorted(STRUCTURED_DIR.glob("*.json")):
        if jf.name in ("extraction-state.json", "meetings-state.json"):
            continue
        try:
            record = json.loads(jf.read_text())
            mid = record.get("meeting_id", "")
            if mid:
                record["_file"] = jf.name
                by_meeting[mid].append(record)
        except (json.JSONDecodeError, Exception):
            continue

    return dict(by_meeting)


def merge_records(meeting_id, records):
    """Merge multiple document records into one consolidated meeting record."""
    all_votes = []
    all_housing = []
    all_fiscal = []
    all_legal = []
    all_positions = []
    all_comments = []
    all_quotes = []
    bodies = set()
    agencies = []
    doc_types = set()
    sources = []
    dates = []
    scores = []

    for r in records:
        if r.get("procedural_only") and len(records) > 1:
            continue

        if r.get("body"):
            bodies.add(r["body"])
        if r.get("agency"):
            agencies.append(r["agency"])
        if r.get("date"):
            dates.append(r["date"])
        if r.get("doc_type"):
            doc_types.add(r["doc_type"])
        if r.get("advocacy_score"):
            scores.append(r["advocacy_score"])
        sources.append({
            "file": r.get("_file", r.get("_source", "")),
            "type": r.get("_source_type", r.get("doc_type", "")),
            "advocacy_score": r.get("advocacy_score"),
        })

        all_votes.extend(r.get("votes", []))
        all_housing.extend(r.get("housing_items", []))
        all_fiscal.extend(r.get("fiscal_items", []))
        all_legal.extend(r.get("legal_flags", []))
        all_positions.extend(r.get("council_positions", []))
        all_comments.extend(r.get("public_comments", []))
        all_quotes.extend(r.get("key_quotes", []))

    # Deduplicate strings
    all_legal = list(dict.fromkeys(all_legal))
    all_comments = list(dict.fromkeys(all_comments))
    all_quotes = list(dict.fromkeys(all_quotes))

    # Deduplicate votes by item description
    seen_votes = set()
    deduped_votes = []
    for v in all_votes:
        key = v.get("item", "").lower().strip()[:80]
        if key and key not in seen_votes:
            seen_votes.add(key)
            deduped_votes.append(v)
        elif not key:
            deduped_votes.append(v)

    # Pick most common date and agency (majority vote, not alphabetical/earliest)
    from collections import Counter
    date = Counter(dates).most_common(1)[0][0] if dates else ""
    agency = Counter(agencies).most_common(1)[0][0] if agencies else "Unknown"

    # Aggregate score: worst score wins (red > yellow > neutral > green)
    score_priority = {"red": 0, "yellow": 1, "neutral": 2, "green": 3}
    worst_score = min(scores, key=lambda s: score_priority.get(s, 99)) if scores else "neutral"

    return {
        "meeting_id": meeting_id,
        "date": date,
        "body": sorted(bodies)[0] if bodies else "Unknown",
        "agency": agency,
        "doc_types": sorted(doc_types),
        "source_count": len(records),
        "sources": sources,
        "advocacy_score": worst_score,
        "votes": deduped_votes,
        "housing_items": all_housing,
        "fiscal_items": all_fiscal,
        "legal_flags": all_legal,
        "council_positions": all_positions,
        "public_comments": all_comments,
        "key_quotes": all_quotes,
        "procedural_only": len(all_votes) == 0 and len(all_housing) == 0 and len(all_fiscal) == 0,
    }


def rebuild_combined():
    """Rebuild combined JSONL from merged meeting records."""
    records = []
    for jf in sorted(MERGED_DIR.glob("*.json")):
        try:
            records.append(json.loads(jf.read_text()))
        except Exception:
            continue

    records.sort(key=lambda r: r.get("date", ""))
    with open(MERGED_JSONL, "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")

    print(f"Combined: {len(records)} meetings → {MERGED_JSONL}")
    return len(records)


def find_changed_meetings(by_meeting, state):
    """Find meetings where source count changed since last merge."""
    changed = []
    for mid, records in by_meeting.items():
        prev_count = state.get(mid, {}).get("source_count", 0)
        if len(records) != prev_count:
            changed.append(mid)
    return changed


def cmd_merge(args):
    """Merge per-document records into per-meeting records."""
    MERGED_DIR.mkdir(parents=True, exist_ok=True)

    by_meeting = collect_records_by_meeting()
    state = load_state()

    if not by_meeting:
        print("No structured records found.")
        return

    if args.force:
        to_merge = list(by_meeting.keys())
    else:
        to_merge = find_changed_meetings(by_meeting, state)

    if not to_merge:
        print("All meetings up to date.")
        return

    print(f"Merging {len(to_merge)} meetings ({len(by_meeting)} total)")

    for mid in sorted(to_merge):
        records = by_meeting[mid]
        merged = merge_records(mid, records)
        out_path = MERGED_DIR / f"{mid}.json"
        out_path.write_text(json.dumps(merged, indent=2, default=str))

        state[mid] = {
            "source_count": len(records),
            "doc_types": merged["doc_types"],
        }

        types = ", ".join(merged["doc_types"])
        print(f"  {mid}: {len(records)} sources ({types}) → {merged['advocacy_score']}")

    save_state(state)
    rebuild_combined()

    # Report meetings that gained new sources (for prose re-summarization)
    gained = [mid for mid in to_merge if state.get(mid, {}).get("source_count", 0) > 1
              and mid in by_meeting and len(by_meeting[mid]) > state.get(mid, {}).get("prev_count", 0)]
    if gained:
        print(f"\n{len(gained)} meetings gained new sources since last merge")


def cmd_check(args):
    """List meetings with new sources since last merge."""
    by_meeting = collect_records_by_meeting()
    state = load_state()
    changed = find_changed_meetings(by_meeting, state)

    if changed:
        print(f"{len(changed)} meetings need merge:")
        for mid in sorted(changed):
            prev = state.get(mid, {}).get("source_count", 0)
            now = len(by_meeting[mid])
            print(f"  {mid}: {prev} → {now} sources")
    else:
        print("All meetings up to date.")


def cmd_stats(args):
    """Show merge statistics."""
    MERGED_DIR.mkdir(parents=True, exist_ok=True)

    merged_files = list(MERGED_DIR.glob("*.json"))
    if not merged_files:
        print("No merged records. Run meeting_merge.py first.")
        return

    source_counts = defaultdict(int)
    multi_source = 0
    has_transcript = 0
    scores = defaultdict(int)

    for jf in merged_files:
        try:
            r = json.loads(jf.read_text())
            sc = r.get("source_count", 1)
            source_counts[sc] += 1
            if sc > 1:
                multi_source += 1
            if "transcript" in r.get("doc_types", []):
                has_transcript += 1
            scores[r.get("advocacy_score", "neutral")] += 1
        except Exception:
            continue

    print(f"Total merged meetings: {len(merged_files)}")
    print(f"Multi-source meetings: {multi_source}")
    print(f"Meetings with transcript: {has_transcript}")
    print(f"\nSources per meeting:")
    for count in sorted(source_counts.keys()):
        print(f"  {count} source(s): {source_counts[count]} meetings")
    print(f"\nAdvocacy scores:")
    for score in ["red", "yellow", "neutral", "green"]:
        if scores[score]:
            print(f"  {score}: {scores[score]}")


def main():
    parser = argparse.ArgumentParser(description="Merge per-document records into per-meeting records")
    parser.add_argument("--force", action="store_true", help="Rebuild all merged records")
    parser.add_argument("--check", action="store_true", help="List meetings needing merge")
    parser.add_argument("--stats", action="store_true", help="Show merge statistics")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    elif args.check:
        cmd_check(args)
    else:
        cmd_merge(args)


if __name__ == "__main__":
    main()
