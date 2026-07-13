# Memcoin Trader — multi-agent trading system

> **Project pivot (July 2026)**: the owner moved away from memecoin
> trading for religious reasons. Active development now targets **gold
> (XAU/USD)** — see **[docs/GOLD.md](docs/GOLD.md)** and the `goldtrader/`
> package (spot + MetaTrader 5 demo bridge, configurable leverage,
> long-only mode). The memecoin system below remains as a working
> reference implementation and is no longer actively traded.

A zero-dependency Python system in which **five cooperating AIs** research,
vet, and paper-trade Solana memecoins — every entry at low market cap,
every exit owned by a disciplined risk engine, and a self-improvement loop
that evolves the strategy parameters against a market simulator calibrated
to real pump.fun-era statistics.

> **This system never touches real money.** It has no wallet, no keys, and
> deliberately no live-execution code. Paper trade until the numbers earn
> anything more — and read the disclaimer at the bottom.

## The agents

```
                        ┌────────────────────────────────────┐
                        │            ORCHESTRATOR            │
                        └────────────────────────────────────┘
   discovery                 vetting                intelligence
┌─────────────┐   ┌────────────────────────┐   ┌─────────────────────┐
│   SCOUT     │──▶│      RUG CHECKER       │   │  NEWS AGENT         │
│ new coins,  │   │ mint/freeze authority, │   │  narrative metas    │
│ low mcap    │   │ LP lock, holder spread,│   ├─────────────────────┤
│ only        │   │ deployer bags → VETO   │   │  WHALE TRACKER      │
└─────────────┘   └────────────────────────┘   │  measured-PnL       │
                              │                │  wallets, copy sigs │
                              ▼                └─────────────────────┘
     ┌────────────────────────────────────────────────┐
     │                  STRATEGIES                    │
     │  graduation_sniper · momentum · copy_trade ·   │
     │  dip_buyer                                     │
     └────────────────────────────────────────────────┘
                              │ buy signals
                              ▼
     ┌────────────────────────────────────────────────┐
     │   RISK ENGINE — sizing, stops, TP ladder,      │
     │   moonbag trailing stop, rug exits             │
     └────────────────────────────────────────────────┘
                              ▲
     ┌────────────────────────────────────────────────┐
     │   STRATEGY LAB — evolves every parameter by    │
     │   paper-trading mutations across market seeds  │
     └────────────────────────────────────────────────┘
```

| Agent | Job |
|---|---|
| **Scout** | Finds newly launched coins (pump.fun bonding curve, DexScreener new pairs) and enforces the house rule: only low market caps enter the funnel |
| **Rug Checker** | Background check with hard veto: mint/freeze authority, LP lock/burn, holder concentration, deployer bags, socials, liquidity. Keeps re-checking open positions and forces instant exits on rug signals |
| **News Agent** | Tracks the hot narrative meta (dogs → AI → politics → …) and boosts/penalizes signals by fit |
| **Whale Tracker** | Maintains measured track records of trader wallets; flags convergent smart-money entries; powers copy trading |
| **Strategy Lab** | The AI that improves the other AIs: evolutionary search over every strategy/risk parameter, fitness = drawdown-penalized return across multiple simulated market seeds |

## Quickstart (no installs — pure stdlib, Python 3.10+)

```bash
# 1. Paper-trade 72 simulated hours of memecoin market
python3 -m memetrader backtest --hours 72 --verbose

# 2. Let the Strategy Lab evolve better parameters (writes data/best_params.json)
python3 -m memetrader evolve --generations 6 --population 10

# 3. Re-test with the evolved champion (picked up automatically)
python3 -m memetrader backtest --hours 72

# 4. LIVE paper trading on real market data (needs internet; still no real money)
python3 -m memetrader paper --minutes 120

# 5. Inspect the saved live-paper portfolio anytime
python3 -m memetrader report

# 6. Quick-flip mode (buy, take profit immediately, move on) + tiny bankroll
python3 -m memetrader backtest --scalp --bankroll 20 --hours 24

# 7. The persistent $20 campaign: one simulated trading day per run,
#    equity carries over, dated ledger written to data/pnl_log.md
python3 -m memetrader campaign
```

### Quick-flip (scalp) profile

`--scalp` switches the exit ladder to fast profit-taking: **sell 60% at
+30%, 25% at +60%, 10% at +120%**, 25% stop, 18% trailing stop, nothing
held past ~2 hours. `python3 -m memetrader evolve --scalp` evolves this
profile separately (champion: `data/best_params_scalp.json`).

Example simulator results (72h, $1,000 start — **simulator numbers do not
promise live results**):

```
final equity  $22,352   return +2135%   trades 119   win rate 89%   max DD 3.9%
  copy_trade         44 trades  avg  8.10x   +$12,149
  momentum           31 trades  avg  4.47x   +$5,985
  graduation_sniper  36 trades  avg  2.06x   +$3,426
  dip_buyer           8 trades  avg  1.31x   +$609
```

## Documentation

- **[docs/MARKET_STUDY.md](docs/MARKET_STUDY.md)** — the memecoin market
  from Dogecoin (2013) through pump.fun industrialization to 2026: the hard
  statistics, why "$50 → $50M" stories are survivorship bias, and how each
  finding became a system rule.
- **[docs/STRATEGIES.md](docs/STRATEGIES.md)** — the four strategies, the
  rug-check gate, the exit ladder, and the evolution loop, with every
  tunable explained.
- **[docs/PLATFORMS.md](docs/PLATFORMS.md)** — what to use alongside
  Phantom (GMGN for copy trading, Axiom/Photon for terminals, RugCheck for
  safety), wallet hygiene, and how to pick wallets worth copying.

## Project layout

```
memetrader/
  config.py            every tunable parameter (evolvable)
  models.py            shared dataclasses
  datafeed/
    simulator.py       offline market calibrated to real memecoin statistics
    live.py            DexScreener + pump.fun + RugCheck clients (keyless)
  agents/              scout, rug_checker, news_agent, whale_tracker, strategy_lab
  strategies/          graduation_sniper, momentum, copy_trade, dip_buyer
  engine/              portfolio, risk (exits/sizing), paper_broker (slippage), orchestrator
  main.py              CLI
data/
  best_params.json     Strategy Lab champion (auto-loaded)
  watch_wallets.json   wallets to copy (seed from GMGN/Kolscan leaderboards)
```

## Disclaimer

Memecoins are the highest-risk corner of crypto: most tokens are scams,
most traders lose money, and nothing in this repository changes those base
rates. This code is a research and paper-trading tool, not financial
advice and not an invitation to deploy capital. Simulator performance
(however good) does not predict live performance. If you ever trade real
money, use a separate small wallet you can afford to lose entirely.
