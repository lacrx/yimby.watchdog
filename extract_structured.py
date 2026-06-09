#!/usr/bin/env python3
"""
Extract structured JSONL records directly from raw document text.

Reads raw extracted text (PDFs → pdftotext), extracts structured fields via
claude -p, writes one JSON record per document to data/structured/.

This extracts from the source documents, not from prose summaries — higher
fidelity, no information lost to intermediate summarization.

Usage:
    python extract_structured.py                # process un-extracted documents
    python extract_structured.py --force        # re-extract everything
    python extract_structured.py --dry-run      # show what would be processed
    python extract_structured.py --stats        # show extraction stats

Output: data/structured/{doc-stem}.json (one file per document)
Combined: data/structured/all-records.jsonl (one line per record, for bulk reads)
"""

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DOCS_DIR = DATA_DIR / "documents"
NCTD_DOCS_DIR = DATA_DIR / "nctd" / "documents"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
STRUCTURED_DIR = DATA_DIR / "structured"
MEETINGS_DIR = DATA_DIR / "meetings"
NCTD_MEETINGS_DIR = DATA_DIR / "nctd" / "meetings"

EXTRACTION_PROMPT = """Extract structured data from this raw local government document. Return ONLY valid JSON, no markdown fencing, no explanation.

Schema:
{
  "meeting_id": "string — from filename or metadata",
  "date": "YYYY-MM-DD",
  "body": "City Council | Planning Commission | NCTD Board | etc",
  "agency": "City of Oceanside | NCTD",
  "doc_type": "agenda | minutes | staff_report | transcript | agenda_packet",
  "advocacy_score": "green | yellow | red | neutral",
  "advocacy_reason": "one sentence justification",
  "votes": [
    {"item": "description", "result": "approved 4-1 | denied | tabled | continued", "yes": ["names"], "no": ["names"], "abstain": ["names"]}
  ],
  "housing_items": [
    {"type": "zoning | density | permit | affordable | adu | transit_oriented | state_compliance", "description": "...", "address": "if mentioned", "units": null_or_number, "outcome": "approved | denied | continued | discussed", "state_law_flags": ["HAA | SB330 | SB79 | SB35 | density_bonus | housing_element"]}
  ],
  "fiscal_items": [
    {"description": "...", "amount": null_or_number, "type": "infrastructure | bond | contract | grant | fee"}
  ],
  "legal_flags": ["string — any potential state law violation, enforcement action, or litigation risk"],
  "council_positions": [
    {"member": "name", "stance": "pro_housing | anti_housing | mixed | procedural", "evidence": "one sentence"}
  ],
  "public_comments": ["notable comments, especially on housing/development"],
  "key_quotes": ["direct quotes that reveal positions or are legally significant"],
  "procedural_only": false
}

Rules:
- If the document is purely procedural (roll call, adjournment, consent calendar with nothing notable), set procedural_only: true and leave arrays empty.
- Only include names that appear BY NAME in the document text. Never invent or guess names.
- For advocacy_score: green = pro-housing/productive, yellow = mixed, red = anti-housing/obstructive, neutral = no housing relevance.
- Empty arrays are fine — don't pad with empty objects.
- Dates must be YYYY-MM-DD format.
- Extract ALL votes, housing items, and dollar amounts — do not summarize or omit.
- Return ONLY the JSON object. No other text.

Document text:
"""


def get_meeting_meta(doc_path):
    """Try to find meeting metadata for a document."""
    stem = doc_path.stem
    mid = stem.split("-")[0]

    for meetings_dir in [MEETINGS_DIR, NCTD_MEETINGS_DIR]:
        meta_file = meetings_dir / mid / "meeting.json"
        if meta_file.exists():
            return json.loads(meta_file.read_text())

    return {}


