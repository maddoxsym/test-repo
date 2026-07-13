# Platform Guide — what to actually use

You have Phantom. Keep it — but understand what it is: **Phantom is a
wallet (a vault + signer), not a trading platform.** Memecoin execution,
discovery, and copy trading happen in dedicated tools that your wallet
connects to or that run their own wallets.

## TL;DR recommendation

| Job | Tool | Why |
|---|---|---|
| Vault / long-term storage | **Phantom** (what you have) + ideally a hardware wallet | Never expose your main stack to a trading bot |
| Trading terminal + **copy trading** | **GMGN.ai** | Wallet PnL leaderboards + built-in copy-trade with per-wallet buy amounts, slippage and auto-sell settings — the best fit for "copy big profitable traders" (6 chains, free analytics) |
| Fast manual sniping terminal | **Axiom** (largest market share & fastest execution as of 2026, 0.75-0.95%/trade) or **Photon** (manual terminal — NOT a copy trader) or **BullX Neo** | Sub-second execution UIs for pump.fun launches |
| Telegram-native trading | **Trojan** or **BonkBot** | If you live in Telegram; fastest way to ape from a group chat |
| Safety background checks | **RugCheck.xyz**, **GoPlus**, holder tab on **GMGN** | Exactly the checks our Rug Checker automates |
| Wallet discovery for copy trading | **GMGN leaderboard**, **Kolscan**, **Cielo** | Find wallets by *realized PnL*, sort by win rate — never copy influencers' public calls |
| Charts / screening | **DexScreener**, **Birdeye** | Free APIs (this bot uses DexScreener) |
| Programmatic execution (this bot, later) | **Jupiter API** (swaps) + **Helius** (RPC/webhooks) | The standard Solana stack for a self-hosted bot |

## Why Solana (and not ETH/Base for now)

Fees of ~$0.01 and 400ms blocks are why the entire low-cap memecoin game
concentrated on Solana. Base has a scene (via clanker etc.), but pump.fun
liquidity, tooling, and copy-trade infrastructure are deepest on Solana.
Phantom already speaks Solana natively, so you're in the right ecosystem.

## Recommended wallet hygiene (IMPORTANT)

1. **Phantom main wallet** — savings only. Never connects to trading bots,
   never signs on memecoin sites.
2. **Fresh hot wallet** (create a second account in Phantom or a burner)
   — fund it with ONLY what you're prepared to lose entirely. This is the
   wallet you connect to GMGN/Axiom or export into a Telegram bot.
   Understand that Telegram bots custody this key — that's why the wallet
   must stay small.
3. Move profits OUT of the hot wallet to the vault regularly (our TP-ladder
   philosophy, applied to wallets).
4. Revoke stale token approvals occasionally (Phantom shows connected
   apps; revoke.cash equivalent on Solana: sol-incinerator / Phantom UI).

## Copy trading — concretely, on GMGN (or Axiom's leaderboard / Kolscan)

1. Leaderboard → filter by 7-day & 30-day **realized** PnL (realized should
   be the majority of total PnL — paper gains can't be copied), win rate
   ≥ 50-70% for snipers, and trade count high enough to mean something
   (≥ 30 trades). A good scoring weight from 2026 guides: win rate 35%,
   realized PnL 30%, trade frequency 20%, token diversity 15%.
2. Open the wallet's history: check they profit on MANY coins (skill), not
   one lucky hold (variance) — and that they aren't the token deployer
   (insider, unrepeatable). Prefer hold times of minutes-to-hours; a
   wallet that wins with 3-second holds is a block-0 sniper you cannot
   copy profitably (you'll always be behind it).
3. Add 3–5 such wallets to copy with SMALL fixed per-trade amounts, set
   max buy, slippage cap (~10–15% on microcaps), and auto-sell mirroring.
4. Feed those same addresses into this bot's `data/watch_wallets.json` so
   the paper trader evaluates them too — the bot keeps its own measured
   track record per wallet and only treats wallets as copyworthy after
   they prove themselves in ITS ledger.

## What this repo's bot uses (all free, keyless)

- `api.dexscreener.com` — pairs, new tokens, boosts
- `frontend-api.pump.fun` — bonding-curve coins & graduations
- `api.rugcheck.xyz` — safety report summaries

Optional upgrades (free tiers): `HELIUS_API_KEY` (wallet tx streams for
the whale tracker), `BIRDEYE_API_KEY` (richer OHLCV + trader data).

## Reality check

No platform fixes the base rate: most memecoin traders lose. Fees +
slippage on microcaps are brutal (this bot models ~1.1% + impact per side
— a round trip can cost 3-5% before the price moves). Paper trade here
first, then tiny size, then — only if the numbers still work — more.
