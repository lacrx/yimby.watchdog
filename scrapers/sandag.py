#!/usr/bin/env python3
"""
SANDAG meeting monitor — fetches from eScribe platform for Board of Directors,
Regional Planning Committee, and Transportation Committee meetings.

Usage:
    python sandag.py fetch [--years N] [--deep]  # pull meetings + download documents
    python sandag.py list                        # list all tracked meetings
    python sandag.py search TERM                 # full-text search across documents

Requires: requests, beautifulsoup4, lxml, pdftotext (CLI)
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib3
from datetime import datetime
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import download_pdf, extract_text, save_json, load_json

DATA_DIR = REPO_ROOT / "data" / "sandag"
MEETINGS_DIR = DATA_DIR / "meetings"
DOCS_DIR = REPO_ROOT / "data" / "documents"
STATE_FILE = DATA_DIR / "state.json"

ESCRIBE_BASE = "https://pub-sandag.escribemeetings.com"
PAST_MEETINGS_URL = f"{ESCRIBE_BASE}/MeetingsCalendarView.aspx/PastMeetings"

BODIES = [
    "Board of Directors",
    "Regional Planning Committee",
    "Transportation Committee",
]


def ensure_dirs():
    for d in [MEETINGS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    return load_json(STATE_FILE) or {"last_fetch": None, "meetings": {}}


def save_state(state):
    save_json(STATE_FILE, state)


def create_session():
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0",
        "Content-Type": "application/json",
    })
    return session


def parse_escribe_date(start_field):
    """Parse /Date(milliseconds)/ format from eScribe API."""
    m = re.search(r"/Date\((\d+)\)/", start_field)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000)
    return None


def make_meeting_id(body_name, date_str):
    """Generate deterministic meeting ID matching Granicus-era formula."""
    return hashlib.md5(
        f"sandag-{body_name}-{date_str}".encode()
    ).hexdigest()[:12]


def extract_document_urls(meeting_data):
    """Pull agenda, minutes, and video URLs from MeetingLinks."""
    links = meeting_data.get("MeetingLinks", [])
    agenda_url = None
    minutes_url = None
    video_url = None
    meeting_page_url = None

    for link in links:
        link_type = link.get("Type", "")
        fmt = link.get("Format", "")
        url = link.get("Url", "")

        if link_type == "Agenda" and fmt == ".pdf":
            agenda_url = url
        elif link_type == "AgendaCover" and fmt == ".pdf" and not agenda_url:
            agenda_url = url
        elif link_type == "PostMinutes" and fmt == ".pdf":
            minutes_url = url
        elif link_type == "Video":
            video_url = url
        elif link_type == "PostAgenda" and fmt == "HTML":
            meeting_page_url = url

    def full_url(u):
        if not u:
            return None
        if u.startswith("http"):
            return u
        return f"{ESCRIBE_BASE}/{u}"

    return {
        "agenda_url": full_url(agenda_url),
        "minutes_url": full_url(minutes_url),
        "video_url": full_url(video_url),
        "meeting_page_url": full_url(meeting_page_url),
    }


def normalize_meeting(api_meeting, body_name):
    """Convert eScribe API meeting to standard meeting.json format."""
    dt = parse_escribe_date(api_meeting.get("Start", ""))
    if not dt:
        return None

    date_str = dt.strftime("%Y-%m-%d")
    mid = make_meeting_id(body_name, date_str)
    urls = extract_document_urls(api_meeting)

    return {
        "id": mid,
        "body": f"SANDAG {body_name}",
        "date": date_str,
        "date_display": api_meeting.get("FormattedStart", ""),
        "agency": "SANDAG",
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "escribe_id": api_meeting.get("Id"),
        "escribe_meeting_url": api_meeting.get("MeetingUrl"),
    }


def fetch_past_meetings(session, body_name, min_year):
    """Paginate PastMeetings API, filter by year, skip cancelled."""
    all_meetings = []
    page = 1

    while True:
        resp = session.post(PAST_MEETINGS_URL, json={
            "type": body_name,
            "pageNumber": page,
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json().get("d", {})
        meetings = data.get("Meetings", [])
        total_count = data.get("TotalCount", 0)

        if not meetings:
            break

        oldest_year = None
        for m in meetings:
            if m.get("Cancelled"):
                continue
            year = m.get("Year")
            if year and year < min_year:
                oldest_year = year
                continue
            normalized = normalize_meeting(m, body_name)
            if normalized:
                all_meetings.append(normalized)
            if year:
                oldest_year = year

        fetched_so_far = page * 50
        if fetched_so_far >= total_count:
            break
        if oldest_year and oldest_year < min_year:
            break

        page += 1
        time.sleep(0.3)

    return all_meetings


def fetch_meeting_items(session, escribe_id):
    """Fetch meeting page and extract agenda item attachments for --deep mode."""
    url = f"{ESCRIBE_BASE}/Meeting.aspx?Id={escribe_id}&Agenda=PostAgenda&lang=English"
    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        print(f"    Failed to fetch meeting page: {e}")
        return []

    soup = BeautifulSoup(resp.text, "lxml")
    items = []

    for item_div in soup.select(".AgendaItemContainer, .agenda-item"):
        title_el = item_div.select_one(".AgendaItemTitle a, .agenda-item-title a")
        title = title_el.get_text(strip=True) if title_el else ""

        attachments = []
        for link in item_div.select("a[href*='FileStream.ashx']"):
            href = link.get("href", "")
            name = link.get_text(strip=True) or "attachment"
            if href:
                if not href.startswith("http"):
                    href = f"{ESCRIBE_BASE}/{href}"
                attachments.append({"name": name, "url": href})

        if attachments:
            items.append({"title": title, "attachments": attachments})

    return items


def sanitize_filename(name, max_len=60):
    """Clean a string for use in filenames."""
    name = re.sub(r'[^\w\s-]', '', name)
    name = re.sub(r'\s+', '_', name.strip())
    return name[:max_len] or "doc"


def cmd_fetch(args):
    ensure_dirs()
    state = load_state()
    session = create_session()

    min_year = datetime.now().year - args.years + 1
    deep = getattr(args, "deep", False)

    total = 0
    new = 0

    for body_name in BODIES:
        print(f"Fetching {body_name}...")
        meetings = fetch_past_meetings(session, body_name, min_year)
        print(f"  {len(meetings)} meetings since {min_year}")
        total += len(meetings)

        for m in meetings:
            mid = m["id"]
            meeting_dir = MEETINGS_DIR / mid
            meeting_dir.mkdir(exist_ok=True)

            meta_file = meeting_dir / "meeting.json"
            meta_file.write_text(json.dumps(m, indent=2))

            is_new = mid not in state.get("meetings", {})

            if m.get("agenda_url"):
                pdf_path = DOCS_DIR / f"{mid}-agenda-packet.pdf"
                txt_path = DOCS_DIR / f"{mid}-agenda-packet.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"  {m['date']} {body_name}: downloading agenda...")
                    if download_pdf(m["agenda_url"], pdf_path, verify=False):
                        text = extract_text(pdf_path)
                        if text and is_new:
                            print(f"    Extracted {len(text)} chars")
                    elif is_new:
                        print(f"    Download failed")
                    time.sleep(0.5)

            if m.get("minutes_url"):
                pdf_path = DOCS_DIR / f"{mid}-minutes.pdf"
                txt_path = DOCS_DIR / f"{mid}-minutes.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"  {m['date']} {body_name}: downloading minutes...")
                    if download_pdf(m["minutes_url"], pdf_path, verify=False):
                        text = extract_text(pdf_path)
                        if text and is_new:
                            print(f"    Extracted {len(text)} chars")
                    time.sleep(0.5)

            if deep and m.get("escribe_id"):
                items = fetch_meeting_items(session, m["escribe_id"])
                for i, item in enumerate(items, 1):
                    for att in item["attachments"]:
                        fname = sanitize_filename(att["name"])
                        pdf_path = DOCS_DIR / f"{mid}-{i:02d}-{fname}.pdf"
                        txt_path = pdf_path.with_suffix(".txt")
                        if not txt_path.exists():
                            if is_new:
                                print(f"    Item {i}: {att['name'][:60]}...")
                            if download_pdf(att["url"], pdf_path, verify=False):
                                extract_text(pdf_path)
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
    parser = argparse.ArgumentParser(description="SANDAG meeting monitor (eScribe)")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="Pull SANDAG meetings and download documents")
    p_fetch.add_argument("--years", type=int, default=1, help="How many years back (default: 1)")
    p_fetch.add_argument("--deep", action="store_true", help="Also download agenda item attachments")

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