def extract_structured(text, meeting_meta):
    """Extract structured JSON from raw document text using claude -p."""
    meta_context = ""
    if meeting_meta:
        meta_context = f"\nMeeting metadata: {json.dumps(meeting_meta, default=str)}\n"

    # Truncate very large docs
    prompt = EXTRACTION_PROMPT + meta_context + text[:80000]

    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=prompt,
            capture_output=True, text=True, timeout=300,
        )
        if result.returncode != 0:
            if result.stderr:
                print(f"  claude -p error: {result.stderr[:200]}")
            return None

        output = result.stdout.strip()
        if output.startswith("```"):
            output = output.split("\n", 1)[1] if "\n" in output else output
            if output.endswith("```"):
                output = output[:-3].strip()

        record = json.loads(output)
        return record
    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        print(f"  Raw output: {result.stdout[:300]}")
        return None
    except FileNotFoundError:
        print("  claude CLI not found.")
        return None
    except subprocess.TimeoutExpired:
        print("  claude -p timed out (300s)")
        return None


def rebuild_combined(structured_dir):
    """Rebuild the combined JSONL file from individual records."""
    jsonl_path = structured_dir / "all-records.jsonl"
    records = []

    for json_file in sorted(structured_dir.glob("*.json")):
        if json_file.name == "extraction-state.json":
            continue
        try:
            record = json.loads(json_file.read_text())
            records.append(record)
        except (json.JSONDecodeError, Exception):
            continue

    records.sort(key=lambda r: r.get("date", ""))
    with open(jsonl_path, "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")

    print(f"Combined JSONL: {len(records)} records → {jsonl_path}")
    return len(records)


def collect_all_sources():
    """Collect all raw source files: documents + transcripts."""
    sources = []

    # Raw document text (PDFs → pdftotext) — newest first so recent meetings get priority
    for ddir in [DOCS_DIR, NCTD_DOCS_DIR]:
        if ddir.exists():
            for f in sorted(ddir.glob("*.txt"), reverse=True):
                sources.append(("doc", f))

    # Transcripts (audio → whisper)
    if TRANSCRIPTS_DIR.exists():
        for f in sorted(TRANSCRIPTS_DIR.glob("*-transcript.txt"), reverse=True):
            sources.append(("transcript", f))

    return sources


def needs_reextraction(source_path, out_path):
    """Check if a source file is newer than its existing JSONL record."""
    if not out_path.exists():
        return True
    return source_path.stat().st_mtime > out_path.stat().st_mtime


