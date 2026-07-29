# Implementation Plan — BTC Adaptive Bot

Status legend: `[x]` done · `[~]` partial · `[ ]` not started

The plan has three parts: the original **Bybit Demo build** (Phases 1–17, complete — kept
below as the historical record), the **OKX Demo migration** (Phases M1–M7), which replaced
the active exchange integration with OKX Demo / BTC X-Perp, and the **regional profile
migration** (Phases R1–R6), which generalised the EEA-only pin into a demo profile
registry defaulting to OKX Global / UAE. Every exchange-independent system is preserved
throughout.

---

# Part II — OKX Europe Demo migration (BTCUSD UM X-Perp)

## Phase M1 — Research current OKX API  `[x]`

- [x] Primary docs site unreachable from this environment (403) — corroborated every fact
      from `okxapi/python-okx` (official) + `tiagosiebler/okx-api` + web search instead
- [x] EEA REST host `https://eea.okx.com`; demo enforced by `x-simulated-trading: 1` header
- [x] EEA demo WS hosts (`wseeapap.okx.com`), EEA **live** WS hosts to reject (`wseea.okx.com`)
- [x] Signing: base64 HMAC-SHA256 over `ts + METHOD + path + body`, ISO-ms timestamp, passphrase header
- [x] Endpoint paths for time/instruments/config/balance/positions/leverage/order/fills/funding
- [x] Candle `confirm` flag (look-ahead guard carries over directly)
- [x] Write `docs/okx_demo_capabilities.md` (incl. what could **not** be verified and how
      runtime discovery covers it)

**Key finding driving the design:** OKX demo is *the same host* as live, switched by a
header — so demo safety moves from "pin a separate host" to "centrally enforce the header
+ pin the EEA host + negative-control probe without the header must fail with 50101".
The WS side *is* host-separated (`wseeapap` vs `wseea`), one infix apart — exact-host
allow-list, never substring matching.

## Phase M2 — Exchange layer replacement  `[x]`

- [x] `endpoints.py` → OKX EEA hosts; forbidden-host list incl. EEA live WS, global/US hosts
- [x] `signing.py` → OKX scheme (base64, ISO ts, passphrase; WS login variant with epoch-seconds ts)
- [x] `rest.py` → `OkxDemoClient`: central `x-simulated-trading: 1` injection, envelope
      (`code`/`sCode` both levels), retry/backoff, clock-drift measurement → trading pause
- [x] `demo_guard.py` → 4 signals: host pin, header enforcement, authed demo reachability,
      live-environment negative control (must fail 50101)
- [x] `instruments.py` → SWAP discovery, X-Perp selection (linear/BTC/live), `ctVal`/`ctMult`/
      `lotSz`/`minSz`/`tickSz`/`lever` spec, contract↔base conversion
