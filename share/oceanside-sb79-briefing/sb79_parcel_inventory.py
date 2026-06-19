#!/usr/bin/env python3
"""
Fetch and analyze SB 79 eligible parcels around Oceanside transit stations.

Pulls live data from SANDAG Hosted Parcels FeatureServer (public, no auth).
Cross-references against PRA walking path shapefile to show coverage gap.

Output: station-by-station parcel counts, land use breakdown, walking path
coverage percentage. Saves CSV summary to out/ directory.

Usage:
    python sb79_parcel_inventory.py
    python sb79_parcel_inventory.py --csv   # also write per-station CSVs
"""

import json, urllib.request, urllib.parse, os, sys, csv
from math import cos, radians, sqrt
from collections import Counter
from datetime import datetime

SANDAG_URL = 'https://geo.sandag.org/server/rest/services/Hosted/Parcels/FeatureServer/0/query'

STATIONS = {
    'OTC':            {'coords': (-117.3795, 33.1960), 'trains': 130, 'tier': 1},
    'Coast Highway':  {'coords': (-117.3773, 33.1880), 'trains': 130, 'tier': 1},
    'Crouch Street':  {'coords': (-117.3530, 33.2000), 'trains': 68,  'tier': 2},
    'Civic Center':   {'coords': (-117.3370, 33.2010), 'trains': 68,  'tier': 2},
    'El Camino Real': {'coords': (-117.3180, 33.2090), 'trains': 68,  'tier': 2},
    'Rancho Del Oro': {'coords': (-117.2880, 33.2170), 'trains': 68,  'tier': 2},
    'College Blvd':   {'coords': (-117.2690, 33.2050), 'trains': 68,  'tier': 2},
    'Melrose':        {'coords': (-117.2500, 33.2050), 'trains': 68,  'tier': 2},
}

# Walking path shapefile parcel counts (from PRA spatial analysis, June 2026)
WP_COUNTS = {
    'OTC': 2, 'Coast Highway': 53, 'Crouch Street': 511, 'Civic Center': 89,
    'El Camino Real': 40, 'Rancho Del Oro': 3, 'College Blvd': 11, 'Melrose': 2,
}

# SANDAG assessor land use codes
LU_LABELS = {
    6: 'Residential (other)', 7: 'Commercial/mixed', 9: 'Vacant/open',
    10: 'Vacant residential', 11: 'SFR detached', 12: 'SFR attached',
    13: 'Duplex', 14: '3-4 units', 15: '5+ apartments',
    16: 'Hotel/motel res', 17: 'Condo', 18: 'Townhouse',
    19: 'Mobile home', 20: 'Commercial general', 21: 'Multi-residential',
    22: 'Store', 24: 'Food service', 25: 'Service station',
    26: 'Auto sales/service', 27: 'Shopping center', 28: 'Office',
    30: 'Industrial light', 31: 'Industrial heavy', 32: 'Warehouse',
    33: 'Lumber yard', 34: 'Parking (commercial)', 35: 'Theater/entertainment',
    38: 'Marina', 39: 'Other commercial', 40: 'Institutional',
    41: 'Church', 43: 'School', 45: 'Hospital', 46: 'Government',
    47: 'Park/recreation', 49: 'Other institutional',
    70: 'Agricultural', 71: 'Institutional (assessor)', 72: 'Cemetery',
    75: 'Camp Pendleton', 76: 'Open space', 77: 'Water/utility',
    79: 'Other exempt', 80: 'Vacant', 81: 'Vacant commercial',
    82: 'Vacant industrial', 86: 'Common area (HOA)', 88: 'Railroad ROW',
}

SFR_CODES = {11}
RESIDENTIAL_NON_SFR = {6, 7, 12, 13, 14, 15, 16, 17, 18, 19, 21}
COMMERCIAL = {20, 22, 24, 25, 26, 27, 28, 30, 31, 32, 33, 34, 35, 38, 39}
INSTITUTIONAL = {40, 41, 43, 45, 46, 47, 49, 70, 71, 72, 75, 76, 77, 79, 88}
VACANT = {9, 10, 80, 81, 82}


def fetch_parcels(slon, slat, radius_mi=0.5):
    r_lat = radius_mi / 69.0
    r_lon = radius_mi / (69.0 * cos(radians(slat)))
    xmin, xmax = slon - r_lon, slon + r_lon
    ymin, ymax = slat - r_lat, slat + r_lat

    all_features = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': 'asr_landuse,total_lvg_area,usable_sq_feet,apn,situs_address,situs_street',
            'geometry': f'{xmin},{ymin},{xmax},{ymax}',
            'geometryType': 'esriGeometryEnvelope',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelContains',
            'returnGeometry': 'false',
            'resultOffset': str(offset),
            'resultRecordCount': '2000',
            'f': 'json',
        })
        req = urllib.request.Request(f'{SANDAG_URL}?{params}')
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        features = data.get('features', [])
        all_features.extend(features)
        if len(features) < 2000:
            break
        offset += 2000
    return all_features


