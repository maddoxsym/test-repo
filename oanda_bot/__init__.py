"""
OANDA adaptive multi-assistant trading bot for XAU_USD and EUR_USD.

Layers:
    config      -- all tunable settings and credentials handling
    oanda       -- thin REST client for the OANDA v20 API
    indicators  -- pure-python technical indicators
    regime      -- MarketRegimeAnalyst (trending / ranging / volatile)
    strategies  -- the four strategy analysts that propose trades
    news        -- NewsSentry: real-time economic-calendar guard
    council     -- TradeCouncil: assistants vote, trades need confirmation
    risk        -- RiskManager: position sizing, daily limits, vetoes
    journal     -- sqlite trade journal (every decision is recorded)
    learning    -- PerformanceCoach: learns from closed trades, reweights
                   strategies and discovers profitable strategy combos
    engine      -- live trading loop against the OANDA API
    backtester  -- offline replay of the same pipeline on historic candles
"""

__version__ = "1.0.0"
