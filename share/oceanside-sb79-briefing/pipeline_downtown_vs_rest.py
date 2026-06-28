#!/usr/bin/env python3
"""Housing units filed: Downtown D-District vs Rest of City, 2018–2026.

Same HCD APR source as both other charts. Downtown defined by D-District
bounding box (lat 33.1858–33.2096, lon -117.3923–-117.3749).
2026: eTRAKiT deduped. DB26-00001 (142 units) is outside D-District (1640 Oceanside Blvd).
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

downtown = {}
rest = {}
for yr in range(2018, 2026):
    downtown[yr] = 0
    rest[yr] = 0

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

        try:
            lat = float(row['LATITUDE'])
            lon = float(row['LONGITUDE'])
        except (ValueError, TypeError):
            rest[yr] += units
            continue

        if LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX:
            downtown[yr] += units
        else:
            rest[yr] += units

# 2026: prorated from 5.5 months
# DB26-00001 (142 units) is at 1640 Oceanside Blvd — NOT downtown
# Downtown: 0 housing filings in D-District in 2026
# Rest: 67 ADU + 4 SFD + 6 MF + 142 (DB26-00001) = 219 actual, prorated
PRORATE = 12 / 5.5
downtown[2026] = 0
rest[2026] = round(219 * PRORATE)

# ─── Print ───
print(f"{'Year':<6} {'Downtown':>10} {'Rest':>10} {'Total':>10} {'Dwtn %':>8}")
for yr in range(2018, 2027):
    d, r = downtown[yr], rest[yr]
    t = d + r
    pct = f"{d/t*100:.0f}%" if t else "0%"
    label = f"{yr}*" if yr == 2026 else str(yr)
    print(f"{label:<6} {d:>10} {r:>10} {t:>10} {pct:>8}")

# ─── Chart ───

years = [str(y) for y in range(2018, 2026)] + ['2026\n(prorated)']

dt_vals = [downtown[y] for y in range(2018, 2027)]
rest_vals = [rest[y] for y in range(2018, 2027)]
totals = [d + r for d, r in zip(dt_vals, rest_vals)]

C_DOWNTOWN = '#8B0000'
C_REST     = '#2D5F8A'
C_ZERO     = '#CC0000'

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8.5), sharey=False)
fig.patch.set_facecolor('#FAFAFA')

x = np.arange(len(years))
bar_width = 0.6

# ─── LEFT: Stacked downtown + rest ───

b1 = ax1.bar(x, dt_vals, color=C_DOWNTOWN, width=bar_width)
b2 = ax1.bar(x, rest_vals, bottom=dt_vals, color=C_REST, width=bar_width)

for i, t in enumerate(totals):
    ax1.text(i, t + 30, '{:,}'.format(t), ha='center', va='bottom',
             fontsize=10, fontweight='bold', color='#1A1A1A')

# Policy lines
ax1.axvline(x=3.5, color='#228B22', linestyle=':', linewidth=2.5, alpha=0.7)
ax1.text(3.45, max(totals) * 0.95, '43 du/acre\ncap removed\nNov 2021',
         fontsize=8, color='#228B22', fontstyle='italic', va='top', ha='right')

ax1.axvline(x=5.5, color=C_DOWNTOWN, linestyle=':', linewidth=2.5, alpha=0.7)
ax1.text(5.55, max(totals) * 0.95, '86 du/acre\ncap adopted\nOct 2023',
         fontsize=8, color=C_DOWNTOWN, fontstyle='italic', va='top')

ax1.set_ylabel('Housing Units Filed', fontsize=12, fontweight='bold')
ax1.set_title('All Units Filed — Downtown vs. Rest of City', fontsize=13,
              fontweight='bold', color='#1A1A1A')
ax1.set_xticks(x)
ax1.set_xticklabels(years, fontsize=9)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)

# ─── RIGHT: Downtown only ───

bars_dt = ax2.bar(x, dt_vals, color=C_DOWNTOWN, width=bar_width)
for i, v in enumerate(dt_vals):
    color = C_ZERO if v == 0 else C_DOWNTOWN
    label = '0' if v == 0 else '{:,}'.format(v)
    ax2.text(i, max(v, 20), label, ha='center', va='bottom', fontsize=11,
             fontweight='bold', color=color)

ax2.axvline(x=3.5, color='#228B22', linestyle=':', linewidth=2.5, alpha=0.7)
ax2.text(3.45, max(dt_vals) * 0.95, '43 du/acre\ncap removed\nNov 2021',
         fontsize=8, color='#228B22', fontstyle='italic', va='top', ha='right')

ax2.axvline(x=5.5, color=C_DOWNTOWN, linestyle=':', linewidth=2.5, alpha=0.7)
ax2.text(5.55, max(dt_vals) * 0.95, '86 du/acre\ncap adopted\nOct 2023',
         fontsize=8, color=C_DOWNTOWN, fontstyle='italic', va='top')

ax2.set_ylabel('Downtown D-District Units Filed', fontsize=12, fontweight='bold')
ax2.set_title('Downtown D-District Only', fontsize=13,
              fontweight='bold', color='#1A1A1A')
ax2.set_xticks(x)
ax2.set_xticklabels(years, fontsize=9)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

# ─── Title + Legend ───

fig.suptitle('Oceanside Housing Units Filed: Downtown vs. Rest of City\n'
             '2018–2026  ·  Source: HCD APR Table A (2026: eTRAKiT, deduped)',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_DOWNTOWN, label='Downtown D-District'),
    mpatches.Patch(facecolor=C_REST, label='Rest of City'),
]
fig.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(0.97, 0.92),
           fontsize=9.5, framealpha=0.9)

plt.tight_layout(rect=[0.02, 0.04, 0.98, 0.92])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-downtown-vs-rest-filings.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('Saved:', out)
