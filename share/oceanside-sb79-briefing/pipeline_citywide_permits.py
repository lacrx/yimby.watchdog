#!/usr/bin/env python3
"""Citywide housing units FILED by year, 2018–2026.

2018–2025: HCD APR Table A (APP_SUBMIT_DT), same source as D-District chart.
2026: Deduped eTRAKiT — discretionary + ADU + non-entitled SFD/MF only.
       Excludes Olive Park (DB24-00001) and North River Farms tract lots.
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import numpy as np
import csv
from datetime import datetime

AFF_COLS = ['ACUTELY_LOW_INCOME_DR','ACUTELY_LOW_INCOME_NDR',
            'EXTREMELY_LOW_INCOME_DR','EXTREMELY_LOW_INCOME_NDR',
            'VLOW_INCOME_DR','VLOW_INCOME_NDR',
            'LOW_INCOME_DR','LOW_INCOME_NDR']

# ─── Load HCD APR data for 2018–2025 ───

hcd = {}
aff = {}
for yr in range(2020, 2026):
    hcd[yr] = {'ADU': 0, 'SFD/SFA': 0, '2-4': 0, '5+': 0}
    aff[yr] = {'ADU': 0, 'SFD/SFA': 0, '2-4': 0, '5+': 0}

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

        hcd[yr][key] += units
        aff[yr][key] += aff_units

# ─── 2026: deduped eTRAKiT ───
# DB26-00001 (142 units, AB 2011) + 67 ADUs + 4 non-NRF SFDs + 6 MF
# 5.5 months of data (Jan 1 – Jun 20). Prorate to full year: × 12/5.5
MONTHS_ELAPSED = 5.5
PRORATE = 12 / MONTHS_ELAPSED
hcd_2026_actual = {'ADU': 67, 'SFD/SFA': 4, '2-4': 6, '5+': 142}
hcd[2026] = {k: round(v * PRORATE) for k, v in hcd_2026_actual.items()}
# 2026 affordable: DB26-00001 is a density bonus project.
# Oceanside inclusionary ordinance requires 15% affordable (adopted Dec 2023).
# 15% of 142 = 21 affordable units (lower income, ≤80% AMI). Statutory floor.
aff_2026_actual = {'ADU': 0, 'SFD/SFA': 0, '2-4': 0, '5+': 21}
aff[2026] = {k: round(v * PRORATE) for k, v in aff_2026_actual.items()}

# ─── Print table ───
print(f"{'Year':<12} {'ADU':>5} {'SFD/SFA':>8} {'2-4':>5} {'5+':>6} {'Total':>7} {'Aff':>5} {'Aff%':>5}")
for yr in range(2020, 2027):
    d = hcd[yr]
    a = aff[yr]
    total = sum(d.values())
    aff_t = sum(a.values())
    pct = f"{aff_t/total*100:.0f}%" if total else "0%"
    label = f"{yr} (pror.)" if yr == 2026 else str(yr)
    print(f"{label:<12} {d['ADU']:>5} {d['SFD/SFA']:>8} {d['2-4']:>5} {d['5+']:>6} {total:>7} {aff_t:>5} {pct:>5}")

# ─── Chart ───

years = [str(y) for y in range(2020, 2026)] + ['2026\n(prorated)']

adu     = [hcd[y]['ADU'] for y in range(2020, 2027)]
sfd_sfa = [hcd[y]['SFD/SFA'] for y in range(2020, 2027)]
small   = [hcd[y]['2-4'] for y in range(2020, 2027)]
large   = [hcd[y]['5+'] for y in range(2020, 2027)]

totals = [a + s + sm + lg for a, s, sm, lg in zip(adu, sfd_sfa, small, large)]

aff_total = [sum(aff[y].values()) for y in range(2020, 2027)]
aff_5plus = [aff[y]['5+'] for y in range(2020, 2027)]
mkt_5plus = [lg - a5 for lg, a5 in zip(large, aff_5plus)]

C_ADU   = '#2D8A5F'
C_SFD   = '#2D5F8A'
C_SMALL = '#5F8A2D'
C_LARGE = '#8A5F2D'
C_AFF   = '#1B6B3A'
C_RED   = '#8B0000'

fig, ax = plt.subplots(1, 1, figsize=(12, 8))
fig.patch.set_facecolor('#FAFAFA')

x = np.arange(len(years))
bar_width = 0.6

# ─── Stacked bars by type + affordable line ───

ax.bar(x, adu, color=C_ADU, width=bar_width)
ax.bar(x, sfd_sfa, bottom=adu, color=C_SFD, width=bar_width)
bot3 = [a + s for a, s in zip(adu, sfd_sfa)]
ax.bar(x, small, bottom=bot3, color=C_SMALL, width=bar_width)
bot4 = [b + sm for b, sm in zip(bot3, small)]
ax.bar(x, large, bottom=bot4, color=C_LARGE, width=bar_width)

for i, t in enumerate(totals):
    ax.text(i, t + 30, '{:,}'.format(t), ha='center', va='bottom',
            fontsize=11, fontweight='bold', color='#1A1A1A')

# Affordable line overlay
ax.plot(x, aff_total, color=C_AFF, marker='o', markersize=7, linewidth=2.5,
        zorder=5, markeredgecolor='white', markeredgewidth=1.5)
for i, a in enumerate(aff_total):
    if a > 0:
        ax.text(i, a + 40, str(a), ha='center', va='bottom', fontsize=9,
                fontweight='bold', color=C_AFF,
                bbox=dict(facecolor='white', edgecolor='none', alpha=0.8, pad=1.5))

# Policy lines
# 2020=0, 2021=1, 2022=2, 2023=3, 2024=4, 2025=5, 2026=6
ax.axvline(x=1.5, color='#228B22', linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(1.45, max(totals) * 0.75, '43 du/acre\ncap removed\nNov 2021',
        fontsize=9, color='#228B22', fontstyle='italic', va='top', ha='right')

ax.axvline(x=3.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(3.55, max(totals) * 0.95, '86 du/acre\ncap adopted\nOct 2023',
        fontsize=9, color=C_RED, fontstyle='italic', va='top')

ax.axvline(x=5.7, color='#4B0082', linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(5.75, max(totals) * 0.95, 'CCC\ncertified\nFeb 2026',
        fontsize=9, color='#4B0082', fontstyle='italic', va='top')

ax.set_ylabel('Housing Units Filed', fontsize=13, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(years, fontsize=10)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

# ─── Title + Legend ───

fig.suptitle('Oceanside Citywide Housing Units Filed\n'
             '2020–2026  ·  Source: HCD APR Table A (2026: eTRAKiT, deduped)',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_ADU, label='ADU'),
    mpatches.Patch(facecolor=C_SFD, label='SFD / SFA'),
    mpatches.Patch(facecolor=C_SMALL, label='2–4 units'),
    mpatches.Patch(facecolor=C_LARGE, label='5+ units'),
    mlines.Line2D([], [], color=C_AFF, marker='o', markersize=5, linewidth=2,
                  markeredgecolor='white', label='Affordable (deed-restricted)'),
]
fig.legend(handles=legend_elements, loc='upper center', bbox_to_anchor=(0.5, 0.91),
           fontsize=10, framealpha=0.9, ncol=5)

plt.tight_layout(rect=[0.02, 0.02, 0.98, 0.86])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-citywide-filings-yearly.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('Saved:', out)
