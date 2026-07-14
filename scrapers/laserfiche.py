#!/usr/bin/env python3
"""
Laserfiche Public Portal scraper — for agencies using portal.laserfiche.com.

Navigates folder tree via JSON API, downloads PDF documents.
Session established via plain GET — no headless browser needed.

Usage:
    python laserfiche.py fetch --agency poway [--years N]
    python laserfiche.py list --agency poway
"""

import argparse
import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests

from civic_utils import (
    extract_text, save_json, load_json,
    safe_filename, agency_data_dir, load_agencies, rebuild_doc_index,
    cmd_list_meetings, log_discovery,
)

PORTAL_BASE = "https://portal.laserfiche.com/Portal"

YEAR_RE = re.compile(r'(\d{4})')
DECADE_RE = re.compile(r'(\d{4})-(\d{4})')


class LaserficheClient:
    def __init__(self, repo, browse_id=None):
        self.repo = repo
        self.session = requests.Session()
        self.session.headers["User-Agent"] = (
            "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
        )
        self.api_headers = {
            "Content-Type": "application/json; charset=utf-8",
            "X-Requested-With": "XMLHttpRequest",
        }
        url = f"{PORTAL_BASE}/Browse.aspx?repo={repo}"
        if browse_id:
            url += f"&id={browse_id}"
        self.session.get(url, timeout=15)
        self.api_headers["Referer"] = url

    def _post(self, endpoint, payload):
        r = self.session.post(
            f"{PORTAL_BASE}/{endpoint}",
            json=payload,
            headers=self.api_headers,
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", {})

    def get_folder_ids(self, folder_id):
        data = self._post(
            "FolderListingService.aspx/GetFolderListingIds",
            {"repoName": self.repo, "folderId": folder_id,
             "sortColumn": "Name", "sortAscending": True},
        )
        return [int(x) for x in data] if isinstance(data, list) else []

    def get_folder_info(self, folder_id):
        """Returns folder dict, or None if entry is a document."""
        data = self._post(
            "FolderListingService.aspx/GetFolderListing2",
            {"repoName": self.repo, "folderId": folder_id,
             "getNewListing": True, "start": 0, "end": 5,
             "sortColumn": "Name", "sortAscending": False},
        )
        if data.get("failed") and "Mismatched entry type" in str(data.get("errMsg", "")):
            return None
        return data

    def get_doc_info(self, entry_id):
        return self._post(
            "DocumentService.aspx/GetBasicDocumentInfo",
            {"repoName": self.repo, "entryId": entry_id},
        )

    def download_pdf(self, doc_id, dest_path):
        url = f"{PORTAL_BASE}/ElectronicFile.aspx?docid={doc_id}&repo={self.repo}"
        r = self.session.get(url, timeout=120)
        if r.status_code != 200 or b"%PDF" not in r.content[:10]:
            return False
        dest_path.write_bytes(r.content)
        return True


def extract_date(name, metadata=None):
    if metadata and metadata.get("fInfo"):
        for field in metadata["fInfo"]:
            if "date" in field.get("name", "").lower() and field.get("values"):
                for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                    try:
                        return datetime.strptime(field["values"][0], fmt)
                    except ValueError:
                        continue

    m = re.search(r"(\d{4})\s+(?:AG\s+)?(\d{2})/(\d{2})", name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = re.search(r"(\d{4})\s+(\d{2})-(\d{2})", name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    m = re.search(r"(\d{2})/(\d{2})/(\d{4})", name)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    m = re.search(r"(\d{2})-(\d{2})-(\d{4})", name)
    if m:
        try:
            return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass

    return None


def folder_in_year_range(name, min_year, max_year):
    m = DECADE_RE.search(name)
    if m:
        return int(m.group(1)) <= max_year and int(m.group(2)) >= min_year

    years = [int(y) for y in YEAR_RE.findall(name)]
    if years:
        return any(min_year <= y <= max_year for y in years)

    return True


def collect_docs(client, folder_id, min_year, max_year, depth=0):
    """Recursively find documents. Returns [(entry_id, parent_folder_name)]."""
    if depth > 6:
        return []

    child_ids = client.get_folder_ids(folder_id)
    if not child_ids:
        return []

    first = client.get_folder_info(child_ids[0])
    time.sleep(0.2)

    if first is None:
        info = client.get_folder_info(folder_id)
        parent_name = info.get("name", "") if info else ""
        return [(cid, parent_name) for cid in child_ids]

    results = []
    for cid in child_ids:
        info = client.get_folder_info(cid)
        time.sleep(0.2)
        if info is None:
            continue
        name = info.get("name", "")
        if not folder_in_year_range(name, min_year, max_year):
            continue
        results.extend(collect_docs(client, cid, min_year, max_year, depth + 1))

    return results


def make_meeting_id(slug, body, dt):
    body_slug = re.sub(r"[^a-z0-9]+", "-", body.lower()).strip("-")[:30]
    return f"{slug}-{body_slug}-{dt.strftime('%Y%m%d')}"


def cmd_fetch(args):
    agencies = load_agencies(enabled_only=False)
    slug = getattr(args, "agency", None)
    if not slug or slug not in agencies:
        print(f"Unknown agency: {slug}")
        sys.exit(1)

    cfg = agencies[slug]
    repo = cfg.get("laserfiche_repo")
    body_folders = cfg.get("body_folders", {})
    browse_id = cfg.get("laserfiche_browse_id")

    if not repo or not body_folders:
        print(f"Missing laserfiche_repo or body_folders for {slug}")
        sys.exit(1)

    data_dir = agency_data_dir(slug)
    docs_dir = data_dir / "documents"
    meetings_dir = data_dir / "meetings"
    state_file = data_dir / "state.json"
    for d in [docs_dir, meetings_dir]:
        d.mkdir(parents=True, exist_ok=True)

    state = load_json(state_file) or {"last_fetch": None, "meetings": {}}
    years = float(args.years) if args.years else 1
    now = datetime.now()
    min_year = now.year - int(years) + 1
    max_year = now.year + 1

    client = LaserficheClient(repo, browse_id)
    print(f"Fetching {cfg.get('name', slug)} meetings (Laserfiche)...")

    new_meetings = 0
    doc_count = 0

    for body, folder_id in body_folders.items():
        print(f"\n  {body} (folder {folder_id})...")

        raw_docs = collect_docs(client, folder_id, min_year, max_year)
        print(f"    {len(raw_docs)} documents found")

        meetings = {}

        for entry_id, parent_name in raw_docs:
            parent_date = extract_date(parent_name)

            if parent_date and parent_date.year >= min_year:
                dt = parent_date
                doc_name = parent_name
            else:
                info = client.get_doc_info(entry_id)
                time.sleep(0.2)
                ext = info.get("extension", "")
                if ext and ext.lower() != "pdf":
                    continue
                doc_name = info.get("name", str(entry_id))
                dt = extract_date(doc_name, info.get("metadata"))

            if not dt or dt.year < min_year:
                continue

            mid = make_meeting_id(slug, body, dt)
            if mid not in meetings:
                meetings[mid] = {
                    "id": mid,
                    "body": body,
                    "date": dt.strftime("%m/%d/%Y"),
                    "title": f"{body} — {dt.strftime('%b %d, %Y')}",
                    "agency": slug,
                    "source": "laserfiche",
                    "docs": [],
                }
            meetings[mid]["docs"].append((entry_id, doc_name))

        print(f"    {len(meetings)} meetings")

        for mid, meeting in sorted(meetings.items()):
            mdir = meetings_dir / mid
            mdir.mkdir(exist_ok=True)

            is_new = mid not in state.get("meetings", {})
            meta = {k: v for k, v in meeting.items() if k != "docs"}
            save_json(mdir / "meeting.json", meta)

            if is_new:
                new_meetings += 1
                print(f"    NEW: {meeting['title']}")

            if mid in state["meetings"] and not getattr(args, "deep", False):
                continue

            for entry_id, doc_name in meeting["docs"]:
                fname = safe_filename(doc_name, max_len=60)
                dest = docs_dir / f"{mid}-{fname}.pdf"
                if dest.exists():
                    continue
                if client.download_pdf(entry_id, dest):
                    text = extract_text(dest)
                    if text:
                        doc_count += 1
                    time.sleep(0.5)
                else:
                    print(f"      Failed: {doc_name[:50]}")

            state["meetings"][mid] = {
                "fetched": datetime.now().isoformat(),
                "body": body,
            }

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index(slug, state, docs_dir)
    save_json(state_file, state)
    log_discovery(slug, meetings_new=new_meetings, docs_new=doc_count)
    print(f"\nDone. {new_meetings} new meetings, {doc_count} documents extracted.")


def cmd_list(args):
    slug = getattr(args, "agency", None)
    if slug:
        cmd_list_meetings(slug)


def main():
    parser = argparse.ArgumentParser(description="Laserfiche Public Portal scraper")
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch")
    p_fetch.add_argument("--agency", required=True)
    p_fetch.add_argument("--years", default="1")
    p_fetch.add_argument("--deep", action="store_true")

    sub.add_parser("list").add_argument("--agency", required=True)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    {"fetch": cmd_fetch, "list": cmd_list}[args.command](args)


if __name__ == "__main__":
    main()
