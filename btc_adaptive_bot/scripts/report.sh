#!/usr/bin/env bash
# Regenerate the strategy, execution and learning reports.
source "$(dirname "$0")/_common.sh"
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" report
echo
echo "Reports are in: reports/"
