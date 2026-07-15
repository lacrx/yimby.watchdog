#!/usr/bin/env python3
"""
Group building permits into projects and cross-reference entitlements.

Clusters individual building permits into named projects using description
patterns, permit adjacency, and entitlement cross-references. No LLM needed —
pure regex + heuristics.

Usage:
    python permit_projects.py              # rebuild if stale
    python permit_projects.py --force      # force rebuild
    python permit_projects.py --stats      # show project summary

Output: data/structured/permit-projects.json
"""

import argparse
import hashlib
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import load_agencies, agency_data_dir

DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
OUTPUT_FILE = STRUCTURED_DIR / "permit-projects.json"

HOUSING_PERMIT_TYPES = {
    "BLD SFD OR DUPLEX", "BLD MULTI FAMILY", "BLD MID RISE",
    "BLD ACCESSORY DWELLING",
}

PROJECT_PATTERNS = [
    # LOT N - PLAN TYPE X - PROJECT_NAME - NEW SFD
    (r"LOT\s+\d+\s*[-–]\s*PLAN\s+TYPE\s+\w+\s*[-–]\s*([\w\s]+?)\s*[-–]", "lot_plan"),
    # MONARCH/MAGNOLIA/MAHOGANY N RIVER FARMS or NORTH RIVER FARMS
    (r"((?:MONARCH|MAGNOLIA|MAHOGANY|CYPRESS|MONTEREY)\s+(?:N|NORTH)\s+RIVER\s+FARMS)", "river_farms"),
    # MELROSE HEIGHTS PA[1-3] (HARBOR|STRAND|SUNSET)
    (r"MELRO[SE]+\s+H(?:EI|GH)?(?:TS|GHTS)\s+(PA\s*\d+\s*(?:HARBOR|STRAND|SUNSET))", "melrose_heights"),
    # PH N MELROSE... — alternate prefix for same project
    (r"PH\s*\d+\s+MELRO[SE]+\s+H(?:EI|GH)?(?:TS|GHTS)\s+(PA\s*\d+\s*(?:HARBOR|STRAND|SUNSET))", "melrose_heights"),
    # PA N HARBOR/STRAND — Melrose subarea without "MELROSE" prefix
    (r"PA\s*([123])\s*(HARBOR|STRAND|SUNSET)", "melrose_pa_num"),
    # PA 1/PA1 references (Melrose PA1 without subarea)
    (r"(PA\s*1)\s+PHASE", "melrose_pa"),
    # PA 2 — Neptune/Melrose PA2 area
    (r"PA\s*2\s+(?:PLAN|LOT)", "melrose_pa2"),
    # PH N NEPTUNE — Neptune tract
    (r"(NEPTUNE)", "named_project"),
    # MODERA MELROSE
    (r"(MODERA\s+MELROSE)", "named_project"),
    # OLIVE PARK
    (r"(OLIVE\s+PARK)", "named_project"),
    # COAST VILLAS
    (r"(COAST\s+VILLAS)", "named_project"),
    # SANDPIPER VILLA
    (r"(SANDPIPER\s+VILLA)", "named_project"),
    # GREENBRIER VILLAGE
    (r"(GREENBRIER\s+VILLAGE)", "named_project"),
    # SOUTH RIVER VILLAGE
    (r"(SOUTH\s+RIVER\s+VILLAGE)", "named_project"),
    # AVOCADO ROAD tract
    (r"(AVOCADO\s+ROAD)", "named_project"),
]


def normalize_project_name(name):
    name = re.sub(r"\s+", " ", name.strip().upper())
    name = re.sub(r"PA\s*(\d)", r"PA \1", name)
    name = re.sub(r"\bNORTH\b", "N", name)
    return name


def extract_project_name(description):
    """Extract project name from permit description. Returns (name, method) or (None, None)."""
    desc = description.upper().strip()
    for pattern, method in PROJECT_PATTERNS:
        m = re.search(pattern, desc)
        if m:
            if method == "melrose_pa2":
                return "MELROSE HEIGHTS PA 2", method
            if method == "melrose_pa_num":
                return f"MELROSE HEIGHTS PA {m.group(1)} {m.group(2).strip()}", method
            name = normalize_project_name(m.group(1))
            if method in ("melrose_heights", "melrose_pa"):
                name = f"MELROSE HEIGHTS {name}"
            return name, method
    return None, None


