#!/usr/bin/env bash
# Debug run: real public market data, NO authenticated orders.
# The 14-day experiment timer does NOT start.
source "$(dirname "$0")/_common.sh"
echo "Dry run — no authenticated orders, no experiment timer."
echo
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" --no-dashboard dry-run
