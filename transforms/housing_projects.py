#!/usr/bin/env python3
"""
Build unified housing project entities from four data silos.

Cross-references building permits (clustered by permit_projects.py),
planning projects (eTRAKit), HCD APR filings, and meeting housing_items
into a single entity per named project with lifecycle status, reconciled
unit counts, and match provenance.

Usage:
    python housing_projects.py              # build if stale
    python housing_projects.py --force      # force rebuild
    python housing_projects.py --stats      # show match stats
    python housing_projects.py --dry-run    # build without writing

Output: data/structured/housing-projects.json
"""

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import load_agencies, agency_data_dir

DATA_DIR = REPO_ROOT / "data"
STRUCTURED_DIR = DATA_DIR / "structured"
OUTPUT_FILE = STRUCTURED_DIR / "housing-projects.json"

HOUSING_PLANNING_TYPES = {
    "DEVELOPMENT PLAN", "DENSITY BONUS APPLICATION", "SPECIFIC PLAN",
    "ZONE AMENDMENT", "GENERAL PLAN AMEND", "TENTATIVE PARCEL MAP",
}

PROJECT_REF_RE = re.compile(
    r"\b(?:D|RD|DB|ZA|CUP|RC|RRP|SC|GPA|EXT|TPM|H)\d{2}-\d{4,5}\b"
)


def _normalize_addr(addr):
    if not addr:
        return ""
    addr = addr.upper().strip()
    addr = re.sub(r"\s+", " ", addr)
    for full, abbr in [("STREET", "ST"), ("AVENUE", "AVE"), ("BOULEVARD", "BLVD"),
                       ("DRIVE", "DR"), ("LANE", "LN"), ("COURT", "CT"),
                       ("ROAD", "RD"), ("HIGHWAY", "HWY"), ("NORTH", "N"),
                       ("SOUTH", "S"), ("EAST", "E"), ("WEST", "W")]:
        addr = re.sub(rf"\b{full}\b", abbr, addr)
    addr = re.sub(r"[#,.]", "", addr)
    return addr.strip()


def _extract_street_number(addr):
    m = re.match(r"(\d+)", addr)
    return m.group(1) if m else ""


def _content_hash(*data):
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


def _extract_units_from_text(text):
    if not text:
        return None
    m = re.search(r"(\d+)\s*(?:UNIT|DU|DWELLING|APT|APARTMENT|CONDO|ROOM)", text.upper())
    if m:
        return int(m.group(1))
    m = re.search(r"(\d+)\s*[-\s]?PLEX", text.upper())
    if m:
        return int(m.group(1))
    return None


# ── Loaders ──

def load_permit_projects():
    if not (STRUCTURED_DIR / "permit-projects.json").exists():
        return []
    data = json.loads((STRUCTURED_DIR / "permit-projects.json").read_text())
    return data.get("projects", [])


def load_planning_projects():
    projects = []
    agencies = load_agencies()
    for slug, agency in agencies.items():
        if not agency.get("permits"):
            continue
        data_dir = agency_data_dir(slug)
        permits_dir = Path(data_dir) / "permits"
        if not permits_dir.exists():
            continue
        for jf in sorted(permits_dir.glob("etrakit-projects-*.jsonl")):
            for line in jf.read_text().splitlines():
                if line.strip():
                    rec = json.loads(line)
                    rec["_agency"] = slug
                    rec["_year"] = jf.stem.split("-")[-1]
                    projects.append(rec)
    return projects


def load_apr_filings():
    apr_file = DATA_DIR / "reference" / "hcd-apr-oceanside.json"
    if not apr_file.exists():
        return []
    return json.loads(apr_file.read_text())


def load_meeting_housing():
    items = []
    combined = STRUCTURED_DIR / "meetings-combined.jsonl"
    if not combined.exists():
        return items
    for line in combined.read_text().splitlines():
        if not line.strip():
            continue
        meeting = json.loads(line)
        mid = meeting.get("meeting_id", "")
        date = meeting.get("date", "")
        body = meeting.get("body", "")
        agency = meeting.get("agency", "")
        for h in meeting.get("housing_items", []):
            items.append({
                "meeting_id": mid,
                "date": date,
                "body": body,
                "agency": agency,
                **h,
            })
    return items


