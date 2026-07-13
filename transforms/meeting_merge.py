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
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from civic_utils import all_docs_dirs

DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
MERGED_JSONL = STRUCTURED_DIR / "meetings-combined.jsonl"
STATE_FILE = STRUCTURED_DIR / "meetings-state.json"
DOC_DATES_FILE = STRUCTURED_DIR / "document-dates.json"


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


MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_document_date(text):
    """Extract a date from the first 1000 chars of a document's raw text.

    Returns ISO date string (YYYY-MM-DD) or None.
    """
    head = text[:1000]

    # "DATE:  August 5, 2020" (staff reports)
    m = re.search(r"DATE:\s*(\w+)\s+(\d{1,2}),?\s+(\d{4})", head)
    if m:
        month = MONTH_NAMES.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"

    # "October 4, 2023" or "October 4 2023" (minutes, agendas)
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(\d{4})", head)
    if m:
        month = MONTH_NAMES.get(m.group(1).lower())
        if month:
            return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"

    # "12/18/2024" (slash dates)
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", head)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"

    # "2024-12-18" (ISO)
    m = re.search(r"(20\d{2})-(\d{2})-(\d{2})", head)
    if m:
        return m.group(0)

    return None


def scan_document_dates():
    """Scan all raw document text files and extract dates from content.

    Returns dict mapping filename stem to ISO date string.
    """
    dates = {}
    for docs_dir in all_docs_dirs():
        for f in docs_dir.glob("*.txt"):
            try:
                text = f.read_text(errors="replace")
                d = parse_document_date(text)
                if d:
                    dates[f.stem] = d
            except Exception:
                continue
    return dates


def load_document_dates():
    """Load cached document dates, scanning if cache doesn't exist."""
    if DOC_DATES_FILE.exists():
        return json.loads(DOC_DATES_FILE.read_text())
    dates = scan_document_dates()
    DOC_DATES_FILE.write_text(json.dumps(dates, indent=2, sort_keys=True))
    return dates


def parse_filename_year(filename):
    """Extract a year from a filename if it refers to a historical document.

    Returns the year as int if found and clearly refers to document origin
    (not a fiscal year, budget label, or project number), else None.
    """
    # Strip meeting_id prefix and item_id: "1355279-8043738-2023_10_04_Meeting_Minutes"
    parts = filename.split("-", 2)
    name = parts[2] if len(parts) > 2 else filename

    # YYYY_MM_DD or MM_DD_YYYY or MM_D_YYYY patterns (underscore-separated dates)
    m = re.search(r"(20\d{2})_(\d{1,2})_(\d{1,2})", name)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{1,2})_(\d{1,2})_(20\d{2})", name)
    if m:
        return int(m.group(3))

    # "MM_DD_YYYY" with hyphens: "10-04-2023"
    m = re.search(r"(\d{1,2})-(\d{1,2})-(20\d{2})", name)
    if m:
        return int(m.group(3))

    # Ordinance/resolution numbers: "Ordinance_No__21_OR" or "Resolution_No__23_"
    # Two-digit year prefix before _OR, _D, _P (ordinance/directive/policy markers)
    m = re.search(r"No__(\d{2})_[A-Z]", name)
    if m:
        yy = int(m.group(1))
        if 19 <= yy <= 99:
            return 1900 + yy
        elif 0 <= yy <= 30:
            return 2000 + yy

    return None


def filename_date_matches(filename, meeting_date):
    """Check if a filename's embedded date is consistent with the meeting date.

    Returns True if no date found in filename, or if the year matches.
    Returns False only when a clear date mismatch is detected.
    """
    if not meeting_date:
        return True
    try:
        meeting_year = int(meeting_date[:4])
    except (ValueError, IndexError):
        return True

    file_year = parse_filename_year(filename)
    if file_year is None:
        return True

    return file_year == meeting_year


