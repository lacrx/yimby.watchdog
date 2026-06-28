#!/usr/bin/env python3
"""
SANDAG meeting monitor — scrapes Granicus for Board of Directors,
Regional Planning Committee, and Transportation Committee meetings.

Usage:
    python sandag.py fetch [--years N]   # pull meetings + download agenda packets
    python sandag.py list                # list all tracked meetings
    python sandag.py search TERM         # full-text search across documents

Requires: requests, beautifulsoup4, lxml, pdftotext (CLI)
"""

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from civic_utils import download_pdf, extract_text, save_json, load_json

DATA_DIR = Path(__file__).parent / "data" / "sandag"
MEETINGS_DIR = DATA_DIR / "meetings"
DOCS_DIR = Path(__file__).parent / "data" / "documents"
STATE_FILE = DATA_DIR / "state.json"

GRANICUS_BASE = "https://sandag.granicus.com"

BODIES = {
    "Board of Directors": 1,
    "Regional Planning Committee": 7,
    "Transportation Committee": 3,
}


def ensure_dirs():
    for d in [MEETINGS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    return load_json(STATE_FILE) or {"last_fetch": None, "meetings": {}}


def save_state(state):
    save_json(STATE_FILE, state)


def fetch_granicus_page(view_id):
    resp = requests.get(
        f"{GRANICUS_BASE}/ViewPublisher.php?view_id={view_id}",
        timeout=30,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"},
    )
    resp.raise_for_status()
    return resp.text


def parse_meetings(html, body_name, min_year=None):
    soup = BeautifulSoup(html, "lxml")
    meetings = []

    for row in soup.select("tr.listingRow"):
        cells = row.find_all("td", class_="listItem")
        if len(cells) < 5:
            continue

        name = cells[0].get_text(strip=True)
        date_str = cells[1].get_text(strip=True)

        if "canceled" in name.lower() or "cancelled" in name.lower():
            continue

        dt = None
        date_clean = re.sub(r"[\s\xa0]+", " ", date_str)
        m = re.match(r"([A-Za-z]+ \d{1,2}, \d{4})", date_clean)
        if m:
            try:
                dt = datetime.strptime(m.group(1), "%b %d, %Y")
            except ValueError:
                try:
                    dt = datetime.strptime(m.group(1), "%B %d, %Y")
                except ValueError:
                    continue
        if not dt:
            continue

        if min_year and dt.year < min_year:
            continue

        agenda_a = cells[2].find("a", href=True)
        minutes_a = cells[3].find("a", href=True)
        packet_a = cells[4].find("a", href=True)

        agenda_url = None
        if agenda_a:
            href = agenda_a["href"]
            if href.startswith("//"):
                href = "https:" + href
            agenda_url = href

        minutes_url = minutes_a["href"] if minutes_a else None
        packet_url = packet_a["href"] if packet_a else None

        event_id = None
        if agenda_url:
            eid = re.search(r"event_id=(\d+)", agenda_url)
            if eid:
                event_id = eid.group(1)

        meeting_id = hashlib.md5(
            f"sandag-{body_name}-{dt.strftime('%Y-%m-%d')}".encode()
        ).hexdigest()[:12]

        meetings.append({
            "id": meeting_id,
            "body": f"SANDAG {body_name}",
            "date": dt.strftime("%Y-%m-%d"),
            "date_display": date_str,
            "agency": "SANDAG",
            "agenda_url": agenda_url,
            "minutes_url": minutes_url,
            "packet_url": packet_url,
            "event_id": event_id,
        })

    return meetings


def cmd_fetch(args):
    ensure_dirs()
    state = load_state()

    min_year = datetime.now().year - args.years + 1

    total = 0
    new = 0

    for body_name, view_id in BODIES.items():
        print(f"Fetching {body_name} (view_id={view_id})...")
        html = fetch_granicus_page(view_id)
        meetings = parse_meetings(html, body_name, min_year=min_year)
        print(f"  {len(meetings)} meetings since {min_year}")
        total += len(meetings)

        for m in meetings:
            mid = m["id"]
            meeting_dir = MEETINGS_DIR / mid
            meeting_dir.mkdir(exist_ok=True)

            meta_file = meeting_dir / "meeting.json"
            meta_file.write_text(json.dumps(m, indent=2))

            is_new = mid not in state.get("meetings", {})

            if m.get("packet_url"):
                pdf_path = DOCS_DIR / f"{mid}-agenda-packet.pdf"
                txt_path = DOCS_DIR / f"{mid}-agenda-packet.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"  {m['date']} {body_name}: downloading packet...")
                    if download_pdf(m["packet_url"], pdf_path):
                        text = extract_text(pdf_path)
                        if text:
                            if is_new:
                                print(f"    Extracted {len(text)} chars")
                    else:
                        if is_new:
                            print(f"    Download failed")
                    time.sleep(0.5)

            if m.get("minutes_url"):
                pdf_path = DOCS_DIR / f"{mid}-minutes.pdf"
                txt_path = DOCS_DIR / f"{mid}-minutes.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"  {m['date']} {body_name}: downloading minutes...")
                    if download_pdf(m["minutes_url"], pdf_path):
                        text = extract_text(pdf_path)
                        if text:
                            if is_new:
                                print(f"    Extracted {len(text)} chars")
                    time.sleep(0.5)

            state["meetings"][mid] = {
                "body": m["body"],
                "date": m["date"],
                "fetched": datetime.now().isoformat(),
            }
            if is_new:
                new += 1

    state["last_fetch"] = datetime.now().isoformat()
    save_state(state)
    print(f"\nDone. {total} meetings total, {new} new. Data in {DATA_DIR}")


def cmd_list(args):
    ensure_dirs()
    state = load_state()

    if not state.get("meetings"):
        print("No meetings tracked. Run 'fetch' first.")
        return

    for mid, info in sorted(state["meetings"].items(), key=lambda x: x[1].get("date", "")):
        body = info.get("body", "?")
        date = info.get("date", "?")
        has_packet = (DOCS_DIR / f"{mid}-agenda-packet.txt").exists()
        has_minutes = (DOCS_DIR / f"{mid}-minutes.txt").exists()
        flags = []
        if has_packet:
            flags.append("packet")
        if has_minutes:
            flags.append("minutes")
        print(f"  {date:12s}  {body:40s}  [{', '.join(flags) or 'metadata only'}]")


def cmd_search(args):
    ensure_dirs()
    query = " ".join(args.terms).lower()
    hits = 0

    for fpath in sorted(DOCS_DIR.glob("*.txt")):
        if not fpath.stem.startswith(tuple(
            m for m in (load_state().get("meetings", {}))
        )):
            continue
        text = fpath.read_text()
        if query not in text.lower():
            continue
        hits += 1
        print(f"\n{'='*60}")
        print(f"MATCH: {fpath.name}")
        for i, line in enumerate(text.split("\n")):
            if query in line.lower():
                print(f"  L{i+1}: {line.strip()[:120]}")
                break
    print(f"\n{hits} file(s) matched '{query}'")


def main():
    parser = argparse.ArgumentParser(description="SANDAG meeting monitor")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="Pull SANDAG meetings and download packets")
    p_fetch.add_argument("--years", type=int, default=1, help="How many years back (default: 1)")

    sub.add_parser("list", help="List tracked meetings")

    p_search = sub.add_parser("search", help="Full-text search")
    p_search.add_argument("terms", nargs="+")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    {"fetch": cmd_fetch, "list": cmd_list, "search": cmd_search}[args.command](args)


if __name__ == "__main__":
    main()