# ── Indexes ──

def build_planning_index(planning_projects):
    by_apn = defaultdict(list)
    by_addr = defaultdict(list)
    by_no = {}
    for p in planning_projects:
        pno = p.get("project_no", "")
        if pno:
            by_no[pno] = p
        apn = (p.get("apn") or "").strip()
        if apn:
            by_apn[apn].append(p)
        addr = _normalize_addr(p.get("address", ""))
        if addr:
            street_num = _extract_street_number(addr)
            if street_num:
                by_addr[street_num].append((addr, p))
    return by_apn, by_addr, by_no


def build_apr_index(apr_filings):
    by_apn = defaultdict(list)
    by_permit = {}
    for rec in apr_filings:
        apn = (rec.get("apn") or "").strip()
        if apn:
            by_apn[apn].append(rec)
        for pm in rec.get("etrakit_permits", []):
            pno = pm.get("permit_no", "")
            if pno:
                by_permit[pno] = rec
    return by_apn, by_permit


def build_meeting_index(meeting_items):
    by_ref = defaultdict(list)
    by_addr = defaultdict(list)
    for item in meeting_items:
        desc = item.get("description", "")
        for ref in PROJECT_REF_RE.findall(desc):
            by_ref[ref].append(item)
        addr = _normalize_addr(item.get("address") or "")
        if addr:
            street_num = _extract_street_number(addr)
            if street_num:
                by_addr[street_num].append((addr, item))
    return by_ref, by_addr


# ── Matchers ──

def match_planning(project, plan_by_apn, plan_by_addr, plan_by_no):
    matched = []
    seen = set()

    apn = (project.get("apn") or "").strip()
    if apn and apn in plan_by_apn:
        for p in plan_by_apn[apn]:
            pno = p.get("project_no", "")
            if pno not in seen:
                matched.append((p, "apn"))
                seen.add(pno)

    for ref in project.get("entitlement_refs", []):
        if ref in plan_by_no and ref not in seen:
            matched.append((plan_by_no[ref], "entitlement_ref"))
            seen.add(ref)

    addr = _normalize_addr(project.get("address", ""))
    if addr:
        street_num = _extract_street_number(addr)
        if street_num and street_num in plan_by_addr:
            for plan_addr, p in plan_by_addr[street_num]:
                pno = p.get("project_no", "")
                if pno not in seen and _addr_match(addr, plan_addr):
                    matched.append((p, "address"))
                    seen.add(pno)

    name = project.get("project_name", "").upper()
    if name and len(name) > 5:
        name_words = set(w for w in re.findall(r"\w+", name) if len(w) > 3)
        if len(name_words) >= 2:
            for pno, p in plan_by_no.items():
                if pno in seen:
                    continue
                p_desc = (p.get("description") or p.get("name") or "").upper()
                p_words = set(w for w in re.findall(r"\w+", p_desc) if len(w) > 3)
                overlap = name_words & p_words
                if len(overlap) >= 2:
                    matched.append((p, "name_fuzzy"))
                    seen.add(pno)

    return matched


def match_apr(project, apr_by_apn, apr_by_permit):
    matched = []
    seen = set()

    for permit_no in project.get("permits", []):
        if permit_no in apr_by_permit:
            rec = apr_by_permit[permit_no]
            key = (rec.get("year"), rec.get("apn"))
            if key not in seen:
                matched.append((rec, "permit_crossref"))
                seen.add(key)

    apn = (project.get("apn") or "").strip()
    if apn and apn in apr_by_apn:
        for rec in apr_by_apn[apn]:
            key = (rec.get("year"), rec.get("apn"))
            if key not in seen:
                matched.append((rec, "apn"))
                seen.add(key)

    return matched


