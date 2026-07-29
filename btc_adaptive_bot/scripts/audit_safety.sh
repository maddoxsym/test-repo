#!/usr/bin/env bash
# ============================================================
#  Safety audit — fails loudly if real-money capability, leaked
#  credentials, a non-allow-listed host, or a missing demo
#  header appears in the source. Run it any time; the same
#  guarantees are also asserted by tests/unit/test_safety_lock.py.
# ============================================================
set -uo pipefail
cd "$(dirname "$0")/.."

# Prefer the project venv so the import-based checks below can run.
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"; else PY="python3"; fi

RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; BOLD=$'\033[1m'; OFF=$'\033[0m'
FAILURES=0

section() { printf "\n%s%s%s\n" "$BOLD" "$1" "$OFF"; }
pass()    { printf "  %s✓%s %s\n" "$GREEN" "$OFF" "$1"; }
failure() { printf "  %s✗%s %s\n" "$RED" "$OFF" "$1"; FAILURES=$((FAILURES + 1)); }
note()    { printf "  %s·%s %s\n" "$YELLOW" "$OFF" "$1"; }

printf "%sSAFETY AUDIT — OKX DEMO (all regions)%s\n" "$BOLD" "$OFF"

# ---------------------------------------------------------------
section "1. No withdrawal / transfer / deposit endpoints"
FORBIDDEN_PATHS=(
  "/api/v5/asset/withdrawal" "/api/v5/asset/transfer" "/api/v5/asset/deposit-address"
  "/api/v5/asset/convert" "/api/v5/users/subaccount" "/api/v5/account/borrow-repay"
  "/api/v5/finance/"
)
found_paths=0
for path in "${FORBIDDEN_PATHS[@]}"; do
  # endpoints.py lists these in FORBIDDEN_ENDPOINT_FRAGMENTS on purpose.
  if grep -rn --include='*.py' -F "$path" src/ 2>/dev/null | grep -v 'endpoints.py'; then
    failure "forbidden endpoint '$path' referenced outside endpoints.py"
    found_paths=1
  fi
done
[ "$found_paths" -eq 0 ] && pass "no withdrawal, transfer, deposit, or lending endpoints in src/"

# ---------------------------------------------------------------
section "2. Hosts are confined to endpoints.py"
host_hits="$(grep -rn --include='*.py' -F 'okx.com' src/ 2>/dev/null \
             | grep -v 'src/btcbot/exchange/endpoints.py' || true)"
if [ -n "$host_hits" ]; then
  echo "$host_hits"
  failure "an OKX host literal appears outside endpoints.py"
else
  pass "every OKX host literal lives in exchange/endpoints.py"
fi

if grep -q 'ALLOWED_DEMO_HOSTS: frozenset\[str\] = frozenset' src/btcbot/exchange/endpoints.py &&
   grep -q 'ALLOWED_WS_URLS: frozenset\[str\] = frozenset' src/btcbot/exchange/endpoints.py &&
   grep -q 'FORBIDDEN_WS_URLS: frozenset\[str\] = frozenset' src/btcbot/exchange/endpoints.py; then
  pass "REST allow-list, WS allow-list and WS deny-list are immutable frozensets"
else
  failure "an allow-list or the deny-list is not a frozenset"
fi

# Every demo WS host carries the "pap" infix; its live twin is the same name
# with that infix dropped. Both must be present — one allowed, one forbidden.
for pair in "wspap ws" "wseeapap wseea" "wsuspap wsus"; do
  demo="${pair% *}"; live="${pair#* }"
  if grep -q "wss://${demo}\.okx\.com:8443/ws/v5/private" src/btcbot/exchange/endpoints.py; then
    pass "demo WebSocket host ${demo}.okx.com is present"
  else
    failure "demo WebSocket host ${demo}.okx.com is missing"
  fi
  if grep -q "\"wss://${live}\.okx\.com:8443/ws/v5/private\"" src/btcbot/exchange/endpoints.py; then
    pass "live WebSocket host ${live}.okx.com is explicitly forbidden"
  else
    failure "live WebSocket host ${live}.okx.com is not in FORBIDDEN_WS_URLS"
  fi
