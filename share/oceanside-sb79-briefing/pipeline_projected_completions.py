#!/usr/bin/env python3
"""Projected D-District housing completions based on building permit pipeline.

Cross-references HCD APR filings (geo-filtered to D-District bounding box)
with eTRAKiT building permit status to project when units will deliver.

Pipeline stages:
  FINALED   = certificate of occupancy issued → completed
  ISSUED    = building permit issued → under construction (18-30 mo to completion)
  RECEIVED  = building permit applied, not yet issued → 6-12 mo to issuance
  APPROVED  = planning entitlement only, no building permit → 1-3 years if they proceed
  PENDING   = still in entitlement → stalled or abandoned
"""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import numpy as np

# ─── D-District projects from cross-referenced HCD APR + eTRAKiT data ───
#
# Each project: (name, units, status, est_completion_year, confidence)
#   confidence: 'confirmed' (BP issued/finaled), 'likely' (BP in review),
#               'uncertain' (approved entitlement, no BP), 'stalled' (pending entitlement)

projects = [
    # COMPLETED
    ('Alta Oceanside (939 N Coast Hwy)',        309, 'completed',   2025, 'confirmed'),
    ('Sunsets Mixed-Use (Horne St, 2019)',        71, 'completed',   2024, 'confirmed'),
    ('Small infill (various)',                    40, 'completed',   2024, 'confirmed'),

    # UNDER CONSTRUCTION — building permit issued
    ('712 Seagaze Mixed Use (179u)',             179, 'construction', 2028, 'confirmed'),

    # IN PERMIT REVIEW — BP application received, not yet issued
    ('Modera Neptune (815 N Coast, 360u)',       360, 'permit_review', 2030, 'likely'),

    # APPROVED ENTITLEMENT — no building permit filed
    ('Horne St 180 apts (DB approved)',          180, 'approved',    2030, 'uncertain'),
    ('Breeze Apts (Nevada St, 146u)',            146, 'approved',    2029, 'uncertain'),

    # STALLED IN ENTITLEMENT — "Pending" in HCD, no building permit
    ('Oceanside Transit Center (547u)',          547, 'stalled',     None, 'stalled'),
    ('901 Mission Ave (298u)',                   298, 'stalled',     None, 'stalled'),
    ('810 Mission Ave (206u)',                   206, 'stalled',     None, 'stalled'),
    ('401 Mission Ave (332u)',                   332, 'stalled',     None, 'stalled'),
    ('Seagaze variants (208+273+230u)',          711, 'stalled',     None, 'stalled'),
    ('901 Pier View (64u)',                       64, 'stalled',     None, 'stalled'),
    ('Sunsets 3.0 dup (180u)',                   180, 'stalled',     None, 'stalled'),
    ('1103 N Coast Hwy (290u)',                  290, 'stalled',     None, 'stalled'),
    ('Other small pending',                       55, 'stalled',     None, 'stalled'),
]

# ─── Print summary ───

print("D-District Pipeline Status")
print("=" * 80)

by_status = {}
for name, units, status, year, conf in projects:
    if status not in by_status:
        by_status[status] = []
    by_status[status].append((name, units, year, conf))

status_labels = {
    'completed': 'COMPLETED (cert of occupancy)',
    'construction': 'UNDER CONSTRUCTION (BP issued)',
    'permit_review': 'IN PERMIT REVIEW (BP received)',
    'approved': 'APPROVED ENTITLEMENT (no BP)',
    'stalled': 'STALLED IN ENTITLEMENT (pending)',
}

for status in ['completed', 'construction', 'permit_review', 'approved', 'stalled']:
    items = by_status.get(status, [])
    total = sum(u for _, u, _, _ in items)
    print(f"\n{status_labels[status]}: {total} units")
    for name, units, year, conf in items:
        yr_str = str(year) if year else 'N/A'
        print(f"  {units:>4}u  est. {yr_str:<6}  {name}")

active = sum(u for _, u, s, _, _ in projects if s != 'stalled')
stalled = sum(u for _, u, s, _, _ in projects if s == 'stalled')
total = active + stalled
print(f"\nTotal filed: {total:,} units")
print(f"Progressing: {active:,} units ({active/total*100:.0f}%)")
print(f"Stalled:     {stalled:,} units ({stalled/total*100:.0f}%)")

# ─── Projected completions chart ───

years = list(range(2024, 2033))
year_labels = [str(y) for y in years]

confirmed = [0] * len(years)     # BP issued or finaled
likely = [0] * len(years)        # BP in review
uncertain = [0] * len(years)     # Approved entitlement, no BP

