# XAUUSD Adaptive Bot — cTrader Python cBot

This repository contains **XAUUSD_Adaptive_Bot**, a native cTrader Algo
**Python** cBot that trades **GOLD only** on a **demo account only**
(built for Skilling on cTrader Mac).

Everything lives in [`ctrader_bot/`](ctrader_bot/):

* [`ctrader_bot/README.md`](ctrader_bot/README.md) — install into cTrader
  Mac, build, configure, backtest, run on demo, emergency stop, logs and
  CSV journal, known limitations.
* [`ctrader_bot/CONVERSION_REPORT.md`](ctrader_bot/CONVERSION_REPORT.md) —
  what was converted from the previous MT5/OANDA versions, what changed,
  what could not be reproduced exactly, remaining risks, and the pre-demo
  test checklist.
* `ctrader_bot/XAUUSD_Adaptive_Bot.py` — the main cBot file (the only file
  that touches the cTrader API).
* `ctrader_bot/adaptive_bot/` — the pure-Python strategy core (market
  structure, liquidity, supply/demand, FVGs, regime, M1 entry trigger,
  scoring, risk, filters, execution safety, journal).
* `ctrader_bot/tests/` — offline test suite
  (`python3 -m unittest discover tests` from `ctrader_bot/`).

The previous OANDA and MetaTrader 5 implementations have been removed; see
the conversion report. **Demo-only, gold-only; no profitability is claimed.**
