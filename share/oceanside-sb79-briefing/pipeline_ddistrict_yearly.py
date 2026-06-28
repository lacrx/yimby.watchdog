#!/usr/bin/env python3
"""Housing units FILED by calendar year — D-District bounding box filter.

Uses HCD APR Table A geocoded filings filtered to the D-District extent
(lat 33.1858–33.2096, lon -117.3923–-117.3749).
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np
import csv
from datetime import datetime

LAT_MIN, LAT_MAX = 33.1858, 33.2096
LON_MIN, LON_MAX = -117.3923, -117.3749

aff_cols = ['ACUTELY_LOW_INCOME_DR','ACUTELY_LOW_INCOME_NDR',
            'EXTREMELY_LOW_INCOME_DR','EXTREMELY_LOW_INCOME_NDR',
            'VLOW_INCOME_DR','VLOW_INCOME_NDR',
            'LOW_INCOME_DR','LOW_INCOME_NDR']

with open('data/hcd-apr-tablea.csv') as f:
    r = csv.DictReader(f)
    rows = []
    for row in r:
        if row['JURIS_NAME'] != 'OCEANSIDE':
            continue
        try:
            lat = float(row['LATITUDE'])
            lon = float(row['LONGITUDE'])
        except (ValueError, TypeError):
            continue
        if LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX:
            rows.append(row)

print(f'{len(rows)} Oceanside filings inside D-District bounding box')

total_filed = []
aff_filed = []
year_range = range(2021, 2026)

for year in year_range:
    yr_rows = []
    for row in rows:
        dt_str = row.get('APP_SUBMIT_DT', '')
        if not dt_str:
            continue
        try:
            dt = datetime.strptime(dt_str, '%Y-%m-%d')
        except Exception:
            try:
                dt = datetime.strptime(dt_str, '%m/%d/%Y')
            except Exception:
                continue
        if dt.year == year:
            yr_rows.append(row)

    units = sum(int(row['TOT_PROPOSED_UNITS'] or 0) for row in yr_rows)
    aff = sum(int(row.get(c, '') or 0) for row in yr_rows for c in aff_cols)
    total_filed.append(units)
    aff_filed.append(aff)
    print(f'  {year}: {units} total, {aff} affordable ({len(yr_rows)} records)')

total_filed.append(0)
aff_filed.append(0)
print(f'  2026: 0 total, 0 affordable (eTRAKiT: zero D-District housing filings as of Jun 18)')
year_range = range(2021, 2027)

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8.5), sharey=False)
fig.patch.set_facecolor('#FAFAFA')

C_AFFORDABLE = '#1B6B3A'
C_MARKET = '#2D5F8A'
C_RED = '#8B0000'
C_AVG = '#FF8C00'

years = [str(y) for y in year_range]
x = np.arange(len(years))

market_filed = [t - a for t, a in zip(total_filed, aff_filed)]
bar_width = 0.6

# ─── LEFT PANEL: Total Units Filed ───

bars_aff = ax1.bar(x, aff_filed, color=C_AFFORDABLE, width=bar_width)
bars_mkt = ax1.bar(x, market_filed, bottom=aff_filed, color=C_MARKET, width=bar_width)

for i, t in enumerate(total_filed):
    ax1.text(i, t + 15, '{:,}'.format(t), ha='center', va='bottom',
             fontsize=10, fontweight='bold', color='#1A1A1A')

# Nov 2021: 43 du/acre cap removed — between 2021 (idx 0) and 2022 (idx 1)
ax1.axvline(x=0.5, color='#228B22', linestyle=':', linewidth=2.5, alpha=0.7)
ax1.text(0.45, 1800, '43 du/acre\ncap removed\nNov 2021',
         fontsize=8, color='#228B22', fontstyle='italic', va='top', ha='right')

# Oct 2023: 86 du/acre cap adopted — between 2023 (idx 2) and 2024 (idx 3)
ax1.axvline(x=2.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.7)
ax1.text(2.55, 1800, '86 du/acre\ncap adopted\nOct 2023',
         fontsize=8, color=C_RED, fontstyle='italic', va='top')

# Feb 2026: CCC certified — early in 2026 (idx 5)
ax1.axvline(x=4.7, color='#4B0082', linestyle=':', linewidth=2.5, alpha=0.7)
ax1.text(4.75, 1800, 'CCC\ncertified\nFeb 2026',
         fontsize=8, color='#4B0082', fontstyle='italic', va='top')

ax1.set_ylabel('Housing Units Filed', fontsize=12, fontweight='bold')
ax1.set_title('Total Units Filed by Year', fontsize=13,
              fontweight='bold', color='#1A1A1A')
ax1.set_xticks(x)
ax1.set_xticklabels(years, fontsize=9)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)

# ─── RIGHT PANEL: Affordable Only ───

ax2.bar(x, aff_filed, color=C_AFFORDABLE, width=bar_width)

for i, a in enumerate(aff_filed):
    if a > 0:
        ax2.text(i, a + 3, str(a), ha='center', va='bottom', fontsize=11,
                 fontweight='bold', color=C_AFFORDABLE)
    else:
        ax2.text(i, 3, '0', ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=C_RED)

ax2.axvline(x=0.5, color='#228B22', linestyle=':', linewidth=2.5, alpha=0.7)
ax2.axvline(x=2.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.7)
ax2.axvline(x=4.7, color='#4B0082', linestyle=':', linewidth=2.5, alpha=0.7)

ax2.set_ylabel('Affordable Units Filed', fontsize=12, fontweight='bold')
ax2.set_title('Affordable Units Only', fontsize=13, fontweight='bold', color='#1A1A1A')
ax2.set_xticks(x)
ax2.set_xticklabels(years, fontsize=9)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

# ─── Footnotes ───


# ─── Title ───

fig.suptitle('Oceanside D-District Housing Application Filings\n'
             'By Calendar Year, 2021–2026',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_AFFORDABLE, label='Affordable units'),
    mpatches.Patch(facecolor=C_MARKET, label='Market rate units'),
]
fig.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(0.97, 0.92),
           fontsize=9.5, framealpha=0.9)

plt.tight_layout(rect=[0.02, 0.04, 0.98, 0.92])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-ddistrict-filings-yearly.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('Saved:', out)
