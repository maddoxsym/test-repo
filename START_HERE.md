# Gold Trader — shared copy

A gold (XAU/USD) trading bot: 3 strategies (trend / mean-reversion /
breakout, long+short), risk-based sizing, broker-side SL/TP, daily loss
stop. Paper/demo trading only — the bridges refuse real-money accounts.

## Run it (Python 3.10+, zero installs)

    python3 -m goldtrader backtest --days 30      # simulator
    python3 -m goldtrader paper --minutes 480     # live real gold prices, no broker
    python3 -m goldtrader campaign                # persistent day-by-day ledger

## Docs
- docs/GOLD.md              — setup incl. MT5 demo (Windows) & MetaApi (Mac)
- docs/GOLD_MARKET_STUDY.md — the market research behind the calibration
- docs/STRATEGIES.md etc.   — the older memecoin system (memetrader/), kept
                              because goldtrader reuses its engine

## Notes
- data/gold_params.json holds the tuned strategy settings.
- Credentials files (data/mt5_config.json, data/telegram_config.json,
  data/metaapi_config.json) are NOT included — create your own if you
  connect your own demo account (see docs/GOLD.md).
- Tests: python3 -m unittest discover -s tests
