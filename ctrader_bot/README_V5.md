# XAUUSD_Adaptive_Bot_V5 — multi-timeframe confluence research bot

V5 replaces V4's standalone-signal entries with **mandatory multi-timeframe
sequences** plus a **correlation-aware confluence score**. Six genuinely
different setup families are researched over **30 active trading days**, and
the final ranking reports which *family* works — not which parameter tweak
got lucky.

V3 and V4 are untouched and still in this repo. V5 is a separate project.

**Absolute rails (validator-enforced; no live switch exists anywhere):**
DEMO-only, GOLD-only, one real position, broker-side SL+TP on every order,
stops never widen, **0.25%** max risk/trade graduated slowly from 0.10%,
**4** real trades/day, 1.7% combined daily loss lock, 5% weekly drawdown
lock, cooldown after 3 consecutive losses, volume rounded down, minimum
volatility-based stop distance, no martingale/grid/averaging/recovery sizing,
and restarting never resets the guards or the research clock.
**No profitability is claimed. This is a demo research instrument.**

---

## 1. Build on cTrader for macOS

1. cTrader → **Algo** → **New cBot** → **Python** → name it exactly
   `XAUUSD_Adaptive_Bot_V5`.
2. Open it in the editor, select all, delete, and **paste the entire contents
   of `ctrader_bot/XAUUSD_Adaptive_Bot_V5_main.py`**.
3. **Build** (⌘B).
4. Attach to your **Skilling DEMO** account on the **XAUUSD M5 chart** and
   start it.

### Generated wrapper files — leave them alone

cTrader creates and owns these; do not edit or delete them:

| File | Owner | What to do |
|---|---|---|
| `robot_wrapper.py` | cTrader | never edit — it provides the `api` object |
| `XAUUSD_Adaptive_Bot_V5.cs` | cTrader | never edit — the C# bridge |
| `*.csproj`, `obj/`, `bin/` | cTrader | never edit |

Only the main Python file is yours. cTrader embeds **only** that file, which
is why V5 ships as one self-contained 9,400-line file with no folders, no
package imports, no `__file__`, no `sys.path` and no third-party packages.

### Rebuilding the single file after editing the modular source

```bash
cd ctrader_bot
python3 build_v5_single_file.py     # regenerates XAUUSD_Adaptive_Bot_V5_main.py
```

The build script refuses to write a file that still references local imports,
`__file__`, `sys.path` or the network, so the artefact cannot silently stop
being self-contained.

### One thing to verify once

Compare the cTrader platform clock against an online UTC clock. If your
server is not UTC, set `server_utc_offset_hours` in `V5Config`. Every candle
time, session window and news window flows through that single conversion.

---

## 2. Restarting, and computer sleep

Just stop/start the instance, or restart cTrader or the Mac. On start the bot:

* restores the research clock (`research clock restored: day X/30 …`) — the
  **earliest** known start always wins, so the clock can never be reset;
* restores the strategy population, all per-variant and per-family statistics,
  and the virtual book equities;
* replays today's closed V5 positions from **broker history** into the daily
  guard, grouping by position id so a partially closed trade counts once;
* merges the persisted guard state **most-restrictive-wins**, so a restart can
  never bypass a daily/weekly lock or serve a loss cooldown early;
* resumes tracking an open position with its full management state
  (`resumed tracking of real position … at stop stage BREAKEVEN`);
* closes any adopted position that has **no stop loss**, for safety.

Open *shadow* trades are deliberately not resumed — their bar-by-bar
management cannot be verified across a gap. They are written to
`shadow_trades.csv` as `discarded` and never fed to the learning system.

A corrupt `research_state.json` is renamed to `.corrupt` (never deleted) and
the bot starts fresh rather than acting on unreadable data. State from a
different schema version is preserved and ignored rather than half-loaded.

---

## 3. The heartbeat

At least every 5 minutes, in the instance **Log**:

```
HEARTBEAT day 3/30 active trading days (2 complete, 118 min observed today,
threshold 60) | 14:35 UTC | session LONDON | regime WEAK_BULL | bias
LONG/STRONG | spread 32pt | news clear | lock NONE | day -0.21% weekDD 0.80%
| shadows open 4 pending 1 | real LONG 6u @ 3312.40 SL 3310.10 | stop stage
BREAKEVEN | top: TREND_CONTINUATION-A(14t +0.22R LOW), …
```

