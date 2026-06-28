#!/usr/bin/env python3
"""Housing units FILED per Feb-Jun window — pure HCD APR Table A data, city-wide."""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import csv
from datetime import datetime

aff_cols = ['ACUTELY_LOW_INCOME_DR','ACUTELY_LOW_INCOME_NDR',
            'EXTREMELY_LOW_INCOME_DR','EXTREMELY_LOW_INCOME_NDR',
            'VLOW_INCOME_DR','VLOW_INCOME_NDR',
            'LOW_INCOME_DR','LOW_INCOME_NDR']

with open('data/hcd-apr-tablea.csv') as f:
    r = csv.DictReader(f)
    rows = [row for row in r if row['JURIS_NAME'] == 'OCEANSIDE']

total_filed = []
aff_filed = []

for year in range(2018, 2026):
    fj = []
    for row in rows:
        dt_str = row.get('APP_SUBMIT_DT', '')
        if not dt_str:
            continue
        try:
            dt = datetime.strptime(dt_str, '%Y-%m-%d')
        except:
            try:
                dt = datetime.strptime(dt_str, '%m/%d/%Y')
            except:
                continue
        if dt.year == year and 2 <= dt.month <= 6:
            fj.append(row)

    units = sum(int(row['TOT_PROPOSED_UNITS'] or 0) for row in fj)
    aff = sum(int(row.get(c, '') or 0) for row in fj for c in aff_cols)
    total_filed.append(units)
    aff_filed.append(aff)

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8.5), sharey=False)
fig.patch.set_facecolor('#FAFAFA')

C_AFFORDABLE = '#1B6B3A'
C_MARKET = '#2D5F8A'
C_RED = '#8B0000'
C_AVG = '#FF8C00'

years = ['Feb-Jun\n2018', 'Feb-Jun\n2019', 'Feb-Jun\n2020', 'Feb-Jun\n2021',
         'Feb-Jun\n2022', 'Feb-Jun\n2023', 'Feb-Jun\n2024', 'Feb-Jun\n2025']
x = np.arange(len(years))

market_filed = [t - a for t, a in zip(total_filed, aff_filed)]
bar_width = 0.6

# ─── LEFT PANEL: Total Units Filed ───

bars_aff = ax1.bar(x, aff_filed, color=C_AFFORDABLE, width=bar_width)
bars_mkt = ax1.bar(x, market_filed, bottom=aff_filed, color=C_MARKET, width=bar_width)

for i, t in enumerate(total_filed):
    ax1.text(i, t + 25, '{:,}'.format(t), ha='center', va='bottom',
             fontsize=10, fontweight='bold', color='#1A1A1A')

avg = np.mean(total_filed)
ax1.axhline(y=avg, color=C_AVG, linestyle='--', linewidth=2, alpha=0.7)
ax1.text(0.3, avg + 30, 'Avg: {:,.0f}'.format(avg),
         fontsize=10, color=C_AVG, fontweight='bold')

ax1.set_ylabel('Housing Units Filed', fontsize=12, fontweight='bold')
ax1.set_title('Total Units Filed per Feb-Jun Window', fontsize=13,
              fontweight='bold', color='#1A1A1A')
ax1.set_ylim(0, 1800)
ax1.set_xticks(x)
ax1.set_xticklabels(years, fontsize=8)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)

# ─── RIGHT PANEL: Affordable Only ───

ax2.bar(x, aff_filed, color=C_AFFORDABLE, width=bar_width)

for i, a in enumerate(aff_filed):
    if a > 0:
        ax2.text(i, a + 5, str(a), ha='center', va='bottom', fontsize=11,
                 fontweight='bold', color=C_AFFORDABLE)
    else:
        ax2.text(i, 5, '0', ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=C_RED)

avg_aff = np.mean(aff_filed)
ax2.axhline(y=avg_aff, color=C_AVG, linestyle='--', linewidth=2, alpha=0.7)
ax2.text(0.3, avg_aff + 6, 'Avg: {:,.0f}'.format(avg_aff),
         fontsize=10, color=C_AVG, fontweight='bold')

ax2.set_ylabel('Affordable Units Filed', fontsize=12, fontweight='bold')
ax2.set_title('Affordable Units Only', fontsize=13, fontweight='bold', color='#1A1A1A')
ax2.set_ylim(0, 300)
ax2.set_xticks(x)
ax2.set_xticklabels(years, fontsize=8)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

# ─── Footnotes ───

fig.text(0.04, 0.10,
         'Source: HCD Annual Progress Report Table A (data.ca.gov), city-wide.\n'
         'Units counted by application filing date (APP_SUBMIT_DT).\n'
         'Affordable = Very Low + Low + Extremely Low + Acutely Low income categories.',
         fontsize=8.5, color='#666666', va='top', family='sans-serif')

fig.text(0.04, 0.055,
         'CCC certified the downtown density cap (5,500 units / 86 du/acre) on Feb 5, 2026.\n'
         'SB 79 evasion ordinance adopted June 3, 2026 defers remaining sites to 2032.\n'
         '2026 APR data not yet reported.',
         fontsize=9, color='#444444', fontstyle='italic', va='top', family='sans-serif')

fig.text(0.04, 0.015,
         'Filings dropped 80% from peak (1,623 → 326) before the cap even took effect.',
         fontsize=11, color=C_RED, fontweight='bold', va='top', family='sans-serif')

# ─── Title ───

fig.suptitle('Oceanside Housing Application Filings\n'
             'Feb-Jun Window, 2018–2025 (HCD Annual Progress Report)',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_AFFORDABLE, label='Affordable units'),
    mpatches.Patch(facecolor=C_MARKET, label='Market rate units'),
    mpatches.Patch(facecolor='none', edgecolor=C_AVG, linestyle='--', linewidth=2,
                   label='2018–2025 average'),
]
fig.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(0.97, 0.92),
           fontsize=9.5, framealpha=0.9)

plt.tight_layout(rect=[0.02, 0.13, 0.98, 0.92])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-filings-collapse.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('Saved:', out)
