#!/bin/bash
# One-time overnight run: extract all structured JSONL + build monthly digests
# Usage: nohup ./run-overnight-extraction.sh >> data/overnight-extraction.log 2>&1 &

set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

echo "[$(date)] Starting structured extraction from raw documents..."
python extract_structured.py
echo "[$(date)] Extraction complete."

echo "[$(date)] Building monthly digests..."
python monthly_rollup.py --force
echo "[$(date)] Monthly rollup complete."

echo "[$(date)] Stats:"
python extract_structured.py --stats
python monthly_rollup.py --stats

echo "[$(date)] All done."
