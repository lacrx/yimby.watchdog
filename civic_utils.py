#!/usr/bin/env python3
"""Shared utilities for civic monitoring scrapers."""

import json
import os
import subprocess
import time
from pathlib import Path

import requests


ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) civics-monitor/1.0"


def download_pdf(url, dest_path, headers=None):
    """Download a PDF. Skips if already exists and non-empty."""
    if dest_path.exists() and dest_path.stat().st_size > 0:
        return True
    hdrs = {"User-Agent": USER_AGENT}
    if headers:
        hdrs.update(headers)
    try:
        resp = requests.get(url, timeout=60, headers=hdrs, allow_redirects=True)
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


SUMMARIZE_PROMPT = """Summarize this local government document from {agency}.

Meeting: {body} — {date}

Provide:
1. A 2-3 sentence overview
2. Key decisions or action items (bulleted)
3. Any items related to: housing, zoning, development, permits, budget, infrastructure, transit, environmental
4. Notable public comments or controversies

Be concise. If the document is just procedural (roll call, adjournment), say so in one line.

CRITICAL: Only name individuals who appear BY NAME in the source text below. NEVER invent, guess, or fill in names. Hallucinating names is worse than leaving a gap.

Document text:
{text}"""


def summarize_text(text, meeting_info, model="claude-sonnet-4-6"):
    """Summarize meeting document text using Claude API."""
    try:
        import anthropic
    except ImportError:
        return None

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    client = anthropic.Anthropic(api_key=api_key)

    prompt = SUMMARIZE_PROMPT.format(
        agency=meeting_info.get("agency", "a local agency"),
        body=meeting_info.get("body", "Unknown"),
        date=meeting_info.get("date", "Unknown date"),
        text=text[:80000],
    )

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


def summarize_text_local(text, meeting_info):
    """Summarize meeting document text using claude -p (subscription, no API cost)."""
    prompt = SUMMARIZE_PROMPT.format(
        agency=meeting_info.get("agency", "a local agency"),
        body=meeting_info.get("body", "Unknown"),
        date=meeting_info.get("date", "Unknown date"),
        text=text[:80000],
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "text"],
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            print(f"  claude -p error: {result.stderr[:200]}")
        return None
    except FileNotFoundError:
        print("  claude CLI not found. Install Claude Code: https://claude.ai/code")
        return None
    except subprocess.TimeoutExpired:
        print("  claude -p timed out (300s)")
        return None


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

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True, text=True, timeout=timeout,
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


def load_json(path):
    """Load JSON file, return empty dict on failure."""
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    """Save data as formatted JSON."""
    Path(path).write_text(json.dumps(data, indent=2, default=str))
