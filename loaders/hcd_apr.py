#!/usr/bin/env python3
"""
Download and process HCD Annual Progress Report (APR) Table A data.

Downloads the statewide APR Table A from data.ca.gov, filters to Oceanside,
identifies downtown (D-District) projects via Oceanside GIS, and cross-
references with eTRAKit building permits by APN.

Usage:
    python hcd_apr.py download                # download + filter to Oceanside
    python hcd_apr.py enrich                  # tag downtown via GIS identify
    python hcd_apr.py crossref                # cross-reference with eTRAKit
    python hcd_apr.py stats                   # show filing summary
    python hcd_apr.py stats --downtown        # downtown-only summary

Data source: https://data.ca.gov/dataset/housing-element-annual-progress-report-apr-data-by-jurisdiction-and-year
Output: data/reference/hcd-apr-oceanside.json
"""

import argparse
import csv
import io
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
APR_FILE = REFERENCE_DIR / "hcd-apr-oceanside.json"
APN_ZONES_FILE = REFERENCE_DIR / "apn-zones.json"
PERMITS_DIR = DATA_DIR / "oceanside" / "permits"

APR_CSV_URL = (
    "https://data.ca.gov/dataset/81b0841f-2802-403e-b48e-2ef4b751f77c"
    "/resource/c78b769d-cc02-4050-91ef-79ded665b5a8/download/tablea.csv"
)

GIS_BASEMAP = "https://gis.oceansideca.org/gis/rest/services/Basemap_Landbase/MapServer"
GIS_PLANNING = "https://gis.oceansideca.org/gis/rest/services/WebService/Planning/MapServer"
UA = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"

INCOME_FIELDS = [
    "acutely_low_income_dr", "acutely_low_income_ndr",
    "extremely_low_income_dr", "extremely_low_income_ndr",
    "vlow_income_dr", "vlow_income_ndr",
    "low_income_dr", "low_income_ndr",
    "mod_income_dr", "mod_income_ndr",
]

APR_KEEP_FIELDS = [
    "YEAR", "PRIOR_APN", "APN", "STREET_ADDRESS", "PROJECT_NAME",
    "JURS_TRACKING_ID", "UNIT_CAT", "TENURE", "APP_SUBMIT_DT",
    "ACUTELY_LOW_INCOME_DR", "ACUTELY_LOW_INCOME_NDR",
    "EXTREMELY_LOW_INCOME_DR", "EXTREMELY_LOW_INCOME_NDR",
    "VLOW_INCOME_DR", "VLOW_INCOME_NDR",
    "LOW_INCOME_DR", "LOW_INCOME_NDR",
    "MOD_INCOME_DR", "MOD_INCOME_NDR",
    "ABOVE_MOD_INCOME",
    "TOT_PROPOSED_UNITS", "TOT_APPROVED_UNITS", "TOT_DISAPPROVED_UNITS",
    "APP_SUBMITTED_SB35", "DENSITY_BONUS_RECEIVED", "DENSITY_BONUS_APPROVED",
    "APPLICATION_STATUS", "PROJECT_TYPE", "NOTES",
    "LATITUDE", "LONGITUDE", "STD_ADDRESS",
]

INT_FIELDS = {
    "acutely_low_income_dr", "acutely_low_income_ndr",
    "extremely_low_income_dr", "extremely_low_income_ndr",
    "vlow_income_dr", "vlow_income_ndr",
    "low_income_dr", "low_income_ndr",
    "mod_income_dr", "mod_income_ndr",
    "above_mod_income",
    "tot_proposed_units", "tot_approved_units", "tot_disapproved_units",
}

FLOAT_FIELDS = {"latitude", "longitude"}


