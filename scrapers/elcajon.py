#!/usr/bin/env python3
"""
El Cajon scraper -- CivicPlus events widget behind Akamai WAF.
Paginates through /-toggle-allpast widget to get full meeting history.
Uses curl_cffi for TLS fingerprint bypass on all HTTP.

Usage:
    python elcajon.py fetch [--years N]
    python elcajon.py list
"""

import argparse
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bs4 import BeautifulSoup
from curl_cffi import requests as cffi_requests

from civic_utils import (
    extract_text, save_json, load_json,
    agency_data_dir, load_agencies, rebuild_doc_index,
    cmd_list_meetings, log_discovery,
)

SLUG = "el_cajon"
BASE_URL = "https://www.elcajon.gov"
EVENTS_PATH = "/your-government/city-meetings-with-agendas-and-minutes-575"
PER_PAGE = 20

BODY_MAP = {
    "council meeting": "City Council",
    "special council meeting": "City Council",
    "planning commission meeting": "Planning Commission",
}


def fetch_page(url):
    r = cffi_requests.get(url, impersonate="firefox", timeout=30)
    r.raise_for_status()
    if "Access Denied" in r.text[:500]:
        raise RuntimeError(f"WAF blocked: {url}")
    return r.text


def download_pdf(url, dest_path):
    r = cffi_requests.get(url, impersonate="firefox", timeout=60)
    if r.status_code != 200 or b"%PDF" not in r.content[:10]:
        return False
    dest_path.write_bytes(r.content)
    return True


def classify_doc(text):
    t = text.lower()
    if "minute" in t:
        return "minutes"
    if "full" in t or "packet" in t:
        return "packet"
    if "agenda" in t:
        return "agenda"
    return "attachment"


def parse_body(event_name):
    key = event_name.lower().strip()
    if key.endswith(" canceled"):
        key = key.replace(" canceled", "")
    return BODY_MAP.get(key, event_name)


def meeting_id(body, dt):
    body_slug = re.sub(r"[^a-z0-9]+", "-", body.lower()).strip("-")[:30]
    return f"ec-{body_slug}-{dt.strftime('%Y%m%d')}"


def parse_events_page(html, cutoff):
    """Parse one page of the events widget. Returns list of meeting dicts."""
    soup = BeautifulSoup(html, "lxml")
    widget = soup.find("div", class_="events_widget")
    if not widget:
        return [], 0

    total = 0
    pager = soup.find("span", class_="pager-info")
    if pager:
        m = re.search(r"of\s+(\d+)", pager.text)
        if m:
            total = int(m.group(1))

    rows = widget.find_all("tr")
    meetings = []

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 4:
            continue

        event_name = cells[0].get_text(strip=True)
        if "canceled" in event_name.lower():
            continue

        body = parse_body(event_name)

        date_text = cells[1].get_text(strip=True)
        m = re.search(r"(\d{2}/\d{2}/\d{4})", date_text)
        if not m:
            continue
        try:
            dt = datetime.strptime(m.group(1), "%m/%d/%Y")
        except ValueError:
            continue

        if dt < cutoff:
            continue

        meeting_type = "Special" if "special" in event_name.lower() else "Regular"

        docs = []
        for cell_idx in [2, 3, 4]:
            if cell_idx >= len(cells):
                break
            for a in cells[cell_idx].find_all("a", href=re.compile(r"showpublisheddocument", re.I)):
                href = a.get("href", "")
                name = a.get_text(strip=True)
                full_url = f"{BASE_URL}{href}" if href.startswith("/") else href
                docs.append({
                    "url": full_url,
                    "doc_type": classify_doc(name),
                    "name": name,
                })

        if docs:
            meetings.append({
                "body": body,
                "date": dt,
                "type": meeting_type,
                "docs": docs,
            })

    return meetings, total


def cmd_fetch(args):
    data_dir = agency_data_dir(SLUG)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}
    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching City of El Cajon meetings...")
    new_count = 0
    doc_count = 0
    all_meetings = {}

    for toggle in ["allupcoming", "allpast"]:
        page = 1
        while True:
            url = f"{BASE_URL}{EVENTS_PATH}/-toggle-{toggle}/-npage-{page}"
            try:
                html = fetch_page(url)
            except Exception as e:
                print(f"  Page {page} failed: {e}")
                break

            meetings, total = parse_events_page(html, cutoff)

            if not meetings and page > 1:
                break

            for m in meetings:
                mid = meeting_id(m["body"], m["date"])
                if mid not in all_meetings:
                    all_meetings[mid] = m
                else:
                    all_meetings[mid]["docs"].extend(m["docs"])

            fetched_through = page * PER_PAGE
            print(f"  {toggle} page {page}: {len(meetings)} meetings (total {total})")

            if total and fetched_through >= total:
                break
            if not meetings:
                break

            page += 1
            time.sleep(1)

    print(f"\n  {len(all_meetings)} unique meetings found")

    for mid, meeting in sorted(all_meetings.items()):
        dt = meeting["date"]
        body = meeting["body"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        if not meeting_file.exists():
            meta = {
                "body": body,
                "date": dt.strftime("%m/%d/%Y"),
                "title": f"{body} — {dt.strftime('%b %d, %Y')} ({meeting['type']})",
                "id": mid,
                "source": "elcajon_cms",
                "agency": SLUG,
            }
            save_json(meeting_file, meta)
            new_count += 1
            print(f"    NEW: {body} — {dt.strftime('%Y-%m-%d')}")

        if mid in state["meetings"]:
            continue

        for doc in meeting["docs"]:
            safe_name = re.sub(r"[^\w\s\-.]", "", doc["name"])
            safe_name = re.sub(r"\s+", "_", safe_name).strip("_")[:60]
            dest = docs_dir / f"{mid}-{doc['doc_type']}-{safe_name}.pdf"
            if dest.exists():
                continue

            if download_pdf(doc["url"], dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.5)
            else:
                print(f"    PDF failed: {doc['name'][:50]}")

        state["meetings"][mid] = {"fetched": datetime.now().isoformat(), "body": body}
        time.sleep(0.3)

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(SLUG, state, docs_dir)
    save_json(state_file, state)
    log_discovery(SLUG, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(SLUG)


def main():
    parser = argparse.ArgumentParser(description="El Cajon CMS scraper")
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
