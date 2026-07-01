#!/usr/bin/env python3
"""
Pipeline doctor — post-run diagnostics and self-healing.

Analyzes nightly pipeline logs, identifies errors, applies safe fixes,
and tracks its own effectiveness across runs.

Usage:
    python pipeline_doctor.py              # diagnose + fix
    python pipeline_doctor.py --dry-run    # diagnose only, no fixes
    python pipeline_doctor.py --history    # show diagnosis history

Called automatically at end of civic-pipeline. Can also run standalone.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).parent
sys.path.insert(0, str(REPO_ROOT))
from civic_utils import load_agencies, all_docs_dirs, agency_data_dir

DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
DIAGNOSIS_LOG = DATA_DIR / "pipeline-doctor.jsonl"
EXTRACTION_LOG = DATA_DIR / "structured-extraction.log"
PIPELINE_LOG = DATA_DIR / "pipeline-cron.log"
NOTIFY_LOG = DATA_DIR / "pipeline-notify.log"

SAFE_SKIP_REASONS = {
    "repeated_auth_blocker": "File blocks extraction repeatedly via auth errors on chunking",
    "oversized_non_meeting": "Enormous file with no meeting content (plans, drawings, schedules)",
    "repeated_failure": "Failed extraction 3+ consecutive runs with no progress",
}

MAX_SOURCE_SIZE_FOR_SKIP = 2_000_000  # 2MB — anything bigger is almost certainly not meeting text


def read_tail(path, lines=500):
    """Read last N lines of a file."""
    if not path.exists():
        return []
    with open(path) as f:
        all_lines = f.readlines()
    return all_lines[-lines:]


def get_extraction_errors(log_lines):
    """Parse extraction log for error patterns."""
    errors = {
        "auth_errors": [],
        "rate_limits": [],
        "timeouts": [],
        "json_parse": [],
        "failures": [],
        "circuit_breakers": [],
        "terminated": [],
    }

    blocking_file = None
    i = 0
    while i < len(log_lines):
        line = log_lines[i].strip()

        if "Auth error" in line:
            # Look back for the filename
            for j in range(max(0, i - 5), i):
                m = re.search(r'\] (.+\.txt)', log_lines[j])
                if m:
                    errors["auth_errors"].append(m.group(1))
                    break

        elif "Rate limit" in line or "rate limit" in line:
            errors["rate_limits"].append(line)

        elif "timed out" in line:
            for j in range(max(0, i - 3), i):
                m = re.search(r'\] (.+\.txt)', log_lines[j])
                if m:
                    errors["timeouts"].append(m.group(1))
                    break

        elif "JSON parse error" in line:
            for j in range(max(0, i - 3), i):
                m = re.search(r'\] (.+\.txt)', log_lines[j])
                if m:
                    errors["json_parse"].append(m.group(1))
                    break

        elif line == "FAILED":
            for j in range(max(0, i - 3), i):
                m = re.search(r'\] (.+\.txt)', log_lines[j])
                if m:
                    errors["failures"].append(m.group(1))
                    break

        elif "Circuit breaker" in line:
            errors["circuit_breakers"].append(line)

        elif "Terminated" in line:
            errors["terminated"].append(line)

        i += 1

    return errors


def get_pipeline_errors(log_lines):
    """Parse pipeline cron log for phase-level errors."""
    errors = {
        "terminated_phases": [],
        "failed_phases": [],
        "zero_extraction_runs": 0,
        "last_productive_run": None,
        "timed_out_phases": [],
        "command_not_found": [],
        "overlapping_runs": [],
        "phase_durations": [],
    }

    current_date = None
    last_start = None
    last_complete = None
    phase_start = None
    phase_label = None

    for line in log_lines:
        line = line.strip()

        ts_m = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
        ts = None
        if ts_m:
            try:
                ts = datetime.strptime(ts_m.group(1), '%Y-%m-%d %H:%M:%S')
            except ValueError:
                pass

        date_m = re.match(r'\[(\d{4}-\d{2}-\d{2})', line)
        if date_m:
            current_date = date_m.group(1)

        if "Starting civic pipeline" in line and ts:
            if last_start and not last_complete:
                errors["overlapping_runs"].append(
                    f"Run started {ts_m.group(1)} while previous run (started {last_start.strftime('%Y-%m-%d %H:%M')}) never completed"
                )
            last_start = ts
            last_complete = None

        if "Pipeline complete" in line and ts:
            last_complete = ts

        if "timed out" in line:
            errors["timed_out_phases"].append(line)

        if "command not found" in line:
            errors["command_not_found"].append(line)

        if "Terminated" in line:
            errors["terminated_phases"].append(current_date or "unknown")

        if "failed" in line.lower() and "continuing" in line.lower():
            errors["failed_phases"].append(line)

        if "No new extractions" in line:
            errors["zero_extraction_runs"] += 1

        if re.search(r'New extractions: \d+', line):
            errors["last_productive_run"] = current_date

        # Track phase durations
        fetch_start_m = re.search(r'Fetching (.+?)\.\.\.', line)
        if fetch_start_m and ts:
            if phase_start and phase_label:
                duration = (ts - phase_start).total_seconds()
                errors["phase_durations"].append((phase_label, duration, phase_start.strftime('%Y-%m-%d')))
            phase_label = fetch_start_m.group(1)
            phase_start = ts

        fetch_done_m = re.search(r'(.+?) fetch done', line) or re.search(r'(.+?) done', line)
        if fetch_done_m and ts and phase_start:
            duration = (ts - phase_start).total_seconds()
            errors["phase_durations"].append((phase_label or fetch_done_m.group(1), duration, phase_start.strftime('%Y-%m-%d')))
            phase_start = None
            phase_label = None

    return errors


def find_blocking_files():
    """Find files that repeatedly block extraction via auth errors on their own chunks.

    Only flags a file if it was being actively chunked when the auth error hit —
    small files that happened to be next in queue after an auth failure are not blockers.
    """
    if not EXTRACTION_LOG.exists():
        return []

    lines = read_tail(EXTRACTION_LOG, 2000)
    auth_files = {}
    for i, line in enumerate(lines):
        if "Auth error" not in line:
            continue
        # Walk backwards to find the file AND confirm it was chunked
        fname = None
        was_chunked = False
        for j in range(max(0, i - 8), i):
            prev = lines[j].strip()
            if "Chunking:" in prev:
                was_chunked = True
            m = re.search(r'\] (.+\.txt)', prev)
            if m:
                fname = m.group(1)
        if fname and was_chunked:
            auth_files[fname] = auth_files.get(fname, 0) + 1

    return [(f, count) for f, count in auth_files.items() if count >= 2]


def find_oversized_files():
    """Find source files that are too large and likely not meeting content."""
    oversized = []
    for docs_dir in all_docs_dirs():
        for f in docs_dir.glob("*.txt"):
            if f.stat().st_size > MAX_SOURCE_SIZE_FOR_SKIP:
                skip_path = f.with_suffix(f.suffix + ".skip")
                json_path = STRUCTURED_DIR / (f.stem + ".json")
                if not skip_path.exists() and not json_path.exists():
                    oversized.append((f, f.stat().st_size))
    return oversized


def check_auth_health():
    """Check if claude -p is currently authenticated."""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input="Reply with exactly: OK",
            capture_output=True, text=True, timeout=30,
            env=env,
        )
        combined = (result.stdout + result.stderr).lower()
        if "invalid authentication" in combined or "401" in combined or "403" in combined:
            return "broken"
        if "session limit" in combined or "rate limit" in combined:
            return "rate_limited"
        if result.returncode == 0 and "ok" in result.stdout.lower():
            return "healthy"
        return "unknown"
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return "unavailable"


def count_remaining():
    """Count files still needing extraction."""
    sources = []
    for docs_dir in all_docs_dirs():
        sources += list(docs_dir.glob("*.txt"))

    extracted = set(f.stem for f in STRUCTURED_DIR.glob("*.json"))
    remaining = []
    for f in sources:
        skip_path = f.with_suffix(f.suffix + ".skip")
        if skip_path.exists():
            continue
        if f.stem in extracted:
            continue
        remaining.append(f)
    return remaining


def diagnose(dry_run=False):
    """Run full diagnosis and apply safe fixes."""
    now = datetime.now()
    diagnosis = {
        "timestamp": now.isoformat(),
        "findings": [],
        "fixes_applied": [],
        "recommendations": [],
        "stats": {},
    }

    print(f"[doctor] Pipeline diagnosis — {now.strftime('%Y-%m-%d %H:%M')}")
    print()

    # 1. Check extraction log errors
    ext_lines = read_tail(EXTRACTION_LOG, 1000)
    ext_errors = get_extraction_errors(ext_lines)

    # 2. Check pipeline log
    pipe_lines = read_tail(PIPELINE_LOG, 200)
    pipe_errors = get_pipeline_errors(pipe_lines)

    # 3. Find blocking files
    blockers = find_blocking_files()

    # 4. Find oversized files
    oversized = find_oversized_files()

    # 5. Count remaining
    remaining = count_remaining()

    # 6. Check auth
    auth_status = check_auth_health()

    # ─── Report ───

    diagnosis["stats"] = {
        "remaining_files": len(remaining),
        "auth_status": auth_status,
        "zero_extraction_runs_recent": pipe_errors["zero_extraction_runs"],
        "last_productive_run": pipe_errors["last_productive_run"],
        "blocking_files": len(blockers),
        "oversized_files": len(oversized),
        "timed_out_phases": len(pipe_errors["timed_out_phases"]),
        "overlapping_runs": len(pipe_errors["overlapping_runs"]),
        "command_not_found": len(pipe_errors["command_not_found"]),
    }

    print(f"  Remaining files:     {len(remaining)}")
    print(f"  Auth status:         {auth_status}")
    print(f"  Last productive run: {pipe_errors['last_productive_run'] or 'unknown'}")
    print(f"  Zero-extraction runs: {pipe_errors['zero_extraction_runs']} (recent)")
    print()

    # ─── Finding: Blocking files ───

    if blockers:
        for fname, count in blockers:
            # Check if already skipped
            source_path = None
            for docs_dir in all_docs_dirs():
                candidate = docs_dir / fname
                if candidate.exists():
                    source_path = candidate
                    break
            skip_path = source_path.with_suffix(source_path.suffix + ".skip") if source_path else None

            if skip_path and skip_path.exists():
                continue  # already handled

            finding = f"File '{fname}' blocked extraction {count} times via auth/chunk errors"
            diagnosis["findings"].append(finding)
            print(f"  FINDING: {finding}")

            if not dry_run and source_path and source_path.exists() and skip_path:
                skip_path.write_text(json.dumps({
                    "reason": "repeated_auth_blocker",
                    "skipped_at": now.isoformat(),
                    "skipped_by": "pipeline_doctor",
                    "block_count": count,
                }))
                fix = f"Skipped '{fname}' — blocked extraction {count} times"
                diagnosis["fixes_applied"].append(fix)
                print(f"  FIX: {fix}")

    # ─── Finding: Oversized files ───

    if oversized:
        for fpath, size in oversized:
            # Check if content is likely non-meeting (plans, drawings, salary schedules)
            text_sample = fpath.read_text()[:2000].lower()
            non_meeting_signals = [
                "salary schedule", "architectural plan", "plan set",
                "drawing", "specification", "bid tabulation",
                "financial statement", "investment portfolio",
            ]
            is_non_meeting = any(sig in text_sample for sig in non_meeting_signals)

            if is_non_meeting:
                finding = f"Oversized non-meeting file: {fpath.name} ({size:,} bytes)"
                diagnosis["findings"].append(finding)
                print(f"  FINDING: {finding}")

                if not dry_run:
                    skip_path = fpath.with_suffix(fpath.suffix + ".skip")
                    skip_path.write_text(json.dumps({
                        "reason": "oversized_non_meeting",
                        "skipped_at": now.isoformat(),
                        "skipped_by": "pipeline_doctor",
                        "size_bytes": size,
                    }))
                    fix = f"Skipped '{fpath.name}' — {size:,} bytes, non-meeting content"
                    diagnosis["fixes_applied"].append(fix)
                    print(f"  FIX: {fix}")

    # ─── Finding: Auth broken ───

    if auth_status == "broken":
        finding = "claude -p authentication is broken — extraction will fail"
        diagnosis["findings"].append(finding)
        diagnosis["recommendations"].append("Run `claude /login` to re-authenticate")
        print(f"  FINDING: {finding}")
        print(f"  RECOMMEND: Run `claude /login` to re-authenticate")

    elif auth_status == "rate_limited":
        finding = "claude -p is rate-limited — extraction will stall until reset"
        diagnosis["findings"].append(finding)
        print(f"  FINDING: {finding}")

    # ─── Finding: NCTD/SANDAG fetch errors ───

    agency_logs = []
    for slug, config in load_agencies().items():
        log_path = agency_data_dir(slug) / "watch.log"
        agency_logs.append((config.get("name", slug), log_path))
        permits_log = agency_data_dir(slug) / "permits" / "fetch.log"
        if permits_log.exists():
            agency_logs.append((f"{config.get('name', slug)} permits", permits_log))
    for label, log_path in agency_logs:
        log_lines_agency = read_tail(log_path, 100)
        agency_errors = [l.strip() for l in log_lines_agency if "error" in l.lower() or "failed" in l.lower() or "Traceback" in l]
        if agency_errors:
            finding = f"{label} fetch had {len(agency_errors)} error(s) in recent log"
            diagnosis["findings"].append(finding)
            print(f"  FINDING: {finding}")
            for e in agency_errors[-3:]:
                print(f"    {e[:120]}")

    # ─── Finding: Timed-out phases ───

    if pipe_errors["timed_out_phases"]:
        for phase_line in pipe_errors["timed_out_phases"][-5:]:
            finding = f"Phase timed out: {phase_line[:120]}"
            diagnosis["findings"].append(finding)
            print(f"  FINDING: {finding}")
        diagnosis["recommendations"].append(
            "Scraper phase exceeded 30min timeout — check if site is slow or scraper is re-fetching old data"
        )

    # ─── Finding: Command not found ───

    if pipe_errors["command_not_found"]:
        finding = f"'command not found' errors ({len(pipe_errors['command_not_found'])}x) — check civic-pipeline uses python3"
        diagnosis["findings"].append(finding)
        print(f"  FINDING: {finding}")

    # ─── Finding: Overlapping runs ───

    if pipe_errors["overlapping_runs"]:
        for overlap in pipe_errors["overlapping_runs"][-3:]:
            finding = f"Overlapping run: {overlap}"
            diagnosis["findings"].append(finding)
            print(f"  FINDING: {finding}")

    # ─── Finding: Slow phases ───

    PHASE_DURATION_WARN = 1200  # 20 min
    slow_phases = [(label, dur, date) for label, dur, date in pipe_errors["phase_durations"]
                   if dur > PHASE_DURATION_WARN]
    if slow_phases:
        for label, dur, date in slow_phases[-5:]:
            finding = f"Slow phase: {label} took {dur/60:.0f}min on {date}"
            diagnosis["findings"].append(finding)
            print(f"  FINDING: {finding}")

    # ─── Finding: Stalled pipeline ───

    if pipe_errors["zero_extraction_runs"] >= 3:
        finding = f"Pipeline produced zero extractions {pipe_errors['zero_extraction_runs']} recent runs"
        diagnosis["findings"].append(finding)
        print(f"  FINDING: {finding}")

        if blockers and auth_status != "broken":
            diagnosis["recommendations"].append(
                "Blocking files have been skipped — next run should resume progress"
            )
        elif auth_status == "broken":
            diagnosis["recommendations"].append(
                "Auth is broken — fix auth first, then pipeline will self-recover"
            )
        else:
            diagnosis["recommendations"].append(
                "No obvious blocker found — check structured-extraction.log manually"
            )

    # ─── Finding: Terminated pipeline ───

    if pipe_errors["terminated_phases"]:
        finding = f"Pipeline was terminated/killed on: {', '.join(pipe_errors['terminated_phases'])}"
        diagnosis["findings"].append(finding)
        print(f"  FINDING: {finding}")

    # ─── Self-evaluation: check if previous fixes worked ───

    if DIAGNOSIS_LOG.exists():
        prev_diagnoses = []
        with open(DIAGNOSIS_LOG) as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        prev_diagnoses.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

        if prev_diagnoses:
            last = prev_diagnoses[-1]
            last_fixes = last.get("fixes_applied", [])
            last_remaining = last.get("stats", {}).get("remaining_files", 0)
            current_remaining = len(remaining)

            if last_fixes:
                delta = last_remaining - current_remaining
                if delta > 0:
                    eval_msg = f"Previous fixes worked: {delta} files processed since last diagnosis"
                    diagnosis["self_eval"] = eval_msg
                    print(f"\n  SELF-EVAL: {eval_msg}")
                elif delta == 0 and last_remaining > 0:
                    eval_msg = "Previous fixes had no effect — still stuck at same count"
                    diagnosis["self_eval"] = eval_msg
                    print(f"\n  SELF-EVAL: {eval_msg}")

                    # Escalate: if we applied fixes and nothing changed, flag for manual review
                    diagnosis["recommendations"].append(
                        "Previous automatic fixes did not help — needs manual investigation"
                    )

    # ─── Summary ───

    print()
    if not diagnosis["findings"]:
        print("  No issues found. Pipeline is healthy.")
        diagnosis["status"] = "healthy"
    elif not diagnosis["fixes_applied"] and not diagnosis["recommendations"]:
        diagnosis["status"] = "issues_found"
    elif diagnosis["fixes_applied"]:
        diagnosis["status"] = "fixes_applied"
        print(f"\n  Applied {len(diagnosis['fixes_applied'])} fix(es).")
    else:
        diagnosis["status"] = "needs_attention"

    if diagnosis["recommendations"]:
        print("\n  Recommendations:")
        for r in diagnosis["recommendations"]:
            print(f"    → {r}")

    # ─── Persist ───

    with open(DIAGNOSIS_LOG, "a") as f:
        f.write(json.dumps(diagnosis, default=str) + "\n")

    print(f"\n  Diagnosis saved to {DIAGNOSIS_LOG}")
    return diagnosis


def show_history():
    """Show diagnosis history."""
    if not DIAGNOSIS_LOG.exists():
        print("No diagnosis history found.")
        return

    with open(DIAGNOSIS_LOG) as f:
        entries = [json.loads(l) for l in f if l.strip()]

    if not entries:
        print("No diagnosis history found.")
        return

    print(f"Pipeline Doctor — {len(entries)} diagnoses\n")
    for e in entries[-10:]:
        ts = e.get("timestamp", "?")[:16]
        status = e.get("status", "?")
        remaining = e.get("stats", {}).get("remaining_files", "?")
        fixes = len(e.get("fixes_applied", []))
        findings = len(e.get("findings", []))
        self_eval = e.get("self_eval", "")

        icon = {"healthy": "OK", "fixes_applied": "FIX", "needs_attention": "WARN",
                "issues_found": "INFO"}.get(status, "?")

        print(f"  [{icon}] {ts}  remaining={remaining}  findings={findings}  fixes={fixes}")
        if self_eval:
            print(f"         eval: {self_eval}")
        for f in e.get("fixes_applied", []):
            print(f"         fix: {f}")
        for r in e.get("recommendations", []):
            print(f"         rec: {r}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Pipeline diagnostics and self-healing")
    parser.add_argument("--dry-run", action="store_true", help="Diagnose only, don't apply fixes")
    parser.add_argument("--history", action="store_true", help="Show diagnosis history")
    args = parser.parse_args()

    if args.history:
        show_history()
    else:
        diagnose(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
