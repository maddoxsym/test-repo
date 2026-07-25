#!/usr/bin/env bash
# Verify the Bybit Demo connection. Places NO orders.
source "$(dirname "$0")/_common.sh"
echo "Verifying Bybit Demo connection (no orders will be placed)…"
echo
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" verify
