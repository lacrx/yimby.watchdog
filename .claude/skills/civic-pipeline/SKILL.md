---
name: civic-pipeline
description: Use when running or configuring the civic monitoring pipeline, adjusting cron schedules, or choosing between full/hybrid/local processing modes for meeting ingestion and structured extraction
---

# Civic Pipeline — Nightly Meeting Monitor

Automated ingestion of Oceanside City Council, Planning Commission, and NCTD Board meetings. Runs nightly 1:00-3:00 AM via cron.

## Nightly Flow

```
1:00 AM  Phase 1 — GATHER
         ├── Fetch new meetings from Legistar (Oceanside + NCTD)
         ├── Check intel feeds (40+ RSS/web sources)
         ├── Discover new meeting videos
         └── Transcribe audio (if enabled)

         Phase 2 — MERGE & ROLL UP (no LLM, fast)
         ├── Merge document records into per-meeting records
         └── Rebuild monthly digests

         Phase 3 — EXTRACT (remaining time until cutoff)
         ├── Structured extraction via claude -p
         ├── Newest documents first
         └── Hard cutoff at 3:00 AM

3:00 AM  Done.
```

## Cron

```bash
0 1 * * * cd ~/repos/civics && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1
```

## Modes

| Mode | Summarizer | Transcriber | Cost | Use Case |
|------|-----------|-------------|------|----------|
| **local** | `claude -p` (subscription) | faster-whisper | $0 | Nightly cron (default) |
| **hybrid** | `claude -p` (subscription) | Whisper API | ~$0.90/meeting | Better transcription |
| **full** | Claude API (Opus) | Whisper API | ~$44/full-run | Bulk reprocessing + exec summaries via API |

## Data Flow

```
Per-document .txt (PDFs + transcripts)
        │
        ▼  extract_structured.py (claude -p, newest first, resumable)
Per-document .json
        │
        ├──► all-records.jsonl (flat concat)
        │
        ▼  meeting_merge.py
meetings-combined.jsonl
        │
        ▼  monthly_rollup.py (no LLM, pure data)
monthly-digests.jsonl (340K vs 2.5M per-record — 7x smaller)
        │
        ├──► executive_summaries.py (reads monthly digests)
        └──► council_member_summaries.py (reads per-record for full attribution)
```

## Structured JSONL Fields

Each source document becomes a JSON record with:

- `votes[]` — item, result, yes/no/abstain names
- `housing_items[]` — type, units, outcome, state_law_flags
- `fiscal_items[]` — description, amount, type
- `legal_flags[]` — potential violations, enforcement actions
- `council_positions[]` — member, stance, evidence
- `advocacy_score` — green/yellow/red/neutral

## Quick Reference

```bash
# Run pipeline manually
./civic-pipeline local --deep

# Check extraction progress
python extract_structured.py --stats

# Rebuild monthly digests
python monthly_rollup.py --force

# Generate executive summaries (reads monthly digests, default)
python executive_summaries.py

# Generate council member profiles (reads per-record data)
python council_member_summaries.py

# Query structured data
jq 'select(.advocacy_score == "red")' data/structured/all-records.jsonl
jq 'select(.council_positions[].member == "Joyce")' data/structured/all-records.jsonl
```

## Flags

| Flag | Effect |
|------|--------|
| `--deep` | Download individual staff reports, not just agendas/minutes |
| `--force` | Re-process already-extracted documents |
| `--years N` | Fetch N years back (default: 1) |
| `--transcribe` | Also run video transcription pipeline |

## Architecture

```
civic-pipeline (bash wrapper, cron entry point)
├── oceanside.py fetch → Legistar → data/documents/
├── nctd.py fetch → gonctd.com → data/nctd/documents/
├── intel_feed.py → data/intel/
├── update_skill_intel.py → .claude/skills/ca-housing-law/recent-developments.md
├── discover_videos.py → data/transcribe-batch.json
├── transcribe.py | transcribe_local.py → data/transcripts/
├── extract_structured.py → data/structured/*.json (newest first, stops at cutoff)
├── meeting_merge.py → meetings-combined.jsonl
└── monthly_rollup.py → monthly-digests.jsonl
```

Executive summaries and council profiles run on-demand, not in the nightly pipeline.
