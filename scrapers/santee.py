#!/usr/bin/env python3
"""
Santee scraper — CivicPlus CMS pages behind Akamai WAF.

Uses curl_cffi with Firefox impersonation to bypass TLS fingerprinting.

Usage:
    python santee.py fetch [--years N] [--deep]
    python santee.py list
"""

import argparse
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from curl_cffi import requests as cffi_requests

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from bs4 import BeautifulSoup
from civic_utils import (
    extract_text, save_json, load_json,
    agency_data_dir, load_agencies, rebuild_doc_index,
    cmd_list_meetings, log_discovery,
)

SLUG = "santee"
BASE_URL = "https://www.cityofsanteeca.gov"

BODY_PAGES = {
    "City Council": "/departments/city-clerk/agendas-minutes",
}

DATE_RE = re.compile(r'(\d{2}-\d{2}-\d{4})')


def fetch_page(url):
    if url.startswith("/"):
        url = f"{BASE_URL}{url}"
    r = cffi_requests.get(url, impersonate='firefox', timeout=30)
    r.raise_for_status()
    if "Access Denied" in r.text[:500]:
        raise RuntimeError(f"WAF blocked: {url}")
    return r.text


def download_pdf(url, dest_path):
    if url.startswith("/"):
        url = f"{BASE_URL}{url}"
    r = cffi_requests.get(url, impersonate='firefox', timeout=60)
    if r.status_code != 200 or b'%PDF' not in r.content[:10]:
        return False
    dest_path.write_bytes(r.content)
    return True


def parse_date_from_filename(href):
    m = DATE_RE.search(href)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%m-%d-%Y")
    except ValueError:
        return None


def classify_doc(href, text):
    lower = (text + " " + href).lower()
    if "correspondence" in lower:
        return "correspondence"
    if re.search(r'item-\d+', lower):
        return "supplemental"
    if "agenda-packet" in lower or "packet" in lower:
        return "packet"
    if "minute" in lower:
        return "minutes"
    if "agenda" in lower:
        return "agenda"
    return "attachment"


def meeting_id(body, dt):
    body_slug = re.sub(r'[^a-z0-9]+', '-', body.lower()).strip('-')[:30]
    return f"santee-{body_slug}-{dt.strftime('%Y%m%d')}"


def parse_pdf_links(html, cutoff):
    soup = BeautifulSoup(html, "lxml")
    links = soup.find_all("a", href=re.compile(r'\.pdf|showpublisheddocument', re.I))
    by_date = defaultdict(list)

    for a in links:
        href = a.get("href", "")
        text = a.get_text(strip=True)
        dt = parse_date_from_filename(href) or parse_date_from_filename(text)
        if not dt or dt < cutoff:
            continue
        doc_type = classify_doc(href, text)
        by_date[dt].append({"url": href, "type": doc_type, "name": text or href.split("/")[-1]})

    return by_date


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

    print(f"Fetching City of Santee meetings...")
    new_count = 0
    doc_count = 0

    for body, path in BODY_PAGES.items():
        print(f"\n  {body}...")
        try:
            html = fetch_page(path)
        except Exception as e:
            print(f"    Failed: {e}")
            continue

        meetings_by_date = parse_pdf_links(html, cutoff)
        print(f"    {len(meetings_by_date)} meetings")

        for dt in sorted(meetings_by_date.keys(), reverse=True):
            docs = meetings_by_date[dt]
            mid = meeting_id(body, dt)
            mdir = meetings_dir / mid
            mdir.mkdir(exist_ok=True)

            meeting_file = mdir / "meeting.json"
            if not meeting_file.exists():
                meta = {
                    "body": body,
                    "date": dt.strftime("%m/%d/%Y"),
                    "title": f"{body} — {dt.strftime('%b %d, %Y')}",
                    "id": mid,
                    "source": "santee_cms",
                    "agency": SLUG,
                }
                save_json(meeting_file, meta)
                new_count += 1
                print(f"    NEW: {body} — {dt.strftime('%Y-%m-%d')}")

            if mid in state["meetings"] and not args.deep:
                continue

            for doc in docs:
                if not args.deep and doc["type"] in ("supplemental", "correspondence"):
                    continue

                safe_name = re.sub(r'[^\w\s\-.]', '', doc["name"])
                safe_name = re.sub(r'\s+', '_', safe_name).strip('_')[:60]
                if not safe_name:
                    safe_name = doc["type"]
                dest = docs_dir / f"{mid}-{doc['type']}-{safe_name}.pdf"

                if dest.exists():
                    continue

                if download_pdf(doc["url"], dest):
                    text = extract_text(dest)
                    if text:
                        doc_count += 1
                    time.sleep(0.5)
                else:
                    print(f"    Failed PDF: {doc['name'][:50]}")

            state["meetings"][mid] = {
                "fetched": datetime.now().isoformat(),
                "body": body,
            }
            time.sleep(1)

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(SLUG, state, docs_dir)
    save_json(state_file, state)
    log_discovery(SLUG, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(SLUG)


def main():
    parser = argparse.ArgumentParser(description="Santee CMS scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")

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
