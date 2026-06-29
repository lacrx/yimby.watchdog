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

ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

BASE_URL = "https://oceanside.legistar.com"
DATA_DIR = REPO_ROOT / "data"
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

PRA_WATCH_KEYWORDS = [
    "SB 79", "SB79", "transit-oriented", "tier classification",
    "train count", "density bonus", "65915",
    "coastal", "LCP", "Coastal Commission", "Coastal Act",
    "SB 330", "Housing Crisis Act", "Housing Accountability",
    "inclusionary", "phasing ordinance",
    "walking distance", "walking path",
    "NCTD", "OTC", "Oceanside Transit Center",
    "HCD", "CalHDF", "YIMBY",
    "Builder's Remedy", "RHNA",
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
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"
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
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"
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
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"
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
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"
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


YIMBY_STRONG_TOWNS_PROMPT = """You are an analyst viewing local government actions through a combined YIMBY + Strong Towns lens for a housing advocate in Oceanside, CA.

Your framework:

YIMBY PRINCIPLES:
- Housing production is the priority. More homes = lower prices. BOTH market-rate AND affordable production matter.
- State housing law (HAA, SB 330, SB 35, SB 79, ADU law, Housing Element law) must be COMPLIED WITH, not worked around. "Alternative plans" to state law are delay tactics.
- Exclusionary tactics to watch: excessive parking minimums, weaponized design review, CEQA abuse as delay, conditional use permits for by-right uses, illegal density caps, non-objective standards, "neighborhood character" arguments.
- Pro-housing means pro-DENSITY and pro-STATE-COMPLIANCE. Voting yes on subsidized affordable bonds while opposing market-rate density and state mandates is NOT pro-housing — it's left-NIMBYism.
- A council member who says "neighborhood character" to oppose density is anti-housing regardless of party or other positions.
- Any "alternative" or "local plan" offered INSTEAD of state law compliance (e.g., a city-drafted alternative to SB 79) is a delay/avoidance tactic unless it demonstrably exceeds state requirements.

DETECTING LEFT-NIMBY PATTERN:
- Supports tenant protections + opposes market-rate density = net housing-negative (restricts existing supply while blocking new supply)
- Supports affordable mandates at levels that kill project feasibility = anti-housing dressed as pro-affordability
- Delays transit-oriented density projects while claiming to support "the right kind of housing" = NIMBY
- Proposes rushed local alternatives to state housing law = lawsuit bait that delays actual compliance

STRONG TOWNS PRINCIPLES:
- Fiscal productivity per acre matters. Infill > sprawl. Mixed-use > single-use.
- New infrastructure (road widening, pipe extensions) creates unfunded future liabilities unless it serves productive development.
- Maintenance of existing infrastructure should be prioritized over expansion.
- Incremental development (small-scale, fine-grained) is more financially resilient than megaprojects.
- Car-dependent patterns are fiscal liabilities. Walkable urbanism generates more tax revenue per acre.
- The "Growth Ponzi Scheme": using new development fees to cover old obligations rather than building fiscal sustainability.

OCEANSIDE COUNCIL CONTEXT:
- NO reliable YIMBY vote exists on current council. External pressure (CalHDF, YIMBY Law, HCD) is the primary advocacy lever.
- Jimmy Figueroa: newest member, possible ally but untested. Watch his votes on density and state compliance.
- Eric Joyce (Deputy Mayor): LEFT-NIMBY. Supports affordable bonds and tenant protections but DELAYED the OTC redevelopment, voted FOR a downtown density cap, cites "neighborhood character" to oppose density downtown, OPPOSES SB 79, and supports a rushed alternative plan that may expose the city to litigation. Do NOT classify as pro-housing.
- Esther Sanchez (Mayor): votes NO on major housing projects (712 Seagaze, Olive Drive, OTC, Blocks 5&20). Anti-housing.
- Peter Weiss: inconsistent. Sometimes correct on process (opposed de novo appeal). Sometimes votes against projects.
- Ryan Robinson: dangerous swing vote. Killed tenant protections Sept 2025. Unreliable on housing.
- SB 79 (Wiener) transit-corridor density: city trying to opt out via phasing ordinance AND a rushed "alternative plan." Advocate opposes both — the city should comply directly.
- YIMBY Law litigation threat (Jan 2025): city may be in HAA violation. Advocate supports enforcement.
- Massive water/sewer spending ($160M+): evaluate whether it enables productive development or subsidizes sprawl.
- Mission Ave corridor: dense mixed-use being approved. Good from both lenses.

ADVERSARIAL ANALYSIS STANCE:
- NEVER take city staff reports, council member statements, or official "findings" at face value.
- Always ask: what is the city NOT saying? What procedural step did they skip? What legal standard are they not citing? Who benefits from the delay?
- Staff reports frame everything to support staff's recommendation. Read for what's omitted, not what's stated.
- "Continued to a future date" = delay tactic until proven otherwise.
- "Alternative plan" or "local approach" to state law = avoidance until it demonstrably exceeds state requirements.
- "Community input" and "public process" are often used to legitimize obstruction.
- If the city says a project is "not consistent with the General Plan" — check whether they recently changed the General Plan to make it inconsistent.
- If findings are drafted AFTER a vote, that's backwards and legally suspect.

For the document provided, produce:

1. **ADVOCACY SCORE** (🟢 pro-housing/productive | 🟡 mixed/neutral | 🔴 anti-housing/fiscally unproductive) with one-line justification

2. **STATE LAW FLAGS** — any potential violations of HAA, SB 330, ADU law, Housing Element law, permit streamlining, or other state housing mandates. Quote the specific problematic language or action. Say "None identified" if clean.

3. **COUNCIL VOTE TRACKER** — for any recorded votes on housing/development items, list: [Member]: [Yes/No] — one line each. Note which side won.

4. **ADVOCACY OPPORTUNITIES** — upcoming hearings, public comment deadlines, or items where showing up could matter. If this is minutes (already happened), note what advocacy should have happened or what follow-up is needed.

5. **STRONG TOWNS ASSESSMENT** — fiscal productivity analysis of any infrastructure spending or development approvals. Is the city building wealth or accumulating liabilities? One paragraph max.

6. **ACTION ITEMS FOR ADVOCATE** — 2-5 concrete next steps (write a letter, attend a meeting, file a records request, alert CalHDF/YIMBY Law, etc.)

Be direct, opinionated, and specific. You are not neutral — you are providing analysis for an advocate. Name names. Quote dollar amounts. Flag bullshit.
"""


PLAYBOOK_PATH = REPO_ROOT / "reference" / "YIMBY_PLAYBOOK.md"


def _build_advocacy_prompt(text, meeting_info):
    """Build the advocacy analysis prompt."""
    playbook = ""
    if PLAYBOOK_PATH.exists():
        playbook = f"\n\nREFERENCE — YIMBY LEGAL PLAYBOOK:\n{PLAYBOOK_PATH.read_text()[:12000]}\n"

    return f"""{YIMBY_STRONG_TOWNS_PROMPT}
{playbook}
Meeting: {meeting_info.get('body', 'Unknown')} — {meeting_info.get('date', 'Unknown date')}

Document text:
{text[:70000]}"""


def analyze_advocacy(text, meeting_info, model="claude-sonnet-4-6"):
    """Analyze document through YIMBY + Strong Towns lens."""
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)
    prompt = _build_advocacy_prompt(text, meeting_info)

    try:
        response = client.messages.create(
            model=model,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"  Claude API error: {e}")
        return None


