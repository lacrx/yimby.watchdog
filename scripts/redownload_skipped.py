#!/usr/bin/env python3
"""
Re-download and re-extract text for skipped documents.

Finds docs with .skip markers, re-downloads the source PDF from Legistar,
re-runs pdftotext, and removes the .skip marker if text extraction succeeds.
Only too_short skips are targeted unless --reason says otherwise. The new PDF is
downloaded to a temp dir and only replaces the existing one after %PDF validation.

Usage:
    python scripts/redownload_skipped.py --year 2022      # fix 2022 skips
    python scripts/redownload_skipped.py --year 2022 2023  # fix both years
    python scripts/redownload_skipped.py --dry-run         # preview only
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scrapers"))

from oceanside import (
    fetch_meeting_detail, fetch_legislation_attachments,
    download_pdf, extract_text, BASE_URL,
)
from civic_utils import agency_data_dir

DATA_DIR = agency_data_dir("oceanside")
DOC_DIR = DATA_DIR / "documents"
MEETINGS_DIR = DATA_DIR / "meetings"
STRUCTURED_DIR = REPO_ROOT / "data" / "structured"
MIN_TEXT_CHARS = 100


def load_doc_index():
    idx_path = DATA_DIR / "doc-index.json"
    return json.loads(idx_path.read_text()).get("documents", {})


def skip_reason(skip_path):
    try:
        return json.loads(skip_path.read_text()).get("reason", "unknown")
    except (json.JSONDecodeError, OSError, AttributeError):
        return "unknown"


def find_skipped_docs(years, reasons=("too_short",)):
    idx = load_doc_index()
    skipped = []
    for fname, meta in idx.items():
        d = meta.get("meeting_date", "") or meta.get("filed_date", "")
        if not d:
            continue
        if not any(d.startswith(y) for y in years):
            continue
        stem = fname[:-4] if fname.endswith(".txt") else fname
        json_path = STRUCTURED_DIR / (stem + ".json")
        skip_path = DOC_DIR / (fname + ".skip")
        if not json_path.exists() and skip_path.exists():
            reason = skip_reason(skip_path)
            if reasons and reason not in reasons:
                continue
            parts = stem.split("-")
            mid = parts[0]
            skipped.append({
                "fname": fname,
                "stem": stem,
                "mid": mid,
                "date": d,
                "body": meta.get("body", ""),
                "reason": reason,
            })
    return skipped


def attachment_slug(name):
    """Same slug the scraper uses in {mid}-{item_id}-{slug}.pdf filenames."""
    return re.sub(r"[^\w]", "_", name)[:40]


def match_attachment(attachments, file_slug):
    """URL of the attachment whose slugged name equals file_slug, else None.

    Never falls back to attachments[0]: a wrong attachment would silently replace
    the right document. Multiple equal slugs are ambiguous and also return None.
    """
    if not file_slug:
        return None
    file_slug = re.sub(r"-part\d+$", "", file_slug)
    hits = [a["url"] for a in attachments
            if a.get("name") and attachment_slug(a["name"]) == file_slug and a.get("url")]
    return hits[0] if len(set(hits)) == 1 else None


def get_attachment_url(mid, fname):
    """Find download URL for a specific attachment via Legistar."""
    import requests
    from bs4 import BeautifulSoup

    meeting_json = MEETINGS_DIR / mid / "meeting.json"
    if not meeting_json.exists():
        return None

    mj = json.loads(meeting_json.read_text())

    stem = fname[:-4] if fname.endswith(".txt") else fname
    parts = stem.split("-", 2)

    # agenda or minutes — use direct meeting-level URLs
    if stem.endswith("-agenda"):
        return mj.get("agenda_url")
    if stem.endswith("-minutes"):
        return mj.get("minutes_url")

    # Attachment: {mid}-{item_id}-{slug}; second segment is the legislation ID
    if len(parts) == 3 and parts[1].isdigit():
        leg_id, file_slug = parts[1], parts[2]

        # 1) Attachments the scraper cached for this item (the URL the doc came from)
        items_file = MEETINGS_DIR / mid / "items.json"
        if items_file.exists():
            try:
                for item in json.loads(items_file.read_text()):
                    if str(item.get("item_id")) == leg_id:
                        url = match_attachment(item.get("attachments") or [], file_slug)
                        if url:
                            return url
            except (json.JSONDecodeError, OSError, AttributeError) as e:
                print(f"    Error reading items.json: {e}")

        # 2) Re-crawl the legislation detail page
        detail_url = mj.get("detail_url", "")
        if detail_url:
            try:
                resp = requests.get(detail_url, timeout=30,
                    headers={"User-Agent": "Mozilla/5.0 yimby-watchdog/1.0"})
                if resp.status_code == 200:
                    soup = BeautifulSoup(resp.text, "lxml")
                    leg_link = soup.find("a", href=re.compile(
                        rf"LegislationDetail\.aspx\?ID={leg_id}"))
                    if leg_link:
                        from urllib.parse import urljoin
                        leg_url = urljoin(BASE_URL + "/", leg_link["href"])
                        return match_attachment(
                            fetch_legislation_attachments(leg_url), file_slug)
            except Exception as e:
                print(f"    Error fetching detail page: {e}")

    return None


def redownload_and_extract(doc, dry_run=False):
    """Re-download PDF and extract text for a single document."""
    fname = doc["fname"]
    stem = doc["stem"]
    mid = doc["mid"]
    txt_path = DOC_DIR / fname
    pdf_path = DOC_DIR / (stem + ".pdf")
    skip_path = DOC_DIR / (fname + ".skip")

    if dry_run:
        print(f"  [dry-run] Would re-download: {fname}")
        return False

    # Get download URL
    url = get_attachment_url(mid, fname)
    if not url:
        print(f"    No matching attachment URL found for {fname}")
        return False

    # Download into a fresh temp dir so download_pdf/extract_text early returns
    # (existing file) cannot short-circuit and the live files stay untouched on failure.
    tmp_dir = Path(tempfile.mkdtemp(prefix="redownload_"))
    try:
        tmp_pdf = tmp_dir / "dl.pdf"
        print(f"    Downloading {fname}...")
        if not download_pdf(url, tmp_pdf) or not tmp_pdf.exists():
            print(f"    Download failed for {fname}")
            return False
        with open(tmp_pdf, "rb") as fh:
            if fh.read(5) != b"%PDF-":
                print(f"    Downloaded file is not a PDF for {fname}")
                return False

        text = extract_text(tmp_pdf) or ""
        n = len(text.strip())
        if n < MIN_TEXT_CHARS:
            print(f"    Re-extracted text still too short ({n} chars)")
            return False

        # Validated: now replace the live files.
        shutil.move(str(tmp_pdf), str(pdf_path))
        existing = len(txt_path.read_text(errors="replace").strip()) if txt_path.exists() else 0
        if n > existing:
            txt_path.write_text(text)

        if skip_path.exists():
            skip_path.unlink()
            print(f"    Removed .skip marker, new text: {n} chars")
        return True
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="Re-download skipped documents")
    parser.add_argument("--year", nargs="+", required=True, help="Years to fix")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--reason", nargs="+", default=["too_short"],
                        help="Skip reasons to target (default: too_short; use 'any' for all)")
    args = parser.parse_args()

    reasons = None if "any" in args.reason else tuple(args.reason)
    skipped = find_skipped_docs(args.year, reasons)
    print(f"Found {len(skipped)} skipped docs for years: {args.year} (reason: {', '.join(args.reason)})")

    if not skipped:
        print("Nothing to fix.")
        return

    # Group by meeting for efficient crawling
    by_meeting = {}
    for doc in skipped:
        mid = doc["mid"]
        if mid not in by_meeting:
            by_meeting[mid] = []
        by_meeting[mid].append(doc)

    print(f"Across {len(by_meeting)} meetings\n")

    fixed = 0
    failed = 0
    for mid, docs in sorted(by_meeting.items()):
        print(f"Meeting {mid} ({docs[0]['body']}, {docs[0]['date']}):")
        for doc in docs:
            try:
                if redownload_and_extract(doc, dry_run=args.dry_run):
                    fixed += 1
                else:
                    failed += 1
            except Exception as e:
                print(f"    Error: {e}")
                failed += 1
        time.sleep(1)  # rate limit

    print(f"\nResults: {fixed} fixed, {failed} failed, {len(skipped) - fixed - failed} skipped")
    if fixed and not args.dry_run:
        print("Run extract_structured.py to process the newly extracted text.")


if __name__ == "__main__":
    main()
