# XAUUSD_Adaptive_Bot — cTrader Python cBot (GOLD, Skilling demo)

A native **cTrader Algo Python cBot** for **GOLD (XAUUSD)** on a **Skilling
DEMO account**. Multi-timeframe smart-money strategy — M15 macro bias, M5
decision layer, M1 entry trigger — with liquidity/zone/FVG analysis, a
transparent 0–100 setup score, strict risk limits and a CSV journal.

**Hard safety facts:**

* **Demo-only.** If the connected account is live the bot prints
  `LIVE ACCOUNT BLOCKED` and stops immediately. There is **no** live-trading
  switch anywhere in the code.
* **Gold-only.** The bot refuses to start on EURUSD or any symbol that is
  not in the gold allowlist (`GOLD`, `XAUUSD`, `XAU/USD`, …; configurable in
  `adaptive_bot/core/config.py`).
* Every order carries a **stop loss and take profit**, sized from the live
  broker volume rules (min/step/max read from the platform, always rounded
  **down**), max **0.25% risk per trade**, max **1% daily loss**, max
  **3 trades/day**, max **one open position**.
* **No profitability claim is made.** This project is for safe technical
  testing on demo. Backtest first, then demo, and judge only the journal.

---

## 1. Install into cTrader Mac — use the SINGLE FILE

> **Important:** cTrader embeds and executes **only the main Python file**
> of a cBot — it does not package sibling folders. That is why the modular
> layout crashes at runtime with `No module named 'adaptive_bot'`. The
> supported build is therefore the **self-contained single file**:
>
> **`XAUUSD_Adaptive_Bot_V3_main.py`** (everything inlined, no local
> imports, no `__file__`, standard library only).

1. Open **cTrader** → **Algo** section (the robot icon in the left rail).
2. Click **New cBot** → choose **Python** → name it
   `XAUUSD_Adaptive_Bot_V3`.
3. Open the new cBot in cTrader's code editor, **select all** the template
   code and **replace it with the entire contents of
   `ctrader_bot/XAUUSD_Adaptive_Bot_V3_main.py`** from this repository.
   (Do not touch `robot_wrapper.py` or the companion `.cs` file that
   cTrader generates — they stay as generated.)
4. Press **Build** (⌘B). No extra files, folders or Python packages are
   needed.

The cBot class inside the file is named exactly `XAUUSD_Adaptive_Bot_V3`.

*The modular tree (`XAUUSD_Adaptive_Bot.py` + `adaptive_bot/`) remains in
the repo as the readable, unit-tested source that the single file is
generated from — develop and run `tests/` against it, but paste only the
V3 single file into cTrader.*

## 2. Select Skilling demo + GOLD

1. In cTrader, log in to your **Skilling DEMO** account (the account id in
   the top-left should be marked Demo).
2. Open a **GOLD chart — symbol `XAUUSD`** (Skilling's gold symbol; the bot
   also accepts `GOLD` and `XAU/USD` aliases). Timeframe of the chart
   doesn't matter for logic (the bot pulls M1/M5/M15/H1/H4/D1 itself);
   **m1 or m5 is recommended** so backtests iterate bar-accurately.
3. Add an instance: Algo → `XAUUSD_Adaptive_Bot` → **Add Instance** →
   pick the Skilling demo account and the XAUUSD chart.

On a live account the bot will refuse to run. On any non-gold symbol it
will refuse to run.

## 3. Configure

In the single-file build, edit the `Config` dataclass **inside
`XAUUSD_Adaptive_Bot_V3_main.py`** (search for `class Config` — it is the
same block as `adaptive_bot/core/config.py`, which remains the reference
for the modular tree). All settings ship with safe defaults
(risk 0.25%/trade, 1%/day, 3 trades/day, min score 70, min RR 1.5, spread
cap 60 points, break-even/trailing/partial **off**, sessions Asia/London/NY
**on** with London+NY preferred). Edit the file and press Build again.

**News protection is manual/schedule-based** (see limitations): NFP is
auto-blocked on its first-Friday schedule; for FOMC / CPI / speeches add
this month's dates to `fomc_events` / `cpi_events` / `speech_events`
("YYYY-MM-DDTHH:MM", UTC). The bot tells you at startup if the lists are
empty.

**Timezone**: session hours are UTC. cTrader's server time is UTC for most
brokers; verify once against your Skilling demo (platform clock vs an
online UTC clock) and set `server_utc_offset_hours` if it differs.

### Optional: UI parameters

You can expose the main knobs in cTrader's parameter panel by declaring
them in the auto-generated companion `.cs` file (cTrader requires Python
cBot parameters to be declared there). Add properties like these inside
the generated class, build, and the bot picks them up automatically —
every one is optional and clamped to safe ranges in code:

```csharp
[Parameter("Min setup score", DefaultValue = 70, MinValue = 70, MaxValue = 100)]
public double MinSetupScore { get; set; }

[Parameter("Risk % per trade", DefaultValue = 0.25, MinValue = 0.01, MaxValue = 0.25)]
public double RiskPercentPerTrade { get; set; }

[Parameter("Max spread (points)", DefaultValue = 60)]
public double MaxSpreadPoints { get; set; }

[Parameter("Max trades per day", DefaultValue = 3, MinValue = 1, MaxValue = 3)]
public int MaxTradesPerDay { get; set; }

[Parameter("Min reward:risk", DefaultValue = 1.5, MinValue = 1.5)]
public double MinRewardRisk { get; set; }

[Parameter("Asia session", DefaultValue = true)]
public bool AsiaEnabled { get; set; }

[Parameter("London session", DefaultValue = true)]
public bool LondonEnabled { get; set; }

[Parameter("New York session", DefaultValue = true)]
public bool NewYorkEnabled { get; set; }

[Parameter("News protection", DefaultValue = true)]
public bool NewsProtectionEnabled { get; set; }

[Parameter("Emergency stop", DefaultValue = false)]
public bool EmergencyStop { get; set; }

[Parameter("Debug logging", DefaultValue = false)]
public bool DebugLogging { get; set; }
```

