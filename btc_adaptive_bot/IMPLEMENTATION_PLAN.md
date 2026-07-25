# Implementation Plan — BTC Adaptive Bot (Bybit Demo Research System)

Status legend: `[x]` done · `[~]` partial · `[ ]` not started

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