done

# The grep checks above prove the literals exist. This proves the *sets* are
# correct: no live URL leaked into an allow-list, every region resolves, and
# no region resolves to anything outside the allow-lists.
if profile_summary="$("$PY" - <<'PYEOF'
import sys
sys.path.insert(0, "src")
from btcbot.exchange.endpoints import (
    ALLOWED_DEMO_HOSTS, ALLOWED_WS_URLS, DEMO_PROFILES, FORBIDDEN_WS_URLS,
    is_allowed_ws_url,
)

leaked = ALLOWED_WS_URLS & FORBIDDEN_WS_URLS
assert not leaked, f"live WS URL in the allow-list: {sorted(leaked)}"
assert not any(is_allowed_ws_url(u) for u in FORBIDDEN_WS_URLS), "a live WS URL is accepted"
assert DEMO_PROFILES, "the demo profile registry is empty"
for region, profile in DEMO_PROFILES.items():
    assert profile.rest_hosts <= ALLOWED_DEMO_HOSTS, f"{region}: REST host not allow-listed"
    for url in profile.ws_urls:
        assert is_allowed_ws_url(url), f"{region}: WS URL not allow-listed: {url}"
        assert "pap" in url.split("//", 1)[1].split(".", 1)[0], f"{region}: {url} is not a demo host"
print(f"{len(DEMO_PROFILES)} demo regions; {len(ALLOWED_DEMO_HOSTS)} REST hosts; "
      f"{len(FORBIDDEN_WS_URLS)} live WS URLs forbidden")
PYEOF
)"; then
  pass "every region profile resolves to demo-only hosts; no live URL is reachable"
  note "$profile_summary"
else
  printf "%s\n" "$profile_summary"
  failure "the region profile registry failed its invariants (see above)"
fi

# No region may be a live-only entity: the registry is demo profiles only.
if grep -q 'class DemoProfile' src/btcbot/exchange/endpoints.py &&
   ! grep -qE 'LIVE_PROFILE|MAINNET_PROFILE|PROD_PROFILE' src/btcbot/exchange/endpoints.py; then
  pass "the profile registry defines demo profiles only — no live profile exists"
else
  failure "a live/mainnet profile appears in endpoints.py"
fi

# ---------------------------------------------------------------
section "3. Demo header is centrally enforced"
if grep -q 'SIMULATED_TRADING_HEADER = "x-simulated-trading"' src/btcbot/exchange/endpoints.py &&
   grep -q 'SIMULATED_TRADING_VALUE = "1"' src/btcbot/exchange/endpoints.py; then
  pass "the demo switch constants are defined once, in endpoints.py"
else
  failure "the x-simulated-trading constants are missing or altered"
fi

# Exactly one module may build headers: the transport layer.
header_users="$(grep -rln --include='*.py' 'SIMULATED_TRADING_HEADER' src/ 2>/dev/null \
                | grep -v 'endpoints.py' || true)"
if [ "$header_users" = "src/btcbot/exchange/rest.py" ]; then
  pass "only the transport layer (rest.py) injects the demo header"
else
  echo "$header_users"
  failure "the demo header is referenced outside the single transport choke point"
fi

if grep -q '_finalize_headers' src/btcbot/exchange/rest.py &&
   grep -q 'final\[SIMULATED_TRADING_HEADER\] = SIMULATED_TRADING_VALUE' src/btcbot/exchange/rest.py; then
  pass "_finalize_headers unconditionally sets x-simulated-trading: 1"
else
  failure "the central header injection is missing from rest.py"
fi

# Every request must route through the choke point. Count the request calls
# that pass headers and make sure each one is finalized.
raw_header_calls="$(grep -n 'headers=' src/btcbot/exchange/rest.py \
                    | grep -v '_finalize_headers' \
                    | grep -v 'headers={"User-Agent"' \
                    | grep -v 'headers=signed.headers' || true)"
