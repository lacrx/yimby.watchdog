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
import hashlib
import json
import os
import re
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

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "meeting_id": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
        "body": {"type": ["string", "null"]},
        "agency": {"type": ["string", "null"]},
        "doc_type": {"type": ["string", "null"], "enum": [
            "agenda", "minutes", "staff_report", "transcript", "agenda_packet", None
        ]},
        "votes": {"type": "array", "items": {"type": "object"}},
        "housing_items": {"type": "array", "items": {"type": "object"}},
        "fiscal_items": {"type": "array", "items": {"type": "object"}},
        "legal_flags": {"type": "array", "items": {"type": "string"}},
        "council_positions": {"type": "array", "items": {"type": "object"}},
        "public_comments": {"type": "array", "items": {"type": "string"}},
        "key_quotes": {"type": "array", "items": {"type": "string"}},
        "procedural_only": {"type": "boolean"},
    },
    "required": ["meeting_id", "date", "body", "agency", "doc_type",
                  "votes", "housing_items", "fiscal_items", "legal_flags",
                  "council_positions", "public_comments", "key_quotes",
                  "procedural_only"],
    "additionalProperties": True,
}

EXTRACTION_PROMPT = f"""Extract structured data from this raw local government document. Return ONLY valid JSON, no markdown fencing, no explanation.

Schema:
{{
  "meeting_id": "string — from filename or metadata",
  "date": "YYYY-MM-DD",
  "body": "string — the legislative body name from the document",
  "agency": "string — the agency or jurisdiction name from the document",
  "doc_type": "agenda | minutes | staff_report | transcript | agenda_packet",
  "votes": [
    {{"item_id": "agenda item number or short label (e.g. 'item-7', 'PH-1')", "item": "description", "result": "approved 4-1 | denied | tabled | continued", "yes": ["names"], "no": ["names"], "abstain": ["names"]}}
  ],
  "housing_items": [
    {{"item_id": "same label as matching vote if voted on", "type": "zoning | density | permit | affordable | adu | transit_oriented | state_compliance", "description": "...", "address": "if mentioned", "units": null_or_number, "outcome": "approved | denied | continued | discussed", "filing_numbers": ["D26-00001", "BLDG26-0001"], "applicant": "developer or applicant name if mentioned", "state_law_flags": ["{_state_law_flags_str}"]}}
  ],
  "fiscal_items": [
    {{"description": "...", "amount": null_or_number, "type": "infrastructure | bond | contract | grant | fee"}}
  ],
  "legal_flags": ["string — any potential state law violation, enforcement action, or litigation risk"],
  "council_positions": [
    {{"item_id": "same label as matching vote/housing_item", "member": "name", "action": "voted yes | voted no | abstained | moved | seconded | spoke for | spoke against | amended", "on": "item description", "evidence": "verbatim quote or factual description of what they did"}}
  ],
  "public_comments": ["public comments mentioning specific agenda items, policies, or legal standards"],
  "key_quotes": ["direct quotes from officials or public — verbatim text only"],
  "procedural_only": false
}}

Rules:
- If the document is purely procedural (roll call, adjournment, consent calendar with nothing notable), set procedural_only: true and leave arrays empty.
- Only include names that appear BY NAME in the document text. Never invent or guess names.
- For council_positions: record what each named member DID (motion, vote, statement, question), not your assessment of their political orientation. Use exact words in evidence when available.
- When a vote, housing_item, and/or council_position refer to the same agenda item, give them the same item_id value. Derive item_id from the agenda item number in the document (e.g. "item-7", "PH-1", "H-2").
- filing_numbers: extract planning case numbers (D26-00001, CUP24-00003, DB25-00012, ZA25-00001, etc.) and building permit numbers (BLDG26-0001) mentioned in each housing item. Omit the field if none are mentioned.
- applicant: extract the developer or applicant name if mentioned. Omit if not mentioned.
- Empty arrays are fine — don't pad with empty objects.
- Dates must be YYYY-MM-DD format.
- Extract ALL votes, housing items, and dollar amounts — do not summarize or omit.
- EXCLUDE state legislature activity: agenda packets often contain legislative tracking tables listing state bill status (AB/SB numbers with Assembly/Senate committee votes). These are NOT local board votes. Only record votes taken by the local body itself. Indicators of state-level activity to exclude: bill numbers (AB ###, SB ###) as the primary subject with no local action (adopt/oppose/support resolution), vote tallies exceeding the board's size, references to Assembly/Senate committees.
- Return ONLY the JSON object. No other text.

Document text:
"""

SKIP_MARKER = ".skip"  # written next to source file to permanently skip un-extractable docs
CHUNK_SIZE = 60000     # chars per chunk (leaves room for prompt + schema in context)
MAX_DOC_CHARS = 2_000_000  # 2M chars (~33 chunks) — bigger docs burn the whole session budget
CHUNK_OVERLAP = 2000   # overlap between chunks to avoid splitting mid-sentence
MAX_FAIL_RETRIES = 3   # auto-skip after this many extraction failures
FAILURE_STATE_FILE = None  # set in cmd_extract() after STRUCTURED_DIR is available


def resolve_meeting_id(stem):
    """Meeting ID for a document or transcript stem, or the stem's first dash-part.

    Hash-based IDs contain no dash, so splitting on the first one worked for
    them and silently truncated slug-based IDs to the agency prefix (every
    Carlsbad record used to be filed under `cb`). Match the longest known
    meeting ID the stem starts with instead.
    """
    stem = stem.replace("-transcript", "")
    best = None
    for mid in _meeting_dirs_by_id():
        if stem == mid or stem.startswith(mid + "-"):
            if best is None or len(mid) > len(best):
                best = mid
    return best or stem.split("-")[0]


