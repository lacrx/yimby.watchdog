#!/usr/bin/env python3
"""
Download and filter HUD LIHTC (Low-Income Housing Tax Credit) data.

Downloads the national LIHTC database CSV from HUD, filters to San Diego
County cities, and matches records to permit projects by address.

Usage:
    python hud_lihtc.py download              # download + filter CSV
    python hud_lihtc.py match                 # match to permit projects
    python hud_lihtc.py stats                 # show local LIHTC summary

Data source: https://www.huduser.gov/portal/datasets/lihtc.html
Output: data/reference/hud-lihtc-sdcounty.json
"""

import argparse
import io
import json
import re
import sys
import tempfile
import zipfile
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DATA_DIR = REPO_ROOT / "data"
REFERENCE_DIR = DATA_DIR / "reference"
LIHTC_FILE = REFERENCE_DIR / "hud-lihtc-sdcounty.json"
OVERRIDES_FILE = REFERENCE_DIR / "hud-lihtc-overrides.json"
PROJECTS_FILE = DATA_DIR / "structured" / "permit-projects.json"

LIHTC_ZIP_URL = "https://www.huduser.gov/portal/datasets/lihtc/LIHTCPUB.zip"

SD_COUNTY_CITIES = {
    "OCEANSIDE", "CARLSBAD", "VISTA", "SAN MARCOS", "ESCONDIDO",
    "ENCINITAS", "SOLANA BEACH", "DEL MAR", "POWAY", "SAN DIEGO",
    "CHULA VISTA", "NATIONAL CITY", "EL CAJON", "LA MESA", "SANTEE",
    "LEMON GROVE", "IMPERIAL BEACH", "CORONADO",
}

LIHTC_FIELDS = [
    "HUD_ID", "PROJECT", "PROJ_ADD", "PROJ_CTY", "PROJ_ST", "PROJ_ZIP",
    "LATITUDE", "LONGITUDE", "N_UNITS", "LI_UNITS", "YR_ALLOC", "YR_PIS",
    "CREDIT", "TYPE", "STATE_ID", "FIPS2010",
]


