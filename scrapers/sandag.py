#!/usr/bin/env python3
"""
eScribe meeting scraper — generic for any eScribe-hosted agency.

Reads agency config from agencies.yaml. Fetches meeting listings via the
eScribe JSON API, downloads agenda/minutes PDFs, extracts text.

Usage:
    python sandag.py fetch --agency sandag [--years N] [--deep]
    python sandag.py fetch [--years N] [--deep]   # defaults to sandag
    python sandag.py list --agency sandag

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

from civic_utils import (
    download_pdf, extract_text, save_json, load_json, parse_escribe_date,
    safe_filename, agency_data_dir, load_agencies, rebuild_doc_index,
    cmd_list_meetings, log_discovery,
)

SANDAG_BODIES = [
    "Board of Directors",
    "Regional Planning Committee",
    "Transportation Committee",
]


def _setup_dirs(slug):
    data_dir = agency_data_dir(slug)
    meetings_dir = data_dir / "meetings"
    docs_dir = data_dir / "documents"
    state_file = data_dir / "state.json"
    for d in [meetings_dir, docs_dir]:
        d.mkdir(parents=True, exist_ok=True)
    return data_dir, meetings_dir, docs_dir, state_file


def create_session():
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0",
        "Content-Type": "application/json",
    })
    return session



def make_meeting_id(slug, body_name, date_str):
    """Generate deterministic meeting ID."""
    return hashlib.md5(
        f"{slug}-{body_name}-{date_str}".encode()
    ).hexdigest()[:12]


def extract_document_urls(meeting_data, escribe_base):
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
        return f"{escribe_base}/{u}"

    return {
        "agenda_url": full_url(agenda_url),
        "minutes_url": full_url(minutes_url),
        "video_url": full_url(video_url),
        "meeting_page_url": full_url(meeting_page_url),
    }


def normalize_meeting(api_meeting, slug, agency_name, body_name, escribe_base):
    """Convert eScribe API meeting to standard meeting.json format."""
    dt = parse_escribe_date(api_meeting.get("Start", ""))
    if not dt:
        return None

    date_str = dt.strftime("%Y-%m-%d")
    mid = make_meeting_id(slug, body_name, date_str)
    urls = extract_document_urls(api_meeting, escribe_base)

    return {
        "id": mid,
        "body": f"{agency_name} {body_name}",
        "date": date_str,
        "date_display": api_meeting.get("FormattedStart", ""),
        "agency": agency_name,
        "agenda_url": urls["agenda_url"],
        "minutes_url": urls["minutes_url"],
        "video_url": urls["video_url"],
        "escribe_id": api_meeting.get("Id"),
        "escribe_meeting_url": api_meeting.get("MeetingUrl"),
    }


def fetch_past_meetings(session, escribe_base, slug, agency_name, body_name, min_year):
    """Paginate PastMeetings API, filter by year, skip cancelled."""
    past_url = f"{escribe_base}/MeetingsCalendarView.aspx/PastMeetings"
    all_meetings = []
    page = 1

    while True:
        resp = session.post(past_url, json={
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
            normalized = normalize_meeting(m, slug, agency_name, body_name, escribe_base)
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


def fetch_meeting_items(session, escribe_base, escribe_id):
    """Fetch meeting page and extract agenda item attachments for --deep mode."""
    url = f"{escribe_base}/Meeting.aspx?Id={escribe_id}&Agenda=PostAgenda&lang=English"
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
                    href = f"{escribe_base}/{href}"
                attachments.append({"name": name, "url": href})

        if attachments:
            items.append({"title": title, "attachments": attachments})

    return items



def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = getattr(args, "agency", None) or "sandag"
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    agency_name = cfg.get("name", slug)
    escribe_base = cfg.get("base_url", "").rstrip("/")
    bodies = cfg.get("bodies", SANDAG_BODIES)

    data_dir, meetings_dir, docs_dir, state_file = _setup_dirs(slug)
    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}
    session = create_session()

    min_year = datetime.now().year - args.years + 1
    deep = getattr(args, "deep", False)

    total = 0
    new = 0

    print(f"Fetching {agency_name} meetings (eScribe)...")
    for body_name in bodies:
        print(f"  {body_name}...")
        meetings = fetch_past_meetings(session, escribe_base, slug, agency_name, body_name, min_year)
        print(f"    {len(meetings)} meetings since {min_year}")
        total += len(meetings)

        for m in meetings:
            mid = m["id"]
            meeting_dir = meetings_dir / mid
            meeting_dir.mkdir(exist_ok=True)

            meta_file = meeting_dir / "meeting.json"
            meta_file.write_text(json.dumps(m, indent=2))

            is_new = mid not in state.get("meetings", {})

            if m.get("agenda_url"):
                pdf_path = docs_dir / f"{mid}-agenda-packet.pdf"
                txt_path = docs_dir / f"{mid}-agenda-packet.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"    {m['date']} {body_name}: downloading agenda...")
                    if download_pdf(m["agenda_url"], pdf_path, verify=False):
                        text = extract_text(pdf_path)
                        if text and is_new:
                            print(f"      Extracted {len(text)} chars")
                    elif is_new:
                        print(f"      Download failed")
                    time.sleep(0.5)

            if m.get("minutes_url"):
                pdf_path = docs_dir / f"{mid}-minutes.pdf"
                txt_path = docs_dir / f"{mid}-minutes.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"    {m['date']} {body_name}: downloading minutes...")
                    if download_pdf(m["minutes_url"], pdf_path, verify=False):
                        text = extract_text(pdf_path)
                        if text and is_new:
                            print(f"      Extracted {len(text)} chars")
                    time.sleep(0.5)

            if deep and m.get("escribe_id"):
                items = fetch_meeting_items(session, escribe_base, m["escribe_id"])
                for i, item in enumerate(items, 1):
                    for att in item["attachments"]:
                        fname = safe_filename(att["name"], max_len=60)
                        pdf_path = docs_dir / f"{mid}-{i:02d}-{fname}.pdf"
                        txt_path = pdf_path.with_suffix(".txt")
                        if not txt_path.exists():
                            if is_new:
                                print(f"      Item {i}: {att['name'][:60]}...")
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
    rebuild_doc_index(slug, state, docs_dir)
    save_json(state_file, state)
    log_discovery(slug, meetings_found=total, meetings_new=new)
    print(f"\nDone. {total} meetings total, {new} new.")


def cmd_list(args):
    slug = getattr(args, "agency", None) or "sandag"
    cmd_list_meetings(slug)


def main():
    parser = argparse.ArgumentParser(description="eScribe meeting scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", default="sandag")
    p_fetch.add_argument("--years", type=int, default=1)
    p_fetch.add_argument("--deep", action="store_true")

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", default="sandag")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    {"fetch": cmd_fetch, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    main()
