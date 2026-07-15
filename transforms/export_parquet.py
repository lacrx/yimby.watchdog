#!/usr/bin/env python3
"""
Export enriched permits and projects as Parquet files for DuckDB queries.

Loads permit JSONL files, joins with project data, and exports Parquet files
compatible with stoside.data's DuckDB Lambda + API Gateway stack.

Usage:
    python export_parquet.py                   # export + upload
    python export_parquet.py --dry-run         # show schema + counts, no upload
    python export_parquet.py --local-only      # export to data/exports/, no S3

Output:
    data/exports/permits.parquet
    data/exports/permit_projects.parquet
    Uploaded to stoside-data S3 bucket under data/ prefix.
"""

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import load_agencies, agency_data_dir

DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
EXPORTS_DIR = DATA_DIR / "exports"
REFERENCE_DIR = DATA_DIR / "reference"
PROJECTS_FILE = STRUCTURED_DIR / "permit-projects.json"
HOUSING_FILE = STRUCTURED_DIR / "housing-projects.json"
APR_FILE = REFERENCE_DIR / "hcd-apr-oceanside.json"
STR_FILE = REFERENCE_DIR / "str-licenses.json"

STOSIDE_BUCKET = os.environ.get("STOSIDE_BUCKET", "stoside-data")
STOSIDE_PROFILE = os.environ.get("AWS_PROFILE", "civic")
STOSIDE_REGION = "us-east-1"
PARCELS_CACHE = EXPORTS_DIR / "parcels.parquet"
ZONING_FILE = REFERENCE_DIR / "parcel-zoning.json"


def load_parcel_addresses():
    """Download parcels.parquet from S3 (cached) and build APN→address index."""
    import pyarrow.parquet as pq

    if not PARCELS_CACHE.exists():
        print("  Downloading parcels.parquet from S3...", flush=True)
        import boto3
        session = boto3.Session(profile_name=STOSIDE_PROFILE, region_name=STOSIDE_REGION)
        s3 = session.client("s3")
        PARCELS_CACHE.parent.mkdir(parents=True, exist_ok=True)
        s3.download_file(STOSIDE_BUCKET, "data/parcels.parquet", str(PARCELS_CACHE))

    table = pq.read_table(PARCELS_CACHE, columns=["apn", "address", "street"])
    apns = table.column("apn").to_pylist()
    nums = table.column("address").to_pylist()
    streets = table.column("street").to_pylist()
    index = {}
    for apn, num, st in zip(apns, nums, streets):
        if apn and num and st:
            index[apn] = f"{num} {st}"
    print(f"  Parcel address index: {len(index)} APNs with addresses")
    return index


_DENSITY_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:dwelling|du)", re.IGNORECASE)


