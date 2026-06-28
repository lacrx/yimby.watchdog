#!/usr/bin/env python3
"""Extract housing UNIT counts from eTRAKiT building permits.

Each permit = 1+ housing units. ADU/SFD = 1 unit. Duplex = 2.
MF/mid-rise/high-rise: parsed from description text.
"""

import json, re, sys
from pathlib import Path

PERMIT_DIR = Path(__file__).resolve().parent.parent.parent / 'data' / 'permits'

HOUSING_TYPES = {
    'BLD ACCESSORY DWELLING': 'adu',
    'BLD SFD OR DUPLEX': 'sfd_dup',
    'BLD MULTI FAMILY': 'mf',
    'BLD MID RISE': 'midhi',
    'BLD HIGH RISE': 'midhi',
}

# Manual overrides for permits where description is truncated or ambiguous.
# Verified against project records and HCD APR data.
MANUAL_UNITS = {
    # 2020
    'BLDG20-4460': 0,   # Marriott Fairfield Inn — hotel, not housing
    # 2021
    'BLDG21-0049': 20,  # Mixed use mid-rise — description truncated, est. 20 units conservative
    'BLDG21-2828': 2,   # Units 15-16
    'BLDG21-2829': 2,   # Units 1-2
    'BLDG21-2832': 4,   # "FOUR-PLEX TYPE 2"
    'BLDG21-3498': 0,   # Sub-building of larger project, units counted elsewhere
    'BLDG21-3499': 0,   # Sub-building of larger project, units counted elsewhere
    'BLDG21-4415': 6,   # "SIX (TOWNHOMES)"
    'BLDG21-4648': 4,   # "(4) NEW CONSTRUCTION"
    'BLDG21-4685': 0,   # Hilton Home2 Suites hotel — not housing
    # 2022
    'BLDG22-0443': 3,   # "3-PLX... UNTS 115-117"
    'BLDG22-1162': 18,  # "WITH 18" (truncated: 18 residential units)
    # Ocean Creek Mixed Use — 295 units across 5 buildings (Crouch St / S Oceanside Blvd)
    # Verified: Coast News, CEQA 2022080294. All filed 12/16/2022, approved 9/14/2023.
    'BLDG22-2573': 60,  # Ocean Creek Bldg 1 (82K SF mixed use)
    'BLDG22-2574': 80,  # Ocean Creek Bldg 2 (110K SF mixed use)
    'BLDG22-2575': 55,  # Ocean Creek Bldg 5 (62K SF residential)
    'BLDG22-2576': 50,  # Ocean Creek Bldg 4 (57K SF residential)
    'BLDG22-2577': 50,  # Ocean Creek Bldg 3 (57K SF residential) — total 295
    'BLDG22-2233': 0,   # "REPAIR OF (E) UNDERGROUND GAS LEAK" — not new housing
    # 2023
    'BLDG23-0899': 1,   # Description truncated, conservative
    'BLDG23-0985': 0,   # Modera Melrose master permit — units in sub-permits
    'BLDG23-1410': 0,   # Modera Melrose master permit — units in sub-permits
    # Modera Melrose — 323 units across 6 buildings (Oceanside Blvd)
    # Verified: Mill Creek official site, MultifamilyBiz, Instagram.
    'BLDG23-1411': 62,  # Modera Melrose Bldg 1 (mixed-use, larger)
    'BLDG23-1412': 52,  # Modera Melrose Bldg 2
    'BLDG23-1413': 52,  # Modera Melrose Bldg 3
    'BLDG23-1414': 21,  # Modera Melrose Bldg 4 — "WITH 21" in description
    'BLDG23-1415': 52,  # Modera Melrose Bldg 5
    'BLDG23-1416': 84,  # Modera Melrose Bldg 6 (remainder: 323-62-52-52-21-52=84)
    'BLDG23-2058': 0,   # Hotel renewal — not housing
    # 2024
    'BLDG24-0343': 0,   # "Do not use" — voided permit
    'BLDG24-1938': 0,   # Fire damage repair — not new housing
    'BLDG24-1974': 0,   # Meter panel upgrade — not new housing
    # 2025
    'BLDG25-0144': 0,   # MPU electrical — not new housing (COMMERCIAL PME)
    # Modera Neptune — 360 units (8-level mixed use, 815 N Coast Hwy)
    # Verified: Mill Creek official site, Coast News, MultifamilyBiz.
    'BLDG25-0374': 360,
    'BLDG25-1032': 12,  # Ground-up 3-story MF (estimated from typical floor plate)
    'BLDG25-1184': 0,   # Meter panel upgrade — not new housing (COMMERCIAL PME)
    # 2026
    'BLDG26-0111': 0,   # Foundation repair — not new housing
    'BLDG26-0182': 4,   # "(4) ADU UNITS" — 4 units in MF building
}

HOTEL_WORDS = ['HOTEL', 'INN', 'SUITES', 'MOTEL', 'ROOM ', 'ROOMS']
SKIP_WORDS = ['SOLAR', 'PV SYSTEM', 'METER PANEL', 'ELECTRIC METER',
              'REPAIR', 'RE-PIPE', 'FOUNDATION SUPPORT', 'GAS LEAK',
              'FIRE-DAMAGED']


