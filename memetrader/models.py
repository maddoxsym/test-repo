"""Core data models shared by every agent, strategy and engine component."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class TokenSource(str, Enum):
    PUMPFUN = "pumpfun"          # pump.fun bonding curve
    RAYDIUM = "raydium"          # graduated / DEX listed
    DEXSCREENER = "dexscreener"  # discovered via new-pair feed
    SIM = "sim"                  # offline simulator


@dataclass
class TokenSnapshot:
    """Point-in-time view of a token. This is the ONLY thing strategies see —
    hidden simulator state (archetype etc.) is never exposed to them."""
    address: str
    symbol: str
    name: str
    source: TokenSource
    created_at: float            # unix ts
    price: float                 # in USD
    market_cap: float            # USD (fully diluted for memecoins)
    liquidity: float             # USD in the pool
    volume_5m: float             # USD traded in the last 5 minutes
    volume_1h: float             # USD traded in the last hour
    holders: int
    top10_holder_pct: float      # fraction 0..1 held by top-10 non-LP wallets
    deployer_holds_pct: float    # fraction 0..1 still held by the deployer
    mint_revoked: bool           # can no one mint more supply?
    freeze_revoked: bool         # can no one freeze wallets?
    lp_locked_or_burned: bool    # is the liquidity pool locked/burned?
    has_socials: bool            # twitter/telegram/website present
    bonding_progress: float      # 0..1 pump.fun bonding-curve progress (1 = graduated)
    graduated: bool
    smart_wallet_buys_30m: int   # how many tracked profitable wallets bought recently
    bonding_velocity: float = 0.0  # curve progress per minute — 2026 studies:
                                   # ACCELERATING curves graduate, plateaus die
    deployer: str = ""           # wallet that created the token (reputation tracking)
    price_high: float = 0.0      # all-time-high price observed
    ts: float = field(default_factory=time.time)

    @property
    def age_minutes(self) -> float:
        return max(0.0, (self.ts - self.created_at) / 60.0)

    @property
    def drawdown_from_high(self) -> float:
        if self.price_high <= 0:
            return 0.0
        return 1.0 - (self.price / self.price_high)


@dataclass
class SafetyReport:
    address: str
    score: int                   # 0 (guaranteed rug) .. 100 (as safe as memecoins get)
    flags: list[str]
    passed: bool

    def __str__(self) -> str:
        state = "PASS" if self.passed else "FAIL"
        return f"[{state} {self.score}/100] {', '.join(self.flags) or 'clean'}"


@dataclass
class Signal:
    strategy: str
    address: str
    symbol: str
    side: str                    # "buy" (strategies only open; risk engine closes)
    confidence: float            # 0..1, scales position size
    reason: str
    ts: float = field(default_factory=time.time)


@dataclass
class Position:
    address: str
    symbol: str
    strategy: str
    entry_price: float
    entry_mcap: float
    size_usd: float              # capital committed at entry
    tokens: float                # token quantity after slippage/fees
    opened_at: float
    high_water_price: float
    tp_levels_hit: int = 0       # how many take-profit rungs already executed
    remaining_fraction: float = 1.0
    last_price: float = 0.0      # previous tick's price (spike detection)
    realized_usd: float = 0.0    # exact cash received from every sell so far

    def unrealized_multiple(self, price: float) -> float:
        if self.entry_price <= 0:
            return 1.0
        return price / self.entry_price


@dataclass
class TradeRecord:
    address: str
    symbol: str
    strategy: str
    entry_price: float
    exit_price: float
    entry_mcap: float
    size_usd: float
    pnl_usd: float
    multiple: float
    hold_minutes: float
    exit_reason: str
    opened_at: float
    closed_at: float


@dataclass
class WalletProfile:
    """Track record of an on-chain trader we might copy."""
    address: str
    label: str = ""
    trades: int = 0
    wins: int = 0
    realized_pnl_usd: float = 0.0
    sum_multiples: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else 0.0

    @property
    def avg_multiple(self) -> float:
        return self.sum_multiples / self.trades if self.trades else 0.0

    @property
    def copyworthy(self) -> bool:
        return self.trades >= 10 and self.win_rate >= 0.45 and self.avg_multiple >= 1.5