def download_apr():
    """Download APR Table A CSV, filter to Oceanside, save locally."""
    print("Downloading APR Table A from data.ca.gov...", flush=True)
    resp = requests.get(APR_CSV_URL, timeout=120, headers={
        "User-Agent": UA,
        "Accept": "text/csv,*/*",
    })
    resp.raise_for_status()
    print(f"  Downloaded {len(resp.content):,} bytes", flush=True)

    records = []
    total = 0
    reader = csv.DictReader(io.StringIO(resp.text))
    for row in reader:
        total += 1
        if row.get("JURIS_NAME", "").strip().upper() != "OCEANSIDE":
            continue

        record = {}
        for field in APR_KEEP_FIELDS:
            val = (row.get(field, "") or "").strip()
            key = field.lower()
            if key in INT_FIELDS:
                try:
                    record[key] = int(val) if val else 0
                except ValueError:
                    record[key] = 0
            elif key in FLOAT_FIELDS:
                try:
                    record[key] = float(val) if val else None
                except ValueError:
                    record[key] = None
            else:
                record[key] = val

        apn = record.get("apn", "").replace("-", "")
        record["apn"] = apn

        record["affordable_units"] = sum(record.get(f, 0) for f in INCOME_FIELDS)
        records.append(record)

    print(f"  {total:,} statewide → {len(records)} Oceanside", flush=True)

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    APR_FILE.write_text(json.dumps(records, indent=2))
    print(f"  Saved to {APR_FILE}")
    return records


