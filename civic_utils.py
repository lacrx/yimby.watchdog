#!/usr/bin/env python3
"""Shared utilities for civic monitoring scrapers."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml


def _resolve_bin(name):
    """Find a working binary, preferring /usr/bin when linuxbrew is broken."""
    system_path = Path(f"/usr/bin/{name}")
    if system_path.exists():
        try:
            subprocess.run([str(system_path), "--version"],
                           capture_output=True, timeout=5)
            return str(system_path)
        except Exception:
            pass
    return shutil.which(name) or name


PDFTOTEXT_BIN = _resolve_bin("pdftotext")
CURL_BIN = _resolve_bin("curl")


ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

REPO_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("WATCHDOG_DATA_DIR", str(REPO_ROOT / "data")))
AGENCIES_FILE = REPO_ROOT / "agencies.yaml"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"

_agencies_cache = None


def load_agencies(enabled_only=True):
    """Load agency registry from agencies.yaml. Returns dict keyed by slug."""
    global _agencies_cache
    if _agencies_cache is None:
        with open(AGENCIES_FILE) as f:
            _agencies_cache = yaml.safe_load(f).get("agencies", {})
    if enabled_only:
        return {k: v for k, v in _agencies_cache.items() if v.get("enabled", True)}
    return dict(_agencies_cache)


def agency_data_dir(slug):
    """Return data directory for an agency: data/{slug}/"""
    return DATA_DIR / slug


def agency_docs_dir(slug):
    """Return documents directory for an agency: data/{slug}/documents/"""
    return DATA_DIR / slug / "documents"


def agency_meetings_dir(slug):
    """Return meetings directory for an agency: data/{slug}/meetings/"""
    return DATA_DIR / slug / "meetings"


def permit_jsonl_files(slug, kind="permits"):
    """Return sorted JSONL permit/project files for an agency, any platform."""
    permits_dir = agency_data_dir(slug) / "permits"
    if not permits_dir.exists():
        return []
    return sorted(permits_dir.glob(f"*-{kind}-*.jsonl"))


def all_docs_dirs(enabled_only=True):
    """Return list of existing document directories across all agencies."""
    dirs = []
    for slug in load_agencies(enabled_only=enabled_only):
        d = agency_docs_dir(slug)
        if d.exists():
            dirs.append(d)
    return dirs


def all_meetings_dirs(enabled_only=True):
    """Return list of existing meeting directories across all agencies."""
    dirs = []
    for slug in load_agencies(enabled_only=enabled_only):
        d = agency_meetings_dir(slug)
        if d.exists():
            dirs.append(d)
    return dirs


# eScribe sits behind a Cloudflare challenge that opens and closes platform-wide
# (successes arrive in clusters across agencies at the same time; once a run
# hits a 403, nothing after it in that run has ever succeeded). So: space
# requests out, and after a challenge stop asking every eScribe host for a
# cooldown shared across processes. A blocked run then costs one request
# instead of dozens, and escribe-sweep retries a few times a day to catch the
# open windows.
ESCRIBE_HOST = "escribemeetings.com"
ESCRIBE_MIN_INTERVAL = float(os.environ.get("ESCRIBE_MIN_INTERVAL", "6"))
ESCRIBE_COOLDOWN_MIN = float(os.environ.get("ESCRIBE_COOLDOWN_MIN", "90"))
_escribe_last_request = 0.0
_escribe_notices = set()
_cf_session = None  # lazy-init CloudflareSession for browser bypass
_cf_cookies = {}    # domain -> {name: value} from last successful bypass


def _escribe_state_file():
    return DATA_DIR / ".escribe-blocked-until"


def escribe_blocked_until():
    """Epoch seconds until which eScribe downloads are paused, or 0."""
    try:
        until = float(_escribe_state_file().read_text().strip())
    except (OSError, ValueError):
        return 0.0
    return until if until > time.time() else 0.0


def _escribe_mark_blocked():
    until = time.time() + ESCRIBE_COOLDOWN_MIN * 60
    try:
        _escribe_state_file().write_text(str(until))
    except OSError:
        pass
    return until


def _escribe_notice(key, msg):
    if key not in _escribe_notices:
        _escribe_notices.add(key)
        print(msg, flush=True)


def _escribe_pace():
    global _escribe_last_request
    wait = _escribe_last_request + ESCRIBE_MIN_INTERVAL - time.time()
    if wait > 0:
        time.sleep(wait)
    _escribe_last_request = time.time()


def _try_browser_bypass(url, dest_path):
    """Attempt PDF download via Playwright browser session.

    Solves the Cloudflare challenge once per domain, then reuses cookies.
    Returns True if PDF was saved, False otherwise.
    """
    global _cf_session, _cf_cookies
    try:
        from lib.cf_bypass import CloudflareSession
    except ImportError:
        return False

    if _cf_session is None:
        _cf_session = CloudflareSession(headless=True)
        import atexit
        atexit.register(lambda: _cf_session.close() if _cf_session else None)

    domain = _cf_session._domain(url)

    # Step 1: try with cached cookies via requests
    if domain in _cf_cookies:
        try:
            resp = requests.get(url, timeout=60, cookies=_cf_cookies[domain],
                                headers={"User-Agent": BROWSER_UA}, allow_redirects=True,
                                verify=False)
            if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                dest_path.write_bytes(resp.content)
                return True
        except Exception:
            pass

    # Step 2: solve challenge, get fresh cookies
    print(f"  cf_bypass: solving Cloudflare challenge for {domain}...", flush=True)
    cookies = _cf_session.get_request_cookies(url)
    if cookies:
        _cf_cookies[domain] = cookies
        # Clear the cooldown since browser bypass works
        try:
            _escribe_state_file().unlink(missing_ok=True)
        except OSError:
            pass
        try:
            resp = requests.get(url, timeout=60, cookies=cookies,
                                headers={"User-Agent": BROWSER_UA}, allow_redirects=True,
                                verify=False)
            if resp.status_code == 200 and b"%PDF" in resp.content[:10]:
                dest_path.write_bytes(resp.content)
                print(f"  cf_bypass: download succeeded with browser cookies", flush=True)
                return True
        except Exception:
            pass

    # Step 3: direct browser download as last resort
    print(f"  cf_bypass: trying direct browser download...", flush=True)
    try:
        data = _cf_session.download(url, dest_path)
        if data and b"%PDF" in data[:10]:
            print(f"  cf_bypass: direct browser download succeeded", flush=True)
            return True
    except Exception as e:
        print(f"  cf_bypass: browser download failed: {e}", flush=True)

    return False


def download_pdf(url, dest_path, headers=None, verify=True):
    """Download a PDF. Skips if already exists and non-empty.

    eScribe URLs are paced and attempt browser bypass on Cloudflare 403.
    """
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    is_escribe = ESCRIBE_HOST in url
    if is_escribe:
        until = escribe_blocked_until()
        if until:
            # Try browser bypass instead of waiting for cooldown
            if _try_browser_bypass(url, dest_path):
                return True
            _escribe_notice("cooldown", f"  eScribe challenged; browser bypass failed; "
                                        f"downloads paused until {datetime.fromtimestamp(until):%H:%M}")
            return False
        _escribe_pace()
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        resp = requests.get(url, timeout=60, headers=hdrs, allow_redirects=True,
                            verify=verify)
        resp.raise_for_status()
        if b"%PDF" in resp.content[:10]:
            dest_path.write_bytes(resp.content)
            return True
        return False
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 403 and is_escribe:
            # Try browser bypass before giving up
            if _try_browser_bypass(url, dest_path):
                return True
            until = _escribe_mark_blocked()
            print(f"  eScribe Cloudflare challenge (403); browser bypass failed; "
                  f"pausing until {datetime.fromtimestamp(until):%H:%M}", flush=True)
        else:
            print(f"  download failed: {e}")
        return False


def extract_text(pdf_path):
    """Extract text from PDF using pdftotext. Caches as .txt alongside."""
    txt_path = pdf_path.with_suffix(".txt")
    if txt_path.exists() and txt_path.stat().st_size > 0:
        return txt_path.read_text()
    try:
        result = subprocess.run(
            [PDFTOTEXT_BIN, "-layout", str(pdf_path), str(txt_path)],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode == 0 and txt_path.exists():
            return txt_path.read_text()
    except Exception as e:
        print(f"  pdftotext failed: {e}")
    return ""




def claude_api_call(client, max_retries=20, **kwargs):
    """Call Claude API with aggressive retry on rate limits.

    Retries with exponential backoff up to 10 minutes between attempts.
    Designed to survive token quota exhaustion — waits for refresh.
    """
    import anthropic

    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError as e:
            wait = min(60 * (2 ** attempt), 600)
            print(f"  Rate limited (attempt {attempt+1}/{max_retries}). Waiting {wait}s...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                wait = min(30 * (2 ** attempt), 300)
                print(f"  API overloaded (attempt {attempt+1}/{max_retries}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"API call failed after {max_retries} retries")


def claude_local_call(prompt, system=None, timeout=300):
    """Call Claude via claude -p (subscription, no API cost).

    Returns the response text, or None on failure.
    Mirrors claude_api_call() but for local/subscription use.
    """
    full_prompt = ""
    if system:
        full_prompt = f"SYSTEM CONTEXT:\n{system}\n\n---\n\n"
    full_prompt += prompt

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True, text=True, timeout=timeout,
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
        print(f"  claude -p timed out ({timeout}s)")
        return None


def parse_escribe_date(start_field):
    """Parse /Date(milliseconds)/ format from eScribe API."""
    m = re.search(r"/Date\((\d+)\)/", start_field)
    if m:
        return datetime.fromtimestamp(int(m.group(1)) / 1000)
    return None


def safe_filename(name, max_len=80):
    """Clean a string for use in filenames."""
    name = re.sub(r'[^\w\s\-.]', '', name)
    name = re.sub(r'\s+', '_', name).strip('_')
    return name[:max_len] or "document"


def make_meeting_id(prefix, body, dt):
    """Generate slug-based meeting ID: {prefix}-{body_slug}-{YYYYMMDD}."""
    body_slug = re.sub(r'[^a-z0-9]+', '-', body.lower()).strip('-')[:30]
    date_part = dt.strftime('%Y%m%d') if isinstance(dt, datetime) else dt
    return f"{prefix}-{body_slug}-{date_part}"


def make_meeting_id_hash(prefix, body, date_str):
    """Generate hash-based meeting ID for platforms with non-unique body+date."""
    return hashlib.md5(f"{prefix}-{body}-{date_str}".encode()).hexdigest()[:12]


def meeting_needs_recheck(meeting_id, docs_dir, meeting_date_str, max_age_days=60):
    """Return True if a meeting should be re-visited despite being in state.

    A meeting needs re-check when it has no extracted documents and its date
    is within max_age_days of today. This catches meetings first seen before
    their agenda was posted, and minutes posted weeks later.
    """
    docs_path = Path(docs_dir)
    if docs_path.exists():
        prefix = meeting_id + "-"
        for f in docs_path.iterdir():
            if f.name.startswith(prefix) and f.suffix == ".txt" and f.stat().st_size > 0:
                return False

    if not meeting_date_str:
        return False
    date_norm = normalize_date(meeting_date_str)
    if not date_norm:
        return False
    try:
        meeting_dt = datetime.strptime(date_norm, "%Y-%m-%d")
    except ValueError:
        return False
    age = (datetime.now() - meeting_dt).days
    return age <= max_age_days


def cmd_list_meetings(slug):
    """Generic list command — prints all meetings for an agency."""
    meetings_dir = agency_meetings_dir(slug)
    if not meetings_dir.exists():
        print("No meetings directory found.")
        return

    meetings = []
    for mdir in sorted(meetings_dir.iterdir()):
        mf = mdir / "meeting.json"
        if mf.exists():
            meetings.append(load_json(mf))

    meetings.sort(key=lambda m: m.get("date", ""), reverse=True)
    for m in meetings:
        print(f"  {m.get('date', '?'):12s}  {m.get('body', '?'):40s}  {m.get('id', '?')}")
    print(f"\n  {len(meetings)} meetings total")


def load_json(path):
    """Load JSON file, return empty dict on failure."""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    """Save data as formatted JSON."""
    Path(path).write_text(json.dumps(data, indent=2, default=str))


def normalize_date(date_str):
    """Normalize date string to YYYY-MM-DD."""
    if not date_str:
        return ""
    if re.match(r"^\d{4}-\d{2}-\d{2}$", str(date_str)):
        return str(date_str)
    if re.match(r"^\d{4}-\d{2}$", str(date_str)):
        return str(date_str) + "-01"
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", str(date_str))
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _match_meeting_id(filename, meeting_ids):
    """Find which meeting_id a document filename belongs to (prefix match)."""
    for mid in sorted(meeting_ids, key=len, reverse=True):
        if filename.startswith(mid + "-") or filename == mid + ".txt":
            return mid
    return None


def _date_from_meeting_id(mid):
    """Extract YYYY-MM-DD from meeting IDs like cb-body-20260623 or ccc-2022-02."""
    m = re.search(r"(\d{4})(\d{2})(\d{2})$", mid)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.search(r"(\d{4}-\d{2})$", mid)
    if m:
        return m.group(1) + "-01"
    return ""


_TITLE_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+(\d{1,2}),?\s+(\d{4})"
)
_TITLE_DATE_RE2 = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")


def _date_from_title(title):
    """Parse date from meeting title like 'City Council Meeting 6:00 p.m. - Jun 24, 2026'."""
    if not title:
        return ""
    m = _TITLE_DATE_RE.search(title)
    if m:
        months = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
                  "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}
        mon = months.get(m.group(1), 0)
        return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"
    m = _TITLE_DATE_RE2.search(title)
    if m:
        return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return ""


def _body_from_title(title):
    """Extract body name from title like 'Regular City Council Meeting 6:00 p.m. - Jun 24, 2026'."""
    if not title:
        return ""
    # Strip time and date suffix
    body = re.split(r"\s+\d{1,2}:\d{2}\s*[ap]\.?m\.?", title, flags=re.IGNORECASE)[0]
    # Strip prefixes like "Regular", "Special"
    body = re.sub(r"^(Regular|Special|Joint|Adjourned)\s+", "", body, flags=re.IGNORECASE)
    return body.strip(" -") or ""


def rebuild_doc_index(slug, state, docs_dir):
    """Build doc-index.json mapping document filenames to meeting dates.

    Called at end of each scraper's cmd_fetch(). Uses meeting_id prefix
    matching against on-disk .txt files.

    Date priority: extraction-derived date (from structured/*.json) is primary
    as meeting_date. Legistar/scraper date kept as filed_date for provenance.
    Falls back to filed_date when no extraction exists.
    """
    meetings = state.get("meetings", {})
    meeting_ids = list(meetings.keys())
    documents = {}

    docs_path = Path(docs_dir)
    if not docs_path.exists():
        return 0

    structured_dir = DATA_DIR / "structured"
    extraction_dates = _load_extraction_dates(structured_dir)

    for txt_file in docs_path.glob("*.txt"):
        mid = _match_meeting_id(txt_file.name, meeting_ids)
        if mid and mid in meetings:
            m = meetings[mid]
            filed_date = normalize_date(m.get("date", ""))
            if not filed_date:
                filed_date = _date_from_title(m.get("title", ""))
            if not filed_date:
                filed_date = _date_from_meeting_id(mid)
            if not filed_date:
                continue

            stem = txt_file.name[:-4] if txt_file.name.endswith(".txt") else txt_file.name
            ext_date = extraction_dates.get(stem, "")

            entry = {
                "meeting_date": ext_date if ext_date else filed_date,
                "filed_date": filed_date,
                "body": m.get("body", "") or _body_from_title(m.get("title", "")),
            }
            documents[txt_file.name] = entry

    index = {
        "_generated": datetime.now().isoformat(timespec="seconds"),
        "documents": documents,
    }

    index_path = agency_data_dir(slug) / "doc-index.json"
    save_json(index_path, index)
    return len(documents)


def _load_extraction_dates(structured_dir):
    """Load date fields from extraction JSONs. Returns {stem: YYYY-MM-DD}."""
    dates = {}
    if not structured_dir.exists():
        return dates
    for json_file in structured_dir.glob("*.json"):
        if json_file.name in ("extraction-state.json", "meetings-state.json", "document-dates.json"):
            continue
        try:
            record = json.loads(json_file.read_text())
            if isinstance(record, dict) and "date" in record:
                d = record["date"]
                if d and re.match(r"^\d{4}-\d{2}-\d{2}$", str(d)):
                    dates[json_file.stem] = str(d)
        except (json.JSONDecodeError, Exception):
            continue
    return dates


DISCOVERY_LOG = DATA_DIR / "pipeline" / "discovery.jsonl"


def log_discovery(agency, meetings_found=0, meetings_new=0, docs_new=0, docs_unchanged=0):
    """Log scraper discovery stats for cost projection."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "agency": agency,
        "meetings_found": meetings_found,
        "meetings_new": meetings_new,
        "docs_new": docs_new,
        "docs_unchanged": docs_unchanged,
    }
    DISCOVERY_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(DISCOVERY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")