def _parse_density(val):
    """Extract numeric density (du/acre) from string or number. Returns float or None."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return None if math.isnan(val) else float(val)
    m = _DENSITY_RE.search(str(val))
    return float(m.group(1)) if m else None


def load_parcel_zoning():
    """Load APN→zone_code index from parcel-zoning.json."""
    if not ZONING_FILE.exists():
        return {}
    data = json.loads(ZONING_FILE.read_text())
    print(f"  Parcel zoning index: {len(data)} APNs with zone codes")
    return data


def parse_date(date_str):
    """Parse M/D/YYYY to YYYY-MM-DD. Returns None on failure."""
    if not date_str:
        return None
    if "/" in date_str:
        parts = date_str.split("/")
        if len(parts) == 3:
            try:
                return f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"
            except ValueError:
                return None
    return date_str if re.match(r"\d{4}-\d{2}-\d{2}", date_str) else None


def load_all_permits():
    """Load all permits across all years and agencies."""
    agencies = load_agencies(enabled_only=False)
    all_permits = []
    for slug, cfg in agencies.items():
        if "permits" not in cfg:
            continue
        permits_dir = agency_data_dir(slug) / "permits"
        if not permits_dir.exists():
            continue
        for pf in sorted(permits_dir.glob("etrakit-permits-*.jsonl")):
            year_match = re.search(r"(\d{4})", pf.name)
            year = int(year_match.group(1)) if year_match else 0
            with open(pf) as f:
                for line in f:
                    if line.strip():
                        p = json.loads(line)
                        p["_year"] = year
                        p["_agency"] = slug
                        all_permits.append(p)
    return all_permits


def load_projects():
    """Load permit projects and build permit_no→project_id index."""
    if not PROJECTS_FILE.exists():
        return {}, []
    data = json.loads(PROJECTS_FILE.read_text())
    projects = data.get("projects", [])
    index = {}
    for proj in projects:
        for pno in proj.get("permits", []):
            index[pno] = proj["project_id"]
    return index, projects


def build_permits_table(permits, project_index, parcel_addrs, parcel_zones):
    """Build column-oriented dict for permits Parquet table."""
    columns = {
        "permit_no": [],
        "year": [],
        "agency": [],
        "type": [],
        "subtype": [],
        "description": [],
        "status": [],
        "applied": [],
        "approved": [],
        "issued": [],
        "apn": [],
        "owner": [],
        "address": [],
        "zone_code": [],
        "zone_category": [],
        "max_density": [],
        "max_height": [],
        "is_downtown": [],
        "project_id": [],
    }

    addr_resolved = 0
    zone_resolved = 0
    for p in permits:
        columns["permit_no"].append(p.get("permit_no", ""))
        columns["year"].append(p.get("_year", 0))
        columns["agency"].append(p.get("_agency", ""))
        columns["type"].append(p.get("type", ""))
        columns["subtype"].append(p.get("subtype", ""))
        columns["description"].append(p.get("description", ""))
        columns["status"].append(p.get("status", ""))
        columns["applied"].append(parse_date(p.get("applied", "")))
        columns["approved"].append(parse_date(p.get("approved", "")))
        columns["issued"].append(parse_date(p.get("issued", "")))
        apn = p.get("apn", "") or ""
        columns["apn"].append(apn)
        columns["owner"].append(p.get("owner", "") or "")
        addr = p.get("address", "") or ""
        if not addr and apn and apn in parcel_addrs:
            addr = parcel_addrs[apn]
            addr_resolved += 1
        columns["address"].append(addr)
        zone = p.get("zone_code", "") or ""
        zone_cat = ""
        if apn and apn in parcel_zones:
            pz = parcel_zones[apn]
            if not zone:
                zone = pz.get("zone_code", "")
                zone_resolved += 1
            zone_cat = pz.get("zone_category", "")
        columns["zone_code"].append(zone)
        columns["zone_category"].append(zone_cat)
        density = None
        height = ""
        if apn and apn in parcel_zones:
            pz = parcel_zones[apn]
            density = _parse_density(pz.get("density"))
            h = pz.get("height")
            if h and not (isinstance(h, float) and math.isnan(h)):
                height = str(h)
        columns["max_density"].append(density)
        columns["max_height"].append(height)
        columns["is_downtown"].append(bool(p.get("is_downtown")))
        columns["project_id"].append(project_index.get(p.get("permit_no", ""), ""))

    if addr_resolved:
        print(f"  Permits: {addr_resolved} addresses resolved via parcel APN join")
    if zone_resolved:
        print(f"  Permits: {zone_resolved} zone codes resolved via parcel zoning join")
    return columns


def build_projects_table(projects):
    """Build column-oriented dict for projects Parquet table."""
    columns = {
        "project_id": [],
        "project_name": [],
        "source": [],
        "permit_count": [],
        "estimated_units": [],
        "apn": [],
        "owner": [],
        "address": [],
        "first_applied": [],
        "last_applied": [],
        "entitlement_refs": [],
    }

    for proj in projects:
        columns["project_id"].append(proj.get("project_id", ""))
        columns["project_name"].append(proj.get("project_name", ""))
        columns["source"].append(proj.get("source", ""))
        columns["permit_count"].append(proj.get("permit_count", 0))
        columns["estimated_units"].append(proj.get("estimated_units", 0))
        columns["apn"].append(proj.get("apn", "") or "")
        columns["owner"].append(proj.get("owner", "") or "")
        columns["address"].append(proj.get("address", "") or "")
        dr = proj.get("date_range", {})
        columns["first_applied"].append(parse_date(dr.get("first_applied", "")))
        columns["last_applied"].append(parse_date(dr.get("last_applied", "")))
        refs = proj.get("entitlement_refs", [])
        columns["entitlement_refs"].append(json.dumps(refs) if refs else "")

    return columns


def build_apr_table():
    """Build column-oriented dict for APR filings Parquet table."""
    if not APR_FILE.exists():
        return None

    records = json.loads(APR_FILE.read_text())
    columns = {
        "year": [], "apn": [], "street_address": [], "project_name": [],
        "tracking_id": [], "unit_cat": [], "tenure": [],
        "app_submit_dt": [], "application_status": [], "project_type": [],
        "tot_proposed_units": [], "tot_approved_units": [], "tot_disapproved_units": [],
        "above_mod_income": [], "affordable_units": [],
        "density_bonus": [], "sb35": [],
        "latitude": [], "longitude": [],
        "is_downtown": [], "zones": [],
        "has_building_permit": [], "match_method": [],
    }

    for r in records:
        columns["year"].append(int(r.get("year", 0) or 0))
        columns["apn"].append(r.get("apn", ""))
        columns["street_address"].append(r.get("street_address", "") or r.get("std_address", ""))
        columns["project_name"].append(r.get("project_name", ""))
        columns["tracking_id"].append(r.get("jurs_tracking_id", ""))
        columns["unit_cat"].append(r.get("unit_cat", ""))
        columns["tenure"].append(r.get("tenure", ""))
        columns["app_submit_dt"].append(r.get("app_submit_dt", ""))
        columns["application_status"].append(r.get("application_status", ""))
        columns["project_type"].append(r.get("project_type", ""))
        columns["tot_proposed_units"].append(r.get("tot_proposed_units", 0))
        columns["tot_approved_units"].append(r.get("tot_approved_units", 0))
        columns["tot_disapproved_units"].append(r.get("tot_disapproved_units", 0))
        columns["above_mod_income"].append(r.get("above_mod_income", 0))
        columns["affordable_units"].append(r.get("affordable_units", 0))
        columns["density_bonus"].append(r.get("density_bonus_received", ""))
        columns["sb35"].append(r.get("app_submitted_sb35", ""))
        columns["latitude"].append(r.get("latitude") or 0.0)
        columns["longitude"].append(r.get("longitude") or 0.0)
        columns["is_downtown"].append(bool(r.get("is_downtown")))
        columns["zones"].append(",".join(r.get("zones", [])))
        columns["has_building_permit"].append(bool(r.get("has_building_permit")))
        columns["match_method"].append(r.get("match_method", "") or "")

    return columns


def build_housing_projects_table(parcel_addrs=None, parcel_zones=None):
    """Build column-oriented dict for unified housing projects Parquet table."""
    if not HOUSING_FILE.exists():
        return None

    parcel_addrs = parcel_addrs or {}
    parcel_zones = parcel_zones or {}
    data = json.loads(HOUSING_FILE.read_text())
    projects = data.get("projects", [])
    columns = {
        "project_id": [], "project_name": [], "agency": [],
        "address": [], "apn": [], "latitude": [], "longitude": [],
        "is_downtown": [], "zone_code": [], "zone_category": [],
        "max_density": [], "max_height": [],
        "units_best": [], "units_source": [],
        "units_apr_proposed": [], "units_apr_approved": [],
        "units_permit_estimated": [],
        "income_very_low": [], "income_low": [],
        "income_moderate": [], "income_above_moderate": [],
        "status": [], "density_bonus": [], "sb35": [],
        "first_activity": [], "last_activity": [],
        "permit_count": [], "planning_refs": [],
        "apr_tracking_ids": [], "meeting_mention_count": [],
    }

    addr_resolved = 0
    for p in projects:
        columns["project_id"].append(p.get("project_id", ""))
        columns["project_name"].append(p.get("project_name", ""))
        columns["agency"].append(p.get("agency", ""))
        addr = p.get("address", "") or ""
        apn = p.get("apn", "") or ""
        if not addr and apn and apn in parcel_addrs:
            addr = parcel_addrs[apn]
            addr_resolved += 1
        columns["address"].append(addr)
        columns["apn"].append(apn)
        pz = parcel_zones.get(apn, {}) if apn else {}
        columns["zone_code"].append(pz.get("zone_code", ""))
        columns["zone_category"].append(pz.get("zone_category", ""))
        density = None
        height = ""
        if pz:
            density = _parse_density(pz.get("density"))
            h = pz.get("height")
            if h and not (isinstance(h, float) and math.isnan(h)):
                height = str(h)
        columns["max_density"].append(density)
        columns["max_height"].append(height)
        columns["latitude"].append(p.get("latitude") or 0.0)
        columns["longitude"].append(p.get("longitude") or 0.0)
        columns["is_downtown"].append(bool(p.get("is_downtown")))

        units = p.get("units", {})
        columns["units_best"].append(units.get("best") or 0)
        columns["units_source"].append(units.get("best_source", ""))
        columns["units_apr_proposed"].append(units.get("apr_proposed") or 0)
        columns["units_apr_approved"].append(units.get("apr_approved") or 0)
        columns["units_permit_estimated"].append(units.get("permit_estimated") or 0)

        tiers = p.get("income_tiers") or {}
        columns["income_very_low"].append(tiers.get("very_low", 0))
        columns["income_low"].append(tiers.get("low", 0))
        columns["income_moderate"].append(tiers.get("moderate", 0))
        columns["income_above_moderate"].append(tiers.get("above_moderate", 0))

        columns["status"].append(p.get("status", ""))
        columns["density_bonus"].append(bool(p.get("density_bonus")))
        columns["sb35"].append(bool(p.get("sb35")))
        columns["first_activity"].append(p.get("first_activity") or "")
        columns["last_activity"].append(p.get("last_activity") or "")

        src = p.get("sources", {})
        pp = src.get("permit_project") or {}
        columns["permit_count"].append(pp.get("permit_count", 0))

        plan_nos = [pl.get("project_no", "") for pl in src.get("planning_projects", [])]
        columns["planning_refs"].append(",".join(plan_nos) if plan_nos else "")

        apr_ids = [a.get("tracking_id", "") for a in src.get("apr_filings", [])]
        columns["apr_tracking_ids"].append(",".join(apr_ids) if apr_ids else "")

        columns["meeting_mention_count"].append(len(src.get("meeting_mentions", [])))

    if addr_resolved:
        print(f"  Housing projects: {addr_resolved} addresses resolved via parcel APN join")
    return columns


def build_planning_projects_table():
    """Build column-oriented dict for planning projects Parquet table."""
    agencies = load_agencies(enabled_only=False)
    records = []
    for slug, cfg in agencies.items():
        if "permits" not in cfg:
            continue
        permits_dir = agency_data_dir(slug) / "permits"
        if not permits_dir.exists():
            continue
        for jf in sorted(permits_dir.glob("etrakit-projects-*.jsonl")):
            year_match = re.search(r"(\d{4})", jf.name)
            year = int(year_match.group(1)) if year_match else 0
            with open(jf) as f:
                for line in f:
                    if line.strip():
                        r = json.loads(line)
                        r["_year"] = year
                        r["_agency"] = slug
                        records.append(r)

    if not records:
        return None

    columns = {
        "project_no": [], "year": [], "agency": [],
        "type": [], "status": [], "name": [], "description": [],
        "applied": [], "approved": [],
        "address": [], "apn": [], "planner": [],
    }

    for r in records:
        columns["project_no"].append(r.get("project_no", ""))
        columns["year"].append(r.get("_year", 0))
        columns["agency"].append(r.get("_agency", ""))
        columns["type"].append(r.get("type", ""))
        columns["status"].append(r.get("status", ""))
        columns["name"].append(r.get("name", ""))
        columns["description"].append(r.get("description", ""))
        columns["applied"].append(r.get("applied") or "")
        columns["approved"].append(r.get("approved") or "")
        columns["address"].append(r.get("address") or "")
        columns["apn"].append(r.get("apn") or "")
        columns["planner"].append(r.get("planner") or "")

    return columns


def build_str_table():
    """Build column-oriented dict for short-term rental licenses Parquet table."""
    if not STR_FILE.exists():
        return None

    records = json.loads(STR_FILE.read_text())
    columns = {
        "account": [], "business": [], "license_status": [],
        "business_area": [], "rental_type": [],
        "address": [], "apn": [],
        "property_manager": [],
        "latitude": [], "longitude": [],
    }

    for r in records:
        columns["account"].append(r.get("account", 0))
        columns["business"].append(r.get("business", ""))
        columns["license_status"].append(r.get("license_status", ""))
        columns["business_area"].append(r.get("business_area", ""))
        columns["rental_type"].append(r.get("rental_type", ""))
        columns["address"].append(r.get("address", ""))
        columns["apn"].append(r.get("apn", ""))
        columns["property_manager"].append(r.get("property_manager", "") or "")
        columns["latitude"].append(r.get("latitude") or 0.0)
        columns["longitude"].append(r.get("longitude") or 0.0)

    return columns


def export_parquet(columns, output_path):
    """Write column dict as Parquet file."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    INT_COLS = {"year", "permit_count", "estimated_units",
                "tot_proposed_units", "tot_approved_units", "tot_disapproved_units",
                "above_mod_income", "affordable_units",
                "units_best", "units_apr_proposed", "units_apr_approved",
                "units_permit_estimated", "income_very_low", "income_low",
                "income_moderate", "income_above_moderate", "meeting_mention_count",
                "account"}
    BOOL_COLS = {"is_downtown", "has_building_permit", "density_bonus", "sb35"}
    # APR table uses string "Yes"/"No" for these; only cast if actual bools
    for bc in list(BOOL_COLS):
        if bc in columns and columns[bc] and isinstance(columns[bc][0], str):
            BOOL_COLS = BOOL_COLS - {bc}
    FLOAT_COLS = {"latitude", "longitude", "max_density"}

    arrays = {}
    for col, values in columns.items():
        if col in INT_COLS:
            arrays[col] = pa.array(values, type=pa.int32())
        elif col in BOOL_COLS:
            arrays[col] = pa.array(values, type=pa.bool_())
        elif col in FLOAT_COLS:
            arrays[col] = pa.array(values, type=pa.float64())
        else:
            arrays[col] = pa.array(values, type=pa.string())

    table = pa.table(arrays)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, output_path, compression="snappy")
    return table


