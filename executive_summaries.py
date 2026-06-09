#!/usr/bin/env python3
"""Generate executive summaries by year from monthly digest JSONL.

Usage:
    python executive_summaries.py                              # claude -p + monthly digests ($0)
    python executive_summaries.py --mode api                   # Claude API + monthly digests ($)
    python executive_summaries.py --source jsonl               # per-record JSONL (more detail, more tokens)
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ENV_FILE = Path(__file__).parent / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from civic_utils import claude_local_call

DATA_DIR = Path(__file__).parent / "data"
MEETINGS_DIR = DATA_DIR / "meetings"
SUMMARIES_DIR = DATA_DIR / "summaries"
STRUCTURED_DIR = DATA_DIR / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
MONTHLY_DIR = STRUCTURED_DIR / "monthly"
OUTPUT_DIR = DATA_DIR / "executive-summaries"

SKILLS_DIR = Path(__file__).parent / ".claude" / "skills"
SKILL_NAMES = ["policy-analysis", "ca-housing-law"]

MODE = "api"  # set by main()
client = None  # initialized only in API mode


def load_skills():
    parts = []
    for name in SKILL_NAMES:
        path = SKILLS_DIR / name / "SKILL.md"
        if path.exists():
            parts.append(path.read_text())
        supplement = SKILLS_DIR / name / "recent-developments.md"
        if supplement.exists():
            parts.append(supplement.read_text())
    return "\n\n---\n\n".join(parts)


SKILLS_CONTEXT = load_skills()


def call_claude(prompt, max_tokens=2000):
    """Route to API or local based on MODE."""
    if MODE == "local":
        return claude_local_call(prompt, system=SKILLS_CONTEXT, timeout=600)
    else:
        from civic_utils import claude_api_call
        response = claude_api_call(
            client,
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=SKILLS_CONTEXT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


def load_summaries_by_year():
    """Load prose summaries grouped by year and body."""
    meeting_info = {}
    for mdir in MEETINGS_DIR.iterdir():
        mf = mdir / "meeting.json"
        if not mf.exists():
            continue
        m = json.loads(mf.read_text())
        date = m.get("date", "")
        body = m.get("body", m.get("EventBodyName", ""))
        year = None
        for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
            try:
                dt = datetime.strptime(
                    date.split("T")[0] if "T" in date else date, fmt
                )
                year = dt.year
                break
            except ValueError:
                pass
        meeting_info[mdir.name] = {"date": date, "body": body, "year": year}

    by_year = defaultdict(lambda: defaultdict(list))
    for sf in sorted(SUMMARIES_DIR.glob("*-summary.md")):
        mid = sf.stem.split("-")[0]
        info = meeting_info.get(mid, {})
        year = info.get("year")
        body = info.get("body", "Unknown")
        if year:
            by_year[year][body].append(
                {"date": info.get("date", ""), "content": sf.read_text()}
            )
    return by_year


def load_jsonl_by_year():
    """Load structured JSONL records grouped by year and body."""
    jsonl_path = STRUCTURED_DIR / "all-records.jsonl"
    if not jsonl_path.exists():
        print(f"JSONL not found: {jsonl_path}")
        print("Run extract_structured.py first, or use --source prose")
        sys.exit(1)

    by_year = defaultdict(lambda: defaultdict(list))
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            date = record.get("date", "")
            body = record.get("body", "Unknown")
            year = None
            if date:
                try:
                    year = int(date[:4])
                except (ValueError, IndexError):
                    pass
            if year:
                by_year[year][body].append(record)

    return by_year


def load_monthly_by_year():
    """Load monthly digests grouped by year. One digest per month, all bodies merged."""
    monthly_jsonl = STRUCTURED_DIR / "monthly-digests.jsonl"
    if not monthly_jsonl.exists():
        print(f"Monthly digests not found: {monthly_jsonl}")
        print("Run monthly_rollup.py first, or use --source jsonl")
        sys.exit(1)

    by_year = defaultdict(lambda: defaultdict(list))
    with open(monthly_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            digest = json.loads(line)
            month = digest.get("month", "")
            if len(month) < 4:
                continue
            year = int(month[:4])
            # Monthly digests span all bodies — use "All Bodies" as key
            # but expand into per-body entries if bodies are available
            bodies = digest.get("bodies", ["All Bodies"])
            for body in bodies:
                by_year[year][body].append(digest)

    return by_year


def format_records_for_prompt(records):
    """Format JSONL records as compact text for Claude."""
    parts = []
    for r in records:
        if r.get("procedural_only"):
            continue
        lines = [f"### {r.get('date', '?')} — {r.get('doc_type', '?')}"]
        if r.get("advocacy_score"):
            lines.append(f"Score: {r['advocacy_score']} — {r.get('advocacy_reason', '')}")
        for v in r.get("votes", []):
            vote_line = f"VOTE: {v['item']} → {v['result']}"
            if v.get("no"):
                vote_line += f" (dissent: {', '.join(v['no'])})"
            lines.append(vote_line)
        for h in r.get("housing_items", []):
            h_line = f"HOUSING: [{h.get('type', '?')}] {h['description']}"
            if h.get("units"):
                h_line += f" ({h['units']} units)"
            if h.get("outcome"):
                h_line += f" → {h['outcome']}"
            if h.get("state_law_flags"):
                h_line += f" ⚠ {', '.join(h['state_law_flags'])}"
            lines.append(h_line)
        for f in r.get("fiscal_items", []):
            f_line = f"FISCAL: {f['description']}"
            if f.get("amount"):
                f_line += f" (${f['amount']:,.0f})"
            lines.append(f_line)
        for flag in r.get("legal_flags", []):
            lines.append(f"LEGAL: {flag}")
        for cp in r.get("council_positions", []):
            lines.append(f"POSITION: {cp['member']} — {cp['stance']}: {cp.get('evidence', '')}")
        for q in r.get("key_quotes", []):
            lines.append(f"QUOTE: {q}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def summarize_body_year(body, year, summaries, source="prose"):
    if source in ("jsonl", "monthly"):
        text = format_records_for_prompt(summaries)
        count = len([r for r in summaries if not r.get("procedural_only")])
    else:
        combined = []
        for s in summaries:
            combined.append(f"### {s['date']}\n{s['content']}\n")
        text = "\n".join(combined)
        count = len(summaries)

    if len(text) > 150000:
        text = text[:150000] + "\n[...truncated...]"

    prompt = f"""You are analyzing local government meeting {'records' if source == 'jsonl' else 'summaries'} from Oceanside, CA for a housing advocate.