def merge_records(meeting_id, records, doc_dates=None):
    """Merge multiple document records into one consolidated meeting record.

    Historical attachments (old minutes, resolutions included as exhibits) are
    detected by date mismatch and excluded from votes/positions/quotes to avoid
    contaminating the meeting record with stale council member data.

    Date matching uses three signals (any mismatch triggers filtering):
      1. Record's extracted date vs meeting's majority-vote date
      2. Filename-embedded year vs meeting year
      3. Raw document text date (from first 1000 chars) vs meeting date
    """
    if doc_dates is None:
        doc_dates = {}
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

    # First pass: determine the meeting's canonical date via majority vote
    for r in records:
        if r.get("date"):
            dates.append(r["date"])

    from collections import Counter
    meeting_date = Counter(dates).most_common(1)[0][0] if dates else ""
    dates = []  # reset for second pass

    for r in records:
        if r.get("procedural_only") and len(records) > 1:
            continue

        record_date = r.get("date", "")
        record_file = r.get("_file", "")
        record_stem = record_file.replace(".json", "") if record_file else ""

        date_matches = (not record_date or not meeting_date or record_date == meeting_date) \
            and filename_date_matches(record_file, meeting_date)

        # Check raw document content date if available
        if date_matches and record_stem and meeting_date:
            doc_date = doc_dates.get(record_stem)
            if doc_date and doc_date[:4] != meeting_date[:4]:
                date_matches = False

        if r.get("body"):
            bodies.add(r["body"])
        if r.get("agency"):
            agencies.append(r["agency"])
        if record_date:
            dates.append(record_date)
        if r.get("doc_type"):
            doc_types.add(r["doc_type"])
        sources.append({
            "file": r.get("_file", r.get("_source", "")),
            "type": r.get("_source_type", r.get("doc_type", "")),
            "date_match": date_matches,
        })

        if date_matches:
            all_votes.extend(r.get("votes", []))
            all_positions.extend(r.get("council_positions", []))
            all_quotes.extend(r.get("key_quotes", []))

        # Housing, fiscal, legal always included — historical context is relevant
        all_housing.extend(r.get("housing_items", []))
        all_fiscal.extend(r.get("fiscal_items", []))
        all_legal.extend(r.get("legal_flags", []))
        all_comments.extend(r.get("public_comments", []))

    # Deduplicate — items may be strings or dicts
    def _dedup(items):
        seen = set()
        out = []
        for item in items:
            key = json.dumps(item, sort_keys=True) if isinstance(item, dict) else item
            if key not in seen:
                seen.add(key)
                out.append(item)
        return out

    all_legal = _dedup(all_legal)
    all_comments = _dedup(all_comments)
    all_quotes = _dedup(all_quotes)

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

    date = Counter(dates).most_common(1)[0][0] if dates else ""
    agency = Counter(agencies).most_common(1)[0][0] if agencies else "Unknown"

    return {
        "meeting_id": meeting_id,
        "date": date,
        "body": sorted(bodies)[0] if bodies else "Unknown",
        "agency": agency,
        "doc_types": sorted(doc_types),
        "source_count": len(records),
        "sources": sources,
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

    if args.meeting:
        to_merge = [mid for mid in args.meeting if mid in by_meeting]
    elif args.force:
        to_merge = list(by_meeting.keys())
    else:
        to_merge = find_changed_meetings(by_meeting, state)

    if not to_merge:
        print("All meetings up to date.")
        return

    print(f"Merging {len(to_merge)} meetings ({len(by_meeting)} total)")

    doc_dates = load_document_dates()
    print(f"  Document dates loaded: {len(doc_dates)} files")

    for mid in sorted(to_merge):
        records = by_meeting[mid]
        merged = merge_records(mid, records, doc_dates)
        out_path = MERGED_DIR / f"{mid}.json"
        out_path.write_text(json.dumps(merged, indent=2, default=str))

        state[mid] = {
            "source_count": len(records),
            "doc_types": merged["doc_types"],
        }

        types = ", ".join(merged["doc_types"])
        v = len(merged.get("votes", []))
        h = len(merged.get("housing_items", []))
        print(f"  {mid}: {len(records)} sources ({types}) → {v}v {h}h")

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
    total_votes = 0
    total_housing = 0

    for jf in merged_files:
        try:
            r = json.loads(jf.read_text())
            sc = r.get("source_count", 1)
            source_counts[sc] += 1
            if sc > 1:
                multi_source += 1
            if "transcript" in r.get("doc_types", []):
                has_transcript += 1
            total_votes += len(r.get("votes", []))
            total_housing += len(r.get("housing_items", []))
        except Exception:
            continue

    print(f"Total merged meetings: {len(merged_files)}")
    print(f"Multi-source meetings: {multi_source}")
    print(f"Meetings with transcript: {has_transcript}")
    print(f"Total votes: {total_votes}")
    print(f"Total housing items: {total_housing}")
    print(f"\nSources per meeting:")
    for count in sorted(source_counts.keys()):
        print(f"  {count} source(s): {source_counts[count]} meetings")


def main():
    parser = argparse.ArgumentParser(description="Merge per-document records into per-meeting records")
    parser.add_argument("--force", action="store_true", help="Rebuild all merged records")
    parser.add_argument("--check", action="store_true", help="List meetings needing merge")
    parser.add_argument("--stats", action="store_true", help="Show merge statistics")
    parser.add_argument("--meeting", action="append", metavar="ID", help="Only merge these meeting IDs (repeatable)")
    parser.add_argument("--scan-dates", action="store_true", help="Rescan document dates from raw text files")

    args = parser.parse_args()

    if args.scan_dates:
        print("Scanning document dates from raw text...")
        dates = scan_document_dates()
        DOC_DATES_FILE.write_text(json.dumps(dates, indent=2, sort_keys=True))
        print(f"Scanned {len(dates)} document dates → {DOC_DATES_FILE}")
    elif args.stats:
        cmd_stats(args)
    elif args.check:
        cmd_check(args)
    else:
        cmd_merge(args)


if __name__ == "__main__":
    main()
