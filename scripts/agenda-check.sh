#!/usr/bin/env bash
# agenda-check.sh — Lightweight Legistar agenda scanner for CCR trigger use.
# Currently hardcoded for Oceanside — parameterization TODO.
# Only needs curl + python3 stdlib. No pip deps, no local data dir.
#
# Usage: ./scripts/agenda-check.sh [--days N] [--quiet]
#   --days N   Only show meetings within N days from today (default: 14)
#   --quiet    Exit silently if no keyword hits (for cron/trigger use)
#
# Exit codes: 0 = hits found, 1 = no hits, 2 = fetch error

set -euo pipefail

LEGISTAR="https://oceanside.legistar.com"
DAYS=14
QUIET=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --days) DAYS="$2"; shift 2 ;;
    --quiet) QUIET=true; shift ;;
    *) echo "Unknown arg: $1" >&2; exit 2 ;;
  esac
done

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

# Fetch calendar page
curl -sf "$LEGISTAR/Calendar.aspx" > "$TMPDIR/calendar.html" 2>/dev/null || {
  echo "ERROR: Failed to fetch Legistar calendar" >&2
  exit 2
}

# Single Python script does all the work: parse calendar, fetch details, scan keywords
python3 - "$LEGISTAR" "$DAYS" "$QUIET" "$TMPDIR" <<'PYEOF'
import json, re, sys, os, subprocess
from html.parser import HTMLParser
from datetime import datetime, timedelta

LEGISTAR = sys.argv[1]
DAYS = int(sys.argv[2])
QUIET = sys.argv[3] == "true"
TMPDIR = sys.argv[4]

KEYWORDS = [
    "SB 79", "SB79", "SB 330", "SB330", "SB 35", "SB35",
    "transit-oriented", "tier classification", "train count",
    "density bonus", "65915",
    "coastal", "LCP", "Coastal Commission", "Coastal Act",
    "Housing Crisis Act", "Housing Accountability",
    "inclusionary", "phasing ordinance",
    "NCTD", "OTC", "Oceanside Transit Center",
    "HCD", "CalHDF", "YIMBY", "RHNA",
    "Builder's Remedy",
    "housing element", "general plan", "specific plan",
    "zoning", "conditional use", "rezone",
    "Measure X", "budget", "CIP",
    "complete streets", "bike lane", "Vision Zero",
    "Coast Highway", "road diet",
]

KW_PATTERN = re.compile("|".join(re.escape(k) for k in KEYWORDS), re.IGNORECASE)

cutoff = datetime.now() - timedelta(days=DAYS)

class CalParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.cells = []
        self.current_text = []
        self.meetings = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if tag == "tr" and ("rgRow" in cls or "rgAltRow" in cls):
            self.in_row = True
            self.cells = []
            self.current_text = []
        if self.in_row:
            if tag == "td":
                if self.current_text:
                    self.cells.append(" ".join(self.current_text).strip())
                self.current_text = []
            if tag == "a":
                href = d.get("href", "").replace("&amp;", "&")
                m = re.search(r"MeetingDetail\.aspx\?ID=(\d+)(?:&GUID=([A-F0-9-]+))?", href)
                if m:
                    tag_str = f"__MEETID={m.group(1)}__"
                    if m.group(2):
                        tag_str += f"__GUID={m.group(2)}__"
                    self.current_text.append(tag_str)

    def handle_endtag(self, tag):
        if tag == "tr" and self.in_row:
            self.in_row = False
            if self.current_text:
                self.cells.append(" ".join(self.current_text).strip())
            if len(self.cells) >= 2:
                self.meetings.append(self.cells)

    def handle_data(self, data):
        if self.in_row:
            d = data.strip()
            if d:
                self.current_text.append(d)

class DetailParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_row = False
        self.items = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        cls = d.get("class", "")
        if tag == "tr" and ("rgRow" in cls or "rgAltRow" in cls):
            self.in_row = True
            self.current = []

    def handle_endtag(self, tag):
        if tag == "tr" and self.in_row:
            self.in_row = False
            text = re.sub(r"\s+", " ", " ".join(self.current).strip())
            if text:
                self.items.append(text)

    def handle_data(self, data):
        if self.in_row:
            d = data.strip()
            if d:
                self.current.append(d)

BODIES_OF_INTEREST = {"City Council", "Planning Commission", "Housing Commission"}

with open(f"{TMPDIR}/calendar.html") as f:
    cal = CalParser()
    cal.feed(f.read())

hits = []
checked = 0

for cells in cal.meetings:
    body = cells[0].replace("\xa0", " ").strip()
    if body not in BODIES_OF_INTEREST:
        continue

    date_match = None
    meet_id = None
    guid = None
    for cell in cells:
        dm = re.search(r"(\d{1,2}/\d{1,2}/\d{4})", cell)
        if dm:
            date_match = dm.group(1)
        im = re.search(r"__MEETID=(\d+)__", cell)
        if im:
            meet_id = im.group(1)
        gm = re.search(r"__GUID=([A-F0-9-]+)__", cell)
        if gm:
            guid = gm.group(1)

    if not date_match or not meet_id:
        continue

    try:
        meet_date = datetime.strptime(date_match, "%m/%d/%Y")
    except ValueError:
        continue
    if meet_date < cutoff:
        continue

    detail_url = f"{LEGISTAR}/MeetingDetail.aspx?ID={meet_id}"
    if guid:
        detail_url += f"&GUID={guid}"

    detail_file = f"{TMPDIR}/detail-{meet_id}.html"
    rc = subprocess.run(
        ["curl", "-sf", detail_url, "-o", detail_file],
        capture_output=True
    )
    if rc.returncode != 0:
        continue
    checked += 1

    with open(detail_file) as f:
        dp = DetailParser()
        dp.feed(f.read())

    all_text = "\n".join(dp.items)
    matched = sorted(set(m.group() for m in KW_PATTERN.finditer(all_text)), key=str.lower)
    if matched:
        hits.append({
            "body": body,
            "date": date_match,
            "meeting_id": meet_id,
            "keywords": matched,
            "items": dp.items,
            "detail_url": detail_url,
        })

if not hits:
    if not QUIET:
        print(f"No keyword hits in {checked} meetings checked (last {DAYS} days).")
    sys.exit(1)

print(f"AGENDA HITS ({checked} meetings checked, last {DAYS} days):")
print("---")
for h in hits:
    print(f"{h['body']} {h['date']} (ID={h['meeting_id']}): {', '.join(h['keywords'])}")
print("---")
print()
print("JSON:")
print(json.dumps(hits, indent=2))
sys.exit(0)
PYEOF
