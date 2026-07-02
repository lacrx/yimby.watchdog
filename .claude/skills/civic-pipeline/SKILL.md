---
name: civic-pipeline
description: Use when running or configuring the civic monitoring pipeline, adjusting cron schedules, or choosing between full/hybrid/local processing modes for meeting ingestion and structured extraction
---

# Civic Pipeline — Nightly Meeting Monitor

Automated ingestion of Oceanside, NCTD, SANDAG, SD County BOS, and CA Coastal Commission meetings, plus building permits and intel feeds. Runs nightly 1:00-4:00 AM via cron, with an evening catch at 6 PM Tue-Fri.

## Nightly Flow

```
1:00 AM  Phase 1 — GATHER
         ├── Fetch meetings from Legistar (Oceanside, NCTD, SD County)
         ├── Fetch meetings from Granicus (SANDAG)
         ├── Fetch meetings from state API (Coastal Commission)
         ├── Fetch building permits from eTRAKiT (current year, incremental)
         ├── Check intel feeds (17+ RSS/web sources)
         ├── Discover new meeting videos
         ├── Transcribe audio (if enabled)
         └── Sync raw sources to S3

         Phase 2 — MERGE & ROLL UP (no LLM, fast)
         ├── Merge document records into per-meeting records
         └── Rebuild stale monthly digests (content-hash detection)

         Phase 3 — EXTRACT (remaining time until 4 AM)
         ├── Structured extraction via claude -p (subscription only)
         ├── Newest documents first
         └── Hard cutoff at 4:00 AM

4:00 AM  Sync structured data to S3. Pipeline doctor runs diagnostics.

6:00 PM  Evening catch (Tue-Fri) — refetch + extract, no time limit.
         Matches when cities typically post agendas.
```

## Cron

```bash
# Nightly: fetch current year + extract until 4 AM
0 1 * * * cd ~/repos/yimby.watchdog && ./civic-pipeline local --deep --years 1 --extract-until 4 >> data/pipeline-cron.log 2>&1

# Evening catch: Tue-Fri, no extraction time limit
0 18 * * 2-5 cd ~/repos/yimby.watchdog && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1
```

## Modes

| Mode | Summarizer | Transcriber | Cost | Use Case |
|------|-----------|-------------|------|----------|
| **local** | `claude -p` (subscription) | faster-whisper | $0 | Nightly cron (default) |
| **hybrid** | `claude -p` (subscription) | Whisper API | ~$0.90/meeting | Better transcription |
| **full** | Claude API (Opus) | Whisper API | ~$44/full-run | Bulk reprocessing + exec summaries via API |

**IMPORTANT:** Pipeline always uses subscription (`claude -p`), never the API key. All `claude -p` subprocess calls strip `ANTHROPIC_API_KEY` from the environment to prevent accidental API credit burn.

## Data Flow

```
Per-document .txt (PDFs + transcripts)
        │
        ▼  transforms/extract_structured.py (claude -p, newest first, resumable)
Per-document .json
        │
        ├──► all-records.jsonl (flat concat)
        │
        ▼  transforms/meeting_merge.py (majority-vote date/agency selection)
meetings-combined.jsonl
        │
        ▼  transforms/monthly_rollup.py (no LLM — merges meetings + permits + intel)
monthly-digests.jsonl
        │
        └──► yimby.analysis repo (reads monthly digests + per-record for summaries/profiles)
```

Monthly digests are independent — each month has a content hash computed from all its inputs (meetings, permits, intel). A month only rebuilds when its hash changes.

## Structured JSONL Fields

Each source document becomes a JSON record with:

- `votes[]` — item, result, yes/no/abstain names
- `housing_items[]` — type, units, outcome, state_law_flags
- `fiscal_items[]` — description, amount, type
- `legal_flags[]` — potential violations, enforcement actions
- `council_positions[]` — member, stance, evidence
Monthly digests add:

- `permits` — total, estimated_units, by_type, by_status (SFD/duplex, multifamily, ADU only)
- `intel[]` — source, title, url, relevance_score

## Quick Reference

```bash
# Run pipeline manually
./civic-pipeline local --deep

# Check extraction progress
python transforms/extract_structured.py --stats

# Check which months need rebuild
python transforms/monthly_rollup.py --check

# Rebuild monthly digests (only stale months)
python transforms/monthly_rollup.py

# Force rebuild all months
python transforms/monthly_rollup.py --force

# Monthly stats table
python transforms/monthly_rollup.py --stats

# Query structured data
jq 'select(.council_positions[].member == "Joyce")' data/structured/all-records.jsonl
```

## Flags

| Flag | Effect |
|------|--------|
| `--deep` | Download individual staff reports, not just agendas/minutes |
| `--force` | Re-process already-extracted documents |
| `--years N` | Fetch N years back (default: 1) |
| `--transcribe` | Also run video transcription pipeline |
| `--extract-until H` | Stop extraction at hour H (0-23) |

## Architecture

```
civic-pipeline (bash wrapper, cron entry point)
├── scrapers/
│   ├── oceanside.py fetch → Legistar → data/documents/
│   ├── nctd.py fetch → Legistar → data/nctd/documents/
│   ├── sandag.py fetch → Granicus → data/sandag/
│   ├── sdcounty.py fetch → Legistar OData → data/sdcounty/
│   ├── coastal.py fetch → state API → data/coastal/
│   ├── intel_feed.py → data/intel/
│   ├── oceanside.py permits → data/permits/ (incremental eTRAKiT scrape)
│   └── discover_videos.py → data/transcribe-batch.json
├── transforms/
│   ├── meeting_merge.py → meetings-combined.jsonl
│   ├── monthly_rollup.py → monthly-digests.jsonl (meetings + permits + intel)
│   ├── extract_structured.py → data/structured/*.json (newest first, stops at cutoff)
│   ├── transcribe.py | transcribe_local.py → data/transcripts/
│   └── S3 sync → s3://yimby-watchdog-data/
└── pipeline_doctor.py → data/pipeline-doctor.jsonl (post-run diagnostics)
```

## S3 Backup

Raw sources and structured data sync to S3 each run. PDFs are archived there — local only keeps `.txt` files. Set `WATCHDOG_S3_BUCKET` in `.env` to enable; pipeline runs fine without it.

## Pipeline Doctor

`pipeline_doctor.py` runs after extraction each night. It:
- Parses extraction + pipeline logs for errors
- Identifies files that repeatedly block extraction (chunked auth failures)
- Skips oversized non-meeting files automatically
- Checks `claude -p` auth health (with API key stripped)
- Detects permit fetch errors (eTRAKiT connection failures)
- Detects regional scraper errors (Legistar 404s, Granicus timeouts)
- Tracks its own fixes and evaluates if they worked on the next run
- Logs diagnosis history to `data/pipeline-doctor.jsonl`

```bash
python pipeline_doctor.py              # diagnose + fix
python pipeline_doctor.py --dry-run    # diagnose only
python pipeline_doctor.py --history    # show diagnosis history
```

Executive summaries and council profiles run on-demand, not in the nightly pipeline.