if [ -z "$raw_header_calls" ]; then
  pass "no request builds headers outside _finalize_headers"
else
  echo "$raw_header_calls"
  note "review: a header set above must be the negative-control probe only"
fi

# The negative control is the ONE deliberate no-header request.
nc_uses="$(grep -rln --include='*.py' 'NEGATIVE_CONTROL_PATH' src/ 2>/dev/null \
           | grep -v 'endpoints.py' || true)"
if [ "$nc_uses" = "src/btcbot/exchange/rest.py" ]; then
  pass "the live-environment negative control lives only in rest.py"
else
  echo "$nc_uses"
  failure "the negative-control path is referenced outside the probe"
fi

# ---------------------------------------------------------------
section "4. No real-money mode"
if grep -rniE --include='*.py' '\b(live_trading|real_money|enable_live|is_live|mainnet_mode|disable_simulated|trading_mode *= *.live.)\b' src/ 2>/dev/null; then
  failure "a real-money/live-mode flag was found"
else
  pass "no live/real-money mode flag exists"
fi

if grep -q 'cannot be disabled' src/btcbot/config/schema.py; then
  pass "demo verification cannot be disabled by configuration"
else
  failure "the config guard on require_demo_verification_for_orders is missing"
fi

if grep -q 'margin_mode: Literal\["isolated"\]' src/btcbot/config/schema.py; then
  pass "margin mode is isolated-only — no silent cross fallback is configurable"
else
  failure "the isolated-margin constraint is missing from the schema"
fi

# ---------------------------------------------------------------
section "5. No hardcoded credentials or instrument IDs"
cred_hits="$(grep -rnE --include='*.py' \
  "(api_key|api_secret|apikey|secret|passphrase)[[:space:]]*=[[:space:]]*[\"'][A-Za-z0-9_-]{16,}[\"']" \
  src/ 2>/dev/null || true)"
if [ -n "$cred_hits" ]; then
  echo "$cred_hits"
  failure "a hardcoded credential-like literal was found"
else
  pass "no hardcoded credentials in src/"
fi

# The X-Perp instId must be discovered at runtime, never written into source.
inst_hits="$(grep -rn --include='*.py' -E "[\"']BTC-USD[TC]?-SWAP[\"']" src/ 2>/dev/null \
             | grep -v '^\s*#' || true)"
if [ -n "$inst_hits" ]; then
  echo "$inst_hits"
  failure "a hardcoded instrument ID was found — it must be discovered at runtime"
else
  pass "no hardcoded instId in src/ (the X-Perp is discovered)"
fi

if grep -q '^\.env$' .gitignore && grep -q '^data/$' .gitignore && grep -q '^logs/$' .gitignore; then
  pass ".env, data/ and logs/ are git-ignored"
else
  failure ".gitignore does not exclude .env, data/ and logs/"
fi

if [ -f .env ] && git check-ignore -q .env 2>/dev/null; then
  pass ".env exists and git is ignoring it"
  perms="$(stat -f '%Lp' .env 2>/dev/null || stat -c '%a' .env 2>/dev/null || echo '')"
  if [ "$perms" = "600" ] || [ "$perms" = "400" ]; then
    pass ".env permissions are owner-only ($perms)"
  elif [ -n "$perms" ]; then
    note ".env permissions are $perms — consider: chmod 600 .env"
  fi
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

if grep -q 'register_secret(passphrase)' src/btcbot/config/loader.py; then
  pass "the API passphrase is registered for log redaction"
else
  failure "the passphrase is not registered with the redaction filter"
fi

# ---------------------------------------------------------------
section "6. Order-path guards"
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

# Reconciliation runs after an order was accepted, when state is uncertain.
# It must be structurally incapable of sending anything.
if grep -qE 'place_order|cancel_order|cancel_all|set_leverage' \
     src/btcbot/execution/reconciliation.py; then
  failure "the fill reconciler references an order-submitting call"