_MEETING_DIR_CACHE = None


def _meeting_dirs_by_id():
    """Map meeting_id -> meeting directory, built once per run."""
    global _MEETING_DIR_CACHE
    if _MEETING_DIR_CACHE is None:
        index = {}
        for meetings_dir in all_meetings_dirs():
            if not meetings_dir.exists():
                continue
            for mdir in meetings_dir.iterdir():
                if mdir.is_dir():
                    index.setdefault(mdir.name, mdir)
        _MEETING_DIR_CACHE = index
    return _MEETING_DIR_CACHE


def get_meeting_meta(doc_path):
    """Try to find meeting metadata for a document.

    Document filenames are '{meeting_id}-{document name}'. Hash-based meeting
    IDs contain no dash, but slug-based ones do (cb-city-council-20261110), so
    splitting on the first dash yielded just the agency prefix and never
    matched a meeting directory. Match the longest meeting ID that the filename
    starts with instead.
    """
    index = _meeting_dirs_by_id()
    mid = resolve_meeting_id(doc_path.stem)
    mdir = index.get(mid)
    if mdir:
        meta_file = mdir / "meeting.json"
        if meta_file.exists():
            return json.loads(meta_file.read_text())
    return {}


class RateLimitHit(Exception):
    """Raised when claude -p hits session limit — caller should stop the run."""
    pass


class AuthError(Exception):
    """Raised when claude -p fails authentication — no point retrying."""
    pass


_REFUSAL_PHRASES = ("can't help with this", "cannot help with this",
                    "i can't assist", "i cannot assist")
# Classifier-level block, not a per-document refusal and not a limit:
#   "API Error: Opus 5's safeguards flagged this message
#    (https://www.anthropic.com/legal/aup)."
# It arrives on stdout with exit code 1, so without this it reads as a generic
# exit_code failure: retried twice per chunk, every chunk, every document, until
# the circuit breaker halts the run. Retrying never helps — the same bytes get
# flagged again — and the block is often session-wide rather than about the doc.
_SAFEGUARD_PHRASES = ("safeguards flagged", "/legal/aup")
# "You've hit your 5-hour limit · resets 5am (America/Los_Angeles)" is the
# phrasing the CLI actually used on 2026-11-13, and none of the older phrases
# matched it: it was read as a generic exit_code failure, retried twice per
# chunk, and counted toward the circuit breaker instead of pausing the run.
_RATE_LIMIT_PHRASES = ("session limit", "rate limit", "you've reached",
                       "you've hit your", "-hour limit", "usage limit",
                       "weekly limit", "limit · resets", "limit - resets",
                       "start a new session", "new session to continue")


def _extract_json(output):
    """Parse JSON from claude output, tolerating prose around one object. None if no JSON."""
    try:
        return json.loads(output)
    except (json.JSONDecodeError, ValueError):
        pass
    brace_start = output.find("{")
    brace_end = output.rfind("}")
    if brace_start >= 0 and brace_end > brace_start:
        try:
            return json.loads(output[brace_start:brace_end + 1])
        except (json.JSONDecodeError, ValueError):
            return None
    return None


def _normalize_record(raw):
    """Return a valid extraction record dict, or None.

    Valid = dict with a 'votes' list (rebuild_combined requires 'votes'). A list of
    several chunk dicts is merged; a one-element list is unwrapped.
    """
    if isinstance(raw, list):
        dicts = [r for r in raw if isinstance(r, dict)]
        if not dicts:
            return None
        raw = dicts[0] if len(dicts) == 1 else _merge_records(dicts)
    if not isinstance(raw, dict) or not isinstance(raw.get("votes"), list):
        return None
    return raw