def load_all_permits():
    """Load all building permits from all agencies."""
    permits = []
    agencies = load_agencies(enabled_only=True)
    for slug, cfg in agencies.items():
        if "permits" not in cfg:
            continue
        permits_dir = agency_data_dir(slug) / "permits"
        if not permits_dir.exists():
            continue
        for pf in sorted(permits_dir.glob("etrakit-permits-*.jsonl")):
            with open(pf) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                        r["_agency"] = slug
                        permits.append(r)
                    except json.JSONDecodeError:
                        continue
    return permits


def load_entitlements():
    """Load entitlement datasets, return unified index keyed by project_no."""
    index = {}
    for fname in ["etrakit-housing-full.json", "etrakit-approved-projects.json", "etrakit-extra-projects.json"]:
        fpath = DATA_DIR / fname
        if not fpath.exists():
            continue
        try:
            records = json.loads(fpath.read_text())
            if isinstance(records, list):
                for r in records:
                    pno = r.get("project_no", "")
                    if pno and pno not in index:
                        index[pno] = r
        except (json.JSONDecodeError, Exception):
            continue
    return index


def estimate_units(permit):
    """Estimate unit count from a single permit."""
    desc = permit.get("description", "").upper()
    ptype = permit.get("type", "")

    unit_match = re.search(r"(\d+)\s*(?:UNIT|DU|DWELLING|APT|APARTMENT|CONDO|ROOM)", desc)
    if unit_match:
        return int(unit_match.group(1))

    plex_match = re.search(r"(\d+)\s*[-\s]?PLEX", desc)
    if plex_match:
        return int(plex_match.group(1))

    if "TRIPLEX" in desc:
        return 3
    if "DUPLEX" in desc:
        return 2

    if ptype == "BLD ACCESSORY DWELLING":
        return 1
    if ptype == "BLD SFD OR DUPLEX":
        if "DUPLEX" in desc:
            return 2
        return 1

    return 1


def parse_date(date_str):
    """Parse M/D/YYYY or YYYY-MM-DD to YYYY-MM-DD. Returns empty string on failure."""
    if not date_str:
        return ""
    if "/" in date_str:
        parts = date_str.split("/")
        if len(parts) == 3:
            try:
                return f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"
            except ValueError:
                return ""
    return date_str


def group_permits(permits, entitlements):
    """Group permits into projects. Priority: linked_parent > description > adjacency."""
    housing = [p for p in permits if p.get("type", "") in HOUSING_PERMIT_TYPES]

    # Phase 1: Group by shared linked_parent (MASTER permits)
    parent_groups = defaultdict(list)
    no_parent = []
    for p in housing:
        parents = p.get("linked_parent", [])
        if parents:
            parent_groups[parents[0]].append(p)
        else:
            no_parent.append(p)

    # Phase 2: Description pattern matching on remaining permits
    named_groups = defaultdict(list)
    ungrouped = []

    for p in no_parent:
        desc = p.get("description", "")
        name, method = extract_project_name(desc)
        if name:
            named_groups[name].append(p)
        else:
            ungrouped.append(p)

    # Merge parent groups into named groups where description matches
    parent_named = {}
    for parent_no, group in parent_groups.items():
        descs = [p.get("description", "") for p in group]
        name = None
        for desc in descs:
            n, _ = extract_project_name(desc)
            if n:
                name = n
                break
        if not name:
            common = _common_prefix([d[:40] for d in descs]).strip().rstrip("-,")
            name = common if len(common) > 5 else f"PROJECT {parent_no}"
        parent_named[parent_no] = name
        named_groups[name].extend(group)

    # Phase 3: Adjacency grouping for ungrouped non-ADU permits
    adjacency_groups = []
    adu_singles = []
    other_singles = []

    ungrouped.sort(key=lambda p: p.get("permit_no", ""))

    current_group = []
    for p in ungrouped:
        if p.get("type") == "BLD ACCESSORY DWELLING":
            adu_singles.append(p)
            continue

        if not current_group:
            current_group = [p]
            continue

        prev = current_group[-1]
        prev_no = prev.get("permit_no", "")
        curr_no = p.get("permit_no", "")

        try:
            prev_seq = int(prev_no.split("-")[1])
            curr_seq = int(curr_no.split("-")[1])
            same_type = prev.get("type") == p.get("type")
            close = curr_seq - prev_seq <= 5
        except (IndexError, ValueError):
            same_type = False
            close = False

        if same_type and close:
            current_group.append(p)
        else:
            if len(current_group) >= 3:
                adjacency_groups.append(current_group)
            else:
                other_singles.extend(current_group)
            current_group = [p]

    if current_group:
        if len(current_group) >= 3:
            adjacency_groups.append(current_group)
        else:
            other_singles.extend(current_group)

    projects = []

    for name, group_permits_list in named_groups.items():
        source = "linked_parent" if any(p.get("linked_parent") for p in group_permits_list) else "description_pattern"
        projects.append(build_project(name, group_permits_list, source, entitlements))

    for group in adjacency_groups:
        descs = [p.get("description", "")[:30] for p in group]
        common = _common_prefix(descs).strip().rstrip("-,")
        name = common if len(common) > 5 else f"TRACT {group[0].get('permit_no', 'UNKNOWN')}"
        projects.append(build_project(name, group, "adjacency", entitlements))

    if adu_singles:
        projects.append(build_project(
            "ADU (individual permits)",
            adu_singles,
            "type_aggregate",
            entitlements,
        ))

    if other_singles:
        for p in other_singles:
            desc = p.get("description", "")[:50].strip()
            name = desc if desc else p.get("permit_no", "UNKNOWN")
            projects.append(build_project(name, [p], "singleton", entitlements))

    projects.sort(key=lambda p: p["permit_count"], reverse=True)
    return projects