Every field the spec asks for is there: research day, session, regime,
higher-timeframe bias, spread, news status, open shadow trades, pending
shadow setups, real position status, current stop-management stage, and the
top strategies with completed-trade counts, net expectancy and a confidence
band. The same rows go to `heartbeat.csv`.

If heartbeats stop, the bot is not receiving ticks (market closed or platform
disconnected).

---

## 4. Reading the research output

Everything lands in `~/Documents/XAUUSD_Adaptive_Bot_V5/`:

| File | What it holds |
|---|---|
| `research_state.json` | the whole restartable state (atomic writes) |
| `shadow_trades.csv` | every virtual trade, ~50 columns (see below) |
| `real_trades.csv` | every real DEMO trade, same columns plus broker P/L |
| `rejected_setups.csv` | every rejection with its stage, reason and the last gates |
| `strategy_rankings.csv` | per-variant ranking snapshot, rewritten daily |
| `family_rankings.csv` | **per-family** ranking — the real question |
| `daily_summary.csv` | one row per day incl. active minutes and lock events |
| `equity_history.csv` | equity, floating, daily % and weekly drawdown trail |
| `learning_log.csv` | every RETIRE / BENCH / UNBENCH / SPAWN / ADAPT decision |
| `parameter_updates.csv` | parameter changes with the evidence that caused them |
| `management_events.csv` | every partial, stop move and close, shadow and real |
| `suspect_trades.csv` | trades that failed an accounting invariant (excluded from ranking) |
| `stop_watch.csv` | stopped-out trades whose target was reached anyway |
| `heartbeat.csv` | the heartbeat trail |
| `daily_report_YYYY-MM-DD.txt` | daily ranking + learning decisions |
| `final_report.txt` / `.json` | the final report, written at the end of day 30 |

Each shadow/real row stores: setup family, strategy version, direction, entry,
initial stop, TP1, main target, confluence score, every confirmation that
passed or failed, initial risk, the partial-close result, breakeven activation
and its exact reason, every trailing-stop update, the early-exit reason, MFE,
MAE, gross R, net R after costs, market regime, session and higher-timeframe
bias.

The final report labels the best family
**"PROVISIONAL WINNER — NOT AUTOMATICALLY READY FOR LIVE TRADING."**, states
its sample size and confidence band, and lists the follow-up testing that
would be required before the result meant anything.

---

## 5. Emergency stop

Create `EMERGENCY_STOP.txt` in `~/Documents/XAUUSD_Adaptive_Bot_V5/`. It is
picked up on the next completed M5 bar: it closes the open position and blocks
all new entries. Delete the file to resume. Stopping the instance also works —
open positions keep their broker-side SL/TP either way.

---

## 6. News protection — honest description

Schedule-based and **manual only. There is no live news feed in this bot and
none is claimed.**

* NFP is blocked automatically by the first-Friday rule.
* **You must maintain `fomc_events` / `cpi_events` / `speech_events`** in the
  config (search `class V5Config` in the single file), as
  `"YYYY-MM-DDTHH:MM"` UTC strings.
* Missing dates produce a `*** NEWS DATES MISSING ***` warning at startup.
* Set `require_news_calendar = True` to **fail closed** (no entries at all)
  when the lists are empty.
* Abnormal spread blocks entries independently, and the
  `ABNORMAL_SPREAD` / `NEWS_VOLATILITY` regimes disable all setup evaluation.

---

## 7. How V5 works

### The six setup families

Each has a **mandatory sequence**. If a step is missing the setup does not
exist, and the exact reason is logged. Only then is the remaining evidence
scored.

1. **LIQUIDITY_REVERSAL** — H1/M30 context or decisive premium/discount →
   important pool swept (previous day, Asia/London/session high-low, equal
   highs-lows, confirmed swing) and reclaimed → M15/M5 CHoCH or MSS against
   the short-term move → displacement → retest of the FVG / order block /
   zone that displacement created → confirming close.
2. **TREND_CONTINUATION** — clear H1+M30 trend → M15 agrees → pullback into a
   logical area (zone / order block / FVG / discount-premium, or the EMA band
   **only when structure supports it**) → M5 BOS or continuation displacement
   → controlled retest, not a chase → enough room to opposing liquidity.
3. **SESSION_LIQUIDITY** — a *previous* session's or previous day's pool swept
   inside an active session → displacement → CHoCH/MSS → FVG or order-block
   retest (required) → room to the next real liquidity target.
4. **BREAKOUT_RETEST** — genuine compression → displacement break that
   **closes** beyond structure → did not fail back inside the range → retest
   holds with a confirming close → HTF not strongly against → not a late entry.
