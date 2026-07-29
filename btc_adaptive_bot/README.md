# BTC Adaptive Bot — OKX Demo Research System

A trading **research** system for Bitcoin. It runs 52 different trading strategies
against the **BTC X-Perp** on an OKX **Demo** account for exactly 14 days,
measures which ones actually work, and then picks a winner based on evidence.

> ### This version cannot trade real money
>
> There is no real-money mode, no "live" switch, and no withdrawal, transfer, or
> deposit code anywhere in this project. OKX selects live-vs-demo with a request
> header, so that header is injected by a **single transport-layer function** that
> every request passes through — no endpoint can omit it. The REST host is pinned
> to one OKX regional entity (`exchange.region` — Global/UAE by default), the
> WebSocket hosts are pinned to that region's *demo* endpoints, and the system
> refuses to send any order until four independent checks prove it is talking to
> the demo environment. See
> [§3 What keeps this safe](#3-what-keeps-this-safe).

---

## Contents

1. [What the bot does](#1-what-the-bot-does)
2. [The three evidence layers](#2-the-three-evidence-layers)
3. [What keeps this safe](#3-what-keeps-this-safe)
4. [Leverage, margin and liquidation](#4-leverage-margin-and-liquidation)
5. [How learning works](#5-how-learning-works)
6. [What champion mode means](#6-what-champion-mode-means)
7. [Install on a Mac](#7-install-on-a-mac)
8. [Get your OKX Demo API keys](#8-get-your-okx-demo-api-keys)
9. [Verify the connection](#9-verify-the-connection)
10. [Start the 14-day experiment](#10-start-the-14-day-experiment)
11. [The dashboard](#11-the-dashboard)
12. [Stopping and restarting safely](#12-stopping-and-restarting-safely)
13. [Reading the reports](#13-reading-the-reports)
14. [Configuration](#14-configuration)
15. [Keeping it running for 14 days](#15-keeping-it-running-for-14-days)
16. [Troubleshooting](#16-troubleshooting)
17. [Honest limitations](#17-honest-limitations)

---

## 1. What the bot does

Most trading bots run one strategy and hope. This one runs **52 genuinely
different strategies at once**, gives each its own $10,000 of pretend money,
records every decision, and after two weeks tells you which strategies earned
their keep and which did not.

Concretely, once you start it, the bot:

- connects to your OKX Demo account and confirms it is really demo
- **discovers** the tradable BTC X-Perp from the exchange's instrument list —
  the instrument ID is never hardcoded
- streams live BTC prices, order book, trades, funding and open interest
- reads BTC-relevant news and tracks upcoming macro events (Fed, CPI, jobs)
- classifies the market into one of 11 "regimes" (trending, ranging, volatile…)
- asks all 52 strategies, on every closed candle, whether they see a setup
- runs every candidate through a **ten-layer decision pipeline** and records
  **every** signal — taken *and* rejected, with the exact layer that refused it
- trades all of them simultaneously in isolated simulated accounts
- picks one qualifying strategy at a time to place a **real order on OKX Demo**,
  long or short
- **chooses leverage automatically** (1x–10x) for every trade, then sets it at the
  exchange and reads it back to confirm before the order exists
- sizes every position automatically, bounded by a hard risk ceiling
- backtests all 52 strategies over roughly a year of historical data
- keeps going for exactly 14 calendar days, then ranks everything and picks a champion

### The 52 strategies

Seven families, chosen so their failure modes differ rather than their parameters:

| Family | Count | Examples |
|---|---|---|
| Trend | 8 | EMA cross, EMA+ADX, Supertrend, MACD, Ichimoku, Donchian, MA pullback, multi-timeframe pullback |
| Breakout | 9 | Donchian, range, volatility, squeeze, S/R, breakout-retest, range expansion, volume-confirmed, failed-breakout trap |
| Momentum | 6 | RSI, MACD histogram, rate-of-change, volume-confirmed, multi-timeframe, shallow pullback |
| Mean reversion | 6 | Bollinger+RSI, Z-score, VWAP, Keltner, extreme deviation, stable-range value |
| Structure / smart money | 14 | Swing structure, BOS, CHoCH, liquidity sweep, S/R rejection, structure retest, FVG, multi-timeframe SMC, order-block mitigation, breaker block, previous-day sweep, weekly sweep, premium/discount, multi-confirmation reversal |
| Volume / positioning | 7 | Abnormal-volume continuation and exhaustion, order-book imbalance, trade-flow imbalance, VWAP+volume, open-interest breakout, funding divergence |
| Ensemble | 2 | Regime-adaptive selector, weighted multi-strategy ensemble |

Two of these are perp-native and **stand down entirely when the data is absent**:
the open-interest strategy needs two comparable open-interest readings, and the
funding-divergence strategy needs a delivered funding rate. Neither ever infers a
value it was not given.

---

## 2. The three evidence layers

A strategy is only trusted when independent lines of evidence agree.

### Layer 1 — Historical backtesting

Roughly a year of candles, replayed bar by bar with hard look-ahead guards:
indicators are computed only from closed bars, and the replay cursor cannot read
past itself. Split into train / validation / out-of-sample with an embargo
between segments, then walk-forward tested across rolling windows. Costs are
modelled: maker/taker fees, spread, slippage, latency, partial fills, contract
rounding — **and perpetual funding**, pro-rated over every bar a position is held.
Everything is re-run at 2× and 4× costs; a strategy that dies under stress is
fragile, and the score says so.

### Layer 2 — Parallel shadow trading (live, simulated)

Every strategy gets its own isolated $10,000 account and trades continuously on
live data. Strategy A's equity cannot touch Strategy B's. Funding accrues at the
exchange's **real** rate as soon as the stream delivers one. This is where the
bulk of the sample comes from.

### Layer 3 — Actual OKX Demo orders

One strategy at a time controls a real demo position, chosen by a Thompson-sampling
allocator with forced exploration so an early lucky winner cannot monopolise the
account. Long and short both go through the discovered X-Perp — never by selling
spot. Every order records the experiment, strategy, version, setup, signal,
direction, leverage, risk percentage, regime, timeframe and confidence.

The champion decision weighs all three.

---

## 3. What keeps this safe

### The environment model, and why it matters

Unlike exchanges that host demo on a separate domain, **OKX serves live and demo
from the same REST host and selects between them with a header**:

```
x-simulated-trading: 1
```

A missing header would mean a live request. So the header is not the caller's
responsibility: every request this system emits — public or authenticated — gets
its headers from one function, `OkxDemoClient._finalize_headers`, which sets the
header unconditionally and cannot be overridden by its input. The safety audit and
the test suite both assert that no second header-building path exists.

WebSockets *are* host-separated, and the demo host differs from the live host by a
single `pap` infix (`wspap` vs `ws`, `wseeapap` vs `wseea`, `wsuspap` vs `wsus`) —
so WS URLs are checked against an **exact-match allow-list**, never a substring
test, and the nine live URLs are named in an explicit deny-list as well.

### Regions

An OKX API key is issued by one regional entity and is unknown to the others,
which OKX reports as `50119 API key doesn't exist` — the most common cause of a
failed verification. Set `exchange.region` to the entity that issued your Demo
Trading key:

| `exchange.region` | Entity | REST | Demo WebSocket |
|---|---|---|---|
| `global` *(default)* | OKX Global / UAE | `https://openapi.okx.com` | `wss://wspap.okx.com:8443/ws/v5/…` |
| `eea` | OKX Europe (EEA) | `https://eea.okx.com` | `wss://wseeapap.okx.com:8443/ws/v5/…` |
| `us` | OKX US | `https://us.okx.com` | `wss://wsuspap.okx.com:8443/ws/v5/…` |

Every profile in that registry is a demo profile. There is no region value that
reaches a live endpoint, and `./scripts/verify_okx_demo_connection.sh` prints the full
table if it sees a `50119`.

### Four independent demo checks

Order submission is structurally impossible until all four pass:

| # | Check | What it proves |
|---|---|---|
| 1 | **Host pin** — the REST base URL must be a recognised OKX demo host and every WS URL must be on the exact-match demo allow-list. The host set is fixed in code; YAML only picks *which* demo region, never an arbitrary URL. | The client cannot be built against an unrecognised host, and the socket cannot be opened to a live host. |
| 2 | **Header enforcement** — the client's own header builder is exercised at runtime and must produce `x-simulated-trading: 1`. | No request can leave without the demo switch. |
| 3 | **Authenticated demo reachability** — account config and balance succeed *with* the header, and a usable position mode comes back. | The key works in demo, and the account is usable. |
| 4 | **Live-environment negative control** — the same credentials are sent **once**, read-only, **without** the header, and are *required to fail* with OKX error `50101`. | The key cannot act on the live environment. If it authenticates there, the system refuses to trade with it. |

If any check fails:

```
==================================================
SAFETY LOCK
OKX DEMO ENVIRONMENT COULD NOT BE VERIFIED
ORDER SUBMISSION DISABLED
==================================================
```

Verification re-runs hourly and after every reconnect. A revoked verification
disables orders immediately.

### Everything else

- **No fund-movement code.** No withdrawal, deposit, transfer, sub-account,
  convert, or lending endpoint exists in `src/`. `./scripts/audit_safety.sh`
  fails the build if one appears.
- **Isolated margin only.** The config type is `Literal["isolated"]` — cross
  margin is not expressible, so there is no silent fallback.
- **Clock-drift detection.** Local time is measured against OKX server time; drift
  beyond the budget pauses authenticated trading rather than stamping orders from
  an untrusted clock.
- **Duplicate-order protection.** A `UNIQUE(setup_id, intent)` constraint is
  written to SQLite *before* the request is sent, so a crash between "sent" and
  "recorded" still blocks a repeat on restart.
- **Circuit breakers** for impossible prices, absurd quantities, invalid balances,
  stale data, clock drift, repeated API errors, state mismatch, duplicate orders,
  abnormal order rates, corrupt strategy output, and liquidation risk. Tripping
  enters `SAFE_MODE`: shadow research continues, real orders stop.
- **Credentials never logged.** Key, secret and passphrase are all registered with
  a redaction filter the moment they load; only a masked prefix is ever displayed.

---

## 4. Leverage, margin and liquidation

Leverage on a perpetual does **not** change stop-out risk — that is set by position
size × stop distance, which the sizer bounds independently. What leverage changes
is margin efficiency and liquidation distance. The `DYNAMIC_LEVERAGE_ENGINE`
treats it that way.

For every entry it selects a value from confidence, volatility, regime confidence,
drawdown and risk state, then clamps it to the **minimum** of: the configured
maximum (10x hard ceiling in the schema), the instrument's discovered maximum, and
a liquidation-safety maximum. Then:

1. The projected liquidation distance must be at least **3× the stop distance**
   (configurable). If it is not, leverage steps down until it is — and if even 1x
   fails, the trade is refused.
2. The chosen leverage is written with `set-leverage` and **read back** with
   `leverage-info`. A mismatch aborts the entry. An order is never sent under an
   unconfirmed leverage value.
3. Required margin must fit inside the configured share of available balance.

Every decision — approved or refused, with all inputs and the computed buffer —
is written to the `leverage_decisions` table. A high-confidence setup does not
automatically get 10x; nothing does.

Live positions are monitored: if margin ratio falls to the configured floor, the
position is flattened and trading pauses. Liquidation is never used as a stop.

---

## 5. How learning works

The bot adapts through **statistics, not self-rewriting code**. There is no
mechanism anywhere for it to modify its own source.

What adapts: strategy allocation weights (Thompson sampling over measured
results), regime fitness weights, confidence calibration, news influence, and
parameter candidates.

### It does not tune after every loss

A parameter candidate must be proposed, validated on held-out data, and beat
production by a configured margin on expectancy, drawdown and profit factor
before it is promoted. Promotions and rejections are both recorded with reasons.
One loss is not evidence.

### Anti-overfitting

Scores are penalised for: small samples, a single trade dominating profit,
profit concentrated in one period, fragility to higher fees/slippage, and
parameter instability (neighbouring parameter values must perform similarly).
Bootstrap confidence intervals are reported rather than point estimates.

---

## 6. What champion mode means

At exactly 14 days the system freezes the dataset, runs final validation across
all three layers, ranks everything, and selects either a single champion, a
regime-dependent champion, or a validated ensemble — or reports
`LOW CONFIDENCE — MORE DEMO RESEARCH REQUIRED` if the evidence does not support a
pick. Refusing to name a winner is a valid outcome.

It then transitions automatically to `OKX_DEMO_CHAMPION` **on the same demo
account**. The champion controls real demo execution; challengers keep running in
shadow mode and can earn promotion — but only after enough new out-of-sample
trades, higher validated expectancy, acceptable drawdown and regime stability.
Never after a few lucky trades. It never switches to live; there is nothing to
switch to.

---

## 7. Install on a Mac

You need macOS and Python 3.11 or newer. If you don't have Python:

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12
```

Then, from the project folder:

```bash
./scripts/setup_mac.sh
```

This creates a virtual environment, installs pinned dependencies, creates the
`data/`, `logs/` and `reports/` folders, copies `.env.example` to `.env`, and runs
the test suite. It does not touch an existing `.env`.

---

## 8. Get your OKX Demo API keys

**The key must be created inside OKX's Demo Trading area.** A normal account key
will not work here — and the bot will refuse it on purpose.

1. Log in to your OKX account
2. Switch to **Demo Trading**
3. Go to your profile → **API** → create a new **demo** API key
4. Give it **Read** and **Trade** permissions. Withdrawal permission is neither
   needed nor used — this project contains no withdrawal code at all.
5. OKX keys have **three** parts. Put all three in `.env`:

```bash
OKX_DEMO_API_KEY=your_demo_key_here
OKX_DEMO_API_SECRET=your_demo_secret_here
OKX_DEMO_PASSPHRASE=the_passphrase_you_chose
```

The passphrase is the one **you** chose when creating the key — not your account
login password.

6. Set `exchange.region` in `config/default.yaml` to the OKX entity you just
   created the key on — `global` (Global/UAE, the default), `eea`, or `us`. A key
   from one entity does not exist on the others; see
   [Regions](#regions) and the `50119` entry in
   [Troubleshooting](#16-troubleshooting).

`.env` is git-ignored. Set owner-only permissions:

```bash
chmod 600 .env
```

### About the instrument

The OKX demo interface shows the contract as **BTCUSD UM X-Perp**. That is a
display name; the API instrument ID behind it is **discovered at runtime** from
`/api/v5/public/instruments`, filtered to a live linear BTC swap and ranked by your
configured settle-currency preference. If no such instrument is available to your
account, verification fails loudly with the full instrument list logged — the
system never substitutes spot, another coin, or a live product.

Full API findings and sources: [`docs/okx_demo_capabilities.md`](docs/okx_demo_capabilities.md).

Whether you are eligible to use OKX is between you and OKX. This project contains
nothing that circumvents age, KYC, geographic, or account restrictions.

---

## 9. Verify the connection

```bash
./scripts/verify_okx_demo_connection.sh
```

Seventeen read-only checks: host pin, demo header enforcement, clock drift,
authentication, the live-environment negative control, account configuration and
position mode, balance, X-Perp discovery, the full contract specification,
leverage limits, market data, funding rate and fee schedule. It **places no
order** and ends with `RESULT: PASS` or `RESULT: FAIL` naming the exact failure.

Then prove the whole order path with one minimum-size round trip:

```bash
./scripts/smoke_test_okx_demo.sh --confirm-demo
```

This sets 1x leverage, confirms it, opens the **smallest valid position**, verifies
the fill, closes it reduce-only, confirms the account is flat, and prints every
order and fill ID. It does **not** start the 14-day timer. The `--confirm-demo`
flag is required because this is the only script that submits an order outside a
running experiment.

To sanity-check your install with no authenticated calls at all:

```bash
./scripts/dry_run.sh
```

Dry run uses real public market data, submits nothing, and **does not start the
14-day timer**.

---

## 10. Start the 14-day experiment

```bash
./scripts/run_research.sh
```

First run prints a banner with the environment, starting demo equity, strategy
count, discovered instrument, duration, and the scheduled end instant. Then it
runs on its own — it will not ask you to confirm individual trades.

The 14-day clock starts **only** after configuration validates, OKX Demo
authenticates, demo status is verified, the X-Perp is discovered, the account and
position modes are understood, market data is confirmed working, and database
migrations succeed.

---

## 11. The dashboard

Open **<http://127.0.0.1:8787>** while the bot is running. It is local-only and
read-only — it cannot affect trading.

It shows system and safety state, the experiment countdown, the demo account
(equity, margin ratio, drawdown, risk state), the discovered instrument and its
contract specification, the open position with leverage and liquidation estimate,
the decision layer (regime, confidence, recent rejections with the layer that
refused them, recent leverage decisions), and the research leaderboards.

---

## 12. Stopping and restarting safely

`Ctrl+C` once. The bot finishes what it is doing, persists shadow equity, allocator
state and open positions, backs up the database, and exits.

Restarting **resumes the same experiment** — the 14-day timer does not restart.
On startup it reloads the experiment, re-verifies demo, reads your real balance,
reconciles open orders and positions against the ledger, re-confirms leverage,
recovers missing candles, and only then resumes. An order is never blindly
repeated after a crash.

---

## 13. Reading the reports

Everything lands in `reports/`.

| File | What it is |
|---|---|
| `daily_report_dayNN.md` | One per day: PnL, trades, leaders, laggards, outages, news, learning |
| `strategy_report.md` | Per-strategy metrics with regime breakdowns |
| `execution_report.md` | Real demo orders, fills, and rejections with attribution |
| `learning_report.md` | Parameter candidates, promotions/rejections, filter effectiveness |
| `final_14_day_report.html` | **The main deliverable** — open in a browser |
| `final_14_day_report.md` | Same content as text |
| `final_14_day_metrics.csv` | Full ranking table for your own analysis |

Regenerate anytime with `./scripts/report.sh`. Export raw data with
`./scripts/export_csv.sh` (signals, rejected signals, shadow trades, demo orders,
fills, positions, leverage decisions, metrics, news, regimes, rankings — all CSV,
all analysable without this codebase).

### How to read the final ranking

Look at **`final_score`** and **`confidence`** together. `HIGH` confidence requires
a full sample, at least 10 real demo trades, positive expectancy on two or more
independent layers, a bootstrap interval clear of zero, and no penalties. Most
strategies will be `LOW` after two weeks — that is the honest result, not a bug.

A zero `Demo` score means the strategy never won an allocation to the real demo
account. Its shadow and historical evidence still counts.

---

## 14. Configuration

Edit `config/research.yaml` (which inherits from `config/default.yaml`). No source
changes needed. Config is validated at startup and fails loudly, naming the exact
field.

Commonly adjusted:

```yaml
market:
  # No instId here on purpose — the X-Perp is discovered at runtime.
  base_currency: BTC
  settle_currency_preference: ["USDT", "USDC", "USD"]
  timeframes: ["1", "3", "5", "15", "30", "60", "240"]

risk:
  margin_mode: isolated       # cannot be changed — no cross fallback
  normal_risk_pct: 0.0075     # 0.75% risk per real demo trade
  max_risk_pct: 0.02          # hard ceiling — cannot be exceeded
  leverage:
    min_leverage: 1.0
    max_leverage: 10.0        # schema-enforced ceiling
    base_leverage: 2.0
    liq_buffer_stop_ratio: 3.0    # liquidation ≥ 3× the stop distance
    margin_utilization_cap: 0.5   # margin ≤ 50% of available

shadow:
  initial_equity: 10000.0     # per strategy

strategies:
  enabled: []                 # empty = all 52
  disabled: []                # e.g. ["orderbook_imbalance_1m"]
  overrides:
    ema_trend_cross_15m:
      rr_target: 3.0
```

There is no setting anywhere that enables real-money trading, selects a live host,
or disables the demo header.

---

## 15. Keeping it running for 14 days

The scripts wrap the bot in `caffeinate` to prevent idle sleep.

**Closing your laptop lid can still suspend the Mac**, depending on your hardware
and power settings. If that happens the bot stops collecting data until you reopen
it — the outage is recorded, the timer keeps running, and the 14-day window does
**not** extend. For an uninterrupted run: keep the lid open, or use a desktop Mac,
or connect to power with an external display.

To auto-restart after a reboot or crash, install the optional launchd agent:

```bash
sed -e "s|__PROJECT_DIR__|$(pwd)|g" -e "s|__USER__|$(whoami)|g" \
  scripts/launchd/com.btcbot.research.plist.template \
  > ~/Library/LaunchAgents/com.btcbot.research.plist
launchctl load -w ~/Library/LaunchAgents/com.btcbot.research.plist
```

Remove it with `launchctl unload -w ~/Library/LaunchAgents/com.btcbot.research.plist`.
Note that with the agent loaded, a clean `Ctrl+C` will also be restarted — unload it
first if you want the bot to stay stopped.

The database is backed up automatically every 6 hours to `data/backups/`, or on
demand with `./scripts/backup.sh`.

---

## 16. Troubleshooting

**`SAFETY LOCK ... COULD NOT BE VERIFIED`**
Your key is probably not a Demo Trading key. Create one inside OKX's Demo Trading
area (§8). Run `./scripts/verify_okx_demo_connection.sh` — it names the exact check
that failed.

**`THESE CREDENTIALS AUTHENTICATE ON THE OKX LIVE ENVIRONMENT`**
Working as designed: that is a live key and the bot will not use it. Create a demo
key instead.

**`OKX demo credentials not found`**
`cp .env.example .env` and fill in all **three** values — OKX needs the passphrase
as well as the key and secret.

**`code=50101` on every authenticated call**
That is the environment-mismatch error. It means a demo key was sent to the live
environment or vice versa. In the negative control this is the *expected, healthy*
result; anywhere else it means your key was not created in the Demo Trading area.

**`no live linear BTC perpetual found`**
Verification could not find the X-Perp in your demo account's instrument list. The
error logs the instruments it did find. Check that Demo Trading is enabled and that
you can see BTCUSD UM X-Perp in the OKX Demo interface.

**`local clock differs from OKX server time`**
Enable NTP time sync (System Settings → General → Date & Time → Set automatically).
OKX rejects requests with stale timestamps, and the bot pauses authenticated
trading rather than sending orders from an untrusted clock.

**`50119 API key doesn't exist`**
Almost always the wrong region, not a bad key: an OKX key is issued by one
regional entity and does not exist on the others. Set `exchange.region` in
`config/default.yaml` to the entity you created the Demo Trading key on —
`global` (Global/UAE), `eea`, or `us`.
`./scripts/verify_okx_demo_connection.sh` prints the full table when it sees this
code. If the region is right, confirm the key was created
*inside* Demo Trading rather than on the live account.

**403 Forbidden reaching OKX**
Either a proxy/firewall is blocking the configured REST host, or your IP is in a region OKX
refuses, or your account isn't eligible from your location. The error lists all
three. This project does not attempt to work around any of them.

**No trades after several hours**
Normal and usually correct. Strategies wait for setups they actually recognise, and
the bot never manufactures signals. Check the dashboard: if `DAY X / 14` is counting
and shadow trades are appearing, it is working. Shadow trades always appear long
before real demo trades, because only one strategy at a time may take a real one.

**"Position sizing rejected" / "leverage engine rejected the entry"**
Also normal. Usually the stop was too tight to size safely, the position rounded
below the contract minimum, or the liquidation buffer could not be satisfied at any
allowed leverage. Refusing is the correct outcome — and the reason is recorded in
`rejected_signals`.

**Dashboard won't load**
Confirm the bot is running, then check the port isn't taken:
`lsof -i :8787`. Change `dashboard.port` in config if needed.

**Tests failing after a change**
`./.venv/bin/python -m pytest -q` and `./scripts/audit_safety.sh`.

---

## 17. Honest limitations

Things worth knowing before you trust any of it:

- **14 days is a small sample.** Most strategies will end at `LOW` confidence.
  That is the point of reporting confidence rather than hiding it.
- **Demo fills are not real fills.** Demo liquidity, slippage and funding differ
  from real markets. A strategy that works on demo has not been proven with real
  money.
- **Exits are managed locally**, not by resting stop orders on the exchange. The
  trade-off is that a local stop only acts while the process is running;
  stale-data detection, restart recovery and the liquidation-protection breaker
  bound that exposure.
- **Backtests can only be as good as bar data allows.** Where a bar's range
  contains both the stop and the target, the simulator always assumes the stop.
- **Backtested funding is modelled, not historical.** The engine pro-rates a
  configured funding rate rather than replaying the real historical funding
  series, which OKX does not expose far enough back for this purpose. Live and
  shadow layers use the exchange's actual rate. Treat backtest funding as a
  sensitivity assumption, and note that the stress runs scale it too.
- **Order-book, trade-flow, open-interest and funding strategies produce no
  historical evidence**, because that data cannot be reconstructed from candles.
  They are scored on shadow and demo evidence only, and the sample-size penalty
  accounts for the thinner record.
- **The news engine uses keyword scoring**, not a language model. Every score is
  traceable to the exact terms that produced it. That is a deliberate choice for
  auditability, and it means subtle headlines will be misread.
- **FOMC dates are modelled by pattern**, not fetched from the Fed's calendar, and
  are marked low-confidence accordingly.
- **OKX's primary documentation was unreachable from the build environment.** The
  API facts were corroborated from two independent maintained SDKs plus search,
  and every uncertain value is discovered at runtime rather than assumed. This is
  documented honestly in [`docs/okx_demo_capabilities.md`](docs/okx_demo_capabilities.md) §0.
- **Past performance does not predict future results.** This is a research tool
  for learning what does and does not work. It is not financial advice.

---

## Project layout

```
btc_adaptive_bot/
├── config/          default.yaml, research.yaml, champion.yaml
├── docs/            okx_demo_capabilities.md — verified API facts and sources
├── scripts/         setup, verify, smoke test, run, status, backup, report, export, audit
├── src/btcbot/
│   ├── app/         orchestrator, experiment timer, CLI, verifier, smoke test
│   ├── exchange/    endpoints (the only place a hostname appears), REST, WS, demo guard
│   ├── market_data/ candle series with look-ahead guards, historical downloader
│   ├── features/    indicator library, shared feature engine
│   ├── regime/      11-regime multi-feature classifier
│   ├── decision/    ten-layer decision engine, risk states
│   ├── strategies/  base interface + 52 strategies in 7 families
│   ├── backtesting/ execution model (incl. funding), bar-replay engine, walk-forward
│   ├── shadow/      isolated $10,000 accounts
│   ├── risk/        bounded position sizing, DYNAMIC_LEVERAGE_ENGINE
│   ├── execution/   allocator, position ledger, order safety, demo executor
│   ├── learning/    metrics, parameter candidates, loss attribution, calibration
│   ├── scoring/     12-component scorer, champion selection, promotion, decay
│   ├── news/        provider adapters, point-in-time store, effectiveness tracking
│   ├── reporting/   daily/strategy/execution/learning/final reports, CSV export
│   ├── dashboard/   local read-only web UI
│   ├── database/    SQLite schema, migrations, repositories
│   └── safety/      circuit breakers, SAFE_MODE
└── tests/           657 tests — unit and integration
```

## Commands

```bash
./scripts/setup_mac.sh                      # one-time setup
./scripts/verify_okx_demo_connection.sh     # 17 checks, no orders
./scripts/smoke_test_okx_demo.sh --confirm-demo   # one minimum-size round trip
./scripts/dry_run.sh                        # debug run (no orders, no timer)
./scripts/run_research.sh                   # start/resume the 14-day experiment
./scripts/run_champion.sh                   # champion mode
./scripts/status.sh                         # current status
./scripts/report.sh                         # regenerate reports
./scripts/export_csv.sh                     # export all data to CSV
./scripts/backup.sh                         # back up the database
./scripts/audit_safety.sh                   # verify the safety architecture
```

## License

MIT. Provided for research and education. Not financial advice. Use at your own risk.
