#!/usr/bin/env bash
# ============================================================
#  Verify the OKX Europe Demo connection.
#
#  PLACES NO ORDERS. Seventeen read-only checks covering the
#  demo safety lock, the account, the discovered BTC X-Perp,
#  and market data. Exits 0 on PASS, 1 on FAIL.
# ============================================================
source "$(dirname "$0")/_common.sh"
echo "Verifying OKX Europe Demo connection (no orders will be placed)…"
echo
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" verify
