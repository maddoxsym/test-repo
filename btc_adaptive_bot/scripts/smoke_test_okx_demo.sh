#!/usr/bin/env bash
# ============================================================
#  Minimum-size OKX Demo round trip.
#
#  Places ONE minimum-size DEMO order at low leverage and closes
#  it again. It does NOT start the 14-day experiment timer.
#
#  Requires the explicit --confirm-demo flag: this is the only
#  script in the project that submits an order outside a running
#  experiment, so it refuses to run by accident.
# ============================================================
source "$(dirname "$0")/_common.sh"

if [ "${1:-}" != "--confirm-demo" ]; then
  cat >&2 <<'USAGE'
This script places a real (demo) order on your OKX Demo account.

It is safe — the account holds simulated funds only, and the demo
safety lock is verified before anything is submitted — but it is
opt-in by design.

Re-run it with the confirmation flag:

    ./scripts/smoke_test_okx_demo.sh --confirm-demo
USAGE
  exit 2
fi

echo "OKX Demo smoke test — minimum size, low leverage, closed immediately."
echo "The 14-day experiment timer will NOT start."
echo
btcbot --config "${BTCBOT_CONFIG:-config/research.yaml}" smoke-test --confirm-demo
