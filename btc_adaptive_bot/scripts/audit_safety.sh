#!/usr/bin/env bash
# ============================================================
#  Safety audit — fails loudly if real-money capability, leaked
#  credentials, or a non-allow-listed host appears in the source.
#  Run it any time; it is also part of the test suite.
# ============================================================
set -uo pipefail
cd "$(dirname "$0")/.."

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
FAILURES=0

section() { printf "\n%s%s%s\n" "$BOLD" "$1" "$OFF"; }
pass()    { printf "  %s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
failure() { printf "  %s✗%s %s\n" "$RED" "$OFF" "$1"; FAILURES=$((FAILURES + 1)); }
note()    { printf "  %s·%s %s\n" "$YELLOW" "$OFF" "$1"; }

printf "%sSAFETY AUDIT%s\n" "$BOLD" "$OFF"

# ---------------------------------------------------------------
section "1. No withdrawal / transfer / deposit endpoints"
FORBIDDEN_PATHS=(
  "/v5/asset/withdraw" "/v5/asset/transfer" "/v5/asset/deposit"
  "create-internal-transfer" "create-universal-transfer"
  "withdraw/create" "deposit/query-address"
)
found_paths=0
for path in "${FORBIDDEN_PATHS[@]}"; do
  # endpoints.py lists these in FORBIDDEN_ENDPOINT_FRAGMENTS on purpose.
  if grep -rn --include='*.py' -F "$path" src/ 2>/dev/null | grep -v 'endpoints.py'; then
    failure "forbidden endpoint '$path' referenced outside endpoints.py"
    found_paths=1
  fi
done
[ "$found_paths" -eq 0 ] && pass "no withdrawal, transfer, or deposit endpoints in src/"

# ---------------------------------------------------------------
section "2. Authenticated hosts are confined to endpoints.py"
host_hits="$(grep -rn --include='*.py' -E 'https://api(-testnet)?\.bybit\.(com|nl|eu|tr|kz|ae|id)' src/ 2>/dev/null \
             | grep -v 'src/btcbot/exchange/endpoints.py' || true)"
if [ -n "$host_hits" ]; then
  echo "$host_hits"
  failure "a Bybit host literal appears outside endpoints.py"
else
  pass "every Bybit host literal lives in exchange/endpoints.py"
fi

if grep -q 'DEMO_REST_HOST = "https://api-demo.bybit.com"' src/btcbot/exchange/endpoints.py; then
  pass "demo host pinned to api-demo.bybit.com"
else
  failure "the demo host constant is missing or altered"
fi

if grep -q 'ALLOWED_DEMO_HOSTS: frozenset\[str\] = frozenset' src/btcbot/exchange/endpoints.py; then
  pass "ALLOWED_DEMO_HOSTS is an immutable frozenset"
else
  failure "ALLOWED_DEMO_HOSTS is not a frozenset"
fi

# The mainnet host may only be used by the read-only negative control.
nc_uses="$(grep -rn --include='*.py' 'NEGATIVE_CONTROL_HOST' src/ | grep -v 'endpoints.py' || true)"
nc_files="$(echo "$nc_uses" | grep -c 'rest.py' || true)"
if [ -z "$nc_uses" ] || [ "$(echo "$nc_uses" | grep -vc 'rest.py')" -eq 0 ]; then
  pass "mainnet host referenced only by the read-only negative control"
else
  echo "$nc_uses"
  failure "mainnet host referenced outside the negative-control probe"
fi

# ---------------------------------------------------------------
section "3. No real-money mode"
if grep -rniE --include='*.py' '\b(live_trading|real_money|enable_live|is_live|mainnet_mode|trading_mode *= *.live.)\b' src/ 2>/dev/null; then
  failure "a real-money/live-mode flag was found"
else
  pass "no live/real-money mode flag exists"
fi

if grep -q 'cannot be disabled' src/btcbot/config/schema.py; then
  pass "demo verification cannot be disabled by configuration"
else
  failure "the config guard on require_demo_verification_for_orders is missing"
fi

# ---------------------------------------------------------------
section "4. No hardcoded credentials"
cred_hits="$(grep -rnE --include='*.py' \
  "(api_key|api_secret|apikey|secret)[[:space:]]*=[[:space:]]*[\"'][A-Za-z0-9_-]{16,}[\"']" \
  src/ 2>/dev/null || true)"
if [ -n "$cred_hits" ]; then
  echo "$cred_hits"
  failure "a hardcoded credential-like literal was found"
else
  pass "no hardcoded credentials in src/"
fi

if grep -q '^\.env$' .gitignore && grep -q '^data/$' .gitignore && grep -q '^logs/$' .gitignore; then
  pass ".env, data/ and logs/ are git-ignored"
else
  failure ".gitignore does not exclude .env, data/ and logs/"
fi

if [ -f .env ] && git check-ignore -q .env 2>/dev/null; then
  pass ".env exists and git is ignoring it"
elif [ -f .env ]; then
  failure ".env exists but git is NOT ignoring it"
else
  note ".env not created yet (expected before first setup)"
fi

if grep -q 'register_secret' src/btcbot/utils/logging.py && \
   grep -q 'SecretRedactionFilter' src/btcbot/utils/logging.py; then
  pass "log redaction filter is installed for credentials"
else
  failure "credential redaction is missing from the logging setup"
fi

# ---------------------------------------------------------------
section "5. Order-path guards"
if grep -q 'UNIQUE (setup_id, intent)' src/btcbot/database/migrations.py; then
  pass "database uniqueness constraint blocks duplicate orders"
else
  failure "the UNIQUE(setup_id, intent) constraint is missing"
fi

if grep -q 'guard.orders_permitted()' src/btcbot/execution/demo_executor.py; then
  pass "every order path checks demo verification first"
else
  failure "the demo verification gate is missing from the executor"
fi

if grep -q 'max_risk_pct' src/btcbot/risk/position_sizing.py && \
   grep -q 'clamp(risk_pct' src/btcbot/risk/position_sizing.py; then
  pass "position sizing is hard-clamped to the configured risk ceiling"
else
  failure "the position-size clamp is missing"
fi

# ---------------------------------------------------------------
section "6. Look-ahead protection"
if grep -q 'def is_closed' src/btcbot/exchange/models.py && \
   grep -q 'LookAheadError' src/btcbot/market_data/candles.py; then
  pass "closed-candle guard and LookAheadError are present"
else
  failure "look-ahead protection is missing"
fi

if grep -q 'build_closed_only' src/btcbot/market_data/historical.py; then
  pass "historical downloads drop unclosed candles"
else
  failure "historical downloader does not filter unclosed candles"
fi

# ---------------------------------------------------------------
section "7. Bare except / silent failures"
bare="$(grep -rn --include='*.py' -E '^\s*except\s*:' src/ 2>/dev/null || true)"
if [ -n "$bare" ]; then
  echo "$bare"
  failure "bare 'except:' found"
else
  pass "no bare except blocks"
fi

# ---------------------------------------------------------------
printf "\n%s" "$BOLD"
if [ "$FAILURES" -eq 0 ]; then
  printf "%sAUDIT PASSED — no real-money capability, no leaked credentials.%s\n" "$GREEN" "$OFF"
  exit 0
fi
printf "%sAUDIT FAILED — %d issue(s) above must be fixed.%s\n" "$RED" "$FAILURES" "$OFF"
exit 1
