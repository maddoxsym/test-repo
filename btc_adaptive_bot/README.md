# BTC Adaptive Bot — Bybit Demo Research System

A trading **research** system for Bitcoin. It runs 38 different trading strategies
against a Bybit **Demo** account for exactly 14 days, measures which ones actually
work, and then picks a winner based on evidence.

> ### This version cannot trade real money
>
> There is no real-money mode, no "live" switch, and no withdrawal, transfer, or
> deposit code anywhere in this project. The demo host is pinned in a single
> source file, and the system refuses to send any order until it has proven — via
> four independent checks — that it is talking to a demo account. See
> [§3 What keeps this safe](#3-what-keeps-this-safe).

---

## Contents

1. [What the bot does](#1-what-the-bot-does)
2. [The three evidence layers](#2-the-three-evidence-layers)
3. [What keeps this safe](#3-what-keeps-this-safe)
4. [How learning works](#4-how-learning-works)
5. [What champion mode means](#5-what-champion-mode-means)
6. [Install on a Mac](#6-install-on-a-mac)
7. [Get your Bybit Demo API keys](#7-get-your-bybit-demo-api-keys)
8. [Verify the connection](#8-verify-the-connection)
9. [Start the 14-day experiment](#9-start-the-14-day-experiment)
10. [The dashboard](#10-the-dashboard)
11. [Stopping and restarting safely](#11-stopping-and-restarting-safely)
12. [Reading the reports](#12-reading-the-reports)
13. [Configuration](#13-configuration)
14. [Keeping it running for 14 days](#14-keeping-it-running-for-14-days)
15. [Troubleshooting](#15-troubleshooting)
16. [Honest limitations](#16-honest-limitations)

---

## 1. What the bot does

Most trading bots run one strategy and hope. This one runs **38 genuinely
different strategies at once**, gives each its own $10,000 of pretend money,
records every decision, and after two weeks tells you which strategies earned
their keep and which did not.

Concretely, once you start it, the bot:

- connects to your Bybit Demo account and confirms it is really a demo account
- streams live BTC prices, order book, and trades from Bybit
- reads BTC-relevant news and tracks upcoming macro events (Fed, CPI, jobs)
- classifies the market into one of 11 "regimes" (trending, ranging, volatile…)
- asks all 38 strategies, on every closed candle, whether they see a setup
- records **every** signal — taken *and* rejected, with the reason
- trades all of them simultaneously in isolated simulated accounts
- picks one qualifying strategy at a time to place a **real order on Bybit Demo**
- sizes every position automatically, bounded by a hard risk ceiling
- backtests all 38 strategies over roughly a year of historical data
- keeps going for exactly 14 calendar days, then ranks everything and picks a champion

### The 38 strategies

Seven families, each testing a different idea about how markets behave:

| Family | Count | The hypothesis being tested |
|---|---:|---|
| Trend following | 7 | Price that has been moving keeps moving |
| Breakout | 7 | Leaving a well-defined range means continuation |
| Momentum | 5 | Rate of change carries information beyond price level |
| Mean reversion | 5 | Price stretched far from a reference snaps back |
| Structure / liquidity | 7 | Swing points, broken levels, stop runs and gaps matter |
| Volume / microstructure | 5 | Participation tells you what price alone does not |
| Regime / ensemble | 2 | Choosing *between* strategies is itself a strategy |

These are not the same strategy 38 times with different numbers. Mean reversion and
momentum make **opposite** predictions from the same data — running both is how the
experiment discovers *when* each one is right. Every strategy declares a written
hypothesis, and a test enforces that no two share one.

---

## 2. The three evidence layers

Two weeks of live results is not enough evidence to trust anything. So every
strategy is judged on three independent sources at once:

### Layer 1 — Historical backtesting

Roughly a year of BTC history, replayed one candle at a time. Split into
**training → validation → out-of-sample** with a gap between segments, plus
**walk-forward** analysis (fit on one period, test on the next, repeatedly).

Everything is charged realistically: maker/taker fees, bid/ask spread, slippage,
latency, minimum order size, quantity rounding, and partial fills. Each strategy
is also re-run at **2× and 4× costs** — an edge that dies when fees double was
never really there.

### Layer 2 — Parallel shadow trading (live, simulated)

All 38 strategies trade simultaneously against live Bybit prices, each in its own
account starting at exactly **$10,000**. The isolation is strict: strategy A
losing money can never affect strategy B's position size. This is where most of
the live evidence comes from, because all 38 can trade at once.

### Layer 3 — Actual Bybit Demo orders

Real authenticated orders on your demo account, from **day 1** — not after two
weeks. Only one strategy at a time may hold the real position, because otherwise
you could never tell whose trade made or lost the money.

An **allocator** decides who gets each turn using Thompson sampling: strategies
with a good record get more turns, strategies with few observations get
guaranteed exploration turns, and a strategy that got lucky early cannot take
over the account. A test enforces that last property specifically.

---

## 3. What keeps this safe

### Four independent demo checks

Before **any** order can be sent, all four of these must pass:

| Check | What it proves |
|---|---|
| **Host pin** | The demo hostname is a constant in one source file. Not from config, not from an environment variable, not from a command-line flag. An authenticated client for any other host raises an error at construction. |
| **Authenticated reachability** | Your credentials genuinely work on the demo module. |
| **Demo-only endpoint probe** | Bybit's demo-funds endpoint exists only on demo. A zero-amount call confirms the route is there without moving anything. |
| **Mainnet negative control** | Your key is sent **once**, read-only, to the mainnet host — and is **required to fail**. If it succeeds, it is a real-money key and the bot refuses to trade with it. |

If any check fails you get this, and no orders are sent:

```
==================================================
SAFETY LOCK
BYBIT DEMO ENVIRONMENT COULD NOT BE VERIFIED
ORDER SUBMISSION DISABLED
==================================================
```

Verification is repeated hourly and after every reconnect. If it ever fails
mid-experiment, order submission stops immediately.

### Everything else

- **Bounded position sizing.** Size varies with equity, stop distance, volatility,
  confidence, measured expectancy, drawdown, regime, spread, and news risk — but
  it is hard-clamped to a maximum of **2% risk per trade**. The pipeline is
  size → exchange rounding → min/max → notional → final risk check → order, and
  any failure means no trade.
- **No duplicate orders.** Every order row is written to the database *before* the
  network call, protected by a uniqueness constraint. A crash mid-send still
  blocks a repeat on restart.
- **Circuit breakers** for impossible prices, absurd quantities, invalid balances,
  stale data, repeated API errors, state mismatches, and runaway order rates. Any
  trip puts the system into `SAFE_MODE`: research continues, real orders stop.
- **Trading pauses on stale data.** It will not act on a price it does not trust,
  and it will not close a position on a bad price either.
- **Credentials** are read only from `.env`, never written to the database, and
  scrubbed from every log by a redaction filter.

Run the audit yourself at any time:

```bash
./scripts/audit_safety.sh
```

---

## 4. How learning works

The bot adapts through **data**, never by rewriting its own code. There is no
source-code self-modification, and nothing here is called AI because nothing here
is AI — it is statistics you can check by hand.

What actually adapts:

- **Allocation** — which strategy gets the next real demo trade
- **Regime weighting** — which strategy is trusted in which market conditions
- **Confidence calibration** — if a strategy's "high confidence" signals don't
  actually win more often, its confidence stops influencing position size
- **Parameters** — proposed on one slice of data, then **validated on a different
  slice**. The window that suggested a change can never be the window that
  approves it. Every promotion and rejection is recorded with its reason.

### It does not tune after every loss

A losing trade in a positive-expectancy strategy is the cost of doing business,
not a mistake. The loss analyser needs at least 15 trades before it will attribute
anything, and it explicitly reports "normal variance" when that is the honest
answer. When it does find something, it names it: unsuitable regime, stop too
tight, target unrealistic, costs eating the edge, sizing amplifying losses, or
statistically-significant deterioration.

### Anti-overfitting

Scoring actively penalises the things that make backtests lie:

- tiny samples (below the minimum, expectancy earns **zero** credit)
- one giant winner carrying the whole record
- profit concentrated in a single historical period
- edges that vanish at higher fees or slippage
- parameters that only work at one exact value (a spike, not a plateau)
- out-of-sample collapse

---

## 5. What champion mode means

At the end of day 14 the bot freezes the dataset, ranks every strategy on a
**12-component weighted score**, and declares a champion.

It will **not** pick a champion on weak evidence. If nothing has earned it, it says
so. If different strategies clearly own different market regimes, the champion
becomes a **regime-aware ensemble** instead of one strategy — and it explains why.

Then it switches automatically from `BYBIT_DEMO_RESEARCH` to
`BYBIT_DEMO_CHAMPION` — **still on the same demo account, still no real money**.
The champion takes the real demo trades; all 37 challengers keep running in shadow
mode and can be promoted later, but only on statistically meaningful new evidence.
Three lucky wins will not do it (there is a test for that).

Scoring deliberately defeats the two classic traps:

- **95% win rate with one catastrophic loss** → loses to a consistent strategy
- **+100% return on 4 trades** → scores near zero, because samples that small earn no credit

---

## 6. Install on a Mac

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

## 7. Get your Bybit Demo API keys

**The key must be created inside Bybit's Demo Trading area.** A normal account key
will not work here — and the bot will refuse it on purpose.

1. Log in at [bybit.com](https://www.bybit.com)
2. Switch to **Demo Trading** (top-right account menu). This is a separate account
   with its own user ID.
3. Hover your avatar → **API** → create a new key
4. Give it trading permission for Spot; **no** withdrawal permission is needed and
   none is used
5. Put the key and secret in `.env`:

```bash
BYBIT_DEMO_API_KEY=your_demo_key_here
BYBIT_DEMO_API_SECRET=your_demo_secret_here
```

`.env` is git-ignored and created with `600` permissions. Never share it.

### If your demo balance isn't around $10,000

```bash
./scripts/topup_demo_funds.sh USDT 10000
```

This uses Bybit's demo-only funds endpoint (limit: one request per minute). The bot
always reads and uses your **actual** balance — it never assumes $10,000.

### A note on Bybit EU

Bybit's own documentation states that the EU site's API (`api.bybit.eu`) only
supports the third-party-application feature for API broker users — it is not a
general trading API. The documented demo-trading module is `api-demo.bybit.com`,
which is what this project uses. Full detail and sources:
[`docs/bybit_capabilities.md`](docs/bybit_capabilities.md).

Whether you are eligible to use Bybit is between you and Bybit. This project
contains nothing that circumvents age, KYC, geographic, or account restrictions.

---

## 8. Verify the connection

```bash
./scripts/verify_demo_connection.sh
```

This authenticates, runs all four demo checks, reads your balance, fetches
instrument rules and live BTC data, checks your key's permissions, and **places no
order**. It ends with `RESULT: PASS` or `RESULT: FAIL`.

Research mode will not place demo orders until this passes.

To sanity-check your install without any authenticated calls:

```bash
./scripts/dry_run.sh
```

Dry run uses real public market data, submits nothing, and **does not start the
14-day timer**.

---

## 9. Start the 14-day experiment

```bash
./scripts/run_research.sh
```

First run prints:

```
==================================================
BYBIT DEMO RESEARCH — 14-DAY RESEARCH

Environment:               DEMO
Starting Demo Equity:      $10,000.00
Expected Research Capital: $10,000.00
Shadow Equity Per Strategy: $10,000.00
Strategies:                38
Primary Market:            BTCUSDT
Duration:                  14 days
Real Money:                DISABLED
Scheduled End:             2026-08-08T11:00:00Z
Experiment ID:             exp_7f3a...
==================================================
```

Then it runs on its own. It will not ask you to confirm individual trades.

The 14-day clock starts **only** after configuration validates, Bybit Demo
authenticates, demo status is verified, market data is confirmed working, and
database migrations succeed.

---

## 10. The dashboard

Open **<http://127.0.0.1:8787>** while the bot is running. It is local-only and
read-only — it cannot affect trading.

It shows system and data health, `DAY X / 14` with time remaining, your demo
balance and open position, shadow trade counts, live BTC price and current regime,
the top-10 strategy leaderboard, recent news, recent orders, learning activity,
and — after day 14 — the champion and its challengers.

Check status without opening a browser:

```bash
./scripts/status.sh
```

---

## 11. Stopping and restarting safely

**Stop:** press `Ctrl+C` once. It saves state, closes streams cleanly, backs up the
database, and exits. Don't `kill -9` it.

**Restart:** run `./scripts/run_research.sh` again.

**The 14-day timer does not restart.** The start time lives in SQLite. Restarting
Python, rebooting your Mac, losing Wi-Fi, or closing the dashboard all resume the
*same* experiment. On restart the bot reloads the experiment, re-verifies demo
status, reads your balance, checks open orders, re-attaches any open position from
its ledger, downloads candles it missed, reconciles everything, and continues.

Outages are recorded. By default the 14 days are **14 real calendar days** — the
window is never silently extended. (You can opt into extension with
`experiment.outage_adjustment_policy: extend_by_outage`.)

---

## 12. Reading the reports

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
`./scripts/export_csv.sh` (signals, shadow trades, demo orders, positions, metrics,
news, regimes, rankings — all CSV, all analysable without this codebase).

### How to read the final ranking

Look at **`final_score`** and **`confidence`** together. `HIGH` confidence requires a
full sample, at least 10 real demo trades, positive expectancy on two or more
independent layers, a bootstrap interval clear of zero, and no penalties. Most
strategies will be `LOW` after two weeks — that is the honest result, not a bug.

Short-only strategies will show a zero `Demo` score if your demo account is
spot-only. That layer was unavailable to them; their shadow and historical
evidence still counts.

---

## 13. Configuration

Edit `config/research.yaml` (which inherits from `config/default.yaml`). No source
changes needed. Config is validated at startup and fails loudly, naming the exact
field.

Commonly adjusted:

```yaml
market:
  primary_symbol: BTCUSDT
  timeframes: ["1", "3", "5", "15", "30", "60", "240"]

risk:
  normal_risk_pct: 0.0075     # 0.75% risk per real demo trade
  max_risk_pct: 0.02          # hard ceiling — cannot be exceeded

shadow:
  initial_equity: 10000.0     # per strategy

strategies:
  enabled: []                 # empty = all 38
  disabled: []                # e.g. ["orderbook_imbalance_1m"]
  overrides:
    ema_trend_cross_15m:
      rr_target: 3.0
```

There is no setting anywhere that enables real-money trading.

---

## 14. Keeping it running for 14 days

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

## 15. Troubleshooting

**`SAFETY LOCK ... COULD NOT BE VERIFIED`**
Your key is probably not a Demo Trading key. Create one inside Bybit's Demo
Trading area (§7). Run `./scripts/verify_demo_connection.sh` — it names the exact
check that failed.

**`THESE CREDENTIALS AUTHENTICATE ON THE BYBIT MAINNET HOST`**
Working as designed: that is a real-money key and the bot will not use it. Create a
demo key instead.

**403 Forbidden reaching Bybit**
Either a proxy/firewall is blocking `api-demo.bybit.com`, or your IP is in a region
Bybit refuses (Bybit documents that US and Mainland China IPs are rejected), or your
account isn't eligible from your location. The error message lists all three. This
project does not attempt to work around any of them.

**`Bybit demo credentials not found`**
`cp .env.example .env` and fill in both values.

**No trades after several hours**
Normal and usually correct. Strategies wait for setups they actually recognise, and
the bot never manufactures signals. Check the dashboard: if `DAY X / 14` is counting
and shadow trades are appearing, it is working. Shadow trades always appear long
before real demo trades, because only one strategy at a time may take a real one.

**"Position sizing rejected"**
Also normal. Usually the stop was too tight to size safely, or the position would
have been below Bybit's $5 minimum notional. Refusing is the correct outcome.

**Dashboard won't load**
Confirm the bot is running, then check the port isn't taken:
`lsof -i :8787`. Change `dashboard.port` in config if needed.

**Tests failing after a change**
`./.venv/bin/python -m pytest -q` and `./scripts/audit_safety.sh`.

---

## 16. Honest limitations

Things worth knowing before you trust any of it:

- **14 days is a small sample.** Most strategies will end at `LOW` confidence.
  That is the point of reporting confidence rather than hiding it.
- **Demo fills are not real fills.** Demo liquidity and slippage differ from real
  markets. A strategy that works on demo has not been proven with real money.
- **Exits are managed locally**, not by resting stop orders on the exchange. This
  is deliberate — on spot there is no exchange-side position to attribute a resting
  order to. The trade-off is that a local stop only acts while the process is
  running; stale-data detection and restart recovery bound that exposure.
- **Backtests can only be as good as bar data allows.** Where a bar's range
  contains both the stop and the target, the simulator always assumes the stop.
- **Order-book and trade-flow strategies produce no historical evidence**, because
  that data cannot be reconstructed from candles. They are scored on shadow and
  demo evidence only, and the sample-size penalty accounts for the thinner record.
- **The news engine uses keyword scoring**, not a language model. Every score is
  traceable to the exact terms that produced it. That is a deliberate choice for
  auditability, and it means subtle headlines will be misread.
- **FOMC dates are modelled by pattern**, not fetched from the Fed's calendar, and
  are marked low-confidence accordingly.
- **Past performance does not predict future results.** This is a research tool
  for learning what does and does not work. It is not financial advice.

---

## Project layout

```
btc_adaptive_bot/
├── config/          default.yaml, research.yaml, champion.yaml
├── docs/            bybit_capabilities.md — verified API facts and sources
├── scripts/         setup, verify, run, status, backup, report, export, audit
├── src/btcbot/
│   ├── app/         orchestrator, experiment timer, CLI, verifier
│   ├── exchange/    endpoints (the only place a hostname appears), REST, WS, demo guard
│   ├── market_data/ candle series with look-ahead guards, historical downloader
│   ├── features/    indicator library, shared feature engine
│   ├── regime/      11-regime multi-feature classifier
│   ├── strategies/  base interface + 38 strategies in 7 families
│   ├── backtesting/ execution model, bar-replay engine, walk-forward, splits
│   ├── shadow/      isolated $10,000 accounts
│   ├── risk/        bounded position sizing
│   ├── execution/   allocator, position ledger, order safety, demo executor
│   ├── learning/    metrics, parameter candidates, loss attribution, calibration
│   ├── scoring/     12-component scorer, champion selection, promotion, decay
│   ├── news/        provider adapters, point-in-time store, effectiveness tracking
│   ├── reporting/   daily/strategy/execution/learning/final reports, CSV export
│   ├── dashboard/   local read-only web UI
│   ├── database/    SQLite schema, migrations, repositories
│   └── safety/      circuit breakers, SAFE_MODE
└── tests/           553 tests — unit and integration
```

## Commands

```bash
./scripts/setup_mac.sh               # one-time setup
./scripts/verify_demo_connection.sh  # check connection (no orders)
./scripts/dry_run.sh                 # debug run (no orders, no timer)
./scripts/run_research.sh            # start/resume the 14-day experiment
./scripts/run_champion.sh            # champion mode
./scripts/status.sh                  # current status
./scripts/report.sh                  # regenerate reports
./scripts/export_csv.sh              # export all data to CSV
./scripts/backup.sh                  # back up the database
./scripts/audit_safety.sh            # verify the safety architecture
./scripts/topup_demo_funds.sh        # request demo funds
```

## License

MIT. Provided for research and education. Not financial advice. Use at your own risk.