def download_lihtc():
    """Download LIHTC CSV from HUD, filter to SD County, save locally."""
    print("Downloading LIHTC database from HUD...", flush=True)
    resp = requests.get(LIHTC_ZIP_URL, timeout=120, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    resp.raise_for_status()
    print(f"  Downloaded {len(resp.content):,} bytes", flush=True)

    records = []
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        dbf_names = [n for n in zf.namelist() if n.upper().endswith(".DBF")]
        if not dbf_names:
            print("  No DBF file found in ZIP")
            return []
        dbf_name = dbf_names[0]
        print(f"  Extracting {dbf_name}...", flush=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            zf.extract(dbf_name, tmpdir)
            from dbfread import DBF
            table = DBF(f"{tmpdir}/{dbf_name}", encoding="latin-1")

            total = 0
            ca_count = 0
            for row in table:
                total += 1
                if str(row.get("PROJ_ST", "")).strip().upper() != "CA":
                    continue
                ca_count += 1

                city = str(row.get("PROJ_CTY", "")).strip().upper()
                if city not in SD_COUNTY_CITIES:
                    continue

                record = {}
                for field in LIHTC_FIELDS:
                    val = row.get(field, "")
                    if val is None:
                        val = ""
                    elif not isinstance(val, str):
                        record[field.lower()] = val
                        continue
                    record[field.lower()] = str(val).strip()

                if record.get("latitude"):
                    try:
                        record["latitude"] = float(record["latitude"])
                    except (ValueError, TypeError):
                        record["latitude"] = None
                if record.get("longitude"):
                    try:
                        record["longitude"] = float(record["longitude"])
                    except (ValueError, TypeError):
                        record["longitude"] = None
                for int_field in ("n_units", "li_units", "yr_alloc", "yr_pis"):
                    if record.get(int_field) is not None:
                        try:
                            record[int_field] = int(record[int_field])
                        except (ValueError, TypeError):
                            record[int_field] = None

                records.append(record)

    print(f"  {total:,} national records, {ca_count} CA, {len(records)} SD County")

    REFERENCE_DIR.mkdir(parents=True, exist_ok=True)
    LIHTC_FILE.write_text(json.dumps(records, indent=2))
    print(f"  Saved to {LIHTC_FILE}")

    return records


def normalize_address(addr):
    """Normalize address for fuzzy matching."""
    if not addr:
        return ""
    addr = addr.upper().strip()
    addr = re.sub(r"\s+", " ", addr)
    addr = re.sub(r"\bSTREET\b", "ST", addr)
    addr = re.sub(r"\bAVENUE\b", "AVE", addr)
    addr = re.sub(r"\bBOULEVARD\b", "BLVD", addr)
    addr = re.sub(r"\bDRIVE\b", "DR", addr)
    addr = re.sub(r"\bLANE\b", "LN", addr)
    addr = re.sub(r"\bCOURT\b", "CT", addr)
    addr = re.sub(r"\bROAD\b", "RD", addr)
    addr = re.sub(r"\bHIGHWAY\b", "HWY", addr)
    addr = re.sub(r"\bNORTH\b", "N", addr)
    addr = re.sub(r"\bSOUTH\b", "S", addr)
    addr = re.sub(r"\bEAST\b", "E", addr)
    addr = re.sub(r"\bWEST\b", "W", addr)
    addr = re.sub(r"[#,.]", "", addr)
    addr = re.sub(r"\b(SUITE|STE|UNIT|APT|BLDG|BUILDING)\s*\S*", "", addr)
    return addr.strip()


def extract_street_number(addr):
    """Extract leading street number from address."""
    m = re.match(r"(\d+)", addr)
    return m.group(1) if m else ""


def match_projects():
    """Match LIHTC records to permit projects by address."""
    if not LIHTC_FILE.exists():
        print("No LIHTC data. Run 'download' first.")
        return

    records = json.loads(LIHTC_FILE.read_text())
    oceanside = [r for r in records if r.get("proj_cty", "").upper() == "OCEANSIDE"]
    print(f"LIHTC records: {len(records)} SD County, {len(oceanside)} Oceanside")

    if not PROJECTS_FILE.exists():
        print("No permit projects file found.")
        return

    projects = json.loads(PROJECTS_FILE.read_text()).get("projects", [])
    print(f"Permit projects: {len(projects)}")

    overrides = {}
    if OVERRIDES_FILE.exists():
        overrides = json.loads(OVERRIDES_FILE.read_text())

    matches = []
    for lihtc in oceanside:
        lihtc_addr = normalize_address(lihtc.get("proj_add", ""))
        lihtc_num = extract_street_number(lihtc_addr)
        lihtc_name = lihtc.get("project", "").upper()

        best_match = None
        best_score = 0

        if lihtc.get("hud_id") in overrides:
            override = overrides[lihtc["hud_id"]]
            matches.append({
                "hud_id": lihtc["hud_id"],
                "project_name": lihtc_name,
                "address": lihtc.get("proj_add", ""),
                "matched_project": override.get("project_id", ""),
                "match_type": "override",
                "units": lihtc.get("n_units"),
                "li_units": lihtc.get("li_units"),
                "yr_alloc": lihtc.get("yr_alloc"),
            })
            continue

        for proj in projects:
            score = 0
            proj_addr = normalize_address(proj.get("address", ""))
            proj_num = extract_street_number(proj_addr)
            proj_name = proj.get("project_name", "").upper()

            if lihtc_num and proj_num and lihtc_num == proj_num:
                score += 40
                lihtc_street = lihtc_addr[len(lihtc_num):].strip()
                proj_street = proj_addr[len(proj_num):].strip()
                if lihtc_street and proj_street:
                    lihtc_words = set(lihtc_street.split())
                    proj_words = set(proj_street.split())
                    overlap = lihtc_words & proj_words
                    if overlap:
                        score += 30 * len(overlap) / max(len(lihtc_words), len(proj_words))

            if lihtc_name and proj_name:
                lihtc_words = set(lihtc_name.split())
                proj_words = set(proj_name.split())
                overlap = lihtc_words & proj_words
                if len(overlap) >= 2:
                    score += 25
                elif len(overlap) == 1 and len(list(overlap)[0]) > 4:
                    score += 10

            if score > best_score:
                best_score = score
                best_match = proj

        if best_score >= 50:
            matches.append({
                "hud_id": lihtc["hud_id"],
                "project_name": lihtc_name,
                "address": lihtc.get("proj_add", ""),
                "matched_project": best_match["project_id"],
                "matched_name": best_match["project_name"],
                "match_type": "auto",
                "match_score": best_score,
                "units": lihtc.get("n_units"),
                "li_units": lihtc.get("li_units"),
                "yr_alloc": lihtc.get("yr_alloc"),
            })

    print(f"\nMatched: {len(matches)}/{len(oceanside)} Oceanside LIHTC records")
    for m in matches:
        print(f"  {m['hud_id']}: {m['project_name']} → {m.get('matched_project', '?')} ({m['match_type']}, units={m.get('units')})")

    unmatched = [r for r in oceanside if r["hud_id"] not in {m["hud_id"] for m in matches}]
    if unmatched:
        print(f"\nUnmatched ({len(unmatched)}):")
        for r in unmatched:
            print(f"  {r['hud_id']}: {r.get('project', '')} | {r.get('proj_add', '')} | units={r.get('n_units')} | yr={r.get('yr_alloc')}")

    match_file = REFERENCE_DIR / "hud-lihtc-matches.json"
    match_file.write_text(json.dumps(matches, indent=2))
    print(f"\nSaved matches to {match_file}")


def cmd_stats(args):
    """Show LIHTC data summary."""
    if not LIHTC_FILE.exists():
        print("No LIHTC data. Run 'download' first.")
        return

    records = json.loads(LIHTC_FILE.read_text())
    from collections import Counter

    by_city = Counter(r.get("proj_cty", "").upper() for r in records)
    print(f"SD County LIHTC records: {len(records)}")
    print(f"\nBy city:")
    for city, count in by_city.most_common():
        total_units = sum(r.get("n_units", 0) or 0 for r in records if r.get("proj_cty", "").upper() == city)
        li_units = sum(r.get("li_units", 0) or 0 for r in records if r.get("proj_cty", "").upper() == city)
        print(f"  {city}: {count} projects, {total_units} total units, {li_units} low-income")

    oceanside = [r for r in records if r.get("proj_cty", "").upper() == "OCEANSIDE"]
    if oceanside:
        print(f"\nOceanside LIHTC projects ({len(oceanside)}):")
        for r in sorted(oceanside, key=lambda x: x.get("yr_alloc") or 0):
            print(f"  {r.get('yr_alloc', '?')}: {r.get('project', '')} | {r.get('proj_add', '')} | {r.get('n_units', '?')} units ({r.get('li_units', '?')} LI) | {r.get('credit', '')}")


def main():
    parser = argparse.ArgumentParser(description="HUD LIHTC data loader")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("download", help="Download + filter LIHTC CSV from HUD")
    sub.add_parser("match", help="Match LIHTC to permit projects")
    sub.add_parser("stats", help="Show LIHTC summary")

    args = parser.parse_args()

    if args.command == "download":
        download_lihtc()
    elif args.command == "match":
        match_projects()
    elif args.command == "stats":
        cmd_stats(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
