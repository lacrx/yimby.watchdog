#!/usr/bin/env python3
"""
Oceanside, CA civic monitor.

Scrapes the Legistar portal for City Council and Planning Commission meetings,
downloads agendas/minutes/staff reports, extracts text, and optionally
summarizes with Claude. Stores everything locally for search and review.

Usage:
    python oceanside.py fetch          # pull latest meetings + documents
    python oceanside.py summarize      # summarize un-summarized documents with Claude
    python oceanside.py search TERM    # full-text search across all extracted text
    python oceanside.py watch          # fetch + summarize + print alerts for keywords
    python oceanside.py list           # list all tracked meetings

Requires: requests, beautifulsoup4, lxml, pdftotext (CLI)
Optional: anthropic (pip install anthropic) + ANTHROPIC_API_KEY env var
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from bs4 import BeautifulSoup
from civic_utils import agency_data_dir, rebuild_doc_index, log_discovery

ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BASE_URL = "https://oceanside.legistar.com"
DATA_DIR = agency_data_dir("oceanside")
MEETINGS_DIR = DATA_DIR / "meetings"
DOCS_DIR = DATA_DIR / "documents"
SUMMARIES_DIR = DATA_DIR / "summaries"
STATE_FILE = DATA_DIR / "state.json"

WATCH_KEYWORDS = [
    "housing", "ADU", "accessory dwelling", "rezone", "rezoning",
    "zoning", "density", "affordable", "development", "permit",
    "variance", "conditional use", "general plan", "specific plan",
    "short-term rental", "STR", "vacation rental",
    "infrastructure", "water", "sewer", "traffic",
    "budget", "tax", "fee", "assessment",
    "eminent domain", "CEQA", "environmental",
]

BODIES = {
    "City Council": None,
    "Planning Commission": None,
    "Housing Commission": None,
    "Manufactured Home Fair Practices Commission": None,
    "Downtown Advisory Committee": None,
    "Historic Preservation Advisory Commission": None,
    "Economic Development Commission": None,
    "Utilities Commission": None,
    "Harbor and Beaches Advisory Committee": None,
    "Citizen Investment Oversight Committee": None,
    "Measure X Citizen Oversight Committee": None,
}


def ensure_dirs():
    for d in [MEETINGS_DIR, DOCS_DIR, SUMMARIES_DIR]:
        d.mkdir(parents=True, exist_ok=True)


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_fetch": None, "meetings": {}}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


def fetch_calendar_page(year=None):
    """Scrape the Legistar calendar page for meeting listings."""
    if year is None:
        year = datetime.now().year
    url = f"{BASE_URL}/Calendar.aspx?From=RSS&Mode={year}"
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"
    })
    resp = session.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def parse_meetings(html):
    """Extract meeting info from Legistar calendar HTML."""
    soup = BeautifulSoup(html, "lxml")
    meetings = []

    rows = soup.select("tr.rgRow, tr.rgAltRow")
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 8:
            continue

        name = cells[0].get_text(strip=True)
        date_str = cells[1].get_text(strip=True)
        time_str = cells[3].get_text(strip=True)

        detail_link = cells[4].find("a", href=True)
        agenda_link = cells[6].find("a", href=True)
        minutes_link = cells[7].find("a", href=True)

        meeting = {
            "body": name,
            "date": date_str,
            "time": time_str,
            "detail_url": urljoin(BASE_URL + "/", detail_link["href"]) if detail_link else None,
            "agenda_url": urljoin(BASE_URL + "/", agenda_link["href"]) if agenda_link else None,
            "minutes_url": urljoin(BASE_URL + "/", minutes_link["href"]) if minutes_link else None,
            "id": None,
        }

        if detail_link and "ID=" in detail_link["href"]:
            match = re.search(r"ID=(\d+)", detail_link["href"])
            if match:
                meeting["id"] = match.group(1)

        if not meeting["id"] and date_str:
            meeting["id"] = hashlib.md5(f"{name}-{date_str}".encode()).hexdigest()[:12]

        meetings.append(meeting)

    return meetings


def fetch_meeting_detail(detail_url):
    """Get agenda items and attachment links from a meeting detail page."""
    resp = requests.get(detail_url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"
    })
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    items = []
    item_links = soup.find_all("a", href=re.compile(r"LegislationDetail\.aspx"))
    for link in item_links:
        row = link.find_parent("tr")
        item = {
            "title": link.get_text(strip=True),
            "url": urljoin(BASE_URL + "/", link["href"]),
            "item_id": None,
            "attachments": [],
        }
        match = re.search(r"ID=(\d+)", link["href"])
        if match:
            item["item_id"] = match.group(1)

        if row:
            cells = row.find_all("td")
            texts = [c.get_text(strip=True) for c in cells]
            item["row_text"] = " | ".join(t for t in texts if t)

        items.append(item)

    return items


def fetch_legislation_attachments(legislation_url):
    """Get staff report and other attachment PDFs from a legislation detail page."""
    resp = requests.get(legislation_url, timeout=30, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"
    })
    resp.raise_for_status()

    attachments = []
    pattern = r'<a\s+href="(View\.ashx\?M=F&ID=(\d+)&GUID=([A-F0-9-]+))"[^>]*>([^<]+)</a>'
    for match in re.finditer(pattern, resp.text):
        rel_url, file_id, guid, name = match.groups()
        attachments.append({
            "name": name.strip(),
            "url": f"{BASE_URL}/{rel_url}",
        })
    return attachments


def download_pdf(url, dest_path):
    """Download a PDF from Legistar."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    try:
        resp = requests.get(url, timeout=60, headers={
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"
        }, allow_redirects=True)
        resp.raise_for_status()
        if b"%PDF" in resp.content[:10]:
            dest_path.write_bytes(resp.content)
            return True
        else:
            return False
    except Exception as e:
        print(f"  download failed: {e}")
        return False


