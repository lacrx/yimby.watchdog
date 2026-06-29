#!/bin/bash
# Backfill eTRAKiT building permits — remaining years.
# Gentle: 2s between requests, 120s pause between years.
# Safe to Ctrl-C and restart — incremental mode picks up where it left off.

set -euo pipefail
cd "$(dirname "$0")"
source .venv/bin/activate

for YEAR in 2021 2022 2023; do
    echo ""
    echo "=== $YEAR ==="
    echo "Started: $(date)"
    python scrapers/oceanside.py permits --year "$YEAR" --delay 2
    echo "Finished $YEAR: $(date)"

    if [ "$YEAR" != "2023" ]; then
        echo "Pausing 120s before next year..."
        sleep 120
    fi
done

echo ""
echo "=== Backfill complete ==="
echo "Finished: $(date)"

echo ""
echo "=== All years summary ==="
for f in data/permits/etrakit-permits-*.jsonl; do
    YEAR=$(basename "$f" | grep -oP '\d{4}')
    COUNT=$(wc -l < "$f")
    echo "  $YEAR: $COUNT permits"
done
