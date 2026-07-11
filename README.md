# OANDA Adaptive Trading Bot — XAU/USD + EUR/USD

A fully automated, self-adjusting trading bot for **gold (XAU_USD)** and
**EUR_USD** on the **OANDA v20 API**, built around a *council of
assistants* that must independently confirm every trade, a *real-time news
guard*, and a *learning engine* that studies its own trade journal,
punishes losing strategies, and discovers which strategy combinations
actually make money.

> The older single-file MetaTrader 5 version of this project lives in
> `xauusd_adaptive_bot.py`. Everything below is about the new OANDA bot in
> `oanda_bot/`.

---

## ⚠️ Read this first: honest expectations

**A 5–15% daily return target is not achievable and no honest software
will promise it.** 5% a day compounds to over 40,000,000% a year; the best
hedge funds in history average 20–40% *a year*. Anyone selling a bot that
"makes 5–15% daily" is selling a fantasy, and chasing that number is how
accounts die — it forces oversized positions, and one bad day wipes out
weeks.

What this bot actually does with that goal:

* `daily_profit_target_pct` (default **5%**) is a **stop**, not a promise —
  on the rare great day the bot hits +5%, it banks the win and stops
  opening trades until tomorrow.
* `daily_loss_limit_pct` (default **3%**) ends the day before a bad day
  becomes a disaster.
* Risk per trade is small (default **0.5%** of balance) so the learning
  engine gets hundreds of trades of experience without any single mistake
  mattering much.

Realistic aspiration for a good system: single-digit percent **per month**
with drawdowns along the way. Judge this bot on its journal after weeks of
**practice-account** trading, never on hope. Trading leveraged FX/metals
carries a real risk of losing your entire deposit. Nothing here is
financial advice.

---

## The council of assistants

No trade is ever forced. Every entry must survive all of these assistants,
each running on every decision cycle:

**Voting assistants** (strategy analysts — at least **2** must agree on
the same direction, and their combined weighted score must clear an
adaptive threshold):

| Analyst | Looks for | Prefers |
|---|---|---|
| `trend_rider` | EMA20/50 alignment + ADX strength + H1 agreement | trending markets |
| `range_fader` | Bollinger-band extremes + RSI reversal | ranging markets |
| `breakout_hunter` | Donchian-channel breaks with range expansion | trends & volatility |
| `momentum_surfer` | MACD histogram flips with RSI alignment | trending markets |

**Veto assistants** (any one of them can kill a trade):

| Assistant | Kills the trade when |
|---|---|
| Market Regime Analyst | classifies the H1 market; off-regime votes count only half |
| **News Sentry** | a high-impact EUR/USD economic event is within 45 min ahead / 20 min behind (live Forex Factory calendar, refreshed every 10 min; if the feed dies it **fails closed** — no blind trading through news) |
| Session Analyst | dead hours, daily rollover spread spike, late Friday (weekend gap risk), weekends |
| Spread Watcher | the spread would eat >35% of the ATR |
| Risk Manager | daily loss limit / profit target hit, max trades reached, position can't be sized safely |
| Performance Coach | this exact strategy combo has a **proven losing record** in this regime |

## How it learns from its mistakes

Every trade is journaled to sqlite with full context: which analysts
confirmed it, the regime, the score, and eventually the outcome in R
(profit measured in units of risk). After every close:

1. **Strategy reweighting** — every confirming analyst's vote weight is
   multiplied by `exp(0.06 × R)`. Winners earn influence, losers lose it
   (clamped 0.2–3.0 so nobody is silenced forever).
2. **Combo discovery** — the exact combination
   (e.g. `breakout_hunter+trend_rider @ trending_up`) gets its own track
   record. With ≥10 trades of history: proven losers are **vetoed** (the
   bot stops repeating that mistake), proven winners get a **relaxed entry
   threshold** (the bot leans into what works). This is how it gradually
   builds its own strategy out of the pieces.
3. **Adaptive selectivity** — after a losing streak or a daily-loss stop,
   the required score rises (pickier); sustained positive expectancy lets
   it drift back down.

Learning state persists across restarts, and you can warm-start it from a
backtest (see below).

## Setup

### 1. Get OANDA API credentials (~5 minutes)

1. Create a **practice (demo) account** at [oanda.com](https://www.oanda.com).
2. Log in → **My Account** → **Manage API Access** → **Generate** a
   personal access token. Copy it immediately.
3. Your account ID is shown on **My Account** (format `101-004-1234567-001`).

### 2. Install and configure

```bash
git clone <this repo> && cd <this repo>
pip install -r requirements.txt          # just `requests`
cp .env.example .env                     # then edit .env with your token/ID
```

`.env` is git-ignored; never commit or share your token. Keep
`OANDA_ENV=practice` until the journal has proven the bot over weeks.

### 3. Verify everything offline (no account needed)

```bash
python -m unittest discover tests -v     # test suite
python backtest.py --synthetic           # full-pipeline mechanics check
```

### 4. Backtest on real data and warm-start the learning

```bash
python backtest.py --oanda --instrument XAU_USD --count 5000
python backtest.py --oanda --instrument EUR_USD --count 5000 --learn-db bot_data.sqlite3
```

### 5. Run it

```bash
python run_bot.py --paper    # identical pipeline, simulated fills — start here
python run_bot.py            # real orders on your PRACTICE account
```

The console and `logs/bot.log` show every council meeting: each analyst's
vote and reasoning, every veto, and every trade with its full rationale.

**Stopping safely:** `Ctrl+C`, or create a file named `KILL_SWITCH` next
to `run_bot.py`. Open positions always have a stop loss and take profit
attached **on OANDA's servers**, so they stay protected even if the bot or
your machine dies.

### Going live (please don't rush this)

The bot **refuses** to trade real money until *both*:
1. `OANDA_ENV=live` in `.env`, **and**
2. you edit `RiskConfig.allow_live = True` in `oanda_bot/config.py`.

Do that only after weeks of profitable, drawdown-tolerable practice
results in the journal — and start with money you can lose entirely.

## Tuning

All knobs live in `oanda_bot/config.py` as documented dataclasses:
risk per trade, daily limits, stop/target ATR multiples, news window,
session hours, council threshold, learning rates. Sensible defaults are
already set.

## Architecture

```
oanda_bot/
├── config.py       all settings + credentials handling
├── oanda.py        OANDA v20 REST client (practice & live hosts)
├── indicators.py   pure-python EMA/RSI/ATR/MACD/Bollinger/ADX/Donchian
├── regime.py       Market Regime Analyst (trending/ranging/volatile)
├── strategies.py   the 4 voting strategy analysts
├── news.py         News Sentry (live economic calendar guard)
├── council.py      Trade Council + Session Analyst + Spread Watcher
├── risk.py         Risk Manager (sizing, daily limits, breakeven stops)
├── journal.py      sqlite trade journal (the bot's memory)
├── learning.py     Performance Coach (weights, combos, selectivity)
├── engine.py       live trading loop
└── backtester.py   replays the identical pipeline on historic candles
run_bot.py          start live/paper trading
backtest.py         backtests + learning warm-start
tests/              offline test suite
```

## Disclaimer

This software is provided for education and research. Trading foreign
exchange and CFDs on margin carries a high level of risk and can result in
the loss of all your funds. Past performance — live, paper, or backtested —
does not indicate future results. You are solely responsible for any use
of this software. This is not financial advice.