def is_non_housing(desc):
    up = desc.upper()
    for w in HOTEL_WORDS:
        if w in up:
            return True
    for w in SKIP_WORDS:
        if w in up and not any(x in up for x in ['UNIT', 'PLEX', 'CONDO', 'APARTMENT', 'TOWNHOME']):
            return True
    return False


def parse_mf_units(desc):
    """Extract unit count from a multi-family permit description."""
    if not desc or is_non_housing(desc):
        return 0

    up = desc.upper()

    # "UNITS 42-44" → 3, "UNITS 1-10" → 10
    m = re.search(r'UNITS?\s+(\d+)\s*[-–]\s*(\d+)', up)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return b - a + 1

    # "N-PLEX" or "N PLEX"
    m = re.search(r'(\d+)\s*[-]?\s*PLEX', up)
    if m:
        return int(m.group(1))

    # "(N) UNITS" or "N UNITS" or "N UNIT" (but not "UNIT 4" which is a unit number)
    m = re.search(r'(?:\((\d+)\)\s*(?:RESIDENTIAL\s+)?UNITS?|(\d+)\s+(?:DWELLING\s+)?UNITS?\b|(\d+)\s+(?:APT|APARTMENT|CONDO|TOWNHOME)S?\b)', up)
    if m:
        n = int(m.group(1) or m.group(2) or m.group(3))
        if n > 0:
            return n

    # "CONSTRUCT N CONDOMINIUMS"
    m = re.search(r'(\d+)\s+CONDOMINIUMS?\b', up)
    if m:
        return int(m.group(1))

    # "N ATTACHED SFR" or "N-UNIT"
    m = re.search(r'(\d+)\s+ATTACHED\s+SFR', up)
    if m:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*-\s*UNIT\b', up)
    if m:
        return int(m.group(1))

    # "TRIPLEX"
    if 'TRIPLEX' in up:
        return 3
    if 'FOURPLEX' in up:
        return 4

    # "(N) ... CONDOMINIUMS" or "(N) ... STORY"
    m = re.search(r'\((\d+)\)\s+(?:NEW\s+)?(?:\d+-?STORY\s+)?(?:CONDO|TOWNHOME|RESIDENTIAL)', up)
    if m:
        return int(m.group(1))

    # Fallback: "CONSTRUCT N-STORY, N UNIT"
    m = re.search(r'(\d+)\s*UNIT', up)
    if m:
        n = int(m.group(1))
        if n > 1:
            return n

    # Can't parse — flag it
    return None


def count_units_for_permit(permit):
    ptype = permit.get('type', '')
    desc = permit.get('description', '')
    pno = permit.get('permit_no', '')
    category = HOUSING_TYPES.get(ptype)

    if not category:
        # Check manual overrides even for non-housing types (PME, repair, etc.)
        if pno in MANUAL_UNITS:
            return None, None  # Already handled — skip
        return None, None

    # Manual override takes precedence
    if pno in MANUAL_UNITS:
        return category, MANUAL_UNITS[pno]

    if category == 'adu':
        m = re.search(r'\((\d+)\)\s*ADU', desc.upper()) if desc else None
        return category, int(m.group(1)) if m else 1

    if category == 'sfd_dup':
        up = (desc or '').upper()
        if 'DUPLEX' in up or '2-UNIT' in up or '2 UNIT' in up:
            return category, 2
        return category, 1

    # MF or midhi
    units = parse_mf_units(desc)
    return category, units


def main():
    results = {}
    unparsed = []

    for year in range(2020, 2027):
        f = PERMIT_DIR / f'etrakit-permits-{year}.jsonl'
        if not f.exists():
            continue

        counts = {'adu': 0, 'sfd_dup': 0, 'mf': 0, 'midhi': 0}
        permits = {'adu': 0, 'sfd_dup': 0, 'mf': 0, 'midhi': 0}

        for line in f.read_text().strip().split('\n'):
            p = json.loads(line)
            cat, units = count_units_for_permit(p)
            if cat is None:
                continue
            if units is None:
                unparsed.append((year, p['permit_no'], p['type'], p.get('description', '')[:80]))
                units = 1  # conservative fallback
            counts[cat] += units
            permits[cat] += 1

        results[year] = counts
        total_units = sum(counts.values())
        total_permits = sum(permits.values())
        label = f"(Jan-Jun)" if year == 2026 else ""
        print(f"{year} {label}")
        print(f"  ADU:        {counts['adu']:>4} units  ({permits['adu']} permits)")
        print(f"  SFD/Duplex: {counts['sfd_dup']:>4} units  ({permits['sfd_dup']} permits)")
        print(f"  MF:         {counts['mf']:>4} units  ({permits['mf']} permits)")
        print(f"  Mid/Hi:     {counts['midhi']:>4} units  ({permits['midhi']} permits)")
        print(f"  TOTAL:      {total_units:>4} units  ({total_permits} permits)")
        print()

    if unparsed:
        print(f"\n=== {len(unparsed)} permits with unparsable unit counts (defaulted to 1) ===")
        for year, pno, ptype, desc in unparsed:
            print(f"  {year} {pno} | {ptype} | {desc}")

    return results


if __name__ == '__main__':
    main()
