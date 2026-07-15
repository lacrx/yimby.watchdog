#!/usr/bin/env python3
"""
eTRAKiT building permit scraper.

Enumerates building permits by ID pattern (BLDG{YY}-{NNNN}).
Reads permit config (base URL, ID pattern) from agencies.yaml.

Usage:
    python etrakit.py fetch                     # scan current year, resume from last
    python etrakit.py fetch --year 2025         # scan specific year
    python etrakit.py fetch --full              # full rescan
    python etrakit.py fetch --agency oceanside  # explicit agency
    python etrakit.py enrich --year 2026        # re-fetch housing permits with extended fields

Requires: requests, beautifulsoup4, pyyaml
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import load_agencies, agency_data_dir

UA = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"

HOUSING_TYPES = {
    "BLD SFD OR DUPLEX", "BLD MULTI FAMILY", "BLD MID RISE",
    "BLD HIGH RISE", "BLD ACCESSORY DWELLING",
}

INFILL_TYPES = HOUSING_TYPES | {
    "BLD COMMERCIAL NEW", "BLD COMMERCIAL SHELL", "BLD COMMERCIAL ADDITION",
    "BLD LIVEWORK OR MIXED USE", "BLD ROOM ADDITION", "BLD MISC STRUCTURE",
    "BLD TI GENERAL", "BLD TI RESTAURANT", "BLD TI MEDICAL", "BLD TI HAZARDOUS",
    "BLD DEMOLITION", "BLD PARKING STRUCT", "BLD NEW RESTAURANT",
    "BLD COMMERCIAL PME",
}


def fetch_permit(session, base_url, permit_no, enrich=False):
    """Fetch a single permit by direct URL. Returns dict or None."""
    url = f"{base_url}/Search/permit.aspx?activityNo={permit_no}"
    resp = session.get(url, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    def label(suffix):
        el = soup.find("span", id=lambda x: x and x.endswith(suffix) and "Lbl" not in x)
        return el.get_text(strip=True) if el else ""

    ptype = label("lblPermitType")
    if not ptype:
        return None

    addr = ""
    for span in soup.find_all("span"):
        sid = span.get("id", "")
        if "Addr" in sid and "Lbl" not in sid and "City" not in sid:
            txt = span.get_text(strip=True)
            if txt and txt != "Address:":
                addr = txt
                break

    result = {
        "permit_no": permit_no,
        "type": ptype,
        "subtype": label("lblPermitSubtype"),
        "description": label("lblPermitDesc"),
        "status": label("lblPermitStatus"),
        "applied": label("lblPermitAppliedDate"),
        "approved": label("lblPermitApprovedDate"),
        "issued": label("lblPermitIssuedDate"),
        "address": addr,
        "apn": label("lblPermitAPN"),
        "owner": label("lblPermitOwner"),
    }

    if enrich:
        result.update(_extract_enriched(soup))

    return result


def _extract_enriched(soup):
    """Extract contacts, site info, linked activities from loaded page."""
    extra = {}

    # Site info
    site_csz = soup.find("span", id=lambda x: x and x.endswith("lblSiteCityStateZip"))
    if site_csz:
        extra["site_city_state_zip"] = site_csz.get_text(strip=True)

    lot_sqft = soup.find("span", id=lambda x: x and x.endswith("lblSiteLotSqFt"))
    if lot_sqft:
        extra["lot_sqft"] = lot_sqft.get_text(strip=True)

    prop_type = soup.find("span", id=lambda x: x and x.endswith("lblPropertyType"))
    if prop_type:
        extra["property_type"] = prop_type.get_text(strip=True)

    # APN from parcel link (more reliable than label)
    parcel_link = soup.find("a", href=lambda x: x and "parcel.aspx?activityNo=" in str(x))
    if parcel_link:
        apn_text = parcel_link.get_text(strip=True)
        if apn_text:
            extra["apn"] = apn_text

    # Contacts from RadGrid
    contacts = []
    contacts_panel = soup.find("span", id=lambda x: x and "rgContactInfo" in str(x))
    if contacts_panel:
        rows = contacts_panel.find_all("tr", class_=lambda c: c and ("rgRow" in c or "rgAltRow" in c))
        for tr in rows:
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if len(cells) >= 2 and cells[0]:
                contact = {"role": cells[0], "name": cells[1]}
                if len(cells) > 2 and cells[2]:
                    contact["phone"] = cells[2]
                if len(cells) > 3 and cells[3]:
                    contact["email"] = cells[3]
                if len(cells) > 4 and cells[4]:
                    contact["address"] = cells[4]
                if len(cells) > 5 and cells[5]:
                    contact["city_state_zip"] = cells[5]
                contacts.append(contact)
    if contacts:
        extra["contacts"] = contacts
        owner = next((c for c in contacts if c["role"] == "OWNER"), None)
        if owner:
            extra["owner"] = owner["name"]

    # Linked parent permits
    linked_parents = []
    parent_links = soup.find_all("a", href=lambda x: x and "permit.aspx?activityNo=" in str(x))
    for a in parent_links:
        parent_no = a.get_text(strip=True)
        if parent_no and parent_no != extra.get("permit_no", "") and "Go to Parent" in a.get("title", ""):
            linked_parents.append(parent_no)
    if linked_parents:
        extra["linked_parent"] = linked_parents

    return extra


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


def cmd_fetch(args):
    """Fetch building permits by enumerating permit numbers."""
    agencies = load_agencies(enabled_only=False)
    agency_slug = args.agency
    config = agencies.get(agency_slug)
    if not config or "permits" not in config:
        print(f"No permit config for agency '{agency_slug}'")
        sys.exit(1)

    permit_config = config["permits"]
    base_url = permit_config["base_url"]
    data_dir = agency_data_dir(agency_slug)
    permits_dir = data_dir / "permits"
    permits_dir.mkdir(parents=True, exist_ok=True)

    year = args.year
    yy = year % 100
    prefix = f"BLDG{yy:02d}-"
    max_seq = args.max_seq
    max_misses = args.max_misses

    out_file = permits_dir / f"etrakit-permits-{year}.jsonl"

    existing = []
    start_seq = 1
    if out_file.exists() and not args.full:
        with open(out_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    existing.append(json.loads(line))
        if existing:
            max_existing = max(
                int(p["permit_no"].split("-")[1]) for p in existing
            )
            start_seq = max_existing + 1
            print(f"Resuming from {prefix}{start_seq:04d} ({len(existing)} existing permits)", flush=True)

    session = _make_session()

    new_results = []
    misses = 0

    if start_seq > max_seq:
        print(f"Already scanned up to {prefix}{start_seq - 1:04d}, checking for new permits...")
        max_seq = start_seq + 200

    delay = args.delay
    print(f"Scanning eTRAKiT permits {prefix}{start_seq:04d} through {prefix}{max_seq:04d} (delay={delay}s)...", flush=True)

    consecutive_failures = 0
    for i in range(start_seq, max_seq + 1):
        permit_no = f"{prefix}{i:04d}"
        try:
            p = fetch_permit(session, base_url, permit_no)
        except Exception as e:
            consecutive_failures += 1
            if consecutive_failures <= 3:
                print(f"  Error on {permit_no}: {e}", flush=True)
            elif consecutive_failures == 4:
                print(f"  (suppressing further errors...)", flush=True)
            if consecutive_failures > max_misses and i > start_seq + 50:
                print(f"  Stopping at {permit_no} after {consecutive_failures} consecutive failures", flush=True)
                break
            time.sleep(10)
            continue

        if p:
            new_results.append(p)
            misses = 0
            consecutive_failures = 0
        else:
            misses += 1
            consecutive_failures += 1
            if misses > max_misses and i > start_seq + 50:
                print(f"  Stopping at {permit_no} after {max_misses} consecutive misses", flush=True)
                break

        time.sleep(delay)

        if i % 100 == 0:
            print(f"  ...{i}: {len(new_results)} new permits found", flush=True)
            time.sleep(3)

    all_results = existing + new_results
    with open(out_file, "w") as f:
        for p in all_results:
            f.write(json.dumps(p) + "\n")

    housing = [p for p in all_results if
               any(h in p["type"].upper() for h in ["DWELLING", "RESIDENTIAL", "SFR", "MULTI", "DUPLEX"])]

    print(f"\n{len(new_results)} new permits, {len(all_results)} total for {year}")
    print(f"{len(housing)} housing-related permits")

    type_counts = Counter(p["type"] for p in all_results)
    print("\nBy type:")
    for t, c in type_counts.most_common(15):
        print(f"  {t}: {c}")


def cmd_enrich(args):
    """Re-fetch housing permits with extended fields (contacts, site info, linked activities)."""
    agencies = load_agencies(enabled_only=False)
    agency_slug = args.agency
    config = agencies.get(agency_slug)
    if not config or "permits" not in config:
        print(f"No permit config for agency '{agency_slug}'")
        sys.exit(1)

    permit_config = config["permits"]
    base_url = permit_config["base_url"]
    data_dir = agency_data_dir(agency_slug)
    permits_dir = data_dir / "permits"

    year = args.year
    in_file = permits_dir / f"etrakit-permits-{year}.jsonl"
    if not in_file.exists():
        print(f"No permit data for {year}. Run fetch first.")
        sys.exit(1)

    permits = []
    with open(in_file) as f:
        for line in f:
            line = line.strip()
            if line:
                permits.append(json.loads(line))

    housing = [p for p in permits if p.get("type", "") in HOUSING_TYPES]
    if args.all_types:
        to_enrich = permits
    elif args.infill:
        to_enrich = [p for p in permits if p.get("type", "") in INFILL_TYPES]
    else:
        to_enrich = housing

    already_enriched = sum(1 for p in to_enrich if p.get("contacts") or p.get("linked_parent"))
    needs_enrichment = [p for p in to_enrich if not p.get("contacts") and not p.get("linked_parent")]

    print(f"Year {year}: {len(permits)} total, {len(housing)} housing, {already_enriched} already enriched")
    print(f"To enrich: {len(needs_enrichment)} permits")

    if not needs_enrichment:
        print("Nothing to enrich.")
        return

    batch_size = args.batch
    if batch_size > 0:
        needs_enrichment = needs_enrichment[:batch_size]
        print(f"Batch limited to {batch_size} permits")

    session = _make_session()
    permit_index = {p["permit_no"]: p for p in permits}
    enriched_count = 0

    for i, p in enumerate(needs_enrichment):
        permit_no = p["permit_no"]
        try:
            result = fetch_permit(session, base_url, permit_no, enrich=True)
        except Exception as e:
            print(f"  Error on {permit_no}: {e}", flush=True)
            time.sleep(5)
            continue

        if result:
            permit_index[permit_no] = result
            enriched_count += 1
            has_owner = bool(result.get("owner"))
            has_parent = bool(result.get("linked_parent"))
            has_apn = bool(result.get("apn"))
            if has_owner or has_parent or has_apn:
                extras = []
                if has_owner:
                    extras.append(f"owner={result['owner'][:30]}")
                if has_parent:
                    extras.append(f"parent={result['linked_parent']}")
                if has_apn:
                    extras.append(f"apn={result['apn']}")
                print(f"  {permit_no}: {', '.join(extras)}", flush=True)

        time.sleep(args.delay)

        if (i + 1) % 50 == 0:
            print(f"  ...{i + 1}/{len(needs_enrichment)}: {enriched_count} enriched", flush=True)
            time.sleep(2)

    all_results = [permit_index[p["permit_no"]] for p in permits]
    with open(in_file, "w") as f:
        for p in all_results:
            f.write(json.dumps(p) + "\n")

    print(f"\nEnriched {enriched_count} permits, wrote {len(all_results)} to {in_file}")


def cmd_backfill_apn(args):
    """Re-fetch permits missing APNs. Faster than full enrich — skips already-enriched."""
    agencies = load_agencies(enabled_only=False)
    agency_slug = args.agency
    config = agencies.get(agency_slug)
    if not config or "permits" not in config:
        print(f"No permit config for agency '{agency_slug}'")
        sys.exit(1)

    permit_config = config["permits"]
    base_url = permit_config["base_url"]
    data_dir = agency_data_dir(agency_slug)
    permits_dir = data_dir / "permits"

    years = list(range(args.start_year, args.end_year + 1)) if not args.year else [args.year]

    session = _make_session()
    total_backfilled = 0

    for year in years:
        in_file = permits_dir / f"etrakit-permits-{year}.jsonl"
        if not in_file.exists():
            continue

        permits = []
        with open(in_file) as f:
            for line in f:
                if line.strip():
                    permits.append(json.loads(line))

        if args.housing_only:
            candidates = [p for p in permits if p.get("type", "") in HOUSING_TYPES and not p.get("apn")]
        else:
            candidates = [p for p in permits if p.get("type", "") in INFILL_TYPES and not p.get("apn")]

        if not candidates:
            print(f"{year}: {len(permits)} permits, 0 need APN backfill")
            continue

        print(f"{year}: {len(permits)} permits, {len(candidates)} need APN backfill")

        batch_size = args.batch
        if batch_size > 0:
            candidates = candidates[:batch_size]

        permit_index = {p["permit_no"]: p for p in permits}
        backfilled = 0

        for i, p in enumerate(candidates):
            permit_no = p["permit_no"]
            try:
                result = fetch_permit(session, base_url, permit_no, enrich=True)
            except Exception as e:
                print(f"  Error on {permit_no}: {e}", flush=True)
                time.sleep(5)
                continue

            if result and result.get("apn"):
                permit_index[permit_no] = result
                backfilled += 1
                print(f"  {permit_no}: apn={result['apn']}", flush=True)
            elif result:
                permit_index[permit_no] = result

            time.sleep(args.delay)

            if (i + 1) % 50 == 0:
                print(f"  ...{i + 1}/{len(candidates)}: {backfilled} APNs found", flush=True)
                time.sleep(2)

        all_results = [permit_index[p["permit_no"]] for p in permits]
        with open(in_file, "w") as f:
            for p in all_results:
                f.write(json.dumps(p) + "\n")

        print(f"  {year}: backfilled {backfilled} APNs")
        total_backfilled += backfilled

    print(f"\nTotal: {total_backfilled} APNs backfilled")


def main():
    parser = argparse.ArgumentParser(description="eTRAKiT building permit scraper")
    sub = parser.add_subparsers(dest="command")

    fetch_p = sub.add_parser("fetch", help="Scan for new permits")
    fetch_p.add_argument("--agency", default="oceanside", help="Agency slug from agencies.yaml")
    fetch_p.add_argument("--year", type=int, default=datetime.now().year, help="Year to scan")
    fetch_p.add_argument("--max-seq", type=int, default=5000, help="Max permit sequence number")
    fetch_p.add_argument("--max-misses", type=int, default=50, help="Stop after N consecutive misses")
    fetch_p.add_argument("--full", action="store_true", help="Full rescan (ignore existing)")
    fetch_p.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")

    enrich_p = sub.add_parser("enrich", help="Re-fetch permits with extended fields")
    enrich_p.add_argument("--agency", default="oceanside", help="Agency slug")
    enrich_p.add_argument("--year", type=int, default=datetime.now().year, help="Year to enrich")
    enrich_p.add_argument("--batch", type=int, default=0, help="Max permits to enrich (0=all)")
    enrich_p.add_argument("--infill", action="store_true", help="Enrich infill/new construction types (housing + commercial + TI + demo)")
    enrich_p.add_argument("--all-types", action="store_true", help="Enrich all types")
    enrich_p.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")

    bfill_p = sub.add_parser("backfill-apn", help="Re-fetch permits missing APNs")
    bfill_p.add_argument("--agency", default="oceanside", help="Agency slug")
    bfill_p.add_argument("--year", type=int, default=0, help="Specific year (default: all years)")
    bfill_p.add_argument("--start-year", type=int, default=2020, help="Start year for range")
    bfill_p.add_argument("--end-year", type=int, default=datetime.now().year, help="End year for range")
    bfill_p.add_argument("--batch", type=int, default=0, help="Max permits per year (0=all)")
    bfill_p.add_argument("--housing-only", action="store_true", help="Only backfill housing types")
    bfill_p.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")

    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "enrich":
        cmd_enrich(args)
    elif args.command == "backfill-apn":
        cmd_backfill_apn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