Body: {body}
Year: {year}
Number of {'substantive records' if source == 'jsonl' else 'meetings'}: {count}

Provide a concise executive summary of this body's activity for the year. Focus on:
- Housing project votes: approvals and denials, with unit counts, vote splits, and who dissented
- Zoning and density actions: any changes to density caps, parking requirements, single-family zoning, or land use designations
- Affordable housing: bond financing, inclusionary policy changes, CDBG/HOME allocations
- Tenant protections: rent stabilization, relocation assistance, just-cause eviction measures
- State housing mandate responses: RHNA compliance, SB 9/SB 10 implementation, "local control" resolutions
- Budget and infrastructure decisions relevant to development capacity
- Controversies or significant public opposition on housing/development items
- Individual council member votes on contested items — name who voted which way

Analytical framework for housing assessment:
- Approving housing projects (market-rate or affordable) = pro-housing. Blocking them = anti-housing.
- Removing or raising density caps = pro-housing. Adding or maintaining caps = anti-housing.
- Tenant protections (relocation assistance, just-cause eviction, rent stabilization) are genuinely pro-housing.
- Using high inclusionary rates to block a SPECIFIC compliant project is anti-housing (poison pill). Raising citywide inclusionary rates as policy is pro-housing.
- Judge by VOTES, not rhetoric. Note when stated positions contradict voting records.

Be specific — name projects, vote counts, dollar amounts, who dissented. Skip procedural items.
Keep it to 400-600 words.

CRITICAL: Only name individuals who appear BY NAME in the source data below. NEVER invent, guess, or fill in names. Hallucinating names is worse than leaving a gap.

Meeting data:
{text}"""

    return call_claude(prompt, max_tokens=2000)


def combine_year_summary(year, body_summaries):
    combined = "\n\n".join(
        f"## {body}\n{summary}" for body, summary in body_summaries.items()
    )

    prompt = f"""You are writing an executive summary of Oceanside, CA city governance for {year}, for a housing advocate.

Below are summaries of each advisory body/commission's activity for the year. Synthesize into a single cohesive executive summary that:

1. Opens with 2-3 sentences capturing the year's character for housing production and tenant protection
2. **Housing supply scorecard**: List every housing project voted on, with unit counts, vote splits, who dissented, and outcome. This is the most important section.
3. **Zoning and density**: Any changes to density caps, parking minimums, land use redesignations, inclusionary rates. Note direction (more restrictive or less).
4. **Tenant protections**: Rent stabilization, relocation assistance, just-cause eviction — what passed, what failed, who voted which way.
5. **State mandate compliance**: RHNA progress, responses to state housing laws, any "local control" resolutions.
6. **Affordable housing finance**: Bonds, CDBG/HOME allocations, inclusionary policy.
7. **Infrastructure and budget** relevant to development capacity.
8. **Council member housing vote patterns**: Who approved projects, who blocked them, who used pro-housing rhetoric while voting anti-housing.

Analytical framework:
- ACTIONS OVER WORDS. Grade the council's year by what it approved and blocked, not what members said.
- Approving housing (market-rate or affordable) is pro-housing. Blocking it is anti-housing.
- Tenant protections are genuinely pro-housing policy.
- Using high inclusionary rates to kill a specific compliant project is anti-housing, even though inclusionary policy itself is pro-housing.
- Note supply skepticism: members who claim to support housing but vote against projects.

Be specific. Name projects, dollar amounts, vote outcomes, individual votes on contested items.

CRITICAL: Only name individuals who appear BY NAME in the body summaries below. NEVER invent, guess, or fill in names. If individual votes are not documented, say so. Hallucinating names is worse than leaving a gap.

Target length: 1000-1500 words.

