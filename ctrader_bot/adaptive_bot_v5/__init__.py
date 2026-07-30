"""
XAUUSD_Adaptive_Bot_V5 — multi-timeframe CONFLUENCE research system.

V5 keeps everything that worked in V4 (broker integration, demo-only and
gold-only locks, persistence, risk limits, research clock, CSV reporting,
shadow research, strategy ranking, cTrader single-file compatibility) and
replaces the part that did not: V4's strategies were largely standalone
signals (a sweep + reclaim was enough to enter).  V5 requires a *sequence*
of multi-timeframe confirmations before any setup exists at all, then scores
the remaining evidence with a transparent, correlation-aware confluence
engine and only trades setups that clear a minimum score.

Modules
-------
config_v5        every policy knob + a validator that refuses looser rails
market_state_v5  one multi-timeframe market picture per completed bar
confluence       transparent, group-capped confluence scoring
trade_plan       structural stops, structural targets, net reward:risk
setups_v5        six genuinely different confluence setup families
management       ONE management state machine shared by shadow and real
shadow_v5        virtual books with a correct bid/ask model + R invariants
learning_v5      net-R statistics per family and per variant, ranking
research_clock   ACTIVE-trading-day research clock (weekends never count)
risk_v5          guards, evidence-graduated risk, order preflight
persistence_v5   atomic JSON state + the CSV research trail
reporting_v5     daily reports, rankings, final report

Nothing here claims profitability, and nothing here is ready for live or
funded trading.  This is a demo research instrument.
"""

__version__ = "5.0.0"

BOT_NAME_V5 = "XAUUSD_Adaptive_Bot_V5"
