"""Live data feeds — zero-dependency stdlib HTTP clients.

Free, keyless APIs used:
  * DexScreener  https://api.dexscreener.com   (pairs, new listings, boosts)
  * RugCheck     https://api.rugcheck.xyz      (token safety reports)
  * pump.fun     frontend API                  (bonding-curve coins)

Optional (better data, needs free API keys — set env vars):
  * HELIUS_API_KEY   -> wallet tx history for the whale tracker
  * BIRDEYE_API_KEY  -> richer OHLCV + top-trader leaderboards

Every call degrades gracefully: on network failure the feed returns [] and
the orchestrator simply skips the tick, so a flaky connection never crashes
a paper-trading session.
"""
from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request

from ..models import TokenSnapshot, TokenSource

UA = {"User-Agent": "memetrader/1.0 (paper-trading research bot)"}
TIMEOUT = 12


def _get(url: str, headers: dict | None = None) -> dict | list | None:
    req = urllib.request.Request(url, headers={**UA, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


# ---------------------------------------------------------------- DexScreener
def dexscreener_token_pairs(chain: str, address: str) -> list[dict]:
    data = _get(f"https://api.dexscreener.com/token-pairs/v1/{chain}/{address}")
    return data if isinstance(data, list) else []


def dexscreener_latest_profiles() -> list[dict]:
    data = _get("https://api.dexscreener.com/token-profiles/latest/v1")
    return data if isinstance(data, list) else []


def dexscreener_boosted() -> list[dict]:
    data = _get("https://api.dexscreener.com/token-boosts/latest/v1")
    return data if isinstance(data, list) else []


def pair_to_snapshot(p: dict) -> TokenSnapshot | None:
    try:
        base = p["baseToken"]
        created = (p.get("pairCreatedAt") or 0) / 1000.0
        info = p.get("info") or {}
        socials = bool(info.get("socials") or info.get("websites"))
        return TokenSnapshot(
            address=base["address"],
            symbol=base.get("symbol", "?"),
            name=base.get("name", "?"),
            source=TokenSource.DEXSCREENER,
            created_at=created or time.time(),
            price=float(p.get("priceUsd") or 0),
            market_cap=float(p.get("marketCap") or p.get("fdv") or 0),
            liquidity=float((p.get("liquidity") or {}).get("usd") or 0),
            volume_5m=float((p.get("volume") or {}).get("m5") or 0),
            volume_1h=float((p.get("volume") or {}).get("h1") or 0),
            holders=0,                      # enriched by RugCheck below
            top10_holder_pct=0.0,
            deployer_holds_pct=0.0,
            mint_revoked=False,
            freeze_revoked=False,
            lp_locked_or_burned=False,
            has_socials=socials,
            bonding_progress=1.0,
            graduated=True,
            smart_wallet_buys_30m=0,
        )
    except (KeyError, TypeError, ValueError):
        return None


# ------------------------------------------------------------------ RugCheck
def rugcheck_summary(mint: str) -> dict | None:
    return _get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")


def enrich_with_rugcheck(snap: TokenSnapshot) -> TokenSnapshot:
    """Fill safety fields from RugCheck's free endpoint (Solana only)."""
    rep = rugcheck_summary(snap.address)
    if not rep:
        return snap
    risks = {r.get("name", ""): r for r in rep.get("risks", [])}
    snap.mint_revoked = "Mint Authority still enabled" not in risks
    snap.freeze_revoked = "Freeze Authority still enabled" not in risks
    snap.lp_locked_or_burned = not any("LP" in k and "unlocked" in k.lower() for k in risks)
    for k, r in risks.items():
        if "Top 10" in k:
            try:
                snap.top10_holder_pct = float(r.get("value", "0").rstrip("%")) / 100.0
            except (ValueError, AttributeError):
                pass
    return snap


# ------------------------------------------------------------------ pump.fun
PUMP_API = "https://frontend-api.pump.fun"


def _pump_coin_to_snapshot(c: dict) -> TokenSnapshot | None:
    try:
        mcap = float(c.get("usd_market_cap") or 0)
        return TokenSnapshot(
            address=c["mint"],
            symbol=c.get("symbol", "?"),
            name=c.get("name", "?"),
            source=TokenSource.PUMPFUN,
            created_at=(c.get("created_timestamp") or 0) / 1000.0,
            price=mcap / 1_000_000_000.0,
            market_cap=mcap,
            liquidity=float(c.get("virtual_sol_reserves") or 0) / 1e9 * 150.0,
            volume_5m=0.0, volume_1h=0.0,
            holders=0,
            top10_holder_pct=0.0,
            deployer_holds_pct=0.0,
            mint_revoked=True,           # pump.fun revokes by design
            freeze_revoked=True,
            lp_locked_or_burned=True,    # bonding curve = no pullable LP
            has_socials=bool(c.get("twitter") or c.get("telegram") or c.get("website")),
            bonding_progress=min(1.0, mcap / 69_000.0),
            graduated=bool(c.get("complete")),
            smart_wallet_buys_30m=0,
            deployer=c.get("creator", "") or "",
        )
    except (KeyError, TypeError, ValueError):
        return None


def pumpfun_new_coins(limit: int = 50) -> list[TokenSnapshot]:
    data = _get(f"{PUMP_API}/coins?offset=0&limit={limit}&sort=created_timestamp&order=DESC")
    if not isinstance(data, list):
        return []
    return [s for s in (_pump_coin_to_snapshot(c) for c in data) if s]


def pumpfun_coin(mint: str) -> TokenSnapshot | None:
    """Single-coin refresh — the only price source for tokens still on the
    bonding curve (no DEX pair exists until graduation)."""
    data = _get(f"{PUMP_API}/coins/{mint}")
    return _pump_coin_to_snapshot(data) if isinstance(data, dict) else None


class LiveFeed:
    """Aggregates the free APIs behind the same interface as SimMarket."""

    def __init__(self, chain: str = "solana"):
        self.chain = chain
        self._known: dict[str, TokenSnapshot] = {}

    def new_tokens(self, max_age_ticks: int = 30) -> list[TokenSnapshot]:
        out: list[TokenSnapshot] = []
        for snap in pumpfun_new_coins():
            self._known[snap.address] = snap
            out.append(snap)
        for prof in dexscreener_latest_profiles()[:30]:
            if prof.get("chainId") != self.chain:
                continue
            for p in dexscreener_token_pairs(self.chain, prof.get("tokenAddress", "")):
                snap = pair_to_snapshot(p)
                if snap:
                    snap = enrich_with_rugcheck(snap)
                    self._known[snap.address] = snap
                    out.append(snap)
                break
        return out

    def snapshot(self, address: str) -> TokenSnapshot | None:
        prev = self._known.get(address)
        for p in dexscreener_token_pairs(self.chain, address):
            snap = pair_to_snapshot(p)
            if snap:
                if prev:
                    snap.price_high = max(prev.price_high, snap.price)
                    # keep safety fields we already paid a request for
                    snap.mint_revoked = prev.mint_revoked
                    snap.freeze_revoked = prev.freeze_revoked
                    snap.lp_locked_or_burned = prev.lp_locked_or_burned
                    snap.top10_holder_pct = prev.top10_holder_pct
                    snap.deployer = snap.deployer or prev.deployer
                self._known[address] = snap
                return snap
        # no DEX pair yet — token is still on the bonding curve, so
        # pump.fun itself is the only live price source
        if prev is not None and not prev.graduated:
            fresh = pumpfun_coin(address)
            if fresh is not None:
                fresh.price_high = max(prev.price_high, fresh.price)
                minutes = max(0.25, (fresh.ts - prev.ts) / 60.0)
                fresh.bonding_velocity = (
                    (fresh.bonding_progress - prev.bonding_progress) / minutes)
                self._known[address] = fresh
                return fresh
        return prev

    def current_narrative(self) -> str:
        # crude live proxy: most-boosted token's name keywords
        boosts = dexscreener_boosted()
        if boosts:
            return str(boosts[0].get("description", ""))[:40].lower()
        return ""
