# XAUUSD_Adaptive_Bot_V4 — 14-day autonomous DEMO research system

V4 turns the bot into a **research system**: a population of exactly-
specified strategies trades in parallel **shadow portfolios** on completed
candles; a **statistical learning system** ranks them risk-adjusted; and at
most **one real DEMO position** at a time is taken by the currently
best-evidenced strategy. After 14 days it stops opening research trades and
writes a final report labelled **"PROVISIONAL WINNER — NOT AUTOMATICALLY
READY FOR LIVE TRADING."**

V3 is untouched and still in this repo. V4 is a separate project.

**Absolute rails (validator-enforced, no live switch exists):** DEMO-only,
GOLD-only, one real position, broker-side SL+TP on every order, stops never
widen, 0.75% max risk/trade, 1.7% max combined daily loss, 5% weekly
drawdown, cooldown after 3 consecutive losses, volume rounded down, no
martingale/grid/averaging/loss-chasing, restart never resets limits or the
research clock. **No profitability is claimed.**

---

## 1. Build (cTrader Mac)

1. cTrader → **Algo** → **New cBot** → **Python** → name it
   `XAUUSD_Adaptive_Bot_V4`.
2. Open it in the editor, select all, and **paste the entire contents of
   `ctrader_bot/XAUUSD_Adaptive_Bot_V4_main.py`** (single self-contained
   file — no folders, no packages, no `__file__`). Leave
   `robot_wrapper.py` and the generated `.cs` file alone.
3. **Build** (⌘B).
4. Attach to your **Skilling DEMO** account on the **XAUUSD** chart
   (m1 or m5 chart recommended) and start it.

The first successful start persists `research_start_time` — the 14-day
clock begins there and survives every restart.

## 2. Restarting

Just stop/start the instance (or restart cTrader / the Mac). On start the
bot logs `restored N strategies from persisted state`, replays today's
closed V4 trades from broker history into the daily guard, merges the
persisted guard state (always keeping the more restrictive figures), and
resumes tracking an open position (`restart recovery: resumed tracking…`).
The research clock does NOT reset — the startup line shows
`day X/14 of research`. Open shadow positions are not resurrected after a
restart (their tick continuity is gone); their books and statistics are.

## 3. Confirming the heartbeat

In the instance **Log**, at least every 5 minutes:

```
HEARTBEAT day 3/14 | 14:35 UTC | bid 3312.40 spread 32pt | regime WEAK_BULL
| session OVERLAP | lock NONE | dayPL -0.21% weekDD 0.80% | news clear
| shadows open 4 pending 1 | real flat | top: TREND_PULLBACK-M15-04(+0.21), …
```

If heartbeats stop, the bot is not receiving ticks (market closed or
platform disconnected).

## 4. Confirming strategy learning

* Log lines starting `LEARNING [RETIRE/BENCH/SPAWN/ADAPT/UNBENCH] …` appear
  once per day (controlled update interval — never after individual trades).
* `~/Documents/XAUUSD_Adaptive_Bot_V4/learning_log.csv` records every
  decision with its evidence; `parameter_updates.csv` records parameter
  changes so you can check later whether they helped.
* `research_state.json` holds every strategy's stats (n, R sums, drawdown,
  per-regime results) — restart the bot and the startup log shows the
  restored counts.

## 5. Viewing shadow trades

`~/Documents/XAUUSD_Adaptive_Bot_V4/shadow_trades.csv` — one row per closed
virtual trade: strategy id, timeframe, direction, signal/entry/exit times,
entry/stop/target/exit prices, units, risk %, P/L, R multiple, MFE/MAE,
spread, regime, session, management mode and the entry reason. Opens/closes
are also logged live when debug logging is on (default).

## 6. Finding the reports

All in `~/Documents/XAUUSD_Adaptive_Bot_V4/`:

* `daily_report_YYYY-MM-DD.txt` — daily ranking snapshot + account state
* `daily_summary.csv`, `equity_history.csv` (equity + drawdown trail),
  `real_trades.csv`, `rejections.csv` (exact rejection reasons)
* **`final_report.txt` / `final_report.json`** — written automatically at
  the end of day 14: provisional winner (with the mandatory label), best
  strategy per regime, full ranking with explanations, sample-size
  confidence, what the learning system did, and the required follow-up
  testing (out-of-sample, walk-forward, ≥100 trades, longer forward demo).

## 7. Emergency stop

Create `EMERGENCY_STOP.txt` inside `~/Documents/XAUUSD_Adaptive_Bot_V4/`
(picked up within a minute; blocks all NEW entries while existing
protection keeps running), or stop the instance — open positions keep
broker-side SL/TP either way. Remove the file to resume.

## 8. News protection (honest)

Schedule-based and manual only — there is no live feed inside cTrader
Python and none is claimed. NFP is blocked automatically (first-Friday
rule). **You must maintain `fomc_events` / `cpi_events` / `speech_events`
in the config section** (search `class V4Config` in the single file);
missing dates produce a `*** NEWS DATES MISSING ***` warning at startup and
in the validator. Set `require_news_calendar = True` to fail closed (no
entries at all) when the lists are empty. Spread spikes block entries
independently, and abnormal-spread/news-volatility regimes disable all
strategy evaluation.

## 9. How it works (short version)

* **Strategy space** — 8 exact rule archetypes (trend pullback, Donchian
  breakout + volume, liquidity-sweep reversal, range fade + RSI, momentum
  continuation, FVG retest, session-open range break, supply/demand zone
  reaction) × timeframes (M5–H1) × bounded parameters × 5 exit-management
  styles (full TP / break-even / partial+runner / ATR trail / structure
  trail) × regime and session whitelists. ~26 seeds; the learning system
  spawns bounded variants of proven performers (≤3/day, population ≤40) and
  retires proven losers. Every strategy is a stored, versioned,
  reproducible configuration — the bot never rewrites its own code.
* **Shadow engine** — every active strategy runs its own virtual book on
  the same completed candles: fills at the next M1 open with spread +
  slippage, stop-first when a candle spans both exits, commission included,
  MFE/MAE tracked, and a post-stop watch that records whether the target
  was hit after a stop-out ("stop too tight" evidence).
* **Learning** — documented statistics, no AI claims: shrunk expectancy
  (k=6) − drawdown penalty − instability penalty − complexity penalty;
  UCB-style exploration for the single real position; real trades require
  ≥5 shadow trades and positive expectancy (regime-specific when there is
  enough regime evidence).
* **Risk** — evidence tiers (0.10–0.25% experimental up to 0.75% only for
  strategies with n≥20 and a strong score), reduction-only adjustments,
  and hard daily/weekly headroom math so no single trade can breach the
  1.7%/5% ceilings.

## 10. Verification (already run in this repo)

* `cd ctrader_bot && python3 -m unittest tests.test_v4 tests.test_adaptive_bot`
  — 74 tests: sizing, 0.75% cap, 1.7% combined daily lock, 5% weekly lock,
  3-loss cooldown + reset, restart-restore never less restrictive,
  persistence round-trip + corrupt-file safety, ranking (one lucky win
  cannot dominate), eligibility gates, retire/bench/spawn/adapt, shadow
  fill realism + stop-first + post-stop watch, no-look-ahead, report labels.
* Runtime smoke test against a mocked cTrader api: live-account refusal,
  EURUSD refusal, 240 simulated minutes (48 heartbeats), a real order via
  the selection path (SL+TP attached, risk 0.232% ≤ cap), one-position
  rule, broker-close reconciliation into guard+learning, emergency stop,
  and full restart recovery.
