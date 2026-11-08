#!/usr/bin/env python3
"""
Solana Beach scraper — dual-source:
  1. eScribe API for 2026+ Council meetings (same API as SANDAG)
  2. Drupal Views HTML for historical (pre-2026) + commission meetings

City Council page: /en/city-council-meetings (Drupal + eScribe iframe)
Commissions page: /en/government/public-meetings/citizen-commission-council-standing-committee-meetings

Usage:
    python solana_beach.py fetch [--years N] [--deep]
    python solana_beach.py list

Requires: requests, beautifulsoup4, lxml
"""

import argparse
import hashlib
import json
import re
import sys
import time
import urllib3
from datetime import datetime, timedelta
from pathlib import Path

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import (
    download_pdf, extract_text, save_json, load_json,
    load_agencies, agency_data_dir, USER_AGENT,
    parse_escribe_date, cmd_list_meetings, rebuild_doc_index, log_discovery,
)

SLUG = "solana_beach"

ESCRIBE_BASE = "https://pub-solanabeach.escribemeetings.com"
PAST_MEETINGS_URL = f"{ESCRIBE_BASE}/MeetingsCalendarView.aspx/PastMeetings"


# ── eScribe source (2026+) ──────────────────────────────────────────


def meeting_id_escribe(body, dt):
    date_str = dt.strftime("%Y-%m-%d")
    return hashlib.md5(f"solana_beach-{body}-{date_str}".encode()).hexdigest()[:12]


def extract_escribe_docs(meeting_data):
    links = meeting_data.get("MeetingLinks", [])
    agenda_url = None
    minutes_url = None

    for link in links:
        link_type = link.get("Type", "")
        fmt = link.get("Format", "")
        url = link.get("Url", "")

        if link_type == "PostAgendaCover" and fmt == ".pdf":
            agenda_url = url
        elif link_type == "PostAgenda" and fmt == ".pdf" and not agenda_url:
            agenda_url = url
        elif link_type == "PostMinutes" and fmt == ".pdf":
            minutes_url = url

    def full_url(u):
        if not u:
            return None
        return u if u.startswith("http") else f"{ESCRIBE_BASE}/{u}"

    return {"agenda_url": full_url(agenda_url), "minutes_url": full_url(minutes_url)}


def fetch_escribe_meetings(cutoff_date, escribe_bodies, deep=False):
    """Fetch meetings from eScribe API. Returns list of (meeting_meta, docs) tuples."""
    session = requests.Session()
    session.verify = False
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    })

    min_year = cutoff_date.year
    results = []

    for body_type in escribe_bodies:
        page = 1
        seen = 0
        seen_ids = set()
        while True:
            resp = session.post(PAST_MEETINGS_URL, json={
                "type": body_type,
                "pageNumber": page,
            }, timeout=30)
            resp.raise_for_status()
            data = resp.json().get("d", {})
            meetings = data.get("Meetings", [])

            if not meetings:
                break

            # Guard against an API that repeats the last page forever.
            page_ids = {m.get("Id") for m in meetings}
            if page_ids and page_ids <= seen_ids:
                break
            seen_ids |= page_ids
            seen += len(meetings)
            total = data.get("TotalCount")

            hit_cutoff = False
            for m in meetings:
                if m.get("Cancelled"):
                    continue
                dt = parse_escribe_date(m.get("Start", ""))
                if not dt:
                    continue
                if dt < cutoff_date:
                    hit_cutoff = True
                    continue

                body = body_type.replace("Council Meetings REGULAR", "City Council")
                body = body.replace("Closed Session", "City Council Closed Session")
                mid = meeting_id_escribe(body, dt)
                docs = extract_escribe_docs(m)

                results.append({
                    "meta": {
                        "id": mid,
                        "body": body,
                        "date": dt.strftime("%m/%d/%Y"),
                        "title": f"{body} — {dt.strftime('%b %d, %Y')}",
                        "source": "escribe",
                        "agency": SLUG,
                        "escribe_id": m.get("Id"),
                    },
                    "docs": docs,
                })

            # Page size is not fixed (17 observed), so paginate until an empty
            # page, the cutoff, or TotalCount is reached.
            if hit_cutoff or (isinstance(total, int) and seen >= total):
                break
            page += 1

    return results


# ── Drupal Views source (historical + commissions) ──────────────────

def meeting_id_drupal(body, dt):
    body_slug = re.sub(r'[^a-z0-9]+', '-', body.lower()).strip('-')[:30]
    return f"sb-{body_slug}-{dt.strftime('%Y%m%d')}"


AGENDA_FIELDS = ".views-field-field-agenda-or-action-agenda, .views-field-field-notice-or-agenda"
PACKET_FIELDS = ".views-field-field-combined-agenda-packet"
MINUTES_FIELDS = ".views-field-field-approved-minutes"


