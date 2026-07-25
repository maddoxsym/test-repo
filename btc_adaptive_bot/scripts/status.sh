#!/usr/bin/env bash
# Print the current experiment status.
source "$(dirname "$0")/_common.sh"
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" status
