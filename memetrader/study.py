"""Market study — the bot researches the REAL memecoin market.

`python3 -m memetrader study` (needs internet) samples live data from
pump.fun and DexScreener and writes data/market_study.json:

  * launch-quality stats: share with socials, share graduated, median
    market caps / liquidity of what's trending
  * the CURRENT hot narrative vocabulary, mined from the names and
    descriptions of trending/boosted tokens right now

The News agent automatically folds the mined vocabulary into its
narrative scoring, so a fresh study immediately changes what the bot
favors — study the market, then trade according to it.
"""
from __future__ import annotations

import collections
import json
import os
import re
import statistics
import time

from .config import DATA_DIR

STUDY_PATH = os.path.join(DATA_DIR, "market_study.json")

_STOPWORDS = {
    "the", "a", "an", "of", "to", "and", "in", "on", "for", "is", "with",
    "coin", "token", "sol", "solana", "pump", "fun", "official", "by",
    "meme", "memecoin", "crypto", "com", "www", "http", "https", "this",
    "first", "new", "just", "your", "our", "it", "its", "at", "be",
}


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]{3,12}", (text or "").lower())
            if w not in _STOPWORDS]


def run_study(verbose: bool = True) -> dict | None:
    from .datafeed import live

    coins = live.pumpfun_new_coins(limit=200)
    boosts = live.dexscreener_boosted()
    profiles = live.dexscreener_latest_profiles()

    if not coins and not boosts and not profiles:
        if verbose:
            print("study: no market data reachable from this machine/network "
                  "— check your internet connection")
        return None

    words: collections.Counter = collections.Counter()
    for c in coins:
        words.update(_words(f"{c.name} {c.symbol}"))
    for b in list(boosts)[:60] + list(profiles)[:60]:
        if isinstance(b, dict):
            words.update(_words(str(b.get("description", ""))))

    mcaps = [c.market_cap for c in coins if c.market_cap > 0]
    study = {
        "ts": time.time(),
        "date": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "sample_launches": len(coins),
        "share_with_socials": round(
            sum(1 for c in coins if c.has_socials) / len(coins), 3) if coins else None,
        "share_graduated": round(
            sum(1 for c in coins if c.graduated) / len(coins), 4) if coins else None,
        "median_launch_mcap": round(statistics.median(mcaps), 0) if mcaps else None,
        "boosted_tokens_seen": len(boosts),
        "hot_words": [w for w, _ in words.most_common(20)],
    }

    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STUDY_PATH, "w") as f:
        json.dump(study, f, indent=2)

    if verbose:
        print(f"market study @ {study['date']}")
        print(f"  fresh launches sampled : {study['sample_launches']}")
        print(f"  with socials           : {study['share_with_socials']}")
        print(f"  median launch mcap     : ${study['median_launch_mcap'] or 0:,.0f}")
        print(f"  hot vocabulary         : {', '.join(study['hot_words'][:12])}")
        print(f"saved -> {STUDY_PATH} (news agent uses this automatically)")
    return study


def load_study(max_age_hours: float = 24.0) -> dict | None:
    """Most recent study, if fresh enough to still describe the market."""
    if not os.path.exists(STUDY_PATH):
        return None
    try:
        with open(STUDY_PATH) as f:
            study = json.load(f)
        if time.time() - study.get("ts", 0) > max_age_hours * 3600:
            return None
        return study
    except (json.JSONDecodeError, OSError):
        return None