- [x] `ws.py` → public/private/**business** (candles live on business, `brokerId=9999`)
- [x] `models.py` → perp fields: posSide, tdMode, leverage, liqPx, mgnRatio, funding
- [x] Remove the Bybit integration (no selectable Bybit path remains)
- [x] `clOrdId` ≤ 32 chars → re-pack the client-order-ID layout

## Phase M3 — Derivatives execution  `[x]`

- [x] Long AND short routing (net vs long/short mode adaptation; `reduceOnly` correctness)
- [x] `DYNAMIC_LEVERAGE_ENGINE` (1x–10x from confidence/vol/regime/drawdown; journaled)
- [x] Set-and-confirm leverage before every entry (`set-leverage` → `leverage-info`)
- [x] Isolated margin only; no silent cross fallback
- [x] Liquidation protection: stop-vs-liqPx clearance check, margin-ratio circuit breaker
- [x] Funding/settlement/fee tracking wired into PnL, scores, champion selection

## Phase M4 — Decision engine  `[x]`

- [x] Ten-layer decision pipeline with per-layer accept/reject logging
- [x] `rejected_signals` + `leverage_decisions` DB entities (new migration)
- [x] Risk states NORMAL / REDUCED / DEFENSIVE / PAUSED

## Phase M5 — Research engines updated for perps  `[x]`

- [x] Shadow accounts: leverage, margin, funding, liquidation modelling
- [x] Backtester/walk-forward: same; deterministic fixture retained and extended
- [x] Strategy library expanded 38 → **52** across the enumerated set

## Phase M6 — Surfaces  `[x]`

- [x] Config/env: `OKX_DEMO_API_KEY/SECRET/PASSPHRASE`, `OKX_DEMO_RESEARCH` mode naming
- [x] Dashboard: instrument, position (leverage/liqPx/funding), decision + management panels
- [x] Scripts: `verify_okx_demo_connection.sh` (17 checks, no orders),
      `smoke_test_okx_demo.sh --confirm-demo`; retire Bybit-named scripts
- [x] Reports/README/docs wording swept for Bybit references

## Phase M7 — Verification  `[x]`

- [x] Full test suite green (all existing + new OKX tests, mocked-client integration)
- [x] `ruff` clean; `audit_safety.sh` updated for OKX (header enforcement, host confinement)
- [x] Dry run offline; audit for live endpoints / missing demo headers / contract maths

---

# Part III — Regional profile migration (OKX Global / UAE default)

**Why.** The build pinned `https://eea.okx.com`. A Demo Trading key created on an OKX
Dubai/UAE account is issued by a *different* regional entity, so that host answers
`50119 API key doesn't exist` — which reads like a bad key but is a region mismatch.

**Key finding.** REST is *not* environment-separated on OKX: every region serves demo and
live from the same host, switched by `x-simulated-trading: 1`. So widening the REST
allow-list across regions does not weaken the lock — the header plus the negative control
are what protect REST. WebSockets *are* environment-separated, by a single `pap` infix, so
that allow-list stays exact-match and the nine live URLs stay explicitly forbidden.

## Phase R1 — Region profiles  `[x]`

- [x] Re-verified endpoints against the maintained SDKs (primary docs still 403 — recorded
      in `docs/okx_demo_capabilities.md` §0)
- [x] `DemoProfile` dataclass + `DEMO_PROFILES` registry (global / eea / us), all demo-only
- [x] Default `global`: REST `https://openapi.okx.com` (alt `https://www.okx.com`),
      WS `wss://wspap.okx.com:8443/ws/v5/{public,private,business}`
- [x] `ALLOWED_DEMO_HOSTS` / `ALLOWED_WS_URLS` derived from the registry;
      `FORBIDDEN_WS_URLS` names all nine live URLs; business URL allowed with and
      without `?brokerId=9999`
- [x] `profile_for()` raises on an unknown region rather than defaulting

## Phase R2 — Thread the region through the exchange layer  `[x]`

- [x] `OkxDemoClient(profile=…)`; `LiveEnvironmentNegativeControlProbe(profile=…)`
- [x] `PublicMarketStream` / `PrivateAccountStream` dial the profile's URLs
- [x] `DemoGuard` adopts the profile its client is bound to; signal 1 validates that
      profile's WS URLs instead of three EEA constants
- [x] `exchange.region` config field (`global` | `eea` | `us`, default `global`)
- [x] 50119 responses carry a region-mismatch diagnostic naming the alternatives

## Phase R3 — App wiring  `[x]`

- [x] Orchestrator resolves the profile once and passes it to client, guard, both streams
- [x] `verify_demo.py` labels checks with the active region; prints the full region table
      on a 50119
- [x] `smoke_test.py` region-aware; dashboard shows environment, region, REST + WS hosts

## Phase R4 — Tests, audit, docs  `[x]`

- [x] `test_safety_lock.py`: region registry, per-region resolution, allow/deny-list
      invariants, guard host pin per region, tampered-profile rejection
- [x] `test_orchestrator_bootstrap.py`: region reaches client, guard and dashboard
- [x] `audit_safety.sh` section 2 rewritten region-general (demo/live pairs per region +
      an import-based invariant check that no live URL is reachable)
- [x] `okx_demo_capabilities.md` §1a, README region table + 50119 troubleshooting,
      `config/default.yaml`, `.env.example`

## Phase R5 — Verification  `[x]`

- [x] Full test suite green; `ruff check` clean; `audit_safety.sh` passing
- [x] No authenticated verification claimed — the sandbox cannot reach any exchange and
      no credentials were supplied to it

## Phase R6 — Order-response handling fix  `[x]`

Found by the first live smoke test: entry order returned `code=1 "All operations
failed"` and the real reason was never seen. OKX trade endpoints are batch-shaped even
for one order — the envelope `code` describes the batch (0 all ok, 1 all failed, 2
partial) and the rejection is per item (`sCode`/`sMsg`/`subCode`). The transport was
raising on the envelope, destroying the only diagnostic that exists.

- [x] `ORDER_OPERATION_PATHS`, `ITEM_LEVEL_ENVELOPE_CODES` and `ITEM_DIAGNOSTIC_FIELDS`
      in `endpoints.py`
- [x] `_request(item_level_errors=True)` defers codes 1 and 2 to the endpoint parser —
      order paths only, and only when a usable data array is present; requesting it on
      any other path raises `ValueError`
- [x] `OrderRejectedError(ApiError)` carrying `s_code`/`s_msg`/`sub_code`/`clOrdId`,
      with `ret_code` set to the real `sCode` so every existing handler keeps working
- [x] `place_order` inspects every item and accepts only `sCode == 0`; `cancel_order`
      and the cancel batch loop do the same, the batch counting mixed results honestly
- [x] Envelope-level rejection preserved for auth, signature, environment mismatch,
      rate limits, malformed bodies and empty/malformed data arrays
- [x] Smoke test prints `OKX sCode=` / `sMsg=` / `subCode=` on separate lines
- [x] `tests/unit/test_order_response_handling.py` — 39 tests driving the real client
      against a stubbed transport, including the reported payload verbatim and a
      credential-leakage check

## Phase R7 — Fill reconciliation  `[x]`

Found by the first fully successful live smoke test: the round trip worked end to end
(order filled, position opened, closed reduce-only, account flat) but the fill step
reported "no fill matched the client order ID". Two causes, both real:

1. OKX's read endpoints are eventually consistent and settle in a fixed order — order
   details, then positions, then **fills last**. The fills endpoint was asked immediately.
2. OKX left `clOrdId` blank on the fills endpoint, so matching on it found nothing even
   once the fill published.

- [x] `OrderDetails` model + `client.get_order(instId, ordId|clOrdId)` — `51603` returns
      `None` (a real answer), everything else still raises
- [x] `execution/reconciliation.py`: order details are the authority, polled
      immediate/0.25/0.5/1/2/2/2s (7.75s); fills polled separately on a shorter schedule
- [x] `match_fills` keys on `ordId` first, `clOrdId` only as fallback
- [x] Partial fills count as confirmed (contracts moved); canceled is terminal
- [x] Executor and smoke test share the one reconciler — no smoke-test-only path
- [x] Fill persistence resolves the order by `ordId`, so a blank-`clOrdId` fill is
      attributed rather than orphaned; late fills are persisted idempotently
- [x] Unconfirmed after the budget → `BreakerType.UNCONFIRMED_FILL` SAFE_MODE, ledger
      untouched, no replacement order (the `UNIQUE(setup_id, intent)` reservation makes
      resubmission structurally impossible)
- [x] Smoke test PASSES on order-details proof; a delayed per-fill record is `[WARN]`
- [x] `tests/unit/test_fill_reconciliation.py` (36) + executor scenarios + audit checks

## Phase R8 — Research-equity cap  `[x]`

The demo account holds BTC, ETH and other assets, so OKX's `totalEq` (~$84,000) is not
the experiment's capital. Sizing from it would let a BTC price move resize every
position and a deposit raise the risk budget, and would make the 14-day "return" a
measurement of the account rather than the strategies.

**Design.** The research ledger reads the account **exactly once**, at experiment start:
`starting = min(execution.research_equity_cap_usdt, usable USDT)`. After that it moves
only by bot-attributable PnL, fees and funding. Because nothing re-reads the account,
external changes are structurally incapable of touching it — there is no clamp to defeat.

- [x] `execution.research_equity_cap_usdt` (default 10,000) + `risk.weekly_loss_limit_pct`
- [x] `execution/research_equity.py`: USDT-only ledger, `start`/`restore`/`observe_balance`,
      `components_from_records` (grosses closed PnL back up so fees count once)
- [x] Migration 5: `research_equity_snapshots` table + the two `experiments` columns, so a
      restart resumes the same ledger instead of re-baselining
- [x] Orchestrator feeds research equity to risk state, sizing, leverage and the allocator;
      `available` stays the REAL usable USDT so margin is still checked against the account
- [x] Startup banner: "Research capital used by bot: $X" / "Other OKX Demo assets: EXCLUDED"
- [x] Dashboard shows the five figures separately; reports and Day-14 metrics use research
      capital; `weekly_loss_limit_pct` added alongside the daily limit
- [x] `tests/unit/test_research_equity.py` (50) + orchestrator, restart and sizing tests;
      3 new audit checks

---

# Part I — Bybit Demo build (historical record, complete)

> **Status note (2026-07-28):** the Bybit integration described below has been replaced by
> the OKX Europe Demo integration (Part II). The exchange-independent systems it built —
> strategies, backtesting, shadow engine, regime, news, learning, scoring, database,
> dashboard, reports — carry forward unchanged in architecture.

---

## Phase 1 — Research current API  `[x]`

- [x] Read official Bybit V5 docs from primary source (`bybit-exchange/docs@master`)
- [x] Establish demo host, WS hosts, available endpoint list, order retention, rate limits
- [x] Establish the EU-domain situation (`api.bybit.eu` = broker-only; demo lives on `api-demo.bybit.com`)
- [x] Confirm HMAC signing scheme, headers, timestamp rule
- [x] Confirm spot vs linear instrument field shapes and their deprecations
- [x] Confirm `orderLinkId` 36-char limit (drives client-order-ID design)
- [x] Write `docs/bybit_capabilities.md`

**Key finding driving the architecture:** `api.bybit.eu` does not expose a general trading
API for individuals; the documented demo module is `api-demo.bybit.com`. Public market data
comes from mainnet public streams; private streams from `stream-demo.bybit.com`.

---

## Phase 2 — Plan  `[x]`

- [x] Inspect workspace, choose structure, write this checklist

---

## Phase 3 — Foundation  `[x]`

- [x] `pyproject.toml` + pinned `requirements.txt`
- [x] `.gitignore` (secrets, db, logs, reports), `.env.example`
- [x] `config/default.yaml`, `config/research.yaml`
- [x] Pydantic config schema + loader with clear failure messages + config hashing
- [x] Structured logging: readable console tags, rotating files, JSON event log
- [x] SQLite layer: WAL, versioned migrations, transactions, repositories, backup
- [x] Error taxonomy

## Phase 4 — Bybit adapter  `[x]`

- [x] Compile-time host allow-list (`endpoints.py`) — mainnet trading unreachable by design
- [x] HMAC-SHA256 signing + server-clock offset tracking
- [x] REST client: retries, backoff, rate-limit awareness, error mapping
- [x] **Demo guard** — 4 independent signals, all must pass (host pin, authed reachability,
      demo-only endpoint probe, mainnet negative control)
- [x] Instrument discovery + capability discovery (spot/linear), periodic refresh
- [x] Wallet balance, open orders, order history, executions, positions
- [x] Order create / cancel / cancel-all (no transfer, withdraw, or deposit code exists)
- [x] Public WebSocket (kline, tickers, orderbook, trades) with heartbeat/reconnect/backoff
- [x] Private WebSocket (order, execution, wallet, position)
- [x] Sequence validation, duplicate detection, staleness tracking
- [x] Startup + reconnect state reconciliation

## Phase 5 — Historical engine  `[x]`

- [x] Paged historical kline downloader with gap detection + repair, local cache
- [x] Realistic execution model: maker/taker fees, spread, slippage, latency, partial fills
- [x] Bar-replay backtester with hard look-ahead guard (closed candles only)
- [x] Train / validation / out-of-sample splitting with embargo
- [x] Walk-forward analysis (rolling + anchored)
- [x] Execution stress tests (2x/4x fees and slippage)

## Phase 6 — Strategies  `[x]`

- [x] `Strategy` ABC: `generate_signal`, `calculate_stop`, `calculate_target`,
      `calculate_confidence`, `allowed_regimes`, `position_size_inputs`, `explain_signal`
- [x] Registry + per-strategy enable/disable + versioning
- [x] 38 distinct strategies across 7 families (trend, breakout, momentum, mean-reversion,
      structure/liquidity, volume/microstructure, regime/ensemble)
- [x] Explicit per-strategy exit-mechanism declaration

## Phase 7 — Shadow engine  `[x]`

- [x] Independent `$10,000` account per strategy, fully isolated
- [x] Concurrent positions across strategies, bar + tick driven
- [x] Fees, slippage, realized/unrealized PnL, drawdown, MFE/MAE per trade

## Phase 8 — Regime engine  `[x]`

- [x] Multi-feature classifier (ADX, ATR, realized vol, MA slope/persistence, HH/LL,
      range compression/expansion, volume, VWAP deviation) — never one indicator alone
- [x] 11 regimes + confidence + regime history persistence
- [x] Per-regime performance attribution

## Phase 9 — News/macro engine  `[x]`

- [x] Provider adapter interface; RSS (no key), CryptoPanic, NewsAPI, macro calendar
- [x] Graceful degradation → `NEWS DATA DEGRADED`, never crashes the bot
- [x] Relevance / category / sentiment / impact scoring, duplicate clustering
- [x] Point-in-time store: a decision may only read news received before its timestamp
- [x] News used as risk/size/confirmation modifier — never "positive headline ⇒ buy"
- [x] A/B tracking of whether news gating actually helps

## Phase 10 — Risk + execution  `[x]`

- [x] Volatility/confidence/drawdown-aware sizing, hard-bounded (default 0.5–1.0%, cap 2%)
- [x] Sizing pipeline: size → round → min/max → notional → final risk check → order
- [x] Thompson-sampling allocator with forced exploration for low-sample strategies
- [x] Master position ledger — one attributable demo position at a time
- [x] Order safety: unique setup/signal/client IDs, DB uniqueness, in-flight guard,
      reconciliation, pre-order disclosure log
- [x] Trade manager: fixed R/R, ATR, structure, trailing, break-even, partial, time,
      volatility, opposite-signal exits
- [x] Circuit breakers + `SAFE_MODE`

## Phase 11 — Learning  `[x]`

- [x] Full metrics engine + breakdowns (timeframe, regime, hour, weekday, vol state, news state)
- [x] Bandit allocation updates from evidence
- [x] Parameter candidate proposal → validation on held-out data → promote/reject with reason
- [x] Anti-overfitting: sample-size penalty, single-winner concentration, period
      concentration, fee/slippage sensitivity, parameter-plateau stability
- [x] Confidence calibration (predicted vs realised win rate)
- [x] Loss attribution (regime / vol / stop / target / costs / news / sizing / normal variance)

## Phase 12 — Champion system  `[x]`

- [x] Multi-factor scorer (12 weighted components) with bootstrap CIs and penalties
- [x] Day-14 finalisation: freeze dataset, stop new exploration entries, resolve positions
- [x] Champion or validated regime-ensemble selection + confidence rating + rationale
- [x] Automatic transition to `BYBIT_DEMO_CHAMPION`
- [x] Champion/challenger promotion rules + full history
- [x] Performance-decay detection

## Phase 13 — Dashboard + reports  `[x]`

- [x] FastAPI dashboard: system, experiment, demo account, research, market, ranking,
      news, trades, learning, champion/challengers
- [x] Daily / strategy / execution / learning / final-14-day reports (MD + HTML + CSV)
- [x] CSV export for all research entities

## Phase 14 — macOS scripts  `[x]`

- [x] `setup_mac.sh`, `verify_demo_connection.sh`, `run_research.sh`, `run_champion.sh`,
      `dry_run.sh`, `status.sh`, `backup.sh`, `report.sh`, `export_csv.sh`, `audit_safety.sh`
- [x] Optional launchd plist + instructions, `caffeinate` handling, lid-close caveat

## Phase 15 — Test everything  `[x]`

- [x] **553 tests passing** (unit + integration), covering every item the brief lists
- [x] Deterministic backtest fixture with hand-computed expected PnL (+$200.00 / -$100.00)
- [x] `ruff` clean across `src/` and `tests/`

### Bugs the tests found and fixed

Worth recording, because each was a real defect rather than a test artefact:

1. **`executescript` implicitly commits** — migrations wrapped in an explicit
   transaction failed with "cannot commit - no transaction is active". Migrations
   are now idempotent (`IF NOT EXISTS`) and run outside an explicit transaction,
   which is the correct pattern for SQLite DDL.
2. **Client-order-ID collisions** — the random block was 4 base-36 chars, so 2,000
   IDs collided by the birthday bound. Re-laid out the 36-char field: timestamp
   trimmed to 6 chars (sufficient past 2038), random block widened to 7 (~7.8e10).
3. **Regime vote dilution** — a textbook strong uptrend resolved to `UNCERTAIN`
   because votes split between `TREND_UP` and `STRONG_TREND_UP`, which *agree*.
   Confidence is now measured against genuinely conflicting regime families.
4. **Unmeasured parameter stability earned free credit** — a strategy with no
   walk-forward evidence collected points for stability it had never demonstrated.
   The component is now zero until actually measured.
5. **Headline deduplication could not match rewordings** — exact hashing treated
   "SEC approves Bitcoin ETF" and "Bitcoin ETF approved by SEC" as two stories.
   Replaced with stemmed-token Jaccard similarity clustering.
6. **Regime timeframes were not backfilled** — backfill covered only the
   timeframes strategies asked for, so the regime engine's context timeframe could
   be left empty if no strategy happened to use it.
7. **Backups overwrote each other within the same second** — two backups in one
   second silently produced one file. Filenames now disambiguate.
8. **Deterministic experiment IDs could collide** — restarting immediately after
   an experiment completed could reuse the primary key. IDs now carry entropy;
   resume works by query, so determinism was never needed.
9. **Unhelpful 403 diagnostics** — a bare `ProxyError: 403` now explains the three
   realistic causes (proxy/firewall, Bybit's documented region block, account
   eligibility).

## Phase 16 — End-to-end dry run  `[x]`

- [x] `btcbot dry-run` runs, and the 14-day timer does **not** start
- [x] Full pipeline verified offline: candles → features → regime → 38 strategies →
      signals journaled → shadow trades opened → metrics → reports (MD/HTML/CSV) →
      CSV export → dashboard state serialises
- [x] Orchestrator bootstrap verified against a mocked exchange: migrations →
      client → 4-signal verification → capability discovery → real balance →
      backfill → market-data check → registry → layer construction
- [x] Every CLI command exercised with correct exit codes

> **Note on the live leg.** The build sandbox's egress proxy blocks
> `api-demo.bybit.com`, so the dry run could not complete a live Bybit fetch here —
> it fails cleanly with the improved 403 diagnostic rather than crashing. The
> pipeline itself is proven by `tests/integration/test_dry_run_pipeline.py` and
> `test_orchestrator_bootstrap.py`, which drive the real components with local
> data. On a machine that can reach Bybit, `./scripts/dry_run.sh` exercises the
> same path against live public market data.

## Phase 17 — Final audit  `[x]`

- [x] No mainnet/real-money path, no withdrawal/transfer/deposit code
      (`scripts/audit_safety.sh`, 16 checks, all passing)
- [x] Every Bybit hostname confined to `exchange/endpoints.py`, asserted by test
- [x] Mainnet host reachable only by the read-only negative-control probe
- [x] No hardcoded credentials; secrets redacted in every log sink
- [x] `require_demo_verification_for_orders` cannot be disabled by configuration
- [x] Look-ahead guards verified by test (indicator causality, candle completion,
      replay bounding)
- [x] Duplicate-order protection verified, including the crash-mid-send window
- [x] Position sizing bounded under adversarial inputs (NaN, inf, zero, negative,
      inverted stops, microscopic stops)
- [x] Restart recovery verified: timer, open position, shadow equity, allocator
      learning, news influence all resume
- [x] No unverified assumptions about Bybit EU — findings and sources in
      `docs/bybit_capabilities.md`
- [x] No bare `except:` blocks anywhere