def _call_claude(prompt, max_retries=2, schema=None):
    """Call claude -p with retries. Returns (record, error_category) tuple.

    On success: (dict, None). On failure: (None, "timeout"|"empty_output"|"json_parse"|"exit_code"|"exit_code_empty").
    Raises RateLimitHit on session/rate limits and AuthError on 401/403.
    Uses subscription auth only — ANTHROPIC_API_KEY is stripped to avoid
    burning API credits on batch extraction.
    """
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    cmd = ["claude", "-p", "--output-format", "text"]
    if schema:
        cmd += ["--json-schema", json.dumps(schema)]
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True, text=True, timeout=300,
                env=env,
            )

            clean_out = _ANSI_RE.sub('', result.stdout)
            clean_err = _ANSI_RE.sub('', result.stderr)
            combined_lower = (clean_err + " " + clean_out).lower().replace("’", "'")
            output = clean_out.strip()

            # Parse first. Refusal / rate-limit phrase checks only apply when the run
            # did not produce a usable record, so a valid JSON record that merely
            # mentions "rate limit" is never discarded.
            raw_json = _extract_json(output) if output else None
            record = _normalize_record(raw_json)
            clean_success = result.returncode == 0 and record is not None

            if not clean_success:
                stdout_is_json = raw_json is not None
                # Content refusal BEFORE rate limit: "can't help with this. Start a new session"
                # contains "new session" but is a per-doc refusal, not a session limit.
                # Confirmed only when stdout is not JSON at all.
                if not stdout_is_json and any(p in combined_lower for p in _REFUSAL_PHRASES):
                    return None, "content_refused"

                # Safeguard block: never retried (deterministic), never permanently
                # skipped (may be session-wide). Caller stops the run if it repeats.
                if not stdout_is_json and any(p in combined_lower for p in _SAFEGUARD_PHRASES):
                    print(f"  Safeguards flagged the request: {(clean_out.strip() or clean_err.strip())[:200]}")
                    return None, "safeguard_flagged"

                # Real limit messages are short, non-JSON text (e.g. "You've hit your
                # session limit · resets 6am"). With rc 0, require non-JSON and short output.
                if (result.returncode != 0 or not stdout_is_json) and \
                        (result.returncode != 0 or len(combined_lower) < 1000) and \
                        any(p in combined_lower for p in _RATE_LIMIT_PHRASES):
                    raise RateLimitHit((clean_err.strip() or clean_out.strip())[:200])

                stderr_lower = clean_err.lower()
                if "invalid authentication" in stderr_lower or "http 401" in stderr_lower or "http 403" in stderr_lower or "status 401" in stderr_lower or "status 403" in stderr_lower or ("401" in stderr_lower and "unauthorized" in stderr_lower) or ("403" in stderr_lower and "forbidden" in stderr_lower):
                    raise AuthError(clean_err.strip()[:200] or clean_out.strip()[:200])

            if result.returncode != 0:
                if not clean_out.strip() and not clean_err.strip():
                    return None, "exit_code_empty"
                if attempt < max_retries:
                    print(f"  claude -p exit code {result.returncode}, retrying ({attempt+1}/{max_retries})...")
                    time.sleep(5 * (attempt + 1))
                    continue
                print(f"  claude -p exit code {result.returncode}")
                if clean_err:
                    print(f"  stderr: {clean_err[:300]}")
                if clean_out:
                    print(f"  stdout: {clean_out[:300]}")
                return None, "exit_code"

            if not output:
                if attempt < max_retries:
                    print(f"  empty output, retrying ({attempt+1}/{max_retries})...")
                    time.sleep(5)
                    continue
                print(f"  claude -p returned empty output")
                return None, "empty_output"

            if record is None:
                if attempt < max_retries:
                    print(f"  JSON parse/validation error, retrying ({attempt+1}/{max_retries})...")
                    time.sleep(5)
                    continue
                print(f"  JSON parse error: could not extract a valid record (needs a 'votes' list)")
                print(f"  Raw output ({len(output)} chars): {output[:500]}")
                return None, "json_parse"

            return record, None

        except FileNotFoundError:
            print("  claude CLI not found.")
            return None, "exit_code"
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                print(f"  timed out, retrying ({attempt+1}/{max_retries})...")
                time.sleep(10)
                continue
            print("  claude -p timed out (300s)")
            return None, "timeout"

    return None, "unknown"


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


_STATE_BILL_RE = re.compile(r'^(AB|SB)\s*\d{2,5}\b', re.IGNORECASE)
_LOCAL_ACTION_RE = re.compile(
    r'oppose|support|implement|ordinance|resolution to|position on|'
    r'continued determination|teleconference|remote meeting|adopt', re.IGNORECASE)


def _sanitize_state_legislature_votes(record):
    """Remove votes that are state legislature activity, not local board actions."""
    votes = record.get("votes", [])
    if not votes:
        return record
    cleaned = []
    removed_ids = set()
    for v in votes:
        item = v.get("item", "")
        if not _STATE_BILL_RE.match(item.strip()):
            cleaned.append(v)
            continue
        if _LOCAL_ACTION_RE.search(item):
            cleaned.append(v)
            continue
        result = v.get("result", "")
        result_match = re.search(r'(\d+)-(\d+)', result)
        result_total = int(result_match.group(1)) + int(result_match.group(2)) if result_match else 0
        total_voters = len(v.get("yes", [])) + len(v.get("no", [])) + len(v.get("abstain", []))
        if result_total > 20 or total_voters == 0:
            if v.get("item_id"):
                removed_ids.add(v["item_id"])
            continue
        cleaned.append(v)
    if len(cleaned) < len(votes):
        record["votes"] = cleaned
        if removed_ids:
            record["council_positions"] = [
                cp for cp in record.get("council_positions", [])
                if cp.get("item_id") not in removed_ids]
    return record


def _split_chunks(text):
    """Split text into overlapping chunks (deterministic for a given text)."""
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
    return chunks


def chunk_cache_path(out_path):
    """Sidecar holding per-chunk results. Suffix is not .json so *.json globs
    (rebuild_combined, stats, doctor, meeting_merge) never treat it as a record."""
    return out_path.with_name(out_path.stem + ".chunks")


def _chunk_fingerprint(text, chunks):
    return {
        "version": 1,
        "source_chars": len(text),
        "chunk_size": CHUNK_SIZE,
        "overlap": CHUNK_OVERLAP,
        "chunks_total": len(chunks),
        "chunk_hashes": [hashlib.sha1(c.encode("utf-8", "replace")).hexdigest() for c in chunks],
    }


def _load_chunk_cache(cache_path, fingerprint):
    """Return {index: {"record":..}|{"error":..}} if the sidecar matches, else {}."""
    if cache_path is None or not cache_path.exists():
        return {}
    try:
        data = json.loads(cache_path.read_text())
        if not isinstance(data, dict):
            return {}
        if any(data.get(k) != v for k, v in fingerprint.items()):
            print("  Chunk cache stale (source changed); re-running all chunks")
            return {}
        return {int(k): v for k, v in (data.get("chunks") or {}).items() if isinstance(v, dict)}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _save_chunk_cache(cache_path, fingerprint, results):
    if cache_path is None:
        return
    try:
        data = dict(fingerprint)
        data["chunks"] = {str(k): v for k, v in sorted(results.items())}
        tmp = cache_path.with_name(cache_path.name + ".tmp")
        tmp.write_text(json.dumps(data, default=str))
        tmp.replace(cache_path)
    except OSError:
        pass


