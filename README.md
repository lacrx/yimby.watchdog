# YIMBY Watchdog

An AI-augmented watchdog stack for monitoring municipal government. Built to give one person the legal knowledge, attention span, and time that holding local government accountable normally requires a full newsroom or advocacy organization to sustain.

Designed to be forked and deployed for any jurisdiction. Configure your city, agencies, and advocacy lens — the scraper platform modules and extraction pipeline are generic.

Modeled after [Listen Public](https://www.listenpublic.com/)'s approach to civic monitoring: ingest everything, structure it, make it queryable.

## What This Does

City councils make hundreds of decisions a year across dozens of meetings. Staff reports run 50-200 pages. Agendas drop 72 hours before votes. Public comment is 3 minutes. No single person can read everything, attend everything, and connect the dots across years of votes to see who's actually doing what.

This stack does:

1. **Scrapes** every meeting agenda, staff report, and attachment from municipal portals (Legistar, Granicus, eScribe, CivicPlus, CivicClerk) and agency APIs
2. **Fetches** building permits from eTRAKiT (housing-relevant types: SFD/duplex, multifamily, ADU)
3. **Transcribes** meeting audio/video (YouTube) using Whisper
4. **Extracts** structured records from every document — votes, motions, fiscal impacts, housing items — into machine-readable JSONL
5. **Rolls up** all data sources (meetings + permits + intel) into independent monthly digests with content-hash change detection
6. **Monitors** RSS feeds and web pages from housing enforcement orgs, state agencies, transit agencies, and local journalism for items relevant to your jurisdiction
7. **Archives** raw sources to S3

The output is structured data that turns years of meeting records into accountability tools. Downstream analysis (executive summaries, council member profiles, leadership grading) lives in `yimby.analysis`.

## Architecture

Split-mode pipeline — AWS Lambda handles scraping, local machine handles LLM extraction:

```
AWS Lambda (scheduled via EventBridge)
  ├── Scrape all enabled agencies (agencies.yaml)
  ├── Merge + rollup structured data
  ├── Sync raw data to S3
  └── Write S3 marker: pipeline/pending-extraction.json

Local machine (cron every 5 min)
  extract-watch
  ├── Poll S3 for marker
  ├── If found: download marker, delete from S3
  └── Run extract-local
        ├── Sync new docs from S3
        ├── Extract via claude -p (subscription, not API)
        └── Push structured records back to S3
```

## Configuration

Jurisdiction-specific settings live in AWS SSM Parameter Store (production) or `config.local.yaml` (local dev). The `config.py` module handles loading and caching.

### What's configurable

| Setting | SSM Key | Example |
|---------|---------|---------|
| City name | `identity/primary_city` | Oceanside |
| State | `identity/state` | California |
| Region label | `identity/region_label` | North San Diego County, CA |
| Advocacy lens | `advocacy/lens` | YIMBY + Strong Towns |
| Council roster | `figures/known_figures` | JSON dict of officials |
| RSS feeds | `feeds/rss_feeds` | JSON array of feed URLs |
| Direct keywords | `feeds/direct_keywords` | Tier 1 keywords |
| Relevance context | `feeds/relevance_context` | Current litigation/enforcement |
| Video playlists | `videos/playlists` | YouTube playlist IDs |
| State law flags | `extraction/state_law_flags` | HAA, SB330, SB79, ... |

### What stays in files

- `agencies.yaml` — agency registry (platform, base_url, bodies, enabled). Forks edit this directly.
- `.env` — AWS bootstrap (region, SSM prefix, S3 bucket)

## Data Flow

```
scrapers/
  Platform modules:
    legistar_html    (oceanside.py)        ─┐
    legistar_odata   (sdcounty.py)          │
    escribe          (sandag.py)            │
    custom_html      (nctd.py)             ├──► data/{agency}/documents/*.txt
    coastal_api      (coastal.py)           │
    granicus         (granicus.py)           │
    civicplus        (civicplus.py)          │
    civicclerk       (carlsbad.py)          │
    solana_drupal    (solana_beach.py)      ─┘

  YouTube ──► discover_videos.py ──► data/transcribe-batch.json
  eTRAKiT ──► etrakit.py ──► data/{agency}/permits/*.jsonl
  RSS/Web ──► intel_feed.py ──► data/intel/*.json

transforms/
  documents/*.txt ──► extract_structured.py (claude -p) ──► structured/*.json
  structured/*.json ──► meeting_merge.py ──► structured/meetings/*.json
  meetings + permits + intel ──► monthly_rollup.py ──► structured/monthly/*.json

S3 (yimby-watchdog-data)
  ├── raw/         extracted text per agency
  ├── structured/  records + meetings + monthly digests
  ├── pipeline/    pending-extraction marker
  └── metadata/    meeting metadata
```

## Pipeline Schedule

Lambda runs on EventBridge schedules (configurable in `infra/variables.tf`):

| Time | What |
|------|------|
| 1:00 AM daily | Full pipeline: preflight → scrape all → merge + rollup |
| 6:00 PM Tue-Fri | Evening catch: scrape + process (agendas often posted afternoon before meetings) |

After Lambda finishes, `extract-watch` detects the S3 marker within 5 minutes and starts `claude -p` extraction locally.

### Cron Setup

```bash
*/5 * * * * cd ~/repos/yimby.watchdog && ./extract-watch >> data/extract-watch.log 2>&1
```

### Manual Pipeline Run

```bash
# Full local run (scrape + extract, no Lambda)
./civic-pipeline local --deep --years 1

# Just extraction
./extract-local

# Just one agency
python scrapers/oceanside.py fetch --deep
```

## Supported Platforms

| Platform | Module | Used By |
|----------|--------|---------|
| Legistar HTML | `oceanside.py` | Any Legistar tenant |
| Legistar OData | `sdcounty.py` | Legistar webapi instances |
| eScribe | `sandag.py` | eScribe meeting portals |
| Granicus | `granicus.py` | Granicus-hosted agendas |
| CivicPlus | `civicplus.py` | CivicPlus portals |
| CivicClerk | `carlsbad.py` | CivicClerk portals |
| Coastal API | `coastal.py` | CA Coastal Commission |
| Custom HTML | `nctd.py` | Custom scraper pattern |

## Forking This

To deploy for your own jurisdiction:

### 1. Fork both repos

```bash
git clone https://github.com/you/yimby.watchdog.git
git clone https://github.com/you/yimby.analysis.git
```

### 2. Configure your agencies (`agencies.yaml`)

```yaml
agencies:
  your_city:
    name: City of Springfield
    platform: legistar_html
    base_url: https://springfield.legistar.com
    bodies:
      - City Council
      - Planning Commission
    deep_fetch: true
    lookback_months: 12
    enabled: true
```

### 3. Configure your jurisdiction (`config.local.yaml`)

```bash
cp config.local.yaml.example config.local.yaml
# Edit: city name, state, council roster, RSS feeds, advocacy lens
```

### 4. Set up AWS

```bash
cp .env.example .env
# Edit: AWS_REGION, SSM_PREFIX, WATCHDOG_S3_BUCKET

cd infra
# Edit variables.tf: project name, bucket name
terraform init && terraform apply

# Push config to SSM
python scripts/seed_ssm.py --write
```

### 5. First run

```bash
# Fetch meetings
./civic-pipeline local --deep --years 1

# Extract structured records
python transforms/extract_structured.py

# Merge + rollup
python transforms/meeting_merge.py
python transforms/monthly_rollup.py

# Set up cron for extract-watch
crontab -e
```

### What you keep as-is

- `transforms/extract_structured.py` — extraction prompt is jurisdiction-agnostic
- `transforms/meeting_merge.py` — merges by meeting_id, no city-specific logic
- `transforms/monthly_rollup.py` — rolls up meetings + permits + intel by month
- `config.py` — SSM/YAML loader
- All platform scraper modules — generic per platform

### Requirements

- Python 3.10+
- Claude Code CLI with active subscription (`claude -p` for extraction)
- `pdftotext` (poppler-utils)
- Python packages: `requests`, `feedparser`, `beautifulsoup4`, `lxml`, `pyyaml`, `yt-dlp`
- AWS account with Terraform (for Lambda + S3 + SSM)
- Optional: `faster-whisper` (for local transcription)

## Directory Structure

```
yimby.watchdog/
├── civic-pipeline              # Bash orchestrator (manual/legacy runs)
├── extract-watch               # Cron poller — detects new docs via S3 marker
├── extract-local               # Runs claude -p extraction, syncs to S3
├── lambda_handler.py           # AWS Lambda entry point
├── civic_utils.py              # Shared utilities (PDF, text extraction, LLM)
├── config.py                   # SSM/YAML config loader
├── config.local.yaml           # Local dev config (gitignored)
├── config.local.yaml.example   # Template for forks
├── agencies.yaml               # Agency registry (platforms, URLs, bodies)
├── pipeline_preflight.py       # Agency health checks
├── scrapers/                   # Platform-specific fetchers
│   ├── oceanside.py            # Legistar HTML
│   ├── sdcounty.py             # Legistar OData
│   ├── sandag.py               # eScribe
│   ├── nctd.py                 # Custom HTML
│   ├── coastal.py              # CA Coastal Commission API
│   ├── granicus.py             # Granicus
│   ├── civicplus.py            # CivicPlus
│   ├── carlsbad.py             # CivicClerk
│   ├── solana_beach.py         # Drupal-based
│   ├── etrakit.py              # eTRAKiT permits
│   ├── intel_feed.py           # RSS/web monitoring
│   └── discover_videos.py      # YouTube video matching
├── transforms/                 # Extraction and rollup
│   ├── extract_structured.py   # LLM → structured JSONL (claude -p)
│   ├── meeting_merge.py        # Document → meeting merge
│   ├── monthly_rollup.py       # Monthly digest rollup
│   ├── split_packets.py        # eScribe packet splitter
│   └── triage.py               # Document relevance filter
├── scripts/
│   ├── seed_ssm.py             # Push config.local.yaml → SSM
│   └── migrate_data_dirs.py    # Data directory migration
├── infra/                      # Terraform (Lambda, S3, EventBridge, IAM)
├── lib/                        # Storage utilities (S3 sync)
└── data/                       # All data (gitignored)
```

## License

This is a civic tool. Use it to hold your city accountable.