def upload_to_s3(local_path, s3_key):
    """Upload a file to the stoside-data S3 bucket."""
    import boto3

    session = boto3.Session(profile_name=STOSIDE_PROFILE, region_name=STOSIDE_REGION)
    s3 = session.client("s3")
    s3.upload_file(str(local_path), STOSIDE_BUCKET, s3_key)
    print(f"  Uploaded {local_path.name} → s3://{STOSIDE_BUCKET}/{s3_key}")


def cmd_export(args):
    """Export permits and projects as Parquet."""
    print("Loading permits...", flush=True)
    permits = load_all_permits()
    project_index, projects = load_projects()

    print(f"  {len(permits)} permits across {len(set(p['_year'] for p in permits))} years")
    print(f"  {len(projects)} projects, {len(project_index)} permit→project mappings")

    with_apn = sum(1 for p in permits if p.get("apn"))
    with_zone = sum(1 for p in permits if p.get("zone_code"))
    downtown = sum(1 for p in permits if p.get("is_downtown"))
    print(f"  {with_apn} with APN, {with_zone} with zone, {downtown} downtown")

    print("Loading parcel data...", flush=True)
    try:
        parcel_addrs = load_parcel_addresses()
    except Exception as e:
        print(f"  WARNING: Could not load parcels: {e}")
        parcel_addrs = {}
    parcel_zones = load_parcel_zoning()

    apr_cols = build_apr_table()
    apr_count = len(apr_cols["year"]) if apr_cols else 0
    housing_cols = build_housing_projects_table(parcel_addrs, parcel_zones)
    housing_count = len(housing_cols["project_id"]) if housing_cols else 0
    planning_cols = build_planning_projects_table()
    planning_count = len(planning_cols["project_no"]) if planning_cols else 0
    str_cols = build_str_table()
    str_count = len(str_cols["account"]) if str_cols else 0

    if args.dry_run:
        print("\n[dry run] Would export:")
        print(f"  permits.parquet: {len(permits)} rows × 18 columns")
        print(f"  permit_projects.parquet: {len(projects)} rows × 11 columns")
        print(f"  apr_filings.parquet: {apr_count} rows × 22 columns")
        print(f"  housing_projects.parquet: {housing_count} rows × 28 columns")
        print(f"  planning_projects.parquet: {planning_count} rows × 12 columns")
        print(f"  str_licenses.parquet: {str_count} rows × 10 columns")
        return

    print("\nExporting Parquet...", flush=True)
    permits_cols = build_permits_table(permits, project_index, parcel_addrs, parcel_zones)
    permits_table = export_parquet(permits_cols, EXPORTS_DIR / "permits.parquet")
    print(f"  permits.parquet: {permits_table.num_rows} rows, {(EXPORTS_DIR / 'permits.parquet').stat().st_size:,} bytes")

    projects_cols = build_projects_table(projects)
    projects_table = export_parquet(projects_cols, EXPORTS_DIR / "permit_projects.parquet")
    print(f"  permit_projects.parquet: {projects_table.num_rows} rows, {(EXPORTS_DIR / 'permit_projects.parquet').stat().st_size:,} bytes")

    if apr_cols:
        apr_table = export_parquet(apr_cols, EXPORTS_DIR / "apr_filings.parquet")
        print(f"  apr_filings.parquet: {apr_table.num_rows} rows, {(EXPORTS_DIR / 'apr_filings.parquet').stat().st_size:,} bytes")

    if housing_cols:
        housing_table = export_parquet(housing_cols, EXPORTS_DIR / "housing_projects.parquet")
        print(f"  housing_projects.parquet: {housing_table.num_rows} rows, {(EXPORTS_DIR / 'housing_projects.parquet').stat().st_size:,} bytes")

    if planning_cols:
        planning_table = export_parquet(planning_cols, EXPORTS_DIR / "planning_projects.parquet")
        print(f"  planning_projects.parquet: {planning_table.num_rows} rows, {(EXPORTS_DIR / 'planning_projects.parquet').stat().st_size:,} bytes")

    if str_cols:
        str_table = export_parquet(str_cols, EXPORTS_DIR / "str_licenses.parquet")
        print(f"  str_licenses.parquet: {str_table.num_rows} rows, {(EXPORTS_DIR / 'str_licenses.parquet').stat().st_size:,} bytes")

    if args.local_only:
        print(f"\nLocal export complete → {EXPORTS_DIR}/")
        return

    print("\nUploading to S3...", flush=True)
    try:
        upload_to_s3(EXPORTS_DIR / "permits.parquet", "data/permits.parquet")
        upload_to_s3(EXPORTS_DIR / "permit_projects.parquet", "data/permit_projects.parquet")
        if apr_cols:
            upload_to_s3(EXPORTS_DIR / "apr_filings.parquet", "data/apr_filings.parquet")
        if housing_cols:
            upload_to_s3(EXPORTS_DIR / "housing_projects.parquet", "data/housing_projects.parquet")
        if planning_cols:
            upload_to_s3(EXPORTS_DIR / "planning_projects.parquet", "data/planning_projects.parquet")
        if str_cols:
            upload_to_s3(EXPORTS_DIR / "str_licenses.parquet", "data/str_licenses.parquet")
        print(f"\nDone. Files queryable via stoside-data DuckDB Lambda.")
    except Exception as e:
        print(f"\nS3 upload failed: {e}")
        print(f"Local files available at {EXPORTS_DIR}/")


def main():
    parser = argparse.ArgumentParser(description="Export permits as Parquet for DuckDB")
    parser.add_argument("--dry-run", action="store_true", help="Show schema + counts only")
    parser.add_argument("--local-only", action="store_true", help="Export locally, skip S3 upload")
    args = parser.parse_args()
    cmd_export(args)


if __name__ == "__main__":
    main()
