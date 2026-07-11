"""
adaptive_bot — pure-Python strategy core of the XAUUSD_Adaptive_Bot cBot.

Every module in this package is broker-independent standard-library Python:
no cAlgo imports, no network calls, no credentials.  Only the main cBot file
(XAUUSD_Adaptive_Bot.py) touches the cTrader Algo API; it feeds candles and
account facts in, and receives decisions out.  That separation is what makes
the strategy testable outside cTrader and safe inside it.

Layout:
    core/       enums, dataclasses, config, math helpers
    strategy/   market structure, liquidity, zones, FVGs, regime,
                M1 entry trigger, scoring, the six-model strategy engine
    filters/    sessions, news blackouts, spread
    risk/       position sizing, daily loss guard, trade limits
    execution/  order safety checklist, open-position management
    journal/    CSV setup + trade journal
"""

__version__ = "2.0.0"
BOT_NAME = "XAUUSD_Adaptive_Bot"
