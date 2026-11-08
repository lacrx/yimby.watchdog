#!/usr/bin/env python3
"""
Municode Meetings scraper — Drupal-based municipal meeting portal.

Scrapes meeting listings from the calendar page, fetches agenda packet PDFs
from Azure blob storage, extracts text.

Usage:
    python municode.py fetch --agency escondido [--years N] [--deep]
    python municode.py list --agency escondido

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
    meeting_needs_recheck,
)
import civic_utils

RECHECK_DAYS = getattr(civic_utils, 'RECHECK_DAYS', 90)
MAX_LIST_PAGES = 60

BODY_CODES = {
    "bc-citycouncil": "City Council",
    "bc-pc": "Planning Commission",
    "bc-library": "Library Board of Trustees",
    "bc-arts": "Public Art Commission",
    "bc-tc": "Transportation Commission",
    "bc-usm": "Utilities Subcommittee",
    "bc-bsm": "Budget Subcommittee",
    "bc-baab": "Building Advisory and Appeals Board",
    "bc-hpc": "Historic Preservation Commission",
    "bc-scsm": "Sister Cities Subcommittee",
    "bc-mi": "Measure I Citizens' Oversight Committee",
}


def fetch_meeting_list(base_url, page=0):
    """Fetch one page of the meetings listing (newest first)."""
    url = base_url + "/" + (f"?page={page}" if page else "")
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def fetch_all_meetings(base_url, cutoff_date):
    """Follow ?page=N until the listing runs out, repeats, or passes the cutoff.

    Returns all parsed meetings on the pages visited (unfiltered by cutoff;
    the caller filters). Raises on a page-0 failure; later page failures
    just end pagination.
    """
    all_meetings = []
    seen = set()
    for page in range(MAX_LIST_PAGES):
        try:
            html = fetch_meeting_list(base_url, page)
        except requests.RequestException:
            if page == 0:
                raise
            print(f"  Listing page {page} failed; stopping pagination")
            break
        rows = parse_meeting_list(html, base_url, "")
        new_rows = [m for m in rows if m["meeting_id"] not in seen]
        if not new_rows:
            break
        seen.update(m["meeting_id"] for m in new_rows)
        all_meetings.extend(new_rows)
        if min(m["date"] for m in new_rows) < cutoff_date:
            break
        if "page=" not in html or not BeautifulSoup(html, "lxml").select("a[href*='page=']:not([href*='/bc-'])"):
            break
    return all_meetings


def parse_meeting_list(html, base_url, cutoff_date):
    """Parse meeting listing HTML into meeting dicts.

    Returns list of {url, date, body, body_code, title, meeting_id}.
    """
    soup = BeautifulSoup(html, "lxml")
    meetings = []
    seen_urls = set()

    links = soup.select("a[href*='/bc-'][href*='/page/']")
    for link in links:
        href = link.get("href", "")
        if href in seen_urls or "webform" in href:
            continue
        seen_urls.add(href)

        body_match = re.match(r"/bc-([^/]+)/page/(.+)", href)
        if not body_match:
            continue

        body_code = f"bc-{body_match.group(1)}"
        slug = body_match.group(2)
        body_name = BODY_CODES.get(body_code, body_code.replace("bc-", "").replace("-", " ").title())

        # The date cell precedes the link inside the same table row, so take
        # it from the link's own row (find_next would grab the NEXT row's date).
        row = link.find_parent("tr") or link.find_parent(class_=re.compile(r"views-row"))
        date_el = row.find("span", attrs={"content": True}) if row else None
        if not date_el:
            continue

        date_str = date_el["content"][:10]
        if date_str < cutoff_date:
            continue

        cancelled = "cancelled" in slug.lower()

        meeting_id = hashlib.md5(f"{body_code}-{slug}".encode()).hexdigest()[:12]

        meetings.append({
            "url": urljoin(base_url + "/", href.lstrip("/")),
            "path": href,
            "date": date_str,
            "body": body_name,
            "body_code": body_code,
            "title": link.get_text(strip=True),
            "meeting_id": meeting_id,
            "cancelled": cancelled,
        })

    return meetings


def fetch_meeting_documents(meeting_url):
    """Fetch meeting detail page and extract document links."""
    resp = requests.get(meeting_url, timeout=30,
                        headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    docs = []

    for link in soup.select("a[href*='mccmeetings.blob']"):
        href = link.get("href", "")
        title = link.get_text(strip=True) or "Agenda Packet"
        if href.endswith(".pdf"):
            docs.append({"url": href, "title": title})

    return docs


def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    if cfg["platform"] != "municode":
        print(f"{slug} is not a Municode agency (platform: {cfg['platform']})")
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
    cutoff_date = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")

    print(f"Fetching {cfg['name']} meetings (Municode Meetings)...")
    try:
        listed = fetch_all_meetings(base_url, cutoff_date)
    except requests.RequestException as e:
        print(f"ERROR: {cfg['name']}: listing fetch failed: {e}")
        sys.exit(3)
    meetings = [m for m in listed if m["date"] >= cutoff_date]
    print(f"  {len(meetings)} meetings found (since {cutoff_date})")

    new_count = 0
    doc_count = 0

    for meeting in meetings:
        mid = meeting["meeting_id"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        is_new = not meeting_file.exists()
        meeting_meta = load_json(meeting_file) if not is_new else {}

        # The live listing is authoritative for date/body: repair stale values.
        if is_new:
            meeting_meta = {
                "body": meeting["body"],
                "date": meeting["date"],
                "id": mid,
                "source": "municode",
                "agency": slug,
                "documents": [],
                "url": meeting["url"],
            }
        else:
            if meeting_meta.get("date") != meeting["date"]:
                print(f"  FIX DATE: {mid} {meeting_meta.get('date')} -> {meeting['date']} ({meeting['body']})")
            meeting_meta.update({"body": meeting["body"], "date": meeting["date"],
                                 "url": meeting["url"]})
        if meeting["cancelled"]:
            meeting_meta["cancelled"] = True
        save_json(meeting_file, meeting_meta)
        if is_new:
            new_count += 1

        st = state["meetings"].setdefault(mid, {})
        st["body"] = meeting["body"]
        st["date"] = meeting["date"]

        if meeting["cancelled"]:
            continue

        if st.get("fetched") and not args.deep and not is_new:
            if not meeting_needs_recheck(mid, docs_dir, meeting["date"], max_age_days=RECHECK_DAYS):
                continue
            print(f"  RECHECK: {meeting['date']} {meeting['body']} (no documents yet)")
        elif is_new or args.deep:
            print(f"  {'NEW: ' if is_new else ''}{meeting['date']} {meeting['body']}")

        try:
            docs = fetch_meeting_documents(meeting["url"])
        except Exception as e:
            print(f"  Detail page failed for {meeting['date']} {meeting['body']} ({mid}): {e}")
            continue
        time.sleep(0.5)

        doc_meta = []
        for doc in docs:
            fname = f"{mid}-{safe_filename(doc['title'])}.pdf"
            dest = docs_dir / fname
            if dest.exists():
                doc_meta.append({"title": doc["title"], "file": fname})
                continue

            if download_pdf(doc["url"], dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                doc_meta.append({"title": doc["title"], "file": fname})
                time.sleep(0.5)

        meeting_meta["documents"] = doc_meta
        save_json(meeting_file, meeting_meta)

        st["fetched"] = datetime.now().isoformat()

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(slug, state, docs_dir)
    save_json(state_file, state)
    log_discovery(slug, meetings_found=len(meetings), meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")
    if not listed:
        print(f"ERROR: {cfg['name']}: listing parsed 0 meetings")
        sys.exit(3)


def cmd_list(args):
    cmd_list_meetings(args.agency)


def main():
    parser = argparse.ArgumentParser(description="Municode Meetings scraper")
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
