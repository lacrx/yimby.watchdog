#!/usr/bin/env python3
"""Shared utilities for civic monitoring scrapers."""

import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

import requests
import yaml


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

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"

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


def download_pdf(url, dest_path, headers=None, verify=True):
    """Download a PDF. Skips if already exists and non-empty."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
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
        print(f"  download failed: {e}")
        return False


def extract_text(pdf_path):
    """Extract text from PDF using pdftotext. Caches as .txt alongside."""
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