def build_project(name, permits_list, source, entitlements):
    """Build a project record from a group of permits."""
    dates = [parse_date(p.get("applied", "")) for p in permits_list]
    dates = [d for d in dates if d]

    type_counts = Counter(p.get("type", "") for p in permits_list)
    status_counts = Counter(p.get("status", "") for p in permits_list)
    total_units = sum(estimate_units(p) for p in permits_list)

    permit_nos = [p.get("permit_no", "") for p in permits_list]

    master_permits = []
    for p in permits_list:
        for ref in p.get("linked_parent", []):
            if ref and ref not in master_permits:
                master_permits.append(ref)

    # APN from enriched permits
    permit_apns = {p.get("apn", "") for p in permits_list if p.get("apn")}
    best_apn = next(iter(permit_apns), "")

    # Owner from enriched permits
    owners = Counter(p.get("owner", "") for p in permits_list if p.get("owner"))
    primary_owner = owners.most_common(1)[0][0] if owners else ""

    # Entitlement cross-reference: APN → name → empty
    ent_address, ent_apn, ent_ref = _match_entitlement(name, permits_list, entitlements)
    entitlement_refs = []
    if ent_ref:
        entitlement_refs.append(ent_ref)

    project_id = _make_project_id(name)

    result = {
        "project_id": project_id,
        "project_name": name,
        "source": source,
        "permits": permit_nos,
        "permit_count": len(permits_list),
        "estimated_units": total_units,
        "type_breakdown": dict(type_counts.most_common()),
        "status_breakdown": dict(status_counts.most_common()),
        "date_range": {
            "first_applied": min(dates) if dates else "",
            "last_applied": max(dates) if dates else "",
        },
        "master_permits": master_permits,
        "entitlement_refs": entitlement_refs,
        "address": ent_address,
        "apn": ent_apn or best_apn,
    }

    if primary_owner:
        result["owner"] = primary_owner

    return result


def _match_entitlement(project_name, permits_list, entitlements):
    """Match project to entitlement by APN, then by name. Returns (address, apn, entitlement_no)."""
    # APN match — most reliable
    permit_apns = {p.get("apn", "") for p in permits_list if p.get("apn")}
    for apn in permit_apns:
        for pno, ent in entitlements.items():
            if ent.get("apn") == apn:
                return ent.get("address", ""), apn, pno

    # Name match — fuzzy keyword
    norm_name = project_name.upper().replace("-", " ")
    keywords = [w for w in norm_name.split() if len(w) > 3]
    if not keywords:
        return "", "", ""

    best_match = None
    best_score = 0
    best_pno = ""
    for pno, ent in entitlements.items():
        ent_name = (ent.get("project_name", "") or ent.get("description", "")).upper()
        if not ent_name:
            continue
        score = sum(1 for kw in keywords if kw in ent_name)
        if score > best_score and score >= 2:
            best_score = score
            best_match = ent
            best_pno = pno

    if best_match:
        return best_match.get("address", ""), best_match.get("apn", ""), best_pno
    return "", "", ""


