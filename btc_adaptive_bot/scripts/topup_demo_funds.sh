#!/usr/bin/env bash
# Request Bybit demo funds (demo-only endpoint; rate limit 1/minute).
#   ./scripts/topup_demo_funds.sh [COIN] [AMOUNT]
source "$(dirname "$0")/_common.sh"
COIN="${1:-USDT}"
AMOUNT="${2:-10000}"
echo "Requesting $AMOUNT $COIN of DEMO funds…"
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" topup --coin "$COIN" --amount "$AMOUNT"
