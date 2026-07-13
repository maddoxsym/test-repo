# Strategy Playbook

Four entry strategies + one risk engine that owns every exit. All entries
are LOW MARKET CAP by hard rule (`max_mcap`, default $400k) — the asymmetry
that makes memecoins worth trading only exists at small caps.

Every parameter below is evolvable by the Strategy Lab
(`python3 -m memetrader evolve`); defaults live in `memetrader/config.py`
and the current champion in `data/best_params.json`.

---

## 0. The gate: Rug Checker (not a strategy — a veto)

Runs before anything else. Score 0–100; below `min_safety_score` (default
60) the token is untouchable regardless of how good the setup looks.

| Check | Penalty | Why |
|---|---|---|
| Mint authority not revoked | −35 | Dev can print supply onto your bid |
| Freeze authority not revoked | −20 | Dev can freeze you out of selling |
| LP not locked/burned (post-graduation) | −35 | Classic liquidity pull |
| Top-10 wallets > 45% / > 30% | −30 / −15 | You are the exit liquidity |
| Deployer holds > 10% / > 5% | −25 / −10 | Guaranteed sell pressure or worse |
| No socials | −10 | Zero community intent |
| Thin liquidity | −15 | Can't exit even if right |
| Stagnant holder count | −10 | Nobody is coming |

While a position is OPEN the checker keeps re-scoring it; a collapse below
25 (or liquidity draining away) triggers an immediate **rug exit** at any
price.

## 1. Graduation Sniper — the bread and butter

- **Thesis**: pump.fun's bonding curve is a free demand-proof: ~99% of
  launches never complete it. Buying at 85–100% completion (~$60–69k mcap)
  enters after the survival filter but before DEX discovery flow.
- **Entry**: bonding ≥ `grad_min_bonding` (0.85) or freshly graduated;
  safety pass; holders ≥ `grad_min_holders`; top10 ≤ `grad_max_top10_pct`.
- **Character**: high frequency, modest multiples (avg ~2x), the book's
  steady compounder.

## 2. Momentum Breakout — ride reflexivity

- **Thesis**: memecoin legs are self-reinforcing; a 5-minute volume spike
  ≥ `mom_vol_spike`× the rolling hourly baseline, with a growing holder
  base, catches leg one while mcap is still low.
- **Entry**: volume spike + holders ≥ `mom_min_holders` + safety pass +
  not already −25% off its high (spike ≠ dump).
- **Character**: medium frequency, fat right tail (avg 4–6x in testing).

## 3. Copy Trade — follow measured smart money

- **Thesis**: a persistent minority of wallets is early on winners. We copy
  **entries only**, from wallets with a *measured* track record
  (`copyworthy`: ≥10 trades, ≥45% win rate, ≥1.5x avg) — never from clout.
- **Entry**: ≥ `copy_min_smart_buys` distinct tracked wallets bought within
  30 minutes; safety pass (smart wallets punt on rugs too — the gate still
  applies); mcap ≤ `copy_max_mcap`.
- **Exits are OURS**: whales exit OTC or diamond-hand past our risk budget.
- **Live setup**: seed `data/watch_wallets.json` from GMGN/Kolscan
  leaderboards; with `HELIUS_API_KEY` the tracker follows their swaps.
- **Character**: lower frequency, the biggest winners in testing.

## 4. Survivor Dip Buyer — the other side of the lifecycle

- **Thesis**: a token >48h old holding >$300k mcap has left the 98% death
  zone. Survivors retrace 30–65% between legs; that dip is the highest
  hit-rate, lowest-multiple trade available.
- **Entry**: age ≥ `dip_min_age_minutes`; drawdown from high inside
  [`dip_drawdown_low`, `dip_drawdown_high`]; holders still growing; safety
  pass.
- **Character**: low frequency, ~1.1–1.3x average — a stabilizer, not a
  moon vehicle.

---

## The Risk Engine (owns all exits)

Checked in strict priority order every tick, for every open position:

1. **Rug exit** — safety collapse or liquidity drain → dump everything now.
2. **Hard stop** — price ≤ entry × (1 − `stop_loss`) → dump.
3. **Take-profit ladder** — sell 50% of the position at 2x, 25% at 4x,
   15% at 10x (defaults). Recoups principal early; a 2x rung alone returns
   your capital plus profit.
4. **Moonbag trailing stop** — the last ~10% rides until price falls
   `trailing_stop` (35%) off its high-water mark. This is how a WIF-style
   500x stays in the book without round-tripping.
5. **Time stop** — anything still < 1.15x after `max_hold_minutes` is dead
   money; free the slot.

**Sizing**: `risk_per_trade` (2%) of equity scaled by signal confidence,
capped by `max_position_usd` and by 50% of cash. Never add to losers.
**Diversification**: `max_concurrent_positions` overall AND
`max_per_strategy` so no single strategy hogs the book.

## The Strategy Lab (self-improvement loop)

`agents/strategy_lab.py` runs an evolutionary search: a population of
mutated parameter sets each paper-trades full epochs on the simulator
across multiple random seeds. Fitness = return **penalized 1.5× by max
drawdown**, and any candidate with <5 trades is discarded (no
overfit-by-inactivity). Elites breed via crossover; the champion is saved
to `data/best_params.json` and used automatically by every other mode.

Re-run it regularly; the market's parameters drift and so should yours:

```bash
python3 -m memetrader evolve --generations 8 --population 12 --hours 48
```
