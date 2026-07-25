#!/usr/bin/env bash
# Run champion mode on the same Bybit Demo account.
# Normally entered automatically after day 14; this restarts it.
source "$(dirname "$0")/_common.sh"
echo "Starting BYBIT_DEMO_CHAMPION…"
echo "Dashboard: http://127.0.0.1:8787   ·   Stop safely with Ctrl+C"
echo
run_awake --config "${BTCBOT_CONFIG:-config/champion.yaml}" champion
