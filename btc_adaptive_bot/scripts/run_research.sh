#!/usr/bin/env bash
# Start (or resume) the 14-day OKX Demo research experiment.
# Restarting this script does NOT restart the 14-day timer.
source "$(dirname "$0")/_common.sh"
echo "Starting OKX_DEMO_RESEARCH…"
echo "Dashboard: http://127.0.0.1:8787   ·   Stop safely with Ctrl+C"
echo
run_awake --config "${BTCBOT_CONFIG:-config/research.yaml}" research