def identify_zone_wgs84(session, lat, lon):
    """Identify zoning + land use at WGS84 lat/lon. Returns (zones, is_downtown)."""
    zones = []
    is_downtown = False
    try:
        resp = session.get(
            f"{GIS_PLANNING}/identify",
            params={
                "geometry": f"{lon},{lat}",
                "geometryType": "esriGeometryPoint",
                "sr": "4326",
                "layers": "all:6,8,9,10",
                "tolerance": "5",
                "mapExtent": f"{lon - 0.005},{lat - 0.005},{lon + 0.005},{lat + 0.005}",
                "imageDisplay": "100,100,96",
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=15,
        )
        for r in resp.json().get("results", []):
            attrs = r.get("attributes", {})
            code = attrs.get("Zoning Code Print", "")
            if code and code not in zones:
                zones.append(code)
            lu = attrs.get("Landuse_Code_Print", "")
            if lu == "DT":
                is_downtown = True
    except Exception as e:
        print(f"  GIS identify error ({lat},{lon}): {e}", flush=True)
    if not is_downtown:
        is_downtown = any(z.startswith("D-") for z in zones)
    return zones, is_downtown


def _query_parcel_centroid(session, apn):
    """Query GIS basemap for parcel centroid by APN. Returns (cx, cy) in State Plane 2230 or None."""
    try:
        resp = session.get(
            f"{GIS_BASEMAP}/2/query",
            params={
                "where": f"APN = '{apn}'",
                "outFields": "APN",
                "returnGeometry": "true",
                "outSR": "2230",
                "f": "json",
            },
            timeout=30,
        )
        features = resp.json().get("features", [])
        if features:
            ring = features[0]["geometry"]["rings"][0]
            cx = sum(p[0] for p in ring) / len(ring)
            cy = sum(p[1] for p in ring) / len(ring)
            return (cx, cy)
    except Exception as e:
        print(f"  Parcel query error for {apn}: {e}", flush=True)
    return None


def _identify_zone_sp(session, cx, cy):
    """Identify zoning + land use at State Plane 2230 point. Returns (zones, is_downtown)."""
    zones = []
    is_downtown = False
    try:
        resp = session.get(
            f"{GIS_PLANNING}/identify",
            params={
                "geometry": f"{cx},{cy}",
                "geometryType": "esriGeometryPoint",
                "sr": "2230",
                "layers": "all:6,8,9,10",
                "tolerance": "1",
                "mapExtent": f"{cx - 1000},{cy - 1000},{cx + 1000},{cy + 1000}",
                "imageDisplay": "100,100,96",
                "returnGeometry": "false",
                "f": "json",
            },
            timeout=15,
        )
        for r in resp.json().get("results", []):
            attrs = r.get("attributes", {})
            code = attrs.get("Zoning Code Print", "")
            if code and code not in zones:
                zones.append(code)
            lu = attrs.get("Landuse_Code_Print", "")
            if lu == "DT":
                is_downtown = True
    except Exception as e:
        print(f"  SP identify error: {e}", flush=True)
    if not is_downtown:
        is_downtown = any(z.startswith("D-") for z in zones)
    return zones, is_downtown


def enrich_downtown():
    """Tag APR records as downtown using GIS zoning identify + APN zone cache.

    Three-pass strategy:
    1. APN cache hit → use cached zones
    2. APN → GIS parcel centroid → zone identify (most accurate)
    3. Lat/lon → zone identify (fallback for APNs not in GIS)
    """
    if not APR_FILE.exists():
        print("No APR data. Run 'download' first.")
        return

    records = json.loads(APR_FILE.read_text())

    apn_cache = {}
    if APN_ZONES_FILE.exists():
        apn_cache = json.loads(APN_ZONES_FILE.read_text())

    # Pass 1: Cache hits
    need_apn_lookup = []
    need_latlon_lookup = []
    cached = 0
    for rec in records:
        apn = rec.get("apn", "")
        if apn and apn in apn_cache:
            entry = apn_cache[apn]
            rec["zones"] = entry.get("zones", [])
            rec["is_downtown"] = entry.get("is_downtown", False)
            cached += 1
        elif apn:
            need_apn_lookup.append(rec)
        else:
            lat, lon = rec.get("latitude"), rec.get("longitude")
            if lat and lon:
                need_latlon_lookup.append(rec)
            else:
                rec["zones"] = []
                rec["is_downtown"] = False

    # Deduplicate APNs
    unique_apns = list(dict.fromkeys(r["apn"] for r in need_apn_lookup))
    print(f"  {len(records)} records: {cached} cached, {len(unique_apns)} APNs to query, {len(need_latlon_lookup)} lat/lon fallback", flush=True)

    session = requests.Session()
    session.headers["User-Agent"] = UA

    # Pass 2: APN → parcel centroid → zone identify
    apn_results = {}
    for i, apn in enumerate(unique_apns):
        centroid = _query_parcel_centroid(session, apn)
        if centroid:
            cx, cy = centroid
            zones, is_downtown = _identify_zone_sp(session, cx, cy)
            apn_results[apn] = {"zones": zones, "is_downtown": is_downtown, "centroid": [cx, cy],
                                "fetched": time.strftime("%Y-%m-%d")}
            apn_cache[apn] = apn_results[apn]
        else:
            apn_results[apn] = {"zones": [], "is_downtown": False, "centroid": None,
                                "fetched": time.strftime("%Y-%m-%d"), "error": "parcel_not_found"}
            apn_cache[apn] = apn_results[apn]

        if (i + 1) % 25 == 0 or i == len(unique_apns) - 1:
            dt = sum(1 for v in apn_results.values() if v.get("is_downtown"))
            print(f"  ...APN {i + 1}/{len(unique_apns)} ({dt} downtown)", flush=True)
        time.sleep(0.4)

    # Apply APN results; if parcel_not_found, try lat/lon instead
    for rec in need_apn_lookup:
        result = apn_results.get(rec["apn"], {})
        if result.get("error") == "parcel_not_found" and rec.get("latitude") and rec.get("longitude"):
            need_latlon_lookup.append(rec)
        else:
            rec["zones"] = result.get("zones", [])
            rec["is_downtown"] = result.get("is_downtown", False)

    # Pass 3: Lat/lon fallback (deduped by coordinate)
    seen_coords = {}
    unique_ll = []
    for rec in need_latlon_lookup:
        lat, lon = rec.get("latitude"), rec.get("longitude")
        if not lat or not lon or (lat == 0 and lon == 0):
            rec["zones"] = []
            rec["is_downtown"] = False
            continue
        key = f"{lat:.6f},{lon:.6f}"
        if key not in seen_coords:
            seen_coords[key] = None
            unique_ll.append(rec)

    if unique_ll:
        print(f"  {len(unique_ll)} unique lat/lon fallbacks", flush=True)

    for i, rec in enumerate(unique_ll):
        key = f"{rec['latitude']:.6f},{rec['longitude']:.6f}"
        zones, is_downtown = identify_zone_wgs84(session, rec["latitude"], rec["longitude"])
        seen_coords[key] = {"zones": zones, "is_downtown": is_downtown}
        if (i + 1) % 25 == 0 or i == len(unique_ll) - 1:
            dt = sum(1 for v in seen_coords.values() if v and v.get("is_downtown"))
            print(f"  ...lat/lon {i + 1}/{len(unique_ll)} ({dt} downtown)", flush=True)
        time.sleep(0.3)

    for rec in need_latlon_lookup:
        lat, lon = rec.get("latitude"), rec.get("longitude")
        if not lat or not lon:
            continue
        key = f"{lat:.6f},{lon:.6f}"
        result = seen_coords.get(key, {})
        if result:
            rec["zones"] = result.get("zones", [])
            rec["is_downtown"] = result.get("is_downtown", False)

    # Save updated APN cache
    from pathlib import Path as _P
    _P(APN_ZONES_FILE).parent.mkdir(parents=True, exist_ok=True)
    _P(APN_ZONES_FILE).write_text(json.dumps(apn_cache, indent=2, sort_keys=True))
    print(f"  APN cache updated: {len(apn_cache)} entries", flush=True)

    APR_FILE.write_text(json.dumps(records, indent=2))

    downtown = sum(1 for r in records if r.get("is_downtown"))
    print(f"  Enriched: {downtown} downtown, {len(records) - downtown} non-downtown")
    return records


def _normalize_addr(addr):
    """Normalize address for matching."""
    if not addr:
        return ""
    addr = addr.upper().strip()
    import re as _re
    addr = _re.sub(r"\s+", " ", addr)
    addr = _re.sub(r"\bSTREET\b", "ST", addr)
    addr = _re.sub(r"\bAVENUE\b", "AVE", addr)
    addr = _re.sub(r"\bBOULEVARD\b", "BLVD", addr)
    addr = _re.sub(r"\bDRIVE\b", "DR", addr)
    addr = _re.sub(r"\bLANE\b", "LN", addr)
    addr = _re.sub(r"\bCOURT\b", "CT", addr)
    addr = _re.sub(r"\bROAD\b", "RD", addr)
    addr = _re.sub(r"\bHIGHWAY\b", "HWY", addr)
    addr = _re.sub(r"\bNORTH\b", "N", addr)
    addr = _re.sub(r"\bSOUTH\b", "S", addr)
    addr = _re.sub(r"\bEAST\b", "E", addr)
    addr = _re.sub(r"\bWEST\b", "W", addr)
    addr = _re.sub(r"[#,.]", "", addr)
    return addr.strip()


def _extract_street_number(addr):
    """Extract leading street number."""
    import re as _re
    m = _re.match(r"(\d+)", addr)
    return m.group(1) if m else ""


def _make_permit_summary(p):
    return {
        "permit_no": p.get("permit_no", ""),
        "type": p.get("type", ""),
        "description": (p.get("description", "") or "")[:120],
        "status": p.get("status", ""),
        "applied": p.get("applied", ""),
        "issued": p.get("issued", ""),
    }


def crossref_etrakit():
    """Cross-reference APR records with eTRAKit permits using multiple strategies."""
    if not APR_FILE.exists():
        print("No APR data. Run 'download' first.")
        return

    records = json.loads(APR_FILE.read_text())

    # Load all eTRAKit permits
    all_permits = []
    etrakit_by_apn = defaultdict(list)
    for f in sorted(PERMITS_DIR.glob("etrakit-permits-*.jsonl")):
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            p = json.loads(line)
            all_permits.append(p)
            apn = p.get("apn", "")
            if apn:
                etrakit_by_apn[apn].append(p)

    print(f"  eTRAKit: {len(all_permits)} total permits, {len(etrakit_by_apn)} with APNs", flush=True)

    # Build description index: extract street numbers + street names from descriptions
    # Many eTRAKit descriptions contain addresses like "BLDG 4877 UNITS..." or "519 TREMONT..."
    import re as _re
    desc_addr_re = _re.compile(r'\b(\d{2,5})\s+([NSEW]\.?\s+)?([A-Z]{3,}\s+(?:ST|AVE|BLVD|DR|LN|CT|RD|HWY|WAY|PL|CIR))', _re.I)

    etrakit_by_addr = defaultdict(list)
    for p in all_permits:
        desc = (p.get("description", "") or "").upper()
        for m in desc_addr_re.finditer(desc):
            num = m.group(1)
            street = _normalize_addr(m.group(0))
            key = f"{num}|{street}"
            etrakit_by_addr[key].append(p)

    # Build owner index for fallback matching
    etrakit_by_owner = defaultdict(list)
    for p in all_permits:
        owner = (p.get("owner", "") or "").upper().strip()
        if owner and len(owner) > 5:
            etrakit_by_owner[owner].append(p)

    # Strategy stats
    by_apn = 0
    by_addr = 0
    by_owner = 0
    unmatched = 0

    for rec in records:
        apn = rec.get("apn", "")
        matched_permits = []
        match_method = None

        # Strategy 1: Direct APN match
        if apn and apn in etrakit_by_apn:
            matched_permits = etrakit_by_apn[apn]
            match_method = "apn"

        # Strategy 2: Address in eTRAKit descriptions
        if not matched_permits:
            apr_addr = _normalize_addr(rec.get("street_address", "") or rec.get("std_address", ""))
            apr_num = _extract_street_number(apr_addr)
            if apr_num and apr_addr:
                key = f"{apr_num}|{apr_addr}"
                if key in etrakit_by_addr:
                    matched_permits = etrakit_by_addr[key]
                    match_method = "addr_in_desc"
                else:
                    # Try matching just number + any street word overlap
                    for dk, dp in etrakit_by_addr.items():
                        dk_num = dk.split("|")[0]
                        if dk_num == apr_num:
                            dk_addr = dk.split("|", 1)[1] if "|" in dk else ""
                            apr_words = set(apr_addr.split())
                            dk_words = set(dk_addr.split())
                            if len(apr_words & dk_words) >= 2:
                                matched_permits = dp
                                match_method = "addr_fuzzy"
                                break

        # Strategy 3: Owner name match (only for permits with same APN prefix)
        # Skip — too many false positives without address confirmation

        if matched_permits:
            # Filter to housing-related permits only
            housing_types = {"BLD MULTI FAMILY", "BLD SINGLE FAMILY", "BLD ACCESSORY DWELLING",
                             "BLD NEW RESIDENTIAL", "BLD ROOM ADDITION", "BLD MOBILE HOME",
                             "BLD DUPLEX", "BLD CONDO", "BLD APARTMENT", "BLD TOWNHOUSE"}
            housing_permits = [p for p in matched_permits
                              if p.get("type", "").upper() in housing_types
                              or any(kw in (p.get("description", "") or "").upper()
                                     for kw in ["UNIT", "DWELLING", "ADU", "SFR", "DUPLEX",
                                                "TRIPLEX", "CONDO", "APARTMENT", "TOWNHOME"])]
            if not housing_permits:
                housing_permits = matched_permits

            rec["etrakit_permits"] = [_make_permit_summary(p) for p in housing_permits[:20]]
            rec["has_building_permit"] = True
            rec["match_method"] = match_method
            if match_method == "apn":
                by_apn += 1
            elif match_method in ("addr_in_desc", "addr_fuzzy"):
                by_addr += 1
        else:
            rec["etrakit_permits"] = []
            rec["has_building_permit"] = False
            rec["match_method"] = None
            unmatched += 1

    total = len(records)
    matched_total = by_apn + by_addr
    print(f"  Cross-referenced: {matched_total}/{total} ({100 * matched_total / total:.1f}%)")
    print(f"    By APN: {by_apn}")
    print(f"    By address: {by_addr}")
    print(f"    Unmatched: {unmatched}")

    APR_FILE.write_text(json.dumps(records, indent=2))
    return records


def cmd_stats(args):
    """Show APR filing statistics."""
    if not APR_FILE.exists():
        print("No APR data. Run 'download' first.")
        return

    records = json.loads(APR_FILE.read_text())
    downtown_only = getattr(args, "downtown", False)

    if downtown_only:
        records = [r for r in records if r.get("is_downtown")]
        print(f"=== Downtown (D-District) Filings ===\n")
    else:
        print(f"=== All Oceanside APR Filings ===\n")

    by_year = defaultdict(lambda: {
        "proposed": 0, "approved": 0, "disapproved": 0,
        "affordable": 0, "above_mod": 0,
        "with_permit": 0, "rows": 0,
        "by_cat": Counter(), "by_type": Counter(),
    })

    for rec in records:
        year = rec.get("year", "")
        d = by_year[year]
        d["proposed"] += rec.get("tot_proposed_units", 0)
        d["approved"] += rec.get("tot_approved_units", 0)
        d["disapproved"] += rec.get("tot_disapproved_units", 0)
        d["affordable"] += rec.get("affordable_units", 0)
        d["above_mod"] += rec.get("above_mod_income", 0)
        d["with_permit"] += 1 if rec.get("has_building_permit") else 0
        d["rows"] += 1
        d["by_cat"][rec.get("unit_cat", "")] += rec.get("tot_proposed_units", 0)
        d["by_type"][rec.get("project_type", "")] += rec.get("tot_proposed_units", 0)

    print(f"{'Year':<6} {'Filed':>8} {'Appr':>8} {'Deny':>8} {'Afford':>8} {'Mkt':>8} {'w/Permit':>9} {'Rows':>6}")
    print("-" * 70)
    for y in sorted(by_year):
        d = by_year[y]
        print(f"{y:<6} {d['proposed']:>8} {d['approved']:>8} {d['disapproved']:>8} "
              f"{d['affordable']:>8} {d['above_mod']:>8} {d['with_permit']:>9} {d['rows']:>6}")

    print(f"\n{'Year':<6}  Unit categories (proposed)")
    print("-" * 60)
    for y in sorted(by_year):
        cats = ", ".join(f"{cat}={n}" for cat, n in by_year[y]["by_cat"].most_common() if n > 0)
        print(f"{y:<6}  {cats}")

    if not downtown_only:
        dt_records = [r for r in records if r.get("is_downtown")]
        if dt_records:
            dt_total = sum(r.get("tot_proposed_units", 0) for r in dt_records)
            all_total = sum(r.get("tot_proposed_units", 0) for r in records)
            print(f"\nDowntown share: {dt_total:,} / {all_total:,} = {100 * dt_total / all_total:.1f}%")

    # Show large projects
    big = sorted(records, key=lambda r: r.get("tot_proposed_units", 0), reverse=True)[:10]
    print(f"\nTop 10 projects by proposed units:")
    for r in big:
        dt = " [DT]" if r.get("is_downtown") else ""
        bp = " +BP" if r.get("has_building_permit") else ""
        print(f"  {r.get('year','')} {r.get('tot_proposed_units',0):>5}u  "
              f"{r.get('project_name','') or r.get('street_address','')}{dt}{bp}")


def main():
    parser = argparse.ArgumentParser(description="HCD APR Table A loader")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("download", help="Download + filter APR Table A to Oceanside")
    sub.add_parser("enrich", help="Tag downtown via GIS identify")
    sub.add_parser("crossref", help="Cross-reference with eTRAKit permits")

    stats_p = sub.add_parser("stats", help="Show filing statistics")
    stats_p.add_argument("--downtown", action="store_true", help="Downtown only")

    args = parser.parse_args()

    if args.command == "download":
        download_apr()
    elif args.command == "enrich":
        enrich_downtown()
    elif args.command == "crossref":
        crossref_etrakit()
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
