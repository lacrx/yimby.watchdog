#!/usr/bin/env python3
"""Projected housing production: approvals → completions with measured lag.

Model:
  1. HCD APR approvals by year/type (actual data 2018-2025)
  2. Apply Oceanside-measured timelines (eTRAKiT plan check + construction)
  3. Apply attrition rates (approved but never completed)
  4. Spread completions across a delivery window (not point-in-time)
  5. 2026+: assume ADU pace continues, zero new MF approvals (observed trend)

Timeline inputs (from eTRAKiT FINALED permits):
  ADU:     plan check 5-6 mo + construction 6-12 mo = 12-18 mo total
  SFD/SFA: plan check 10-11 mo + construction 8-14 mo = 18-24 mo total
  2-4:     plan check 10-12 mo + construction 12-16 mo = 22-28 mo total
  5+:      plan check 14-16 mo + construction 18-30 mo = 32-46 mo total

Attrition (approval → CO):
  ADU: 10% (low — individual owners, ministerial)
  SFD/SFA: 12%
  2-4: 15%
  5+: 25% (financing risk, market shifts, entitlement lapse)
  Sources: Terner Center (2020), CA HCD annual reports, ULI development benchmarks
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import csv
from datetime import datetime
from collections import defaultdict

# ─── Timeline parameters (months from approval to completion) ───
# Each type gets a delivery window: [earliest, peak, latest] in months
# Completions spread as: 25% at earliest, 50% at peak, 25% at latest

PARAMS = {
    'ADU':     {'window': (10, 15, 20),  'attrition': 0.10},
    'SFD/SFA': {'window': (16, 20, 26),  'attrition': 0.12},
    '2-4':     {'window': (20, 24, 30),  'attrition': 0.15},
    '5+':      {'window': (30, 40, 50),  'attrition': 0.25},
}

# ─── Load HCD APR approvals ───

approvals = defaultdict(lambda: {'ADU': 0, 'SFD/SFA': 0, '2-4': 0, '5+': 0})

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

        approved = int(row.get('TOT_APPROVED_UNITS', '') or 0)
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

        approvals[yr][key] += approved

# 2026 forward: assume ADU pace continues (~130/yr approved based on 2021 rate),
# DB26-00001 (142 units) as only 5+ approval, zero SFD/2-4 approvals
approvals[2026] = {'ADU': 130, 'SFD/SFA': 0, '2-4': 0, '5+': 142}
approvals[2027] = {'ADU': 130, 'SFD/SFA': 0, '2-4': 0, '5+': 0}
approvals[2028] = {'ADU': 130, 'SFD/SFA': 0, '2-4': 0, '5+': 0}

# ─── Project completions with delivery window spread ───

completions = defaultdict(lambda: {'ADU': 0, 'SFD/SFA': 0, '2-4': 0, '5+': 0})

for yr in range(2018, 2029):
    for cat in ['ADU', 'SFD/SFA', '2-4', '5+']:
        units = approvals[yr][cat]
        if units == 0:
            continue

        p = PARAMS[cat]
        surviving = units * (1 - p['attrition'])

        early_mo, peak_mo, late_mo = p['window']
        early_yr = yr + early_mo / 12
        peak_yr = yr + peak_mo / 12
        late_yr = yr + late_mo / 12

        # Distribute: 25% early, 50% peak, 25% late
        for frac, delivery_yr in [(0.25, early_yr), (0.50, peak_yr), (0.25, late_yr)]:
            target_yr = round(delivery_yr)
            if target_yr < 2020:
                target_yr = 2020
            completions[target_yr][cat] += round(surviving * frac)

# ─── Actual completions from eTRAKiT (override model for 2020-2024) ───

ACTUAL_COMPLETIONS = {
    2020: 399,
    2021: 460,
    2022: 387,
    2023: 529,
    2024: 221,
}

# ─── Print table ───

print(f"{'Year':<6} {'ADU':>5} {'SFD/SFA':>8} {'2-4':>5} {'5+':>6} {'Model':>7} {'Actual':>7}")
print('-' * 50)
for yr in range(2020, 2033):
    d = completions[yr]
    model_total = sum(d.values())
    actual = ACTUAL_COMPLETIONS.get(yr)
    actual_str = str(actual) if actual is not None else '—'
    src = '(actual)' if actual is not None else '(model)'
    print(f"{yr:<6} {d['ADU']:>5} {d['SFD/SFA']:>8} {d['2-4']:>5} {d['5+']:>6} {model_total:>7} {actual_str:>7} {src}")

# ─── Chart ───

years = list(range(2020, 2033))
year_labels = [str(y) for y in years]

# Use actuals where available, model for projections
actual_years = list(range(2020, 2025))
projected_years = list(range(2025, 2033))

actual_vals = [ACTUAL_COMPLETIONS.get(y, 0) for y in actual_years]
projected_vals = [sum(completions[y].values()) for y in projected_years]

# Break down projected by type for stacking
proj_adu = [completions[y]['ADU'] for y in projected_years]
proj_sfd = [completions[y]['SFD/SFA'] for y in projected_years]
proj_sm  = [completions[y]['2-4'] for y in projected_years]
proj_lg  = [completions[y]['5+'] for y in projected_years]

C_ACTUAL   = '#2D5F8A'
C_ADU      = '#2D8A5F'
C_SFD      = '#5B9BD5'
C_SMALL    = '#5F8A2D'
C_LARGE    = '#8A5F2D'
C_RED      = '#8B0000'
C_ZERO     = '#CC0000'

fig, ax = plt.subplots(1, 1, figsize=(16, 8))
fig.patch.set_facecolor('#FAFAFA')

x = np.arange(len(years))
bar_width = 0.65

# Actual completions (solid bars)
n_actual = len(actual_years)
ax.bar(x[:n_actual], actual_vals, color=C_ACTUAL, width=bar_width, zorder=3)

# Projected completions (stacked by type, hatched)
x_proj = x[n_actual:]
n_proj = len(projected_years)

ax.bar(x_proj, proj_adu, color=C_ADU, width=bar_width, zorder=3, alpha=0.7)
bot2 = proj_adu
ax.bar(x_proj, proj_sfd, bottom=bot2, color=C_SFD, width=bar_width, zorder=3, alpha=0.7)
bot3 = [a + s for a, s in zip(bot2, proj_sfd)]
ax.bar(x_proj, proj_sm, bottom=bot3, color=C_SMALL, width=bar_width, zorder=3, alpha=0.7)
bot4 = [b + s for b, s in zip(bot3, proj_sm)]
ax.bar(x_proj, proj_lg, bottom=bot4, color=C_LARGE, width=bar_width, zorder=3, alpha=0.7)

# Dashed outline on projected bars to show they're estimates
for i, pv in enumerate(projected_vals):
    xi = x_proj[i]
    rect = plt.Rectangle((xi - bar_width/2, 0), bar_width, pv,
                          fill=False, edgecolor='#666', linewidth=1, linestyle='--', zorder=4)
    ax.add_patch(rect)

# Labels
all_vals = actual_vals + projected_vals
for i, v in enumerate(all_vals):
    if v > 0:
        ax.text(i, v + 15, f'{v:,}', ha='center', va='bottom',
                fontsize=10, fontweight='bold', color='#1A1A1A')
    else:
        ax.text(i, 15, '0', ha='center', va='bottom',
                fontsize=12, fontweight='bold', color=C_ZERO)

# Divider line between actual and projected
ax.axvline(x=n_actual - 0.5, color='#333', linestyle='-', linewidth=1, alpha=0.5)
ax.text(n_actual - 0.45, max(all_vals) * 0.98, 'Actual ←',
        fontsize=9, color='#666', ha='right', va='top')
ax.text(n_actual - 0.35, max(all_vals) * 0.98, '→ Projected',
        fontsize=9, color='#666', ha='left', va='top', fontstyle='italic')

# Policy lines
# 2020=0, 2021=1, ..., 2023=3
ax.axvline(x=3.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(3.55, max(all_vals) * 0.88, '86 du/acre\ncap adopted\nOct 2023',
        fontsize=8, color=C_RED, fontstyle='italic', va='top')

# RHNA deadline
ax.axvline(x=11.0, color='#4B0082', linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(11.05, max(all_vals) * 0.95, 'RHNA 6th cycle\ndeadline 2031',
        fontsize=8, color='#4B0082', fontstyle='italic', va='top')

# Annotation: production cliff
cliff_year_idx = years.index(2027)
ax.annotate(
    'Production cliff:\napproval drought\nhits construction',
    xy=(cliff_year_idx, all_vals[cliff_year_idx] + 20),
    xytext=(cliff_year_idx - 1.5, max(all_vals) * 0.55),
    fontsize=10, fontweight='bold', color=C_RED, ha='center',
    arrowprops=dict(arrowstyle='->', color=C_RED, lw=2),
)

ax.set_ylabel('Housing Units Completed', fontsize=13, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(year_labels, fontsize=10)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)
ax.set_ylim(bottom=0)

# ─── Title + Legend ───

fig.suptitle('Oceanside Projected Housing Production\n'
             'Actual Completions (2020–2024) → Modeled from Approval Pipeline',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_ACTUAL, label='Actual completions'),
    mpatches.Patch(facecolor=C_ADU, alpha=0.7, label='ADU (projected)'),
    mpatches.Patch(facecolor=C_SFD, alpha=0.7, label='SFD/SFA (projected)'),
    mpatches.Patch(facecolor=C_LARGE, alpha=0.7, label='5+ units (projected)'),
]
fig.legend(handles=legend_elements, loc='upper center', bbox_to_anchor=(0.5, 0.91),
           fontsize=10, framealpha=0.9, ncol=4)

plt.tight_layout(rect=[0.02, 0.02, 0.98, 0.86])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-projected-production.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('\nSaved:', out)

# ─── Summary stats ───
print('\n=== Summary ===')
actual_total = sum(ACTUAL_COMPLETIONS.values())
projected_26_31 = sum(sum(completions[y].values()) for y in range(2025, 2032))
print(f'Actual completions 2020-2024: {actual_total:,} ({actual_total/5:.0f}/yr avg)')
print(f'Projected completions 2025-2031: {projected_26_31:,} ({projected_26_31/7:.0f}/yr avg)')
print(f'Production decline: {(1 - (projected_26_31/7)/(actual_total/5))*100:.0f}%')
annual_actual = actual_total / 5
annual_proj = projected_26_31 / 7
print(f'Average annual: {annual_actual:.0f} (actual) → {annual_proj:.0f} (projected)')
