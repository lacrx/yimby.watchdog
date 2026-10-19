#!/usr/bin/env python3
"""
ArcGIS Feature Service building permit scraper.

Generic scraper for cities that publish building permits via ArcGIS Hub /
ArcGIS Feature Service. Config-driven field mapping from agencies.yaml.

Usage:
    python arcgis_permits.py fetch                     # incremental, current year
    python arcgis_permits.py fetch --agency carlsbad   # explicit agency
    python arcgis_permits.py fetch --year 2023         # specific year
    python arcgis_permits.py fetch --full              # full rescan, all years
    python arcgis_permits.py stats --agency carlsbad   # show permit summary

Requires: requests, pyyaml
"""

import argparse
import json
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests

from civic_utils import load_agencies, agency_data_dir

UA = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"

CANONICAL_FIELDS = [
    "permit_no", "type", "subtype", "description", "status",
    "applied", "approved", "issued", "address", "apn", "owner",
]


def _make_session():
    session = requests.Session()
    session.headers["User-Agent"] = UA
    retry_adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.packages.urllib3.util.retry.Retry(
            total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503]
        )
    )
    session.mount("https://", retry_adapter)
    return session


def _load_permit_config(agency_slug):
    """Load and validate ArcGIS permit config for an agency."""
    agencies = load_agencies(enabled_only=False)
    config = agencies.get(agency_slug)
    if not config or "permits" not in config:
        print(f"No permit config for agency '{agency_slug}'")
        sys.exit(1)
    permit_config = config["permits"]
    if permit_config.get("platform") != "arcgis":
        print(f"Agency '{agency_slug}' platform is '{permit_config.get('platform')}', not arcgis")
        sys.exit(1)
    return permit_config


def normalize_arcgis_date(value):
    """Convert ArcGIS date (epoch ms or ISO string) to YYYY-MM-DD."""
    if not value:
        return ""
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d")
    if isinstance(value, str):
        m = re.match(r"(\d{4}-\d{2}-\d{2})", value)
        if m:
            return m.group(1)
    return ""


def map_feature_to_permit(feature, field_map):
    """Map ArcGIS feature attributes to canonical permit schema."""
    attrs = feature.get("attributes", {})
    permit = {}
    for canonical, arcgis_field in field_map.items():
        permit[canonical] = attrs.get(arcgis_field, "") or ""

    for date_field in ("applied", "approved", "issued", "completed"):
        if date_field in permit:
            permit[date_field] = normalize_arcgis_date(permit[date_field])

    for required in CANONICAL_FIELDS:
        if required not in permit:
            permit[required] = ""

    return permit


def fetch_features(session, service_url, where="1=1", page_size=2000, delay=0.5):
    """Paginate through all features from an ArcGIS Feature Service."""
    offset = 0
    all_features = []
    while True:
        params = {
            "where": where,
            "outFields": "*",
            "resultOffset": offset,
            "resultRecordCount": page_size,
            "f": "json",
        }
        resp = session.get(f"{service_url}/query", params=params, timeout=60)
        resp.raise_for_status()
        data = resp.json()

        if "error" in data:
            print(f"  ArcGIS error: {data['error'].get('message', data['error'])}")
            break

        features = data.get("features", [])
        if not features:
            break
        all_features.extend(features)
        offset += len(features)

        if not data.get("exceededTransferLimit", False):
            break
        if delay:
            time.sleep(delay)

    return all_features


def load_existing_permits(permits_dir, prefix="arcgis"):
    """Load all existing permits from JSONL files. Returns dict keyed by permit_no."""
    existing = {}
    for pf in sorted(permits_dir.glob(f"{prefix}-permits-*.jsonl")):
        with open(pf) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    existing[rec["permit_no"]] = rec
                except (json.JSONDecodeError, KeyError):
                    continue
    return existing


