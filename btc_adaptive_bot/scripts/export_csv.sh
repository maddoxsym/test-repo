#!/usr/bin/env bash
# Export every research entity to CSV for independent analysis.
source "$(dirname "$0")/_common.sh"
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" export
echo
echo "CSV files are in: reports/exports/"