def match_meetings(project, planning_nos, mtg_by_ref, mtg_by_addr):
    matched = []
    seen = set()

    for pno in planning_nos:
        if pno in mtg_by_ref:
            for item in mtg_by_ref[pno]:
                key = (item["meeting_id"], item.get("description", "")[:80])
                if key not in seen:
                    matched.append((item, "project_ref"))
                    seen.add(key)

    addr = _normalize_addr(project.get("address", ""))
    if addr:
        street_num = _extract_street_number(addr)
        if street_num and street_num in mtg_by_addr:
            for mtg_addr, item in mtg_by_addr[street_num]:
                key = (item["meeting_id"], item.get("description", "")[:80])
                if key not in seen and _addr_match(addr, mtg_addr):
                    matched.append((item, "address"))
                    seen.add(key)

    name = project.get("project_name", "").upper()
    if name and len(name) > 5:
        name_words = [w for w in re.findall(r"\w+", name) if len(w) > 3]
        if len(name_words) >= 2:
            for ref_items in mtg_by_ref.values():
                for item in ref_items:
                    key = (item["meeting_id"], item.get("description", "")[:80])
                    if key in seen:
                        continue
                    desc = (item.get("description") or "").upper()
                    if sum(1 for w in name_words if w in desc) >= 2:
                        matched.append((item, "name_fuzzy"))
                        seen.add(key)

    return matched


def _addr_match(a, b):
    a_num = _extract_street_number(a)
    b_num = _extract_street_number(b)
    if not a_num or a_num != b_num:
        return False
    a_words = set(re.findall(r"[A-Z]+", a)) - {"ST", "AVE", "BLVD", "DR", "LN", "CT", "RD", "HWY", "N", "S", "E", "W", "CA"}
    b_words = set(re.findall(r"[A-Z]+", b)) - {"ST", "AVE", "BLVD", "DR", "LN", "CT", "RD", "HWY", "N", "S", "E", "W", "CA"}
    if not a_words or not b_words:
        return False
    return len(a_words & b_words) >= 1


# ── Status & Units ──

def derive_status(permit_statuses, planning_statuses):
    if not permit_statuses and not planning_statuses:
        return "planning"
    if permit_statuses:
        s = set(permit_statuses)
        if "FINALED" in s:
            return "built"
        if "ISSUED" in s or "TEMP CERT OF OCC" in s:
            return "under_construction"
        if s & {"APPROVED", "RECEIVED", "PLAN CHECK", "IN REVIEW"}:
            return "permitted"
        if s == {"EXPIRED"}:
            return "expired"
        return "permitted"
    if any("APPROVED" in (s or "").upper() for s in planning_statuses):
        return "entitled"
    return "planning"


def reconcile_units(apr_filings, planning_projects, permit_estimated):
    apr_proposed = 0
    apr_approved = 0
    for rec in apr_filings:
        apr_proposed += rec.get("tot_proposed_units") or 0
        apr_approved += rec.get("tot_approved_units") or 0

    planning_units = None
    for p in planning_projects:
        u = _extract_units_from_text(p.get("description") or p.get("name") or "")
        if u:
            planning_units = (planning_units or 0) + u

    if apr_approved and apr_approved > 0:
        best, source = apr_approved, "apr"
    elif apr_proposed and apr_proposed > 0:
        best, source = apr_proposed, "apr"
    elif planning_units and planning_units > 0:
        best, source = planning_units, "planning"
    else:
        best, source = permit_estimated or 0, "permits"

    return {
        "best": best,
        "best_source": source,
        "apr_proposed": apr_proposed or None,
        "apr_approved": apr_approved or None,
        "permit_estimated": permit_estimated or None,
        "planning_units": planning_units,
    }


def income_tiers_from_apr(apr_filings):
    if not apr_filings:
        return None
    tiers = {"very_low": 0, "low": 0, "moderate": 0, "above_moderate": 0}
    for rec in apr_filings:
        tiers["very_low"] += (rec.get("vlow_income_dr") or 0) + (rec.get("vlow_income_ndr") or 0)
        tiers["low"] += (rec.get("low_income_dr") or 0) + (rec.get("low_income_ndr") or 0)
        tiers["moderate"] += (rec.get("mod_income_dr") or 0) + (rec.get("mod_income_ndr") or 0)
        tiers["above_moderate"] += rec.get("above_mod_income") or 0
    if sum(tiers.values()) == 0:
        return None
    return tiers


