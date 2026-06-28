#!/usr/bin/env python3
"""Generate verified housing pipeline chart from eTRAKiT + HCD APR data."""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 8.5), sharey=False)
fig.patch.set_facecolor('#FAFAFA')

C_AFFORDABLE = '#1B6B3A'
C_MARKET = '#2D5F8A'
C_GHOST = '#B8B8B8'
C_RED = '#8B0000'
C_AVG = '#FF8C00'

# ─── LEFT PANEL: Feb-Jun Approvals by Year ───

years = ['Feb-Jun\n2020', 'Feb-Jun\n2021', 'Feb-Jun\n2022', 'Feb-Jun\n2023',
         'Feb-Jun\n2024', 'Feb-Jun\n2025', 'Feb-Jun\n2026']
x = np.arange(len(years))

affordable = [26,  0,   3,  40,  54,   3,  21]
market =     [354, 0,  39, 373, 485,  35, 187]
ghost =      [0,   0,   0,   0,   0,   0,   0]

# 2026 is entirely vested pre-cap → ghost category
ghost[6] = market[6] + affordable[6]
market[6] = 0
affordable[6] = 0

totals = [a + m + g for a, m, g in zip(affordable, market, ghost)]
bar_width = 0.55

bars_aff = ax1.bar(x, affordable, color=C_AFFORDABLE, width=bar_width)
bars_mkt = ax1.bar(x, market, bottom=affordable, color=C_MARKET, width=bar_width)
bars_ghost = ax1.bar(x, ghost, bottom=[a + m for a, m in zip(affordable, market)],
                     color=C_GHOST, width=bar_width, edgecolor='#888888',
                     linestyle='--', linewidth=1.2)

# Labels above bars
for i, t in enumerate(totals):
    if t > 0:
        color = C_RED if i >= 5 else '#1A1A1A'
        label = '{:,}'.format(t)
        if i == 6:
            label += '*'
        ax1.text(i, t + 12, label, ha='center', va='bottom', fontsize=11,
                 fontweight='bold', color=color)

# Pre-cap average line (2020-2024)
avg_precap = np.mean(totals[:5])
ax1.axhline(y=avg_precap, color=C_AVG, linestyle='--', linewidth=2, alpha=0.7)
ax1.text(0.5, avg_precap + 15, 'Pre-cap avg: {:,.0f}'.format(avg_precap),
         fontsize=10, color=C_AVG, fontweight='bold')

ax1.set_ylabel('Housing Units Approved (Entitlements)', fontsize=11, fontweight='bold')
ax1.set_title('Total Units Approved per Feb-Jun Window', fontsize=13, fontweight='bold',
              color='#1A1A1A')
ax1.set_ylim(0, 620)
ax1.set_xticks(x)
ax1.set_xticklabels(years, fontsize=9)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)

# Vertical line at density cap
ax1.axvline(x=4.5, color=C_RED, linestyle=':', linewidth=2, alpha=0.6)
ax1.text(4.55, 590, 'Density cap\ncertified', fontsize=8, color=C_RED,
         fontstyle='italic', va='top')

# ─── RIGHT PANEL: Affordable Only ───

x2 = np.arange(len(years))
aff_values = [26, 0, 3, 40, 54, 3, 21]
# 2026 affordable is vested pre-cap
aff_ghost = [0, 0, 0, 0, 0, 0, 21]
aff_real = [26, 0, 3, 40, 54, 3, 0]

bar_colors = [C_AFFORDABLE if i < 5 else C_RED for i in range(len(years))]

bars2 = ax2.bar(x2, aff_real, color=bar_colors, width=bar_width)
ax2.bar(x2, aff_ghost, bottom=aff_real, color=C_GHOST, width=bar_width,
        edgecolor='#888888', linestyle='--', linewidth=1.2)

for i in range(len(years)):
    val = aff_values[i]
    if val > 0:
        color = C_RED if i >= 5 else C_AFFORDABLE
        label = str(val)
        if i == 6:
            label += '*'
        ax2.text(i, val + 3, label, ha='center', va='bottom', fontsize=12,
                 fontweight='bold', color=color)
    else:
        ax2.text(i, 3, '0', ha='center', va='bottom', fontsize=11,
                 fontweight='bold', color=C_RED)

avg_aff_precap = np.mean(aff_values[:5])
ax2.axhline(y=avg_aff_precap, color=C_AVG, linestyle='--', linewidth=2, alpha=0.7)
ax2.text(0.5, avg_aff_precap + 3, 'Pre-cap avg: {:,.0f}'.format(avg_aff_precap),
         fontsize=10, color=C_AVG, fontweight='bold')

ax2.axvline(x=4.5, color=C_RED, linestyle=':', linewidth=2, alpha=0.6)
ax2.text(4.55, 62, 'Density cap\ncertified', fontsize=8, color=C_RED,
         fontstyle='italic', va='top')

ax2.set_ylabel('Affordable Units Approved', fontsize=11, fontweight='bold')
ax2.set_title('Affordable Units Only', fontsize=13, fontweight='bold', color='#1A1A1A')
ax2.set_ylim(0, 70)
ax2.set_xticks(x2)
ax2.set_xticklabels(years, fontsize=9)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)

# ─── Footnotes ───

fig.text(0.04, 0.115,
         '* 2026: 801 Mission Ave (208 units, 21 affordable) was filed Nov 2023 before density cap & CCC certification.\n'
         '   At 153 du/acre with 8 density bonus waivers, it could not be approved under current rules. '
         'Remove it and 2026 drops to zero.',
         fontsize=8.5, color='#444444', fontstyle='italic', va='top', family='sans-serif')

fig.text(0.04, 0.065,
         'Data: eTRAKiT entitlement approval dates cross-referenced with HCD Annual Progress Report (Table A) unit counts.\n'
         'Includes Development Plans, Density Bonus, and Tentative Maps. Excludes building permits and companion records.',
         fontsize=8.5, color='#666666', va='top', family='sans-serif')

fig.text(0.04, 0.025,
         '947 housing units filed since density cap — zero approved. '
         'No new downtown development plan filed since Feb 5, 2026.',
         fontsize=10, color=C_RED, fontweight='bold', va='top', family='sans-serif')

# ─── Title ───

fig.suptitle('Oceanside Housing Pipeline Collapse\nAfter Coastal Commission Density Cap Certification (Feb 5, 2026)',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_AFFORDABLE, label='Affordable units'),
    mpatches.Patch(facecolor=C_MARKET, label='Market rate units'),
    mpatches.Patch(facecolor=C_GHOST, edgecolor='#888888', linestyle='--',
                   label='Vested pre-cap (not repeatable)'),
    mpatches.Patch(facecolor='none', edgecolor=C_AVG, linestyle='--', linewidth=2,
                   label='2020-2024 average'),
]
fig.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(0.97, 0.92),
           fontsize=9, framealpha=0.9)

plt.tight_layout(rect=[0.02, 0.15, 0.98, 0.92])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-pipeline-verified.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('Saved:', out)
