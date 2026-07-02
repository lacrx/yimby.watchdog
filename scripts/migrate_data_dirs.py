#!/usr/bin/env python3
"""
One-time migration: restructure data/ into uniform per-agency directories.

Before:
    data/documents/     (mixed: Oceanside + SANDAG + sdcounty + coastal)
    data/meetings/      (Oceanside only, implicit)
    data/state.json     (Oceanside only)
    data/permits/       (Oceanside only)
    data/summaries/     (Oceanside only)
    data/nctd/          (already namespaced)
    data/sandag/        (meetings only, docs in shared dir)
    data/sdcounty/      (meetings only, docs in shared dir)
    data/coastal/       (meetings only, docs in shared dir)

After:
    data/oceanside/documents/, meetings/, state.json, permits/, summaries/
    data/sandag/documents/   (SANDAG docs moved from shared dir)
    data/sdcounty/documents/ (sdcounty docs moved from shared dir)
    data/coastal/documents/  (coastal docs moved from shared dir)
    data/nctd/               (unchanged)

Usage:
    python scripts/migrate_data_dirs.py --dry-run   # preview moves
    python scripts/migrate_data_dirs.py              # execute migration
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SHARED_DOCS = DATA_DIR / "documents"

SANDAG_HEX = re.compile(r"^[0-9a-f]{12}-")


def classify_doc(filename):
    """Classify a document file by agency based on filename prefix."""
    if filename.startswith("sdcounty-"):
        return "sdcounty"
    if filename.startswith("ccc-"):
        return "coastal"
    if SANDAG_HEX.match(filename):
        return "sandag"
    return "oceanside"


def plan_moves(dry_run=False):
    moves = []
    created_dirs = set()

    def ensure_dir(d):
        if d not in created_dirs:
            if not d.exists():
                if dry_run:
                    print(f"  mkdir {d.relative_to(REPO_ROOT)}")
                else:
                    d.mkdir(parents=True, exist_ok=True)
                created_dirs.add(d)

    # 1. Move Oceanside meetings/ → oceanside/meetings/
    src = DATA_DIR / "meetings"
    dst = DATA_DIR / "oceanside" / "meetings"
    if src.exists() and not src.is_symlink():
        ensure_dir(dst.parent)
        moves.append((src, dst, "Oceanside meetings"))

    # 2. Move Oceanside state.json → oceanside/state.json
    src = DATA_DIR / "state.json"
    dst = DATA_DIR / "oceanside" / "state.json"
    if src.exists() and not src.is_symlink():
        ensure_dir(dst.parent)
        moves.append((src, dst, "Oceanside state"))

    # 3. Move Oceanside summaries/ → oceanside/summaries/
    src = DATA_DIR / "summaries"
    dst = DATA_DIR / "oceanside" / "summaries"
    if src.exists() and not src.is_symlink():
        ensure_dir(dst.parent)
        moves.append((src, dst, "Oceanside summaries"))

    # 4. Move Oceanside permits/ → oceanside/permits/
    src = DATA_DIR / "permits"
    dst = DATA_DIR / "oceanside" / "permits"
    if src.exists() and not src.is_symlink():
        ensure_dir(dst.parent)
        moves.append((src, dst, "Oceanside permits"))

    # 5. Move watch.log → oceanside/watch.log
    src = DATA_DIR / "watch.log"
    dst = DATA_DIR / "oceanside" / "watch.log"
    if src.exists() and not src.is_symlink():
        ensure_dir(dst.parent)
        moves.append((src, dst, "Oceanside watch log"))

    # 6. Split shared documents/ by prefix
    if SHARED_DOCS.exists() and not SHARED_DOCS.is_symlink():
        counts = {"oceanside": 0, "sandag": 0, "sdcounty": 0, "coastal": 0}
        for f in sorted(SHARED_DOCS.iterdir()):
            if not f.is_file():
                continue
            agency = classify_doc(f.name)
            dest_dir = DATA_DIR / agency / "documents"
            ensure_dir(dest_dir)
            dest = dest_dir / f.name
            moves.append((f, dest, None))
            counts[agency] += 1

        print(f"\n  Document split: {counts}")

    return moves


def create_symlinks(dry_run=False):
    """Create backward-compat symlinks at old locations."""
    links = [
        (DATA_DIR / "meetings", DATA_DIR / "oceanside" / "meetings"),
        (DATA_DIR / "documents", DATA_DIR / "oceanside" / "documents"),
        (DATA_DIR / "summaries", DATA_DIR / "oceanside" / "summaries"),
        (DATA_DIR / "permits", DATA_DIR / "oceanside" / "permits"),
    ]
    for link_path, target in links:
        if link_path.exists() or link_path.is_symlink():
            continue
        rel_target = target.relative_to(link_path.parent)
        if dry_run:
            print(f"  symlink {link_path.relative_to(REPO_ROOT)} -> {rel_target}")
        else:
            link_path.symlink_to(rel_target)
            print(f"  Created symlink: {link_path.name} -> {rel_target}")


def main():
    parser = argparse.ArgumentParser(description="Migrate data/ to per-agency directories")
    parser.add_argument("--dry-run", action="store_true", help="Preview moves without executing")
    args = parser.parse_args()

    print("Planning migration...")
    moves = plan_moves(dry_run=args.dry_run)

    named_moves = [(s, d, label) for s, d, label in moves if label]
    file_moves = [(s, d) for s, d, label in moves if not label]

    print(f"\n  {len(named_moves)} directory/file moves")
    print(f"  {len(file_moves)} document file moves")

    if args.dry_run:
        print("\nNamed moves:")
        for src, dst, label in named_moves:
            print(f"  {label}: {src.relative_to(REPO_ROOT)} -> {dst.relative_to(REPO_ROOT)}")
        print("\nSymlinks to create:")
        create_symlinks(dry_run=True)
        print("\n[DRY RUN] No changes made.")
        return

    confirm = input("\nProceed with migration? [y/N] ")
    if confirm.lower() != "y":
        print("Aborted.")
        sys.exit(1)

    print("\nExecuting moves...")
    for src, dst, label in named_moves:
        if label:
            print(f"  {label}: {src.name} -> {dst.relative_to(REPO_ROOT)}")
        shutil.move(str(src), str(dst))

    print(f"\nMoving {len(file_moves)} document files...")
    for i, (src, dst) in enumerate(file_moves):
        shutil.move(str(src), str(dst))
        if (i + 1) % 500 == 0:
            print(f"    {i + 1}/{len(file_moves)} files moved")

    # Remove now-empty documents/ dir
    if SHARED_DOCS.exists() and not any(SHARED_DOCS.iterdir()):
        SHARED_DOCS.rmdir()
        print("  Removed empty documents/")

    print("\nCreating backward-compat symlinks...")
    create_symlinks()

    # Verify
    print("\nVerification:")
    for agency in ["oceanside", "nctd", "sandag", "sdcounty", "coastal"]:
        docs = DATA_DIR / agency / "documents"
        meetings = DATA_DIR / agency / "meetings"
        doc_count = len(list(docs.glob("*"))) if docs.exists() else 0
        meeting_count = len(list(meetings.iterdir())) if meetings.exists() else 0
        print(f"  {agency}: {doc_count} docs, {meeting_count} meetings")

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
