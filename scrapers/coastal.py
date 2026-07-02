#!/usr/bin/env python3
"""
California Coastal Commission meeting monitor.

Uses the CCC JSON API — filters for San Diego district and Oceanside items.

Usage:
    python coastal.py fetch [--years N]   # pull agendas, filter for Oceanside items
    python coastal.py list                # list tracked meetings with Oceanside items

Requires: requests
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests

from civic_utils import download_pdf, extract_text, save_json, load_json, agency_data_dir

DATA_DIR = agency_data_dir("coastal")
MEETINGS_DIR = DATA_DIR / "meetings"
DOCS_DIR = DATA_DIR / "documents"
STATE_FILE = DATA_DIR / "state.json"

API_BASE = "https://api.coastal.ca.gov/agendas/v1"

WATCH_KEYWORDS = [
    "Oceanside", "LCPA", "LCP Amendment",
    "North County", "San Luis Rey",
]

SD_DISTRICT_CLASSES = ["san-diego"]


def ensure_dirs():
    for d in [MEETINGS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    return load_json(STATE_FILE) or {"last_fetch": None, "meetings": {}}


def save_state(state):
    save_json(STATE_FILE, state)


def fetch_agenda(year, month):
    url = f"{API_BASE}/{year}/{month}"
    try:
        resp = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 yimby-watchdog/1.0",
        })
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        return data[0] if isinstance(data, list) else data
    except (requests.RequestException, json.JSONDecodeError):
        return None


def find_relevant_items(agenda):
    items = []
    for day in agenda.get("days", []):
        date = day.get("date", "")
        for district in day.get("districts", []):
            district_class = district.get("class", "")
            is_sd = district_class in SD_DISTRICT_CLASSES
            is_statewide = district_class == "statewide"

            for cat in district.get("categories", []):
                title = cat.get("title", {}).get("english", "")
                blurb = cat.get("blurb", {}).get("english", "")
                result = cat.get("result")
                assets = cat.get("assets", [])
                combined = f"{title} {blurb}"

                is_relevant = is_sd or any(kw in combined for kw in WATCH_KEYWORDS)

                if is_relevant:
                    items.append({
                        "date": date,
                        "district": district.get("name", {}).get("english", district_class),
                        "title": title,
                        "blurb": blurb[:500] if blurb else "",
                        "result": result,
                        "assets": [{"name": a["name"], "url": a["url"]} for a in assets if a.get("url")],
                        "is_oceanside": any(kw in combined for kw in ["Oceanside"]),
                    })

                for sub in cat.get("items", []):
                    sub_title = sub.get("title", {}).get("english", "")
                    sub_blurb = sub.get("blurb", {}).get("english", "")
                    sub_combined = f"{sub_title} {sub_blurb}"
                    sub_relevant = is_sd or any(kw in sub_combined for kw in WATCH_KEYWORDS)

                    if sub_relevant:
                        items.append({
                            "date": date,
                            "district": district.get("name", {}).get("english", district_class),
                            "title": f"{title} > {sub_title}" if sub_title else title,
                            "blurb": sub_blurb[:500] if sub_blurb else blurb[:500] if blurb else "",
                            "result": sub.get("result") or result,
                            "assets": [{"name": a["name"], "url": a["url"]} for a in sub.get("assets", []) if a.get("url")],
                            "is_oceanside": any(kw in sub_combined for kw in ["Oceanside"]),
                        })

    return items


def cmd_fetch(args):
    ensure_dirs()
    state = load_state()
    min_year = datetime.now().year - args.years + 1
    now = datetime.now()

    total_items = 0
    oceanside_items = 0

    for year in range(min_year, now.year + 1):
        max_month = 12 if year < now.year else now.month
        for month in range(1, max_month + 1):
            mid = f"ccc-{year}-{month:02d}"

            print(f"Fetching CCC {year}/{month:02d}...", end=" ", flush=True)
            agenda = fetch_agenda(year, month)
            if not agenda:
                print("no data")
                continue

            meeting_dir = MEETINGS_DIR / mid
            meeting_dir.mkdir(exist_ok=True)

            venues = agenda.get("venues", [])
            venue = venues[0] if venues else {}

            meeting = {
                "id": mid,
                "body": "California Coastal Commission",
                "date": f"{year}-{month:02d}",
                "month": agenda.get("month", ""),
                "agency": "California Coastal Commission",
                "location": f"{venue.get('city', '')} {venue.get('name', '')}".strip(),
            }

            items = find_relevant_items(agenda)
            meeting["sd_items"] = len(items)
            meeting["oceanside_items"] = sum(1 for i in items if i.get("is_oceanside"))

            meta_file = meeting_dir / "meeting.json"
            meta_file.write_text(json.dumps(meeting, indent=2))

            items_file = meeting_dir / "items.json"
            items_file.write_text(json.dumps(items, indent=2))

            total_items += len(items)
            oceanside_items += meeting["oceanside_items"]

            print(f"{len(items)} SD items, {meeting['oceanside_items']} Oceanside")

            for item in items:
                if not item.get("is_oceanside"):
                    continue
                for asset in item.get("assets", []):
                    url = asset.get("url", "")
                    if not url:
                        continue
                    name = asset.get("name", "report").lower().replace(" ", "-")
                    safe_title = "".join(c if c.isalnum() or c in "-_" else "_" for c in item["title"][:60])
                    pdf_path = DOCS_DIR / f"{mid}-{safe_title}-{name}.pdf"
                    txt_path = pdf_path.with_suffix(".txt")
                    if not txt_path.exists():
                        print(f"    Downloading {name}: {item['title'][:80]}...")
                        if download_pdf(url, pdf_path):
                            text = extract_text(pdf_path)
                            if text:
                                print(f"      Extracted {len(text)} chars")
                        time.sleep(0.3)

            state["meetings"][mid] = {
                "body": "CCC",
                "date": f"{year}-{month:02d}",
                "sd_items": len(items),
                "oceanside_items": meeting["oceanside_items"],
                "fetched": now.isoformat(),
            }

            time.sleep(0.5)

    state["last_fetch"] = now.isoformat()
    save_state(state)
    print(f"\nDone. {total_items} SD district items, {oceanside_items} Oceanside-specific.")


def cmd_list(args):
    ensure_dirs()
    state = load_state()

    if not state.get("meetings"):
        print("No meetings tracked. Run 'fetch' first.")
        return

    for mid, info in sorted(state["meetings"].items(), key=lambda x: x[1].get("date", "")):
        date = info.get("date", "?")
        sd = info.get("sd_items", 0)
        oc = info.get("oceanside_items", 0)
        marker = " *** OCEANSIDE ***" if oc > 0 else ""
        print(f"  {date}  CCC  [{sd} SD items, {oc} Oceanside]{marker}")


def main():
    parser = argparse.ArgumentParser(description="California Coastal Commission monitor")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="Pull CCC agendas, filter for SD/Oceanside items")
    p_fetch.add_argument("--years", type=int, default=1, help="How many years back (default: 1)")

    sub.add_parser("list", help="List tracked meetings")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    {"fetch": cmd_fetch, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    main()