5. **HTF_ZONE_REVERSAL** — price inside a fresh H1/M30 zone → location agrees
   → M5 CHoCH/MSS with displacement out of the zone → HTF not against → room.
6. **FVG_CONTINUATION** — HTF bias → prior M5 BOS (continuation, not a blind
   gap fill) → unmitigated displacement FVG → price returns into it →
   rejection close → room.

**M1 is never a setup source.** No family reads M1; it only times the fill.

There are 15 seeded variants across the six families — small, meaningful
parameter differences, not 21 near-duplicates. The learning system may spawn
up to 2 bounded variants a day (population ≤ 24), and rankings always report
the family alongside the variant.

### The confluence engine

Factors are grouped and each **group is capped**, so correlated confirmations
cannot stack:

| Group | Raw | Cap | Factors |
|---|---|---|---|
| HTF_DIRECTION | 22 | 20 | H1 aligned, M30 aligned |
| LTF_STRUCTURE | 33 | 25 | M15 trend, M5 CHoCH/MSS, displacement, **EMA stack** |
| LOCATION | 32 | 22 | premium/discount, S/D zone, order block, FVG |
| LIQUIDITY | 22 | 20 | important sweep, room to opposing liquidity |
| CONTEXT | 15 | 13 | active session, spread, momentum/volume |

The caps sum to 100, so the score reads as a percentage of available evidence.
The **EMA sits inside LTF_STRUCTURE on purpose**: "price above its EMAs" and
"structure is bullish" are largely the same observation, so they compete for
one capped budget instead of double-counting. Global floor 58; per-family
thresholds 60–66. Every factor's pass/fail and its evidence is logged and
stored in the CSV.

### Stops and targets

The stop goes beyond the **primary invalidation the family declares** (swept
extreme, protected swing, zone or order-block edge) plus an ATR buffer and a
spread buffer, then is **pushed further out — never pulled in** to satisfy a
minimum distance of `0.90 × M5 ATR` (floor 90 points), so GOLD M5 stops are
never unrealistically tight. Wider invalidations are recorded but not used, so
an M5 pullback entry is not tied to an M15 swing tens of dollars away. A stop
beyond `4.5 × ATR` rejects the setup instead of being trimmed.

Targets are **structural**: opposing liquidity, previous day/session extremes,
confirmed swings, supply/demand zones, higher-timeframe imbalance. Nothing is
forced to 2.5R. A target beyond a blocking higher-timeframe zone is clipped or
the setup is rejected. Both the main target (`≥ 2.0R net`) and the
**blended** reward:risk after the TP1 partial (`≥ 1.55R`) must clear their
floors — so a "2.5R target" cannot quietly become 1.4R once 45% is banked.

### Trade management — one implementation, shared by shadow and real

`management.py` is the only management logic in the system. The shadow engine
applies its actions to a virtual book; the cBot translates the same actions
into `ClosePosition` / `ModifyPosition`. They cannot drift apart. Management
runs **once per completed M5 bar**, never per tick.

* **Partial** — 45% (configurable 30–55%) when a bar *closes* beyond TP1.
  Volume is rounded down to the broker step; if the slice or the remainder
  would fall below the broker minimum, the position is managed whole and the
  reason is recorded rather than emitting an invalid order.
* **Breakeven** — `r ≥ 1.0R` is only a floor. A move also needs
  **justification**: TP1 banked, a close beyond a named structure level, a new
  protected swing formed beyond entry since the trade opened, or continuation
  displacement. The exact justification is logged. `be_require_justification`
  cannot be turned off — the validator refuses.
* **Trailing** — only in a genuinely strong trend (nine-factor reading; H1+M30
  alignment and ≥2 recent BOS are mandatory), only at/after 1.5R, only when a
  **newly confirmed** protected swing appears, only on a bar close, only ever
  tightening. Large runners (≥3R) switch to M15 swings. Strong trends are
  allowed to run past any fixed R because the target came from structure.
* **Early exit** — scored structural evidence: opposite M5 CHoCH/MSS with
  displacement (3), protected swing lost on a close (2), M15 flip (2), strong
  rejection from an opposing HTF zone (2), momentum collapse after failing the
  target (1), breakout back inside its range (3). Threshold 3, so one noisy
  candle scores nothing. Below score 5 with the H1 trend still valid it
  reduces and tightens; at or above it closes the remainder. The evidence is
  logged verbatim.

### Learning — ordinary statistics, no AI claims