def extract_text(pdf_path):
    """Extract text from a PDF using pdftotext."""
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 0:
        return txt_path.read_text()
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), str(txt_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and txt_path.exists():
            return txt_path.read_text()
    except Exception as e:
        print(f"  pdftotext failed: {e}")
    return ""


def summarize_text(text, meeting_info, model="claude-sonnet-4-6"):
    """Summarize meeting document text using Claude."""
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""Summarize this local government document from Oceanside, CA.

Meeting: {meeting_info.get('body', 'Unknown')} — {meeting_info.get('date', 'Unknown date')}

Provide:
1. A 2-3 sentence overview
2. Key decisions or action items (bulleted)
3. Any items related to: housing, zoning, development, permits, budget, infrastructure, environmental
4. Notable public comments or controversies

Be concise. If the document is just procedural (roll call, adjournment), say so in one line.

Document text:
{text[:80000]}"""

    try:
        response = client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"  Claude API error: {e}")
        return None



def keyword_scan(text, keywords=None):
    """Find keyword matches in text. Returns list of (keyword, context) tuples."""
    if keywords is None:
        keywords = WATCH_KEYWORDS
    matches = []
    text_lower = text.lower()
    lines = text.split("\n")
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower in text_lower:
            for i, line in enumerate(lines):
                if kw_lower in line.lower():
                    context = line.strip()
                    if len(context) > 200:
                        context = context[:200] + "..."
                    matches.append((kw, context))
                    break
    return matches


# ── Commands ──


def cmd_fetch(args):
    """Fetch latest meetings and download documents."""
    ensure_dirs()
    state = load_state()

    current_year = datetime.now().year
    years = list(range(current_year - (args.years - 1), current_year + 1)) if hasattr(args, "years") and args.years > 1 else [current_year]

    meetings = []
    for year in years:
        print(f"Fetching Oceanside Legistar calendar ({year})...")
        html = fetch_calendar_page(year)
        year_meetings = parse_meetings(html)
        meetings.extend(year_meetings)
        if len(years) > 1:
            time.sleep(1)

    print(f"Found {len(meetings)} meetings across {len(years)} year(s)")

    force = getattr(args, "force", False)
    recency_cutoff = datetime.now() - timedelta(days=30)
    skipped = 0

    for m in meetings:
        mid = m["id"]
        if not mid:
            continue

        meeting_dir = MEETINGS_DIR / mid
        meeting_dir.mkdir(exist_ok=True)

        meta_file = meeting_dir / "meeting.json"
        meta_file.write_text(json.dumps(m, indent=2))

        already_fetched = mid in state.get("meetings", {})
        if already_fetched and not force:
            meeting_date = None
            try:
                meeting_date = datetime.strptime(m["date"].strip(), "%m/%d/%Y")
            except (ValueError, AttributeError):
                pass
            if meeting_date and meeting_date < recency_cutoff:
                skipped += 1
                continue
            # Recent meeting: skip if agenda/minutes already present and no new minutes URL
            prev = state["meetings"].get(mid, {})
            has_agenda = (DOCS_DIR / f"{mid}-agenda.pdf").exists() if m.get("agenda_url") else True
            has_minutes = (DOCS_DIR / f"{mid}-minutes.pdf").exists() if m.get("minutes_url") else True
            minutes_unchanged = prev.get("minutes_url") == m.get("minutes_url")
            if has_agenda and has_minutes and minutes_unchanged:
                skipped += 1
                continue
            state["meetings"].setdefault(mid, {})["minutes_url"] = m.get("minutes_url")

        print(f"\n{m['body']} — {m['date']} {m['time']}")

        if m.get("agenda_url"):
            agenda_path = DOCS_DIR / f"{mid}-agenda.pdf"
            print(f"  Downloading agenda...")
            if download_pdf(m["agenda_url"], agenda_path):
                text = extract_text(agenda_path)
                print(f"  Extracted {len(text)} chars from agenda")
            else:
                print(f"  No PDF available")

        if m.get("minutes_url"):
            minutes_path = DOCS_DIR / f"{mid}-minutes.pdf"
            print(f"  Downloading minutes...")
            if download_pdf(m["minutes_url"], minutes_path):
                text = extract_text(minutes_path)
                print(f"  Extracted {len(text)} chars from minutes")
            else:
                print(f"  No PDF available")

        if m.get("detail_url"):
            print(f"  Fetching agenda items...")
            try:
                items = fetch_meeting_detail(m["detail_url"])
                items_file = meeting_dir / "items.json"
                items_file.write_text(json.dumps(items, indent=2))
                print(f"  Found {len(items)} agenda items")

                if args.deep:
                    deep_skipped = 0
                    deep_new = 0
                    for item in items:
                        if not item.get("url"):
                            continue
                        # Skip crawl if cached attachments all have PDFs on disk
                        cached_atts = item.get("attachments")
                        if cached_atts:
                            item_id = item.get('item_id', 'x')
                            att_slugs = [re.sub(r"[^\w]", "_", a["name"])[:40] for a in cached_atts]
                            all_present = all(
                                (DOCS_DIR / f"{mid}-{item_id}-{slug}.pdf").exists()
                                for slug in att_slugs
                            )
                            if all_present:
                                deep_skipped += 1
                                continue
                        try:
                            attachments = fetch_legislation_attachments(item["url"])
                            item["attachments"] = attachments
                            for att in attachments:
                                slug = re.sub(r"[^\w]", "_", att["name"])[:40]
                                att_path = DOCS_DIR / f"{mid}-{item.get('item_id','x')}-{slug}.pdf"
                                if download_pdf(att["url"], att_path):
                                    text = extract_text(att_path)
                                    if text:
                                        print(f"    Staff report: {att['name'][:50]} ({len(text)} chars)")
                            deep_new += 1
                            time.sleep(0.5)
                        except Exception as e:
                            print(f"    Failed to fetch attachments for {item.get('title','?')[:40]}: {e}")
                    if deep_skipped:
                        print(f"  Skipped {deep_skipped} items (attachments already on disk)")
                    items_file.write_text(json.dumps(items, indent=2))
            except Exception as e:
                print(f"  Failed to fetch items: {e}")

        state["meetings"][mid] = {
            "body": m["body"],
            "date": m["date"],
            "minutes_url": m.get("minutes_url"),
            "fetched": datetime.now().isoformat(),
        }
        time.sleep(1)

    if skipped:
        print(f"\nSkipped {skipped} previously fetched meetings (unchanged)")

    state["last_fetch"] = datetime.now().isoformat()
    rebuild_doc_index("oceanside", state, DOCS_DIR)
    save_state(state)
    log_discovery("oceanside", meetings_found=len(meetings), meetings_new=len(meetings) - skipped)
    print(f"\nDone. Data stored in {DATA_DIR}")


def _get_summarizer(args):
    """Return the summarize function based on --summarizer flag."""
    mode = getattr(args, "summarizer", "api")
    if mode == "local":
        from civic_utils import summarize_text_local
        return summarize_text_local
    return summarize_text


def cmd_summarize(args):
    """Summarize un-summarized documents with Claude."""
    ensure_dirs()
    state = load_state()
    summarizer = _get_summarizer(args)

    if getattr(args, "summarizer", "api") == "api":
        try:
            import anthropic
        except ImportError:
            print("anthropic package not installed. Run: pip install anthropic")
            sys.exit(1)
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
        meeting_meta = {}
        meta_file = MEETINGS_DIR / mid / "meeting.json"
        if meta_file.exists():
            meeting_meta = json.loads(meta_file.read_text())

        doc_type = "agenda" if "agenda" in txt_path.stem else "minutes"
        print(f"Summarizing {meeting_meta.get('body', '?')} {meeting_meta.get('date', '?')} ({doc_type})...")

        summary = summarizer(text, meeting_meta)
        if summary:
            header = f"# {meeting_meta.get('body', 'Meeting')} — {meeting_meta.get('date', 'Unknown')}\n"
            header += f"**Document:** {doc_type}\n\n"
            summary_path.write_text(header + summary)
            print(f"  Saved summary ({len(summary)} chars)")
        else:
            print(f"  Failed to summarize")

        time.sleep(1)


def cmd_search(args):
    """Full-text search across all extracted documents."""
    ensure_dirs()
    query = " ".join(args.terms).lower()
    if not query:
        print("Usage: oceanside.py search TERM [TERM ...]")
        return

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
        meta_file = MEETINGS_DIR / mid / "meeting.json"
        label = fpath.name
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            label = f"{meta.get('body', '?')} — {meta.get('date', '?')} ({fpath.suffix})"

        print(f"\n{'='*60}")
        print(f"MATCH: {label}")
        print(f"File: {fpath}")

        for i, line in enumerate(text.split("\n")):
            if query in line.lower():
                print(f"  L{i+1}: {line.strip()[:120]}")

    print(f"\n{hits} file(s) matched '{query}'")


def cmd_watch(args):
    """Fetch + summarize + scan for keyword alerts."""
    cmd_fetch(args)

    txt_files = sorted(DOCS_DIR.glob("*.txt"))
    alerts = []

    for txt_path in txt_files:
        text = txt_path.read_text()
        matches = keyword_scan(text, args.keywords if hasattr(args, "keywords") and args.keywords else None)
        if matches:
            mid = txt_path.stem.rsplit("-", 1)[0]
            meta_file = MEETINGS_DIR / mid / "meeting.json"
            meta = {}
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())

            for kw, context in matches:
                alerts.append({
                    "keyword": kw,
                    "meeting": f"{meta.get('body', '?')} — {meta.get('date', '?')}",
                    "context": context,
                    "file": str(txt_path),
                })

    if alerts:
        print(f"\n{'='*60}")
        print(f"KEYWORD ALERTS ({len(alerts)} matches)")
        print(f"{'='*60}")
        seen = set()
        for a in alerts:
            key = (a["keyword"], a["meeting"])
            if key in seen:
                continue
            seen.add(key)
            print(f"\n  [{a['keyword'].upper()}] {a['meeting']}")
            print(f"  > {a['context']}")
    else:
        print("\nNo keyword matches found.")

    mode = getattr(args, "summarizer", "api")
    if mode == "local":
        cmd_summarize(args)
    else:
        has_api = os.environ.get("ANTHROPIC_API_KEY") and True
        if has_api:
            try:
                import anthropic
                cmd_summarize(args)
            except ImportError:
                pass



def cmd_list(args):
    """List all tracked meetings."""
    ensure_dirs()
    state = load_state()

    if not state["meetings"]:
        print("No meetings tracked. Run 'fetch' first.")
        return

    for mid, info in sorted(state["meetings"].items(), key=lambda x: x[1].get("date", "")):
        body = info.get("body", "?")
        date = info.get("date", "?")

        has_agenda = (DOCS_DIR / f"{mid}-agenda.txt").exists()
        has_minutes = (DOCS_DIR / f"{mid}-minutes.txt").exists()
        has_summary = any(SUMMARIES_DIR.glob(f"{mid}-*-summary.md"))

        flags = []
        if has_agenda: flags.append("agenda")
        if has_minutes: flags.append("minutes")
        if has_summary: flags.append("summarized")

        print(f"  {date:12s}  {body:25s}  [{', '.join(flags) or 'metadata only'}]")


def main():
    parser = argparse.ArgumentParser(
        description="Oceanside, CA civic monitor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    p_fetch = sub.add_parser("fetch", help="Pull latest meetings and documents")
    p_fetch.add_argument("--deep", action="store_true", help="Also download staff reports from each agenda item")
    p_fetch.add_argument("--years", type=int, default=1, help="How many years back to fetch (default: 1 = current year only)")

    p_sum = sub.add_parser("summarize", help="Summarize documents with Claude")
    p_sum.add_argument("--force", action="store_true", help="Re-summarize already summarized docs")
    p_sum.add_argument("--summarizer", choices=["api", "local"], default="api", help="api=Claude API, local=claude -p (subscription)")

    p_search = sub.add_parser("search", help="Full-text search")
    p_search.add_argument("terms", nargs="+", help="Search terms")

    p_watch = sub.add_parser("watch", help="Fetch + summarize + keyword alerts")
    p_watch.add_argument("--keywords", nargs="+", help="Custom keywords (default: housing/zoning/etc)")
    p_watch.add_argument("--force", action="store_true")
    p_watch.add_argument("--deep", action="store_true", help="Also download staff reports")
    p_watch.add_argument("--years", type=int, default=1, help="How many years back to fetch")
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