# ── Builder ──

def build_all():
    permit_projects = load_permit_projects()
    planning_projects = load_planning_projects()
    apr_filings = load_apr_filings()
    meeting_items = load_meeting_housing()

    plan_by_apn, plan_by_addr, plan_by_no = build_planning_index(planning_projects)
    apr_by_apn, apr_by_permit = build_apr_index(apr_filings)
    mtg_by_ref, mtg_by_addr = build_meeting_index(meeting_items)

    housing = []
    matched_planning_nos = set()

    for pp in permit_projects:
        if pp.get("source") == "type_aggregate":
            housing.append(_passthrough_aggregate(pp))
            continue

        plan_matches = match_planning(pp, plan_by_apn, plan_by_addr, plan_by_no)
        planning_nos = [p.get("project_no", "") for p, _ in plan_matches]
        matched_planning_nos.update(planning_nos)

        apr_matches = match_apr(pp, apr_by_apn, apr_by_permit)
        mtg_matches = match_meetings(pp, planning_nos, mtg_by_ref, mtg_by_addr)

        plan_statuses = [p.get("status", "") for p, _ in plan_matches]
        permit_statuses = []
        for status, count in (pp.get("status_breakdown") or {}).items():
            permit_statuses.extend([status] * count)

        units = reconcile_units(
            [r for r, _ in apr_matches],
            [p for p, _ in plan_matches],
            pp.get("estimated_units"),
        )

        dates = _collect_dates(pp, plan_matches, apr_matches, mtg_matches)
        location = _location_from_sources(pp, apr_matches, plan_matches)

        hp = {
            "project_id": pp["project_id"].replace("PRJ-", "HP-"),
            "project_name": pp["project_name"],
            "agency": "oceanside",
            "address": pp.get("address") or location.get("address") or "",
            "apn": pp.get("apn") or "",
            "latitude": location.get("latitude"),
            "longitude": location.get("longitude"),
            "is_downtown": location.get("is_downtown", False),
            "units": units,
            "income_tiers": income_tiers_from_apr([r for r, _ in apr_matches]),
            "status": derive_status(permit_statuses, plan_statuses),
            "density_bonus": any(
                (r.get("density_bonus_received") or "").lower() == "yes"
                for r, _ in apr_matches
            ),
            "sb35": any(
                (r.get("app_submitted_sb35") or "").lower() == "yes"
                for r, _ in apr_matches
            ),
            "first_activity": dates.get("first"),
            "last_activity": dates.get("last"),
            "sources": {
                "permit_project": {
                    "project_id": pp["project_id"],
                    "permit_count": pp.get("permit_count", 0),
                    "status_breakdown": pp.get("status_breakdown", {}),
                },
                "planning_projects": [
                    {"project_no": p.get("project_no", ""), "type": p.get("type", ""),
                     "status": p.get("status", ""), "match_method": method}
                    for p, method in plan_matches
                ],
                "apr_filings": [
                    {"year": r.get("year"), "tracking_id": r.get("jurs_tracking_id", ""),
                     "tot_proposed_units": r.get("tot_proposed_units", 0),
                     "match_method": method}
                    for r, method in apr_matches
                ],
                "meeting_mentions": [
                    {"meeting_id": item["meeting_id"], "date": item.get("date", ""),
                     "type": item.get("type", ""), "match_method": method}
                    for item, method in mtg_matches
                ],
            },
        }
        housing.append(hp)

    # Planning-only projects (no permits yet)
    # Cluster by APN so D + DB + RD on same parcel become one entity
    plan_clusters = defaultdict(list)
    for p in planning_projects:
        pno = p.get("project_no", "")
        if pno in matched_planning_nos:
            continue
        ptype = (p.get("type") or "").upper()
        if not any(ht in ptype for ht in HOUSING_PLANNING_TYPES):
            continue
        apn = (p.get("apn") or "").strip()
        cluster_key = apn if apn else pno
        plan_clusters[cluster_key].append(p)

    for cluster_key, cluster in plan_clusters.items():
        all_nos = [p.get("project_no", "") for p in cluster]
        matched_planning_nos.update(all_nos)

        best_name = ""
        best_addr = ""
        best_apn = ""
        has_db = False
        has_approved = False
        all_applied = []
        all_approved = []
        for p in cluster:
            desc = p.get("description") or p.get("name") or ""
            if len(desc) > len(best_name):
                best_name = desc
            if not best_addr and p.get("address"):
                best_addr = p["address"]
            if not best_apn and p.get("apn"):
                best_apn = p["apn"]
            if "DENSITY BONUS" in (p.get("type") or "").upper():
                has_db = True
            if "APPROVED" in (p.get("status") or "").upper():
                has_approved = True
            if p.get("applied"):
                all_applied.append(p["applied"])
            if p.get("approved"):
                all_approved.append(p["approved"])

        apr_matches = []
        if best_apn and best_apn in apr_by_apn:
            apr_matches = [(r, "apn") for r in apr_by_apn[best_apn]]

        mtg_matches = match_meetings(
            {"address": best_addr, "project_name": best_name or all_nos[0]},
            all_nos, mtg_by_ref, mtg_by_addr,
        )

        planning_units = None
        for p in cluster:
            u = _extract_units_from_text(p.get("description") or p.get("name") or "")
            if u and (planning_units is None or u > planning_units):
                planning_units = u

        units = reconcile_units(
            [r for r, _ in apr_matches], cluster, planning_units,
        )

        slug = re.sub(r"[^A-Z0-9]+", "-", all_nos[0].upper()).strip("-")

        hp = {
            "project_id": f"HP-PLAN-{slug}",
            "project_name": best_name or all_nos[0],
            "agency": cluster[0].get("_agency", "oceanside"),
            "address": best_addr,
            "apn": best_apn,
            "latitude": None,
            "longitude": None,
            "is_downtown": False,
            "units": units,
            "income_tiers": income_tiers_from_apr([r for r, _ in apr_matches]),
            "status": "entitled" if has_approved else "planning",
            "density_bonus": has_db,
            "sb35": False,
            "first_activity": min(all_applied) if all_applied else None,
            "last_activity": max(all_approved + all_applied) if (all_approved or all_applied) else None,
            "sources": {
                "permit_project": None,
                "planning_projects": [
                    {"project_no": p.get("project_no", ""), "type": p.get("type", ""),
                     "status": p.get("status", ""), "match_method": "primary"}
                    for p in cluster
                ],
                "apr_filings": [
                    {"year": r.get("year"), "tracking_id": r.get("jurs_tracking_id", ""),
                     "tot_proposed_units": r.get("tot_proposed_units", 0),
                     "match_method": method}
                    for r, method in apr_matches
                ],
                "meeting_mentions": [
                    {"meeting_id": item["meeting_id"], "date": item.get("date", ""),
                     "type": item.get("type", ""), "match_method": method}
                    for item, method in mtg_matches
                ],
            },
        }
        housing.append(hp)

    housing.sort(key=lambda h: -(h["units"]["best"] or 0))

    counts = {
        "total_projects": len(housing),
        "with_permits": sum(1 for h in housing if h["sources"]["permit_project"]),
        "with_planning": sum(1 for h in housing if h["sources"]["planning_projects"]),
        "with_apr": sum(1 for h in housing if h["sources"]["apr_filings"]),
        "with_meetings": sum(1 for h in housing if h["sources"]["meeting_mentions"]),
    }

    return {
        "projects": housing,
        "_source_hash": _content_hash(housing),
        "_counts": counts,
    }


