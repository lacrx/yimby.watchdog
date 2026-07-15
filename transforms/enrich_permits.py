#!/usr/bin/env python3
"""
Enrich building permits with zoning data from Oceanside GIS.

For each permit with an APN, queries the GIS Basemap Parcels layer to get
the parcel centroid, then identifies the zoning via the Planning MapServer.
Results are cached in data/reference/apn-zones.json to avoid re-querying.

Usage:
    python enrich_permits.py                   # enrich current year
    python enrich_permits.py --year 2025       # specific year
    python enrich_permits.py --all-years       # all years
    python enrich_permits.py --refresh         # re-query GIS for cached APNs
    python enrich_permits.py --stats           # show enrichment coverage

Output: zone_code + is_downtown written back to permit JSONL files.
        APN→zone cache at data/reference/apn-zones.json.
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import load_agencies, agency_data_dir

DATA_DIR = REPO_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
APN_ZONES_FILE = REFERENCE_DIR / "apn-zones.json"

GIS_BASEMAP = "https://gis.oceansideca.org/gis/rest/services/Basemap_Landbase/MapServer"
GIS_PLANNING = "https://gis.oceansideca.org/gis/rest/services/WebService/Planning/MapServer"
UA = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"


def load_apn_cache():
    """Load cached APN→zone mappings."""
    if APN_ZONES_FILE.exists():
        return json.loads(APN_ZONES_FILE.read_text())
    return {}


def save_apn_cache(cache):
    """Save APN→zone cache."""
    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    APN_ZONES_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))


def query_parcel_centroids(session, apns):
    """Batch query Basemap Parcels by APN, return dict of APN→(cx, cy) in State Plane 2230."""
    centroids = {}
    batch_size = 20
    for i in range(0, len(apns), batch_size):
        batch = apns[i:i + batch_size]
        in_clause = ",".join(f"'{a}'" for a in batch)
        try:
            resp = session.get(
                f"{GIS_BASEMAP}/2/query",
                params={
                    "where": f"APN IN ({in_clause})",
                    "outFields": "APN",
                    "returnGeometry": "true",
                    "outSR": "2230",
                    "f": "json",
                },
                timeout=30,
            )
            data = resp.json()
            for feat in data.get("features", []):
                apn = feat["attributes"]["apn"] if "apn" in feat["attributes"] else feat["attributes"].get("APN", "")
                ring = feat["geometry"]["rings"][0]
                cx = sum(p[0] for p in ring) / len(ring)
                cy = sum(p[1] for p in ring) / len(ring)
                centroids[apn] = (cx, cy)
        except Exception as e:
            print(f"  Parcel batch error at {i}: {e}", flush=True)
        time.sleep(0.5)
    return centroids


def identify_zone(session, cx, cy):
    """Identify zoning at a State Plane 2230 point. Returns list of zone codes."""
    try:
        resp = session.get(
            f"{GIS_PLANNING}/identify",
            params={
                "geometry": f"{cx},{cy}",
                "geometryType": "esriGeometryPoint",
                "sr": "2230",
                "layers": "all:6,8,9",
                "tolerance": "1",
                "mapExtent": f"{cx - 1000},{cy - 1000},{cx + 1000},{cy + 1000}",
                "imageDisplay": "100,100,96",
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=15,
        )
        results = resp.json().get("results", [])
        zones = []
        for r in results:
            code = r.get("attributes", {}).get("Zoning Code Print", "")
            if code:
                zones.append(code)
        return zones
    except Exception as e:
        print(f"  Identify error at ({cx:.0f}, {cy:.0f}): {e}", flush=True)
        return []


def resolve_apn_zones(apns, cache, session, refresh=False):
    """Resolve zone codes for a list of APNs, using cache where possible."""
    to_query = []
    for apn in apns:
        if refresh or apn not in cache:
            to_query.append(apn)

    if not to_query:
        return cache

    print(f"  Querying GIS for {len(to_query)} APNs...", flush=True)

    centroids = query_parcel_centroids(session, to_query)
    print(f"  Got centroids for {len(centroids)}/{len(to_query)} APNs", flush=True)

    resolved = 0
    for j, (apn, (cx, cy)) in enumerate(centroids.items()):
        zones = identify_zone(session, cx, cy)
        is_downtown = any(z.startswith("D-") for z in zones)
        cache[apn] = {
            "zones": zones,
            "is_downtown": is_downtown,
            "centroid": [cx, cy],
            "fetched": datetime.now().strftime("%Y-%m-%d"),
        }
        resolved += 1

        if (j + 1) % 20 == 0:
            print(f"  ...{j + 1}/{len(centroids)} identified ({sum(1 for a in cache if cache[a].get('is_downtown'))} downtown)", flush=True)
        time.sleep(0.3)

    missing = [a for a in to_query if a not in centroids]
    for apn in missing:
        cache[apn] = {"zones": [], "is_downtown": False, "centroid": None, "fetched": datetime.now().strftime("%Y-%m-%d"), "error": "parcel_not_found"}

    print(f"  Resolved {resolved} APNs, {len(missing)} not found in GIS", flush=True)
    return cache


def load_permits(year):
    """Load permits for a given year from all agencies with permit config."""
    agencies = load_agencies(enabled_only=False)
    for slug, cfg in agencies.items():
        if "permits" not in cfg:
            continue
        permits_dir = agency_data_dir(slug) / "permits"
        pf = permits_dir / f"etrakit-permits-{year}.jsonl"
        if pf.exists():
            permits = []
            with open(pf) as f:
                for line in f:
                    if line.strip():
                        permits.append(json.loads(line))
            return permits, pf
    return [], None


def enrich_year(year, cache, session, refresh=False):
    """Enrich permits for a single year with zone data."""
    permits, pf = load_permits(year)
    if not permits:
        print(f"{year}: no permit data found")
        return 0

    apns = sorted(set(p.get("apn", "") for p in permits if p.get("apn")))
    print(f"{year}: {len(permits)} permits, {len(apns)} unique APNs", flush=True)

    if not apns:
        print(f"  No APNs to enrich")
        return 0

    cache = resolve_apn_zones(apns, cache, session, refresh)
    save_apn_cache(cache)

    enriched = 0
    for p in permits:
        apn = p.get("apn", "")
        if apn and apn in cache:
            entry = cache[apn]
            zones = entry.get("zones", [])
            d_zones = [z for z in zones if z.startswith("D-")]
            p["zone_code"] = d_zones[0] if d_zones else (zones[0] if zones else "")
            p["is_downtown"] = entry.get("is_downtown", False)
            enriched += 1

    with open(pf, "w") as f:
        for p in permits:
            f.write(json.dumps(p) + "\n")

    downtown = sum(1 for p in permits if p.get("is_downtown"))
    print(f"  Enriched {enriched} permits with zone data ({downtown} downtown)")
    return enriched


def cmd_enrich(args):
    """Enrich permits with GIS zoning data."""
    cache = load_apn_cache()
    session = requests.Session()
    session.headers["User-Agent"] = UA

    if args.all_years:
        years = list(range(2020, datetime.now().year + 1))
    else:
        years = [args.year]

    total = 0
    for year in years:
        total += enrich_year(year, cache, session, args.refresh)

    print(f"\nTotal: {total} permits enriched across {len(years)} year(s)")
    print(f"APN cache: {len(cache)} entries → {APN_ZONES_FILE}")


def cmd_stats(args):
    """Show enrichment coverage statistics."""
    cache = load_apn_cache()
    print(f"APN cache: {len(cache)} entries")
    downtown = sum(1 for v in cache.values() if v.get("is_downtown"))
    errors = sum(1 for v in cache.values() if v.get("error"))
    print(f"  Downtown: {downtown}, Non-downtown: {len(cache) - downtown - errors}, Errors: {errors}")

    print(f"\nBy year:")
    for year in range(2020, datetime.now().year + 1):
        permits, _ = load_permits(year)
        if not permits:
            continue
        total = len(permits)
        with_apn = sum(1 for p in permits if p.get("apn"))
        with_zone = sum(1 for p in permits if p.get("zone_code"))
        dt = sum(1 for p in permits if p.get("is_downtown"))
        print(f"  {year}: {total} permits, {with_apn} APNs, {with_zone} zoned, {dt} downtown")

    if cache:
        zone_counts = Counter()
        for v in cache.values():
            for z in v.get("zones", []):
                zone_counts[z] += 1
        print(f"\nTop zones:")
        for z, c in zone_counts.most_common(15):
            prefix = "*** " if z.startswith("D-") else "    "
            print(f"  {prefix}{z}: {c}")


def main():
    parser = argparse.ArgumentParser(description="Enrich permits with GIS zoning data")
    sub = parser.add_subparsers(dest="command")

    enrich_p = sub.add_parser("enrich", help="Enrich permits with zone data")
    enrich_p.add_argument("--year", type=int, default=datetime.now().year)
    enrich_p.add_argument("--all-years", action="store_true")
    enrich_p.add_argument("--refresh", action="store_true", help="Re-query GIS for cached APNs")

    stats_p = sub.add_parser("stats", help="Show enrichment coverage")

    args = parser.parse_args()

    if args.command == "enrich":
        cmd_enrich(args)
    elif args.command == "stats":
        cmd_stats(args)
    else:
        # Default: enrich current year
        args.year = datetime.now().year
        args.all_years = False
        args.refresh = False
        cmd_enrich(args)


if __name__ == "__main__":
    main()
