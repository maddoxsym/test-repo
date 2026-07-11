# XAUUSD Adaptive Trading Bot

A fully automated, adaptive gold (XAUUSD) trading system for MetaTrader 5,
contained in **one Python file**: [`xauusd_adaptive_bot.py`](xauusd_adaptive_bot.py).

It reads higher-timeframe market structure, detects liquidity, supply/demand
zones, fair value gaps and order blocks, waits for sweep + displacement +
BOS/CHoCH/MSS confirmation, and only then trades — with structural stops,
setup-graded position sizing and hard capital-protection locks.

> **No profitability is claimed or implied.** The daily +2%/+3% figures in
> the config are risk-off throttles, not targets the bot chases. Capital
> protection overrides profit everywhere in the code.

## Modes

| Mode | What it does | Requirements |
|------|--------------|--------------|
| `BACKTEST` | Research on CSV or synthetic candles | any OS, stdlib only |
| `PAPER` *(default)* | Live analysis, simulated fills, no orders | Windows + MT5 terminal |
| `DEMO` | Real orders on an MT5 **demo** account | Windows + MT5 terminal |
| `LIVE` | Real money — **disabled by default**, quadruple-gated | Windows + MT5 + explicit opt-in |

## Quick start

```bash
# Core (backtest/tests, any OS)
pip install requests

# Full stack (paper/demo/live — Windows only; MetaTrader5 has no Linux build)
pip install MetaTrader5 numpy pandas requests

python xauusd_adaptive_bot.py --test                        # 86 built-in tests
python xauusd_adaptive_bot.py --mode BACKTEST --synthetic   # mechanics check
python xauusd_adaptive_bot.py --mode BACKTEST --data m5.csv --walk-forward
python xauusd_adaptive_bot.py --mode PAPER                  # default mode
python xauusd_adaptive_bot.py --report-day 2025-01-07
python xauusd_adaptive_bot.py --report-week 2025-W02
python xauusd_adaptive_bot.py --report-month 2025-01
python xauusd_adaptive_bot.py --emergency-close             # close everything
```

Credentials come **only** from environment variables (`MT5_LOGIN`,
`MT5_PASSWORD`, `MT5_SERVER`, optional `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`, `NEWS_API_KEY`). Nothing is hard-coded.

## Safety model (enforced in code, covered by tests)

- 5% risk per trade is a **hard ceiling** the validator refuses to raise;
  defaults are 0.5–2% by setup grade (B/A/A+), score < 70 = no trade.
- Daily −5% / weekly −10% / 3-consecutive-loss locks; +2% soft and +3% hard
  daily profit stops (measured from broker-day-reset equity).
- Stops are structural, never widened, never removed; volumes round **down**;
  minimum-lot-too-risky setups are rejected.
- No martingale, no grid, no averaging losers, no risk increase after losses.
- LIVE requires: `--mode LIVE` **and** `LIVE_TRADING_ENABLED = True` **and**
  `--i-understand-live-risk` **and** account/server matching **and** recorded
  backtest runs meeting research standards **and** the backtest/paper/demo
  verification flags you set only after reviewing each phase yourself.
- Kill switch: create a file named `KILL_SWITCH` next to the bot.

## What's inside the single file

Config + validator, structured logging, SQLite journaling (every accepted
*and* rejected setup, config audit trail, heartbeats, daily/weekly/monthly
stats), swing/structure/liquidity/zone/FVG/order-block detectors, an
11-class market-regime classifier, an adaptive timeframe selector, six entry
models with 0–100 scoring, true-monetary position sizing from live broker
symbol specs, a look-ahead-free candle backtester (conservative SL-first
intrabar assumption), walk-forward analysis, Monte Carlo trade reshuffling,
paper simulator, MT5 execution path with pre-flight checklist and restart
reconciliation, Telegram alerts, and an 86-test built-in suite
(`--test`).

## Known limitations

- Candle-based backtests can't know intrabar order of SL vs TP → the engine
  assumes **stop first** (conservative) and documents every assumption in
  its report output.
- The MetaTrader5 Python package is Windows-only; on other platforms the bot
  runs BACKTEST and tests, and refuses live-feed modes with a clear message.
- Without `NEWS_API_KEY` the news filter logs that live news protection is
  incomplete and relies on manual blackout windows + spread/volatility locks.
  It never fabricates events.
- Synthetic-data runs verify mechanics only, never profitability.

Full setup, CSV format, MT5 configuration and per-mode instructions are in
the header docstring of `xauusd_adaptive_bot.py`.
