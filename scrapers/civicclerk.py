#!/usr/bin/env python3
"""
CivicClerk scraper — generic for any CivicClerk-hosted agency.

CivicClerk is a React SPA backed by an OData v2 API at
https://[tenant].api.civicclerk.com/v2/. Fetches events, downloads
agenda/minutes/packet PDFs, extracts text.

Usage:
    python civicclerk.py fetch --agency carlsbad [--years N] [--deep]
    python civicclerk.py probe --agency carlsbad
    python civicclerk.py list --agency carlsbad

Requires: requests
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir,
)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"
HEADERS = {"Accept": "application/json", "User-Agent": USER_AGENT}

# fileType values for GetEventFileStream
FILE_TYPE_AGENDA = 1
FILE_TYPE_MINUTES = 2
FILE_TYPE_AGENDA_PACKET = 4


def api_get(api_base, endpoint, params=None):
    url = f"{api_base}{endpoint}"
    resp = requests.get(url, params=params, timeout=30, headers=HEADERS)
    resp.raise_for_status()
    return resp.json()


def fetch_categories(api_base):
    data = api_get(api_base, "/EventCategories")
    return {c["eventCategoryId"]: c["eventCategoryName"]
            for c in data.get("value", data) if "eventCategoryId" in c}


def fetch_events(api_base, cutoff_date):
    cutoff_str = cutoff_date.strftime("%Y-%m-%dT00:00:00Z")
    params = {
        "$filter": f"eventDate ge {cutoff_str}",
        "$orderby": "eventDate desc",
    }
    data = api_get(api_base, "/Events", params=params)
    return data.get("value", data) if isinstance(data, dict) else data


def download_event_file(api_base, file_id, file_type, dest_path):
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    url = f"{api_base}/Events/GetEventFileStream(fileId={file_id},fileType={file_type})"
    try:
        resp = requests.get(url, timeout=60, headers={"User-Agent": USER_AGENT})
        resp.raise_for_status()
        if b"%PDF" in resp.content[:10]:
            dest_path.write_bytes(resp.content)
            return True
        return False
    except Exception as e:
        print(f"  download failed (fileId={file_id}, type={file_type}): {e}")
        return False


def probe_api(api_base):
    endpoints = ["/EventCategories", "/Events?$top=1"]
    for ep in endpoints:
        url = f"{api_base}{ep}"
        try:
            resp = requests.get(url, timeout=10, headers=HEADERS)
            if resp.status_code == 200 and resp.text.strip():
                return True, ep, resp.status_code
            print(f"  {ep}: HTTP {resp.status_code}")
        except Exception as e:
            print(f"  {ep}: {e}")
    return False, None, None


def cmd_probe(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    api_base = cfg.get("api_base", "")
    if not api_base:
        print(f"No api_base configured for {slug}")
        sys.exit(1)

    print(f"Probing CivicClerk API: {api_base}")
    ok, endpoint, code = probe_api(api_base)
    if ok:
        print(f"  API accessible via {endpoint} (HTTP {code})")
        cats = fetch_categories(api_base)
        print(f"  Bodies: {', '.join(cats.values())}")
    else:
        print(f"  API not accessible.")


def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    api_base = cfg.get("api_base", "")
    if not api_base:
        print(f"No api_base configured for {slug}")
        sys.exit(1)

    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    print(f"CivicClerk fetch for {cfg['name']}...")
    ok, _, _ = probe_api(api_base)
    if not ok:
        print(f"  API not accessible for {slug} — skipping.")
        return

    categories = fetch_categories(api_base)
    print(f"  {len(categories)} event categories")

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    events = fetch_events(api_base, cutoff)
    print(f"  {len(events)} events since {cutoff.strftime('%Y-%m-%d')}")

    new_count = 0
    doc_count = 0

    for event in events:
        eid = event.get("eventId")
        if not eid:
            continue

        cat_id = event.get("eventCategoryId")
        body = categories.get(cat_id, f"Category {cat_id}")
        event_date = event.get("eventDate", "")

        # Parse date from ISO format
        dt = None
        if event_date:
            try:
                dt = datetime.fromisoformat(event_date.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                pass

        mid = f"cc-{eid}"
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        if not meeting_file.exists():
            meeting_meta = {
                "body": body,
                "date": dt.strftime("%m/%d/%Y") if dt else event_date,
                "title": f"{body} — {dt.strftime('%b %d, %Y') if dt else event_date}",
                "id": mid,
                "event_id": eid,
                "source": "civicclerk",
                "agency": slug,
            }
            save_json(meeting_file, meeting_meta)
            new_count += 1
            print(f"  NEW: {body} — {dt.strftime('%Y-%m-%d') if dt else event_date}")

        if mid in state["meetings"] and not args.deep:
            continue

        # Download documents
        has_agenda = event.get("publishedAgenda", False)
        has_minutes = event.get("publishedMinutes", False)
        has_packet = event.get("publishedAgendaPacket", False)

        doc_list = event.get("documentList") or []

        if has_agenda:
            dest = docs_dir / f"{mid}-agenda.pdf"
            if download_event_file(api_base, eid, FILE_TYPE_AGENDA, dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.3)

        if has_minutes:
            dest = docs_dir / f"{mid}-minutes.pdf"
            if download_event_file(api_base, eid, FILE_TYPE_MINUTES, dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.3)

        if args.deep and has_packet:
            dest = docs_dir / f"{mid}-packet.pdf"
            if download_event_file(api_base, eid, FILE_TYPE_AGENDA_PACKET, dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.3)

        # Additional documents from documentList
        if args.deep and doc_list:
            for doc in doc_list:
                fid = doc.get("eventItemFileId")
                ftype = doc.get("fileType", 1)
                fname = doc.get("eventItemFileName", f"doc-{fid}")
                if not fid:
                    continue
                safe_name = re.sub(r'[^\w\s\-.]', '', fname)
                safe_name = re.sub(r'\s+', '_', safe_name).strip('_')[:80]
                dest = docs_dir / f"{mid}-{fid}-{safe_name}.pdf"
                if download_event_file(api_base, fid, ftype, dest):
                    text = extract_text(dest)
                    if text:
                        doc_count += 1
                    time.sleep(0.3)

        state["meetings"][mid] = {
            "fetched": datetime.now().isoformat(),
            "body": body,
        }
        time.sleep(0.5)

    state["last_fetch"] = datetime.now().isoformat()
    save_json(state_file, state)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    meetings_dir = agency_data_dir(slug) / "meetings"
    if not meetings_dir.exists():
        print("No meetings directory found.")
        return

    meetings = []
    for mdir in sorted(meetings_dir.iterdir()):
        mf = mdir / "meeting.json"
        if mf.exists():
            meetings.append(load_json(mf))

    meetings.sort(key=lambda m: m.get("date", ""), reverse=True)
    for m in meetings:
        print(f"  {m.get('date', '?'):12s}  {m.get('body', '?'):35s}  {m.get('id', '?')}")


def main():
    parser = argparse.ArgumentParser(description="CivicClerk scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", required=True)
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")

    p_probe = sub.add_parser("probe")
    p_probe.add_argument("--agency", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--agency", required=True)

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "probe":
        cmd_probe(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