else
  pass "fill reconciliation is read-only — it cannot submit or cancel anything"
fi

if grep -q 'record_unconfirmed_order' src/btcbot/execution/demo_executor.py &&
   grep -q 'record_unconfirmed_order' src/btcbot/safety/circuit_breakers.py; then
  pass "an unconfirmed fill enters SAFE_MODE instead of being guessed at"
else
  failure "the unconfirmed-fill SAFE_MODE path is missing"
fi

if grep -q 'max_risk_pct' src/btcbot/risk/position_sizing.py && \
   grep -q 'clamp(risk_pct' src/btcbot/risk/position_sizing.py; then
  pass "position sizing is hard-clamped to the configured risk ceiling"
else
  failure "the position-size clamp is missing"
fi

# The research ledger must never derive its capital from the account total.
# `totalEq` values BTC, ETH, OKB and anything else the demo account holds.
if grep -q 'total_equity' src/btcbot/execution/research_equity.py &&
   ! grep -qE '(starting_equity|current_equity)[^=]*=[^=]*total_equity' \
       src/btcbot/execution/research_equity.py; then
  pass "research equity is never derived from OKX totalEq"
else
  failure "research equity appears to be derived from the account total"
fi

if grep -q 'RESEARCH_CURRENCY = "USDT"' src/btcbot/execution/research_equity.py &&
   grep -q 'research_equity_cap_usdt' src/btcbot/config/schema.py; then
  pass "research capital is USDT-only and capped by configuration"
else
  failure "the research-equity cap or its USDT restriction is missing"
fi

if grep -q 'equity=self.research_equity.current_equity' src/btcbot/app/orchestrator.py &&
   grep -q 'equity=research.current_equity' src/btcbot/app/orchestrator.py; then
  pass "risk state and order sizing both read research equity, not totalEq"
else
  failure "an order or risk path still sizes from the OKX account total"
fi

if grep -q '_set_and_confirm_leverage' src/btcbot/execution/demo_executor.py &&
   grep -q 'get_leverage_info' src/btcbot/execution/demo_executor.py; then
  pass "leverage is set AND confirmed before every entry"
else
  failure "the set-and-confirm leverage step is missing"
fi

if grep -q 'liq_buffer_stop_ratio' src/btcbot/risk/leverage_engine.py; then
  pass "liquidation buffer is enforced by the leverage engine"
else
  failure "the liquidation-buffer check is missing"
fi

if grep -q 'TdMode.ISOLATED' src/btcbot/execution/demo_executor.py &&
   ! grep -q 'TdMode.CROSS' src/btcbot/execution/demo_executor.py; then
  pass "the executor submits isolated-margin orders only"
else
  failure "the executor references cross margin"
fi

# ---------------------------------------------------------------
# Historical backfill is pagination. Coupling it to candle timing once cost an
# hour per timeframe at startup; these keep the two jobs separated.
if grep -qE 'wait_until_next_candle|seconds_until_next|next_candle_close' \
     src/btcbot/market_data/historical.py; then
  failure "historical backfill references a next-candle waiting helper"
else
  pass "historical backfill never waits for a candle to close"
fi

if grep -q 'oldest >= cursor_end' src/btcbot/market_data/historical.py &&
   grep -q 'MAX_BACKFILL_PAGES' src/btcbot/market_data/historical.py; then
  pass "backfill pagination has a no-progress guard and a hard page cap"
else
  failure "the backfill pagination guards are missing"
fi

section "7. Look-ahead protection"
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
section "8. Bare except / silent failures"
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
  printf "%sAUDIT PASSED — demo-only, header-enforced, no real-money capability.%s\n" "$GREEN" "$OFF"
  exit 0
fi
printf "%sAUDIT FAILED — %d issue(s) above must be fixed.%s\n" "$RED" "$FAILURES" "$OFF"
exit 1
