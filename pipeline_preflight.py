#!/usr/bin/env python3
"""
Pipeline preflight — lightweight endpoint health checks before nightly run.

Probes each enabled agency's data source with a minimal request. Catches
broken endpoints, changed APIs, auth walls, and WAF blocks before the
pipeline wastes time on full fetches that'll fail.

Tracks consecutive failures per agency. After QUARANTINE_THRESHOLD consecutive
failures, the agency is auto-quarantined: preflight skips it and logs a
warning instead of probing. Manual intervention required to un-quarantine.

Usage:
    python pipeline_preflight.py              # check all enabled agencies
    python pipeline_preflight.py --verbose    # show response details
    python pipeline_preflight.py --agency X   # check one agency only
    python pipeline_preflight.py --status     # show health history
    python pipeline_preflight.py --unquarantine X  # reset failure count

Called automatically as PHASE 0 of civic-pipeline. Exit code:
    0 = all healthy (or quarantined)
    1 = new failures detected (pipeline continues but logs warnings)
    2 = ALL agencies failing (pipeline should abort)

Cost: ~0. Just HTTP GETs, no API keys, no LLM calls.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))

import requests
from civic_utils import load_agencies, agency_data_dir

HEALTH_FILE = REPO_ROOT / "data" / "preflight-health.json"
QUARANTINE_THRESHOLD = 3
PROBE_TIMEOUT = 15
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) yimby-watchdog/1.0"


def load_health():
    if HEALTH_FILE.exists():
        return json.loads(HEALTH_FILE.read_text())
    return {}


def save_health(health):
    HEALTH_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEALTH_FILE.write_text(json.dumps(health, indent=2, default=str))


# ── Platform-specific probes ──────────────────────────────────────────
# Each returns (ok: bool, detail: str)
# Probe = lightest possible request that proves the data source works.

def probe_legistar_html(cfg):
    url = f"{cfg['base_url']}/Calendar.aspx"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"User-Agent": USER_AGENT})
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    if "rgRow" not in resp.text and "rgAltRow" not in resp.text:
        if "Calendar" not in resp.text:
            return False, "no calendar content in response"
    return True, f"OK ({len(resp.text)} bytes)"


def probe_legistar_odata(cfg):
    api_base = cfg.get("api_base", "")
    if not api_base:
        return False, "no api_base configured"
    url = f"{api_base}/Bodies?$top=1"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"Accept": "application/json"})
    if resp.status_code == 403:
        return False, "HTTP 403 Forbidden (API key required?)"
    if resp.status_code == 500:
        return False, "HTTP 500 (API may be down)"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        data = resp.json()
        return True, f"OK ({len(data)} items)"
    except Exception:
        return False, "response is not JSON"


def probe_escribe(cfg):
    base = cfg.get("base_url", "")
    url = f"{base}/MeetingsCalendarView.aspx/PastMeetings"
    bodies = cfg.get("bodies", [])
    body_name = bodies[0] if bodies else "Board of Directors"
    payload = {"type": body_name, "pageNumber": 1}
    resp = requests.post(url, json=payload, timeout=PROBE_TIMEOUT, verify=False,
                         headers={"Content-Type": "application/json",
                                  "User-Agent": USER_AGENT})
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        data = resp.json()
        groups = data.get("d", {}).get("MeetingGroup", [])
        return True, f"OK ({len(groups)} meeting groups)"
    except Exception:
        return False, "response is not JSON"


def probe_granicus(cfg):
    base = cfg.get("base_url", "")
    view_id = cfg.get("view_id", 1)
    url = f"{base}/ViewPublisherRSS.php?view_id={view_id}&mode=agendas"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"User-Agent": USER_AGENT})
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    if "<item>" not in resp.text:
        if "<rss" not in resp.text:
            return False, "not valid RSS"
        return False, "RSS feed is empty (no items)"
    item_count = resp.text.count("<item>")
    return True, f"OK ({item_count} items in feed)"


def probe_civicplus(cfg):
    base = cfg.get("base_url", "").rstrip("/")
    end = datetime.now()
    start_str = end.strftime("%m/%d/%Y")
    url = f"{base}/AgendaCenter/Search?term=&CIDs=all&startDate=01/01/{end.year}&endDate={start_str}"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"User-Agent": USER_AGENT})
    if resp.status_code == 403:
        return False, "HTTP 403 (WAF block?)"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    if "catAgendaRow" not in resp.text:
        if "AgendaCenter" not in resp.text:
            return False, "page doesn't look like AgendaCenter"
        return False, "no agenda rows found (empty or restructured)"
    row_count = resp.text.count("catAgendaRow")
    return True, f"OK ({row_count} agenda rows)"


def probe_civicclerk(cfg):
    api_base = cfg.get("api_base", "")
    if not api_base:
        return False, "no api_base configured"
    url = f"{api_base}/EventCategories"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"Accept": "application/json",
                                 "User-Agent": USER_AGENT})
    if resp.status_code == 404:
        return False, "HTTP 404 (API tenant not provisioned)"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        resp.json()
        return True, "OK"
    except Exception:
        return False, "response is not JSON"


def probe_coastal_api(cfg):
    api_base = cfg.get("api_base", "")
    if not api_base:
        return False, "no api_base configured"
    now = datetime.now()
    url = f"{api_base}/{now.year}/{now.month}"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"Accept": "application/json",
                                 "User-Agent": USER_AGENT})
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    try:
        data = resp.json()
        return True, f"OK ({len(data)} items)"
    except Exception:
        return False, "response is not JSON"


def probe_carlsbad_cms(cfg):
    import subprocess
    base = cfg.get("base_url", "").rstrip("/")
    url = f"{base}/city-hall/meetings-agendas"
    try:
        result = subprocess.run([
            "curl", "-s", "--compressed", "--max-time", str(PROBE_TIMEOUT),
            "-H", "User-Agent: Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Sec-Fetch-Dest: document",
            "-H", "Sec-Fetch-Mode: navigate",
            "-H", "Sec-Fetch-Site: none",
            "-w", "\n%{http_code}",
            url,
        ], capture_output=True, text=True, timeout=PROBE_TIMEOUT + 5)
        lines = result.stdout.strip().split("\n")
        code = lines[-1] if lines else "0"
        body = "\n".join(lines[:-1])
        if code != "200":
            return False, f"HTTP {code} (Akamai WAF block — curl TLS fingerprint may need updating)"
        if "Access Denied" in body[:500]:
            return False, "WAF soft-block (Access Denied in response body)"
        if "-folder-" not in body:
            return False, "no folder links found (page structure changed?)"
        folder_count = body.count("-folder-")
        return True, f"OK ({folder_count} folder links)"
    except subprocess.TimeoutExpired:
        return False, f"timeout after {PROBE_TIMEOUT}s"
    except FileNotFoundError:
        return False, "curl not found"


def probe_solana_drupal(cfg):
    base = cfg.get("base_url", "").rstrip("/")
    url = f"{base}/en/city-council-meetings"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"User-Agent": USER_AGENT})
    if resp.status_code == 403:
        return False, "HTTP 403 (blocked)"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    if "views-row" not in resp.text:
        return False, "no views-row content (Drupal structure changed?)"
    row_count = resp.text.count("views-row")
    # Also probe eScribe
    escribe_base = cfg.get("escribe_base", "")
    escribe_ok = ""
    if escribe_base:
        try:
            er = requests.post(
                f"{escribe_base}/MeetingsCalendarView.aspx/PastMeetings",
                json={"type": "Council Meetings REGULAR", "pageNumber": 1},
                timeout=PROBE_TIMEOUT, verify=False,
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            )
            if er.status_code == 200:
                escribe_ok = " + eScribe OK"
        except Exception:
            pass
    return True, f"OK ({row_count} rows{escribe_ok})"


def probe_custom_html(cfg):
    url = cfg.get("base_url", "")
    if not url:
        return False, "no base_url configured"
    resp = requests.get(url, timeout=PROBE_TIMEOUT,
                        headers={"User-Agent": USER_AGENT})
    if resp.status_code == 403:
        return False, "HTTP 403 (blocked)"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}"
    if len(resp.text) < 500:
        return False, f"suspiciously small response ({len(resp.text)} bytes)"
    return True, f"OK ({len(resp.text)} bytes)"


PROBES = {
    "legistar_html": probe_legistar_html,
    "legistar_odata": probe_legistar_odata,
    "escribe": probe_escribe,
    "granicus": probe_granicus,
    "civicplus": probe_civicplus,
    "civicclerk": probe_civicclerk,
    "coastal_api": probe_coastal_api,
    "carlsbad_cms": probe_carlsbad_cms,
    "solana_drupal": probe_solana_drupal,
    "custom_html": probe_custom_html,
}


# ── Main logic ────────────────────────────────────────────────────────

def check_agency(slug, cfg, health, verbose=False):
    """Probe one agency. Returns (ok, detail) and updates health record."""
    platform = cfg.get("platform", "unknown")
    probe_fn = PROBES.get(platform)

    if slug not in health:
        health[slug] = {
            "consecutive_failures": 0,
            "last_check": None,
            "last_ok": None,
            "last_error": None,
            "quarantined": False,
        }

    rec = health[slug]

    if rec.get("quarantined"):
        return None, f"QUARANTINED (failed {rec['consecutive_failures']}x — run --unquarantine {slug} to reset)"

    if not probe_fn:
        return None, f"no probe for platform '{platform}'"

    try:
        ok, detail = probe_fn(cfg)
    except requests.Timeout:
        ok, detail = False, f"timeout after {PROBE_TIMEOUT}s"
    except requests.ConnectionError as e:
        ok, detail = False, f"connection error: {e}"
    except Exception as e:
        ok, detail = False, f"probe error: {e}"

    rec["last_check"] = datetime.now().isoformat()

    if ok:
        rec["consecutive_failures"] = 0
        rec["last_ok"] = datetime.now().isoformat()
        rec["last_error"] = None
    else:
        rec["consecutive_failures"] += 1
        rec["last_error"] = detail
        if rec["consecutive_failures"] >= QUARANTINE_THRESHOLD:
            rec["quarantined"] = True

    return ok, detail


def cmd_check(args):
    agencies = load_agencies(enabled_only=True)
    if args.agency:
        all_agencies = load_agencies(enabled_only=False)
        if args.agency not in all_agencies:
            print(f"Unknown agency: {args.agency}")
            sys.exit(1)
        agencies = {args.agency: all_agencies[args.agency]}

    health = load_health()
    results = {}
    any_new_fail = False
    all_fail = True

    print(f"[preflight] Checking {len(agencies)} agencies...")
    for slug, cfg in agencies.items():
        ok, detail = check_agency(slug, cfg, health, verbose=args.verbose)
        results[slug] = (ok, detail)

        if ok is None:
            icon = "⊘"
        elif ok:
            icon = "✓"
            all_fail = False
        else:
            icon = "✗"
            was_quarantined = health[slug].get("quarantined", False)
            fails = health[slug]["consecutive_failures"]
            if was_quarantined:
                detail += f" → QUARANTINED after {fails} failures"
            any_new_fail = True

        name = cfg.get("name", slug)
        platform = cfg.get("platform", "?")
        print(f"  {icon} {slug:15s} ({platform:15s})  {detail}")

    save_health(health)

    # Summary
    ok_count = sum(1 for ok, _ in results.values() if ok is True)
    fail_count = sum(1 for ok, _ in results.values() if ok is False)
    skip_count = sum(1 for ok, _ in results.values() if ok is None)

    print(f"\n[preflight] {ok_count} healthy, {fail_count} failing, {skip_count} skipped/quarantined")

    if all_fail and ok_count == 0 and fail_count > 0:
        print("[preflight] ALL agencies failing — possible network issue. Pipeline should abort.")
        sys.exit(2)
    elif any_new_fail:
        sys.exit(1)
    else:
        sys.exit(0)


def cmd_status(args):
    health = load_health()
    if not health:
        print("No preflight history yet.")
        return

    agencies = load_agencies(enabled_only=False)

    for slug, rec in sorted(health.items()):
        name = agencies.get(slug, {}).get("name", slug)
        fails = rec.get("consecutive_failures", 0)
        quarantined = rec.get("quarantined", False)
        last_ok = rec.get("last_ok", "never")
        last_err = rec.get("last_error", "none")
        last_check = rec.get("last_check", "never")

        if quarantined:
            status = f"QUARANTINED ({fails} consecutive failures)"
        elif fails > 0:
            status = f"FAILING ({fails}x): {last_err}"
        else:
            status = "healthy"

        print(f"  {slug:15s}  {status}")
        print(f"                   last check: {last_check}")
        print(f"                   last OK:    {last_ok}")
        print()


def cmd_unquarantine(args):
    health = load_health()
    slug = args.unquarantine
    if slug not in health:
        print(f"No health record for {slug}")
        sys.exit(1)

    rec = health[slug]
    if not rec.get("quarantined"):
        print(f"{slug} is not quarantined (failures: {rec.get('consecutive_failures', 0)})")
        return

    rec["quarantined"] = False
    rec["consecutive_failures"] = 0
    save_health(health)
    print(f"Un-quarantined {slug}. Will probe again on next preflight run.")


def main():
    parser = argparse.ArgumentParser(description="Pipeline preflight health checks")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--agency", help="Check one agency only")
    parser.add_argument("--status", action="store_true", help="Show health history")
    parser.add_argument("--unquarantine", metavar="SLUG", help="Reset quarantine for an agency")

    args = parser.parse_args()

    if args.unquarantine:
        cmd_unquarantine(args)
    elif args.status:
        cmd_status(args)
    else:
        cmd_check(args)


if __name__ == "__main__":
    main()
