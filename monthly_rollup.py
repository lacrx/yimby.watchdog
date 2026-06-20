#!/usr/bin/env python3
"""
Roll up per-document structured JSONL records into monthly digests.

Pure data operation — no LLM, no information loss. Concatenates and deduplicates
structured arrays from individual records into one record per month.

Usage:
    python monthly_rollup.py                # build missing monthly digests
    python monthly_rollup.py --force        # rebuild all months
    python monthly_rollup.py --month 2026-03  # rebuild specific month
    python monthly_rollup.py --stats        # show monthly digest stats

Input:  data/structured/all-records.jsonl
Output: data/structured/monthly/{YYYY-MM}.json + monthly-digests.jsonl
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
MONTHLY_DIR = STRUCTURED_DIR / "monthly"
MERGED_JSONL = STRUCTURED_DIR / "meetings-combined.jsonl"
INDIVIDUAL_JSONL = STRUCTURED_DIR / "all-records.jsonl"
MONTHLY_JSONL = STRUCTURED_DIR / "monthly-digests.jsonl"


def load_records():
    """Load per-meeting merged records (preferred) or individual records (fallback)."""
    if MERGED_JSONL.exists():
        source = MERGED_JSONL
    elif INDIVIDUAL_JSONL.exists():
        source = INDIVIDUAL_JSONL
    else:
        print(f"No records found. Run extract_structured.py + meeting_merge.py first.")
        sys.exit(1)

    records = []
    with open(source) as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    print(f"Loaded {len(records)} records from {source.name}")
    return records


def group_by_month(records):
    """Group records by YYYY-MM."""
    by_month = defaultdict(list)
    for r in records:
        date = r.get("date", "")
        if len(date) >= 7:
            month = date[:7]
            by_month[month].append(r)
    return dict(sorted(by_month.items()))


def deduplicate(items):
    """Deduplicate a list of strings."""
    seen = set()
    result = []
    for item in items:
        key = item.lower().strip() if isinstance(item, str) else json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def merge_month(month, records):
    """Merge all records for a month into one digest."""
    bodies = set()
    agencies = set()
    all_votes = []
    all_housing = []
    all_fiscal = []
    all_legal = []
    all_positions = []
    all_comments = []
    all_quotes = []
    scores = {"green": 0, "yellow": 0, "red": 0, "neutral": 0}
    substantive_count = 0

    for r in records:
        if r.get("procedural_only"):
            continue
        substantive_count += 1

        if r.get("body"):
            bodies.add(r["body"])
        if r.get("agency"):
            agencies.add(r["agency"])

        score = r.get("advocacy_score", "neutral")
        scores[score] = scores.get(score, 0) + 1

        all_votes.extend(r.get("votes", []))
        all_housing.extend(r.get("housing_items", []))
        all_fiscal.extend(r.get("fiscal_items", []))
        all_legal.extend(r.get("legal_flags", []))
        all_positions.extend(r.get("council_positions", []))
        all_comments.extend(r.get("public_comments", []))
        all_quotes.extend(r.get("key_quotes", []))

    dominant_score = max(scores, key=scores.get) if substantive_count > 0 else "neutral"
    red_pct = scores["red"] / substantive_count * 100 if substantive_count > 0 else 0

    return {
        "month": month,
        "record_count": len(records),
        "substantive_count": substantive_count,
        "bodies": sorted(bodies),
        "agencies": sorted(agencies),
        "advocacy_summary": {
            "dominant_score": dominant_score,
            "breakdown": scores,
            "red_pct": round(red_pct, 1),
        },
        "votes": all_votes,
        "housing_items": all_housing,
        "fiscal_items": all_fiscal,
        "legal_flags": deduplicate(all_legal),
        "council_positions": all_positions,
        "public_comments": deduplicate(all_comments),
        "key_quotes": deduplicate(all_quotes),
    }


def rebuild_combined():
    """Rebuild the combined monthly digests JSONL."""
    digests = []
    for jf in sorted(MONTHLY_DIR.glob("*.json")):
        try:
            digests.append(json.loads(jf.read_text()))
        except Exception:
            continue

    with open(MONTHLY_JSONL, "w") as f:
        for d in digests:
            f.write(json.dumps(d, default=str) + "\n")

    print(f"Combined: {len(digests)} monthly digests → {MONTHLY_JSONL}")
    return len(digests)


def cmd_rollup(args):
    """Build monthly digests."""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)

    records = load_records()
    by_month = group_by_month(records)

    target_months = sorted(by_month.keys())
    if args.month:
        if args.month not in by_month:
            print(f"No records for {args.month}")
            return
        target_months = [args.month]

    built = 0
    skipped = 0

    for month in target_months:
        out_path = MONTHLY_DIR / f"{month}.json"
        if out_path.exists() and not args.force and not args.month:
            existing = json.loads(out_path.read_text())
            if existing.get("record_count", 0) == len(by_month[month]):
                skipped += 1
                continue

        month_records = by_month[month]
        digest = merge_month(month, month_records)
        out_path.write_text(json.dumps(digest, indent=2, default=str))
        built += 1

        score_bar = ""
        breakdown = digest["advocacy_summary"]["breakdown"]
        if breakdown["red"]:
            score_bar += f" 🔴{breakdown['red']}"
        if breakdown["yellow"]:
            score_bar += f" 🟡{breakdown['yellow']}"
        if breakdown["green"]:
            score_bar += f" 🟢{breakdown['green']}"

        print(f"  {month}: {digest['substantive_count']} substantive / {digest['record_count']} total{score_bar}")

    print(f"\nBuilt {built}, skipped {skipped} (already exist)")

    if built > 0:
        rebuild_combined()


def cmd_stats(args):
    """Show monthly digest statistics."""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)

    digests = sorted(MONTHLY_DIR.glob("*.json"))
    if not digests:
        print("No monthly digests found. Run monthly_rollup.py first.")
        return

    total_votes = 0
    total_housing = 0
    total_legal = 0
    total_records = 0

    print(f"{'Month':8s} {'Records':>8s} {'Subst':>6s} {'Votes':>6s} {'Housing':>8s} {'Legal':>6s} {'Score':>8s}")
    print("-" * 60)

    for df in digests:
        d = json.loads(df.read_text())
        votes = len(d.get("votes", []))
        housing = len(d.get("housing_items", []))
        legal = len(d.get("legal_flags", []))
        records = d.get("record_count", 0)
        subst = d.get("substantive_count", 0)
        score = d.get("advocacy_summary", {}).get("dominant_score", "?")

        total_votes += votes
        total_housing += housing
        total_legal += legal
        total_records += records

        print(f"{d['month']:8s} {records:8d} {subst:6d} {votes:6d} {housing:8d} {legal:6d} {score:>8s}")

    print("-" * 60)
    print(f"{'TOTAL':8s} {total_records:8d} {'':6s} {total_votes:6d} {total_housing:8d} {total_legal:6d}")
    print(f"\n{len(digests)} monthly digests")


def find_missing_months():
    """Find months that have individual records but no monthly digest."""
    if not INDIVIDUAL_JSONL.exists():
        return []

    records = load_records()
    by_month = group_by_month(records)
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)

    missing = []
    for month in sorted(by_month.keys()):
        digest_path = MONTHLY_DIR / f"{month}.json"
        if not digest_path.exists():
            missing.append(month)
        else:
            existing = json.loads(digest_path.read_text())
            if existing.get("record_count", 0) != len(by_month[month]):
                missing.append(month)

    return missing


def main():
    parser = argparse.ArgumentParser(description="Build monthly digests from structured JSONL")
    parser.add_argument("--force", action="store_true", help="Rebuild all months")
    parser.add_argument("--month", help="Rebuild specific month (YYYY-MM)")
    parser.add_argument("--stats", action="store_true", help="Show monthly stats")
    parser.add_argument("--check", action="store_true", help="List months needing rebuild")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    elif args.check:
        missing = find_missing_months()
        if missing:
            print(f"{len(missing)} months need rebuild: {', '.join(missing)}")
        else:
            print("All months up to date.")
    else:
        cmd_rollup(args)


if __name__ == "__main__":
    main()