def extract_structured(text, meeting_meta, cache_path=None):
    """Extract structured JSON from document text, chunking if large.

    Returns (record, error_category) tuple. error_category is None on success.
    If cache_path is given, per-chunk results are persisted there; a later call
    on the same source re-runs only failed chunks (refusals are not retried).
    The sidecar is removed once every chunk has a final result.
    """
    meta_context = ""
    if meeting_meta:
        meta_context = f"\nMeeting metadata: {json.dumps(meeting_meta, default=str)}\n"

    if len(text) <= CHUNK_SIZE:
        prompt = EXTRACTION_PROMPT + meta_context + text
        return _call_claude(prompt, schema=EXTRACTION_SCHEMA)

    chunks = _split_chunks(text)
    print(f"  Chunking: {len(text):,} chars → {len(chunks)} chunks", flush=True)

    fingerprint = _chunk_fingerprint(text, chunks)
    results = _load_chunk_cache(cache_path, fingerprint)
    if results:
        redo = [i for i in range(len(chunks))
                if "record" not in results.get(i, {}) and results.get(i, {}).get("error") != "content_refused"]
        print(f"  Resuming from chunk cache: re-running {len(redo)}/{len(chunks)} chunk(s)", flush=True)

    def _collect():
        recs = [results[i]["record"] for i in sorted(results) if "record" in results[i]]
        errs = [results[i]["error"] for i in sorted(results) if "error" in results[i]]
        return recs, errs

    for ci, chunk in enumerate(chunks):
        prev = results.get(ci)
        if prev and ("record" in prev or prev.get("error") == "content_refused"):
            continue
        prompt = EXTRACTION_PROMPT + meta_context + "\n" + chunk
        try:
            record, err = _call_claude(prompt, schema=EXTRACTION_SCHEMA)
        except RateLimitHit as rl:
            records, errors = _collect()
            if records:
                merged = _merge_records(records)
                if merged:
                    merged = dict(merged)
                    merged["_partial"] = True
                    merged["_chunks_completed"] = len(records)
                    merged["_chunks_total"] = len(chunks)
                    merged["_failed_chunks"] = len(chunks) - len(records) - errors.count("content_refused")
                    refused = errors.count("content_refused")
                    if refused:
                        merged["_refused_chunks"] = refused
                    rl.partial_result = merged
                    print(f"  Rate limit at chunk {ci+1}/{len(chunks)}, {len(records)} partial chunks salvaged")
            raise
        if record:
            results[ci] = {"record": record}
        elif err:
            results[ci] = {"error": err}
            print(f"  chunk {ci+1}/{len(chunks)} failed: {err}")
        _save_chunk_cache(cache_path, fingerprint, results)
        if not record and err in ("exit_code_empty", "safeguard_flagged"):
            break
        time.sleep(1)

    records, errors = _collect()
    merged = _merge_records(records)
    if merged:
        # Chunks that failed, timed out, or were never attempted must not vanish silently.
        # Refused chunks are permanent (retrying cannot help): recorded, but not "partial".
        refused = errors.count("content_refused")
        failed_chunks = len(chunks) - len(records) - refused
        if refused or failed_chunks:
            merged = dict(merged)
            merged["_chunks_total"] = len(chunks)
            merged["_chunks_completed"] = len(records)
        if refused:
            merged["_refused_chunks"] = refused
        if failed_chunks:
            merged["_partial"] = True
            merged["_failed_chunks"] = failed_chunks
            if "safeguard_flagged" in errors:
                # Saved as partial, but the caller still needs to know the run
                # is being blocked so it can stop instead of flagging every doc.
                return merged, "safeguard_flagged"
        else:
            merged.pop("_partial", None)
            merged.pop("_failed_chunks", None)
            if cache_path is not None:
                try:
                    cache_path.unlink()
                except OSError:
                    pass
        return merged, None
    # Every chunk failed: return the error (content_refused only if all were refusals).
    non_refused = [e for e in errors if e != "content_refused"]
    if non_refused:
        return None, non_refused[-1]
    return None, errors[-1] if errors else "unknown"


CHARS_PER_TOKEN = 4
OPUS_INPUT_PER_MTOK = 15.0
OPUS_OUTPUT_PER_MTOK = 75.0
PROMPT_OVERHEAD_TOKENS = 800
OUTPUT_TOKENS_EST = 1500
TALLY_PATH = DATA_DIR / "pipeline" / "extraction-tally.jsonl"


