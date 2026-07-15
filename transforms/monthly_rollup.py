#!/usr/bin/env python3
"""
Roll up all data sources into independent monthly digests.

Each month is self-contained — rebuilds only when its inputs change.
Content-hashes all source data per month; any change (new record, updated
extraction, new permit, new intel item) triggers a rebuild for that month only.

Data sources:
  1. Meeting records  — data/structured/meetings/*.json (merged per-meeting)
  2. Building permits — data/{agency}/permits/etrakit-permits-*.jsonl
  3. Intel feed       — data/intel/intel-*.json

Usage:
    python monthly_rollup.py                # build stale months
    python monthly_rollup.py --force        # rebuild all months
    python monthly_rollup.py --month 2026-03  # rebuild specific month
    python monthly_rollup.py --stats        # show monthly digest stats

Output: data/structured/monthly/{YYYY-MM}.json + monthly-digests.jsonl
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import load_agencies, agency_data_dir

DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
MONTHLY_DIR = STRUCTURED_DIR / "monthly"
MONTHLY_JSONL = STRUCTURED_DIR / "monthly-digests.jsonl"
INTEL_DIR = DATA_DIR / "intel"

HOUSING_PERMIT_TYPES = {
    "BLD SFD OR DUPLEX", "BLD MULTI FAMILY", "BLD ACCESSORY DWELLING",
}

PROJECTS_FILE = STRUCTURED_DIR / "permit-projects.json"


def parse_date_to_month(date_str):
    """Parse M/D/YYYY or YYYY-MM-DD to YYYY-MM. Returns None on failure."""
    if not date_str:
        return None
    if "/" in date_str:
        parts = date_str.split("/")
        if len(parts) == 3:
            try:
                return f"{int(parts[2]):04d}-{int(parts[0]):02d}"
            except ValueError:
                return None
    elif "-" in date_str and len(date_str) >= 7:
        return date_str[:7]
    return None


def content_hash(data):
    """Stable hash of serialized data for change detection."""
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ── Source loaders ──

def load_meetings_by_month():
    """Load merged per-meeting records, grouped by month."""
    by_month = defaultdict(list)
    if not MERGED_DIR.exists():
        return by_month
    for jf in sorted(MERGED_DIR.glob("*.json")):
        try:
            r = json.loads(jf.read_text())
            month = parse_date_to_month(r.get("date", ""))
            if month:
                by_month[month].append(r)
        except (json.JSONDecodeError, Exception):
            continue
    return by_month


def load_permits_by_month():
    """Load building permits from all agencies, grouped by month. Only housing-relevant types."""
    by_month = defaultdict(list)
    agencies = load_agencies(enabled_only=True)
    for slug, cfg in agencies.items():
        if "permits" not in cfg:
            continue
        permits_dir = agency_data_dir(slug) / "permits"
        if not permits_dir.exists():
            continue
        for pf in sorted(permits_dir.glob("etrakit-permits-*.jsonl")):
            try:
                with open(pf) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        r = json.loads(line)
                        ptype = r.get("type", "")
                        if ptype not in HOUSING_PERMIT_TYPES:
                            continue
                        month = parse_date_to_month(r.get("applied", ""))
                        if month:
                            by_month[month].append(r)
            except (json.JSONDecodeError, Exception):
                continue
    return by_month


def load_intel_by_month():
    """Load intel feed items, grouped by month."""
    by_month = defaultdict(list)
    for inf in sorted(INTEL_DIR.glob("intel-*.json")):
        try:
            items = json.loads(inf.read_text())
            if not isinstance(items, list):
                continue
            for item in items:
                pub = item.get("published", "")
                month = None
                # Try parsing RFC 2822 date
                for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d"):
                    try:
                        dt = datetime.strptime(pub.strip(), fmt)
                        month = dt.strftime("%Y-%m")
                        break
                    except ValueError:
                        continue
                if not month:
                    # Fall back to filename date: intel-YYYY-MM-DD.json
                    match = re.search(r"intel-(\d{4}-\d{2})", inf.name)
                    if match:
                        month = match.group(1)
                if month:
                    by_month[month].append(item)
        except (json.JSONDecodeError, Exception):
            continue
    return by_month


# ── Merge logic ──

def deduplicate(items):
    """Deduplicate a list of strings or dicts."""
    seen = set()
    result = []
    for item in items:
        key = item.lower().strip() if isinstance(item, str) else json.dumps(item, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def load_projects():
    """Load permit projects index. Returns dict: permit_no → project record."""
    if not PROJECTS_FILE.exists():
        return {}, []
    try:
        data = json.loads(PROJECTS_FILE.read_text())
        projects = data.get("projects", [])
        index = {}
        for proj in projects:
            for pno in proj.get("permits", []):
                index[pno] = proj
        return index, projects
    except (json.JSONDecodeError, Exception):
        return {}, []


def summarize_permits(permits):
    """Summarize permits into counts by type and status."""
    by_type = Counter()
    by_status = Counter()
    total_units = 0
    for p in permits:
        by_type[p.get("type", "unknown")] += 1
        by_status[p.get("status", "unknown")] += 1
        desc = p.get("description", "").upper()
        units = 1
        match = re.search(r"(\d+)\s*(?:UNIT|DU|DWELLING)", desc)
        if match:
            units = int(match.group(1))
        total_units += units
    return {
        "total": len(permits),
        "estimated_units": total_units,
        "by_type": dict(by_type.most_common()),
        "by_status": dict(by_status.most_common()),
    }


def merge_month(month, meetings, permits, intel, project_index=None):
    """Merge all data sources for a month into one digest."""
    bodies = set()
    agencies = set()
    all_votes = []
    all_housing = []
    all_fiscal = []
    all_legal = []
    all_positions = []
    all_comments = []
    all_quotes = []
    substantive_count = 0

    for r in meetings:
        if r.get("body"):
            bodies.add(r["body"])
        if r.get("agency"):
            agencies.add(r["agency"])

        if r.get("procedural_only"):
            continue
        substantive_count += 1

        all_votes.extend(r.get("votes", []))
        all_housing.extend(r.get("housing_items", []))
        all_fiscal.extend(r.get("fiscal_items", []))
        all_legal.extend(r.get("legal_flags", []))
        all_positions.extend(r.get("council_positions", []))
        all_comments.extend(r.get("public_comments", []))
        all_quotes.extend(r.get("key_quotes", []))

    digest = {
        "month": month,
        "meeting_count": len(meetings),
        "substantive_count": substantive_count,
        "bodies": sorted(bodies),
        "agencies": sorted(agencies),
        "activity_summary": {
            "meetings_with_votes": sum(1 for r in meetings if r.get("votes")),
            "meetings_with_housing": sum(1 for r in meetings if r.get("housing_items")),
            "total_votes": len(all_votes),
            "total_housing_items": len(all_housing),
            "total_legal_flags": len(all_legal),
        },
        "votes": all_votes,
        "housing_items": all_housing,
        "fiscal_items": all_fiscal,
        "legal_flags": deduplicate(all_legal),
        "council_positions": all_positions,
        "public_comments": deduplicate(all_comments),
        "key_quotes": deduplicate(all_quotes),
    }

    if permits:
        permit_summary = summarize_permits(permits)
        if project_index:
            active_projects = {}
            for p in permits:
                proj = project_index.get(p.get("permit_no"))
                if proj and proj["project_id"] not in active_projects:
                    entry = {
                        "project_name": proj["project_name"],
                        "estimated_units": proj["estimated_units"],
                        "permits_this_month": 0,
                    }
                    if proj.get("owner") and proj.get("source") != "type_aggregate":
                        entry["owner"] = proj["owner"]
                    active_projects[proj["project_id"]] = entry
                if proj:
                    active_projects[proj["project_id"]]["permits_this_month"] += 1
            if active_projects:
                permit_summary["projects"] = sorted(
                    active_projects.values(),
                    key=lambda x: x["permits_this_month"],
                    reverse=True,
                )
        digest["permits"] = permit_summary

    if intel:
        digest["intel"] = [
            {
                "source": i.get("source", ""),
                "title": i.get("title", ""),
                "url": i.get("url", ""),
                "relevance_score": i.get("relevance_score", 0),
            }
            for i in intel
        ]

    # Source fingerprint for change detection
    digest["_source_hash"] = content_hash({
        "meetings": [(r.get("meeting_id"), r.get("source_count")) for r in meetings],
        "permits": len(permits),
        "intel": len(intel),
        "meeting_content": content_hash(meetings),
    })

    return digest


# ── Commands ──

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
    """Build monthly digests from all data sources."""
    MONTHLY_DIR.mkdir(parents=True, exist_ok=True)

    meetings_by_month = load_meetings_by_month()
    permits_by_month = load_permits_by_month()
    intel_by_month = load_intel_by_month()
    project_index, _ = load_projects()

    all_months = sorted(set(
        list(meetings_by_month.keys()) +
        list(permits_by_month.keys()) +
        list(intel_by_month.keys())
    ))

    m_count = sum(len(v) for v in meetings_by_month.values())
    p_count = sum(len(v) for v in permits_by_month.values())
    i_count = sum(len(v) for v in intel_by_month.values())
    proj_str = f", {len(project_index)} permit→project mappings" if project_index else ""
    print(f"Loaded {m_count} meetings, {p_count} permits, {i_count} intel items across {len(all_months)} months{proj_str}")

    if args.month:
        if args.month not in all_months:
            print(f"No data for {args.month}")
            return
        all_months = [args.month]

    built = 0
    skipped = 0

    for month in all_months:
        meetings = meetings_by_month.get(month, [])
        permits = permits_by_month.get(month, [])
        intel = intel_by_month.get(month, [])

        digest = merge_month(month, meetings, permits, intel, project_index)

        out_path = MONTHLY_DIR / f"{month}.json"
        if out_path.exists() and not args.force and not args.month:
            existing = json.loads(out_path.read_text())
            if existing.get("_source_hash") == digest["_source_hash"]:
                skipped += 1
                continue

        out_path.write_text(json.dumps(digest, indent=2, default=str))
        built += 1

        activity = digest["activity_summary"]
        counts = f" ({activity['total_votes']}v {activity['total_housing_items']}h)"

        parts = [f"{digest['substantive_count']} meetings"]
        if permits:
            parts.append(f"{digest['permits']['total']} permits")
        if intel:
            parts.append(f"{len(intel)} intel")

        print(f"  {month}: {' + '.join(parts)}{counts}")

    print(f"\nBuilt {built}, skipped {skipped} (unchanged)")

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
    total_meetings = 0
    total_permits = 0

    print(f"{'Month':8s} {'Mtgs':>5s} {'Subst':>6s} {'Votes':>6s} {'Housing':>8s} {'Legal':>6s} {'Permits':>8s}")
    print("-" * 60)

    for df in digests:
        d = json.loads(df.read_text())
        votes = len(d.get("votes", []))
        housing = len(d.get("housing_items", []))
        legal = len(d.get("legal_flags", []))
        meetings = d.get("meeting_count", d.get("record_count", 0))
        subst = d.get("substantive_count", 0)
        permits = d.get("permits", {}).get("total", 0)

        total_votes += votes
        total_housing += housing
        total_legal += legal
        total_meetings += meetings
        total_permits += permits

        print(f"{d['month']:8s} {meetings:5d} {subst:6d} {votes:6d} {housing:8d} {legal:6d} {permits:8d}")

    print("-" * 60)
    print(f"{'TOTAL':8s} {total_meetings:5d} {'':6s} {total_votes:6d} {total_housing:8d} {total_legal:6d} {total_permits:8d}")
    print(f"\n{len(digests)} monthly digests")


def main():
    parser = argparse.ArgumentParser(description="Build monthly digests from all data sources")
    parser.add_argument("--force", action="store_true", help="Rebuild all months")
    parser.add_argument("--month", help="Rebuild specific month (YYYY-MM)")
    parser.add_argument("--stats", action="store_true", help="Show monthly stats")
    parser.add_argument("--check", action="store_true", help="List months needing rebuild")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    elif args.check:
        # Quick check: compute hashes and compare
        MONTHLY_DIR.mkdir(parents=True, exist_ok=True)
        meetings_by_month = load_meetings_by_month()
        permits_by_month = load_permits_by_month()
        intel_by_month = load_intel_by_month()
        project_index, _ = load_projects()
        all_months = sorted(set(
            list(meetings_by_month.keys()) +
            list(permits_by_month.keys()) +
            list(intel_by_month.keys())
        ))
        stale = []
        for month in all_months:
            digest = merge_month(
                month,
                meetings_by_month.get(month, []),
                permits_by_month.get(month, []),
                intel_by_month.get(month, []),
                project_index,
            )
            out_path = MONTHLY_DIR / f"{month}.json"
            if not out_path.exists():
                stale.append((month, "missing"))
            else:
                existing = json.loads(out_path.read_text())
                if existing.get("_source_hash") != digest["_source_hash"]:
                    stale.append((month, "changed"))
        if stale:
            print(f"{len(stale)} months need rebuild:")
            for month, reason in stale:
                print(f"  {month}: {reason}")
        else:
            print("All months up to date.")
    else:
        cmd_rollup(args)


if __name__ == "__main__":
    main()
