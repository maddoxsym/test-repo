# RUNBOOK — run the paper trader on your own computer

This is the copy-paste guide. No wallet, no keys, no real money — the bot
watches the REAL memecoin market (pump.fun, DexScreener, RugCheck) and
paper-trades it. ~5 minutes to set up.

## 1. One-time setup

**Mac**: open the Terminal app. Python 3 is already installed on recent
macOS; check with `python3 --version` (needs 3.10+).

**Windows**: install Python from https://python.org (tick "Add to PATH"),
then open PowerShell and use `python` wherever this guide says `python3`.

Then:

```bash
git clone https://github.com/aosman999/Memcoin-trader.git
cd Memcoin-trader
git checkout claude/memecoin-trader-ai-39lzel
```

(If you don't have git: download the repo as a ZIP from GitHub — green
"Code" button → Download ZIP — unzip it, and `cd` into the folder.)

There is nothing to install — the bot has zero dependencies.

## 2. Let the bot study the real market, then trade it

```bash
# 1) research pass: samples live launches, mines the hot narrative
#    vocabulary of the moment (the news agent uses it automatically)
python3 -m memetrader study

# 2) paper trade the real market
python3 -m memetrader paper --scalp --bankroll 20 --minutes 1440
```

Re-run `study` every day or two — metas rotate fast and the bot trades
according to whatever it studied last.

That runs 24 hours of live paper trading in quick-flip mode with a $20
paper bankroll:
- open positions are re-checked **every 5 seconds** (spike exits, stops,
  take-profit rungs, rug alerts)
- new-coin discovery sweeps run every 45 seconds
- every OPEN and CLOSE is printed as it happens
- progress is saved continuously — Ctrl-C any time, restart later and it
  resumes the same portfolio

Check results any time (in a second terminal or after stopping):

```bash
python3 -m memetrader report
```

## 3. Keep it watching 24/7

Simplest: leave the terminal open (prevent the laptop sleeping: on Mac,
System Settings → keep awake when plugged in, or run `caffeinate` in
another tab). More robust — survives closing the terminal:

```bash
nohup python3 -m memetrader paper --scalp --bankroll 20 --minutes 10080 > bot.log 2>&1 &
tail -f bot.log        # watch it live; Ctrl-C stops the *viewing*, not the bot
```

`--minutes 10080` = one week. To stop the bot: `pkill -f memetrader`.

For true always-on operation, a ~$5/month VPS (e.g. any basic Ubuntu box)
runs the exact same two commands.

## 4. Optional upgrades

- **Copy trading watchlist**: copy `data/watch_wallets.example.json` to
  `data/watch_wallets.json` and replace the placeholder addresses with
  wallets from GMGN.ai's leaderboard (sort by realized PnL and win rate).
- **Refresh strategies**: `python3 -m memetrader evolve --scalp` re-tunes
  parameters on the simulator any time.
- **Pull the latest bot improvements**: `git pull` inside the folder.

## 5. What NOT to do yet

Do not connect any wallet or trade real money on the back of simulator
results. The bar to even consider real funds: **several weeks of live
paper trading with a growing equity curve**. If it can't beat the real
market on paper, it won't beat it with money — and if it can, you'll see
it in `python3 -m memetrader report` first.
