# YIMBY Watchdog

An AI-augmented watchdog stack for monitoring municipal government. Built to give one person the legal knowledge, attention span, and time that holding local government accountable normally requires a full newsroom or advocacy organization to sustain.

Currently monitoring **Oceanside, CA** and the regional agencies that govern it — but designed to be replicated for any California city with a Legistar portal.

Modeled after [Listen Public](https://www.listenpublic.com/)'s approach to civic monitoring: ingest everything, structure it, make it queryable.

## What This Does

City councils make hundreds of decisions a year across dozens of meetings. Staff reports run 50-200 pages. Agendas drop 72 hours before votes. Public comment is 3 minutes. No single person can read everything, attend everything, and connect the dots across years of votes to see who's actually doing what.

This stack does:

1. **Scrapes** every meeting agenda, staff report, and attachment from Legistar portals (Oceanside, NCTD, SD County) and agency APIs (SANDAG Granicus, Coastal Commission)
2. **Fetches** building permits from eTRAKiT (housing-relevant types: SFD/duplex, multifamily, ADU)
3. **Transcribes** meeting audio/video (YouTube, KOCT) using Whisper
4. **Extracts** structured records from every document — votes, motions, fiscal impacts, housing items — into machine-readable JSONL
5. **Rolls up** all data sources (meetings + permits + intel) into independent monthly digests with content-hash change detection
6. **Monitors** RSS feeds and web pages from housing enforcement orgs, state agencies, transit agencies, and local journalism for items relevant to the city
7. **Generates** per-council-member advocacy profiles graded on housing votes, yearly executive summaries, legal exposure analyses, and issue-specific deep dives
8. **Archives** raw sources to S3, keeping only working data (text + structured records) local

The output is structured data and analysis that turns years of meeting records into accountability tools — the kind of institutional memory that usually only exists in a reporter's notebook or a lobbyist's CRM.

## Data Flow

```
scrapers/
  Legistar API ────────────────────────────┐
    oceanside.py (Oceanside)               │
    nctd.py (NCTD)                         │
    sdcounty.py (SD County BOS)            │
  SANDAG Granicus API                      │
    sandag.py                              │
  Coastal Commission State API             │
    coastal.py                             │
                                           ▼
  YouTube/KOCT ──► discover_videos.py      data/documents/*.txt
  eTRAKiT ──► oceanside.py permits ──► data/permits/*.jsonl
  RSS/Web ──► intel_feed.py ──► data/intel/*.json

transforms/
         data/documents/*.txt  (pdftotext output)
                    │
                    ▼
         extract_structured.py            ◄── claude -p per document
                    │
                    ▼
         data/structured/*.json           ◄── one JSON per source doc
                    │
                    ▼
         meeting_merge.py                 ◄── majority-vote date/agency
                    │
                    ▼
         data/structured/meetings/*.json  ◄── one JSON per meeting
                    │
          ┌─────────┼──────────────────────────────┐
          ▼         ▼                              ▼
    meetings-   all-records.jsonl         monthly_rollup.py
    combined.jsonl                     ◄── meetings + permits + intel
                                      ◄── content-hash change detection
                                           │
                                           ▼
                                  data/structured/monthly/*.json

analysis/
                                           │
                                   ┌───────┴──────────┐
                                   ▼                  ▼
                            executive_          leadership_
                            summaries.py        profiles.py
                                   │                  │
                                   ▼                  ▼
                             yearly executive   per-member profiles
                             summaries (.md)    with grades (.md)

S3 (civics-monitor bucket)
  ├── archive/    PDFs + audio (durable archive)
  ├── raw/        extracted text + transcripts
  ├── structured/ records + meetings + monthly digests
  ├── metadata/   meeting metadata per agency
  └── operational/ permits + intel
```

## Nightly Pipeline

Runs via cron at 1:00 AM. Three phases, prioritized so new meetings always get processed first:

```
1:00 AM  Phase 1 — GATHER
         ├── Fetch meetings (Oceanside, NCTD, SANDAG, SD County, CCC)
         ├── Fetch building permits from eTRAKiT (current year, incremental)
         ├── Check intel feeds (RSS, web pages)
         ├── Discover new meeting videos
         ├── Transcribe audio (if enabled)
         └── Sync raw sources to S3

         Phase 2 — MERGE & ROLL UP
         ├── Merge document records into per-meeting records
         └── Rebuild stale monthly digests (content-hash detection)

         Phase 3 — EXTRACT (remaining time until 4 AM)
         ├── Structured extraction via claude -p (subscription, not API)
         ├── Newest documents first (recent meetings get priority)
         └── Hard cutoff at 4:00 AM

4:00 AM  Sync structured data to S3. Pipeline doctor runs diagnostics.

6:00 PM  Evening catch (Tue-Fri) — refetch + extract with no time limit.
         Matches when cities typically post agendas for upcoming meetings.
```

Extraction is resumable — each document produces its own output file. The pipeline picks up where it left off the next run.

### Cron Setup

```bash
# Nightly: fetch current year + extract until 4 AM
0 1 * * * cd ~/repos/yimby.watchdog && ./civic-pipeline local --deep --years 1 --extract-until 4 >> data/pipeline-cron.log 2>&1

# Evening catch: Tue-Fri, no extraction time limit
0 18 * * 2-5 cd ~/repos/yimby.watchdog && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1
```

### Processing Modes

| Mode | Summarizer | Transcriber | Cost | Use Case |
|------|-----------|-------------|------|----------|
| **local** | `claude -p` (subscription) | faster-whisper | $0 | Nightly cron (default) |
| **hybrid** | `claude -p` (subscription) | Whisper API | ~$0.90/meeting | Better transcription quality |
| **full** | Claude API (Opus) | Whisper API | ~$44/full run | Bulk backfill |

### Pipeline Options

| Flag | Effect |
|------|--------|
| `--deep` | Download staff reports (not just agendas/minutes) |
| `--force` | Re-process already summarized/extracted documents |
| `--years N` | How many years back to fetch (default: 1) |
| `--transcribe` | Also transcribe meeting videos |
| `--extract-until H` | Stop extraction at hour H (0-23) |

### Initial Backfill

For the first run, use `--years 5` or higher to pull historical meetings. This only needs to happen once — after that, `--years 1` keeps up with new meetings. Historical data is archived to S3 and doesn't need to be re-fetched.

```bash
# One-time backfill (will take several nights of extraction)
./civic-pipeline local --deep --years 7

# After backfill completes, switch cron to --years 1
```

## Structured Data Layer

Every raw source (PDF text, transcript) becomes a structured JSON record with typed fields:

- `votes[]` — item, result, yes/no/abstain names
- `housing_items[]` — type, units, outcome, state_law_flags
- `fiscal_items[]` — description, amount, type
- `legal_flags[]` — potential violations, enforcement actions
- `council_positions[]` — member, stance, evidence
- `advocacy_score` — green/yellow/red/neutral

Per-document records merge into per-meeting records (majority-vote date/agency selection), then roll up into monthly digests alongside permit data and intel feed items. Each month is independent — rebuilds only when its content hash changes.

```bash
# Check extraction progress
python transforms/extract_structured.py --stats

# Query with jq
jq 'select(.advocacy_score == "red")' data/structured/all-records.jsonl
jq 'select(.council_positions[].member == "Joyce")' data/structured/all-records.jsonl

# Check which months need rebuild
python transforms/monthly_rollup.py --check
```

## What's Monitored

**Agencies (5):**
- City of Oceanside (Legistar) — 28 boards/commissions, 46 unique bodies
- NCTD (Legistar) — Board of Directors, committees
- SANDAG (Granicus) — Board, Regional Planning, Transportation, committees
- San Diego County Board of Supervisors (Legistar OData) — BOS, Land Use
- California Coastal Commission (state API) — permits and LCPs affecting Oceanside

**Building Permits:** eTRAKiT scraper pulls SFD/duplex, multifamily, and ADU permits by year (2020-present). Rolled into monthly digests with unit estimates.

**External Sources (17+ feeds):** State agencies (HCD, Attorney General, CCC), enforcement orgs (CalHDF, YIMBY Law, Californians for Homeownership), regional transit (SANDAG, NCTD), legal analysis (Holland & Knight), journalism (Voice of San Diego, CalMatters, Circulate SD).

**Council Members Profiled (11):** Each profile includes a housing advocacy letter grade, net score, every recorded vote on housing-related items, factional analysis, and an assessment of the gap between stated goals and actual votes.

## Replicating This

The stack works for any California city with a Legistar portal (most cities use one). The analysis layer — extraction, merge, rollups, summaries — is jurisdiction-agnostic.

### What to change

1. **Add a scraper for your city.** Copy `scrapers/oceanside.py` and change the Legistar base URL and body names. The scraper pattern (fetch calendar → parse meetings → download PDFs → extract text) works for any Legistar instance.

2. **Add your regional agencies.** Copy `scrapers/sandag.py` (Granicus API) or `scrapers/sdcounty.py` (Legistar OData) depending on what your regional agencies use. Wire them into `civic-pipeline`.

3. **Update the intel feed.** `scrapers/intel_feed.py` monitors feeds relevant to Oceanside/San Diego. Replace local sources with your city's regional journalism and advocacy orgs. Keep statewide sources (HCD, CalHDF, YIMBY Law, AG, CalMatters).

4. **Point permits at your city.** `scrapers/oceanside.py permits` scrapes eTRAKiT. If your city uses a different permit portal, write a scraper that outputs the same JSONL format (`permit_no`, `type`, `status`, `applied`, `address`, `description`).

5. **Set up S3 (optional).** Set `S3_BUCKET` in `.env`. The pipeline syncs automatically — raw sources, structured records, monthly digests. Without it, everything stays local.

### What you keep as-is

- `transforms/extract_structured.py` — structured extraction prompt is jurisdiction-agnostic
- `transforms/meeting_merge.py` — merges by meeting_id, no city-specific logic
- `transforms/monthly_rollup.py` — rolls up meetings + permits + intel by month
- `analysis/executive_summaries.py` — reads monthly digests, no city-specific logic
- `analysis/leadership_profiles.py` — reads per-record data, no city-specific logic
- Policy knowledge base — CA housing law, fiscal analysis, crash data methodology (separate repo: `lacrx/policy-knowledge-docs`)

### What you need

- Python 3.10+
- Claude Code CLI with active subscription (`claude -p` for structured extraction)
- `pdftotext` (poppler-utils)
- Python packages: `requests`, `feedparser`, `yt-dlp`
- Optional: AWS CLI + S3 bucket (for archival backup)
- Optional: `faster-whisper` (for zero-cost local transcription)
- Optional: OpenAI API key (for Whisper API transcription in hybrid/full modes)

### First run

```bash
# 1. Clone and set up
git clone https://github.com/lacrx/yimby.watchdog.git
cd yimby.watchdog
python -m venv .venv && source .venv/bin/activate
pip install requests feedparser yt-dlp

# 2. Configure
cp .env.example .env
# Edit .env — set S3_BUCKET if using AWS, otherwise leave empty

# 3. Fetch meetings (current year, with staff reports)
python scrapers/oceanside.py fetch --deep

# 4. Extract structured records (runs claude -p per document)
python transforms/extract_structured.py

# 5. Merge into per-meeting records
python transforms/meeting_merge.py

# 6. Build monthly digests (meetings + permits + intel)
python transforms/monthly_rollup.py

# 7. Generate executive summaries
python analysis/executive_summaries.py

# 8. Generate leadership profiles
python analysis/leadership_profiles.py

# 9. Set up nightly cron
crontab -e
# Add the two cron lines from the "Cron Setup" section above
```

The initial extraction takes time — `extract_structured.py` calls `claude -p` per document. For a city with 2,000+ documents, expect several nights of incremental processing. The pipeline is resumable; it picks up where it left off.

## Directory Structure

```
yimby.watchdog/
├── civic-pipeline              # Bash orchestrator (cron entry point)
├── civic_utils.py              # Shared utilities (PDF download, text extraction, LLM calls)
├── pipeline_doctor.py          # Self-healing pipeline diagnostics
├── backfill-permits.sh         # One-time historical permit backfill
├── scrapers/                   # Phase 1: agency fetchers
│   ├── oceanside.py            # Oceanside Legistar scraper + eTRAKiT permits
│   ├── nctd.py                 # NCTD Legistar scraper
│   ├── sandag.py               # SANDAG Granicus scraper
│   ├── sdcounty.py             # SD County BOS Legistar OData scraper
│   ├── coastal.py              # CA Coastal Commission API scraper
│   ├── intel_feed.py           # External source monitor (17+ feeds)
│   └── discover_videos.py      # YouTube video matching
├── transforms/                 # Phase 2-3: merge, rollup, extract, transcribe
│   ├── extract_structured.py   # LLM → structured JSONL (claude -p)
│   ├── meeting_merge.py        # Document → meeting merge (majority-vote)
│   ├── monthly_rollup.py       # Monthly digest rollup (meetings + permits + intel)
│   ├── transcribe.py           # Whisper API transcription
│   └── transcribe_local.py     # Local Whisper transcription
├── analysis/                   # Phase 4: LLM-powered summaries
│   ├── executive_summaries.py  # Yearly summaries (reads monthly digests)
│   ├── leadership_profiles.py  # Per-member profiles with grades
│   ├── council_member_summaries.py # Per-member summaries (reads per-record)
│   └── update_skill_intel.py   # Intel → skill supplement
├── data/                       # All data (gitignored)
│   ├── audio/                  # Downloaded meeting audio (pre-transcription)
│   ├── coastal/                # Coastal Commission meeting data
│   ├── cpra-templates/         # Public records act request templates
│   ├── documents/              # Extracted text (.txt — PDFs archived to S3)
│   ├── executive-summaries/    # Generated analysis + leadership profiles
│   ├── intel/                  # External feed hits
│   ├── meetings/               # Per-meeting metadata + agenda items (Legistar)
│   ├── nctd/                   # NCTD meeting data
│   ├── permits/                # eTRAKiT permit JSONL (one file per year)
│   ├── public-comments/        # Drafted public comments
│   ├── sandag/                 # SANDAG meeting data
│   ├── sdcounty/               # SD County BOS meeting data
│   ├── structured/             # JSONL records + meetings + monthly digests
│   ├── transcripts/            # Whisper transcription output (JSON)
│   └── transport-safety-refs/  # Vision Zero / crash data references
└── .claude/skills/
    ├── ca-housing-law/         # Auto-generated intel supplement
    └── civic-pipeline/         # Pipeline operation guide
```

## License

This is a civic tool. Use it to hold your city accountable.
