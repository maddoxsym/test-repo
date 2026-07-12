# CONVERSION_REPORT — MT5/OANDA ➜ native cTrader Python cBot

> **V3 single-file build:** cTrader embeds only the main Python file of a
> cBot and does not package sibling directories, so the modular layout
> fails at runtime with `No module named 'adaptive_bot'`. The deployable
> artefact is **`XAUUSD_Adaptive_Bot_V3_main.py`** (class
> `XAUUSD_Adaptive_Bot_V3`): the entire `adaptive_bot` package inlined
> into one self-contained file — no local imports, no `__file__`, no
> third-party packages. It is generated from the modular tree, which stays
> in the repo as the unit-tested source of truth. The V3 file passed
> `py_compile`, pyflakes (only the expected `api`/`TimeFrame`/`TradeType`
> star-import names remain, provided by cTrader at runtime), and a
> runtime smoke test against a mocked cTrader API covering: live-account
> refusal, EURUSD refusal, 130 simulated minutes of ticks with new-bar
> detection and M5 decisions, a real order execution path
> (ExecuteMarketOrder → ModifyPosition → journal, risk capped at 0.25%),
> duplicate-order blocking, broker-side close reconciliation from History,
> and the emergency-stop file.

This report documents the full conversion of the repository's previous
trading bots into `XAUUSD_Adaptive_Bot`, a native cTrader Algo Python cBot
for GOLD on a Skilling demo account.

## 1. What existed before

| Legacy artefact | Description | Outcome |
|---|---|---|
| `xauusd_adaptive_bot.py` | 7,041-line single-file **MetaTrader 5** bot: smart-money strategy engine (structure/liquidity/zones/FVGs/order blocks/scoring/risk locks) + MT5 connector/execution + Telegram + sqlite + backtester | Strategy core **ported** into `ctrader_bot/adaptive_bot/`; every MT5-specific layer **removed and replaced** with cTrader equivalents; file **deleted** |
| `oanda_bot/` package, `run_bot.py`, `backtest.py`, `tests/`, `.env.example`, `requirements.txt` | OANDA v20 REST bot (XAU_USD + EUR_USD) with REST client, API tokens, account ids, practice/live endpoints | **Deleted entirely** (all OANDA code, tokens, account ids, `api-fxpractice`/`api-fxtrade` endpoints, REST requests) |

Validation greps for `MetaTrader5`, `mt5`, `OANDA`, `OANDA_API_TOKEN`,
`OANDA_ACCOUNT_ID`, `api-fxpractice`, `api-fxtrade`, `requests`,
`telegram` return **zero code hits** — the only remaining occurrences of
"MT5" are two explanatory comments in `core/models.py` documenting what
replaced the old concepts (this paragraph and those comments are
documentation, not code). Greps for `TODO`, `NotImplemented`,
`placeholder`, `fake`, `mock` and bare `pass` statements return nothing.

## 2. What was preserved (rule: do not shorten the system)

The entire broker-independent strategy core was ported class-for-class,
with its logic intact:

* **SwingDetector** — fractal swings, confirmation-delayed, non-repainting.
* **StructureAnalyzer** — HH/HL/LH/LL trend, BOS / CHoCH / MSS events,
  protected swings, dealing range, premium/discount.
* **LiquidityDetector** — equal highs/lows clusters, swing/PDH/PDL/PWH/PWL
  and session levels, objective sweep definition (trade-through + close-back
  or displacement; a wick alone is not a sweep), targets from opposing
  liquidity.
* **SupplyDemandDetector** — leg-in/base/leg-out zones (RBD/DBR/DBD/RBR),
  displacement scoring, touch/age freshness, invalidation.
* **OrderBlockDetector** — last opposing candle before displacement with a
  required structure/sweep link, mitigation tracking.
* **FVGDetector** — strict 3-candle gaps, ATR-filtered, fill-state tracked;
  confluence only, never a standalone entry.
* **MarketRegimeDetector** — 11 regimes from structure + volatility; spread
  and news-volatility regimes disable all entry models.
* **All six entry models** — TREND_CONTINUATION, LIQUIDITY_SWEEP_REVERSAL,
  BREAK_RETEST, RANGE_EXTREME, SESSION_LIQUIDITY, HTF_ZONE_REACTION —
  including the do-not-chase extension rule and per-regime model gating.
