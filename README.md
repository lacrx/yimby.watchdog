# YIMBY Watchdog

An AI-augmented watchdog stack for monitoring municipal government. Built to give one person the legal knowledge, attention span, and time that holding local government accountable normally requires a full newsroom or advocacy organization to sustain.

Designed to be forked and deployed for any jurisdiction. Configure your city, agencies, and advocacy lens; the scraper platform modules and extraction pipeline are generic.

Modeled after [Listen Public](https://www.listenpublic.com/)'s approach to civic monitoring: ingest everything, structure it, make it queryable.

## What This Does

City councils make hundreds of decisions a year across dozens of meetings. Staff reports run 50-200 pages. Agendas drop 72 hours before votes. Public comment is 3 minutes. No single person can read everything, attend everything, and connect the dots across years of votes to see who's actually doing what.

This stack does:

1. **Scrapes** every meeting agenda, staff report, and attachment from 22 agencies across 16 platform types
2. **Fetches** building permits (eTRAKiT, ArcGIS) and planning projects, cross-referenced into unified housing project entities
3. **Transcribes** meeting audio/video using Whisper
4. **Extracts** structured records from every document (votes, motions, fiscal impacts, housing items, council positions) into machine-readable JSONL
5. **Rolls up** all sources (meetings + permits + intel) into independent monthly digests with content-hash change detection
6. **Monitors** RSS feeds and web pages from housing enforcement orgs, state agencies, transit, and local journalism
7. **Exports** to Parquet for DuckDB queries
8. **Archives** raw sources to S3

The output is structured data that turns years of meeting records into accountability tools. Downstream analysis (executive summaries, council member profiles, leadership grading) lives in `yimby.analysis`.

## Architecture

Everything runs on the local machine via cron. `civic-pipeline local` scrapes, merges, extracts with `claude -p` (subscription, never API) and syncs to S3:

```
civic-pipeline local (cron: 1am nightly, 6pm Tue-Fri --hot-only)
  ├── Scrape all enabled agencies (agencies.yaml)
  ├── Permits + planning projects (current and previous year)
  ├── eTRAKiT status refresh (nightly only, ~300 open records per kind)
  ├── Merge + rollup structured data, Parquet export
  ├── Extract via claude -p: hot queue (last 14 days), then cold backlog
  ├── Sync raw + structured data to S3
  └── pipeline_doctor health check

escribe-sweep (cron, a few times a day)
  └── Retry eScribe downloads blocked by the Cloudflare challenge
```

A split mode (AWS Lambda scrapes on an EventBridge schedule, writes an S3 marker, and `extract-watch` picks it up locally) is designed in `infra/`, `lambda_handler.py` and `extract-watch`, but it is not deployed: Terraform state holds only the S3 bucket.

Agencies marked `local_only: true` in `agencies.yaml` are scraped by `civic-pipeline` on the local machine (WAF-blocked sites that reject AWS IPs).

## Supported Platforms

Meeting platforms (dispatched by `platform` field in `agencies.yaml`):

| Platform | Module | Sites |
|----------|--------|-------|
| Legistar HTML | `oceanside.py` | Oceanside |
| Legistar OData | `sdcounty.py` | SD County BOS |
| eScribe | `sandag.py` | SANDAG, Chula Vista, National City, Imperial Beach, La Mesa |
| Granicus | `granicus.py` | Encinitas, San Marcos, San Diego |
| CivicPlus | `civicplus.py` | Del Mar |
| Swagit | `swagit.py` | Carlsbad |
| Laserfiche | `laserfiche.py` | Poway, Vista |
| Municode | `municode.py` | Escondido |
| PrimeGov | `primegov.py` | Coronado |
| Coastal API | `coastal.py` | CA Coastal Commission |
| Solana Drupal | `solana_beach.py` | Solana Beach (dual-source: Drupal + eScribe) |
| Custom HTML | `nctd.py`, `elcajon.py`, `santee.py`, `lemon_grove.py` | NCTD, El Cajon, Santee, Lemon Grove |

Permit platforms (dispatched by `permits.platform`):

| Platform | Module | Sites |
|----------|--------|-------|
| eTRAKiT | `etrakit.py` | Oceanside (permits + planning projects) |
| ArcGIS Feature Service | `arcgis_permits.py` | Carlsbad (building permits) |

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

- `agencies.yaml` — agency registry (platform, base_url, bodies, enabled, lookback, tally_only). Forks edit this directly.
- `.env` — AWS bootstrap (region, SSM prefix, S3 bucket)

## Data Flow

```
scrapers/
  Meeting platforms:
    legistar_html    (oceanside.py)        ─┐
    legistar_odata   (sdcounty.py)          │
    escribe          (sandag.py)            │
    granicus         (granicus.py)           │
    civicplus        (civicplus.py)          │
    swagit           (swagit.py)            ├──► data/{agency}/documents/*.txt
    laserfiche       (laserfiche.py)         │
    municode         (municode.py)          │
    primegov         (primegov.py)           │
    coastal_api      (coastal.py)           │
    solana_drupal    (solana_beach.py)       │
    custom_html      (nctd.py, elcajon.py, │
                      santee.py, ...)      ─┘

  Permits:
    eTRAKiT  ──► etrakit.py ──► data/{agency}/permits/etrakit-permits-{YYYY}.jsonl
    ArcGIS   ──► arcgis_permits.py ──► data/{agency}/permits/arcgis-permits-{YYYY}.jsonl

  Planning projects:
    eTRAKiT  ──► etrakit.py projects ──► data/{agency}/permits/etrakit-projects-{YYYY}.jsonl

  Intel:
    RSS/Web  ──► intel_feed.py ──► data/intel/*.json

  Video:
    YouTube  ──► discover_videos.py ──► data/transcribe-batch.json

transforms/
  documents/*.txt ──► extract_structured.py (claude -p) ──► structured/*.json
  structured/*.json ──► meeting_merge.py ──► structured/meetings/*.json
  meetings + permits + intel ──► monthly_rollup.py ──► structured/monthly/*.json
  permits + projects + meetings ──► housing_projects.py ──► structured/housing-projects.json
  structured/ ──► export_parquet.py ──► data/exports/*.parquet

S3 (yimby-watchdog-data)
  ├── raw/         extracted text per agency
  ├── structured/  records + meetings + monthly digests
  ├── pipeline/    pending-extraction marker
  └── metadata/    meeting metadata
```

## Pipeline Schedule

Local cron:

| Time | What |
|------|------|
| 1:00 AM daily | Full pipeline: scrape all, permits, status refresh, merge + rollup, hot + cold extraction until 4:00 |
| 6:00 PM Tue-Fri | Evening catch (`--hot-only`): scrape + recent-meeting extraction (agendas often posted afternoon before meetings) |
| 4:17, 8:17, 11:17, 14:17, 21:17 | `escribe-sweep`: retry Cloudflare-blocked eScribe downloads |

### Cron Setup

```bash
0 1 * * * cd ~/repos/yimby.watchdog && ./civic-pipeline local --deep --extract-until 4 >> data/pipeline-cron.log 2>&1
0 18 * * 2-5 cd ~/repos/yimby.watchdog && ./civic-pipeline local --deep --hot-only >> data/pipeline-cron.log 2>&1
17 4,8,11,14,21 * * * cd ~/repos/yimby.watchdog && ./escribe-sweep >> data/escribe-sweep.log 2>&1
```

### Manual Pipeline Run

```bash
# Full local run (scrape + extract, always use --extract-until for manual runs)
./civic-pipeline local --deep --years 1 --extract-until 20

# Just extraction
./extract-local

# Just one agency
python scrapers/oceanside.py fetch --deep

# Permits
python scrapers/etrakit.py fetch                       # Oceanside (current year, incremental)
python scrapers/arcgis_permits.py fetch --agency carlsbad --full  # Carlsbad

# Planning projects
python scrapers/etrakit.py projects

# Housing project cross-reference
python transforms/housing_projects.py --stats
```

## Pipeline Operations

```bash
# Health checks
python pipeline_preflight.py              # agency connectivity
python pipeline_doctor.py                 # post-run diagnostics + autofix
python pipeline_doctor.py --history       # diagnosis history

# Extraction status
python transforms/extract_structured.py --stats

# Monthly digest status
python transforms/monthly_rollup.py --check    # which months need rebuild
python transforms/monthly_rollup.py --stats    # stats table

# Estimate extraction volume
python pipeline_estimate.py                    # since earliest enabled_date
python pipeline_estimate.py --days 14          # last 14 days

# Export to Parquet (for DuckDB)
python transforms/export_parquet.py

# Deploy Lambda (after changing lambda_handler, scrapers, or agencies.yaml)
./deploy-lambda
```

## Agency Tiers

Agencies in `agencies.yaml` fall into three operational tiers:

| Tier | Flag | Behavior |
|------|------|----------|
| Full extraction | (default) | Scrape + extract via claude -p. Structured JSONL output. |
| Forward-only | `forward_only: true` | Scrape + extract, but only for meetings after `enabled_date`. No backfill. |
| Tally-only | `tally_only: true` | Scrape only (for cost projection). No extraction. |

Currently: 22 agencies enabled, 13 with extraction, 9 tally-only.

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
./civic-pipeline local --deep --years 1 --extract-until 20

# Merge + rollup
python transforms/meeting_merge.py
python transforms/monthly_rollup.py

# Set up cron (see Cron Setup above)
crontab -e
```

### What you keep as-is

- `transforms/extract_structured.py` — extraction prompt is jurisdiction-agnostic
- `transforms/meeting_merge.py` — merges by meeting_id, no city-specific logic
- `transforms/monthly_rollup.py` — rolls up meetings + permits + intel by month
- `transforms/housing_projects.py` — cross-references permits, projects, and meeting extractions
- `config.py` — SSM/YAML loader
- All platform scraper modules — generic per platform

### Requirements

- Python 3.10+
- Claude Code CLI with active subscription (`claude -p` for extraction)
- `pdftotext` (poppler-utils)
- Python packages: `requests`, `feedparser`, `beautifulsoup4`, `lxml`, `pyyaml`, `yt-dlp`
- AWS account with Terraform (S3 + SSM; Lambda only for the undeployed split mode)
- Optional: `faster-whisper` (local transcription), `pyarrow` (Parquet export)

## Directory Structure

```
yimby.watchdog/
├── civic-pipeline              # Bash orchestrator (manual/local runs)
├── escribe-sweep               # Cron: retry Cloudflare-blocked eScribe downloads
├── extract-watch               # Split mode only (not deployed): S3 marker poller
├── extract-local               # Runs claude -p extraction, syncs to S3
├── extract-backlog             # Backlog extraction runner
├── deploy-lambda               # Lambda deployment script
├── lambda_handler.py           # AWS Lambda entry point
├── civic_utils.py              # Shared utilities (PDF, text, agency helpers)
├── config.py                   # SSM/YAML config loader
├── config.local.yaml           # Local dev config (gitignored)
├── agencies.yaml               # Agency registry
├── pipeline_preflight.py       # Agency connectivity checks
├── pipeline_doctor.py          # Post-run diagnostics + autofix
├── pipeline_estimate.py        # Extraction volume estimator
├── scrapers/
│   ├── oceanside.py            # Legistar HTML
│   ├── sdcounty.py             # Legistar OData
│   ├── sandag.py               # eScribe
│   ├── granicus.py             # Granicus
│   ├── civicplus.py            # CivicPlus
│   ├── swagit.py               # Swagit
│   ├── laserfiche.py           # Laserfiche
│   ├── municode.py             # Municode
│   ├── primegov.py             # PrimeGov
│   ├── coastal.py              # CA Coastal Commission API
│   ├── solana_beach.py         # Drupal + eScribe dual-source
│   ├── carlsbad.py             # CivicClerk (Carlsbad CMS)
│   ├── nctd.py                 # Custom HTML (NCTD)
│   ├── elcajon.py              # Custom HTML (El Cajon)
│   ├── santee.py               # Custom HTML (Santee)
│   ├── lemon_grove.py          # Custom HTML (Lemon Grove)
│   ├── etrakit.py              # eTRAKiT permits + planning projects
│   ├── arcgis_permits.py       # ArcGIS Feature Service permits
│   ├── intel_feed.py           # RSS/web monitoring
│   └── discover_videos.py      # YouTube video matching
├── transforms/
│   ├── extract_structured.py   # LLM extraction (claude -p)
│   ├── meeting_merge.py        # Document-to-meeting merge
│   ├── monthly_rollup.py       # Monthly digest rollup
│   ├── housing_projects.py     # Unified housing project builder
│   ├── permit_projects.py      # Permit-to-project linking
│   ├── enrich_permits.py       # Permit zone/GIS enrichment
│   ├── export_parquet.py       # Parquet export for DuckDB
│   ├── split_packets.py        # eScribe packet splitter
│   ├── transcribe.py           # Whisper API transcription
│   ├── transcribe_local.py     # Local faster-whisper transcription
│   └── triage.py               # Document relevance filter
├── scripts/
│   ├── seed_ssm.py             # Push config.local.yaml to SSM
│   ├── seed_doc_index.py       # Bootstrap doc-index from existing files
│   ├── migrate_data_dirs.py    # Data directory migration
│   └── verify_legistar.py      # Legistar connectivity check
├── lib/
│   └── storage.py              # Storage abstraction (local or S3)
├── infra/                      # Terraform (Lambda, S3, EventBridge, IAM)
└── data/                       # All data (gitignored)
    ├── {agency}/documents/     # Raw scraped text
    ├── {agency}/permits/       # Permit JSONL files
    ├── structured/             # Extracted records, meetings, monthly digests
    ├── exports/                # Parquet files
    ├── transcripts/            # Whisper output
    └── intel/                  # RSS/web intel
```

## License

This is a civic tool. Use it to hold your city accountable.
