#!/usr/bin/env python3
"""
El Cajon scraper -- CivicPlus CMS pages behind Akamai WAF.
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

BODY_PAGES = {
    "City Council": "/your-government/city-meetings-with-agendas-and-minutes-575",
    "Planning Commission": "/your-government/commissions/planning-commission/planning-commission-archive-agendas",
}

DATE_RE_SHORT = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{2})(?!\d)")
DATE_RE_LONG = re.compile(r"(\d{1,2})-(\d{1,2})-(\d{4})")


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


def parse_date(text):
    m = DATE_RE_SHORT.search(text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%m-%d-%y")
        except ValueError:
            pass
    m = DATE_RE_LONG.search(text)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)}-{m.group(2)}-{m.group(3)}", "%m-%d-%Y")
        except ValueError:
            pass
    return None


def classify_doc(text):
    t = text.lower()
    if "minute" in t:
        return "minutes"
    if "full" in t or "packet" in t:
        return "packet"
    if "agenda" in t:
        return "agenda"
    return "attachment"


def meeting_id(body, dt):
    body_slug = re.sub(r"[^a-z0-9]+", "-", body.lower()).strip("-")[:30]
    return f"ec-{body_slug}-{dt.strftime('%Y%m%d')}"


def parse_pdf_links(html, cutoff):
    soup = BeautifulSoup(html, "lxml")
    links = soup.find_all("a", href=re.compile(r"showpublisheddocument", re.I))
    meetings = {}

    for a in links:
        text = a.get_text(strip=True)
        href = a.get("href", "")
        dt = parse_date(text)
        if not dt:
            continue
        if dt < cutoff:
            continue

        full_url = f"{BASE_URL}{href}" if href.startswith("/") else href
        doc_type = classify_doc(text)
        meeting_type = "Special" if "special" in text.lower() else "Regular"

        key = dt.strftime("%Y%m%d")
        if key not in meetings:
            meetings[key] = {"date": dt, "type": meeting_type, "docs": []}
        meetings[key]["docs"].append({"url": full_url, "doc_type": doc_type, "name": text})

    return list(meetings.values())


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

    cfg = load_agencies(enabled_only=False).get(SLUG, {})
    body_pages = cfg.get("body_pages") or BODY_PAGES

    print(f"Fetching City of El Cajon meetings...")
    new_count = 0
    doc_count = 0

    for body, path in body_pages.items():
        print(f"\n  {body}...")
        url = f"{BASE_URL}{path}" if path.startswith("/") else path
        try:
            html = fetch_page(url)
        except Exception as e:
            print(f"    Failed: {e}")
            continue

        meetings = parse_pdf_links(html, cutoff)
        print(f"    {len(meetings)} meetings found")

        for meeting in meetings:
            dt = meeting["date"]
            mid = meeting_id(body, dt)
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
            time.sleep(1)

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
