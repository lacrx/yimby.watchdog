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
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from civic_utils import all_docs_dirs, all_meetings_dirs, load_json, agency_data_dir, agency_docs_dir, load_agencies
from transforms.triage import predict_relevance
import config

DATA_DIR = REPO_ROOT / "data"
TRANSCRIPTS_DIR = DATA_DIR / "transcripts"
STRUCTURED_DIR = DATA_DIR / "structured"

_state_law_flags = config.get("extraction/state_law_flags",
                              ["HAA", "SB330", "SB79", "SB35", "density_bonus", "housing_element"])
_state_law_flags_str = " | ".join(_state_law_flags)

EXTRACTION_PROMPT = f"""Extract structured data from this raw local government document. Return ONLY valid JSON, no markdown fencing, no explanation.

Schema:
{{
  "meeting_id": "string — from filename or metadata",
  "date": "YYYY-MM-DD",
  "body": "string — the legislative body name from the document",
  "agency": "string — the agency or jurisdiction name from the document",
  "doc_type": "agenda | minutes | staff_report | transcript | agenda_packet",
  "votes": [
    {{"item": "description", "result": "approved 4-1 | denied | tabled | continued", "yes": ["names"], "no": ["names"], "abstain": ["names"]}}
  ],
  "housing_items": [
    {{"type": "zoning | density | permit | affordable | adu | transit_oriented | state_compliance", "description": "...", "address": "if mentioned", "units": null_or_number, "outcome": "approved | denied | continued | discussed", "state_law_flags": ["{_state_law_flags_str}"]}}
  ],
  "fiscal_items": [
    {{"description": "...", "amount": null_or_number, "type": "infrastructure | bond | contract | grant | fee"}}
  ],
  "legal_flags": ["string — any potential state law violation, enforcement action, or litigation risk"],
  "council_positions": [
    {{"member": "name", "action": "voted yes | voted no | abstained | moved | seconded | spoke for | spoke against | amended", "on": "item description", "evidence": "verbatim quote or factual description of what they did"}}
  ],
  "public_comments": ["public comments mentioning specific agenda items, policies, or legal standards"],
  "key_quotes": ["direct quotes from officials or public — verbatim text only"],
  "procedural_only": false
}}

Rules:
- If the document is purely procedural (roll call, adjournment, consent calendar with nothing notable), set procedural_only: true and leave arrays empty.
- Only include names that appear BY NAME in the document text. Never invent or guess names.
- For council_positions: record what each named member DID (motion, vote, statement, question), not your assessment of their political orientation. Use exact words in evidence when available.
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

    for meetings_dir in all_meetings_dirs():
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
                if isinstance(record, list):
                    record = record[0] if len(record) == 1 else {"_chunks": record}
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

    records = [r for r in records if isinstance(r, dict)]
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


CHARS_PER_TOKEN = 4
OPUS_INPUT_PER_MTOK = 15.0
OPUS_OUTPUT_PER_MTOK = 75.0
PROMPT_OVERHEAD_TOKENS = 800
OUTPUT_TOKENS_EST = 1500
TALLY_PATH = DATA_DIR / "pipeline" / "extraction-tally.jsonl"


def cmd_tally(args):
    """Log what incoming docs would cost at API rates, without extracting."""
    from transforms.triage import predict_relevance
    from collections import defaultdict

    all_sources = collect_all_sources()
    if not all_sources:
        print("No sources found.")
        return

    agency_tally = defaultdict(lambda: {
        "docs": 0, "need_llm": 0, "triage_skip": 0, "skip_marker": 0,
        "skip_short": 0, "input_tokens": 0, "output_tokens_est": 0, "chunks": 0,
    })

    for source_type, sf in all_sources:
        if source_type == "transcript":
            agency = "transcripts"
        elif sf.parent.name == "documents":
            agency = sf.parent.parent.name
        else:
            agency = sf.parent.name

        stem = sf.stem.replace("-transcript", "") if source_type == "transcript" else sf.stem
        suffix = "-transcript" if source_type == "transcript" else ""
        out_path = STRUCTURED_DIR / f"{stem}{suffix}.json"

        if has_skip_marker(sf):
            agency_tally[agency]["skip_marker"] += 1
            continue

        if out_path.exists():
            try:
                data = json.loads(out_path.read_text())
                if isinstance(data, dict):
                    continue
            except (json.JSONDecodeError, OSError):
                pass

        agency_tally[agency]["docs"] += 1

        try:
            text = sf.read_text()
        except OSError:
            agency_tally[agency]["skip_short"] += 1
            continue

        if is_skippable(text):
            agency_tally[agency]["skip_short"] += 1
            continue

        extract, _ = predict_relevance(text, sf.name)
        if not extract:
            agency_tally[agency]["triage_skip"] += 1
            continue

        text_len = len(text)
        if text_len <= CHUNK_SIZE:
            n_chunks = 1
        else:
            n_chunks = (text_len // (CHUNK_SIZE - CHUNK_OVERLAP)) + 1

        input_tokens = (text_len // CHARS_PER_TOKEN) + (PROMPT_OVERHEAD_TOKENS * n_chunks)
        output_tokens = OUTPUT_TOKENS_EST * n_chunks

        agency_tally[agency]["need_llm"] += 1
        agency_tally[agency]["input_tokens"] += input_tokens
        agency_tally[agency]["output_tokens_est"] += output_tokens
        agency_tally[agency]["chunks"] += n_chunks

    totals = {"docs": 0, "need_llm": 0, "triage_skip": 0, "input_tokens": 0,
              "output_tokens_est": 0, "chunks": 0}
    incoming = {}
    for agency, t in sorted(agency_tally.items()):
        if t["docs"] == 0 and t["skip_marker"] == 0:
            continue
        incoming[agency] = t
        for k in totals:
            totals[k] += t.get(k, 0)

    input_cost = (totals["input_tokens"] / 1_000_000) * OPUS_INPUT_PER_MTOK
    output_cost = (totals["output_tokens_est"] / 1_000_000) * OPUS_OUTPUT_PER_MTOK
    est_cost = round(input_cost + output_cost, 4)

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "incoming": incoming,
        "totals": {**totals, "est_cost_usd": est_cost},
    }

    TALLY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TALLY_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

    print(f"=== Extraction Cost Tally — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print()
    print(f"{'Agency':<15} {'New':>5} {'LLM':>5} {'Skip':>5} {'Tokens':>10} {'Chunks':>7}")
    print("-" * 52)
    for agency in sorted(incoming):
        t = incoming[agency]
        print(f"{agency:<15} {t['docs']:>5} {t['need_llm']:>5} {t['triage_skip']:>5} {t['input_tokens']:>10} {t['chunks']:>7}")
    print("-" * 52)
    print(f"{'TOTAL':<15} {totals['docs']:>5} {totals['need_llm']:>5} {totals['triage_skip']:>5} {totals['input_tokens']:>10} {totals['chunks']:>7}")
    print(f"\nEstimated API cost (Opus 4.8): ${est_cost:.4f}")
    print(f"Logged to {TALLY_PATH}")


DISCOVERY_LOG = DATA_DIR / "pipeline" / "discovery.jsonl"


def cmd_cost_report(args):
    """Project monthly API cost from discovery rate + triage pass rate."""
    from collections import defaultdict
    from datetime import timedelta

    now = datetime.now()
    lookback = timedelta(days=30)
    cutoff = (now - lookback).isoformat()

    # Load discovery history (publication rate)
    discovery_by_agency = defaultdict(list)
    if DISCOVERY_LOG.exists():
        for line in open(DISCOVERY_LOG):
            try:
                entry = json.loads(line.strip())
                if entry.get("ts", "") >= cutoff:
                    discovery_by_agency[entry["agency"]].append(entry)
            except (json.JSONDecodeError, KeyError):
                continue

    # Load tally history (triage + token data)
    tally_entries = []
    if TALLY_PATH.exists():
        for line in open(TALLY_PATH):
            try:
                entry = json.loads(line.strip())
                if entry.get("ts", "") >= cutoff:
                    tally_entries.append(entry)
            except (json.JSONDecodeError, KeyError):
                continue

    # Compute per-agency stats
    print(f"=== Cost Projection Report — {now.strftime('%Y-%m-%d')} ===")
    print(f"    (Based on last 30 days of data)")
    print()

    # From discovery: publication rate
    pub_rates = {}
    for agency, entries in sorted(discovery_by_agency.items()):
        total_new = sum(e.get("meetings_new", 0) for e in entries)
        total_docs = sum(e.get("docs_new", 0) for e in entries)
        n_runs = len(entries)
        days_spanned = max(1, (now - datetime.fromisoformat(entries[0]["ts"])).days)
        pub_rates[agency] = {
            "meetings_new_total": total_new,
            "docs_new_total": total_docs,
            "runs": n_runs,
            "days": days_spanned,
            "meetings_per_week": round(total_new / days_spanned * 7, 1),
            "docs_per_week": round(total_docs / days_spanned * 7, 1) if total_docs else None,
        }

    # From tally: triage pass rate + avg tokens per doc
    triage_stats = {}
    if tally_entries:
        latest = tally_entries[-1]
        for agency, data in latest.get("incoming", {}).items():
            docs = data.get("docs", 0) + data.get("skip_marker", 0)
            need_llm = data.get("need_llm", 0)
            triage_skip = data.get("triage_skip", 0)
            tokens = data.get("input_tokens", 0)
            triage_stats[agency] = {
                "total_pending": docs,
                "need_llm": need_llm,
                "triage_skip": triage_skip,
                "triage_pass_rate": round(need_llm / max(1, need_llm + triage_skip), 2),
                "avg_input_tokens": round(tokens / max(1, need_llm)),
            }

    # Combine into projection
    all_agencies = sorted(set(list(pub_rates.keys()) + list(triage_stats.keys())))

    print(f"{'Agency':<16} {'New/wk':>7} {'Triage%':>8} {'Tok/doc':>8} {'$/month':>9}")
    print("-" * 52)

    total_monthly = 0
    for agency in all_agencies:
        pr = pub_rates.get(agency, {})
        ts = triage_stats.get(agency, {})

        docs_per_week = pr.get("docs_per_week") or pr.get("meetings_per_week", 0) * 3
        triage_rate = ts.get("triage_pass_rate", 0.9)
        avg_tokens = ts.get("avg_input_tokens", 15000)

        llm_docs_per_month = docs_per_week * 4.33 * triage_rate
        input_cost = (llm_docs_per_month * avg_tokens / 1_000_000) * OPUS_INPUT_PER_MTOK
        output_cost = (llm_docs_per_month * OUTPUT_TOKENS_EST / 1_000_000) * OPUS_OUTPUT_PER_MTOK
        monthly_cost = input_cost + output_cost
        total_monthly += monthly_cost

        print(f"{agency:<16} {docs_per_week:>7.1f} {triage_rate*100:>7.0f}% {avg_tokens:>8,} ${monthly_cost:>8.2f}")

    print("-" * 52)
    print(f"{'TOTAL':<16} {'':>7} {'':>8} {'':>8} ${total_monthly:>8.2f}")
    print()
    print(f"  Annualized: ${total_monthly * 12:,.0f}/yr")
    print()

    if not discovery_by_agency:
        print("  NOTE: No discovery data yet. Run scrapers to start collecting publication rates.")
        print("        Projection above uses tally backlog data only (not ongoing rate).")

    # Log report
    report = {
        "ts": now.isoformat(timespec="seconds"),
        "pub_rates": pub_rates,
        "triage_stats": triage_stats,
        "projected_monthly_usd": round(total_monthly, 2),
        "projected_annual_usd": round(total_monthly * 12, 2),
    }
    report_path = DATA_DIR / "pipeline" / "cost-report.jsonl"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "a") as f:
        f.write(json.dumps(report, default=str) + "\n")
    print(f"  Logged to {report_path}")


def rebuild_combined(structured_dir):
    """Rebuild the combined JSONL file from individual records."""
    jsonl_path = structured_dir / "all-records.jsonl"
    records = []

    skip_files = {"extraction-state.json", "meetings-state.json", "document-dates.json"}
    for json_file in sorted(structured_dir.glob("*.json")):
        if json_file.name in skip_files:
            continue
        try:
            record = json.loads(json_file.read_text())
            if not isinstance(record, dict) or "votes" not in record:
                continue
            records.append(record)
        except (json.JSONDecodeError, Exception):
            continue

    records.sort(key=lambda r: r.get("date") or "")
    with open(jsonl_path, "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")

    print(f"Combined JSONL: {len(records)} records → {jsonl_path}")
    return len(records)


def load_doc_index():
    """Load doc-index.json from all enabled agencies. Returns {filename: meeting_date}."""
    index = {}
    for slug in load_agencies(enabled_only=True):
        idx_path = agency_data_dir(slug) / "doc-index.json"
        if not idx_path.exists():
            continue
        try:
            data = json.loads(idx_path.read_text())
            for fname, info in data.get("documents", {}).items():
                index[fname] = info.get("meeting_date", "")
        except (json.JSONDecodeError, OSError):
            continue
    return index


def collect_all_sources(queue=None, hot_days=14):
    """Collect raw source files, optionally filtered by queue.

    queue: None (all), "hot" (recent meetings), or "cold" (backlog).
    hot_days: days back from today that counts as "hot" (default 14).

    forward_only agencies use enabled_date as cutoff instead of hot_days,
    and are excluded from cold queue entirely.
    """
    sources = []
    agencies = load_agencies(enabled_only=True)
    doc_index = load_doc_index() if queue else {}
    hot_cutoff = (datetime.now().date() - timedelta(days=hot_days)).isoformat()

    for slug, cfg in agencies.items():
        ddir = agency_docs_dir(slug)
        if not ddir.exists():
            continue

        is_forward_only = cfg.get("forward_only", False)
        is_tally_only = cfg.get("tally_only", False)
        enabled_date = cfg.get("enabled_date", "")

        if is_tally_only and queue:
            continue
        if queue == "cold" and is_forward_only:
            continue

        for f in sorted(ddir.glob("*.txt"), reverse=True):
            if queue:
                meeting_date = doc_index.get(f.name, "")

                if is_forward_only:
                    if not meeting_date or meeting_date < enabled_date:
                        continue
                else:
                    is_hot = meeting_date >= hot_cutoff if meeting_date else False
                    if queue == "hot" and not is_hot:
                        continue
                    if queue == "cold" and is_hot:
                        continue

            sources.append(("doc", f))

    # Transcripts are always hot — expensive to produce, extract promptly
    if queue != "cold":
        if TRANSCRIPTS_DIR.exists():
            for f in sorted(TRANSCRIPTS_DIR.glob("*-transcript.txt"), reverse=True):
                sources.append(("transcript", f))

    # Cold queue: oldest first to work through backlog systematically
    if queue == "cold":
        sources.reverse()

    return sources


def needs_reextraction(source_path, out_path):
    """Check if an existing extraction is empty/corrupt and needs redo.

    Mtime comparison removed — S3 sync resets source mtimes, causing
    thousands of phantom re-extractions. Use --force for intentional
    full re-extraction.
    """
    if not out_path.exists():
        return True
    try:
        data = json.loads(out_path.read_text())
        return not isinstance(data, dict)
    except (json.JSONDecodeError, OSError):
        return True


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
    """Check if source has been permanently marked as skip or split."""
    if source_path.with_suffix(source_path.suffix + SKIP_MARKER).exists():
        return True
    if source_path.with_suffix(source_path.suffix + ".split").exists():
        return True
    return False


def cmd_extract(args):
    """Extract structured records from all raw sources (documents + transcripts)."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    queue = getattr(args, "queue", None)
    hot_days = getattr(args, "hot_days", 14)

    all_sources = collect_all_sources(queue=queue, hot_days=hot_days)

    if not all_sources:
        queue_label = f" ({queue})" if queue else ""
        print(f"No source files found{queue_label}. Run fetch first.")
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
        queue_label = f" [{queue.upper()}]" if queue else ""
        print(f"{len(to_process)} sources to process{queue_label} ({len(all_sources)} total, {already} already extracted)")

        if not queue:
            doc_index = load_doc_index()
            hot_cutoff = (datetime.now().date() - timedelta(days=hot_days)).isoformat()
            hot_count = cold_count = unindexed = 0
            for _, sf, _ in to_process:
                meeting_date = doc_index.get(sf.name, "")
                if not meeting_date:
                    unindexed += 1
                elif meeting_date >= hot_cutoff:
                    hot_count += 1
                else:
                    cold_count += 1
            print(f"  HOT (last {hot_days}d): {hot_count}")
            print(f"  COLD (backlog):   {cold_count}")
            if unindexed:
                print(f"  Unindexed:        {unindexed}")

        docs = sum(1 for t, _, _ in to_process if t == "doc")
        transcripts = sum(1 for t, _, _ in to_process if t == "transcript")
        print(f"  {docs} documents, {transcripts} transcripts")
        for _, sf, _ in to_process[:10]:
            print(f"  {sf.name}")
        if len(to_process) > 10:
            print(f"  ... and {len(to_process) - 10} more")
        return

    use_triage = not getattr(args, "no_triage", False)
    if use_triage:
        print("  Triage enabled (rule-based substantive filter)")

    stop_hour = getattr(args, "stop_at", None)
    print(f"Extracting structured data: {len(to_process)} sources to process")
    if stop_hour:
        print(f"  Will stop at {stop_hour}:00")
    success = 0
    failed = 0
    skipped = 0
    triage_skipped = 0
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
        if getattr(args, "limit", None) and success >= args.limit:
            remaining = len(to_process) - i
            print(f"\nLimit reached ({args.limit} extractions). {remaining} remaining, will resume next run.")
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

        if use_triage:
            extract, prob = predict_relevance(text, sf.name)
            if not extract:
                triage_skipped += 1
                print(f"  Triage skip (p={prob:.3f})")
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
            print(f"  OK ({len(text)} chars, {len(record.get('votes',[]))}v {len(record.get('housing_items',[]))}h)")
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

    triage_msg = f", {triage_skipped} triage-skipped" if triage_skipped else ""
    print(f"\nDone. {success} extracted, {failed} failed, {skipped} permanently skipped{triage_msg}.")

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

    total_docs = sum(len(list(d.glob("*.txt"))) for d in all_docs_dirs())
    total_transcripts = len(list(TRANSCRIPTS_DIR.glob("*-transcript.txt"))) if TRANSCRIPTS_DIR.exists() else 0
    total_sources = total_docs + total_transcripts

    total_votes = 0
    total_housing = 0
    total_legal = 0
    total_procedural = 0
    total_with_positions = 0

    for jf in json_files:
        try:
            r = json.loads(jf.read_text())
            total_votes += len(r.get("votes", []))
            total_housing += len(r.get("housing_items", []))
            total_legal += len(r.get("legal_flags", []))
            if r.get("procedural_only"):
                total_procedural += 1
            if r.get("council_positions"):
                total_with_positions += 1
        except Exception:
            continue

    print(f"Structured records: {len(json_files)} / {total_sources} sources ({total_docs} docs, {total_transcripts} transcripts)")
    print(f"\nTotals:")
    print(f"  Votes recorded:      {total_votes}")
    print(f"  Housing items:       {total_housing}")
    print(f"  Legal flags:         {total_legal}")
    print(f"  Council positions:   {total_with_positions}")
    print(f"  Procedural-only:     {total_procedural}")


