#!/usr/bin/env python3
"""Map: SB 79 walking path exemptions vs total eligible parcel universe."""

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.lines as mlines
import numpy as np
import zipfile, shapefile, os, tempfile, json, urllib.request, urllib.parse
from pyproj import Transformer
from math import sqrt, cos, radians

STATIONS = {
    'Oceanside\nTransit Center': {'coords': (-117.3795, 33.1960), 'trains': 130, 'tier': 1,
                                    'color': '#8B0000', 'label_offset': (0.004, 0.003)},
    'Coast\nHighway':            {'coords': (-117.3773, 33.1880), 'trains': 130, 'tier': 1,
                                    'color': '#8B0000', 'label_offset': (0.004, -0.004)},
    'Civic\nCenter':             {'coords': (-117.3370, 33.2010), 'trains': 68, 'tier': 2,
                                    'color': '#2D5F8A', 'label_offset': (0.004, 0.003)},
    'Crouch\nStreet':            {'coords': (-117.3530, 33.2000), 'trains': 68, 'tier': 2,
                                    'color': '#2D5F8A', 'label_offset': (-0.015, 0.004)},
    'El Camino\nReal':           {'coords': (-117.3180, 33.2090), 'trains': 68, 'tier': 2,
                                    'color': '#2D5F8A', 'label_offset': (0.004, 0.003)},
    'Rancho\nDel Oro':           {'coords': (-117.2880, 33.2170), 'trains': 68, 'tier': 2,
                                    'color': '#2D5F8A', 'label_offset': (0.004, -0.004)},
    'College\nBlvd':             {'coords': (-117.2690, 33.2050), 'trains': 68, 'tier': 2,
                                    'color': '#2D5F8A', 'label_offset': (0.004, 0.003)},
    'Melrose':                   {'coords': (-117.2500, 33.2050), 'trains': 68, 'tier': 2,
                                    'color': '#2D5F8A', 'label_offset': (0.004, -0.004)},
}

SANDAG_URL = 'https://geo.sandag.org/server/rest/services/Hosted/Parcels/FeatureServer/0/query'


def haversine_miles(lon1, lat1, lon2, lat2):
    dlat = (lat2 - lat1) * 69.0
    dlon = (lon2 - lon1) * 69.0 * cos(radians((lat1 + lat2) / 2))
    return sqrt(dlat**2 + dlon**2)


def fetch_sandag_centroids(slon, slat, radius_mi=0.5):
    """Fetch parcel centroids from SANDAG within bbox of station."""
    r_lat = radius_mi / 69.0
    r_lon = radius_mi / (69.0 * cos(radians(slat)))
    xmin, xmax = slon - r_lon, slon + r_lon
    ymin, ymax = slat - r_lat, slat + r_lat

    centroids = []
    offset = 0
    while True:
        params = urllib.parse.urlencode({
            'where': '1=1',
            'outFields': 'asr_landuse',
            'geometry': f'{xmin},{ymin},{xmax},{ymax}',
            'geometryType': 'esriGeometryEnvelope',
            'inSR': '4326',
            'spatialRel': 'esriSpatialRelContains',
            'returnGeometry': 'true',
            'returnCentroid': 'true',
            'outSR': '4326',
            'resultOffset': str(offset),
            'resultRecordCount': '2000',
            'f': 'json',
        })
        req = urllib.request.Request(f'{SANDAG_URL}?{params}')
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read())
        features = data.get('features', [])
        for f in features:
            geom = f.get('geometry')
            if geom and 'x' in geom:
                centroids.append((geom['x'], geom['y'],
                                  f['attributes'].get('asr_landuse', 0)))
            elif geom and 'rings' in geom:
                ring = geom['rings'][0]
                cx = sum(p[0] for p in ring) / len(ring)
                cy = sum(p[1] for p in ring) / len(ring)
                centroids.append((cx, cy, f['attributes'].get('asr_landuse', 0)))
        if len(features) < 2000:
            break
        offset += 2000
    return centroids


