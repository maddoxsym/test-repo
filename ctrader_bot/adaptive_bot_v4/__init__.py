"""
adaptive_bot_v4 — 14-day autonomous DEMO research & paper-trading system.

V4 reuses the battle-tested detectors from adaptive_bot (market structure,
liquidity, supply/demand, FVGs, regime) and adds on top:

    config_v4       V4 policy: risk caps (0.75%/trade, 1.7%/day combined,
                    5%/week), research clock, exploration settings
    features        per-timeframe feature computation (completed candles)
    strategy_space  autonomous strategy generation: 8 rule archetypes x
                    timeframes x bounded parameters, versioned + mutable
    shadow          parallel virtual portfolios (one per strategy) with
                    realistic bid/ask/spread/slippage/commission fills
    learning        statistical performance tracking, risk-adjusted
                    ranking, exploration-vs-exploitation selection,
                    controlled adaptation (no fake AI, just statistics)
    risk_v4         adaptive risk engine + V4 guard (daily 1.7% combined,
                    weekly 5%, 3-loss cooldown) + order preflight
    persistence     restart-proof JSON state + CSV research files
    reporting       daily summaries and the final 14-day report

The V3 project (XAUUSD_Adaptive_Bot / XAUUSD_Adaptive_Bot_V3_main.py) is
untouched.  Everything here is DEMO-only and GOLD-only, exactly like V3.
"""

__version__ = "4.0.0"
BOT_NAME_V4 = "XAUUSD_Adaptive_Bot_V4"
