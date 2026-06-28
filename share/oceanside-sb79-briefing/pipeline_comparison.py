#!/usr/bin/env python3
"""Housing pipeline collapse chart — D-District (Article 12) approvals only.

Data: eTRAKiT entitlement approval dates × HCD APR Table A unit counts.
Filtered to projects within the official D-District zoning boundary
(gis.oceansideca.org polygon, point-in-polygon verified).
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 8), sharey=False)
fig.patch.set_facecolor('#FAFAFA')

C_AFFORDABLE = '#1B6B3A'
C_MARKET = '#2D5F8A'
C_GHOST = '#B8B8B8'
C_RED = '#8B0000'
C_AVG = '#FF8C00'

years = ['Feb-Jun\n2022', 'Feb-Jun\n2023', 'Feb-Jun\n2024', 'Feb-Jun\n2025', 'Feb-Jun\n2026']
x = np.arange(len(years))

# D-District-only approved projects (verified by GIS polygon):
# 2022: (none in D-District)
# 2023: RD22-00002 901 Pier View (64u/7a)
# 2024: RD23-00003 712 Seagaze (179u/18a) + RD22-00005 Modera Neptune (360u/36a)
# 2025: T24-00001 Kelly St (36u/3a) — in D-12 per GIS
# 2026: RD23-00005 801 Mission (208u/21a) — vested pre-cap
affordable = [0,    7,   54,    3,   21]
market =     [0,   57,  485,   33,  187]
ghost =      [0,    0,    0,    0,    0]

# 2026 is entirely vested pre-cap
ghost[4] = market[4] + affordable[4]
market[4] = 0
affordable[4] = 0

totals = [a + m + g for a, m, g in zip(affordable, market, ghost)]
bar_width = 0.55

# ─── LEFT PANEL ───

bars_aff = ax1.bar(x, affordable, color=C_AFFORDABLE, width=bar_width)
bars_mkt = ax1.bar(x, market, bottom=affordable, color=C_MARKET, width=bar_width)
bars_ghost = ax1.bar(x, ghost, bottom=[a + m for a, m in zip(affordable, market)],
                     color=C_GHOST, width=bar_width, edgecolor='#888888',
                     linestyle='--', linewidth=1.2)

for i, t in enumerate(totals):
    color = C_RED if i >= 4 else '#1A1A1A'
    label = '{:,}'.format(t)
    if i == 4:
        label += '*'
    if t == 0:
        ax1.text(i, 12, '0', ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=C_RED)
    else:
        ax1.text(i, t + 12, label, ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=color)

avg_precap = np.mean(totals[:3])
ax1.axhline(y=avg_precap, color=C_AVG, linestyle='--', linewidth=2, alpha=0.7)
ax1.text(0.1, avg_precap + 15, '2022-24 avg: {:,.0f}'.format(avg_precap),
         fontsize=10, color=C_AVG, fontweight='bold')

ax1.axvline(x=3.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.6)
ax1.text(3.55, 520, 'Density cap\ncertified\nFeb 5, 2026', fontsize=8,
         color=C_RED, fontstyle='italic', va='top')

ax1.set_ylabel('Housing Units Approved', fontsize=12, fontweight='bold')
ax1.set_title('D-District Units Approved per Feb-Jun Window', fontsize=13,
              fontweight='bold', color='#1A1A1A')
ax1.set_ylim(0, 600)
ax1.set_xticks(x)
ax1.set_xticklabels(years, fontsize=9.5)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)

# ─── RIGHT PANEL: Affordable Only ───

aff_values = [0, 7, 54, 3, 21]
aff_ghost = [0, 0, 0, 0, 21]
aff_real = [0, 7, 54, 3, 0]

bar_colors = [C_AFFORDABLE if i < 4 else C_RED for i in range(len(years))]
ax2.bar(x, aff_real, color=bar_colors, width=bar_width)
ax2.bar(x, aff_ghost, bottom=aff_real, color=C_GHOST, width=bar_width,
        edgecolor='#888888', linestyle='--', linewidth=1.2)

for i, a in enumerate(aff_values):
    color = C_RED if i >= 4 else C_AFFORDABLE
    if a == 0 and i < 4:
        ax2.text(i, 1.5, '0', ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=C_RED)
    else:
        label = str(a)
        if i == 4:
            label += '*'
        ax2.text(i, a + 1.5, label, ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=color)

avg_aff = np.mean(aff_values[:3])
ax2.axhline(y=avg_aff, color=C_AVG, linestyle='--', linewidth=2, alpha=0.7)
ax2.text(0.1, avg_aff + 2, '2022-24 avg: {:,.0f}'.format(avg_aff),
         fontsize=10, color=C_AVG, fontweight='bold')

ax2.axvline(x=3.5, color=C_RED, linestyle=':', linewidth=2.5, alpha=0.6)

ax2.set_ylabel('Affordable Units Approved', fontsize=12, fontweight='bold')
ax2.set_title('Affordable Units Only', fontsize=13, fontweight='bold', color='#1A1A1A')
ax2.set_ylim(0, 70)
ax2.set_xticks(x)
ax2.set_xticklabels(years, fontsize=9.5)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

# ─── Footnotes ───

fig.text(0.04, 0.115,
         '* 2026: 801 Mission Ave (208 units, 21 affordable) filed Nov 2023 before density cap.\n'
         '   153 du/acre with 8 density bonus waivers — could not be approved under current rules. '
         'Remove it and 2026 drops to zero.',
         fontsize=8.5, color='#444444', fontstyle='italic', va='top', family='sans-serif')

fig.text(0.04, 0.06,
         'Data: eTRAKiT approval dates × HCD APR Table A unit counts, filtered to D-District (Article 12)\n'
         'using official zoning polygon from gis.oceansideca.org. Excludes non-housing and companion records.',
         fontsize=8.5, color='#666666', va='top', family='sans-serif')

fig.text(0.04, 0.02,
         'CCC certified the downtown density cap on Feb 5, 2026. '
         'No new D-District housing project has been filed or approved since.',
         fontsize=10, color=C_RED, fontweight='bold', va='top', family='sans-serif')

# ─── Title ───

fig.suptitle('Oceanside D-District Housing Pipeline Collapse\n'
             'After Coastal Commission Density Cap Certification (Feb 5, 2026)',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_AFFORDABLE, label='Affordable units'),
    mpatches.Patch(facecolor=C_MARKET, label='Market rate units'),
    mpatches.Patch(facecolor=C_GHOST, edgecolor='#888888', linestyle='--',
                   label='Vested pre-cap (not repeatable)'),
    mpatches.Patch(facecolor='none', edgecolor=C_AVG, linestyle='--', linewidth=2,
                   label='2022–2024 average'),
]
fig.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(0.97, 0.92),
           fontsize=9, framealpha=0.9)

plt.tight_layout(rect=[0.02, 0.14, 0.98, 0.92])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-pipeline-collapse.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('Saved:', out)
