#!/usr/bin/env python3
"""Completion forecast: when will filed units actually get built?

Four-tier data model:
  Tier 1 — eTRAKiT FINALED: verified completions (CO issued). Ground truth.
  Tier 2 — eTRAKiT ISSUED + construction lag: under construction, high confidence.
  Tier 3 — ADU continuation: ~130 approvals/yr, 10% attrition, 15mo lag.
  Tier 4 — SSCSP projected: corridor plan filings → completions with 25% attrition.

SSCSP assumptions (conservative):
  Adopted June 24, 2026. Covers 1,437 acres, 8,300-unit capacity.
  DB26-00001 (142u) filed June 2026 — completion ~2030.
  New filings ramp: 200u (2027), 300u (2028), 400u (2029).
  MF completion lag: 3.5-4yr from filing (ministerial process faster than discretionary).
  25% attrition (financing, market, entitlement lapse).
  Spread: 25% early (3yr), 50% peak (3.5yr), 25% late (4yr).

Left chart:  Units filed by year (HCD APR, actual)
Right chart: Projected completions (eTRAKiT-calibrated + ADU + SSCSP)
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import numpy as np
import csv
import json
import importlib.util
from datetime import datetime
from collections import defaultdict

AFF_COLS = ['ACUTELY_LOW_INCOME_DR','ACUTELY_LOW_INCOME_NDR',
            'EXTREMELY_LOW_INCOME_DR','EXTREMELY_LOW_INCOME_NDR',
            'VLOW_INCOME_DR','VLOW_INCOME_NDR',
            'LOW_INCOME_DR','LOW_INCOME_NDR']

CONSTRUCTION_LAG = {'adu': 9, 'sfd_dup': 12, 'mf': 21, 'midhi': 24}

# ─── Load unit-counting logic from count_housing_units.py ───

spec = importlib.util.spec_from_file_location(
    'chu', '/home/thomas/repos/civics/share/oceanside-sb79-briefing/count_housing_units.py')
chu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(chu)

# ─── FILINGS: HCD APR by year (left chart, ground truth) ───

filings = defaultdict(lambda: {'ADU': 0, 'SFD/SFA': 0, '2-4': 0, '5+': 0})
aff_filings = defaultdict(int)

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
        if yr < 2020 or yr > 2025:
            continue

        units = int(row.get('TOT_PROPOSED_UNITS', '') or 0)
        aff_units = sum(int(row.get(c, '') or 0) for c in AFF_COLS)
        cat = row.get('UNIT_CAT', '')

        if cat == 'ADU':
            key = 'ADU'
        elif cat in ('SFD', 'SFA'):
            key = 'SFD/SFA'
        elif cat == '2 to 4':
            key = '2-4'
        elif cat == '5+':
            key = '5+'
        else:
            continue

        filings[yr][key] += units
        aff_filings[yr] += aff_units

filings[2026] = {'ADU': 67, 'SFD/SFA': 4, '2-4': 6, '5+': 142}
aff_filings[2026] = 21

# ─── COMPLETIONS: eTRAKiT building permits (right chart, tiers 1+2) ───

confirmed = defaultdict(lambda: {'adu': 0, 'sfd': 0, 'mf': 0})
in_progress = defaultdict(lambda: {'adu': 0, 'sfd': 0, 'mf': 0})

for file_year in range(2018, 2027):
    try:
        with open(f'/home/thomas/repos/civics/data/permits/etrakit-permits-{file_year}.jsonl') as f:
            for line in f:
                p = json.loads(line)
                cat, units = chu.count_units_for_permit(p)
                if cat is None or units is None or units <= 0:
                    continue

                status = p.get('status', '')
                issued = p.get('issued', '')
                if not issued:
                    continue
                parts = issued.split('/')
                if len(parts) != 3:
                    continue
                iss_month, iss_year = int(parts[0]), int(parts[2])
                if iss_year < 2018 or iss_year > 2026:
                    continue

                ckey = 'adu' if cat == 'adu' else ('sfd' if cat == 'sfd_dup' else 'mf')
                lag = CONSTRUCTION_LAG.get(cat, 18)
                comp_year = (iss_year * 12 + iss_month + lag) // 12

                if status == 'FINALED':
                    confirmed[comp_year][ckey] += units
                elif status in ('ISSUED', 'TEMP CERT OF OCC'):
                    in_progress[comp_year][ckey] += units
    except FileNotFoundError:
        pass

# ─── TIER 3: ADU continuation (beyond eTRAKiT pipeline) ───
# ~130 ADU approvals/yr (observed 2021 pace), 10% attrition, ~15mo to completion

adu_model = defaultdict(int)
for yr in range(2027, 2032):
    adu_model[yr + 1] += round(130 * 0.90)  # 117/yr

# ─── TIER 4: SSCSP projected completions ───
# Adopted June 24, 2026. 1,437 acres, three Sprinter corridors.
# DB26-00001: 142 units filed June 2026, density bonus under AB 2011.
# New filings ramp conservatively as developers learn the ministerial process.

sscsp_model = defaultdict(int)

# DB26-00001: filed June 2026 → entitled ~late 2026 → BP ~mid 2027 → complete ~2030
sscsp_model[2030] += round(142 * 0.75)  # 107 units after 25% attrition

# New SSCSP filings (beyond DB26-00001):
#   2027: ~200 units (1-2 projects, developers testing new ministerial path)
#   2028: ~300 units (word spreads, more developers file)
#   2029: ~400 units (pipeline matures)
# Completion spread: 25% at filing+3yr, 50% at filing+3.5yr (→ round to +4), 25% at +4yr
SSCSP_FILINGS = {2027: 200, 2028: 300, 2029: 400}
SSCSP_ATTRITION = 0.25

for filing_yr, units in SSCSP_FILINGS.items():
    surviving = units * (1 - SSCSP_ATTRITION)
    sscsp_model[filing_yr + 3] += round(surviving * 0.25)
    sscsp_model[filing_yr + 4] += round(surviving * 0.75)

# ─── Affordable rate by HCD filing year ───

total_by_yr = defaultdict(int)
aff_by_yr = defaultdict(int)
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
        total_by_yr[yr] += int(row.get('TOT_PROPOSED_UNITS', '') or 0)
        aff_by_yr[yr] += sum(int(row.get(c, '') or 0) for c in AFF_COLS)

aff_frac = {}
for yr in range(2018, 2026):
    aff_frac[yr] = aff_by_yr[yr] / total_by_yr[yr] if total_by_yr[yr] else 0

def aff_rate_for(cy):
    hcd_years = [cy - 4, cy - 3]
    rates = [aff_frac.get(y, 0) for y in hcd_years if y in aff_frac]
    return sum(rates) / len(rates) if rates else 0

# ─── Assemble final completion series ───

FILING_YEARS = list(range(2020, 2027))
COMPLETION_YEARS = list(range(2021, 2033))

comp_confirmed = []
comp_inprogress = []
comp_adu = []
comp_sscsp = []
comp_aff = []

for cy in COMPLETION_YEARS:
    c = sum(confirmed[cy].values())
    ip = sum(in_progress[cy].values())
    adu = adu_model.get(cy, 0)
    sscsp = sscsp_model.get(cy, 0)

    # Only use modeled tiers where eTRAKiT has no coverage
    if c + ip > 0:
        adu = 0

    comp_confirmed.append(c)
    comp_inprogress.append(ip)
    comp_adu.append(adu)
    comp_sscsp.append(sscsp)

    # Affordable estimate: MF completions (eTRAKiT) + SSCSP inclusionary
    mf_total = confirmed[cy].get('mf', 0) + in_progress[cy].get('mf', 0)
    aff_est = round(mf_total * aff_rate_for(cy))
    # SSCSP projects: 15% inclusionary (DB26-00001 confirmed), apply to all SSCSP
    aff_est += round(sscsp * 0.15)
    comp_aff.append(aff_est)

comp_totals = [c + ip + a + s for c, ip, a, s in
               zip(comp_confirmed, comp_inprogress, comp_adu, comp_sscsp)]

# ─── Print table ───

print("CALIBRATED COMPLETION FORECAST (4-tier)")
print("=" * 85)
print(f"{'Year':<6} {'Verified':>9} {'InProg':>7} {'ADU':>5} {'SSCSP':>6} {'Total':>7} {'Aff':>5}  {'Source'}")
print('-' * 85)
for i, cy in enumerate(COMPLETION_YEARS):
    c, ip = comp_confirmed[i], comp_inprogress[i]
    adu, sscsp = comp_adu[i], comp_sscsp[i]
    total = comp_totals[i]
    aff = comp_aff[i]
    if c > 0 or ip > 0:
        src = 'eTRAKiT'
        if adu > 0 or sscsp > 0:
            src += ' + model'
    elif adu > 0 and sscsp > 0:
        src = 'ADU + SSCSP model'
    elif adu > 0:
        src = 'ADU model'
    elif sscsp > 0:
        src = 'SSCSP model'
    else:
        src = 'pipeline empty'
    print(f"{cy:<6} {c:>9} {ip:>7} {adu:>5} {sscsp:>6} {total:>7} {aff:>5}  {src}")

# ─── Chart ───

C_CONFIRMED  = '#1A3A5C'  # dark blue — verified completions
C_INPROG     = '#5B9BD5'  # medium blue — under construction
C_ADU        = '#BDC3C7'  # gray — ADU continuation
C_SSCSP      = '#D4770B'  # amber — SSCSP projected
C_AFF        = '#1B6B3A'  # green — affordable line
C_RED        = '#8B0000'

# Cohort colors for left chart
COHORT_COLORS = {
    2020: '#A8D5BA', 2021: '#2D8A5F', 2022: '#2D5F8A',
    2023: '#5B9BD5', 2024: '#8A5F2D', 2025: '#BDC3C7', 2026: '#E74C3C',
}

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(20, 8),
                                gridspec_kw={'width_ratios': [7, 12]})
fig.patch.set_facecolor('#FAFAFA')
bar_width = 0.6

# ─── LEFT: Filings by year ───

x1 = np.arange(len(FILING_YEARS))
filing_totals = [sum(filings[y].values()) for y in FILING_YEARS]

for i, fy in enumerate(FILING_YEARS):
    ax1.bar(i, filing_totals[i], color=COHORT_COLORS[fy], width=bar_width,
            edgecolor='white', linewidth=1)
    ax1.text(i, filing_totals[i] + 30, f'{filing_totals[i]:,}', ha='center',
             va='bottom', fontsize=10, fontweight='bold')

# Affordable line on filings
aff_filing_vals = [aff_filings[y] for y in FILING_YEARS]
ax1.plot(x1, aff_filing_vals, color=C_AFF, marker='o', markersize=7, linewidth=2.5,
         zorder=5, markeredgecolor='white', markeredgewidth=1.5)
for i, a in enumerate(aff_filing_vals):
    if a > 0:
        ax1.text(i, a + 25, str(a), ha='center', va='bottom', fontsize=9,
                 fontweight='bold', color=C_AFF,
                 bbox=dict(facecolor='white', edgecolor='none', alpha=0.8, pad=1.5))

labels1 = [str(y) for y in FILING_YEARS]
labels1[-1] = '2026\n(5.5 mo)'
ax1.set_xticks(x1)
ax1.set_xticklabels(labels1, fontsize=10)
ax1.set_ylabel('Housing Units Filed', fontsize=12, fontweight='bold')
ax1.set_title('Units Filed (Input)', fontsize=13, fontweight='bold')
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)

# ─── RIGHT: Completions by data tier ───

x2 = np.arange(len(COMPLETION_YEARS))

# Stack: confirmed → in-progress → ADU → SSCSP
ax2.bar(x2, comp_confirmed, color=C_CONFIRMED, width=bar_width, zorder=3)
ax2.bar(x2, comp_inprogress, bottom=comp_confirmed, color=C_INPROG,
        width=bar_width, zorder=3)
bot3 = [c + ip for c, ip in zip(comp_confirmed, comp_inprogress)]
ax2.bar(x2, comp_adu, bottom=bot3, color=C_ADU, width=bar_width,
        zorder=3, edgecolor='#95A5A6', linewidth=0.5, linestyle='--')
bot4 = [b + a for b, a in zip(bot3, comp_adu)]
ax2.bar(x2, comp_sscsp, bottom=bot4, color=C_SSCSP, width=bar_width,
        zorder=3, edgecolor='#B8650A', linewidth=0.8)

# Total labels
for i, t in enumerate(comp_totals):
    if t > 0:
        ax2.text(i, t + 10, f'{t:,}', ha='center', va='bottom',
                fontsize=10, fontweight='bold')
    else:
        ax2.text(i, 10, '0', ha='center', va='bottom',
                fontsize=12, fontweight='bold', color='#CC0000')

# Affordable line on completions
ax2.plot(x2, comp_aff, color=C_AFF, marker='o', markersize=7, linewidth=2.5,
         zorder=5, markeredgecolor='white', markeredgewidth=1.5)
for i, a in enumerate(comp_aff):
    if a > 0:
        ax2.text(i, a + 12, str(a), ha='center', va='bottom', fontsize=9,
                 fontweight='bold', color=C_AFF,
                 bbox=dict(facecolor='white', edgecolor='none', alpha=0.8, pad=1.5))

# RHNA pace line
rhna_annual = 3439 / 7
ax2.axhline(y=rhna_annual, color='#4B0082', linestyle='--', linewidth=2, alpha=0.5)
ax2.text(len(COMPLETION_YEARS) - 1, rhna_annual + 15, f'RHNA pace\n({rhna_annual:.0f}/yr)',
         fontsize=8, color='#4B0082', ha='right', va='bottom')

labels2 = [str(y) for y in COMPLETION_YEARS]
ax2.set_xticks(x2)
ax2.set_xticklabels(labels2, fontsize=10)
ax2.set_ylabel('Housing Units Completing', fontsize=12, fontweight='bold')
ax2.set_title('Completions (Output) — eTRAKiT-calibrated', fontsize=13, fontweight='bold')
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

# Arrow
fig.text(0.37, 0.5, '→', fontsize=40, ha='center', va='center',
         color='#666', fontweight='bold')
fig.text(0.37, 0.43, '3–5 yr\nlag', fontsize=10, ha='center', va='center',
         color='#666', fontstyle='italic')

# ─── Title + Legend ───

fig.suptitle('Oceanside Housing Pipeline: What Goes In vs. What Comes Out\n'
             'eTRAKiT-calibrated completions + SSCSP corridor projections',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.99)

legend_elements = [
    mpatches.Patch(facecolor=C_CONFIRMED, label='Verified complete (CO issued)'),
    mpatches.Patch(facecolor=C_INPROG, label='Under construction (BP issued)'),
    mpatches.Patch(facecolor=C_ADU, edgecolor='#95A5A6', linestyle='--',
                   label='ADU continuation (modeled)'),
    mpatches.Patch(facecolor=C_SSCSP, edgecolor='#B8650A',
                   label='SSCSP corridor (projected)'),
    mlines.Line2D([], [], color=C_AFF, marker='o', markersize=5, linewidth=2,
                  markeredgecolor='white', label='Affordable (deed-restricted)'),
]
fig.legend(handles=legend_elements, loc='upper center', bbox_to_anchor=(0.5, 0.92),
           fontsize=9, framealpha=0.9, ncol=5)

plt.tight_layout(rect=[0.01, 0.02, 0.99, 0.87])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-completion-forecast.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('\nSaved:', out)
