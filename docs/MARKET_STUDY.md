# Memecoin Market Study (2013 → 2026)

This is the research the trading system is built on. Every strategy rule in
`docs/STRATEGIES.md` traces back to a finding here.

## 1. Timeline of the memecoin market

### 2013–2019: The joke era
- **Dec 2013 — Dogecoin (DOGE)** launches as a literal joke fork of
  Luckycoin/Litecoin. For years it trades sideways; its lesson comes later.
- Almost no infrastructure: buying a small-cap meme required mining it or
  obscure exchanges. Rug pulls existed but the market was tiny.

### 2020–2021: The first mania
- **DOGE** runs roughly **100x+** (sub-cent to ~$0.74, May 2021) on Elon
  Musk tweets and r/wallstreetbets spillover. First proof that attention
  alone can move tens of billions of dollars.
- **SHIB (Aug 2020)** does one of the largest multiples in financial
  history from its lows — the famous early wallets turned thousands into
  (paper) billions. Critically: almost nobody could realize those gains;
  selling even a fraction would have crushed the price. **Lesson: quoted
  multiples ≥ realizable multiples. Liquidity is the real constraint.**
- **SafeMoon and clones**: "tokenomics" (taxes, reflections) used to trap
  exit liquidity. Most went to ~zero. Lesson: complexity in a memecoin
  contract is a red flag, not a feature.

### 2022: Winter
- Meme activity collapses with the broader market. Survivors: DOGE, SHIB.
  Lesson: in bear phases the strategy should mostly sit in cash — volume
  filters (not price filters) tell you when the game is on.

### 2023: PEPE and the modern meta cycle
- **PEPE (Apr 2023)**: $0 → multi-billion FDV in ~3 weeks. Early buyers on
  day 1-2 did 1000x+. It codified the **narrative meta**: money rotates
  through themes (frogs → dogs-with-hats → AI → politics) and the theme's
  first mover captures most of the flow.
- **BONK (Dec 2022/23)** revives Solana; airdropped, then ~100x through
  2023. Marks the migration of memecoin activity from Ethereum (gas too
  expensive for small punts) to **Solana — cheap, fast, retail-friendly.**

### 2024: Industrialization — pump.fun
- **Jan 2024: pump.fun** launches: anyone creates a token in 2 minutes for
  ~$2. Tokens trade on a **bonding curve** until ~$69k market cap, then
  "graduate" to a real DEX (Raydium) with liquidity automatically deposited
  and burned. This one product multiplies token supply by orders of
  magnitude: **millions of launches**.
- **WIF (dogwifhat)** runs from ~$0.002 (Dec 2023) to **$4.80+ (Mar 2024)**
  — >2000x in under 4 months, the cycle's flagship "you could have been
  rich" chart.
- Celebrity-coin summer (mid-2024): the overwhelming majority collapse.
  Insiders sniped their own launches. Lesson: **assume every launch is
  adversarial by default.**

### 2025: Peak and reckoning
- **Jan 2025 — TRUMP** reaches multi-billion FDV within ~48h of launch.
  Wallets in the first minutes did 100-500x. It also **drained liquidity
  from every other memecoin** — correlation risk: one giant launch is a
  sell signal for the rest of the board.
- **Feb 2025 — LIBRA** scandal: insider-allocated launch collapses,
  retail holds the bag. Regulatory and social backlash follows.
- Sniper/bundler bots dominate block-0 of every notable launch. Retail
  buying at t=0 is usually buying *from* insiders, not with them.

## 2. The hard numbers (pump.fun era)

These are the statistics the simulator is calibrated to and the strategies
are designed around:

| Fact | Approximate value | Strategy consequence |
|---|---|---|
| Launches that never graduate the bonding curve | **~98.6%** | The curve is a free survival filter — let it do the first cut |
| Median time-to-death of a failed launch | **< 1 hour** | Never marry a fresh token; time-stop everything |
| Graduated tokens that trend afterward | minority, but contains ~all big winners | Fish exclusively in the graduated/surviving pool |
| Share of profits captured by top ~1% of wallets | the overwhelming majority | Copy proven wallets; measured PnL > influencer clout |
| Rug mechanics: unrevoked mint / unlocked LP / concentrated holders | present in most rugs, rare in runners | Safety screening is the single highest-edge filter |
| Realized multiple distribution of winners | log-normal: most 1.5–5x, thin 10–100x tail, moonshots ~1/1000s | Take-profit ladders: sell most into strength, always keep a moonbag |

## 3. About the "$50 → $50M" stories