Net-R statistics per variant *and* per family. Ranking is a **lower confidence
bound**: shrunk expectancy (k=8) − drawdown penalty − instability penalty −
complexity penalty − `z·σ/√n`. That last term is what stops a single lucky
outcome from topping the table (unit-tested: one +7R trade cannot outrank
fourteen +0.45R trades). Real trades need ≥5 variant trades **and** ≥8
family trades **and** positive shrunk expectancy, with a per-regime veto.
Risk graduates 0.10% → 0.15% → 0.20% → 0.25% on completed-trade counts, and
the top two tiers additionally require *real* trades, not just shadow ones.
Selection is UCB-style so under-tested strategies keep getting opportunities.
Daily (never per-trade) the system retires, benches, unbenches, adapts one
evidence-named parameter within published bounds, and spawns bounded variants.
Nothing rewrites source code.

### The research clock

Counts **active trading days**: a date counts only once 60 observed minutes of
open market have accrued. Weekends, holidays, outages and computer sleep cost
no research days. Default 30 days, configurable.

---

## 8. What was wrong in V4, and what changed

| V4 defect (reproduced) | V5 fix |
|---|---|
| A trailed stop exit was labelled `STOP_LOSS`, giving **`STOP_LOSS` with +2.08R** | Labels come from what the stop actually was: `STOP_LOSS`, `STOP_AFTER_PARTIAL`, `BREAKEVEN_STOP`, `TRAIL_STOP` |
| R used planned risk money while MFE used the actual fill distance, so **R could exceed MFE** | One denominator: `risk_money = units × risk_dist`; MFE/MAE use the same `risk_dist` |
| Longs paid a spread on the target, shorts did not | Every level is a BID level; triggers are pure bid comparisons; a long buys the ask at entry, a short buys the ask back at exit. Long and short now return **identical R** on identical setups |
| Pending setups never expired: one filled 3 days later and recorded a **`TAKE_PROFIT` worth −11.58R** | Fills refused when the signal is stale (>12 min), the open gapped (>0.6 ATR), the fill is already past the stop or TP1, or the reward:risk no longer holds |
| Partials used fractional units no broker accepts | Broker `volume_step`/`volume_min` rounding in the shadow too; if a split is impossible the position is managed whole |
| Breakeven fired on a bare 1R touch | Justification required, and logged |
| Trailing ran every candle at 1R regardless of trend | Nine-factor strong-trend gate, new-protected-swing requirement, bar-close only, tighten-only |
| No early exit at all | Scored structural early exit with a partial-or-full policy |
| Calendar research days — weekends burned days | Active-trading-day clock |
| Settlement read only the first history row, losing partial P/L | Every history row for the position id is summed |

The three headline defects were **reproduced against V4's own engine** before
V5 was written, and V5 now has explicit accounting invariants that flag any
such trade as `suspect`, write it to `suspect_trades.csv`, and **exclude it
from ranking** — an unexplainable result must never rank a strategy.

---

## 9. Tests

```bash
cd ctrader_bot
python3 -m unittest tests.test_v5 tests.test_v5_integration \
                    tests.test_v4 tests.test_adaptive_bot
```

**247 tests, all passing** (144 V5 unit + 29 V5 integration + 74 V3/V4
regression). The integration tests import and drive
`XAUUSD_Adaptive_Bot_V5_main.py` — the file you paste into cTrader — against a
mocked cTrader API, so what is verified is the deployable artefact.

Every case the spec listed is covered: long/short stop-loss and take-profit
execution, partial closing, breakeven activation, breakeven *not* activating
too early, trailing in strong trends, trailing *not* activating in ranges,
trailing never widening, early exit after confirmed opposite structure, no
early exit from one noisy candle, MFE/MAE correctness, gross and net R,
spread and slippage symmetry, minimum stop distance, volume rounding,
restart/state restoration, weekends not counting as research days, one real
position maximum, and the daily and weekly loss locks.

---

## 10. Honest limitations

* **No live news feed.** Schedule-based and manual only.
* Shadow fills are simulated at the next M1 open with spread, a slippage
  allowance and commission. Real slippage can differ, especially around news.
* Demo execution is not live execution: no real queue position, no real
  rejections, no funding effects.
* Parameters adapt during the run, so the results are **in-sample by
  construction**. The final report says so.
* 30 active trading days on one instrument is a research observation, not
  evidence. The final report requires out-of-sample and walk-forward testing
  and ≥100 trades per family before any conclusion.

**Nothing here is ready for live or funded trading, and no profitability is
claimed.**