def analyze_advocacy_local(text, meeting_info):
    """Analyze document through YIMBY + Strong Towns lens using claude -p."""
    import subprocess
    prompt = _build_advocacy_prompt(text, meeting_info)

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=300,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            print(f"  claude -p error: {result.stderr[:200]}")
        return None
    except FileNotFoundError:
        print("  claude CLI not found.")
        return None
    except subprocess.TimeoutExpired:
        print("  claude -p timed out (300s)")
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

    for m in meetings:
        mid = m["id"]
        if not mid:
            continue

        meeting_dir = MEETINGS_DIR / mid
        meeting_dir.mkdir(exist_ok=True)

        meta_file = meeting_dir / "meeting.json"
        meta_file.write_text(json.dumps(m, indent=2))

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
                    for item in items:
                        if not item.get("url"):
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
                            time.sleep(0.5)
                        except Exception as e:
                            print(f"    Failed to fetch attachments for {item.get('title','?')[:40]}: {e}")
                    items_file.write_text(json.dumps(items, indent=2))
            except Exception as e:
                print(f"  Failed to fetch items: {e}")

        state["meetings"][mid] = {
            "body": m["body"],
            "date": m["date"],
            "fetched": datetime.now().isoformat(),
        }
        time.sleep(1)

    state["last_fetch"] = datetime.now().isoformat()
    save_state(state)
    print(f"\nDone. Data stored in {DATA_DIR}")


