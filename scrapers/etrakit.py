#!/usr/bin/env python3
"""
eTRAKiT building permit scraper.

Enumerates building permits by ID pattern (BLDG{YY}-{NNNN}).
Currently configured for Oceanside; reads permit config from agencies.yaml.

Usage:
    python etrakit.py fetch                     # scan current year, resume from last
    python etrakit.py fetch --year 2025         # scan specific year
    python etrakit.py fetch --full              # full rescan
    python etrakit.py fetch --agency oceanside  # explicit agency

Requires: requests, beautifulsoup4, pyyaml
"""

import argparse
import json
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

UA = "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"


def fetch_permit(session, base_url, permit_no):
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

    return {
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

    session = requests.Session()
    session.headers["User-Agent"] = UA
    retry_adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.packages.urllib3.util.retry.Retry(
            total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503]
        )
    )
    session.mount("https://", retry_adapter)

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


def main():
    parser = argparse.ArgumentParser(description="eTRAKiT building permit scraper")
    parser.add_argument("command", choices=["fetch"], help="Command to run")
    parser.add_argument("--agency", default="oceanside", help="Agency slug from agencies.yaml")
    parser.add_argument("--year", type=int, default=datetime.now().year, help="Year to scan")
    parser.add_argument("--max-seq", type=int, default=5000, help="Max permit sequence number")
    parser.add_argument("--max-misses", type=int, default=50, help="Stop after N consecutive misses")
    parser.add_argument("--full", action="store_true", help="Full rescan (ignore existing)")
    parser.add_argument("--delay", type=float, default=1.0, help="Seconds between requests")
    args = parser.parse_args()

    if args.command == "fetch":
        cmd_fetch(args)


if __name__ == "__main__":
    main()
