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
    cmd_list_meetings, log_discovery, BROWSER_UA,
    meeting_needs_recheck,
)
import civic_utils

RECHECK_DAYS = getattr(civic_utils, 'RECHECK_DAYS', 90)

SLUG = "lemon_grove"
BASE_URL = "https://events.lemongrove.ca.gov"
LISTING_URL = f"{BASE_URL}/council"

UUID_RE = re.compile(r'/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$', re.I)
# Anchored: meeting detail URLs end at the slug. Document URLs append /<uuid>
# and must not match (they created phantom "lg-agenda-pdf-*" meetings).
DETAIL_RE = re.compile(r'/council/Detail/(\d{4})-(\d{2})-(\d{2})-(\d{4})-([^/?#]+)/?$')


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
        m = DETAIL_RE.search(href.split("?")[0].split("#")[0])
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


def _guess_detail_url(session, body, dt):
    """Best-effort detail URL for meetings stored before detail_url existed.

    Pattern: /council/Detail/YYYY-MM-DD-HHMM-Body-Words[N]. Past meetings 302
    to /council/Index when the slug is wrong.
    """
    slug = re.sub(r"[^A-Za-z0-9]+", "-", body).strip("-")
    if not slug:
        return None
    for hhmm in ("1800", "1700", "1900"):
        for suffix in ("", "2", "3"):
            url = f"{BASE_URL}/council/Detail/{dt.strftime('%Y-%m-%d')}-{hhmm}-{slug}{suffix}"
            try:
                r = session.get(url, timeout=30, allow_redirects=False)
            except requests.RequestException:
                continue
            time.sleep(0.3)
            if r.status_code == 200:
                return url
    return None


def cmd_fetch(args):
    _, meetings_dir, docs_dir, state_file = _setup_dirs()
    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching Lemon Grove meetings (cutoff {cutoff.strftime('%Y-%m-%d')})...")

    session = requests.Session()
    session.headers.update({"User-Agent": BROWSER_UA})

    events = []
    try:
        resp = session.get(LISTING_URL, timeout=30)
        resp.raise_for_status()
        events = _parse_listing(resp.text)
    except requests.RequestException as e:
        # HTTPError, Timeout, ConnectionError: fall through to rechecks of
        # stored meetings; the empty-listing failure is emitted at the end.
        print(f"  Listing page failed: {e}")
    print(f"  Found {len(events)} events on listing page")

    # The listing only shows upcoming events, so past meetings drop off before
    # their documents are posted. Add recheck candidates from stored detail URLs.
    work = {}
    for detail_url, dt, title in events:
        work[f"lg-{_body_slug(title)}-{dt.strftime('%Y%m%d')}"] = (detail_url, dt, title)
    for mdir in sorted(meetings_dir.iterdir()):
        mid = mdir.name
        if mid in work or mid.startswith("lg-agenda-pdf-"):
            continue
        meta = load_json(mdir / "meeting.json")
        if not meta or meta.get("cancelled"):
            continue
        try:
            dt = datetime.strptime(meta["date"], "%m/%d/%Y")
        except (KeyError, ValueError):
            continue
        if (datetime.now() - dt).days > RECHECK_DAYS or dt < cutoff:
            continue
        if not meeting_needs_recheck(mid, docs_dir, meta["date"], max_age_days=RECHECK_DAYS):
            continue
        url = meta.get("detail_url")
        if not url:
            url = _guess_detail_url(session, meta.get("body", ""), dt)
        if url:
            work[mid] = (url, dt, meta.get("body", ""))

    new_count = 0
    doc_count = 0

    for mid, (detail_url, dt, title) in sorted(work.items(), key=lambda kv: kv[1][1]):
        if dt < cutoff:
            continue
        if "cancelled" in title.lower():
            continue

        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)
        meeting_file = mdir / "meeting.json"

        if not meeting_file.exists():
            meta = {
                "id": mid,
                "body": title,
                "date": dt.strftime("%m/%d/%Y"),
                "title": f"{title} \u2014 {dt.strftime('%b %d, %Y')}",
                "source": "events_portal",
                "agency": SLUG,
                "detail_url": detail_url,
            }
            if re.search(r"cancel", title, re.I):
                meta["cancelled"] = True
            save_json(meeting_file, meta)
            new_count += 1
            print(f"    NEW: {title} \u2014 {dt.strftime('%Y-%m-%d')}")
        else:
            meta = load_json(meeting_file)
            if meta.get("detail_url") != detail_url:
                meta["detail_url"] = detail_url
                save_json(meeting_file, meta)

        if mid in state["meetings"]:
            if not meeting_needs_recheck(mid, docs_dir, dt.strftime("%m/%d/%Y"), max_age_days=RECHECK_DAYS):
                continue
            print(f"    RECHECK: {title} \u2014 {dt.strftime('%Y-%m-%d')} (no documents yet)")

        # Fetch detail page for document links
        time.sleep(0.5)
        try:
            dresp = session.get(detail_url, timeout=30)
            dresp.raise_for_status()
        except Exception as e:
            print(f"    Detail page failed: {e}")
            continue

        doc_links = _parse_detail(dresp.text, detail_url)

        for doc_url, doc_label in doc_links:
            safe_label = safe_filename(doc_label)
            dest = docs_dir / f"{mid}-{safe_label}.pdf"
            if dest.exists() and dest.stat().st_size > 0:
                continue

            if download_pdf(doc_url, dest, headers={"User-Agent": BROWSER_UA}):
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
    if not events:
        print("ERROR: Lemon Grove: listing parsed 0 meetings")
        sys.exit(3)


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
