#!/usr/bin/env python3
"""
Lemon Grove scraper — custom events portal at events.lemongrove.ca.gov.

Usage:
    python lemon_grove.py fetch [--years N]
    python lemon_grove.py list

Requires: requests, beautifulsoup4, lxml
"""

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    safe_filename, agency_data_dir, rebuild_doc_index,
    cmd_list_meetings, log_discovery, USER_AGENT,
)

SLUG = "lemon_grove"
BASE_URL = "https://events.lemongrove.ca.gov"
LISTING_URL = f"{BASE_URL}/council"

UUID_RE = re.compile(r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', re.I)
DETAIL_RE = re.compile(r'/council/Detail/(\d{4})-(\d{2})-(\d{2})-(\d{4})-(.+)')


def _setup_dirs():
    data_dir = agency_data_dir(SLUG)
    meetings_dir = data_dir / "meetings"
    docs_dir = data_dir / "documents"
    state_file = data_dir / "state.json"
    for d in [meetings_dir, docs_dir]:
        d.mkdir(parents=True, exist_ok=True)
    return data_dir, meetings_dir, docs_dir, state_file


def _body_slug(title):
    slug = re.sub(r'[^a-z0-9]+', '-', title.lower()).strip('-')[:30]
    return slug or "council"


def _parse_listing(html):
    """Parse listing page, return list of (url, date, title) tuples."""
    soup = BeautifulSoup(html, "lxml")
    results = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = DETAIL_RE.search(href)
        if not m:
            continue
        text = a.get_text(strip=True)
        if not text:
            continue
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                      int(m.group(4)[:2]), int(m.group(4)[2:]))
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        results.append((url, dt, text))
    return results


def _parse_detail(html, detail_url):
    """Parse detail page, return list of (doc_url, doc_label) for UUID-linked docs."""
    soup = BeautifulSoup(html, "lxml")
    docs = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if UUID_RE.search(href) and "/Detail/" in href:
            label = a.get_text(strip=True)
            url = href if href.startswith("http") else f"{BASE_URL}{href}"
            docs.append((url, label))
    return docs


def cmd_fetch(args):
    _, meetings_dir, docs_dir, state_file = _setup_dirs()
    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching Lemon Grove meetings (cutoff {cutoff.strftime('%Y-%m-%d')})...")

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})

    resp = session.get(LISTING_URL, timeout=30)
    resp.raise_for_status()
    events = _parse_listing(resp.text)
    print(f"  Found {len(events)} events on listing page")

    new_count = 0
    doc_count = 0

    for detail_url, dt, title in events:
        if dt < cutoff:
            continue
        if "cancelled" in title.lower():
            continue

        body_s = _body_slug(title)
        mid = f"lg-{body_s}-{dt.strftime('%Y%m%d')}"

        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)
        meeting_file = mdir / "meeting.json"

        if not meeting_file.exists():
            meta = {
                "id": mid,
                "body": title,
                "date": dt.strftime("%m/%d/%Y"),
                "title": f"{title} — {dt.strftime('%b %d, %Y')}",
                "source": "events_portal",
                "agency": SLUG,
            }
            save_json(meeting_file, meta)
            new_count += 1
            print(f"    NEW: {title} — {dt.strftime('%Y-%m-%d')}")

        if mid in state["meetings"]:
            continue

        # Fetch detail page for document links
        time.sleep(0.5)
        try:
            dresp = session.get(detail_url, timeout=30)
            dresp.raise_for_status()
        except Exception as e:
            print(f"    Detail page failed: {e}")
            state["meetings"][mid] = {"fetched": datetime.now().isoformat(), "body": title}
            continue

        doc_links = _parse_detail(dresp.text, detail_url)

        for doc_url, doc_label in doc_links:
            safe_label = safe_filename(doc_label)
            dest = docs_dir / f"{mid}-{safe_label}.pdf"
            if dest.exists() and dest.stat().st_size > 0:
                continue

            if download_pdf(doc_url, dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.3)

        state["meetings"][mid] = {
            "fetched": datetime.now().isoformat(),
            "body": title,
            "date": dt.strftime("%m/%d/%Y"),
        }

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(SLUG, state, docs_dir)
    save_json(state_file, state)
    log_discovery(SLUG, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(SLUG)


def main():
    parser = argparse.ArgumentParser(description="Lemon Grove scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--years", default="1")

    sub.add_parser("list")

    args = parser.parse_args()
    if args.command == "fetch":
        cmd_fetch(args)
    elif args.command == "list":
        cmd_list(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
