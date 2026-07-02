#!/usr/bin/env python3
"""Rule-based triage filter for meeting documents.

Predicts whether a document contains substantive civic proceedings worth
sending to LLM extraction, or is purely procedural/administrative.

This filter is topic-neutral — it does not favor housing, fiscal, or any
other policy domain. Domain-specific interpretation belongs in yimby.analysis.

Usage:
    from transforms.triage import should_extract

    if not should_extract(text, filename):
        print("skipping — triage says procedural-only")
"""

import re

PROCEDURAL_KW = [
    "consent calendar", "roll call", "pledge of allegiance", "invocation",
    "approval of minutes", "adjournment", "public comment period",
    "ceremonial", "proclamation", "presentation", "recognition",
    "warrant register", "treasurer report", "city manager report",
    "moment of silence", "flag salute",
]

SUBSTANTIVE_KW = [
    "motion", "second", "vote", "aye", "nay", "approved", "denied",
    "continued", "tabled", "public hearing", "ordinance", "resolution",
    "zoning", "permit", "variance", "conditional use", "general plan",
    "specific plan", "ceqa", "eir", "subdivision", "development",
    "budget", "bond", "tax", "fee", "assessment", "grant",
    "appropriat", "revenue", "expenditure", "contract",
    "violation", "enforcement", "litigation", "compliance",
    "penalty", "lawsuit", "state law", "appeal",
    "housing", "density", "affordable", "rezone",
    "infrastructure", "traffic", "water", "sewer", "utilities",
    "public safety", "fire", "police", "emergency",
    "park", "recreation", "library", "transit",
]

SKIP_FILENAME_PATTERNS = [
    r"bid.?tabulation",
    r"warrant.?register",
    r"check.?register",
    r"treasurer.?report",
    r"vendor.?list",
]

MIN_TEXT_LENGTH = 200


def predict_relevance(text, filename=""):
    """Return (should_extract, confidence) tuple.

    confidence is a float 0-1 indicating how substantive the document appears.
    """
    if len(text.strip()) < MIN_TEXT_LENGTH:
        return False, 0.0

    fn_lower = filename.lower()
    for pat in SKIP_FILENAME_PATTERNS:
        if re.search(pat, fn_lower):
            return False, 0.0

    text_lower = text[:8000].lower()
    denom = max(len(text), 1) / 1000

    proc_hits = sum(text_lower.count(kw) for kw in PROCEDURAL_KW)
    proc_unique = sum(1 for kw in PROCEDURAL_KW if kw in text_lower)

    subst_hits = sum(text_lower.count(kw) for kw in SUBSTANTIVE_KW)
    subst_unique = sum(1 for kw in SUBSTANTIVE_KW if kw in text_lower)

    has_vote_pattern = bool(re.search(r'(aye|yea|nay|yes|no)\s*[-:]\s*\d', text_lower))
    has_item_numbers = bool(re.search(r'item\s+\d+', text_lower))

    is_staff_report = any(x in fn_lower for x in ["staff_report", "staff-report"])
    is_agenda = "agenda" in fn_lower
    is_minutes = "minutes" in fn_lower

    if is_staff_report or has_vote_pattern:
        return True, 0.9

    if subst_unique == 0 and proc_unique >= 3:
        return False, 0.05

    subst_density = subst_hits / denom
    proc_density = proc_hits / denom

    if subst_density < 0.5 and proc_density > subst_density * 3 and not is_minutes:
        return False, 0.1

    if subst_unique >= 2 or has_item_numbers or is_agenda:
        confidence = min(0.95, 0.3 + subst_unique * 0.05 + subst_density * 0.1)
        return True, confidence

    if is_minutes and subst_unique >= 1:
        return True, 0.5

    if subst_hits > 0:
        return True, 0.3

    return False, 0.1


def should_extract(text, filename=""):
    """Returns True if document should be sent to LLM extraction."""
    extract, _ = predict_relevance(text, filename)
    return extract


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            p = Path(path)
            if p.exists():
                text = p.read_text()
                extract, conf = predict_relevance(text, p.name)
                verdict = "EXTRACT" if extract else "SKIP"
                print(f"  {p.name}: {verdict} (confidence={conf:.2f})")
    else:
        print("Usage: python triage.py <file1.txt> [file2.txt ...]")