(Also supported: `NewsMinutesBefore`, `NewsMinutesAfter`, `StopBufferAtr`,
`BreakEvenEnabled`, `BreakEvenTriggerR`, `TrailingEnabled`,
`TrailingAtrMultiple`, `PartialTpEnabled`, `StrictMode`.) Note there is
deliberately **no** parameter that enables live trading or raises risk
above the hard caps.

## 4. Backtest (do this before demo)

1. Algo → `XAUUSD_Adaptive_Bot` → **Backtest** tab.
2. Symbol **XAUUSD**, chart timeframe **m1** (best trigger fidelity; m5
   works but M1 triggers then derive from platform-built m1 bars).
3. Data: **tick data** if offered (most accurate) or m1 bars.
4. Starting balance: something realistic for you, e.g. **10,000**.
5. Spread: **fixed 30–40 points** ($0.30–0.40) or historical spread if
   available — gold spread matters a lot.
6. Period: **several months** (e.g. 6), then keep the most recent 1–2
   months untouched as **out-of-sample**: first optimise/inspect on the
   older window, then run once on the unseen window and compare.
   Walk-forward by hand: repeat train-2-months → test-1-month, rolling
   forward, and check the test windows stay consistent.
7. Press play. Setups, scores, rejections and trades appear in the
   backtest **Log**; the CSV journal is also written (see below).

**Backtest assumptions (read before trusting results):**

* Decisions happen on **completed candles only**; the forming bar is
  always excluded, so there is no look-ahead bias in the logic.
* SL/TP are held broker-side and filled by cTrader's backtest engine; with
  bar data (not tick), intra-bar order of events is approximated by the
  engine — a bar that touches both SL and TP is resolved by cTrader's
  model, so prefer tick data for honesty.
* Slippage is not simulated by the bot; a 15-point buffer is priced into
  sizing and net-RR instead.
* Session times assume the backtest data clock is the same server time as
  live (UTC for most brokers).
* News blackouts apply exactly as configured — for past periods that means
  the NFP rule plus whatever dates you put in the config lists; an empty
  list means historical news is NOT avoided in the backtest.
* Missing data: the bot skips evaluation until it has enough completed
  candles (`min_candles_required`), so the first hours of a backtest are
  warm-up.

## 5. Run on demo

Start the instance (play button). Startup logs show: demo confirmation,
gold-symbol confirmation, symbol spec (tick/pip/volume rules), news
status, daily-guard state and the journal location. The bot **never trades
at startup** — the first evaluation happens on the next completed M5
candle, and an entry additionally needs an M1 trigger.

Every decision is logged: armed setups print the full score breakdown
(15m bias, zone, sweep, 5m structure, displacement, confluence,
premium/discount, session, news, target — total /100), the M1 trigger,
the full order-safety checklist on failures, and every rejection with its
reason.

## 6. Stop / emergency stop

* **Stop**: the Stop button on the instance. Open positions keep their SL
  and TP on the broker side — they remain protected with the bot off.
* **Emergency stop** (blocks all NEW entries, keeps managing/protecting):
  * create a file named `EMERGENCY_STOP.txt` in the journal folder or in
    the cBot source folder (no rebuild needed, picked up within a minute), or
  * set `emergency_stop: True` in config.py (or the `EmergencyStop` UI
    parameter if you wired it) and rebuild/restart.
* To flatten everything immediately: Stop the bot, then close the position
  manually in cTrader — one click in the Positions panel.

## 7. Logs and journal

* **cTrader log**: the Log tab of the running instance (and the backtest
  log in backtests). Everything important is printed there.
* **CSV journal**: `~/Documents/XAUUSD_Adaptive_Bot/`
  * `setups_YYYY-MM.csv` — every evaluated setup, accepted AND rejected,
    with the full score breakdown, spread, session, news status, risk %,
    volume, entry/SL/TP, RR and rejection reason.
  * `trades_YYYY-MM.csv` — every completed trade with entry/exit
    time/price, volume, P/L, R-multiple, MFE/MAE, setup score and exit
    reason.
  * If macOS sandboxing blocks writing, the journal disables itself with a
    log line and the bot keeps running on cTrader logs alone.

## 8. Known limitations

* **No live news feed.** News protection is manual/schedule-based (NFP
  first-Friday rule + date lists you maintain). The bot says so at startup
  and the setup score can never claim full news safety. This is honest by
  design — cTrader Python has no reliable built-in calendar and the bot
  makes no network calls.
* **Server-time dependence.** Sessions assume the platform clock is UTC
  (standard for cTrader brokers); verify once and set the offset if not.
* **Bar-data backtests** approximate intra-bar SL/TP ordering (use tick
  data where possible).
* **MFE/MAE** are tracked from completed M1 candles, so they are close but
  not tick-perfect.
* **Restart state**: daily loss/trade counters are rebuilt from broker
  history at startup; an open bot position is adopted and managed, and an
  adopted position without a stop loss is closed for safety.
* **Demo-only by design.** There is no supported path to live trading in
  this version.
* **No profitability claim.** Expect losing trades and losing days; the
  point of the risk caps is that they stay survivable while you evaluate
  the journal.
