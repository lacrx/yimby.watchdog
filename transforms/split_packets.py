#!/usr/bin/env python3
"""Split large agenda packets into per-item documents for more efficient extraction.

Detects item boundaries using agency-specific patterns and writes one text file
per agenda item. The original packet gets a .split marker so extraction skips it
and processes the per-item files instead.

Usage:
    python split_packets.py                # split all eligible packets
    python split_packets.py --dry-run      # show what would be split
    python split_packets.py --stats        # show split coverage
    python split_packets.py --undo         # remove split files and markers
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
from civic_utils import DATA_DIR

MIN_PACKET_SIZE = 100_000
SPLIT_MARKER = ".split"


def detect_agency(doc_path):
    """Determine agency from file path."""
    parts = doc_path.parts
    for agency in ["sandag", "nctd", "oceanside", "coastal", "sdcounty"]:
        if agency in parts:
            return agency
    return None


def split_sandag_packet(text):
    """Split SANDAG agenda packet on 'Item: N' page markers.

    Returns list of (item_id, item_text) tuples. First entry may be
    ("preamble", ...) for content before the first item.
    """
    pages = text.split("\f")
    if len(pages) < 3:
        return []

    item_pages = []
    for i, page in enumerate(pages):
        m = re.search(r"Item:\s+(\S+)", page)
        if m:
            item_pages.append((i, m.group(1)))

    if len(item_pages) < 2:
        return []

    items = []

    if item_pages[0][0] > 0:
        preamble = "\f".join(pages[: item_pages[0][0]])
        if len(preamble.strip()) > 200:
            items.append(("preamble", preamble))

    for j, (start, item_id) in enumerate(item_pages):
        end = item_pages[j + 1][0] if j + 1 < len(item_pages) else len(pages)
        item_text = "\f".join(pages[start:end])
        items.append((item_id, item_text))

    return items


SPLITTERS = {
    "sandag": split_sandag_packet,
}


def has_split_marker(doc_path):
    return doc_path.with_suffix(doc_path.suffix + SPLIT_MARKER).exists()


def write_split_marker(doc_path, item_count, item_files):
    marker_path = doc_path.with_suffix(doc_path.suffix + SPLIT_MARKER)
    marker_path.write_text(
        json.dumps(
            {
                "split_at": datetime.now().isoformat(),
                "items": item_count,
                "files": [f.name for f in item_files],
                "original_size": len(doc_path.read_text()),
            },
            indent=2,
        )
    )


def remove_split_marker(doc_path):
    marker_path = doc_path.with_suffix(doc_path.suffix + SPLIT_MARKER)
    if marker_path.exists():
        marker_path.unlink()


def get_split_files(doc_path):
    """Find per-item files created from a split packet."""
    marker_path = doc_path.with_suffix(doc_path.suffix + SPLIT_MARKER)
    if not marker_path.exists():
        return []
    try:
        meta = json.loads(marker_path.read_text())
        return [doc_path.parent / f for f in meta.get("files", [])]
    except (json.JSONDecodeError, KeyError):
        return []


def cmd_split(args):
    """Split large agenda packets into per-item files."""
    split_count = 0
    skip_count = 0
    total_saved = 0

    for agency, splitter in SPLITTERS.items():
        docs_dir = DATA_DIR / agency / "documents"
        if not docs_dir.exists():
            continue

        for doc_path in sorted(docs_dir.glob("*.txt")):
            if doc_path.stat().st_size < MIN_PACKET_SIZE:
                continue
            if has_split_marker(doc_path):
                skip_count += 1
                continue

            text = doc_path.read_text()
            items = splitter(text)

            if len(items) < 2:
                continue

            if args.dry_run:
                preamble_size = 0
                item_sizes = []
                for item_id, item_text in items:
                    if item_id == "preamble":
                        preamble_size = len(item_text)
                    else:
                        item_sizes.append((item_id, len(item_text)))
                print(f"{doc_path.name}: {len(text) / 1e3:.0f}K → {len(items)} items")
                if preamble_size:
                    print(f"  preamble: {preamble_size / 1e3:.0f}K (dropped)")
                for item_id, size in item_sizes:
                    print(f"  item {item_id}: {size / 1e3:.0f}K")
                split_count += 1
                continue

            stem = doc_path.stem
            item_files = []
            id_counts = {}

            for item_id, item_text in items:
                if item_id == "preamble":
                    total_saved += len(item_text)
                    continue

                safe_id = item_id.replace(".", "-")
                id_counts[safe_id] = id_counts.get(safe_id, 0) + 1
                if id_counts[safe_id] > 1:
                    safe_id = f"{safe_id}-{chr(96 + id_counts[safe_id])}"

                item_path = docs_dir / f"{stem}-item-{safe_id}.txt"
                item_path.write_text(item_text)
                item_files.append(item_path)

            write_split_marker(doc_path, len(items), item_files)
            split_count += 1
            total_saved += sum(
                len(t)
                for iid, t in items
                if iid == "preamble"
            )
            print(
                f"  {doc_path.name}: split into {len(item_files)} items"
            )

    if args.dry_run:
        print(f"\nWould split: {split_count} packets ({skip_count} already split)")
    else:
        print(
            f"\nSplit {split_count} packets, "
            f"dropped {total_saved / 1e3:.0f}K preamble text "
            f"({skip_count} already split)"
        )


def cmd_undo(args):
    """Remove all split files and markers."""
    removed = 0
    for agency in SPLITTERS:
        docs_dir = DATA_DIR / agency / "documents"
        if not docs_dir.exists():
            continue

        for marker in docs_dir.glob("*.split"):
            doc_path = Path(str(marker).replace(SPLIT_MARKER, ""))
            for item_file in get_split_files(doc_path):
                if item_file.exists():
                    item_file.unlink()
                    removed += 1
            marker.unlink()

    print(f"Removed {removed} split files and markers")


def cmd_stats(args):
    """Show split coverage statistics."""
    for agency in SPLITTERS:
        docs_dir = DATA_DIR / agency / "documents"
        if not docs_dir.exists():
            continue

        large = [f for f in docs_dir.glob("*.txt") if f.stat().st_size >= MIN_PACKET_SIZE]
        # Exclude item files from count
        large = [f for f in large if "-item-" not in f.name]
        split = [f for f in large if has_split_marker(f)]

        total_size = sum(f.stat().st_size for f in large)
        split_size = sum(f.stat().st_size for f in split)

        item_files = []
        for f in split:
            item_files.extend(get_split_files(f))
        item_size = sum(f.stat().st_size for f in item_files if f.exists())

        print(f"{agency}:")
        print(f"  Large packets (>100K): {len(large)}")
        print(f"  Already split: {len(split)}")
        print(f"  Unsplit: {len(large) - len(split)}")
        print(f"  Original size: {total_size / 1e6:.1f} MB")
        if split:
            print(f"  Split items: {len(item_files)} files, {item_size / 1e6:.1f} MB")
            saved = split_size - item_size
            print(f"  Preamble dropped: {saved / 1e3:.0f}K")


def main():
    parser = argparse.ArgumentParser(
        description="Split large agenda packets into per-item documents"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would be split")
    parser.add_argument("--stats", action="store_true", help="Show split coverage")
    parser.add_argument("--undo", action="store_true", help="Remove split files and markers")

    args = parser.parse_args()

    if args.undo:
        cmd_undo(args)
    elif args.stats:
        cmd_stats(args)
    else:
        cmd_split(args)


if __name__ == "__main__":
    main()
