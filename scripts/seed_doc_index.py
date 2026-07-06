#!/usr/bin/env python3
"""One-shot: generate doc-index.json for all agencies from existing state.json.

Run once after deploying hot/cold extraction so indexes exist immediately
without waiting for the next fetch cycle.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from civic_utils import (
    load_agencies, agency_data_dir, agency_docs_dir,
    load_json, rebuild_doc_index,
)

def main():
    agencies = load_agencies(enabled_only=False)
    for slug in agencies:
        state_path = agency_data_dir(slug) / "state.json"
        docs_dir = agency_docs_dir(slug)
        if not state_path.exists():
            print(f"  {slug:15s}  no state.json, skipping")
            continue
        if not docs_dir.exists():
            print(f"  {slug:15s}  no documents dir, skipping")
            continue

        state = load_json(state_path)
        n = rebuild_doc_index(slug, state, docs_dir)
        print(f"  {slug:15s}  {n} documents indexed")

if __name__ == "__main__":
    main()
