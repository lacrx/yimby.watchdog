#!/usr/bin/env python3
"""
Estimate extraction volume for rollout planning.

Tallies documents per agency per day from doc-index meeting dates,
then projects daily extraction load at different coverage tiers.

Agencies have three extraction tiers in agencies.yaml:
  - FULL:    extract all documents (hot + cold backlog)
  - FORWARD: extract only documents from enabled_date onward
  - TALLY:   scrape only, no extraction — counted here for projections

Usage:
    python pipeline_estimate.py                  # show daily tallies + projections
    python pipeline_estimate.py --since 2026-07-13  # custom start date
    python pipeline_estimate.py --days 14        # last N days (default: since Jul 13)
    python pipeline_estimate.py --csv            # CSV output for spreadsheets
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO_ROOT))
from civic_utils import load_agencies, agency_data_dir

DATA_DIR = REPO_ROOT / "data"


def default_since_date():
    """Derive default start date from the earliest enabled_date in agencies.yaml."""
    agencies = load_agencies(enabled_only=True)
    enabled_dates = [cfg["enabled_date"] for cfg in agencies.values() if cfg.get("enabled_date")]
    if enabled_dates:
        return min(enabled_dates)
    return (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")


def load_doc_dates():
    """Load meeting dates from all agencies' doc-index files."""
    agencies = load_agencies(enabled_only=True)
    docs_by_agency = defaultdict(list)

    for slug in agencies:
        idx_path = agency_data_dir(slug) / "doc-index.json"
        if not idx_path.exists():
            continue
        with open(idx_path) as f:
            idx = json.load(f)
        docs = idx.get("documents", {})
        if not isinstance(docs, dict):
            continue

        for doc_key, meta in docs.items():
            doc_date = meta.get("date") or meta.get("meeting_date")
            if not doc_date:
                continue
            try:
                # normalize M/D/YYYY to ISO
                if "/" in doc_date:
                    parts = doc_date.split("/")
                    doc_date = f"{parts[2]}-{int(parts[0]):02d}-{int(parts[1]):02d}"
                dt = datetime.fromisoformat(doc_date[:10])
            except (ValueError, IndexError):
                continue

            # skip if marked skip or tiny
            txt_path = agency_data_dir(slug) / "documents" / doc_key
            skip_path = txt_path.parent / (doc_key + ".skip")
            if skip_path.exists():
                continue
            if txt_path.exists() and txt_path.stat().st_size < 200:
                continue

            docs_by_agency[slug].append(dt.strftime("%Y-%m-%d"))

    return docs_by_agency


def get_agency_tier(slug, cfg):
    if cfg.get("tally_only"):
        return "TALLY"
    if cfg.get("forward_only"):
        return "FORWARD"
    return "FULL"


def tally(since_date):
    agencies = load_agencies(enabled_only=True)
    docs_by_agency = load_doc_dates()

    # Per-agency per-day counts
    daily = defaultdict(lambda: defaultdict(int))
    for slug, dates in docs_by_agency.items():
        for d in dates:
            if d >= since_date:
                daily[d][slug] += 1

    return daily, agencies, docs_by_agency