def main():
    transformer = Transformer.from_crs('EPSG:2230', 'EPSG:4326', always_xy=True)
    shp_path = os.path.expanduser('~/Downloads/otc/pathoftravel.shp.zip')

    # Load walking path parcels
    with tempfile.TemporaryDirectory() as td:
        with zipfile.ZipFile(shp_path) as z:
            z.extractall(td)
        sf = shapefile.Reader(os.path.join(td, 'pathoftravel.shp'))
        shapes = sf.shapes()

        wp_parcels = []
        for s in shapes:
            pts_ll = [transformer.transform(x, y) for x, y in s.points]
            cx = sum(p[0] for p in pts_ll) / len(pts_ll)
            cy = sum(p[1] for p in pts_ll) / len(pts_ll)
            wp_parcels.append({'points': pts_ll, 'centroid': (cx, cy)})

    # Fetch SANDAG parcels around all stations
    print("Fetching SANDAG parcel data around each station...")
    all_sandag = []
    seen_coords = set()
    for sname, sdata in STATIONS.items():
        slon, slat = sdata['coords']
        centroids = fetch_sandag_centroids(slon, slat, 0.5)
        for cx, cy, lu in centroids:
            key = (round(cx, 6), round(cy, 6))
            if key not in seen_coords:
                seen_coords.add(key)
                all_sandag.append((cx, cy, lu))
        print(f"  {sname.replace(chr(10), ' ')}: {len(centroids)} parcels")
    print(f"  Total unique SANDAG parcels: {len(all_sandag)}")

    # ─── Build figure ───
    fig, ax = plt.subplots(figsize=(22, 14))
    fig.patch.set_facecolor('#F5F5F0')
    ax.set_facecolor('#E8E4D8')

    # Draw 0.5-mile radius circles
    for sname, sdata in STATIONS.items():
        slon, slat = sdata['coords']
        r_lat = 0.5 / 69.0
        r_lon = 0.5 / (69.0 * cos(radians(slat)))
        ellipse = matplotlib.patches.Ellipse(
            (slon, slat), width=2*r_lon, height=2*r_lat,
            facecolor='#F0EDE4', edgecolor='#999999', linewidth=1.0,
            linestyle='--', alpha=0.4, zorder=1
        )
        ax.add_patch(ellipse)

    # Plot SANDAG parcels as background dots (the full universe)
    sandag_lons = [c[0] for c in all_sandag]
    sandag_lats = [c[1] for c in all_sandag]
    ax.scatter(sandag_lons, sandag_lats, s=1.2, c='#CCCCBB', alpha=0.5,
               zorder=2, rasterized=True)

    # Plot walking path parcels as filled polygons (the 3.8%)
    for p in wp_parcels:
        polygon = plt.Polygon(
            p['points'], closed=True,
            facecolor='#D32F2F', edgecolor='#B71C1C',
            alpha=0.7, linewidth=0.3, zorder=4
        )
        ax.add_patch(polygon)

    # Station markers
    for sname, sdata in STATIONS.items():
        slon, slat = sdata['coords']
        ax.plot(slon, slat, marker='*', markersize=20, color=sdata['color'],
                markeredgecolor='white', markeredgewidth=1.5, zorder=6)
        ox, oy = sdata['label_offset']
        ax.annotate(sname, (slon, slat), xytext=(slon + ox, slat + oy),
                    fontsize=8.5, fontweight='bold', color=sdata['color'],
                    ha='left', va='center', zorder=7,
                    bbox=dict(boxstyle='round,pad=0.2', facecolor='white',
                              edgecolor=sdata['color'], alpha=0.9, linewidth=0.8))

    # ─── Formatting ───
    all_lons = sandag_lons + [p['centroid'][0] for p in wp_parcels]
    all_lats = sandag_lats + [p['centroid'][1] for p in wp_parcels]
    pad = 0.012
    ax.set_xlim(min(all_lons) - pad - 0.005, max(all_lons) + pad + 0.005)
    ax.set_ylim(min(all_lats) - pad, max(all_lats) + pad)
    ax.set_xlabel('Longitude', fontsize=10)
    ax.set_ylabel('Latitude', fontsize=10)
    ax.tick_params(labelsize=8)
    ax.set_aspect(1 / cos(radians(33.2)))

    # Title
    fig.suptitle(
        'Oceanside SB 79: Walking Path Exemptions vs. Total Eligible Parcels\n'
        '710 parcels in walking path shapefile (red) out of ~18,800 total (grey) — 3.8% coverage',
        fontsize=15, fontweight='bold', color='#1A1A1A', y=0.97
    )

    # Stats box
    stats = (
        "SANDAG parcel count (0.5 mi):\n"
        "  OTC:           4,572  (2 in WP = 0.04%)\n"
        "  Coast Hwy:     5,297  (53 in WP = 1.0%)\n"
        "  Crouch St:     1,334  (511 in WP = 38%)\n"
        "  Civic Center:    974  (89 in WP = 9%)\n"
        "  El Camino Real:  562  (40 in WP = 7%)\n"
        "  Rancho Del Oro:2,220  (3 in WP = 0.1%)\n"
        "  College Blvd:  1,813  (11 in WP = 0.6%)\n"
        "  Melrose:       2,031  (2 in WP = 0.1%)\n"
        "  ─────────────────────────────\n"
        f"  Total unique: ~{len(all_sandag):,}  (710 in WP = 3.8%)\n\n"
        "  WP = Walking Path shapefile\n"
        "  Grey dots = all SANDAG parcels\n"
        "  Red polygons = WP-exempted parcels"
    )
    ax.text(0.99, 0.99, stats, transform=ax.transAxes,
            fontsize=8, fontfamily='monospace', verticalalignment='top',
            horizontalalignment='right', zorder=8,
            bbox=dict(boxstyle='round,pad=0.5', facecolor='white',
                      edgecolor='#444444', alpha=0.95))

    # Legend
    legend_elements = [
        mlines.Line2D([0], [0], marker='*', color='w', markerfacecolor='#8B0000',
                       markersize=14, label='Tier 1 station (72+ trains)'),
        mlines.Line2D([0], [0], marker='*', color='w', markerfacecolor='#2D5F8A',
                       markersize=14, label='Tier 2 station (48-71 trains)'),
        mpatches.Patch(facecolor='#D32F2F', alpha=0.7, edgecolor='#B71C1C',
                       label='Walking path exempted (710 parcels, 3.8%)'),
        mlines.Line2D([0], [0], marker='o', color='w', markerfacecolor='#CCCCBB',
                       markersize=6, alpha=0.6,
                       label=f'All SANDAG parcels (~{len(all_sandag):,} unique)'),
        mlines.Line2D([0], [0], linestyle='--', color='#999999',
                       label='0.5-mile SB 79 zone'),
    ]
    ax.legend(handles=legend_elements, loc='lower left', fontsize=9, framealpha=0.95)

    # Footnotes
    fig.text(0.04, 0.025,
             'The walking path exemption covers 3.8% of SB 79 eligible parcels. '
             'The city claims deferments, rent control reclassification, and habitat '
             'exclusions cover the rest — no parcel-level data has been produced.\n'
             'Source: PRA shapefiles (pathoftravel.shp) + SANDAG Hosted Parcels FeatureServer. '
             'OTC/Coast Highway zones overlap; unique total is lower than sum of station counts.',
             fontsize=8.5, color='#444444', fontstyle='italic', va='top')

    plt.tight_layout(rect=[0.02, 0.06, 0.98, 0.94])

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       'oceanside-sb79-exemption-map.jpg')
    fig.savefig(out, dpi=200, bbox_inches='tight', facecolor=fig.get_facecolor())
    print(f'Saved: {out}')


if __name__ == '__main__':
    main()