* **SetupScorer** — identical 0–100 component weights (bias 15, zone 15,
  sweep 15, structure 15, displacement 10, confluence 10,
  premium/discount 5, session 5, news 5, target 5); grades B/A/A+ at
  70/80/90.
* **PositionSizer** — true monetary risk from live symbol spec with
  spread+slippage+commission cost model, round-down, min-volume rejection.
* **Risk locks** — daily loss, daily trade cap, per-session cap, weekly
  drawdown, consecutive-loss pause, daily profit hard stop.
* **TradeManager** (now PositionManager) — break-even with structure
  evidence, partial TP, structural trailing, time stop, weekend flat;
  stops only ever tighten.
* **SessionManager** — Asia/London/NY/overlap tracking, session highs/lows
  as liquidity, rollover/Friday/Monday windows.
* **Journal** — every accepted AND rejected setup with full score
  breakdown; every completed trade with R-multiple and MFE/MAE.

## 3. What changed for cTrader (and why)

| Legacy (MT5/OANDA) | cTrader replacement | Notes |
|---|---|---|
| `MetaTrader5.initialize/login` + credentials in env vars | none needed — the cBot runs inside the platform session | rule: no API keys / passwords |
| `mt5.copy_rates_from_pos()` / OANDA `/candles` REST | `api.MarketData.GetBars(TimeFrame.…)`, forming bar excluded | six series: M1, M5, M15, H1, H4, D1 |
| `mt5.symbol_info()` | `api.Symbol` → `TickSize`, `TickValue`, `PipSize`, `PipValue`, `Digits`, `VolumeInUnitsMin/Max/Step`, `MarketHours.IsOpened()` | volumes are cTrader **units** (oz), not MT5 lots |
| `mt5.order_send(request)` | `api.ExecuteMarketOrder(TradeType, symbol, volumeUnits, label, slPips, tpPips)` then `api.ModifyPosition(position, slPrice, tpPrice)` to pin the exact structural levels | SL/TP attached at fill; refine step never widens the stop |
| `mt5.positions_get()` / ticket numbers | `api.Positions` scan by `SymbolName` + `Label`; `Position.Id` | duplicate prevention by label |
| MT5 deal history | `api.History` (`HistoricalTrade.PositionId/NetProfit/ClosingTime/ClosingPrice`) | also used to rebuild the daily guard after a restart |
| `mt5.account_info().equity` | `api.Account.Equity` / `.Balance` / `.IsLive` / `.Currency` | `IsLive` drives the demo-only lock |
| MT5 lot rounding | `Symbol.VolumeInUnits*` + own round-down (`CTraderSymbolSpec.round_volume_down`) | always DOWN, min-volume ⇒ reject |
| Telegram notifier | removed | cTrader log + CSV journal instead |
| sqlite `DatabaseManager` | CSV journal + broker-history state rebuild | no DB dependency inside cTrader |
| OANDA/API news provider (`NEWS_API_KEY`) | **removed**; manual/schedule-based `NewsFilter` | see "not reproducible" below |
| Adaptive timeframe selector (D1/H4/H1/M30/M15/M5/M1 combos) | **fixed plan M15 bias / M5 decision / M1 trigger** per your spec; H1/H4/D1 retained for HTF-zone context | the spec explicitly fixes the roles |
| `EntryMode.LIMIT_ON_RETEST` pending limit orders | **M1-trigger-confirmed market entry** (micro CHoCH/BOS, sweep, rejection candle, FVG retest, OB mitigation, displacement close, break-and-retest) | closest safe equivalent: the retest confirmation happens on M1 *before* entry instead of resting a limit order; avoids unmanaged pending orders |
| Learning engine / strategy re-weighting (OANDA bot) | **not carried over** | your spec defines a transparent fixed scoring system instead |
| LiveGate (multi-step path to live) | **demo-only lock, no live path at all** | stricter than before, per your instructions |
| Backtest/walk-forward/Monte-Carlo engines | cTrader's built-in backtester + guide in README | the strategy core stays testable offline via `tests/` |

New (was not in either legacy bot): the explicit **M1 entry-trigger gate**
(`strategy/entry_trigger.py`) with armed-setup flow — a setup confirmed on
M15+M5 is "armed" for a bounded window and only executes when a 1-minute
trigger fires; it is invalidated if price breaks the structural stop first.

