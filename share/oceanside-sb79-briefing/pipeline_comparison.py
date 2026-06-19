#!/usr/bin/env python3
"""Generate housing pipeline comparison chart: 2025 vs 2026 vs projected 2027."""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(16, 7.5), sharey=False)
fig.patch.set_facecolor('#FAFAFA')

# Colors
C_AFFORDABLE = '#1B6B3A'
C_MARKET = '#2D5F8A'
C_GHOST = '#B8B8B8'
C_RED = '#8B0000'

# ─── LEFT PANEL: Total Units ───

x = np.arange(3)
categories = ['Feb-Jun\n2025', 'Feb-Jun\n2026', 'Feb-Jun\n2027\n(projected)']

affordable =  [392,  23,   0]
market =      [ 98,   0,  10]
ghost =       [  0, 207,   0]

bars_aff = ax1.bar(x, affordable, color=C_AFFORDABLE, width=0.5)
bars_mkt = ax1.bar(x, market, bottom=affordable, color=C_MARKET, width=0.5)
bars_ghost = ax1.bar(x, ghost, bottom=[a+m for a,m in zip(affordable, market)],
                     color=C_GHOST, width=0.5, edgecolor='#888888', linestyle='--', linewidth=1.2)

# Labels on bars
ax1.text(0, 392/2, '392\naffordable', ha='center', va='center', fontsize=11,
         fontweight='bold', color='white')
ax1.text(0, 392 + 98/2, '98', ha='center', va='center', fontsize=10,
         fontweight='bold', color='white')

ax1.text(1, 23/2, '23', ha='center', va='center', fontsize=10,
         fontweight='bold', color='white')
ax1.text(1, 23 + 207/2, '207*', ha='center', va='center', fontsize=11,
         fontweight='bold', color='#444444')

ax1.text(2, 10/2, '~10', ha='center', va='center', fontsize=9,
         fontweight='bold', color='white')

ax1.set_ylabel('Housing Units Approved', fontsize=12, fontweight='bold')
ax1.set_title('Total Units Approved', fontsize=14, fontweight='bold', color='#1A1A1A')
ax1.set_ylim(0, 580)
ax1.set_xticks(x)
ax1.set_xticklabels(categories)
ax1.spines['top'].set_visible(False)
ax1.spines['right'].set_visible(False)
ax1.tick_params(labelsize=11)

# Totals above bars
ax1.text(0, 498, '490 units', ha='center', va='bottom', fontsize=13,
         fontweight='bold', color='#1A1A1A')
ax1.text(1, 238, '230 units', ha='center', va='bottom', fontsize=13,
         fontweight='bold', color=C_RED)
ax1.text(2, 18, '~10 units', ha='center', va='bottom', fontsize=13,
         fontweight='bold', color=C_RED)

# Dashed border on 2027 projected bar
for bar in bars_mkt.patches[2:]:
    bar.set_edgecolor('#888888')
    bar.set_linestyle('--')
    bar.set_linewidth(1.5)

# ─── RIGHT PANEL: Affordable Only ───

x2 = np.arange(3)
aff_labels = ['Feb-Jun\n2025', 'Feb-Jun\n2026', 'Feb-Jun\n2027\n(projected)']
aff_values = [392, 23, 0]
bar_colors = [C_AFFORDABLE, C_RED, C_RED]

bars2 = ax2.bar(x2, aff_values, color=bar_colors, width=0.5)
# Dashed outline at baseline so zero bar is visible
ax2.bar(x2[2:], [4], color='none', width=0.5, edgecolor=C_RED, linewidth=2, linestyle='--')

ax2.text(0, 392 + 10, '392', ha='center', va='bottom', fontsize=16,
         fontweight='bold', color=C_AFFORDABLE)
ax2.text(1, 23 + 15, '23', ha='center', va='bottom', fontsize=16,
         fontweight='bold', color=C_RED)
ax2.text(2, 10, '0', ha='center', va='bottom', fontsize=16,
         fontweight='bold', color=C_RED)

ax2.set_ylabel('Affordable Units Approved', fontsize=12, fontweight='bold')
ax2.set_title('Affordable Units Only', fontsize=14, fontweight='bold', color='#1A1A1A')
ax2.set_ylim(0, 500)
ax2.set_xticks(x2)
ax2.set_xticklabels(aff_labels)
ax2.spines['top'].set_visible(False)
ax2.spines['right'].set_visible(False)
ax2.tick_params(labelsize=11)

# Drop arrow from 2025 to 2027
ax2.annotate('', xy=(2, 10), xytext=(0.15, 350),
             arrowprops=dict(arrowstyle='->', color=C_RED, lw=2.5))
ax2.text(1.1, 210, '-100%', fontsize=18, fontweight='bold', color=C_RED,
         ha='center', va='center', rotation=-42)

# ─── Footnotes ───

fig.text(0.04, 0.12,
         '* 801 Mission Ave (230 units, 2026) was filed Nov 2023 before the density cap and CCC certification.\n'
         '   At 153 du/acre with 8 waivers, it could not be approved under current rules. Remove it: 2026 drops to zero.',
         fontsize=9, color='#444444', fontstyle='italic', va='top', family='sans-serif')

fig.text(0.04, 0.065,
         '2027 projection: No new applications filed since Feb 5, 2026. Remaining pipeline is ~10 units of 2-4 unit infill.\n'
         'Pre-cap vested projects exhausted. No 100% affordable projects in pipeline. SB 79 evasion ordinance defers sites to 2032.',
         fontsize=9, color='#444444', fontstyle='italic', va='top', family='sans-serif')

fig.text(0.04, 0.02,
         'CCC certified Coastal Act shield on Feb 5, 2026. No new downtown project has been approved since.',
         fontsize=10, color=C_RED, fontweight='bold', va='top', family='sans-serif')

# ─── Title ───

fig.suptitle('Oceanside Housing Pipeline Collapse\nAfter Coastal Commission Certification (Feb 5, 2026)',
             fontsize=16, fontweight='bold', color='#1A1A1A', y=0.98)

# Legend
legend_elements = [
    mpatches.Patch(facecolor=C_AFFORDABLE, label='Affordable units'),
    mpatches.Patch(facecolor=C_MARKET, label='Market rate units'),
    mpatches.Patch(facecolor=C_GHOST, edgecolor='#888888', linestyle='--',
                   label='Vested pre-cap (not repeatable)'),
]
fig.legend(handles=legend_elements, loc='upper right', bbox_to_anchor=(0.97, 0.92),
           fontsize=9.5, framealpha=0.9)

plt.tight_layout(rect=[0.02, 0.16, 0.98, 0.92])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-pipeline-collapse.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print(f'Saved: {out}')