def cmd_tally(args):
    """Tally forward-facing document volume across all agencies.

    Counts docs from meetings within each agency's lookback window (default 2mo).
    Use --tally-all to count everything regardless of date.
    Apples-to-apples comparison for cost projection.
    """
    from transforms.triage import predict_relevance
    from collections import defaultdict

    tally_all = getattr(args, "tally_all", False)
    agencies_cfg = load_agencies(enabled_only=True)
    doc_index = load_doc_index()

    if not tally_all:
        window_months = 2
        cutoff = (datetime.now().date() - timedelta(days=window_months * 30)).isoformat()

    all_sources = collect_all_sources(include_tally=True)
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

        if not tally_all and source_type != "transcript":
            meeting_date = doc_index.get(sf.name, "")
            if meeting_date and meeting_date < cutoff:
                continue
            if not meeting_date:
                continue

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
    monthly_cost = round(est_cost / 2, 4) if not tally_all else None

    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "mode": "all" if tally_all else "forward",
        "incoming": incoming,
        "totals": {**totals, "est_cost_usd": est_cost},
    }

    TALLY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(TALLY_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")

    window_label = "ALL TIME" if tally_all else "last 2 months"
    print(f"=== Forward Tally ({window_label}) — {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    print()
    print(f"{'Agency':<18} {'Docs':>5} {'LLM':>5} {'Skip':>5} {'Tokens':>10} {'Chunks':>7}")
    print("-" * 55)
    for agency in sorted(incoming):
        t = incoming[agency]
        print(f"{agency:<18} {t['docs']:>5} {t['need_llm']:>5} {t['triage_skip']:>5} {t['input_tokens']:>10} {t['chunks']:>7}")
    print("-" * 55)
    print(f"{'TOTAL':<18} {totals['docs']:>5} {totals['need_llm']:>5} {totals['triage_skip']:>5} {totals['input_tokens']:>10} {totals['chunks']:>7}")
    print(f"\nEstimated API cost (Opus 4.8): ${est_cost:.4f}")
    if monthly_cost:
        print(f"Estimated monthly rate: ${monthly_cost:.2f}/mo")
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
    doc_index = load_doc_index()
    backfilled = 0

    skip_files = {"extraction-state.json", "meetings-state.json", "document-dates.json",
                  "extraction-failures.json"}
    for json_file in sorted(structured_dir.glob("*.json")):
        if json_file.name in skip_files:
            continue
        try:
            record = json.loads(json_file.read_text())
            if not isinstance(record, dict) or "votes" not in record:
                continue
            if not record.get("date") and record.get("_source"):
                idx_date = doc_index.get(record["_source"], "")
                if not idx_date:
                    idx_date = _parse_date_from_filename(record["_source"])
                if idx_date:
                    record["date"] = idx_date
                    json_file.write_text(json.dumps(record, indent=2, default=str))
                    backfilled += 1
            records.append(record)
        except (json.JSONDecodeError, Exception):
            continue

    records.sort(key=lambda r: r.get("date") or "")
    with open(jsonl_path, "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")

    backfill_msg = f" ({backfilled} dates backfilled)" if backfilled else ""
    print(f"Combined JSONL: {len(records)} records → {jsonl_path}{backfill_msg}")
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


def collect_all_sources(queue=None, hot_days=14, include_tally=False, skip_forward_backlog=False):
    """Collect raw source files, optionally filtered by queue.

    queue: None (all), "hot" (recent meetings), or "cold" (backlog).
    hot_days: days back from today that counts as "hot" (default 14).
    include_tally: include tally-only agencies (for cost projection, not extraction).

    forward_only agencies use enabled_date as cutoff for hot queue;
    cold queue includes their full backlog unless skip_forward_backlog=True.
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

        if is_tally_only and not include_tally:
            continue
        if is_forward_only and queue == "cold" and skip_forward_backlog:
            continue
        for f in sorted(ddir.glob("*.txt"), reverse=True):
            if queue:
                meeting_date = doc_index.get(f.name, "")

                if is_forward_only and queue == "hot":
                    if not meeting_date or meeting_date < enabled_date:
                        continue
                elif not is_forward_only:
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
        if not isinstance(data, dict):
            return True
        # Partial records (failed/timed-out chunks, rate-limit salvage) are not done.
        return bool(data.get("_partial"))
    except (json.JSONDecodeError, OSError):
        return True


def is_skippable(text):
    """Check if text is un-extractable garbage from PDF rendering."""
    stripped = text.strip()
    if len(stripped) < MIN_DOC_CHARS:
        return "too_short"
    if len(stripped) > MAX_DOC_CHARS:
        return "oversized"

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


MIN_DOC_CHARS = 100                 # below this a source is "too_short"
REPEATED_FAILURE_EXPIRY_DAYS = 14   # repeated_failure markers are retried after this


def write_skip_marker(source_path, reason):
    """Write a .skip file recording why, plus the source size/mtime it was judged on.

    A marker whose source later changed is ignored (see has_skip_marker).
    """
    skip_path = source_path.with_suffix(source_path.suffix + SKIP_MARKER)
    data = {"reason": reason, "skipped_at": datetime.now().isoformat()}
    try:
        st = source_path.stat()
        data["src_size"] = st.st_size
        data["src_mtime"] = st.st_mtime
    except OSError:
        pass
    skip_path.write_text(json.dumps(data))


def clear_skip_marker(source_path):
    """Remove a stale .skip marker (called after a successful extraction)."""
    try:
        source_path.with_suffix(source_path.suffix + SKIP_MARKER).unlink()
    except OSError:
        pass


def has_skip_marker(source_path):
    """Check if source has been permanently marked as skip or split.

    A .skip marker is ignored when the source text changed since it was written
    (src_size differs; mtime alone is not used because S3 sync resets mtimes),
    when a too_short source has grown past MIN_DOC_CHARS, or when a
    repeated_failure marker is older than REPEATED_FAILURE_EXPIRY_DAYS.
    Legacy markers without src fields stay valid for every other reason.
    """
    if source_path.with_suffix(source_path.suffix + ".split").exists():
        return True
    skip_path = source_path.with_suffix(source_path.suffix + SKIP_MARKER)
    if not skip_path.exists():
        return False
    try:
        marker = json.loads(skip_path.read_text())
        if not isinstance(marker, dict):
            marker = {}
    except (json.JSONDecodeError, OSError):
        return True
    reason = marker.get("reason")
    try:
        size = source_path.stat().st_size
    except OSError:
        return True
    if "src_size" in marker and marker["src_size"] != size:
        return False
    if reason == "too_short" and size >= MIN_DOC_CHARS:
        try:
            if len(source_path.read_text().strip()) >= MIN_DOC_CHARS:
                return False
        except OSError:
            pass
    if reason == "repeated_failure":
        try:
            age = datetime.now() - datetime.fromisoformat(marker.get("skipped_at", ""))
            if age > timedelta(days=REPEATED_FAILURE_EXPIRY_DAYS):
                return False
        except (ValueError, TypeError):
            pass
    return True


import re as _re

TARGET_PART_CHARS = 500_000  # aim for ~500K per part (~8 chunks each)

_SECTION_RE = _re.compile(
    r'^\s*(?:STAFF REPORT|PUBLIC HEARING|CONSENT CALENDAR|NEW BUSINESS|'
    r'OLD BUSINESS|ACTION ITEMS?|CLOSED SESSION)\s*$',
    _re.IGNORECASE,
)

_ANSI_RE = _re.compile(r'\x1b\[[0-9;]*[a-zA-Z]')

_FILENAME_DATE_RE = _re.compile(r'(\d{4})(\d{2})(\d{2})')


def _parse_date_from_filename(filename):
    """Extract YYYY-MM-DD from filenames like cb-planning-commission-20220615-agenda-part25.txt"""
    for m in _FILENAME_DATE_RE.finditer(filename):
        try:
            d = datetime.strptime("".join(m.groups()), "%Y%m%d")
        except ValueError:
            continue
        if 2000 <= d.year <= 2035:
            return d.strftime("%Y-%m-%d")
    return None


def split_oversized_doc(source_path):
    """Split an oversized agenda packet into parts at section boundaries.

    Targets ~500K chars per part, splitting at the nearest section header
    (STAFF REPORT, PUBLIC HEARING, etc.). Falls back to line-count splits
    when no headers exist.

    Writes {stem}-part01.txt, etc. into the same directory.
    Marks the original with .split so it's skipped on future runs.
    Returns count of parts written.
    """
    text = source_path.read_text()
    lines = text.split("\n")

    # Build cumulative char offsets per line
    offsets = []
    cum = 0
    for line in lines:
        offsets.append(cum)
        cum += len(line) + 1  # +1 for newline

    # Find section boundary line numbers
    section_starts = []
    for i, line in enumerate(lines):
        if _SECTION_RE.match(line):
            section_starts.append(i)

    # Build parts by accumulating sections until we hit the target size
    parts = []
    part_start = 0

    for boundary in section_starts:
        if boundary <= part_start:
            continue
        part_chars = offsets[boundary] - offsets[part_start]
        if part_chars >= TARGET_PART_CHARS:
            parts.append((part_start, boundary))
            part_start = boundary

    # Final part
    if part_start < len(lines):
        parts.append((part_start, len(lines)))

    # If no section headers found, split by line count
    if len(parts) <= 1:
        n_parts = max(2, len(text) // TARGET_PART_CHARS + 1)
        lines_per = len(lines) // n_parts
        parts = []
        for i in range(n_parts):
            start = i * lines_per
            end = (i + 1) * lines_per if i < n_parts - 1 else len(lines)
            parts.append((start, end))

    # Sub-split any part still over MAX_DOC_CHARS
    final_parts = []
    for start, end in parts:
        part_chars = offsets[min(end, len(offsets) - 1)] - offsets[start]
        if part_chars > MAX_DOC_CHARS:
            n_sub = part_chars // TARGET_PART_CHARS + 1
            sub_lines = (end - start) // n_sub
            for j in range(n_sub):
                s = start + j * sub_lines
                e = start + (j + 1) * sub_lines if j < n_sub - 1 else end
                final_parts.append((s, e))
        else:
            final_parts.append((start, end))

    stem = source_path.stem
    parent = source_path.parent
    parts_written = 0
    for idx, (start, end) in enumerate(final_parts):
        part_text = "\n".join(lines[start:end]).strip()
        if len(part_text) < 100:
            continue
        part_path = parent / f"{stem}-part{idx+1:02d}.txt"
        part_path.write_text(part_text)
        parts_written += 1

    split_marker = source_path.with_suffix(source_path.suffix + ".split")
    split_marker.write_text(json.dumps({
        "split_at": datetime.now().isoformat(),
        "parts": parts_written,
        "original_chars": len(text),
    }))

    return parts_written


def presplit_oversized(sources):
    """Pre-split oversized documents before extraction starts.

    Runs before the main loop so parts enter the queue immediately
    instead of waiting for the next run.
    """
    skip_names = ["project_plans", "environmental_doc", "pavement",
                  "plan_set", "architectural", "traffic_study",
                  "transportation_assessment", "geotechnical"]
    split_count = 0
    for source_type, sf in sources:
        if has_skip_marker(sf):
            continue
        try:
            size = sf.stat().st_size
        except OSError:
            continue
        if size <= MAX_DOC_CHARS:
            continue
        try:
            text = sf.read_text()
        except OSError:
            continue
        stripped = text.strip()
        if len(stripped) <= MAX_DOC_CHARS:
            continue
        name_lower = sf.name.lower()
        if any(s in name_lower for s in skip_names):
            write_skip_marker(sf, "oversized_non_meeting")
            print(f"  Pre-skip: {sf.name} (oversized non-meeting, {len(stripped):,} chars)")
        else:
            parts = split_oversized_doc(sf)
            print(f"  Pre-split: {sf.name} -> {parts} parts ({len(stripped):,} chars)")
            split_count += 1
    return split_count


def _failure_state_path():
    return STRUCTURED_DIR / "extraction-failures.json"


def load_failure_state():
    path = _failure_state_path()
    if path.exists():
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_failure_state(state):
    _failure_state_path().write_text(json.dumps(state, indent=2, default=str))


def record_failure(state, filename, error_category, queue=None, count=True):
    """Record a failure. count=False logs it without advancing the auto-skip
    counter — for failures that are not the document's fault (a session-wide
    safeguard block), where auto-skipping would bury good documents."""
    entry = state.get(filename, {})
    if count:
        entry["fail_count"] = entry.get("fail_count", 0) + 1
    else:
        entry.setdefault("fail_count", 0)
    if "first_failed" not in entry:
        entry["first_failed"] = datetime.now().isoformat()
    entry["last_failed"] = datetime.now().isoformat()
    entry["last_error"] = error_category or "unknown"
    if queue:
        entry["queue"] = queue
    state[filename] = entry


def clear_failure(state, filename):
    state.pop(filename, None)


def _source_size_key(item):
    """Sort key: file size; files that vanished mid-run sort last."""
    try:
        return item[1].stat().st_size
    except OSError:
        return float("inf")


def cmd_extract(args):
    """Extract structured records from all raw sources (documents + transcripts)."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    queue = getattr(args, "queue", None)
    skip_fwd = getattr(args, "skip_forward_backlog", False)
    hot_days = getattr(args, "hot_days", 14)

    all_sources = collect_all_sources(queue=queue, hot_days=hot_days, skip_forward_backlog=skip_fwd)

    # Pre-split oversized docs from ALL queues so they don't accumulate
    all_for_presplit = collect_all_sources(queue=None, hot_days=hot_days) if queue else all_sources
    split_count = presplit_oversized(all_for_presplit)
    if split_count:
        print(f"  Pre-split {split_count} oversized doc(s), re-scanning...")
        all_sources = collect_all_sources(queue=queue, hot_days=hot_days, skip_forward_backlog=skip_fwd)

    if not all_sources:
        queue_label = f" ({queue})" if queue else ""
        print(f"No source files found{queue_label}. Run fetch first.")
        return

    meeting_filter = set(args.meeting) if args.meeting else None
    agency_filter = getattr(args, "agency", None)

    to_process = []
    skipped_markers = 0
    for source_type, sf in all_sources:
        if agency_filter and f"/{agency_filter}/" not in str(sf):
            continue
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

    # Size ordering (small first) is for hot/default runs only; cold keeps the
    # oldest-first order produced by collect_all_sources.
    if queue != "cold":
        to_process.sort(key=_source_size_key)

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

    doc_index = load_doc_index()
    failure_state = load_failure_state()
    rate_limited = False
    stop_hour = getattr(args, "stop_at", None)
    print(f"Extracting structured data: {len(to_process)} sources to process")
    if stop_hour:
        print(f"  Will stop at {stop_hour}:00")
    success = 0
    failed = 0
    skipped = 0
    failure_skipped = 0
    triage_skipped = 0
    consecutive_failures = 0
    consecutive_safeguards = 0
    auth_failure_count = 0
    MAX_CONSECUTIVE_FAILURES = 5
    MAX_CONSECUTIVE_SAFEGUARDS = 3
    MAX_AUTH_RETRIES = 2

    i = 0
    try:
        while i < len(to_process):
            source_type, sf, out_path = to_process[i]
            try:
                if stop_hour and datetime.now().hour >= stop_hour:
                    remaining = len(to_process) - i
                    print(f"\nStopping at {datetime.now().strftime('%H:%M')} ({remaining} remaining, will resume next run)")
                    break
                if getattr(args, "limit", None) and success >= args.limit:
                    remaining = len(to_process) - i
                    print(f"\nLimit reached ({args.limit} extractions). {remaining} remaining, will resume next run.")
                    break

                prior = failure_state.get(sf.name, {})
                if prior.get("fail_count", 0) >= MAX_FAIL_RETRIES:
                    last_err = prior.get("last_error", "unknown")
                    write_skip_marker(sf, "repeated_failure")
                    clear_failure(failure_state, sf.name)
                    failure_skipped += 1
                    print(f"[{i+1}/{len(to_process)}] {sf.name}")
                    print(f"  Auto-skipped: failed {prior['fail_count']} times (last: {last_err})")
                    i += 1
                    continue

                label = f"[{source_type}]" if source_type == "transcript" else ""
                print(f"[{i+1}/{len(to_process)}] {sf.name} {label}")

                text = sf.read_text()

                skip_reason = is_skippable(text)
                if skip_reason == "oversized":
                    name_lower = sf.name.lower()
                    skip_names = ["project_plans", "environmental_doc", "pavement",
                                  "plan_set", "architectural", "traffic_study",
                                  "transportation_assessment", "geotechnical"]
                    if any(s in name_lower for s in skip_names):
                        write_skip_marker(sf, "oversized_non_meeting")
                        skipped += 1
                        print(f"  Skipping permanently: oversized non-meeting ({len(text.strip()):,} chars)")
                    else:
                        parts_written = split_oversized_doc(sf)
                        print(f"  Split into {parts_written} parts ({len(text.strip()):,} chars)")
                        # Add split parts to current run queue
                        parent = sf.parent
                        for idx in range(1, parts_written + 1):
                            part_path = parent / f"{sf.stem}-part{idx:02d}.txt"
                            if part_path.exists():
                                part_out = STRUCTURED_DIR / f"{part_path.stem}.json"
                                if not part_out.exists():
                                    to_process.append((source_type, part_path, part_out))
                        if parts_written:
                            if queue != "cold":
                                to_process[i+1:] = sorted(to_process[i+1:], key=_source_size_key)
                    i += 1
                    continue
                elif skip_reason:
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
                    record, error_cat = extract_structured(text, meta, cache_path=None if args.force else chunk_cache_path(out_path))
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
                    partial = getattr(e, 'partial_result', None)
                    if partial:
                        if not partial.get("meeting_id"):
                            mid = resolve_meeting_id(sf.stem)
                            partial["meeting_id"] = mid
                        partial["_source"] = sf.name
                        partial["_source_type"] = source_type
                        out_path.write_text(json.dumps(partial, indent=2, default=str))
                        success += 1
                        print(f"  Saved partial extraction ({len(partial.get('votes',[]))}v {len(partial.get('housing_items',[]))}h)")

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
                    rate_limited = True
                    break

                if error_cat == "content_refused":
                    write_skip_marker(sf, "content_refused")
                    skipped += 1
                    print(f"  Content refused by model — permanently skipped")
                    i += 1
                    continue

                if error_cat == "safeguard_flagged":
                    consecutive_safeguards += 1
                else:
                    consecutive_safeguards = 0

                if record:
                    if not record.get("meeting_id"):
                        mid = resolve_meeting_id(sf.stem)
                        record["meeting_id"] = mid
                    if not record.get("date"):
                        idx_date = doc_index.get(sf.name, "")
                        if idx_date:
                            record["date"] = idx_date
                        if not record.get("date"):
                            record["date"] = _parse_date_from_filename(sf.name)
                    record["_source"] = sf.name
                    record["_source_type"] = source_type
                    record = _sanitize_state_legislature_votes(record)

                    out_path.write_text(json.dumps(record, indent=2, default=str))
                    success += 1
                    consecutive_failures = 0
                    clear_skip_marker(sf)  # drop a stale marker that has_skip_marker ignored
                    if record.get("_partial"):
                        # Saved, but not done: needs_reextraction retries it next run.
                        record_failure(failure_state, sf.name, "partial_chunks", queue=queue)
                        print(f"  PARTIAL ({record.get('_failed_chunks', '?')} chunk(s) failed, "
                              f"{len(record.get('votes',[]))}v {len(record.get('housing_items',[]))}h)")
                    else:
                        clear_failure(failure_state, sf.name)
                        print(f"  OK ({len(text)} chars, {len(record.get('votes',[]))}v {len(record.get('housing_items',[]))}h)")
                elif error_cat == "safeguard_flagged":
                    # Not the document's fault unless it is the only one flagged:
                    # the first in a row counts toward auto-skip, a streak does not,
                    # because a streak means the block is session-wide.
                    failed += 1
                    record_failure(failure_state, sf.name, error_cat, queue=queue,
                                   count=(consecutive_safeguards == 1))
                    fc = failure_state[sf.name]["fail_count"]
                    print(f"  FAILED (safeguard_flagged, attempt {fc}/{MAX_FAIL_RETRIES})")
                else:
                    failed += 1
                    consecutive_failures += 1
                    record_failure(failure_state, sf.name, error_cat, queue=queue)
                    fc = failure_state[sf.name]["fail_count"]
                    print(f"  FAILED ({error_cat or 'unknown'}, attempt {fc}/{MAX_FAIL_RETRIES})")

                    if error_cat == "exit_code_empty" and consecutive_failures >= 2:
                        remaining = len(to_process) - i - 1
                        print(f"\n  Likely session limit: {consecutive_failures} consecutive empty-output failures.")
                        print(f"  Stopping — run `claude -p \"test\"` to check session status.")
                        print(f"  ({success} extracted, {failed} failed, {skipped} skipped, {remaining} remaining)")
                        break

                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        remaining = len(to_process) - i - 1
                        print(f"\n  Circuit breaker: {MAX_CONSECUTIVE_FAILURES} consecutive failures.")
                        print(f"  Stopping — something is broken (auth, network, CLI).")
                        print(f"  ({success} extracted, {failed} failed, {skipped} skipped, {remaining} remaining)")
                        break

                if consecutive_safeguards >= MAX_CONSECUTIVE_SAFEGUARDS:
                    remaining = len(to_process) - i - 1
                    print(f"\n  Safeguards flagged {consecutive_safeguards} documents in a row.")
                    print(f"  Stopping — the block looks session-wide, not document-specific.")
                    print(f"  Nothing was permanently skipped; rerun later or check "
                          f"`claude -p \"test\"`.")
                    print(f"  ({success} extracted, {failed} failed, {skipped} skipped, {remaining} remaining)")
                    break

                auth_failure_count = 0  # reset on any successful claude call
                time.sleep(1)
                i += 1
            except Exception as e:
                print(f"ERROR: extract {sf}: {e}")
                failed += 1
                consecutive_failures += 1
                record_failure(failure_state, sf.name, f"exception:{type(e).__name__}", queue=queue)
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    print(f"\n  Circuit breaker: {MAX_CONSECUTIVE_FAILURES} consecutive failures.")
                    break
                i += 1
                continue
    finally:
        # Persist failure state and the combined file even on RateLimitHit / crash.
        try:
            save_failure_state(failure_state)
        except Exception as e:
            print(f"ERROR: save_failure_state: {e}")
        if success > 0:
            try:
                rebuild_combined(STRUCTURED_DIR)
            except Exception as e:
                print(f"ERROR: rebuild_combined: {e}")


    triage_msg = f", {triage_skipped} triage-skipped" if triage_skipped else ""
    fail_skip_msg = f", {failure_skipped} auto-skipped (repeated failures)" if failure_skipped else ""
    print(f"\nDone. {success} extracted, {failed} failed, {skipped} permanently skipped{fail_skip_msg}{triage_msg}.")

    if rate_limited:
        sys.exit(2)


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
    parser.add_argument("--tally", action="store_true", help="Forward-facing tally (last 2mo) for cost projection")
    parser.add_argument("--tally-all", action="store_true", help="Tally all docs regardless of date (backlog + forward)")
    parser.add_argument("--limit", type=int, metavar="N", help="Stop after N successful extractions")
    parser.add_argument("--queue", choices=["hot", "cold"], help="hot=recent meetings only, cold=backlog only")
    parser.add_argument("--hot-days", type=int, default=14, help="Days back that counts as 'hot' (default: 14)")
    parser.add_argument("--cost-report", action="store_true", help="Project monthly API cost from discovery + tally history")
    parser.add_argument("--agency", metavar="SLUG", help="Only extract sources for this agency")
    parser.add_argument("--skip-forward-backlog", action="store_true",
                        help="Cold queue: exclude forward_only agencies (their pre-enable history)")

    args = parser.parse_args()

    if args.cost_report:
        cmd_cost_report(args)
    elif args.tally or args.tally_all:
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