Body summaries:
{combined}"""

    return call_claude(prompt, max_tokens=4000)


def create_overall_summary(year_summaries):
    combined = "\n\n---\n\n".join(
        f"# {year}\n{summary}" for year, summary in sorted(year_summaries.items())
    )

    prompt = f"""You are writing a comprehensive executive summary covering Oceanside, CA city governance from 2020 through 2026, the tenure of Mayor Esther Sanchez (took office December 2020). This is for a housing advocate.

Below are executive summaries for each year. Synthesize into a single narrative:

1. **The arc of housing production**: How many units were approved vs blocked? What was the net direction — more housing or less? Which projects were the biggest wins and losses?
2. **Council member vote patterns across years**: Who consistently approved housing? Who consistently blocked it? Who used pro-housing rhetoric while voting anti-housing (supply skepticism)? Track individual patterns across years — are members getting better or worse?
3. **Density and zoning trajectory**: Did density caps go up or down? Did parking requirements change? Did the city respond to state mandates or resist them?
4. **Tenant protection trajectory**: What passed, what failed, and who was on which side?
5. **The inclusionary question**: Was inclusionary policy used to genuinely increase affordable production, or weaponized to block specific projects?
6. **Turning points**: Which votes or policy changes most significantly shaped the housing landscape?
7. **Current trajectory**: Is the council moving toward more housing production or less? What does the advocate need to watch?

Analytical framework:
- ACTIONS OVER WORDS. A member's speeches and resolutions are noise. Their votes on projects, density, and zoning are signal.
- Tenant protections (relocation assistance, just-cause eviction, rent stabilization) are genuinely pro-housing.
- Using inclusionary rates to kill specific compliant projects is anti-housing. Raising citywide inclusionary rates is pro-housing.
- Supply skepticism — claiming to support housing while blocking projects — is the most common and most damaging form of anti-housing politics. Name it when the vote record shows it.
- Both market-rate and affordable housing add supply. Blocking either is anti-housing.

Be specific — name projects, policies, vote patterns, dollar amounts. Be analytical, not descriptive. This is an advocacy tool.

CRITICAL: Only name individuals who appear BY NAME in the year summaries below. NEVER invent, guess, or fill in names of council members, commissioners, or other officials. If you are uncertain whether someone served in a given year, do not name them. Hallucinating names is worse than leaving a gap.

Target length: 1500-2500 words.

Year-by-year summaries:
{combined}"""

    return call_claude(prompt, max_tokens=6000)


def main():
    global MODE, client

    parser = argparse.ArgumentParser(description="Generate executive summaries")
    parser.add_argument("--mode", choices=["api", "local"], default="local",
                        help="api=Claude API ($), local=claude -p (subscription, $0)")
    parser.add_argument("--source", choices=["monthly", "jsonl"], default="monthly",
                        help="monthly=monthly digests (default, 7x smaller), jsonl=individual records")
    args = parser.parse_args()

    MODE = args.mode
    source = args.source

    if MODE == "api":
        import anthropic
        client = anthropic.Anthropic()

    print(f"Mode: {MODE} | Source: {source}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if source == "monthly":
        by_year = load_monthly_by_year()
    elif source == "jsonl":
        by_year = load_jsonl_by_year()
    else:
        by_year = load_summaries_by_year()

    year_summaries = {}

    years_to_process = sorted(by_year.keys())
    if 2020 in by_year and 2021 in by_year:
        for body, summaries in by_year[2020].items():
            by_year[2021][body] = summaries + by_year[2021].get(body, [])
        years_to_process = [y for y in years_to_process if y != 2020]

    for year in years_to_process:
        bodies = by_year[year]
        total = sum(len(s) for s in bodies.values())
        print(f"\n{'='*60}")
        print(f"Processing {year} ({total} records across {len(bodies)} bodies)")
        print(f"{'='*60}")

        body_summaries = {}
        for body in sorted(bodies.keys()):
            summaries = bodies[body]
            print(f"  Summarizing {body} ({len(summaries)} records)...")
            result = summarize_body_year(body, year, summaries, source=source)
            if result is None:
                print(f"  WARNING: claude -p returned None for {body} {year}, skipping.")
                continue
            body_summaries[body] = result

        print(f"  Combining into year executive summary...")
        year_exec = combine_year_summary(year, body_summaries)
        year_summaries[year] = year_exec

        label = f"{year}" if year != 2021 else "2020-2021"
        outfile = OUTPUT_DIR / f"executive-summary-{label}.md"
        outfile.write_text(f"# Oceanside, CA — Executive Summary {label}\n\n{year_exec}\n")
        print(f"  Saved: {outfile}")

    print(f"\n{'='*60}")
    print("Creating overall executive summary...")
    print(f"{'='*60}")
    overall = create_overall_summary(year_summaries)
    outfile = OUTPUT_DIR / "executive-summary-full.md"
    outfile.write_text(
        f"# Oceanside, CA — Executive Summary: The Sanchez Era (2020–2026)\n\n{overall}\n"
    )
    print(f"Saved: {outfile}")
    print("\nDone.")


if __name__ == "__main__":
    main()