def parse_drupal_page(html, base_url, body_prefix=None, cutoff_date=None):
    """Parse Drupal Views output. Each views-row has structured fields."""
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select(".views-row")
    meetings = []

    for row in rows:
        # Title field: "MM/DD/YYYY - Body/Type Description"
        title_el = row.select_one(".views-field-title .field-content")
        if not title_el:
            title_el = row.select_one(".views-field-title")
        if not title_el:
            continue

        title_text = title_el.get_text(strip=True)
        date_match = re.match(r'(\d{1,2}/\d{1,2}/\d{4})\s*-?\s*(.*)', title_text)
        if not date_match:
            continue

        try:
            dt = datetime.strptime(date_match.group(1), "%m/%d/%Y")
        except ValueError:
            continue

        if cutoff_date and dt < cutoff_date:
            continue

        body_text = date_match.group(2).strip()
        if body_prefix and body_prefix not in body_text:
            body = f"{body_prefix} — {body_text}" if body_text else body_prefix
        else:
            body = body_text or "Unknown"
        # Clean up body name
        body = re.sub(r'\s*(?:Meeting|Session)\s*$', '', body, flags=re.I).strip()

        mid = meeting_id_drupal(body, dt)

        # Collect document links from each field
        docs = []

        # Agenda. Council rows use field-agenda-or-action-agenda; commission
        # rows use field-notice-or-agenda. Accept both.
        for agenda_field in row.select(AGENDA_FIELDS):
            for a in agenda_field.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    href = f"{base_url}{href}"
                docs.append({"url": href, "type": "agenda", "text": a.get_text(strip=True)})

        # Packet
        for packet_field in row.select(PACKET_FIELDS):
            for a in packet_field.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    href = f"{base_url}{href}"
                docs.append({"url": href, "type": "packet", "text": a.get_text(strip=True)})

        # Minutes
        for minutes_field in row.select(MINUTES_FIELDS):
            for a in minutes_field.find_all("a", href=True):
                href = a["href"]
                if not href.startswith("http"):
                    href = f"{base_url}{href}"
                docs.append({"url": href, "type": "minutes", "text": a.get_text(strip=True)})

        # Video link
        video_field = row.select_one(".views-field-nothing")
        video_url = None
        if video_field:
            for a in video_field.find_all("a", href=True):
                if "12milesout" in a["href"]:
                    video_url = a["href"]
                    break

        meetings.append({
            "id": mid,
            "body": body,
            "date": dt,
            "title": title_text,
            "docs": docs,
            "video_url": video_url,
        })

    return meetings


def fetch_drupal_meetings(base_url, cutoff_date, drupal_pages):
    """Fetch meetings from all Drupal pages."""
    all_meetings = []

    for page_key, page_cfg in drupal_pages.items():
        path = page_cfg["path"]
        body_prefix = page_cfg["body_prefix"]
        url = f"{base_url}{path}"

        print(f"  Fetching {page_key} ({path})...")
        try:
            resp = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
        except Exception as e:
            print(f"    Failed: {e}")
            continue

        meetings = parse_drupal_page(resp.text, base_url, body_prefix, cutoff_date)
        print(f"    {len(meetings)} meetings")
        all_meetings.extend(meetings)

    return all_meetings


# ── Commands ─────────────────────────────────────────────────────────

