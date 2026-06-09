---
name: civic-pipeline
description: Use when running or configuring the civic monitoring pipeline, adjusting cron schedules, or choosing between full/hybrid/local processing modes for meeting ingestion and summarization
---

# Civic Pipeline — Three-Tier Meeting Monitor

Automated ingestion of Oceanside City Council, Planning Commission, and NCTD Board meetings. Three modes trade off cost vs capability.

## Modes

| Mode | Summarizer | Transcriber | Cost | Use Case |
|------|-----------|-------------|------|----------|
| **full** | Claude Sonnet API + Opus exec summaries | Whisper API | ~$44/full-run | Bulk/historical processing, executive summary generation |
| **hybrid** | `claude -p` (subscription) | Whisper API | ~$0.90/meeting (transcription only) | Daily cron, normal operations |
| **local** | `claude -p` (subscription) | faster-whisper (local) | $0 | Overnight batch, zero-cost daily checks |

## Quick Reference

```bash
# Daily cron (recommended default)
./civic-pipeline hybrid --deep

# Zero-cost overnight batch
./civic-pipeline local --deep

# Bulk historical reprocessing with executive summaries
./civic-pipeline full --deep --years 6 --force

# With video transcription
./civic-pipeline hybrid --deep --transcribe
./civic-pipeline local --deep --transcribe
```

## Cron Setup

```bash
# Hybrid daily at 8am (Whisper API for transcription, subscription for summaries)
0 8 * * * cd ~/repos/civics && ./civic-pipeline hybrid --deep >> data/pipeline-cron.log 2>&1

# Local nightly at 2am (zero API cost)
0 2 * * * cd ~/repos/civics && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1
```

## Pipeline Steps

Two phases: GATHER all raw sources, then PROCESS.

**Phase 1 — Gather:**
1. **Fetch** — Scrape Legistar (Oceanside) and gonctd.com (NCTD), download PDFs, extract text
2. **Video discovery** — Auto-match YouTube videos to meetings via playlist scraping
3. **Transcribe** (`--transcribe`) — Audio-to-text via Whisper API or faster-whisper

**Phase 2 — Process (after all sources collected):**
4. **Structured extraction** — ALL raw sources (docs + transcripts) → JSONL via `claude -p`
5. **Monthly rollup** — Deterministic merge into monthly digests (no LLM)
6. **Executive summaries** (full mode or catch-up) — Opus reads monthly digests + policy skills

Late-arriving sources (video posted days after meeting): next cron run detects new transcript, re-extracts that meeting's JSONL, rebuilds affected monthly digest.

## Structured JSONL Layer

Primary data layer. Each raw source (doc or transcript) becomes a structured JSON record with typed fields:

- `votes[]` — item, result, yes/no/abstain names
- `housing_items[]` — type, units, outcome, state_law_flags
- `fiscal_items[]` — description, amount, type
- `legal_flags[]` — potential violations, enforcement actions
- `council_positions[]` — member, stance, evidence
- `advocacy_score` — green/yellow/red/neutral

```bash
# Extract structured data from all summaries (overnight run)
python extract_structured.py

# Re-extract everything
python extract_structured.py --force

# Check progress
python extract_structured.py --stats

# Rebuild combined JSONL from individual records
python extract_structured.py --rebuild

# Query with jq
jq 'select(.advocacy_score == "red")' data/structured/all-records.jsonl
jq 'select(.council_positions[].member == "Joyce")' data/structured/all-records.jsonl
```

Storage: `data/structured/` — one `.json` per document + `all-records.jsonl` combined file.

## Monthly Digests

Deterministic rollup of individual JSONL records into per-month digests. No LLM — pure array concatenation + deduplication. Zero information loss.

```bash
# Build missing monthly digests
python monthly_rollup.py

# Rebuild all months
python monthly_rollup.py --force

# Rebuild one month
python monthly_rollup.py --month 2026-03

# Check for stale months
python monthly_rollup.py --check

# Stats
python monthly_rollup.py --stats
```

Storage: `data/structured/monthly/{YYYY-MM}.json` + `monthly-digests.jsonl` combined file.

Daily cron auto-rebuilds current month + any months with changed record counts. Executive summaries read monthly digests by default (`--source monthly`), reducing input from ~2,600 records to ~72 digests.

## Flags

| Flag | Effect |
|------|--------|
| `--deep` | Download individual staff reports, not just agendas/minutes |
| `--force` | Re-summarize already-processed documents |
| `--years N` | Fetch N years back (default: 1 = current year) |
| `--transcribe` | Also run video transcription pipeline |

## Individual Script Usage

```bash
# Oceanside only, local summarizer
python oceanside.py watch --deep --summarizer local

# NCTD only, API summarizer
python nctd.py watch --summarizer api

# Local transcription of a single meeting
python transcribe_local.py transcribe <meeting_id> <youtube_url>

# Batch local transcription
python transcribe_local.py batch data/transcribe-batch.json
```

## Architecture

```
civic-pipeline (bash wrapper)
├── oceanside.py fetch → Legistar scraper → data/documents/
├── nctd.py fetch → gonctd.com scraper → data/nctd/documents/
├── intel_feed.py → data/intel/intel-*.json
├── update_skill_intel.py → .claude/skills/ca-housing-law/recent-developments.md
├── discover_videos.py → data/transcribe-batch.json
├── transcribe.py (Whisper API) or transcribe_local.py (faster-whisper)
├── extract_structured.py → data/structured/*.json + all-records.jsonl
├── meeting_merge.py → per-meeting merged records
├── monthly_rollup.py → data/structured/monthly/*.json + monthly-digests.jsonl
└── executive_summaries.py (reads monthly digests, --mode api|local)

civic-catchup (historical reprocessor, runs 1am-7am until caught up)
└── Same steps with --force --years 7, self-removes from cron when done
```

## Cost Estimates

| Scenario | Monthly Cost |
|----------|-------------|
| hybrid daily cron, no transcription | ~$0 (subscription only) |
| hybrid daily cron + 6 transcriptions | ~$5-7 |
| local daily cron + local transcription | $0 |
| full reprocess (ad-hoc, ~3-4x/year) | ~$44/run |

## Model Details

- **Structured extraction**: `claude -p` (subscription Opus) — raw text → typed JSONL
- **Executive summaries**: Claude Opus 4-6 via API, max 2000-4000 tokens
- **API transcription**: OpenAI Whisper-1, $0.006/min
- **Local transcription**: faster-whisper large-v3, int8 quantization, CPU or GPU