def _make_project_id(name):
    """Generate a stable project ID slug."""
    slug = re.sub(r"[^A-Z0-9]+", "-", name.upper()).strip("-")
    if len(slug) > 40:
        slug = slug[:40].rstrip("-")
    return f"PRJ-{slug}"


def _common_prefix(strings):
    """Find common prefix of a list of strings."""
    if not strings:
        return ""
    prefix = strings[0]
    for s in strings[1:]:
        while not s.startswith(prefix) and prefix:
            prefix = prefix[:-1]
    return prefix


def content_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def cmd_build(args):
    """Build or rebuild the permit projects file."""
    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)

    permits = load_all_permits()
    entitlements = load_entitlements()

    housing_count = sum(1 for p in permits if p.get("type", "") in HOUSING_PERMIT_TYPES)
    print(f"Loaded {len(permits)} total permits, {housing_count} housing, {len(entitlements)} entitlements")

    source_hash = content_hash({
        "permit_count": len(permits),
        "housing_count": housing_count,
        "entitlement_count": len(entitlements),
    })

    if not args.force and OUTPUT_FILE.exists():
        existing = json.loads(OUTPUT_FILE.read_text())
        if existing.get("_source_hash") == source_hash:
            print("No changes detected. Use --force to rebuild.")
            return

    projects = group_permits(permits, entitlements)

    output = {
        "projects": projects,
        "_source_hash": source_hash,
        "_permit_count": len(permits),
        "_housing_count": housing_count,
    }

    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nGrouped {housing_count} housing permits into {len(projects)} projects → {OUTPUT_FILE}")

    multi_permit = [p for p in projects if p["permit_count"] > 1 and p["source"] != "type_aggregate"]
    if multi_permit:
        print(f"\nTop multi-permit projects:")
        for p in multi_permit[:15]:
            print(f"  {p['permit_count']:4d} permits  {p['estimated_units']:4d} units  {p['project_name'][:50]}")


def cmd_stats(args):
    """Show project summary statistics."""
    if not OUTPUT_FILE.exists():
        print("No project data. Run permit_projects.py first.")
        return

    data = json.loads(OUTPUT_FILE.read_text())
    projects = data["projects"]

    total_permits = sum(p["permit_count"] for p in projects)
    total_units = sum(p["estimated_units"] for p in projects)

    multi = [p for p in projects if p["permit_count"] > 1 and p["source"] != "type_aggregate"]
    singles = [p for p in projects if p["permit_count"] == 1]
    adu_agg = [p for p in projects if p["source"] == "type_aggregate"]

    print(f"Projects: {len(projects)} ({len(multi)} multi-permit, {len(singles)} singletons, {len(adu_agg)} aggregated)")
    print(f"Permits:  {total_permits}")
    print(f"Units:    {total_units} (estimated)")

    with_addr = sum(1 for p in projects if p.get("address"))
    with_ent = sum(1 for p in projects if p.get("entitlement_refs"))
    print(f"With address: {with_addr}/{len(projects)}")
    print(f"With entitlement refs: {with_ent}/{len(projects)}")

    print(f"\n{'Permits':>8s}  {'Units':>6s}  {'Source':16s}  {'Project Name'}")
    print("-" * 80)
    for p in sorted(projects, key=lambda x: x["permit_count"], reverse=True)[:25]:
        print(f"{p['permit_count']:8d}  {p['estimated_units']:6d}  {p['source']:16s}  {p['project_name'][:45]}")


def main():
    parser = argparse.ArgumentParser(description="Group building permits into projects")
    parser.add_argument("--force", action="store_true", help="Force rebuild")
    parser.add_argument("--stats", action="store_true", help="Show project stats")
    args = parser.parse_args()

    if args.stats:
        cmd_stats(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