def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    if SLUG not in agencies:
        print(f"Agency {SLUG} not in agencies.yaml")
        sys.exit(1)

    cfg = agencies[SLUG]
    base_url = cfg["base_url"].rstrip("/")
    escribe_bodies = cfg.get("escribe_bodies", ["Council Meetings REGULAR"])
    drupal_pages = cfg.get("drupal_pages", {})

    data_dir = agency_data_dir(SLUG)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"

    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}

    years = float(args.years) if args.years else 1
    cutoff = datetime.now() - timedelta(days=365 * years)

    print(f"Fetching {cfg['name']} meetings...")

    new_count = 0
    doc_count = 0
    failures = 0  # eScribe API errors (download misses are only retried)

    # Source 1: eScribe for 2026+ council meetings
    print("\n  eScribe API (2026+ council)...")
    try:
        escribe_meetings = fetch_escribe_meetings(cutoff, escribe_bodies, deep=args.deep)
        print(f"    {len(escribe_meetings)} meetings from eScribe")

        for item in escribe_meetings:
            meta = item["meta"]
            mid = meta["id"]
            mdir = meetings_dir / mid
            mdir.mkdir(exist_ok=True)

            meeting_file = mdir / "meeting.json"
            if not meeting_file.exists():
                save_json(meeting_file, meta)
                new_count += 1
                print(f"    NEW: {meta['body']} — {meta['date']}")

            # eScribe IDs are hashes, so rebuild_doc_index can only date these
            # docs from the state entry. Always record date/title, even when
            # the download step below is skipped.
            state["meetings"].setdefault(mid, {}).update({
                "body": meta["body"],
                "date": meta["date"],
                "title": meta["title"],
            })

            has_docs = any(f.name.startswith(mid) for f in docs_dir.glob(f"{mid}-*.txt"))
            if state["meetings"][mid].get("fetched") and has_docs and not args.deep:
                continue

            docs = item["docs"]
            all_ok = True
            if docs.get("agenda_url"):
                dest = docs_dir / f"{mid}-agenda.pdf"
                if download_pdf(docs["agenda_url"], dest, verify=False):
                    text = extract_text(dest)
                    if text:
                        doc_count += 1
                    time.sleep(0.3)
                else:
                    all_ok = False

            if docs.get("minutes_url"):
                dest = docs_dir / f"{mid}-minutes.pdf"
                if download_pdf(docs["minutes_url"], dest, verify=False):
                    text = extract_text(dest)
                    if text:
                        doc_count += 1
                    time.sleep(0.3)
                else:
                    all_ok = False

            # Only mark fetched when every attempted download succeeded.
            if all_ok:
                state["meetings"][mid]["fetched"] = datetime.now().isoformat()
            else:
                print(f"    Download failed for {meta['body']} {meta['date']}; will retry next run")
    except Exception as e:
        failures += 1
        print(f"ERROR: Solana Beach: eScribe API failed: {e}")

    # Source 2: Drupal for historical + commissions
    print("\n  Drupal pages (historical + commissions)...")
    drupal_meetings = fetch_drupal_meetings(base_url, cutoff, drupal_pages)
    print(f"    {len(drupal_meetings)} meetings total from Drupal")

    for meeting in drupal_meetings:
        mid = meeting["id"]
        mdir = meetings_dir / mid
        mdir.mkdir(exist_ok=True)

        meeting_file = mdir / "meeting.json"
        if not meeting_file.exists():
            meta = {
                "body": meeting["body"],
                "date": meeting["date"].strftime("%m/%d/%Y"),
                "title": f"{meeting['body']} — {meeting['date'].strftime('%b %d, %Y')}",
                "id": mid,
                "source": "drupal",
                "agency": SLUG,
            }
            if meeting.get("video_url"):
                meta["video_url"] = meeting["video_url"]
            save_json(meeting_file, meta)
            new_count += 1
            print(f"    NEW: {meeting['body']} — {meeting['date'].strftime('%Y-%m-%d')}")

        has_docs = any(f.name.startswith(mid) for f in docs_dir.glob(f"{mid}-*.txt"))
        if mid in state["meetings"] and has_docs and not args.deep:
            continue

        for doc in meeting["docs"]:
            doc_type = doc["type"]
            # Without --deep, only download agendas and minutes
            if not args.deep and doc_type not in ("agenda", "minutes"):
                continue

            url = doc["url"]
            url_fname = url.split("/")[-1].split("?")[0] if "/" in url else "doc.pdf"
            from urllib.parse import unquote
            url_fname = unquote(url_fname)
            safe_name = re.sub(r'[^\w\s\-.]', '', url_fname)
            safe_name = re.sub(r'\s+', '_', safe_name).strip('_')[:80]
            dest = docs_dir / f"{mid}-{doc_type}-{safe_name}"

            if not dest.suffix:
                dest = dest.with_suffix(".pdf")

            if dest.exists():
                continue

            if download_pdf(url, dest):
                text = extract_text(dest)
                if text:
                    doc_count += 1
                time.sleep(0.3)

        # Update in place: keep any date/title already recorded.
        state["meetings"].setdefault(mid, {}).update({
            "fetched": datetime.now().isoformat(),
            "body": meeting["body"],
            "date": meeting["date"].strftime("%m/%d/%Y"),
            "title": f"{meeting['body']} \u2014 {meeting['date'].strftime('%b %d, %Y')}",
        })

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index("solana_beach", state, docs_dir)
    save_json(state_file, state)
    log_discovery("solana_beach", meetings_new=new_count, docs_new=doc_count)
    print(f"\nDone. {new_count} new meetings, {doc_count} documents extracted.")
    if not drupal_meetings:
        print("ERROR: Solana Beach: listing parsed 0 meetings")
        sys.exit(3)
    if failures:
        sys.exit(1)


def cmd_list(args):
    cmd_list_meetings(SLUG)


def main():
    parser = argparse.ArgumentParser(description="Solana Beach scraper")
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