for name, units, status, year, conf in projects:
    if year is None:
        continue
    if year < 2024 or year > 2032:
        continue
    idx = year - 2024
    if conf == 'confirmed':
        confirmed[idx] += units
    elif conf == 'likely':
        likely[idx] += units
    elif conf == 'uncertain':
        uncertain[idx] += units

# Colors
C_CONFIRMED  = '#2D8A5F'   # dark green
C_LIKELY     = '#5B9BD5'   # blue
C_UNCERTAIN  = '#BDC3C7'   # light gray
C_STALLED    = '#E74C3C'   # red
C_RED        = '#8B0000'
C_ZERO       = '#CC0000'

fig, ax = plt.subplots(1, 1, figsize=(14, 8))
fig.patch.set_facecolor('#FAFAFA')

x = np.arange(len(years))
bar_width = 0.6

# Stacked bars
b1 = ax.bar(x, confirmed, color=C_CONFIRMED, width=bar_width)
b2 = ax.bar(x, likely, bottom=confirmed, color=C_LIKELY, width=bar_width)
bot3 = [c + l for c, l in zip(confirmed, likely)]
b3 = ax.bar(x, uncertain, bottom=bot3, color=C_UNCERTAIN, width=bar_width,
            edgecolor='#95A5A6', linewidth=0.8, linestyle='--')

totals = [c + l + u for c, l, u in zip(confirmed, likely, uncertain)]

for i, t in enumerate(totals):
    if t > 0:
        ax.text(i, t + 10, f'{t:,}', ha='center', va='bottom',
                fontsize=12, fontweight='bold', color='#1A1A1A')
    else:
        ax.text(i, 10, '0', ha='center', va='bottom',
                fontsize=13, fontweight='bold', color=C_ZERO)

# Bracket showing stalled units
stalled_total = sum(u for _, u, s, _, _ in projects if s == 'stalled')
ax.annotate(
    f'{stalled_total:,} units stalled\nin entitlement\n(no building permit)',
    xy=(6.5, 0), xytext=(6.5, max(max(totals), 100) * 0.65),
    fontsize=11, fontweight='bold', color=C_RED,
    ha='center', va='bottom',
    arrowprops=dict(arrowstyle='->', color=C_RED, lw=2),
)

# "Pipeline empty" annotation
ax.annotate(
    'Pipeline\nempty',
    xy=(8, 0), xytext=(8, max(max(totals), 100) * 0.3),
    fontsize=14, fontweight='bold', color=C_RED, ha='center',
    arrowprops=dict(arrowstyle='->', color=C_RED, lw=2.5),
)

# RHNA deadline marker
ax.axvline(x=7.0, color='#4B0082', linestyle=':', linewidth=2.5, alpha=0.7)
ax.text(7.05, max(max(totals), 100) * 0.95, 'RHNA 6th cycle\ndeadline 2031',
        fontsize=9, color='#4B0082', fontstyle='italic', va='top')

# Policy event: cap adopted
ax.axvline(x=-0.5, color=C_RED, linestyle=':', linewidth=2, alpha=0.5)
ax.text(-0.45, max(max(totals), 100) * 0.95, '86 du/acre cap\nadopted Oct 2023',
        fontsize=8, color=C_RED, fontstyle='italic', va='top')

ax.set_ylabel('Projected Units Completing', fontsize=13, fontweight='bold')
ax.set_xticks(x)
ax.set_xticklabels(year_labels, fontsize=11)
ax.set_xlim(-0.8, len(years) - 0.2)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

# ─── Title + Legend ───

fig.suptitle('Oceanside D-District: Projected Housing Completions\n'
             'Based on Building Permit Pipeline Status (eTRAKiT cross-referenced)',
             fontsize=15, fontweight='bold', color='#1A1A1A', y=0.98)

legend_elements = [
    mpatches.Patch(facecolor=C_CONFIRMED, label='Confirmed (BP issued/finaled)'),
    mpatches.Patch(facecolor=C_LIKELY, label='Likely (BP in review)'),
    mpatches.Patch(facecolor=C_UNCERTAIN, edgecolor='#95A5A6', linestyle='--',
                   label='Uncertain (entitlement only, no BP)'),
]
fig.legend(handles=legend_elements, loc='upper center', bbox_to_anchor=(0.5, 0.91),
           fontsize=10, framealpha=0.9, ncol=3)

plt.tight_layout(rect=[0.02, 0.02, 0.98, 0.86])

out = '/home/thomas/repos/civics/share/oceanside-sb79-briefing/oceanside-ddistrict-projected-completions.jpg'
fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
print('\nSaved:', out)