def print_report(since_date, csv_mode=False):
    daily, agencies, docs_by_agency = tally(since_date)
    today = datetime.now().strftime("%Y-%m-%d")

    # Agency metadata
    tiers = {}
    for slug, cfg in agencies.items():
        tiers[slug] = get_agency_tier(slug, cfg)

    # Find all active agencies in window
    active = sorted(set(a for d in daily.values() for a in d))
    if not active:
        print(f"No documents found since {since_date}.")
        return

    days = sorted(daily.keys())
    num_days = max(1, len(days))

    # --- Per-agency summary ---
    agency_totals = {a: sum(daily[d].get(a, 0) for d in days) for a in active}

    if csv_mode:
        print("agency,tier,docs,days,docs_per_day")
        for a in sorted(active, key=lambda x: agency_totals[x], reverse=True):
            t = agency_totals[a]
            print(f"{a},{tiers.get(a, '?')},{t},{num_days},{t/num_days:.2f}")
        return

    print(f"Document volume: {since_date} → {today} ({num_days} days)")
    print()

    # Daily grid
    header = f"{'Date':<12}"
    for a in active:
        header += f"{a[:8]:>10}"
    header += f"{'TOTAL':>8}"
    print(header)
    print("-" * len(header))

    for day in days:
        total = sum(daily[day].get(a, 0) for a in active)
        if total == 0:
            continue
        row = f"{day:<12}"
        for a in active:
            c = daily[day].get(a, 0)
            row += f"{c:>10}" if c > 0 else f"{'·':>10}"
        row += f"{total:>8}"
        print(row)

    print("-" * len(header))
    row = f"{'TOTAL':<12}"
    grand = 0
    for a in active:
        t = agency_totals[a]
        grand += t
        row += f"{t:>10}"
    row += f"{grand:>8}"
    print(row)

    row = f"{'per day':<12}"
    for a in active:
        row += f"{agency_totals[a]/num_days:>10.1f}"
    row += f"{grand/num_days:>8.1f}"
    print(row)

    # --- Tier breakdown ---
    print()
    print("Extraction tiers (from agencies.yaml):")
    tier_groups = defaultdict(list)
    for slug in sorted(agencies.keys()):
        tier_groups[tiers[slug]].append(slug)

    for tier in ["FULL", "FORWARD", "TALLY"]:
        slugs = tier_groups.get(tier, [])
        t_docs = sum(agency_totals.get(s, 0) for s in slugs)
        t_rate = t_docs / num_days
        label = {"FULL": "extract all", "FORWARD": "extract new", "TALLY": "scrape only"}[tier]
        print(f"  {tier:<8} ({label}): {len(slugs)} agencies, {t_docs} docs, {t_rate:.1f}/day")
        for s in slugs:
            d = agency_totals.get(s, 0)
            marker = f" ({d} docs, {d/num_days:.1f}/day)" if d else ""
            print(f"           {s}{marker}")

    # --- Rollout projections ---
    print()
    print("Rollout projections (docs/day):")

    full_rate = sum(agency_totals.get(s, 0) for s in tier_groups.get("FULL", [])) / num_days
    fwd_rate = sum(agency_totals.get(s, 0) for s in tier_groups.get("FORWARD", [])) / num_days
    tally_rate = sum(agency_totals.get(s, 0) for s in tier_groups.get("TALLY", [])) / num_days

    scenarios = [
        ("Current (FULL only)", full_rate),
        ("+ FORWARD agencies", full_rate + fwd_rate),
        ("+ TALLY → FORWARD", full_rate + fwd_rate + tally_rate),
    ]

    for label, rate in scenarios:
        monthly = rate * 30
        print(f"  {label:<30} {rate:>6.1f}/day  ~{monthly:>5.0f}/month")

    # Agencies with zero docs in window (may be on recess or not yet posting)
    zero_agencies = [s for s in agencies if agency_totals.get(s, 0) == 0 and tiers[s] != "TALLY"]
    if zero_agencies:
        print()
        print(f"No docs in window ({since_date}→{today}):")
        for s in zero_agencies:
            # Find their latest meeting date for context
            all_dates = docs_by_agency.get(s, [])
            latest = max(all_dates) if all_dates else "never"
            print(f"  {s} (last meeting: {latest})")
        print("  Projections may undercount if these agencies are on recess.")

    print()
    print("Cost notes:")
    print("  Each doc = 1 claude -p call (~$0.02-0.08 depending on length)")
    print("  Hot queue runs 1-2x/day; cold backlog runs overnight")
    print("  Rate limit: ~150 extractions/night on Max plan")


def main():
    parser = argparse.ArgumentParser(description="Estimate extraction volume for rollout")
    default_since = default_since_date()
    parser.add_argument("--since", default=default_since, help=f"Start date (default: {default_since}, from earliest enabled_date)")
    parser.add_argument("--days", type=int, help="Last N days (overrides --since)")
    parser.add_argument("--csv", action="store_true", help="CSV output")
    args = parser.parse_args()

    if args.days:
        since = (datetime.now() - timedelta(days=args.days)).strftime("%Y-%m-%d")
    else:
        since = args.since

    print_report(since, csv_mode=args.csv)


if __name__ == "__main__":
    main()
