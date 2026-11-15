#!/usr/bin/env python3
"""Collapse the duplicate records that chunked extraction leaves behind.

A document longer than CHUNK_SIZE is extracted one chunk at a time, and each chunk
sees only its own text. An agenda item that spans chunks therefore produces one
record per chunk, and the early ones are not merely redundant, they are wrong: a
chunk that ends before the vote records `outcome: "discussed"` and
`result: "transcript ends before result is stated"`. Querying the record set for
that item then returns a mix of stale and final verdicts with nothing to rank them.

This pass groups the per-item lists by item_id, keeps the most terminal outcome,
unions the flags and stitches the complementary descriptions together, so one item
yields one record. It also drops near-identical legal_flags, which repeat across
chunks because each chunk re-derives the same statutory exposure.

Writes nothing without --write; prints a diff summary either way.
"""
import argparse, json, re, shutil, sys
from pathlib import Path

# Terminal beats provisional. A chunk that saw the vote outranks one that did not.
OUTCOME_RANK = {
    "approved": 3, "denied": 3, "withdrawn": 3, "certified": 3,
    "continued": 2, "tabled": 2, "referred": 2,
    "discussed": 1, "presented": 1, None: 0, "": 0,
}
INDETERMINATE = re.compile(
    r"transcript ends|no vote recorded|before the result|result is not stated|"
    r"ends before", re.I)

_word = re.compile(r"[a-z0-9]+")


def tokens(text):
    return set(_word.findall((text or "").lower()))


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def rank_outcome(value):
    v = (value or "").strip().lower()
    for key, score in OUTCOME_RANK.items():
        if key and v.startswith(key):
            return score
    return 0


def merge_group(records, key_order):
    """Fold records for one item_id into a single record."""
    best = max(records, key=lambda r: rank_outcome(r.get("outcome")))
    out = dict(best)

    # Descriptions from different chunks are complementary, so keep them all, in
    # document order, rather than discarding everything but the longest.
    seen_desc, descs = [], []
    for r in records:
        d = (r.get("description") or "").strip()
        if not d:
            continue
        if any(jaccard(tokens(d), tokens(prev)) > 0.75 for prev in seen_desc):
            continue
        seen_desc.append(d)
        descs.append(d)
    if descs:
        out["description"] = " ".join(descs)

    # Union the list-valued fields; prefer any non-null scalar over a null one.
    for k in key_order:
        vals = [r.get(k) for r in records if r.get(k) not in (None, "", [], {})]
        if not vals:
            continue
        if isinstance(vals[0], list):
            merged, seen = [], set()
            for v in vals:
                for item in v:
                    marker = json.dumps(item, sort_keys=True) if not isinstance(item, str) else item
                    if marker not in seen:
                        seen.add(marker)
                        merged.append(item)
            out[k] = merged
        elif k not in ("description", "outcome") and out.get(k) in (None, ""):
            out[k] = vals[0]

    out["_merged_from"] = len(records)
    return out


def dedupe_votes(votes):
    """Drop a vote whose result was never captured when a sibling captured one."""
    kept, dropped = [], []
    for v in votes:
        if not INDETERMINATE.search(v.get("result") or ""):
            kept.append(v)
            continue
        subject = tokens(v.get("item"))
        if any(jaccard(subject, tokens(k.get("item"))) > 0.35
               and not INDETERMINATE.search(k.get("result") or "")
               for k in votes if k is not v):
            dropped.append(v)
        else:
            kept.append(v)          # nothing else covers it: keep the gap visible
    return kept, dropped


def dedupe_flags(flags, threshold=0.6):
    kept = []
    for f in flags:
        t = tokens(f)
        hit = next((i for i, k in enumerate(kept) if jaccard(t, tokens(k)) >= threshold), None)
        if hit is None:
            kept.append(f)
        elif len(f) > len(kept[hit]):
            kept[hit] = f           # keep whichever states it most fully
    return kept


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="structured JSON file(s)")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    for path in args.paths:
        p = Path(path)
        d = json.loads(p.read_text())
        report = []

        for field in ("housing_items", "fiscal_items"):
            rows = d.get(field) or []
            if not rows or not isinstance(rows[0], dict):
                continue
            groups = {}
            for r in rows:
                groups.setdefault(r.get("item_id") or id(r), []).append(r)
            merged = [merge_group(g, list(rows[0].keys())) if len(g) > 1 else g[0]
                      for g in groups.values()]
            if len(merged) != len(rows):
                report.append(f"  {field}: {len(rows)} -> {len(merged)}")
                for gid, g in groups.items():
                    if len(g) > 1:
                        outs = [r.get("outcome") for r in g]
                        report.append(f"    {gid}: merged {len(g)} ({', '.join(map(str, outs))})")
            d[field] = merged

        votes = d.get("votes") or []
        if votes and isinstance(votes[0], dict):
            kept, dropped = dedupe_votes(votes)
            if dropped:
                report.append(f"  votes: {len(votes)} -> {len(kept)}")
                for v in dropped:
                    report.append(f"    dropped: {v.get('item_id')} ({v.get('result')})")
            d["votes"] = kept

        flags = d.get("legal_flags") or []
        if flags and isinstance(flags[0], str):
            kept = dedupe_flags(flags)
            if len(kept) != len(flags):
                report.append(f"  legal_flags: {len(flags)} -> {len(kept)}")
            d["legal_flags"] = kept

        print(f"{p.name}")
        print("\n".join(report) if report else "  no duplicates")

        if args.write and report:
            shutil.copy2(p, p.with_suffix(".json.predupe"))
            p.write_text(json.dumps(d, indent=1, ensure_ascii=False))
            print(f"  written (backup {p.name}.predupe)")


if __name__ == "__main__":
    main()