def write_permits_by_year(permits, permits_dir, prefix="arcgis"):
    """Partition permits by year and write to JSONL files."""
    by_year = defaultdict(list)
    for p in permits:
        date_str = p.get("applied") or p.get("issued") or ""
        m = re.match(r"(\d{4})", date_str)
        if m:
            by_year[m.group(1)].append(p)
        else:
            by_year["unknown"].append(p)

    if "unknown" in by_year:
        print(f"  {len(by_year['unknown'])} permits with no parseable date, skipped.")
        del by_year["unknown"]

    total = 0
    for year in sorted(by_year):
        out_file = permits_dir / f"{prefix}-permits-{year}.jsonl"
        records = sorted(by_year[year], key=lambda p: p.get("applied", ""))
        with open(out_file, "w") as f:
            for rec in records:
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        total += len(records)
        print(f"  {out_file.name}: {len(records)} permits")

    return total


def _build_where(date_field, year=None, hwm=None):
    """Build the ArcGIS where clause for a fetch."""
    if year:
        return f"{date_field} >= DATE '{year}-01-01' AND {date_field} < DATE '{year + 1}-01-01'"
    if hwm:
        return f"{date_field} > DATE '{hwm}'"
    return "1=1"


def cmd_fetch(args):
    """Fetch building permits from ArcGIS Feature Service."""
    permit_config = _load_permit_config(args.agency)
    service_url = permit_config["service_url"]
    field_map = permit_config["field_map"]
    date_field = permit_config.get("date_field", "AppliedDate")

    permits_dir = agency_data_dir(args.agency) / "permits"
    permits_dir.mkdir(parents=True, exist_ok=True)

    session = _make_session()
    existing = {} if args.full else load_existing_permits(permits_dir)

    hwm = None
    if not args.full and not args.year and existing:
        dates = [p.get("applied", "") for p in existing.values() if p.get("applied")]
        hwm = max(dates) if dates else None

    where = _build_where(date_field, year=args.year, hwm=hwm)

    mode = "Full" if args.full else "Incremental"
    scope = f"year {args.year}" if args.year else (f"after {hwm}" if hwm else "all years")
    print(f"{mode} fetch for {args.agency}, {scope}...")
    print(f"  Service: {service_url}")
    print(f"  Filter: {where}")

    features = fetch_features(session, service_url, where=where, delay=args.delay)
    print(f"  Fetched {len(features)} features from ArcGIS.")

    if not features:
        print("  0 new permits.")
        return

    new_permits = [
        p for p in (map_feature_to_permit(f, field_map) for f in features)
        if p.get("permit_no")
    ]
    print(f"  Mapped {len(new_permits)} permits.")

    merged = dict(existing)
    new_count = 0
    updated_count = 0
    for p in new_permits:
        key = p["permit_no"]
        if key in merged:
            if p != merged[key]:
                updated_count += 1
        else:
            new_count += 1
        merged[key] = p

    total = write_permits_by_year(list(merged.values()), permits_dir)
    print(f"\nDone. {new_count} new, {updated_count} updated, {total} total written.")


def cmd_stats(args):
    """Show summary statistics for existing permit data."""
    permit_config = _load_permit_config(args.agency)
    permits_dir = agency_data_dir(args.agency) / "permits"
    existing = load_existing_permits(permits_dir)

    if not existing:
        print(f"No permit data for {args.agency}.")
        return

    permits = list(existing.values())
    dates = [p.get("applied", "") for p in permits if p.get("applied")]
    statuses = Counter(p.get("status", "") for p in permits)
    types = Counter(p.get("type", "") for p in permits)

    print(f"{args.agency}: {len(permits)} permits")
    print(f"  Date range: {min(dates)} to {max(dates)}")
    print(f"  Service: {permit_config['service_url']}")
    print(f"\n  By type:")
    for t, n in types.most_common():
        print(f"    {n:6d}  {t}")
    print(f"\n  By status:")
    for s, n in statuses.most_common(10):
        print(f"    {n:6d}  {s}")


def main():
    parser = argparse.ArgumentParser(description="ArcGIS Feature Service permit scraper")
    sub = parser.add_subparsers(dest="command")

    fetch_p = sub.add_parser("fetch", help="Fetch permits from ArcGIS")
    fetch_p.add_argument("--agency", default="carlsbad")
    fetch_p.add_argument("--year", type=int, default=None)
    fetch_p.add_argument("--full", action="store_true", help="Full rescan (ignore existing)")
    fetch_p.add_argument("--delay", type=float, default=0.5, help="Delay between pages")

    stats_p = sub.add_parser("stats", help="Show permit summary")
    stats_p.add_argument("--agency", default="carlsbad")

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
