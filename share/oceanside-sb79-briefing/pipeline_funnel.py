#!/usr/bin/env python3
"""Housing pipeline funnel: Filed → Approved → Building Permit → Completed.

Two data sources, two stages:
  HCD APR Table A: planning applications (units). Filed vs approved.
  eTRAKiT: building permits (permits). Issued vs finaled (CO).

The lag between planning filing and construction completion is 3-5 years,
so the approval collapse visible in 2022-2025 HCD data won't show up in
building permit completions until ~2027-2030.
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import csv
from datetime import datetime
from collections import defaultdict

# ─── HCD APR: planning application units by year ───

hcd_filed = defaultdict(int)
hcd_approved = defaultdict(int)

with open('/home/thomas/repos/civics/data/hcd-apr-tablea.csv') as f:
    for row in csv.DictReader(f):
        if row['JURIS_NAME'] != 'OCEANSIDE':
            continue
        dt = row.get('APP_SUBMIT_DT', '')
        if not dt:
            continue
        try:
            yr = datetime.strptime(dt, '%Y-%m-%d').year
        except ValueError:
            try:
                yr = datetime.strptime(dt, '%m/%d/%Y').year
            except ValueError:
                continue
        if yr < 2018 or yr > 2025:
            continue

        units = int(row.get('TOT_PROPOSED_UNITS', '') or 0)
        approved = int(row.get('TOT_APPROVED_UNITS', '') or 0)

        hcd_filed[yr] += units
        hcd_approved[yr] += approved

# ─── eTRAKiT: building permits by ISSUED year → finaled count ───
# Group by the year the BP was ISSUED (construction start proxy)
# Status FINALED = certificate of occupancy (construction complete)

import json

bp_issued_units = defaultdict(int)
bp_finaled_units = defaultdict(int)

MANUAL_UNITS = {
    'BLDG20-4460': 0, 'BLDG21-0049': 20, 'BLDG21-2828': 2, 'BLDG21-2829': 2,
    'BLDG21-2832': 4, 'BLDG21-3498': 0, 'BLDG21-3499': 0, 'BLDG21-4415': 6,
    'BLDG21-4648': 4, 'BLDG21-4685': 0, 'BLDG22-0443': 3, 'BLDG22-1162': 18,
    'BLDG22-2573': 60, 'BLDG22-2574': 80, 'BLDG22-2575': 55, 'BLDG22-2576': 50,
    'BLDG22-2577': 50, 'BLDG22-2233': 0, 'BLDG23-0899': 1, 'BLDG23-0985': 0,
    'BLDG23-1410': 0, 'BLDG23-1411': 62, 'BLDG23-1412': 52, 'BLDG23-1413': 52,
    'BLDG23-1414': 21, 'BLDG23-1415': 52, 'BLDG23-1416': 84, 'BLDG23-2058': 0,
    'BLDG24-0343': 0, 'BLDG24-1938': 0, 'BLDG24-1974': 0, 'BLDG25-0144': 0,
    'BLDG25-0374': 360, 'BLDG25-1032': 12, 'BLDG25-1184': 0, 'BLDG26-0111': 0,
    'BLDG26-0182': 4, 'BLDG23-1983': 43, 'BLDG24-2302': 111, 'BLDG25-1204': 56,
}

import re

HOUSING_TYPES = {'BLD ACCESSORY DWELLING', 'BLD SFD OR DUPLEX', 'BLD MULTI FAMILY',
                 'BLD MID RISE', 'BLD HIGH RISE'}

def estimate_units(p):
    pno = p.get('permit_no', '')
    if pno in MANUAL_UNITS:
        return MANUAL_UNITS[pno]
    ptype = p.get('type', '')
    if ptype not in HOUSING_TYPES:
        return 0
    if ptype == 'BLD ACCESSORY DWELLING':
        desc = (p.get('description', '') or '').upper()
        m = re.search(r'\((\d+)\)\s*ADU', desc)
        return int(m.group(1)) if m else 1
    if ptype == 'BLD SFD OR DUPLEX':
        desc = (p.get('description', '') or '').upper()
        if 'DUPLEX' in desc or '2-UNIT' in desc or '2 UNIT' in desc:
            return 2
        return 1
    # MF/mid/high — try to parse
    desc = (p.get('description', '') or '').upper()
    if not desc:
        return 1
    # Quick patterns
    m = re.search(r'(\d+)\s+UNIT', desc)
    if m and int(m.group(1)) > 1:
        return int(m.group(1))
    m = re.search(r'(\d+)\s*-?\s*PLEX', desc)
    if m:
        return int(m.group(1))
    m = re.search(r'UNITS?\s+(\d+)\s*[-–]\s*(\d+)', desc)
    if m:
        return int(m.group(2)) - int(m.group(1)) + 1
    return 1

def parse_year(date_str):
    if not date_str:
        return None
    parts = date_str.split('/')
    if len(parts) == 3:
        return int(parts[2])
    return None

for file_year in range(2018, 2027):
    try:
        with open(f'/home/thomas/repos/civics/data/permits/etrakit-permits-{file_year}.jsonl') as f:
            for line in f:
                p = json.loads(line)
                ptype = p.get('type', '')
                if ptype not in HOUSING_TYPES:
                    continue

                units = estimate_units(p)
                if units <= 0:
                    continue

                status = p.get('status', '')
                issued_year = parse_year(p.get('issued', ''))

                if status in ('ISSUED', 'FINALED', 'TEMP CERT OF OCC', 'RE-ISSUED') and issued_year:
                    if 2018 <= issued_year <= 2026:
                        bp_issued_units[issued_year] += units

                if status == 'FINALED' and issued_year:
                    if 2018 <= issued_year <= 2026:
                        bp_finaled_units[issued_year] += units
    except FileNotFoundError:
        pass

# ─── Print table ───

print(f"{'Year':<6} {'Plan Filed':>10} {'Plan Apprvd':>11} {'BP Issued':>10} {'CO (Done)':>10}")
print('-' * 52)
for yr in range(2018, 2026):
    print(f"{yr:<6} {hcd_filed[yr]:>10} {hcd_approved[yr]:>11} {bp_issued_units[yr]:>10} {bp_finaled_units[yr]:>10}")

# ─── Chart: dual-axis showing the pipeline ───

years = list(range(2018, 2026))
year_labels = [str(y) for y in years]

filed = [hcd_filed[y] for y in years]
approved = [hcd_approved[y] for y in years]
bp_issued = [bp_issued_units[y] for y in years]
bp_done = [bp_finaled_units[y] for y in years]

C_FILED    = '#BDC3C7'   # light gray
C_APPROVED = '#2D8A5F'   # green
C_BP       = '#2D5F8A'   # blue
C_DONE     = '#1A3A5C'   # dark blue
C_RED      = '#8B0000'
C_GAP      = '#E74C3C'   # red for the gap

fig, ax = plt.subplots(1, 1, figsize=(14, 8))
fig.patch.set_facecolor('#FAFAFA')

x = np.arange(len(years))
bar_width = 0.35

# Planning stage: filed (background) + approved (foreground)
ax.bar(x - bar_width/2, filed, width=bar_width, color=C_FILED, edgecolor='#95A5A6',
       linewidth=0.5, zorder=2)
ax.bar(x - bar_width/2, approved, width=bar_width, color=C_APPROVED, zorder=3)

# Construction stage: BP issued (background) + completed (foreground)
ax.bar(x + bar_width/2, bp_issued, width=bar_width, color=C_BP, alpha=0.4,
       edgecolor='#2D5F8A', linewidth=0.5, zorder=2)
ax.bar(x + bar_width/2, bp_done, width=bar_width, color=C_DONE, zorder=3)

# Labels on planning bars
for i, (f, a) in enumerate(zip(filed, approved)):
    ax.text(i - bar_width/2, f + 30, f'{f:,}', ha='center', va='bottom',
            fontsize=8, color='#666')
    if a > 0:
        pct = f'{a/f*100:.0f}%' if f else ''
        ax.text(i - bar_width/2, a + 30, pct, ha='center', va='bottom',
                fontsize=9, fontweight='bold', color=C_APPROVED)

# Labels on construction bars
for i, (bp, done) in enumerate(zip(bp_issued, bp_done)):
    if bp > 0:
        ax.text(i + bar_width/2, bp + 30, f'{bp:,}', ha='center', va='bottom',
                fontsize=8, color='#666')

# Policy lines
# 2018=0, ..., 2021=3, 2023=5
ax.axvline(x=3.5, color='#228B22', linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(3.45, max(max(filed), max(bp_issued)) * 0.85, '43 du/acre\ncap removed\nNov 2021',
        fontsize=8, color='#228B22', fontstyle='italic', va='top', ha='right')

ax.axvline(x=5.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(5.55, max(max(filed), max(bp_issued)) * 0.95, '86 du/acre\ncap adopted\nOct 2023',
        fontsize=8, color=C_RED, fontstyle='italic', va='top')

# Annotation: approval collapse
ax.annotate(
    'Approval rate\n100% → 0%',
    xy=(7, approved[7] + 50), xytext=(6.5, max(filed) * 0.5),
    fontsize=10, fontweight='bold', color=C_RED, ha='center',
    arrowprops=dict(arrowstyle='->', color=C_RED, lw=2),
)

# Annotation: lag
ax.annotate(
    '3–5 year lag\nbefore completions\ncollapse',
    xy=(7, bp_done[7]), xytext=(7.3, bp_done[7] + max(filed) * 0.25),
    fontsize=9, color=C_DONE, ha='left',
    arrowprops=dict(arrowstyle='->', color=C_DONE, lw=1.5),
)

ax.set_ylabel('Housing Units', fontsize=13, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(year_labels, fontsize=11)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

# ─── Title + Legend ───

fig.suptitle('Oceanside Housing Pipeline Funnel\n'
             'Planning Applications → Approvals → Building Permits → Completions',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_FILED, edgecolor='#95A5A6', label='Units filed (planning)'),
    mpatches.Patch(facecolor=C_APPROVED, label='Units approved (planning)'),
    mpatches.Patch(facecolor=C_BP, alpha=0.4, edgecolor='#2D5F8A', label='BP issued (units, est.)'),
    mpatches.Patch(facecolor=C_DONE, label='Construction complete (CO)'),
]
fig.legend(handles=legend_elements, loc='upper center', bbox_to_anchor=(0.5, 0.91),
           fontsize=10, framealpha=0.9, ncol=4)

plt.tight_layout(rect=[0.02, 0.02, 0.98, 0.86])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-pipeline-funnel.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('\nSaved:', out)
