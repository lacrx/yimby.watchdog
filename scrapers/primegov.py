#!/usr/bin/env python3
"""
PrimeGov meeting scraper — generic for any PrimeGov-hosted agency.

Reads agency config from agencies.yaml. Fetches meeting listings via the
PrimeGov v2 API, downloads agenda/minutes PDFs, extracts text.

Usage:
    python primegov.py fetch --agency coronado [--years N]
    python primegov.py list --agency coronado
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    safe_filename, agency_data_dir, load_agencies, rebuild_doc_index,
    cmd_list_meetings, log_discovery,
)


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
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0",
        "Accept": "application/json",
    })
    return session


def make_meeting_id(slug, title, date_time):
    return hashlib.md5(
        f"{slug}-{title}-{date_time}".encode()
    ).hexdigest()[:12]


def fetch_meetings_for_year(session, base_url, year):
    url = f"{base_url}/api/v2/PublicPortal/ListArchivedMeetings?year={year}"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def fetch_upcoming_meetings(session, base_url):
    url = f"{base_url}/api/v2/PublicPortal/ListUpcomingMeetings"
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.json()


def normalize_meeting(raw, slug, agency_name, base_url):
    title = raw.get("title", "")
    date_time = raw.get("dateTime", "")
    mid = make_meeting_id(slug, title, date_time)

    try:
        dt = datetime.fromisoformat(date_time)
        date_str = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        date_str = date_time[:10] if date_time else "unknown"

    docs = []
    for doc in raw.get("documentList", []):
        if doc.get("compileOutputType") == 1:
            doc_id = doc.get("id")
            if doc_id:
                docs.append({
                    "id": doc_id,
                    "name": doc.get("templateName", "document"),
                    "url": f"{base_url}/Portal/Meeting?compiledMeetingDocumentFileId={doc_id}",
                })

    return {
        "id": mid,
        "body": title,
        "date": date_str,
        "date_display": date_time,
        "agency": agency_name,
        "primegov_id": raw.get("id"),
        "committee_id": raw.get("committeeId"),
        "documents": docs,
    }


def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = getattr(args, "agency", None) or "coronado"
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    agency_name = cfg.get("name", slug)
    base_url = cfg.get("base_url", "").rstrip("/")

    data_dir, meetings_dir, docs_dir, state_file = _setup_dirs(slug)
    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}
    session = create_session()

    now = datetime.now()
    all_meetings = []

    # Archived meetings by year
    for year in range(now.year, now.year - args.years, -1):
        print(f"  Fetching {year} archived meetings...")
        try:
            raw_list = fetch_meetings_for_year(session, base_url, year)
            print(f"    {len(raw_list)} meetings")
            for raw in raw_list:
                all_meetings.append(normalize_meeting(raw, slug, agency_name, base_url))
            time.sleep(0.3)
        except Exception as e:
            print(f"    Failed: {e}")

    # Upcoming meetings
    print("  Fetching upcoming meetings...")
    try:
        raw_list = fetch_upcoming_meetings(session, base_url)
        print(f"    {len(raw_list)} upcoming meetings")
        for raw in raw_list:
            all_meetings.append(normalize_meeting(raw, slug, agency_name, base_url))
    except Exception as e:
        print(f"    Failed: {e}")

    # Deduplicate by meeting ID
    seen = {}
    for m in all_meetings:
        seen[m["id"]] = m
    all_meetings = list(seen.values())

    total = len(all_meetings)
    new = 0

    print(f"Fetching {agency_name} documents (PrimeGov)...")
    for m in all_meetings:
        mid = m["id"]
        meeting_dir = meetings_dir / mid
        meeting_dir.mkdir(exist_ok=True)

        meta_file = meeting_dir / "meeting.json"
        meta_file.write_text(json.dumps(m, indent=2))

        is_new = mid not in state.get("meetings", {})

        for doc in m.get("documents", []):
            fname = safe_filename(doc["name"], max_len=60)
            pdf_path = docs_dir / f"{mid}-{fname}.pdf"
            txt_path = pdf_path.with_suffix(".txt")
            if not txt_path.exists():
                if is_new:
                    print(f"    {m['date']} {m['body']}: {doc['name'][:50]}...")
                if download_pdf(doc["url"], pdf_path):
                    text = extract_text(pdf_path)
                    if text and is_new:
                        print(f"      Extracted {len(text)} chars")
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
    slug = getattr(args, "agency", None) or "coronado"
    cmd_list_meetings(slug)


def main():
    parser = argparse.ArgumentParser(description="PrimeGov meeting scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", default="coronado")
    p_fetch.add_argument("--years", type=int, default=1)

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", default="coronado")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    {"fetch": cmd_fetch, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    main()
