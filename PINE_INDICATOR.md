# AI Confluence Engine — 10-layer TradingView indicator

`btc_ai_confluence_10layer.pine` — Pine Script **v6**, crypto/BTC oriented.
It marks the setups your strategy describes **and** grades the strategy itself with a
built-in bar-by-bar simulation, so you can see whether the rules actually work before
risking anything.

It is an **indicator**, not a `strategy()`. It never places an order and never connects
to a broker.

## Install

1. TradingView → **Pine Editor** (bottom panel) → paste the whole file.
2. **Save**, then **Add to chart**.
3. Suggested start: `BTCUSDT` (Binance) or `BTCUSD.P`, **15m or 1H**.

Three panels appear:

| Panel | Position | What it tells you |
|-------|----------|-------------------|
| **AI CONFLUENCE — LIVE** | top right | every layer's current reading, the long/short scores, the risk state, the open trade |
| **STRATEGY REPORT** | bottom right | the full performance of the rule set over your loaded history |
| **SCORE / TRADES / WIN% / AVG R** | bottom left | performance split by confidence bucket — this is the one that tells you whether your score means anything |

## The ten layers, and where each one lives

| Layer | Implemented as | Where to tune it |
|-------|----------------|------------------|
| 1 · Market regime | ADX trend/range, ATR percentile band, ATR expansion vs contraction, EMA 21/50/200 context | `① Market regime` |
| 2 · HTF bias | Daily + 4H EMA structure from the **last closed** HTF bar, PDH/PDL, PWH/PWL, proximity test | `② Higher timeframe bias` |
| 3 · Smart money | Pivot swings → BOS / CHoCH, sweeps of swing highs/lows + PDH/PDL/PWH/PWL, order blocks, FVGs with ATR-minimum size, premium/discount from the dealing range | `③ Smart money concepts` |
| 4 · Confirmation | Body/range close strength, volume vs its average, RSI + MACD histogram agreement, retest of the broken level | `④ Confirmation` |
| 5 · Confidence score | Weighted buckets normalised to 0–100; below the threshold the setup is counted but not taken | `⑤ Confidence score` |
| 6 · Risk management | Fixed-fractional risk, daily/weekly loss locks, max-drawdown halt, consecutive-loss lock, automatic de-risking after 2 and 3 losses | `⑥ Risk management` |
| 7 · Position sizing | `equity × risk% ÷ stop distance`, scaled by confidence and volatility, capped by a leverage limit | `⑦ Position sizing` |
| 8 · Dynamic exits | Partial at 1R, break-even only after the partial, ATR trail once the trade earns it, structure-flip exit, time stop | `⑧ Dynamic exits` |
| 9 · News awareness | Blackout sessions, a high-impact date list, an automatic volatility-shock pause, optional weekend off | `⑨ News / event awareness` |
| 10 · Journal | Per-trade label with the full reason, MAE/MFE, P/L, equity; report panel; Data Window series for CSV export | `⑩ Journal & performance` |

### Default score weights (they sum to 100)

Trend 20 · Liquidity sweep 15 · FVG 10 · Volume 15 · Volatility filter 10 · Momentum 15 ·
Market regime 15. Four optional buckets (structure, order block, premium/discount, retest)
default to 0.

The score is **normalised by the weights you enable**, so it always reads 0–100 no matter
what you change. A bucket that cannot be evaluated — volume on a feed that has none — is
dropped from the denominator instead of silently scoring zero.

Default threshold is **80**, as in your spec.

### Hard filters vs score

Every layer has a `HARD FILTER` toggle. A hard filter is a veto: no score can override it.
Defaults on: regime match, HTF bias, recent BOS/CHoCH, strong candle close, momentum
agreement. Defaults off: sweep, FVG/OB, premium/discount, volume, retest — these still
feed the score, they just aren't mandatory. Turn them on one at a time and watch what
happens to the trade count and the profit factor.

## Reading the report honestly

- **Expectancy (R)** and **Profit factor** matter. The equity number does not.
- **Under ~50 trades, nothing in the panel is meaningful.** Widen the history (a higher
  plan loads more bars), drop to a lower timeframe, or loosen a filter.
- The **bucket table** is the payoff of layer 5. If the 95–100 bucket does not out-earn
  the 80–85 bucket, your score is not measuring anything real and the weights need work —
  or the threshold should simply be lower with fewer filters.
- **Blocked by filters / risk / news** counters show what your rules are rejecting. A huge
  filter count with a tiny trade count means the rule set is over-constrained.
- `Sharpe (annualised)` extrapolates the per-trade Sharpe by the observed trade frequency.
  It's a rough comparison tool, not a fund-grade statistic.

## What the simulator does and does not do

**Does:** bar-by-bar management; entry at the signal bar's close plus slippage; stop,
partial, break-even, ATR trail, structure exit and time stop each checked in order; fees
charged on entry and on every exit leg; equity compounding; daily/weekly/drawdown locks
that actually stop trading; MAE/MFE tracked in R.

**Does not:** see inside a bar. When a bar touches both the stop and a target, it books
the **stop** — deliberately pessimistic. It also cannot model funding, partial fills,
order-book depth, or exchange outages, and it assumes the stop fills at your slippage
setting rather than wherever a wick actually takes you.

Set **Fee per side %** and **Slippage %** to your venue's real numbers before you believe
anything. The defaults (0.05% / 0.02%) are taker-ish for a major spot venue.

## Repainting

- Signals fire only on closed bars (`Only act on closed bars`, on by default).
- HTF bias uses the last **closed** higher-timeframe bar, so it never repaints — at the
  cost of lagging up to one HTF bar.
- Pivots confirm `Swing pivot right` bars late by definition. That's structure detection,
  not a bug, and the simulation respects the same delay.

## Alerts

Three alert conditions: **Long setup**, **Short setup**, **Trade closed**. There is also a
rich `alert()` payload (score, entry, stop, target, size) — pick *Any alert() function
call* when creating the alert to receive it.

## Exporting the journal (layer 10)

Score long/short, equity, drawdown %, ATR percentile, ADX, position, closed R and trade
count are published to the **Data Window**. Chart menu → **Export chart data** gives you a
CSV of all of them per bar, for analysis outside TradingView.

## If the panel shows HALTED

Max drawdown was breached and the engine stopped for the rest of the history — that is
the rule doing its job. Raise `Max drawdown %` if you want to see how the rest of the
period would have gone.

## Warning

No profitability is claimed or implied. Backtest numbers produced by re-optimising inputs
until the panel looks good are curve fitting, and curve fitting is the fastest known way
to lose money with a tool like this. Paper trade first, then trade small.