def _get_summarizer(args):
    """Return the summarize function based on --summarizer flag."""
    mode = getattr(args, "summarizer", "api")
    if mode == "local":
        from civic_utils import summarize_text_local
        return summarize_text_local
    return summarize_text


def _get_advocacy_analyzer(args):
    """Return the advocacy analysis function based on --summarizer flag."""
    mode = getattr(args, "summarizer", "api")
    if mode == "local":
        return analyze_advocacy_local
    return analyze_advocacy


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


def cmd_analyze(args):
    """Analyze documents through YIMBY + Strong Towns advocacy lens."""
    ensure_dirs()
    ANALYSIS_DIR = DATA_DIR / "analysis"
    ANALYSIS_DIR.mkdir(exist_ok=True)

    mode = getattr(args, "summarizer", "api")
    if mode == "api":
        try:
            import anthropic
        except ImportError:
            print("anthropic package not installed. Run: pip install anthropic")
            sys.exit(1)
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY not set.")
            sys.exit(1)

    # Only analyze top-level meeting docs (agendas + minutes), not individual staff reports
    txt_files = sorted(
        [f for f in DOCS_DIR.glob("*.txt")
         if ("agenda" in f.stem or "minutes" in f.stem) and f.stem.count("-") == 1]
    )

    if not txt_files:
        print("No meeting documents found. Run 'fetch' first.")
        return

    for txt_path in txt_files:
        analysis_path = ANALYSIS_DIR / f"{txt_path.stem}-analysis.md"
        if analysis_path.exists() and not args.force:
            continue

        text = txt_path.read_text()
        if len(text.strip()) < 500:
            continue

        mid = txt_path.stem.rsplit("-", 1)[0]
        meeting_meta = {}
        meta_file = MEETINGS_DIR / mid / "meeting.json"
        if meta_file.exists():
            meeting_meta = json.loads(meta_file.read_text())

        doc_type = "agenda" if "agenda" in txt_path.stem else "minutes"
        print(f"Analyzing {meeting_meta.get('body', '?')} {meeting_meta.get('date', '?')} ({doc_type})...")

        analyzer = _get_advocacy_analyzer(args)
        analysis = analyzer(text, meeting_meta)
        if analysis:
            header = f"# ADVOCACY ANALYSIS: {meeting_meta.get('body', 'Meeting')} — {meeting_meta.get('date', 'Unknown')}\n"
            header += f"**Document:** {doc_type} | **Lens:** YIMBY + Strong Towns\n\n"
            analysis_path.write_text(header + analysis)
            print(f"  Saved analysis ({len(analysis)} chars)")
        else:
            print(f"  Failed to analyze")

        time.sleep(1)

    # Print latest analyses
    recent = sorted(ANALYSIS_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    if recent and not args.quiet:
        print(f"\n{'='*60}")
        print("LATEST ADVOCACY ANALYSIS")
        print(f"{'='*60}")
        print(recent[0].read_text())


ETRAKIT_BASE = "https://etrakit.cityofoceanside.com/etrakit3"
ETRAKIT_UA = "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"

PERMIT_HOUSING_TYPES = [
    "BLD ACCESSORY DWELLING", "BLD RESIDENTIAL", "BLD NEW RESIDENTIAL",
    "BLD DWELLING", "BLD SFR", "BLD MULTI", "BLD DUPLEX",
]


def fetch_etrakit_permit(session, permit_no):
    """Fetch a single permit by direct URL. Returns dict or None."""
    url = f"{ETRAKIT_BASE}/Search/permit.aspx?activityNo={permit_no}"
    resp = session.get(url, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    def label(suffix):
        el = soup.find("span", id=lambda x: x and x.endswith(suffix) and "Lbl" not in x)
        return el.get_text(strip=True) if el else ""

    ptype = label("lblPermitType")
    if not ptype:
        return None

    addr = ""
    for span in soup.find_all("span"):
        sid = span.get("id", "")
        if "Addr" in sid and "Lbl" not in sid and "City" not in sid:
            txt = span.get_text(strip=True)
            if txt and txt != "Address:":
                addr = txt
                break

    return {
        "permit_no": permit_no,
        "type": ptype,
        "subtype": label("lblPermitSubtype"),
        "description": label("lblPermitDesc"),
        "status": label("lblPermitStatus"),
        "applied": label("lblPermitAppliedDate"),
        "approved": label("lblPermitApprovedDate"),
        "issued": label("lblPermitIssuedDate"),
        "address": addr,
        "apn": label("lblPermitAPN"),
        "owner": label("lblPermitOwner"),
    }


def cmd_permits(args):
    """Fetch building permits from eTRAKiT by enumerating permit numbers."""
    ensure_dirs()
    PERMITS_DIR = DATA_DIR / "permits"
    PERMITS_DIR.mkdir(exist_ok=True)

    year = getattr(args, "year", datetime.now().year)
    yy = year % 100
    prefix = f"BLDG{yy:02d}-"
    max_seq = getattr(args, "max_seq", 5000)
    max_misses = getattr(args, "max_misses", 50)
    housing_only = getattr(args, "housing_only", False)

    out_file = PERMITS_DIR / f"etrakit-permits-{year}.jsonl"

    existing = []
    start_seq = 1
    if out_file.exists() and not getattr(args, "full", False):
        with open(out_file) as f:
            for line in f:
                line = line.strip()
                if line:
                    existing.append(json.loads(line))
        if existing:
            max_existing = max(
                int(p["permit_no"].split("-")[1]) for p in existing
            )
            start_seq = max_existing + 1
            print(f"Resuming from {prefix}{start_seq:04d} ({len(existing)} existing permits)", flush=True)

    session = requests.Session()
    session.headers["User-Agent"] = ETRAKIT_UA
    retry_adapter = requests.adapters.HTTPAdapter(
        max_retries=requests.packages.urllib3.util.retry.Retry(
            total=3, backoff_factor=2, status_forcelist=[429, 500, 502, 503]
        )
    )
    session.mount("https://", retry_adapter)

    new_results = []
    misses = 0

    if start_seq > max_seq:
        print(f"Already scanned up to {prefix}{start_seq - 1:04d}, checking for new permits...")
        max_seq = start_seq + 200

    delay = getattr(args, "delay", 1.0)
    print(f"Scanning eTRAKiT permits {prefix}{start_seq:04d} through {prefix}{max_seq:04d} (delay={delay}s)...", flush=True)

    consecutive_failures = 0
    for i in range(start_seq, max_seq + 1):
        permit_no = f"{prefix}{i:04d}"
        try:
            p = fetch_etrakit_permit(session, permit_no)
        except Exception as e:
            consecutive_failures += 1
            if consecutive_failures <= 3:
                print(f"  Error on {permit_no}: {e}", flush=True)
            elif consecutive_failures == 4:
                print(f"  (suppressing further errors...)", flush=True)
            if consecutive_failures > max_misses and i > start_seq + 50:
                print(f"  Stopping at {permit_no} after {consecutive_failures} consecutive failures", flush=True)
                break
            time.sleep(10)
            continue

        if p:
            new_results.append(p)
            misses = 0
            consecutive_failures = 0
        else:
            misses += 1
            consecutive_failures += 1
            if misses > max_misses and i > start_seq + 50:
                print(f"  Stopping at {permit_no} after {max_misses} consecutive misses", flush=True)
                break

        time.sleep(delay)

        if i % 100 == 0:
            print(f"  ...{i}: {len(new_results)} new permits found", flush=True)
            time.sleep(3)

    all_results = existing + new_results
    with open(out_file, "w") as f:
        for p in all_results:
            f.write(json.dumps(p) + "\n")

    from collections import Counter

    housing = [p for p in all_results if
               any(h in p["type"].upper() for h in ["DWELLING", "RESIDENTIAL", "SFR", "MULTI", "DUPLEX"])]

    print(f"\n{len(new_results)} new permits, {len(all_results)} total for {year}")
    print(f"{len(housing)} housing-related permits")

    type_counts = Counter(p["type"] for p in all_results)
    print("\nBy type:")
    for t, c in type_counts.most_common(15):
        print(f"  {t}: {c}")

    print(f"\nSaved to {out_file}")


def cmd_pra_watch(args):
    """Lightweight fetch + PRA-relevant keyword scan. Outputs JSONL."""
    ensure_dirs()
    ALERTS_DIR = DATA_DIR / "pra-alerts"
    ALERTS_DIR.mkdir(exist_ok=True)

    args.deep = True
    args.years = 1
    cmd_fetch(args)

    state = load_state()
    last_pra_scan = state.get("last_pra_scan", "1970-01-01T00:00:00")
    days_limit = getattr(args, "days", None)
    if days_limit:
        cutoff = datetime.now() - timedelta(days=days_limit)
    hits = []

    for txt_path in sorted(DOCS_DIR.glob("*.txt")):
        mid = txt_path.stem.split("-")[0]
        meeting_meta = state.get("meetings", {}).get(mid, {})

        if days_limit:
            meeting_date = meeting_meta.get("date", "")
            if meeting_date:
                try:
                    md = datetime.strptime(meeting_date.strip(), "%m/%d/%Y")
                except ValueError:
                    md = None
                if md and md < cutoff:
                    continue

        fetched = meeting_meta.get("fetched", "")
        if fetched <= last_pra_scan and not getattr(args, "force", False):
            continue

        text = txt_path.read_text()
        matches = keyword_scan(text, PRA_WATCH_KEYWORDS)
        if matches:
            hits.append({
                "file": txt_path.name,
                "meeting_id": mid,
                "body": meeting_meta.get("body", "unknown"),
                "date": meeting_meta.get("date", "unknown"),
                "keywords": list({kw for kw, _ in matches}),
                "contexts": {kw: ctx for kw, ctx in matches},
                "scanned": datetime.now().isoformat(),
            })

    state["last_pra_scan"] = datetime.now().isoformat()
    save_state(state)

    if hits:
        out_file = ALERTS_DIR / f"pra-scan-{datetime.now().strftime('%Y%m%d')}.jsonl"
        with open(out_file, "a") as f:
            for h in hits:
                f.write(json.dumps(h) + "\n")
        print(f"{len(hits)} PRA-relevant hits written to {out_file}")
        for h in hits:
            print(f"  {h['body']} {h['date']}: {', '.join(h['keywords'])}")
    else:
        print("No PRA-relevant hits in new documents.")


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

    p_analyze = sub.add_parser("analyze", help="YIMBY + Strong Towns advocacy analysis")
    p_analyze.add_argument("--force", action="store_true", help="Re-analyze already analyzed docs")
    p_analyze.add_argument("--quiet", action="store_true", help="Don't print latest analysis")
    p_analyze.add_argument("--summarizer", choices=["api", "local"], default="api", help="api=Claude API, local=claude -p (subscription)")

    p_pra = sub.add_parser("pra-watch", help="Fetch + scan for PRA/SB79/coastal keywords, output JSONL")
    p_pra.add_argument("--force", action="store_true", help="Re-scan all documents, not just new ones")
    p_pra.add_argument("--days", type=int, default=None, help="Only scan meetings from the last N days")

    p_permits = sub.add_parser("permits", help="Fetch building permits from eTRAKiT")
    p_permits.add_argument("--year", type=int, default=datetime.now().year, help="Year to scan (default: current)")
    p_permits.add_argument("--max-seq", type=int, default=5000, help="Max permit sequence number to try")
    p_permits.add_argument("--max-misses", type=int, default=50, help="Stop after N consecutive misses")
    p_permits.add_argument("--housing-only", action="store_true", help="Only output housing-related permits")
    p_permits.add_argument("--full", action="store_true", help="Full rescan (ignore existing data)")
    p_permits.add_argument("--delay", type=float, default=1.0, help="Seconds between requests (default: 1.0)")

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
        "analyze": cmd_analyze,
        "pra-watch": cmd_pra_watch,
        "permits": cmd_permits,
        "list": cmd_list,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
