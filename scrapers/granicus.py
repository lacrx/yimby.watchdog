#!/usr/bin/env python3
"""
Granicus Publisher scraper — generic for any Granicus-hosted agency.

Reads agency config from agencies.yaml. Fetches meeting listings via RSS,
downloads agenda PDFs from AgendaViewer/MetaViewer pages, extracts text.

Usage:
    python granicus.py fetch --agency encinitas [--years N] [--deep]
    python granicus.py list --agency encinitas

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
from urllib.parse import urljoin, urlparse, parse_qs
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir, USER_AGENT,
    safe_filename, cmd_list_meetings, rebuild_doc_index, log_discovery,
)

GRAN_NS = "https://www.granicus.com/schema/rss-supplements"

DATE_PATTERNS = [
    (r"(\w+ \d{1,2},?\s+\d{4})", ["%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"]),
    (r"(\d{1,2}/\d{1,2}/\d{4})", ["%m/%d/%Y"]),
]


def parse_date_from_title(title):
    for pattern, fmts in DATE_PATTERNS:
        m = re.search(pattern, title)
        if m:
            s = m.group(1)
            for fmt in fmts:
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    continue
    return None


def extract_body_from_title(title):
    """Extract body name from RSS item title like 'Regular City Council Meeting 6:00 p.m. - Jun 24, 2026'."""
    # Strip date portion (everything after " - Mon DD, YYYY" pattern)
    body = re.split(r'\s*-\s*(?:\w{3}\s+\d{1,2},?\s+\d{4}|\d{1,2}:\d{2})', title)[0].strip()
    # Strip leading "Regular/Special" and trailing "Meeting/Hearing/Session"
    body = re.sub(r'^(?:Regular|Special)\s+', '', body, flags=re.I)
    body = re.sub(r'\s+\d{1,2}:\d{2}\s*[ap]\.?m\.?\s*$', '', body, flags=re.I).strip()
    body = re.sub(r'\s+(?:Regular |Special )?(?:Meeting|Hearing|Session|Workshop)\s*$', '', body, flags=re.I).strip()
    return body or title.strip()


def meeting_id_from_link(link):
    """Extract event_id or clip_id from AgendaViewer URL."""
    parsed = urlparse(link)
    params = parse_qs(parsed.query)
    if "event_id" in params:
        return f"evt-{params['event_id'][0]}"
    if "clip_id" in params:
        return f"clip-{params['clip_id'][0]}"
    return hashlib.md5(link.encode()).hexdigest()[:12]


def fetch_rss(base_url, view_id, mode="agendas"):
    url = f"{base_url}/ViewPublisherRSS.php?view_id={view_id}&mode={mode}"
    resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    return resp.text


def parse_rss(xml_text, cutoff_date=None):
    root = ElementTree.fromstring(xml_text)
    items = []
    for item in root.findall(".//item"):
        title = item.findtext("title", "").strip()
        link = item.findtext("link", "").strip()
        guid = item.findtext("guid", "").strip()

        dt = parse_date_from_title(title)
        if not dt:
            pub_parts = item.find(f"{{{GRAN_NS}}}pubDateParts")
            if pub_parts is not None:
                try:
                    dt = datetime(
                        int(pub_parts.get("yr")),
                        int(pub_parts.get("mo")),
                        int(pub_parts.get("day")),
                    )
                except (ValueError, TypeError):
                    pass

        if cutoff_date and dt and dt < cutoff_date:
            continue

        items.append({
            "title": title,
            "link": link,
            "guid": guid,
            "date": dt,
            "body": extract_body_from_title(title),
            "meeting_id": meeting_id_from_link(link),
        })

    return items


def extract_pdf_links_from_agenda(agenda_url, base_url):
    """Scrape AgendaViewer page for PDF download links."""
    resp = requests.get(agenda_url, timeout=30, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()

    pdfs = []
    text = resp.text

    # Type 1: Google Docs Viewer embed → DocumentViewer.php?file=...
    # URL may be encoded (%3D for =, %26 for &, or double-encoded %253D)
    from urllib.parse import unquote
    decoded = unquote(unquote(text))
    doc_matches = re.findall(r'DocumentViewer\.php\?file=([^&"\'\\]+\.pdf)', decoded)
    seen_docs = set()
    for fname in doc_matches:
        if fname in seen_docs:
            continue
        seen_docs.add(fname)
        url = f"{base_url}/DocumentViewer.php?file={fname}&view=1"
        pdfs.append({"url": url, "name": fname, "type": "agenda"})

    # Type 2: MetaViewer.php links (archived meetings with individual docs)
    meta_matches = re.findall(
        r'MetaViewer\.php\?[^"\']+clip_id=(\d+)[^"\']*meta_id=(\d+)', resp.text
    )
    if meta_matches:
        soup = BeautifulSoup(resp.text, "lxml")
        for link in soup.find_all("a", href=re.compile(r"MetaViewer\.php")):
            href = link.get("href", "")
            meta_m = re.search(r'meta_id=(\d+)', href)
            if meta_m:
                meta_id = meta_m.group(1)
                full_url = urljoin(base_url + "/", href)
                label = link.get_text(strip=True) or f"meta-{meta_id}"
                pdfs.append({"url": full_url, "name": label, "meta_id": meta_id, "type": "staff_report"})

    # If we found MetaViewer links but no main agenda DocumentViewer, the page itself is the agenda
    if meta_matches and not doc_matches:
        pdfs.insert(0, {"url": None, "name": "agenda_html", "type": "agenda_html"})

    return pdfs


def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = args.agency
    if slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    if cfg["platform"] != "granicus":
        print(f"{slug} is not a Granicus agency (platform: {cfg['platform']})")
        sys.exit(1)

    base_url = cfg["base_url"]
    view_id = cfg.get("view_id", 1)
    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching {cfg['name']} meetings (Granicus view_id={view_id})...")
    xml_text = fetch_rss(base_url, view_id)
    items = parse_rss(xml_text, cutoff_date=cutoff)
    print(f"  {len(items)} meetings in RSS feed (after {cutoff.strftime('%Y-%m-%d')} cutoff)")

    new_count = 0
    doc_count = 0

    for item in items:
        mid = item["meeting_id"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        if not meeting_file.exists():
            meeting_meta = {
                "body": item["body"],
                "date": item["date"].strftime("%m/%d/%Y") if item["date"] else "",
                "title": item["title"],
                "agenda_url": item["link"],
                "id": mid,
                "source": "granicus",
                "agency": slug,
            }
            save_json(meeting_file, meeting_meta)
            new_count += 1
            print(f"  NEW: {item['title']}")

        if mid in state["meetings"] and not args.deep:
            continue

        # Fetch agenda PDFs
        try:
            pdfs = extract_pdf_links_from_agenda(item["link"], base_url)
        except Exception as e:
            print(f"  Failed to fetch agenda page for {mid}: {e}")
            continue

        has_agenda = any(p["type"] == "agenda" for p in pdfs)

        for i, pdf_info in enumerate(pdfs):
            if pdf_info["type"] == "agenda_html":
                continue

            if pdf_info["type"] == "agenda":
                fname = f"{mid}-agenda.pdf"
            else:
                meta_id = pdf_info.get("meta_id", "")
                label = safe_filename(pdf_info["name"])
                fname = f"{mid}-{meta_id}-{label}.pdf" if meta_id else f"{mid}-{label}.pdf"

            dest = docs_dir / fname
            if dest.exists():
                continue

            # Without --deep: download agendas always; for clip_id meetings
            # (no main agenda PDF), download first 3 MetaViewer docs (agenda items)
            if not args.deep and pdf_info["type"] == "staff_report":
                if has_agenda:
                    continue
                meta_idx = len([p for p in pdfs[:i] if p["type"] == "staff_report"])
                if meta_idx >= 3:
                    continue

            if download_pdf(pdf_info["url"], dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.5)

        state["meetings"][mid] = {
            "fetched": datetime.now().isoformat(),
            "title": item["title"],
        }
        time.sleep(1)

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(slug, state, docs_dir)
    save_json(state_file, state)
    log_discovery(slug, meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    cmd_list_meetings(args.agency)


def main():
    parser = argparse.ArgumentParser(description="Granicus Publisher scraper")
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
