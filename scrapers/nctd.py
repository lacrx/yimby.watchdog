#!/usr/bin/env python3
"""
NCTD (North County Transit District) board meeting monitor.

Scrapes gonctd.com/boardagenda/ for board and committee meetings,
downloads agenda packet PDFs, extracts text, and summarizes with Claude.

Usage:
    python nctd.py fetch          # pull meetings + download PDFs
    python nctd.py summarize      # summarize un-summarized documents
    python nctd.py search TERM    # full-text search across extracted text
    python nctd.py watch          # fetch + summarize + keyword alerts
    python nctd.py list           # list all tracked meetings

Requires: requests, beautifulsoup4, lxml, pdftotext (CLI)
Optional: anthropic (pip install anthropic) + ANTHROPIC_API_KEY env var
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup

from civic_utils import download_pdf, extract_text, save_json, load_json, agency_data_dir
import config

DATA_DIR = agency_data_dir("nctd")
MEETINGS_DIR = DATA_DIR / "meetings"
DOCS_DIR = DATA_DIR / "documents"
SUMMARIES_DIR = DATA_DIR / "summaries"
STATE_FILE = DATA_DIR / "state.json"

BOARD_AGENDA_URL = "https://gonctd.com/boardagenda/"

DEFAULT_WATCH_KEYWORDS = [
    "housing", "TOD", "transit-oriented", "density", "affordable",
    "development", "zoning", "SANDAG", "LOSSAN", "Coaster", "Sprinter",
    "BREEZE", "frequency", "headway", "fare", "ridership",
    "bus rapid transit", "BRT", "rail", "station",
    "infrastructure", "budget", "capital", "bond",
    "active transportation", "bike", "pedestrian", "sidewalk",
    "parking", "climate", "electrification", "zero emission",
]

WATCH_KEYWORDS = DEFAULT_WATCH_KEYWORDS + [
    config.get("identity/primary_city", "Oceanside"),
]


def ensure_dirs():
    for d in [MEETINGS_DIR, DOCS_DIR, SUMMARIES_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    return load_json(STATE_FILE) or {"last_fetch": None, "meetings": {}}


def save_state(state):
    save_json(STATE_FILE, state)


def fetch_board_agenda_page():
    """Fetch the NCTD board agenda page."""
    resp = requests.get(BOARD_AGENDA_URL, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"
    })
    resp.raise_for_status()
    return resp.text


def parse_meetings(html):
    """Extract meetings from NCTD board agenda page.

    Page uses <table class="agenda-table"> with <th> cells (not <td>).
    Layout: th[0]=date, th[1]=time, th[2]=type, th[3]=location, th[4]=agenda, th[5]=video.
    First two rows per table are headers. Year from first row text.
    """
    soup = BeautifulSoup(html, "lxml")
    meetings = []

    tables = soup.find_all("table", class_="agenda-table")

    for table in tables:
        rows = table.find_all("tr")
        if not rows:
            continue

        year_match = re.search(r"(20\d{2})", rows[0].get_text())
        year = int(year_match.group(1)) if year_match else datetime.now().year

        for row in rows[2:]:
            cells = row.find_all("th")
            if len(cells) < 3:
                continue

            date_text = cells[0].get_text(strip=True)
            if not re.match(r"[A-Z]", date_text):
                continue

            time_text = cells[1].get_text(strip=True)
            meeting_type = cells[2].get_text(strip=True)

            full_date = f"{date_text}, {year}"
            dt = None
            for fmt in ["%B %d, %Y", "%b %d, %Y"]:
                try:
                    dt = datetime.strptime(full_date, fmt)
                    break
                except ValueError:
                    pass

            links = row.find_all("a", href=True)
            pdf_url = next(
                (a["href"] for a in links if ".pdf" in a["href"].lower()), None
            )
            video_url = next(
                (a["href"] for a in links if "youtube" in a["href"].lower()), None
            )

            meeting_id = hashlib.md5(
                f"nctd-{date_text}-{year}-{meeting_type}".encode()
            ).hexdigest()[:12]

            meetings.append({
                "id": meeting_id,
                "body": f"NCTD {meeting_type}" if meeting_type else "NCTD Board",
                "date": dt.strftime("%m/%d/%Y") if dt else full_date,
                "time": time_text,
                "pdf_url": pdf_url,
                "video_url": video_url,
                "agency": "NCTD",
            })

    return meetings


def keyword_scan(text, keywords=None):
    """Find keyword matches in text."""
    if keywords is None:
        keywords = WATCH_KEYWORDS
    matches = []
    text_lower = text.lower()
    lines = text.split("\n")
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            for line in lines:
                if kw_lower in line.lower():
                    context = line.strip()[:200]
                    matches.append((kw, context))
                    break
    return matches


# ── Commands ──


def cmd_fetch(args):
    """Fetch NCTD meetings and download agenda packets."""
    ensure_dirs()
    state = load_state()

    print("Fetching NCTD board agenda page...")
    html = fetch_board_agenda_page()
    meetings = parse_meetings(html)
    print(f"Found {len(meetings)} meetings")

    for m in meetings:
        mid = m["id"]
        meeting_dir = MEETINGS_DIR / mid
        meeting_dir.mkdir(exist_ok=True)

        meta_file = meeting_dir / "meeting.json"
        meta_file.write_text(json.dumps(m, indent=2))

        print(f"\n{m['body']} — {m['date']} {m['time']}")

        if m.get("pdf_url"):
            pdf_path = DOCS_DIR / f"{mid}-agenda.pdf"
            print(f"  Downloading agenda packet...")
            if download_pdf(m["pdf_url"], pdf_path):
                text = extract_text(pdf_path)
                print(f"  Extracted {len(text)} chars")
            else:
                print(f"  No PDF available")

        state["meetings"][mid] = {
            "body": m["body"],
            "date": m["date"],
            "fetched": datetime.now().isoformat(),
        }
        time.sleep(0.5)

    state["last_fetch"] = datetime.now().isoformat()
    save_state(state)
    print(f"\nDone. Data stored in {DATA_DIR}")


def _get_summarizer(args):
    raise NotImplementedError("Summarization moved to yimby.analysis")


def cmd_summarize(args):
    """Summarize un-summarized NCTD documents."""
    ensure_dirs()
    summarizer = _get_summarizer(args)

    if getattr(args, "summarizer", "api") == "api":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY not set.")
            sys.exit(1)

    txt_files = sorted(DOCS_DIR.glob("*.txt"))
    if not txt_files:
        print("No extracted text files found. Run 'fetch' first.")
        return

    for txt_path in txt_files:
        summary_path = SUMMARIES_DIR / f"{txt_path.stem}-summary.md"
        if summary_path.exists() and not args.force:
            continue

        text = txt_path.read_text()
        if len(text.strip()) < 100:
            continue

        mid = txt_path.stem.rsplit("-", 1)[0]
        meeting_meta = load_json(MEETINGS_DIR / mid / "meeting.json")
        meeting_meta.setdefault("agency", "NCTD")

        doc_type = "agenda packet"
        print(f"Summarizing {meeting_meta.get('body', '?')} {meeting_meta.get('date', '?')} ({doc_type})...")

        summary = summarizer(text, meeting_meta)
        if summary:
            header = f"# {meeting_meta.get('body', 'NCTD Meeting')} — {meeting_meta.get('date', 'Unknown')}\n"
            header += f"**Document:** {doc_type}\n\n"
            summary_path.write_text(header + summary)
            print(f"  Saved summary ({len(summary)} chars)")
        else:
            print(f"  Failed to summarize")

        time.sleep(1)


def cmd_search(args):
    """Full-text search across NCTD documents."""
    ensure_dirs()
    query = " ".join(args.terms).lower()

    txt_files = sorted(DOCS_DIR.glob("*.txt"))
    summary_files = sorted(SUMMARIES_DIR.glob("*.md"))
    all_files = txt_files + summary_files

    if not all_files:
        print("No documents found. Run 'fetch' first.")
        return

    hits = 0
    for fpath in all_files:
        text = fpath.read_text()
        if query not in text.lower():
            continue
        hits += 1
        mid = fpath.stem.rsplit("-", 1)[0]
        meta = load_json(MEETINGS_DIR / mid / "meeting.json")
        label = f"{meta.get('body', '?')} — {meta.get('date', '?')} ({fpath.suffix})"
        print(f"\n{'='*60}")
        print(f"MATCH: {label}")
        for i, line in enumerate(text.split("\n")):
            if query in line.lower():
                print(f"  L{i+1}: {line.strip()[:120]}")

    print(f"\n{hits} file(s) matched '{query}'")


def cmd_watch(args):
    """Fetch + summarize + keyword alerts for NCTD."""
    cmd_fetch(args)

    txt_files = sorted(DOCS_DIR.glob("*.txt"))
    alerts = []

    for txt_path in txt_files:
        text = txt_path.read_text()
        matches = keyword_scan(text)
        if matches:
            mid = txt_path.stem.rsplit("-", 1)[0]
            meta = load_json(MEETINGS_DIR / mid / "meeting.json")
            for kw, context in matches:
                alerts.append({
                    "keyword": kw,
                    "meeting": f"{meta.get('body', '?')} — {meta.get('date', '?')}",
                    "context": context,
                })

    if alerts:
        print(f"\n{'='*60}")
        print(f"NCTD KEYWORD ALERTS ({len(alerts)} matches)")
        print(f"{'='*60}")
        seen = set()
        for a in alerts:
            key = (a["keyword"], a["meeting"])
            if key not in seen:
                seen.add(key)
                print(f"\n  [{a['keyword'].upper()}] {a['meeting']}")
                print(f"  > {a['context']}")
    else:
        print("\nNo keyword matches found.")

    mode = getattr(args, "summarizer", "api")
    if mode == "local":
        cmd_summarize(args)
    elif os.environ.get("ANTHROPIC_API_KEY"):
        cmd_summarize(args)


def cmd_list(args):
    """List tracked NCTD meetings."""
    ensure_dirs()
    state = load_state()

    if not state.get("meetings"):
        print("No meetings tracked. Run 'fetch' first.")
        return

    for mid, info in sorted(state["meetings"].items(), key=lambda x: x[1].get("date", "")):
        body = info.get("body", "?")
        date = info.get("date", "?")
        has_text = (DOCS_DIR / f"{mid}-agenda.txt").exists()
        has_summary = any(SUMMARIES_DIR.glob(f"{mid}-*-summary.md"))
        flags = []
        if has_text:
            flags.append("extracted")
        if has_summary:
            flags.append("summarized")
        print(f"  {date:12s}  {body:30s}  [{', '.join(flags) or 'metadata only'}]")


def main():
    parser = argparse.ArgumentParser(
        description="NCTD board meeting monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("fetch", help="Pull NCTD meetings and download agenda packets")

    p_sum = sub.add_parser("summarize", help="Summarize documents with Claude")
    p_sum.add_argument("--force", action="store_true")
    p_sum.add_argument("--summarizer", choices=["api", "local"], default="api", help="api=Claude API, local=claude -p (subscription)")

    p_search = sub.add_parser("search", help="Full-text search")
    p_search.add_argument("terms", nargs="+")

    p_watch = sub.add_parser("watch", help="Fetch + summarize + keyword alerts")
    p_watch.add_argument("--force", action="store_true")
    p_watch.add_argument("--summarizer", choices=["api", "local"], default="api", help="api=Claude API, local=claude -p (subscription)")

    sub.add_parser("list", help="List tracked meetings")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "fetch": cmd_fetch,
        "summarize": cmd_summarize,
        "search": cmd_search,
        "watch": cmd_watch,
        "list": cmd_list,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