Risk defaults were tightened to your spec: 0.25% per trade (hard-capped in
the validator — the config refuses larger values), 1% daily loss including
floating, 3 trades/day, one position, min RR 1.5, break-even/trailing/
partials disabled by default, reversals against the M15 bias disabled by
default (`allow_reversals=False`, and countertrend still requires score 80+
when enabled).

## 4. What could NOT be reproduced exactly

1. **Live news protection.** The legacy bots could poll a REST calendar
   (with an API key) or a public feed. Inside cTrader Python there is no
   reliable built-in calendar, and this bot deliberately makes no network
   calls and uses no keys. Replacement: NFP first-Friday schedule rule +
   user-maintained FOMC/CPI/speech date lists + free-form blackout windows,
   with minutes-before/after configurable, every news rejection logged, and
   the score capped so the bot never pretends protection is complete.
   **You must keep the date lists current for real event protection.**
2. **Tick-level paper engine.** The legacy PAPER mode simulated fills
   locally. cTrader's own backtester replaces it (better data, real fill
   model); there is no separate paper mode in the cBot.
3. **Pending limit entries.** Replaced by M1-trigger-confirmed market
   entries (see table). Behaviourally close, structurally safer under the
   one-position + no-pending-order rules.
4. **Sub-minute MFE/MAE.** Tracked from completed M1 candles rather than
   ticks — accurate to the candle extreme.
5. **Exact intra-bar SL/TP ordering in bar-data backtests** is decided by
   cTrader's backtest model, not the bot; use tick data for fidelity.

## 5. Remaining risks (honest list)

* **API-surface risk.** The cTrader Python wrapper evolves; if your
  cTrader Mac build exposes a slightly different generated template, follow
  README §1 ("if the build complains about imports"). All platform calls
  are concentrated in the single main file to make any such fix local.
  The calls used are the standard documented cAlgo members
  (`MarketData.GetBars`, `Symbol.*`, `Account.*`, `Positions`, `History`,
  `ExecuteMarketOrder`, `ModifyPosition`, `ClosePosition`,
  `Server.Time`, `Print`, `Stop`).
* **TickValue semantics.** Sizing assumes `Symbol.TickValue` is per **one
  unit** of volume (the documented cAlgo convention used with
  `VolumeInUnits`). The bot prints the derived per-unit value at startup
  and, in strict mode, refuses to trade if it is implausible for gold —
  verify that line on first run.
* **Server-time offset.** If Skilling's cTrader server clock is not UTC,
  session windows shift; verify once, set `server_utc_offset_hours`.
* **Empty news lists = no FOMC/CPI protection.** The bot warns at startup.
* **Symbol spec drift.** Volume min/step and spread differ across brokers;
  all are read live at startup and logged — read that log line once.
* **Strategy risk.** Unchanged from any discretionary SMC system: the
  logic is objective but markets are not obliged to cooperate. No
  profitability is claimed anywhere.

## 6. What must be tested before demo execution

Offline (already done in this repo, re-run any time):

1. `cd ctrader_bot && python3 -m unittest discover tests -v` — 43 tests
   cover swings/structure/liquidity/zones/FVGs/order blocks, scoring,
   sizing (incl. min-volume rejection and round-down), daily locks
   (realised, floating, cap, emergency, new-day reset, streak), session
   gating, news rules (NFP first Friday, FOMC lists, manual windows,
   never-complete), spread gate, M1 triggers, the order-safety checklist
   (live-account block, duplicate block, wrong-side SL, failure cooldown)
   and an end-to-end pipeline run with a no-look-ahead resampling check.

Inside cTrader (your side, in order):

2. **Build** the cBot (README §1) — must compile with no errors.
3. **Backtest** on XAUUSD m1, several months (README §4). Confirm in the
   log: no trades in blocked sessions/rollover, no trade without SL/TP,
   never more than 1 position / 3 trades per day, daily lock engages after
   a −1% day, scores/breakdowns printed for every armed setup.
4. **Demo dry-run**: attach to the Skilling demo, verify the startup block
   (demo confirmed, symbol accepted, spec line plausible, journal path),
   let it run at least a week, and review `setups_*.csv` / `trades_*.csv`
   before drawing any conclusion.
5. Confirm the **emergency stop** works: create `EMERGENCY_STOP.txt` in the
   journal folder and watch entries stop within a minute.
6. Confirm the **live block**: attaching to a live account must print
   `LIVE ACCOUNT BLOCKED` and stop (do this with the platform in read-only
   mood, i.e. just attach and watch it refuse).
