#!/usr/bin/env python3
"""
CivicPlus AgendaCenter scraper — generic for any CivicPlus-hosted agency.

Reads agency config from agencies.yaml. Fetches meeting listings from the
AgendaCenter page, downloads agenda/packet PDFs, extracts text.

Usage:
    python civicplus.py fetch --agency del_mar [--years N] [--deep]
    python civicplus.py list --agency del_mar

Requires: requests, beautifulsoup4, lxml
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir, USER_AGENT,
    safe_filename, cmd_list_meetings, rebuild_doc_index, log_discovery,
)


def fetch_agenda_page(base_url, start_date, end_date):
    """Fetch AgendaCenter page filtered by date range."""
    url = f"{base_url}/AgendaCenter/Search"
    params = {
        "term": "",
        "CIDs": "all",
        "startDate": start_date.strftime("%m/%d/%Y"),
        "endDate": end_date.strftime("%m/%d/%Y"),
    }
    resp = requests.get(url, params=params, timeout=30,
                        headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def parse_agenda_page(html, base_url):
    """Parse AgendaCenter HTML into grouped meeting items."""
    soup = BeautifulSoup(html, "lxml")
    content = soup.select_one(".contentMain") or soup

    current_body = "Unknown"
    raw_items = []

    for el in content.find_all(["h2", "tr"]):
        if el.name == "h2":
            text = el.get_text(strip=True)
            if text and len(text) > 2:
                current_body = text
            continue

        if "catAgendaRow" not in el.get("class", []):
            continue

        links = el.select("a[href*=ViewFile]")
        if not links:
            continue

        href = links[0].get("href", "")
        title = links[0].get_text(strip=True)

        date_m = re.search(r"_(\d{2})(\d{2})(\d{4})-(\d+)", href)
        if not date_m:
            continue

        mo, day, yr, item_id = date_m.groups()
        date_str = f"{mo}/{day}/{yr}"
        full_url = urljoin(base_url + "/", href.lstrip("/"))

        raw_items.append({
            "body": current_body,
            "date": date_str,
            "title": title,
            "item_id": item_id,
            "url": full_url,
        })

    # Group by (body, date) into meetings
    meetings = {}
    for item in raw_items:
        key = (item["body"], item["date"])
        if key not in meetings:
            meetings[key] = {
                "body": item["body"],
                "date": item["date"],
                "documents": [],
                "meeting_id": hashlib.md5(
                    f"{item['body']}-{item['date']}".encode()
                ).hexdigest()[:12],
            }
        meetings[key]["documents"].append({
            "title": item["title"],
            "item_id": item["item_id"],
            "url": item["url"],
        })

    return list(meetings.values())



def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    if cfg["platform"] != "civicplus":
        print(f"{slug} is not a CivicPlus agency (platform: {cfg['platform']})")
        sys.exit(1)

    base_url = cfg["base_url"].rstrip("/")
    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    end_date = datetime.now()
    start_date = end_date - timedelta(days=365 * years)

    print(f"Fetching {cfg['name']} meetings (CivicPlus AgendaCenter)...")
    html = fetch_agenda_page(base_url, start_date, end_date)
    meetings = parse_agenda_page(html, base_url)
    print(f"  {len(meetings)} meetings found ({start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')})")

    new_count = 0
    doc_count = 0

    for meeting in meetings:
        mid = meeting["meeting_id"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        if not meeting_file.exists():
            meeting_meta = {
                "body": meeting["body"],
                "date": meeting["date"],
                "id": mid,
                "source": "civicplus",
                "agency": slug,
                "documents": [
                    {"title": d["title"], "item_id": d["item_id"]}
                    for d in meeting["documents"]
                ],
            }
            save_json(meeting_file, meeting_meta)
            new_count += 1
            print(f"  NEW: {meeting['date']} {meeting['body']}")

        if mid in state["meetings"] and not args.deep:
            continue

        for doc in meeting["documents"]:
            title = doc["title"]
            is_packet = "packet" in title.lower()

            if not args.deep and not is_packet and len(meeting["documents"]) > 1:
                # Without --deep, skip non-packet docs when multiple exist
                # (agenda packet is the comprehensive one, others are supplements)
                is_agenda = "agenda" in title.lower() and "packet" not in title.lower()
                is_red_dot = "red dot" in title.lower()
                if not is_agenda:
                    continue

            fname = f"{mid}-{doc['item_id']}-{safe_filename(title)}.pdf"
            dest = docs_dir / fname
            if dest.exists():
                continue

            if download_pdf(doc["url"], dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.5)

        state["meetings"][mid] = {
            "fetched": datetime.now().isoformat(),
            "body": meeting["body"],
            "date": meeting["date"],
        }

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(slug, state, docs_dir)
    save_json(state_file, state)
    log_discovery(slug, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(args.agency)


def main():
    parser = argparse.ArgumentParser(description="CivicPlus AgendaCenter scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", required=True)
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", required=True)

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
