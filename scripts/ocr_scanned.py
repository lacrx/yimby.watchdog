#!/usr/bin/env python3
"""
OCR scanned image PDFs that pdftotext couldn't handle.

Uses ocrmypdf + tesseract to extract text from scanned documents.
Only targets docs whose .skip marker says too_short or garbled_pdf. Never replaces
an existing .txt unless the OCR text is longer. Only too_short markers are removed.

Tools are located with shutil.which. Set OCR_TOOLS_PATH (os.pathsep-separated dirs)
to prepend extra directories to PATH.

Usage:
    python scripts/ocr_scanned.py --year 2022 2023
    python scripts/ocr_scanned.py --year 2022 --dry-run
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import agency_data_dir

DATA_DIR = agency_data_dir("oceanside")
DOC_DIR = DATA_DIR / "documents"
STRUCTURED_DIR = REPO_ROOT / "data" / "structured"

OCR_TOOLS_PATH = os.environ.get("OCR_TOOLS_PATH", "")
OCR_REASONS = ("too_short", "garbled_pdf")
CLEAR_REASONS = ("too_short",)   # only these markers are removed after OCR
MIN_CLEAR_CHARS = 100            # matches extract_structured.MIN_DOC_CHARS
BASE_TIMEOUT = 300
PER_PAGE_TIMEOUT = 30
MAX_TIMEOUT = 3600


def tool_env():
    path = os.environ.get("PATH", "")
    if OCR_TOOLS_PATH:
        path = f"{OCR_TOOLS_PATH}{os.pathsep}{path}"
    return {**os.environ, "PATH": path}


def check_tools(env):
    """Return a list of missing tool names (ocrmypdf is run via this interpreter)."""
    missing = [t for t in ("tesseract",) if not shutil.which(t, path=env["PATH"])]
    try:
        import ocrmypdf  # noqa: F401
    except ImportError:
        missing.append("ocrmypdf (python module)")
    return missing


def page_count(pdf_path, env):
    """Page count via pdfinfo; None if unavailable."""
    pdfinfo = shutil.which("pdfinfo", path=env["PATH"])
    if not pdfinfo:
        return None
    try:
        out = subprocess.run([pdfinfo, str(pdf_path)], capture_output=True,
                             text=True, timeout=30, env=env).stdout
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in out.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def ocr_timeout(pages, pdf_path):
    if pages is None:   # fall back to ~1 page per 50KB
        pages = max(1, pdf_path.stat().st_size // 50_000)
    return min(MAX_TIMEOUT, max(BASE_TIMEOUT, PER_PAGE_TIMEOUT * pages))


def skip_reason(fname):
    """Reason from DOC_DIR/<fname>.skip, or None if absent/unreadable."""
    sp = DOC_DIR / (fname + ".skip")
    if not sp.exists():
        return None
    try:
        return json.loads(sp.read_text()).get("reason", "unknown")
    except (json.JSONDecodeError, OSError, AttributeError):
        return "unknown"


def find_unextracted(years):
    idx_path = DATA_DIR / "doc-index.json"
    idx = json.loads(idx_path.read_text()).get("documents", {})
    docs = []
    for fname, meta in sorted(idx.items()):
        d = meta.get("meeting_date", "")
        if not d or not any(d.startswith(y) for y in years):
            continue
        stem = fname[:-4] if fname.endswith(".txt") else fname
        if (STRUCTURED_DIR / (stem + ".json")).exists():
            continue
        reason = skip_reason(fname)
        if reason not in OCR_REASONS:
            continue
        pdf_path = DOC_DIR / (stem + ".pdf")
        if not pdf_path.exists():
            continue
        docs.append({"fname": fname, "stem": stem, "pdf": pdf_path, "date": d,
                     "reason": reason})
    return docs


def ocr_pdf(pdf_path, txt_path):
    env = tool_env()
    timeout = ocr_timeout(page_count(pdf_path, env), pdf_path)
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp_pdf, \
         tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp_txt:
        tmp_pdf_path = tmp_pdf.name
        tmp_txt_path = tmp_txt.name

    try:
        result = subprocess.run(
            [sys.executable, "-m", "ocrmypdf",
             "--force-ocr", "--sidecar", tmp_txt_path,
             "--skip-big", "50",
             str(pdf_path), tmp_pdf_path],
            capture_output=True, text=True, timeout=timeout, env=env
        )
        if result.returncode != 0:
            return None, result.stderr[:200]

        if os.path.exists(tmp_txt_path):
            text = open(tmp_txt_path).read().strip()
            if len(text) < 50:
                return None, f"too short ({len(text)} chars)"
            existing = 0
            if txt_path.exists():
                existing = len(txt_path.read_text(errors="replace").strip())
            if len(text) <= existing:
                return None, f"OCR text ({len(text)}) not longer than existing ({existing})"
            txt_path.write_text(text)
            return len(text), None
        return None, "no sidecar output"
    except subprocess.TimeoutExpired:
        return None, f"timeout ({timeout}s)"
    finally:
        for f in [tmp_pdf_path, tmp_txt_path]:
            if os.path.exists(f):
                os.unlink(f)


def main():
    parser = argparse.ArgumentParser(description="OCR scanned PDFs")
    parser.add_argument("--year", nargs="+", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    docs = find_unextracted(args.year)
    print(f"Found {len(docs)} skipped docs ({'/'.join(OCR_REASONS)}) for years: {args.year}")

    if args.dry_run:
        for d in docs:
            print(f"  {d['fname']} [{d['reason']}] ({d['pdf'].stat().st_size // 1024}KB)")
        return

    missing = check_tools(tool_env())
    if missing:
        print(f"ERROR: missing tools: {', '.join(missing)} (set OCR_TOOLS_PATH to add a PATH dir)")
        sys.exit(1)

    ok = 0
    fail = 0
    skip_cleared = 0

    for i, doc in enumerate(docs, 1):
        fname = doc["fname"]
        txt_path = DOC_DIR / fname
        skip_path = DOC_DIR / (fname + ".skip")

        print(f"[{i}/{len(docs)}] {fname}...", end=" ", flush=True)

        chars, err = ocr_pdf(doc["pdf"], txt_path)
        if chars:
            print(f"{chars} chars")
            ok += 1
            if doc["reason"] in CLEAR_REASONS and chars >= MIN_CLEAR_CHARS \
                    and skip_path.exists():
                skip_path.unlink()
                skip_cleared += 1
        else:
            print(f"FAIL: {err}")
            fail += 1

    print(f"\nDone: {ok} OCR'd, {fail} failed, {skip_cleared} skip markers cleared")
    if ok:
        print("Run: python transforms/extract_structured.py")


if __name__ == "__main__":
    main()