def cmd_extract(args):
    """Extract structured records from all raw sources (documents + transcripts)."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    all_sources = collect_all_sources()

    if not all_sources:
        print("No source files found. Run fetch first.")
        return

    to_process = []
    for source_type, sf in all_sources:
        stem = sf.stem.replace("-transcript", "") if source_type == "transcript" else sf.stem
        suffix = "-transcript" if source_type == "transcript" else ""
        out_path = STRUCTURED_DIR / f"{stem}{suffix}.json"

        if args.force:
            to_process.append((source_type, sf, out_path))
        elif not out_path.exists():
            to_process.append((source_type, sf, out_path))
        elif needs_reextraction(sf, out_path):
            to_process.append((source_type, sf, out_path))

    if args.dry_run:
        already = len(all_sources) - len(to_process)
        print(f"{len(to_process)} sources to process ({len(all_sources)} total, {already} already extracted)")
        docs = sum(1 for t, _, _ in to_process if t == "doc")
        transcripts = sum(1 for t, _, _ in to_process if t == "transcript")
        print(f"  {docs} documents, {transcripts} transcripts")
        for _, sf, _ in to_process[:10]:
            print(f"  {sf.name}")
        if len(to_process) > 10:
            print(f"  ... and {len(to_process) - 10} more")
        return

    stop_hour = getattr(args, "stop_at", None)
    print(f"Extracting structured data: {len(to_process)} sources to process")
    if stop_hour:
        print(f"  Will stop at {stop_hour}:00")
    success = 0
    failed = 0
    stopped_early = False

    for i, (source_type, sf, out_path) in enumerate(to_process):
        if stop_hour and datetime.now().hour >= stop_hour:
            remaining = len(to_process) - i
            print(f"\nStopping at {datetime.now().strftime('%H:%M')} ({remaining} remaining, will resume next run)")
            stopped_early = True
            break

        label = f"[{source_type}]" if source_type == "transcript" else ""
        print(f"[{i+1}/{len(to_process)}] {sf.name} {label}")

        text = sf.read_text()
        if len(text.strip()) < 100:
            print(f"  Skipping: too short ({len(text.strip())} chars)")
            continue

        meta = get_meeting_meta(sf)
        record = extract_structured(text, meta)

        if record:
            if not record.get("meeting_id"):
                mid = sf.stem.replace("-transcript", "").split("-")[0]
                record["meeting_id"] = mid
            record["_source"] = sf.name
            record["_source_type"] = source_type

            out_path.write_text(json.dumps(record, indent=2, default=str))
            success += 1
            print(f"  OK ({record.get('advocacy_score', '?')}, {len(text)} chars)")
        else:
            failed += 1
            print(f"  FAILED")

        time.sleep(1)

    print(f"\nDone. {success} extracted, {failed} failed.")

    if success > 0:
        rebuild_combined(STRUCTURED_DIR)


def cmd_stats(args):
    """Show extraction statistics."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    json_files = list(STRUCTURED_DIR.glob("*.json"))
    json_files = [f for f in json_files if f.name != "extraction-state.json"]

    if not json_files:
        print("No structured records found. Run extract first.")
        return

    total_docs = len(list(DOCS_DIR.glob("*.txt")))
    if NCTD_DOCS_DIR.exists():
        total_docs += len(list(NCTD_DOCS_DIR.glob("*.txt")))
    total_transcripts = len(list(TRANSCRIPTS_DIR.glob("*-transcript.txt"))) if TRANSCRIPTS_DIR.exists() else 0
    total_sources = total_docs + total_transcripts

    scores = {"green": 0, "yellow": 0, "red": 0, "neutral": 0}
    total_votes = 0
    total_housing = 0
    total_legal = 0
    total_procedural = 0

    for jf in json_files:
        try:
            r = json.loads(jf.read_text())
            score = r.get("advocacy_score", "neutral")
            scores[score] = scores.get(score, 0) + 1
            total_votes += len(r.get("votes", []))
            total_housing += len(r.get("housing_items", []))
            total_legal += len(r.get("legal_flags", []))
            if r.get("procedural_only"):
                total_procedural += 1
        except Exception:
            continue

    print(f"Structured records: {len(json_files)} / {total_sources} sources ({total_docs} docs, {total_transcripts} transcripts)")
    print(f"\nAdvocacy scores:")
    for score, count in sorted(scores.items(), key=lambda x: -x[1]):
        bar = "█" * (count // 5) if count > 0 else ""
        print(f"  {score:8s}: {count:4d} {bar}")
    print(f"\nTotals:")
    print(f"  Votes recorded:    {total_votes}")
    print(f"  Housing items:     {total_housing}")
    print(f"  Legal flags:       {total_legal}")
    print(f"  Procedural-only:   {total_procedural}")


def main():
    parser = argparse.ArgumentParser(description="Extract structured JSONL from raw documents + transcripts")
    parser.add_argument("--force", action="store_true", help="Re-extract already processed sources")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--stats", action="store_true", help="Show extraction statistics")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild combined JSONL from existing records")
    parser.add_argument("--stop-at", type=int, metavar="HOUR", help="Stop extraction at this hour (0-23). Resumes next run.")

    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    elif args.rebuild:
        STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)
        rebuild_combined(STRUCTURED_DIR)
    else:
        cmd_extract(args)


if __name__ == "__main__":
    main()