def _passthrough_aggregate(pp):
    return {
        "project_id": pp["project_id"].replace("PRJ-", "HP-"),
        "project_name": pp["project_name"],
        "agency": "oceanside",
        "address": "",
        "apn": "",
        "latitude": None,
        "longitude": None,
        "is_downtown": False,
        "units": {
            "best": pp.get("estimated_units", 0),
            "best_source": "permits",
            "apr_proposed": None,
            "apr_approved": None,
            "permit_estimated": pp.get("estimated_units", 0),
            "planning_units": None,
        },
        "income_tiers": None,
        "status": "aggregate",
        "density_bonus": False,
        "sb35": False,
        "first_activity": (pp.get("date_range") or {}).get("first_applied"),
        "last_activity": (pp.get("date_range") or {}).get("last_applied"),
        "sources": {
            "permit_project": {
                "project_id": pp["project_id"],
                "permit_count": pp.get("permit_count", 0),
                "status_breakdown": pp.get("status_breakdown", {}),
            },
            "planning_projects": [],
            "apr_filings": [],
            "meeting_mentions": [],
        },
    }


def _collect_dates(pp, plan_matches, apr_matches, mtg_matches):
    dates = []
    dr = pp.get("date_range") or {}
    if dr.get("first_applied"):
        dates.append(dr["first_applied"])
    if dr.get("last_applied"):
        dates.append(dr["last_applied"])
    for p, _ in plan_matches:
        if p.get("applied"):
            dates.append(p["applied"])
        if p.get("approved"):
            dates.append(p["approved"])
    for r, _ in apr_matches:
        if r.get("app_submit_dt"):
            dates.append(r["app_submit_dt"])
    for item, _ in mtg_matches:
        if item.get("date"):
            dates.append(item["date"])

    valid = []
    for d in dates:
        try:
            if "/" in str(d):
                parts = str(d).split("/")
                if len(parts) == 3:
                    d = f"{int(parts[2]):04d}-{int(parts[0]):02d}-{int(parts[1]):02d}"
            if len(str(d)) >= 10:
                valid.append(str(d)[:10])
        except (ValueError, IndexError):
            continue

    valid.sort()
    return {"first": valid[0] if valid else None, "last": valid[-1] if valid else None}


