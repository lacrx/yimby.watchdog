#!/usr/bin/env python3
"""
San Diego County Board of Supervisors meeting monitor.

Uses the Legistar OData REST API — no HTML scraping needed.

Usage:
    python sdcounty.py fetch [--years N]   # pull meetings + download agendas/minutes
    python sdcounty.py list                # list all tracked meetings

Requires: requests, pdftotext (CLI)
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

from civic_utils import download_pdf, extract_text, save_json, load_json

DATA_DIR = REPO_ROOT / "data" / "sdcounty"
MEETINGS_DIR = DATA_DIR / "meetings"
DOCS_DIR = DATA_DIR / "documents"
STATE_FILE = DATA_DIR / "state.json"

API_BASE = "https://webapi.legistar.com/v1/sdcounty"

BODIES = {
    181: "Board of Supervisors",
    138: "Board of Supervisors - Land Use",
}


def ensure_dirs():
    for d in [MEETINGS_DIR, DOCS_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    return load_json(STATE_FILE) or {"last_fetch": None, "meetings": {}}


def save_state(state):
    save_json(STATE_FILE, state)


def fetch_events(body_id, min_year):
    url = (
        f"{API_BASE}/events?"
        f"$filter=EventBodyId eq {body_id} "
        f"and EventDate ge datetime'{min_year}-01-01'"
        f"&$orderby=EventDate desc"
        f"&$top=200"
    )
    resp = requests.get(url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 yimby-watchdog/1.0",
        "Accept": "application/json",
    })
    resp.raise_for_status()
    return resp.json()


def cmd_fetch(args):
    ensure_dirs()
    state = load_state()
    min_year = datetime.now().year - args.years + 1

    total = 0
    new = 0

    for body_id, body_name in BODIES.items():
        print(f"Fetching {body_name} (body_id={body_id})...")
        events = fetch_events(body_id, min_year)
        print(f"  {len(events)} events since {min_year}")
        total += len(events)

        for ev in events:
            event_id = str(ev["EventId"])
            date_str = ev["EventDate"][:10]
            mid = f"sdcounty-{event_id}"

            meeting_dir = MEETINGS_DIR / mid
            meeting_dir.mkdir(exist_ok=True)

            meeting = {
                "id": mid,
                "body": f"SD County {body_name}",
                "date": date_str,
                "time": ev.get("EventTime", ""),
                "agency": "San Diego County",
                "event_id": event_id,
                "location": ev.get("EventLocation", ""),
                "agenda_url": ev.get("EventAgendaFile"),
                "minutes_url": ev.get("EventMinutesFile"),
                "video_url": ev.get("EventVideoPath"),
            }

            meta_file = meeting_dir / "meeting.json"
            meta_file.write_text(json.dumps(meeting, indent=2))

            is_new = mid not in state.get("meetings", {})

            if meeting.get("agenda_url"):
                pdf_path = DOCS_DIR / f"{mid}-agenda.pdf"
                txt_path = DOCS_DIR / f"{mid}-agenda.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"  {date_str} {body_name}: downloading agenda...")
                    if download_pdf(meeting["agenda_url"], pdf_path):
                        text = extract_text(pdf_path)
                        if text and is_new:
                            print(f"    Extracted {len(text)} chars")
                    time.sleep(0.3)

            if meeting.get("minutes_url"):
                pdf_path = DOCS_DIR / f"{mid}-minutes.pdf"
                txt_path = DOCS_DIR / f"{mid}-minutes.txt"
                if not txt_path.exists():
                    if is_new:
                        print(f"  {date_str} {body_name}: downloading minutes...")
                    if download_pdf(meeting["minutes_url"], pdf_path):
                        text = extract_text(pdf_path)
                        if text and is_new:
                            print(f"    Extracted {len(text)} chars")
                    time.sleep(0.3)

            state["meetings"][mid] = {
                "body": meeting["body"],
                "date": date_str,
                "fetched": datetime.now().isoformat(),
            }
            if is_new:
                new += 1

    state["last_fetch"] = datetime.now().isoformat()
    save_state(state)
    print(f"\nDone. {total} events total, {new} new. Data in {DATA_DIR}")


def cmd_list(args):
    ensure_dirs()
    state = load_state()

    if not state.get("meetings"):
        print("No meetings tracked. Run 'fetch' first.")
        return

    for mid, info in sorted(state["meetings"].items(), key=lambda x: x[1].get("date", "")):
        body = info.get("body", "?")
        date = info.get("date", "?")
        has_agenda = (DOCS_DIR / f"{mid}-agenda.txt").exists()
        has_minutes = (DOCS_DIR / f"{mid}-minutes.txt").exists()
        flags = []
        if has_agenda:
            flags.append("agenda")
        if has_minutes:
            flags.append("minutes")
        print(f"  {date:12s}  {body:45s}  [{', '.join(flags) or 'metadata only'}]")


def main():
    parser = argparse.ArgumentParser(description="SD County Board of Supervisors monitor")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="Pull meetings and download documents")
    p_fetch.add_argument("--years", type=int, default=1, help="How many years back (default: 1)")

    sub.add_parser("list", help="List tracked meetings")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    {"fetch": cmd_fetch, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    main()
