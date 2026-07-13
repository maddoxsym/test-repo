# Gold Market Study — since free trading opened (1971 → July 2026)

The research behind the gold bot's calibration and strategies. Gold has
no bonding curves and no rug pulls; its game is macro flows, sessions,
and volatility regimes.

## 1. Price history since gold "opened" (free floating)

| era | what happened | level |
|---|---|---|
| **Aug 1971** | Nixon ends dollar-gold convertibility — free trading begins | ~$43 by end-1971 |
| **Jan 1980** | Inflation + Iran crisis + Afghanistan: first mania peak | **$850** intraday (≈$3,200 in today's dollars) |
| 1980-1999 | 20-year bear/flat: gold loses to stocks all the way to ~$253 (1999) | the "lost decades" |
| **2011** | Post-GFC + European debt crisis peak | **$1,921** |
| 2013-2018 | Correction and long base ~$1,050-1,350 | |
| **Aug 2020** | Pandemic money-printing: first-ever $2,000+ | **$2,074** |
| 2024 | Central-bank buying + rate-cut cycle: **+26%** | ~$2,600s |
| 2025 | Historic melt-up: **+65%** in one year | $4,000+ |
| **Jan 28, 2026** | All-time high — climax of the 2024-26 run | **~$5,590** |
| Feb-Jul 2026 | Sharp correction, realized vol spikes **>50%** | **~$4,100** now |

## 2. What this history teaches a trading bot

1. **Gold trends HARD, then ranges for years.** The 2024-26 melt-up and
   the current correction are classic: strong macro trends punctuated by
   violent countermoves. Hence a trend strategy AND a mean-reversion
   strategy, gated so they don't fight each other.
2. **Current regime = elevated volatility.** 2026 realized vol ran above
   50% at the peak of the correction and sits near 30% now vs a 20-year
   average of 17%. Daily moves of 1-2% are normal; the simulator is
   calibrated to this regime (~24% annualized), not to sleepy gold.
3. **Sessions matter.** Asia is quiet; London opens the range; the
   London/NY overlap (12:00-16:00 UTC) carries the most volume and the
   biggest moves; late NY fades. The bot's simulator and (implicitly)
   its signals live on this rhythm.
4. **Scheduled news moves gold violently**: CPI, NFP, FOMC. These appear
   in the sim as 0.1-0.5%+ jump events several times per week. Stops are
   attached broker-side in the MT5 bridge precisely because news gaps
   don't wait for a polling loop.
5. **Leverage math is merciless at 100:1.** A 1% adverse move against a
   full-margin 100:1 position is -100% of the account. Anyone "turning
   $20 into thousands in a day" at 100:1 is one 0.5% wiggle from zero —
   survivorship stories, same as memecoins. The bot uses leverage the
   professional way: the STOP defines position size (risk % / stop
   distance), and 100:1 is the ceiling that sizing may use, not a
   target it chases.

## 3. Current state (July 2026, live research)

- Spot: **~$4,103** (Jul 10, 2026), steadying above $4,100
- Forecast ranges being discussed: $3,365-$4,236 for July
- Drivers on the tape: Middle East risk, CPI/PPI prints, Fed policy
- Regime: post-blowoff correction — two-sided, volatile, news-driven.
  Good conditions for both breakout and mean-reversion entries; trend
  entries need the stricter slope filter (already tuned in).

Sources: USAGold/JM Bullion/AURUM price histories, World Gold Council
volatility data & 2026 mid-year outlook, TradingEconomics/Fortune July
2026 quotes, LiteFinance July 2026 forecast.
