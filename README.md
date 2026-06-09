# Civics

An AI-augmented watchdog stack for monitoring municipal government. Built to give one person the legal knowledge, attention span, and time that holding local government accountable normally requires a full newsroom or advocacy organization to sustain.

Currently monitoring **Oceanside, CA** — City Council, Planning Commission, and 25+ boards and commissions — but designed to be replicated for any California city with a Legistar portal.

## What This Does

City councils make hundreds of decisions a year across dozens of meetings. Staff reports run 50-200 pages. Agendas drop 72 hours before votes. Public comment is 3 minutes. No single person can read everything, attend everything, and connect the dots across years of votes to see who's actually doing what.

This stack does:

1. **Scrapes** every meeting agenda, staff report, and attachment from the city's Legistar portal
2. **Transcribes** meeting audio/video (YouTube, KOCT) using Whisper
3. **Extracts** structured records from every document — votes, motions, fiscal impacts, housing items — into machine-readable JSONL
4. **Monitors** RSS feeds and web pages from housing enforcement orgs, state agencies, transit agencies, and local journalism for items relevant to the city
5. **Generates** per-council-member advocacy profiles graded on housing votes, yearly executive summaries, legal exposure analyses, and issue-specific deep dives
6. **Maintains** a legal reference library and policy analysis skill that any agent can use to evaluate votes against state law and peer-reviewed evidence

The output is structured data and analysis that turns years of meeting records into accountability tools — the kind of institutional memory that usually only exists in a reporter's notebook or a lobbyist's CRM.

## Architecture

Modeled after [Listen Public](https://www.listenpublic.com/)'s approach to civic monitoring: ingest everything, structure it, make it queryable.

```
Legistar API          YouTube/KOCT
     │                      │
     ▼                      ▼
 oceanside.py ──────── transcribe.py
     │                      │
     ▼                      ▼
 data/documents/       data/transcripts/
     │                      │
     └──────────┬───────────┘
                ▼
      extract_structured.py
                │
                ▼
      data/structured/*.jsonl     ◄── all-records.jsonl (1,619 records)
                │                     meetings-combined.jsonl (281 meetings)
                │                     monthly-digests.jsonl
                ▼
     ┌──────────┼──────────────┐
     ▼          ▼              ▼
executive_  council_member_  monthly_
summaries.py summaries.py   rollup.py
     │          │              │
     ▼          ▼              ▼
  yearly      per-member     monthly
  summaries   grades         digests

intel_feed.py ──► external monitoring (RSS, web pages)
     │              40+ sources: CalHDF, YIMBY Law, HCD, SANDAG,
     ▼              NCTD, Voice of SD, CalMatters, Holland & Knight
update_skill_intel.py ──► recent-developments.md (auto-updated)
```

### Pipeline Scripts

| Script | What It Does |
|--------|-------------|
| `oceanside.py` | Scrapes Legistar for meetings, agendas, staff reports, attachments. Downloads PDFs, extracts text |
| `discover_videos.py` | Matches YouTube playlist videos to meeting IDs by date |
| `transcribe.py` | Downloads audio, transcribes via OpenAI Whisper API |
| `transcribe_local.py` | Local Whisper transcription (no API) |
| `extract_structured.py` | LLM extraction of structured JSONL records from raw document text |
| `meeting_merge.py` | Merges document-level records with meeting metadata |
| `monthly_rollup.py` | Aggregates records into monthly digests (pure data, no LLM) |
| `executive_summaries.py` | Generates yearly executive summaries from structured data |
| `council_member_summaries.py` | Generates per-member housing advocacy profiles with letter grades |
| `intel_feed.py` | Monitors 40+ external feeds for relevant items |
| `update_skill_intel.py` | Generates recent-developments supplement from intel hits |
| `civic_utils.py` | Shared utilities |
| `nctd.py` | NCTD-specific data collection |

### Data

| Path | Contents |
|------|----------|
| `data/documents/` | ~1,950 staff reports, attachments, resolutions (PDF + extracted text + structured JSON) |
| `data/structured/` | JSONL databases: all records, meetings, monthly digests |
| `data/executive-summaries/` | Yearly summaries, issue analyses, legal exposure forecasts |
| `data/executive-summaries/council-members/` | 12 individual profiles with housing grades and vote tables |

### Knowledge Base

| Path | Contents |
|------|----------|
| `skills/housing-policy-analysis/` | Portable policy analysis skill — methodology, CA housing law, case law, obstruction patterns, evidence base, sources. Designed for agent consumption |
| `legal-reference/` | 21 files of CA housing law — statutes, enforcement frameworks, case opinions, advocacy playbooks |
| `research/` | Peer-reviewed literature reviews (density, fiscal productivity, sprawl costs) |

## What's Monitored

### Government Bodies (28)

City Council, Planning Commission, Housing Commission, Utilities Commission, Arts Commission, Parks and Recreation, Harbor and Beaches, Historic Preservation, Economic Development, Downtown Advisory Committee, Library Board, and 17 others.

### External Sources (40+)

State agencies (HCD, Attorney General, CCC, OPR), regional agencies (SANDAG, NCTD, MTS), enforcement orgs (CalHDF, YIMBY Law, Californians for Homeownership, CA YIMBY, Circulate SD, YIMBY Dems), legal analysis (Holland & Knight, Cox Castle), journalism (Voice of San Diego, CalMatters), and academic research.

### Council Members Profiled (12)

Each profile includes a housing advocacy letter grade, net score, every recorded vote on housing-related items, factional analysis, and an assessment of the gap between stated goals and actual votes. Current and recent members across the 2020-2026 period.

## Replicating This

The stack is designed to work for any California city that uses Legistar (most do). To adapt:

1. **Change the scraper target.** `oceanside.py` hits `oceanside.legistar.com`. Swap the base URL and body names for your city.
2. **Adjust the video sources.** `discover_videos.py` looks for YouTube playlists matching meeting dates. Point it at your city's channel.
3. **Update the intel feed.** `intel_feed.py` monitors feeds relevant to Oceanside/San Diego. Replace with your city's regional sources.
4. **Keep the analysis stack.** `extract_structured.py`, `council_member_summaries.py`, and `executive_summaries.py` are jurisdiction-agnostic.
5. **Keep the skill.** `skills/housing-policy-analysis/` covers California housing law statewide — no modification needed for any CA city.

The legal reference library and evidence base are California-specific. The methodology is universal.

## Requirements

- Python 3.10+
- OpenAI API key (for Whisper transcription and LLM extraction)
- Anthropic API key (for Claude-based analysis)
- `pdftotext` (poppler-utils)
- Standard Python packages (requests, feedparser, etc.)

## License

This is a civic tool. Use it to hold your city accountable.