def classify_lu(code):
    if code in SFR_CODES:
        return 'SFR'
    elif code in RESIDENTIAL_NON_SFR:
        return 'Residential (non-SFR)'
    elif code in COMMERCIAL:
        return 'Commercial'
    elif code in VACANT:
        return 'Vacant'
    elif code in INSTITUTIONAL:
        return 'Institutional'
    else:
        return 'Other'


def main():
    write_csv = '--csv' in sys.argv
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')

    ts = datetime.now().strftime('%Y%m%d_%H%M')
    results = []

    print("Oceanside SB 79 Parcel Inventory")
    print("=" * 70)
    print(f"Fetching from SANDAG FeatureServer at {datetime.now():%Y-%m-%d %H:%M}\n")

    for sname, sdata in STATIONS.items():
        slon, slat = sdata['coords']
        features = fetch_parcels(slon, slat)
        total = len(features)
        wp = WP_COUNTS.get(sname, 0)
        wp_pct = (wp / total * 100) if total else 0

        lu_counts = Counter()
        category_counts = Counter()
        for f in features:
            code = f['attributes'].get('asr_landuse')
            if code is None:
                code = -1
            lu_counts[code] += 1
            category_counts[classify_lu(code)] += 1

        sfr = category_counts.get('SFR', 0)
        res_non_sfr = category_counts.get('Residential (non-SFR)', 0)
        commercial = category_counts.get('Commercial', 0)
        vacant = category_counts.get('Vacant', 0)
        institutional = category_counts.get('Institutional', 0)
        other = category_counts.get('Other', 0)

        print(f"{'─' * 70}")
        print(f"  {sname}  |  Tier {sdata['tier']}  |  {sdata['trains']} trains/day")
        print(f"  Total parcels (0.5 mi): {total:,}")
        print(f"  Walking path shapefile: {wp} ({wp_pct:.1f}%)")
        print(f"  Unaccounted by WP:      {total - wp:,} ({100 - wp_pct:.1f}%)")
        print(f"")
        print(f"  Land use breakdown:")
        print(f"    SFR detached:          {sfr:>5,}  ({sfr/total*100:.0f}%)")
        print(f"    Residential (non-SFR): {res_non_sfr:>5,}  ({res_non_sfr/total*100:.0f}%)")
        print(f"    Commercial:            {commercial:>5,}  ({commercial/total*100:.0f}%)")
        print(f"    Vacant:                {vacant:>5,}  ({vacant/total*100:.0f}%)")
        print(f"    Institutional:         {institutional:>5,}  ({institutional/total*100:.0f}%)")
        print(f"    Other/unknown:         {other:>5,}  ({other/total*100:.0f}%)")
        print(f"")
        print(f"  Non-SFR (redevelopment candidates): {total - sfr:,}")
        print()

        results.append({
            'station': sname,
            'tier': sdata['tier'],
            'trains': sdata['trains'],
            'total_parcels': total,
            'wp_parcels': wp,
            'wp_pct': round(wp_pct, 2),
            'sfr': sfr,
            'res_non_sfr': res_non_sfr,
            'commercial': commercial,
            'vacant': vacant,
            'institutional': institutional,
            'other': other,
            'non_sfr_total': total - sfr,
        })

    # Summary
    tot = sum(r['total_parcels'] for r in results)
    tot_wp = sum(r['wp_parcels'] for r in results)
    tot_sfr = sum(r['sfr'] for r in results)
    tot_non_sfr = sum(r['non_sfr_total'] for r in results)

    print("=" * 70)
    print(f"TOTALS (with station overlap — unique count is lower)")
    print(f"  All parcels:    {tot:>7,}")
    print(f"  Walking path:   {tot_wp:>7,}  ({tot_wp/tot*100:.1f}%)")
    print(f"  SFR:            {tot_sfr:>7,}  ({tot_sfr/tot*100:.1f}%)")
    print(f"  Non-SFR:        {tot_non_sfr:>7,}  ({tot_non_sfr/tot*100:.1f}%)")
    print(f"  Gap (no WP):    {tot - tot_wp:>7,}  ({(tot-tot_wp)/tot*100:.1f}%)")
    print()
    print("The walking path exemption covers 3.8% of the SB 79 parcel universe.")
    print("The city claims deferment/Good Cause/habitat cover the rest.")
    print("No parcel-level data for those tools has been produced.")
    print("Next PRA target: deferment parcel list by station and category.")

    # Write summary CSV
    if write_csv or True:
        os.makedirs(out_dir, exist_ok=True)
        csv_path = os.path.join(out_dir, f'sb79_parcel_inventory_{ts}.csv')
        with open(csv_path, 'w', newline='') as f:
            w = csv.DictWriter(f, fieldnames=[
                'station', 'tier', 'trains', 'total_parcels', 'wp_parcels',
                'wp_pct', 'sfr', 'res_non_sfr', 'commercial', 'vacant',
                'institutional', 'other', 'non_sfr_total',
            ])
            w.writeheader()
            w.writerows(results)
        print(f"\nSaved: {csv_path}")


if __name__ == '__main__':
    main()