def main():
    parser = argparse.ArgumentParser(description="Extract structured JSONL from raw documents + transcripts")
    parser.add_argument("--force", action="store_true", help="Re-extract already processed sources")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be processed")
    parser.add_argument("--stats", action="store_true", help="Show extraction statistics")
    parser.add_argument("--rebuild", action="store_true", help="Rebuild combined JSONL from existing records")
    parser.add_argument("--stop-at", type=int, metavar="HOUR", help="Stop extraction at this hour (0-23). Resumes next run.")
    parser.add_argument("--meeting", action="append", metavar="ID", help="Only extract sources for these meeting IDs (repeatable)")
    parser.add_argument("--no-triage", action="store_true", help="Disable ML triage — extract all documents")
    parser.add_argument("--tally", action="store_true", help="Log incoming doc counts and estimated API cost without extracting")
    parser.add_argument("--limit", type=int, metavar="N", help="Stop after N successful extractions")
    parser.add_argument("--queue", choices=["hot", "cold"], help="hot=recent meetings only, cold=backlog only")
    parser.add_argument("--hot-days", type=int, default=14, help="Days back that counts as 'hot' (default: 14)")
    parser.add_argument("--cost-report", action="store_true", help="Project monthly API cost from discovery + tally history")

    args = parser.parse_args()

    if args.cost_report:
        cmd_cost_report(args)
    elif args.tally:
        cmd_tally(args)
    elif args.stats:
        cmd_stats(args)
    elif args.rebuild:
        STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)
        rebuild_combined(STRUCTURED_DIR)
    else:
        cmd_extract(args)


if __name__ == "__main__":
    main()
