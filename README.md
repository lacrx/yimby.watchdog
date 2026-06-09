# Civics

An AI-augmented watchdog stack for monitoring municipal government. Built to give one person the legal knowledge, attention span, and time that holding local government accountable normally requires a full newsroom or advocacy organization to sustain.

Currently monitoring **Oceanside, CA** — City Council, Planning Commission, and 25+ boards and commissions — but designed to be replicated for any California city with a Legistar portal.

Modeled after [Listen Public](https://www.listenpublic.com/)'s approach to civic monitoring: ingest everything, structure it, make it queryable.

## What This Does

City councils make hundreds of decisions a year across dozens of meetings. Staff reports run 50-200 pages. Agendas drop 72 hours before votes. Public comment is 3 minutes. No single person can read everything, attend everything, and connect the dots across years of votes to see who's actually doing what.

This stack does:

1. **Scrapes** every meeting agenda, staff report, and attachment from the city's Legistar portal
2. **Transcribes** meeting audio/video (YouTube, KOCT) using Whisper
3. **Extracts** structured records from every document — votes, motions, fiscal impacts, housing items — into machine-readable JSONL
4. **Rolls up** per-document records into monthly digests (pure data, no LLM, zero information loss)
5. **Monitors** RSS feeds and web pages from housing enforcement orgs, state agencies, transit agencies, and local journalism for items relevant to the city
6. **Generates** per-council-member advocacy profiles graded on housing votes, yearly executive summaries, legal exposure analyses, and issue-specific deep dives
7. **Maintains** a legal reference library and policy analysis skill that any agent can use to evaluate votes against state law and peer-reviewed evidence

The output is structured data and analysis that turns years of meeting records into accountability tools — the kind of institutional memory that usually only exists in a reporter's notebook or a lobbyist's CRM.

## Data Flow

```
Legistar API              YouTube/KOCT
     │                          │
     ▼                          ▼
 oceanside.py              transcribe.py
     │                          │
     ▼                          ▼
 data/documents/*.txt      data/transcripts/*.txt
     │                          │
     └────────────┬─────────────┘
                  ▼
       extract_structured.py          ◄── claude -p per document
                  │
                  ▼
       data/structured/*.json         ◄── one JSON per source doc
                  │
          ┌───────┼──────────┐
          ▼       ▼          ▼
    meeting_   rebuild     all-records.jsonl
    merge.py  combined     (flat concat)
          │
          ▼
    meetings-combined.jsonl
                  │
                  ▼
         monthly_rollup.py            ◄── pure data, no LLM
                  │
                  ▼
         monthly-digests.jsonl        ◄── primary input for summaries
                  │
          ┌───────┴──────────┐
          ▼                  ▼
   executive_          council_member_
   summaries.py        summaries.py
   (reads monthly)     (reads per-record)
          │                  │
          ▼                  ▼
    yearly executive    per-member profiles
    summaries (.md)     with grades (.md)

intel_feed.py ──► external monitoring (40+ RSS/web sources)
     │              CalHDF, YIMBY Law, HCD, SANDAG, NCTD,
     ▼              Voice of SD, CalMatters, Holland & Knight
update_skill_intel.py ──► recent-developments.md (auto-updated)
```

## Nightly Pipeline

Runs via cron at 1:00 AM. Three phases, prioritized so new meetings always get processed first:

```
1:00 AM  Phase 1 — GATHER
         ├── Fetch new meetings from Legistar (Oceanside + NCTD)
         ├── Check intel feeds (RSS, web pages)
         ├── Discover new meeting videos
         └── Transcribe audio (if enabled)

         Phase 2 — MERGE & ROLL UP
         ├── Merge document records into per-meeting records
         └── Rebuild monthly digests

         Phase 3 — EXTRACT (remaining time)
         ├── Structured extraction via claude -p
         ├── Newest documents first (recent meetings get priority)
         └── Hard cutoff at 3:00 AM

3:00 AM  Done. Usage window recovers before morning.
```

Extraction is resumable — each document produces its own output file. The pipeline picks up where it left off the next night.

### Cron Setup

```bash
# Nightly pipeline: 1am-3am, zero API cost
0 1 * * * cd ~/repos/civics && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1
```

### Processing Modes

| Mode | Summarizer | Transcriber | Cost | Use Case |
|------|-----------|-------------|------|----------|
| **local** | `claude -p` (subscription) | faster-whisper | $0 | Nightly cron (default) |
| **hybrid** | `claude -p` (subscription) | Whisper API | ~$0.90/meeting | Better transcription quality |
| **full** | Claude API (Opus) | Whisper API | ~$44/full-run | Bulk reprocessing, exec summaries via API |

## Pipeline Scripts

| Script | What It Does |
|--------|-------------|
| `oceanside.py` | Scrapes Legistar for meetings, agendas, staff reports, attachments. Downloads PDFs, extracts text |
| `discover_videos.py` | Matches YouTube playlist videos to meeting IDs by date |
| `transcribe.py` | Downloads audio, transcribes via OpenAI Whisper API |
| `transcribe_local.py` | Local Whisper transcription (no API) |
| `extract_structured.py` | LLM extraction of structured JSONL records from raw document text. Newest first, resumable |
| `meeting_merge.py` | Merges document-level records with meeting metadata |
| `monthly_rollup.py` | Aggregates records into monthly digests (pure data, no LLM, zero information loss) |
| `executive_summaries.py` | Generates yearly executive summaries from monthly digests |
| `council_member_summaries.py` | Generates per-member housing advocacy profiles with letter grades from per-record data |
| `intel_feed.py` | Monitors 40+ external feeds for relevant items |
| `update_skill_intel.py` | Generates recent-developments supplement from intel hits |
| `civic_utils.py` | Shared utilities |
| `nctd.py` | NCTD-specific data collection |
| `civic-pipeline` | Bash orchestrator — runs everything in order with time limits |

## Structured Data Layer

Every raw source (PDF text, transcript) becomes a structured JSON record with typed fields:

- `votes[]` — item, result, yes/no/abstain names
- `housing_items[]` — type, units, outcome, state_law_flags
- `fiscal_items[]` — description, amount, type
- `legal_flags[]` — potential violations, enforcement actions
- `council_positions[]` — member, stance, evidence
- `advocacy_score` — green/yellow/red/neutral

Per-document records roll up into monthly digests (no LLM, pure concatenation + dedup). Executive summaries read monthly digests (~340K) instead of per-record data (~2.5M) — same information, 7x less input.

```bash
# Check extraction progress
python extract_structured.py --stats

# Query with jq
jq 'select(.advocacy_score == "red")' data/structured/all-records.jsonl
jq 'select(.council_positions[].member == "Joyce")' data/structured/all-records.jsonl
```

## Directory Structure

```
civics/
├── civic-pipeline              # Bash orchestrator (cron entry point)
├── oceanside.py                # Legistar scraper
├── nctd.py                     # NCTD scraper
├── transcribe.py               # Whisper API transcription
├── transcribe_local.py         # Local Whisper transcription
├── discover_videos.py          # YouTube video matching
├── extract_structured.py       # LLM → structured JSONL
├── meeting_merge.py            # Document → meeting merge
├── monthly_rollup.py           # Monthly digest rollup
├── executive_summaries.py      # Yearly summaries (reads monthly digests)
├── council_member_summaries.py # Per-member profiles (reads per-record)
├── intel_feed.py               # External source monitor
├── update_skill_intel.py       # Intel → skill supplement
├── civic_utils.py              # Shared utilities
├── data/                       # All data (gitignored)
│   ├── documents/              # Raw PDFs + extracted text
│   ├── transcripts/            # Meeting transcripts
│   ├── structured/             # JSONL records + monthly digests
│   ├── executive-summaries/    # Generated analysis
│   ├── intel/                  # External feed hits
│   └── public-comments/        # Council comment submissions
├── skills/                     # Portable agent skills
│   └── housing-policy-analysis/  # 7-file policy analysis framework
├── legal-reference/            # CA housing law library (21 files)
├── research/                   # Peer-reviewed literature reviews
└── archive/                    # Completed one-time scripts
```

## What's Monitored

**Government Bodies (28):** City Council, Planning Commission, Housing Commission, Utilities Commission, Arts Commission, Parks and Recreation, Harbor and Beaches, Historic Preservation, Economic Development, Downtown Advisory Committee, Library Board, and 17 others.

**External Sources (40+):** State agencies (HCD, Attorney General, CCC), regional agencies (SANDAG, NCTD, MTS), enforcement orgs (CalHDF, YIMBY Law, Californians for Homeownership, CA YIMBY, Circulate SD, YIMBY Dems), legal analysis (Holland & Knight, Cox Castle), journalism (Voice of San Diego, CalMatters).

**Council Members Profiled (12):** Each profile includes a housing advocacy letter grade, net score, every recorded vote on housing-related items, factional analysis, and an assessment of the gap between stated goals and actual votes.

## Replicating This

The stack works for any California city with a Legistar portal (most cities use one). To adapt:

1. **Change the scraper target.** `oceanside.py` hits `oceanside.legistar.com`. Swap the base URL and body names for your city.
2. **Adjust the video sources.** `discover_videos.py` looks for YouTube playlists matching meeting dates. Point it at your city's channel.
3. **Update the intel feed.** `intel_feed.py` monitors feeds relevant to Oceanside/San Diego. Replace with your city's regional sources, keep the statewide sources (HCD, CalHDF, YIMBY Law, CalMatters).
4. **Keep the analysis stack.** `extract_structured.py`, `monthly_rollup.py`, `council_member_summaries.py`, and `executive_summaries.py` are jurisdiction-agnostic. No changes needed.
5. **Keep the skill.** `skills/housing-policy-analysis/` covers California housing law statewide. No modification needed for any CA city.
6. **Set up cron.** One line: `0 1 * * * cd ~/repos/civics && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1`

### What You Need

- Python 3.10+
- Claude Code CLI (`claude -p` for structured extraction)
- `pdftotext` (poppler-utils)
- Standard Python packages: `requests`, `feedparser`, `yt-dlp`
- Optional: OpenAI API key (for Whisper transcription), Anthropic API key (for `full` mode)
- Optional: `faster-whisper` (for zero-cost local transcription)

### First Run

```bash
# 1. Clone and set up
git clone https://github.com/lacrx/stoside.watchdog.git civics
cd civics
python -m venv .venv && source .venv/bin/activate
pip install requests feedparser yt-dlp

# 2. Configure
cp .env.example .env
# Edit .env with your API keys (optional — local mode uses claude -p subscription)

# 3. Fetch meetings (current year)
python oceanside.py fetch --deep

# 4. Extract structured records
python extract_structured.py

# 5. Build monthly digests
python monthly_rollup.py

# 6. Generate executive summaries
python executive_summaries.py

# 7. Generate council member profiles
python council_member_summaries.py

# 8. Set up nightly cron
crontab -e
# Add: 0 1 * * * cd ~/repos/civics && ./civic-pipeline local --deep >> data/pipeline-cron.log 2>&1
```

The initial extraction takes time — `extract_structured.py` calls `claude -p` per document. For a city with 2,000+ documents, expect several nights of incremental processing. The pipeline is resumable; it picks up where it left off.

## License

This is a civic tool. Use it to hold your city accountable.
