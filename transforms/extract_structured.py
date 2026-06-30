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
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
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

SKIP_MARKER = ".skip"  # written next to source file to permanently skip un-extractable docs
CHUNK_SIZE = 60000     # chars per chunk (leaves room for prompt + schema in context)
CHUNK_OVERLAP = 2000   # overlap between chunks to avoid splitting mid-sentence


def get_meeting_meta(doc_path):
    """Try to find meeting metadata for a document."""
    stem = doc_path.stem
    mid = stem.split("-")[0]

    for meetings_dir in [MEETINGS_DIR, NCTD_MEETINGS_DIR]:
        meta_file = meetings_dir / mid / "meeting.json"
        if meta_file.exists():
            return json.loads(meta_file.read_text())

    return {}


class RateLimitHit(Exception):
    """Raised when claude -p hits session limit — caller should stop the run."""
    pass


class AuthError(Exception):
    """Raised when claude -p fails authentication — no point retrying."""
    pass


def _call_claude(prompt, max_retries=2):
    """Call claude -p with retries. Returns parsed JSON or None.

    Raises RateLimitHit on session/rate limits and AuthError on 401/403.
    Uses subscription auth only — ANTHROPIC_API_KEY is stripped to avoid
    burning API credits on batch extraction.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                ["claude", "-p", "--output-format", "text"],
                input=prompt,
                capture_output=True, text=True, timeout=300,
                env=env,
            )

            stderr_lower = result.stderr.lower()
            if "session limit" in stderr_lower or "rate limit" in stderr_lower:
                raise RateLimitHit(result.stderr.strip()[:200])

            if "invalid authentication" in stderr_lower or "401" in stderr_lower or "403" in stderr_lower:
                raise AuthError(result.stderr.strip()[:200] or result.stdout.strip()[:200])

            if result.returncode != 0:
                if attempt < max_retries:
                    print(f"  claude -p exit code {result.returncode}, retrying ({attempt+1}/{max_retries})...")
                    time.sleep(5 * (attempt + 1))
                    continue
                print(f"  claude -p exit code {result.returncode}")
                if result.stderr:
                    print(f"  stderr: {result.stderr[:300]}")
                if result.stdout:
                    print(f"  stdout: {result.stdout[:300]}")
                return None

            output = result.stdout.strip()
            if not output:
                if attempt < max_retries:
                    print(f"  empty output, retrying ({attempt+1}/{max_retries})...")
                    time.sleep(5)
                    continue
                print(f"  claude -p returned empty output")
                return None

            if output.startswith("```"):
                output = output.split("\n", 1)[1] if "\n" in output else output
                if output.endswith("```"):
                    output = output[:-3].strip()

            try:
                record = json.loads(output)
                return record
            except json.JSONDecodeError as e:
                if attempt < max_retries:
                    print(f"  JSON parse error, retrying ({attempt+1}/{max_retries})...")
                    time.sleep(5)
                    continue
                print(f"  JSON parse error: {e}")
                print(f"  Raw output ({len(result.stdout)} chars): {result.stdout[:500]}")
                return None

        except FileNotFoundError:
            print("  claude CLI not found.")
            return None
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                print(f"  timed out, retrying ({attempt+1}/{max_retries})...")
                time.sleep(10)
                continue
            print("  claude -p timed out (300s)")
            return None

    return None


def _merge_records(records):
    """Merge multiple chunk records into one, deduplicating arrays."""
    if not records:
        return None
    if len(records) == 1:
        return records[0]

    merged = records[0].copy()
    for r in records[1:]:
        for key in ["votes", "housing_items", "fiscal_items", "legal_flags",
                     "council_positions", "public_comments", "key_quotes"]:
            existing = merged.get(key, [])
            new_items = r.get(key, [])
            for item in new_items:
                item_str = json.dumps(item, sort_keys=True) if not isinstance(item, str) else item.lower().strip()
                is_dup = False
                for e in existing:
                    e_str = json.dumps(e, sort_keys=True) if not isinstance(e, str) else e.lower().strip()
                    if e_str == item_str:
                        is_dup = True
                        break
                if not is_dup:
                    existing.append(item)
            merged[key] = existing

        if r.get("advocacy_score") == "red":
            merged["advocacy_score"] = "red"
            merged["advocacy_reason"] = r.get("advocacy_reason", merged.get("advocacy_reason", ""))
        elif r.get("advocacy_score") == "yellow" and merged.get("advocacy_score") not in ("red",):
            merged["advocacy_score"] = "yellow"
        if not merged.get("date") and r.get("date"):
            merged["date"] = r["date"]
        if not merged.get("body") and r.get("body"):
            merged["body"] = r["body"]
        if r.get("procedural_only") is False:
            merged["procedural_only"] = False

    return merged


def extract_structured(text, meeting_meta):
    """Extract structured JSON from document text, chunking if large."""
    meta_context = ""
    if meeting_meta:
        meta_context = f"\nMeeting metadata: {json.dumps(meeting_meta, default=str)}\n"

    if len(text) <= CHUNK_SIZE:
        prompt = EXTRACTION_PROMPT + meta_context + text
        return _call_claude(prompt)

    chunks = []
    pos = 0
    while pos < len(text):
        end = pos + CHUNK_SIZE
        if end < len(text):
            newline = text.rfind("\n", pos + CHUNK_SIZE - CHUNK_OVERLAP, end)
            if newline > pos:
                end = newline + 1
        chunks.append(text[pos:end])
        pos = end - CHUNK_OVERLAP if end < len(text) else end

    print(f"  Chunking: {len(text):,} chars → {len(chunks)} chunks", flush=True)

    records = []
    for ci, chunk in enumerate(chunks):
        prompt = EXTRACTION_PROMPT + meta_context + "\n" + chunk
        record = _call_claude(prompt)
        if record:
            records.append(record)
        time.sleep(1)

    return _merge_records(records)


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


def is_skippable(text):
    """Check if text is un-extractable garbage from PDF rendering."""
    stripped = text.strip()
    if len(stripped) < 100:
        return "too_short"

    sample = stripped[:5000]
    alpha_ratio = sum(c.isalpha() or c.isspace() for c in sample) / max(len(sample), 1)
    if alpha_ratio < 0.2:
        return "garbled_pdf"

    lines = stripped.split("\n")
    non_empty = [l for l in lines[:200] if l.strip()]
    if non_empty:
        avg_len = sum(len(l.strip()) for l in non_empty) / len(non_empty)
        if avg_len < 3 and len(non_empty) > 50:
            return "garbled_pdf"

    lower = stripped[:2000].lower()
    skip_keywords = ["salary schedule", "revised architectural plans", "plan set\n"]
    for kw in skip_keywords:
        if kw in lower and len(stripped) > 200000:
            return "non_meeting_content"

    return None


def write_skip_marker(source_path, reason):
    """Write a .skip file so this source is never retried."""
    skip_path = source_path.with_suffix(source_path.suffix + SKIP_MARKER)
    skip_path.write_text(json.dumps({"reason": reason, "skipped_at": datetime.now().isoformat()}))


def has_skip_marker(source_path):
    """Check if source has been permanently marked as skip."""
    return source_path.with_suffix(source_path.suffix + SKIP_MARKER).exists()


def cmd_extract(args):
    """Extract structured records from all raw sources (documents + transcripts)."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    all_sources = collect_all_sources()

    if not all_sources:
        print("No source files found. Run fetch first.")
        return

    meeting_filter = set(args.meeting) if args.meeting else None

    to_process = []
    skipped_markers = 0
    for source_type, sf in all_sources:
        if meeting_filter and not any(sf.stem.startswith(mid) for mid in meeting_filter):
            continue

        if has_skip_marker(sf):
            skipped_markers += 1
            continue

        stem = sf.stem.replace("-transcript", "") if source_type == "transcript" else sf.stem
        suffix = "-transcript" if source_type == "transcript" else ""
        out_path = STRUCTURED_DIR / f"{stem}{suffix}.json"

        if args.force:
            to_process.append((source_type, sf, out_path))
        elif not out_path.exists():
            to_process.append((source_type, sf, out_path))
        elif needs_reextraction(sf, out_path):
            to_process.append((source_type, sf, out_path))

    if skipped_markers:
        print(f"  {skipped_markers} sources permanently skipped (.skip marker)")

    if args.dry_run:
        already = len(all_sources) - len(to_process) - skipped_markers
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
    skipped = 0
    consecutive_failures = 0
    auth_failure_count = 0
    MAX_CONSECUTIVE_FAILURES = 5
    MAX_AUTH_RETRIES = 2

    i = 0
    while i < len(to_process):
        source_type, sf, out_path = to_process[i]
        if stop_hour and datetime.now().hour >= stop_hour:
            remaining = len(to_process) - i
            print(f"\nStopping at {datetime.now().strftime('%H:%M')} ({remaining} remaining, will resume next run)")
            break

        label = f"[{source_type}]" if source_type == "transcript" else ""
        print(f"[{i+1}/{len(to_process)}] {sf.name} {label}")

        text = sf.read_text()

        skip_reason = is_skippable(text)
        if skip_reason:
            write_skip_marker(sf, skip_reason)
            skipped += 1
            print(f"  Skipping permanently: {skip_reason} ({len(text.strip())} chars)")
            i += 1
            continue

        meta = get_meeting_meta(sf)
        try:
            record = extract_structured(text, meta)
        except AuthError as e:
            auth_failure_count += 1

            if auth_failure_count <= MAX_AUTH_RETRIES:
                wait = 60 * auth_failure_count
                print(f"\n  Auth error ({auth_failure_count}/{MAX_AUTH_RETRIES}): {str(e)[:100]}")
                print(f"  Waiting {wait}s then retrying...", flush=True)
                time.sleep(wait)
                continue  # retry same file (i not incremented)

            remaining = len(to_process) - i
            print(f"\n  Auth error (persistent): {str(e)[:100]}")
            print(f"  Stopping — authentication broken after {auth_failure_count} attempts.")
            print(f"  Run `claude /login` to re-authenticate, then re-run extraction.")
            print(f"  ({success} extracted, {failed} failed, {skipped} skipped, {remaining} remaining)")
            break
        except RateLimitHit as e:
            remaining = len(to_process) - i
            msg = str(e).lower()
            # Parse reset time from "resets 6am" style messages
            reset_hour = None
            if "resets" in msg:
                import re
                m = re.search(r"resets\s+(\d+)(am|pm)", msg)
                if m:
                    h = int(m.group(1))
                    if m.group(2) == "pm" and h != 12:
                        h += 12
                    reset_hour = h

            now = datetime.now()
            if reset_hour is not None and stop_hour is not None:
                if reset_hour < stop_hour:
                    wait_minutes = (reset_hour * 60 - now.hour * 60 - now.minute)
                    if wait_minutes < 0:
                        wait_minutes += 24 * 60
                    if wait_minutes <= 120:
                        print(f"\n  Rate limit hit — resets at {reset_hour}:00 ({wait_minutes} min). Waiting...", flush=True)
                        time.sleep(wait_minutes * 60 + 30)
                        print(f"  Resuming after rate limit reset.", flush=True)
                        continue

            print(f"\n  Rate limit hit: {e}")
            print(f"  Stopping — {remaining} sources remaining, will resume next run.")
            print(f"  ({success} extracted, {failed} failed, {skipped} skipped this run)")
            break

        if record:
            if not record.get("meeting_id"):
                mid = sf.stem.replace("-transcript", "").split("-")[0]
                record["meeting_id"] = mid
            record["_source"] = sf.name
            record["_source_type"] = source_type

            out_path.write_text(json.dumps(record, indent=2, default=str))
            success += 1
            consecutive_failures = 0
            print(f"  OK ({record.get('advocacy_score', '?')}, {len(text)} chars)")
        else:
            failed += 1
            consecutive_failures += 1
            print(f"  FAILED")

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                remaining = len(to_process) - i - 1
                print(f"\n  Circuit breaker: {MAX_CONSECUTIVE_FAILURES} consecutive failures.")
                print(f"  Stopping — something is broken (auth, network, CLI).")
                print(f"  ({success} extracted, {failed} failed, {skipped} skipped, {remaining} remaining)")
                break

        auth_failure_count = 0  # reset on any successful claude call
        time.sleep(1)
        i += 1

    print(f"\nDone. {success} extracted, {failed} failed, {skipped} permanently skipped.")

    if success > 0:
        rebuild_combined(STRUCTURED_DIR)


def cmd_stats(args):
    """Show extraction statistics."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    json_files = list(STRUCTURED_DIR.glob("*.json"))
    json_files = [f for f in json_files if f.name not in ("extraction-state.json", "meetings-state.json")]

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
    parser.add_argument("--meeting", action="append", metavar="ID", help="Only extract sources for these meeting IDs (repeatable)")

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