They are real — and they are the wrong thing to build a system around:

1. **Survivorship bias**: for every wallet that rode one coin to 1,000,000x
   there are millions of wallets that went to zero playing the same game.
   The strategy "put everything on one coin and never sell" has a negative
   expected value with a lottery-ticket tail.
2. Many famous "genius" wallets were later shown to be **insiders** in the
   token they got rich on (deployer-linked funding paths).
3. The realistic, repeatable version of memecoin profitability looks like:
   **many small asymmetric bets, ruthless rug filtering, laddered exits,
   and a moonbag policy** — so that when a WIF-type runner happens to be in
   the book, you're still holding a slice of it at 100x. That is exactly
   what this system implements.

## 4. Structural lessons → system design

| Lesson | Where it lives in the code |
|---|---|
| Most tokens are scams by construction | `agents/rug_checker.py` hard-vetoes before any strategy sees the coin |
| Attention rotates through narratives | `agents/news_agent.py` boosts/penalizes by meta fit |
| A small wallet set is consistently early | `agents/whale_tracker.py` + `strategies/copy_trade.py` |
| The graduation moment is the best repeatable entry | `strategies/graduation_sniper.py` |
| Reflexive volume begets volume | `strategies/momentum.py` |
| Survivors dip 30–60% between legs | `strategies/dip_buyer.py` |
| Winners must fund a moonbag, losers must die fast | `engine/risk.py` TP ladder + stops |
| Fills on microcaps are bad | `engine/paper_broker.py` models impact + fees |
| Parameters decay; the market adapts | `agents/strategy_lab.py` re-evolves them |

## 5. July 2026 live-web study update

Findings from live web research (July 2026) now baked into the simulator
calibration and strategy signals:

- **Graduation collapse**: pump.fun graduation rates fell from ~1.4%
  (2024) to ~0.6% (late 2025) to **~0.2-0.26% by mid-2026** (DEXTools;
  SSRN survival analysis of 832,941 launches). The simulator's archetype
  weights now reflect this much harsher reality. Graduations migrate to
  **PumpSwap** (pump.fun's own DEX), no longer Raydium.
- **Socials are the strongest survival signal**: launches advertising all
  three social channels graduate at ~17x the rate of those with none;
  a Telegram alone is a ~9x lift. The Rug Checker's socials penalty and
  the simulator's social-quality correlation were strengthened accordingly.
- **Curve acceleration beats curve position**: the "graduation gap" study
  found graduating tokens show *accelerating* bonding-curve progress and
  organic, rhythmic buying in their first minutes; plateaued curves die
  regardless of how far they got. The graduation sniper now requires
  minimum bonding VELOCITY, not just progress (`grad_min_velocity`).
- **Liquidity velocity** is the single most informative graduation
  predictor (arXiv pump.fun success-prediction study) — reflected in the
  momentum strategy's volume-spike entry.
- **Current metas** (July 2026): brainrot memes, AI-agent coins
  (PIPPIN/FARTCOIN lineage), PolitiFi, NFT-community coins (PENGU/BONK),
  KOL coins (ANSEM +299%/wk). TikTok now rivals X as the discovery
  channel. The News agent's narrative table was extended, and
  `python3 -m memetrader study` mines the live vocabulary directly from
  trending tokens whenever the bot has internet.
- **Platform landscape**: Axiom now holds the largest trading-terminal
  market share (fastest execution, 0.75-0.95%/trade); GMGN remains the
  copy-trading + wallet-analytics default (6 chains, free analytics);
  Photon is a manual terminal, not a copy trader. New launchpads (Believe,
  Moonshot, Virl.fun) chip at pump.fun without displacing it.

Sources: DEXTools news & 2026 Solana guide, SSRN "Pump.fun Graduation
Regime Windows" (832,941 launches), arXiv 2602.14860, VoluTools
"Graduation Gap" study, walletmaster/telegramtrading 2026 tool comparisons.

## 6. Honest limitations

- The simulator is calibrated to published aggregate statistics, not tick
  data. It is a **strategy development environment**, not proof of future
  profit. Sim results (even +1000%) do NOT transfer 1:1 to live markets.
- Live paper trading (`python3 -m memetrader paper`) against real pump.fun
  and DexScreener feeds is the required next validation step, for weeks,
  before any real capital.
- Latency matters live: block-0 snipers will always be ahead of a polling
  bot. The strategies here deliberately avoid t=0 entries for that reason.
- Most memecoin traders lose money. Nothing here changes that base rate;
  it only tries to put you on the right side of the filters that separate
  the winning minority.