def _location_from_sources(pp, apr_matches, plan_matches):
    loc = {"address": pp.get("address") or "", "latitude": None, "longitude": None, "is_downtown": False}

    for rec, _ in apr_matches:
        if rec.get("latitude") and rec.get("longitude"):
            loc["latitude"] = rec["latitude"]
            loc["longitude"] = rec["longitude"]
        if rec.get("is_downtown"):
            loc["is_downtown"] = True
        if not loc["address"] and rec.get("street_address"):
            loc["address"] = rec["street_address"]
        break

    if not loc["address"]:
        for p, _ in plan_matches:
            if p.get("address"):
                loc["address"] = p["address"]
                break

    return loc


# ── CLI ──

def print_stats(data):
    projects = data["projects"]
    counts = data["_counts"]

    print(f"\n{'='*60}")
    print(f"Housing Projects: {counts['total_projects']}")
    print(f"  With permits:   {counts['with_permits']}")
    print(f"  With planning:  {counts['with_planning']}")
    print(f"  With APR:       {counts['with_apr']}")
    print(f"  With meetings:  {counts['with_meetings']}")

    by_status = defaultdict(int)
    total_units = 0
    for p in projects:
        by_status[p["status"]] += 1
        total_units += p["units"]["best"] or 0

    print(f"\nTotal units: {total_units:,}")
    print(f"\nBy status:")
    for status in ["built", "under_construction", "permitted", "entitled", "planning", "expired", "aggregate"]:
        if status in by_status:
            print(f"  {status:25s} {by_status[status]:4d}")

    print(f"\nTop 10 by units:")
    for p in projects[:10]:
        src = p["units"]["best_source"]
        plan = len(p["sources"]["planning_projects"])
        apr = len(p["sources"]["apr_filings"])
        mtg = len(p["sources"]["meeting_mentions"])
        print(f"  {p['units']['best']:5d} units  {p['status']:20s}  "
              f"plan={plan} apr={apr} mtg={mtg}  {p['project_name'][:40]}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Build unified housing projects")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    data = build_all()

    if args.stats or args.dry_run:
        print_stats(data)

    if args.dry_run:
        print("Dry run — not writing output.")
        return

    if not args.force and OUTPUT_FILE.exists():
        existing = json.loads(OUTPUT_FILE.read_text())
        if existing.get("_source_hash") == data["_source_hash"]:
            if args.stats:
                return
            print("No changes detected. Use --force to rebuild.")
            return

    STRUCTURED_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(data, indent=2, default=str))
    print(f"Wrote {len(data['projects'])} housing projects to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
