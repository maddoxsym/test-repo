"""Offline memecoin market simulator.

Calibrated to empirically observed pump.fun-era statistics (2024-2026):
  * ~98.6% of launched tokens never graduate the bonding curve
  * the median dead token rugs or bleeds out within the first hour
  * of graduated tokens, most peak at 1.5-5x; a small tail runs 10-100x;
    genuine moonshots (500x+) are roughly 1-in-several-thousand launches
  * observable safety features (mint revoked, LP burned, holder spread)
    correlate strongly with survival — which is exactly the edge the
    Rug Checker agent exploits
  * "smart money" wallets are early in a disproportionate share of the
    runners, but they also punt on plenty of rugs — copy trading has
    edge, not certainty.

Strategies never see a token's hidden archetype, only TokenSnapshots.
"""
from __future__ import annotations

import math
import random
import string
from dataclasses import dataclass, field

from ..models import TokenSnapshot, TokenSource

TICK_SECONDS = 60.0  # one simulator tick = one minute

# archetype -> probability. Calibrated to observed mid-2026 statistics:
# pump.fun graduation rate ~0.2-0.3% (DEXTools/SSRN survival analysis of
# 832k launches), i.e. ~99.7% of launches never complete the curve. The
# tradeable tail (runner+moonshot+part of pump_dump) is a few per thousand.
ARCHETYPES = {
    "hard_rug":   0.600,   # dev pulls liquidity / dumps within minutes
    "slow_bleed": 0.365,   # never finds buyers, -99% over hours
    "pump_dump":  0.028,   # coordinated pump then collapse (some graduate)
    "runner":     0.0065,  # real community, graduates, 3-40x
    "moonshot":   0.0005,  # the WIF/PEPE tail: 50-800x
}

# July 2026 metas (live web research): brainrot memes, AI agents,
# PolitiFi, NFT-community coins, KOL coins — plus the evergreens
NARRATIVES = [
    "dog", "cat", "frog", "ai", "political", "celebrity", "food",
    "retro", "space", "baby", "chad", "quant", "brainrot", "nft", "kol",
]

_SYLLABLES = ["do", "ge", "pe", "wif", "bo", "nk", "moo", "gi", "ga",
              "pu", "mp", "za", "ro", "ket", "flo", "ki", "sha", "ib"]


def _rand_address(rng: random.Random) -> str:
    return "SIM" + "".join(rng.choices(string.ascii_letters + string.digits, k=40))


def _rand_name(rng: random.Random, narrative: str) -> tuple[str, str]:
    base = "".join(rng.choices(_SYLLABLES, k=rng.randint(2, 3)))
    name = f"{narrative}-{base}" if rng.random() < 0.5 else base
    return name.capitalize(), base.upper()[:6]


@dataclass
class SimToken:
    address: str
    symbol: str
    name: str
    narrative: str
    archetype: str
    deployer: str
    created_tick: int
    price: float
    supply: float
    liquidity: float
    holders: int
    top10_pct: float
    deployer_pct: float
    mint_revoked: bool
    freeze_revoked: bool
    lp_locked: bool
    has_socials: bool
    smart_buys_30m: int = 0
    bonding_progress: float = 0.02
    bonding_velocity: float = 0.0
    graduated: bool = False
    dead: bool = False
    price_high: float = 0.0
    volume_5m: float = 0.0
    volume_1h: float = 0.0
    # hidden dynamics
    peak_multiple: float = 1.0
    rug_tick: int = 10 ** 9
    drift: float = 0.0
    entry_price0: float = 0.0

    def snapshot(self, now_ts: float) -> TokenSnapshot:
        return TokenSnapshot(
            address=self.address, symbol=self.symbol, name=self.name,
            source=TokenSource.SIM,
            created_at=now_ts - (0),  # patched by SimMarket
            price=self.price,
            market_cap=self.price * self.supply,
            liquidity=self.liquidity,
            volume_5m=self.volume_5m, volume_1h=self.volume_1h,
            holders=self.holders,
            top10_holder_pct=self.top10_pct,
            deployer_holds_pct=self.deployer_pct,
            mint_revoked=self.mint_revoked,
            freeze_revoked=self.freeze_revoked,
            lp_locked_or_burned=self.lp_locked,
            has_socials=self.has_socials,
            bonding_progress=self.bonding_progress,
            graduated=self.graduated,
            smart_wallet_buys_30m=self.smart_buys_30m,
            bonding_velocity=self.bonding_velocity,
            deployer=self.deployer,
            price_high=self.price_high,
            ts=now_ts,
        )


class SimMarket:
    """Generates and evolves a population of simulated memecoins tick by tick."""

    def __init__(self, seed: int = 7, launches_per_tick: float = 2.0):
        self.rng = random.Random(seed)
        self.launches_per_tick = launches_per_tick
        self.tick = 0
        self.tokens: dict[str, SimToken] = {}
        self.hot_narrative = self.rng.choice(NARRATIVES)
        self._narrative_ttl = self.rng.randint(180, 720)
        self.smart_wallets = [f"SMART{i:02d}" for i in range(12)]
        # deployer pools: serial ruggers grind out scam after scam, a small
        # set of credible builders launch most of the real runners
        self.rug_devs = [f"RUGDEV{i:03d}" for i in range(40)]
        self.builder_devs = [f"GOODDEV{i:02d}" for i in range(15)]

    # ------------------------------------------------------------------
    @property
    def now_ts(self) -> float:
        return 1_700_000_000.0 + self.tick * TICK_SECONDS

    def _spawn_token(self) -> SimToken:
        rng = self.rng
        archetype = rng.choices(list(ARCHETYPES), weights=list(ARCHETYPES.values()))[0]
        narrative = self.hot_narrative if rng.random() < 0.25 else rng.choice(NARRATIVES)
        name, symbol = _rand_name(rng, narrative)

        good = archetype in ("runner", "moonshot")
        # Observable features correlate with (but do not determine) quality.
        p_clean = 0.92 if good else (0.55 if archetype == "pump_dump" else 0.25)
        mint_revoked = rng.random() < p_clean
        freeze_revoked = rng.random() < (p_clean + 0.05)
        lp_locked = rng.random() < p_clean
        # socials are the single strongest observed graduation signal in the
        # 2026 survival studies (~17x lift with all three channels present)
        has_socials = rng.random() < (0.95 if good else 0.30)
        top10 = rng.uniform(0.10, 0.30) if good else rng.uniform(0.25, 0.85)
        deployer = rng.uniform(0.0, 0.04) if good else rng.uniform(0.02, 0.30)

        if good:
            deployer_addr = rng.choice(self.builder_devs) if rng.random() < 0.5 \
                else "DEV" + _rand_address(rng)[-8:]
        else:
            deployer_addr = rng.choice(self.rug_devs) if rng.random() < 0.65 \
                else "DEV" + _rand_address(rng)[-8:]

        supply = 1_000_000_000.0
        price = rng.uniform(6e-6, 3.5e-5)        # ~$6-35k FDV at launch
        tok = SimToken(
            address=_rand_address(rng), symbol=symbol, name=name,
            narrative=narrative, archetype=archetype, deployer=deployer_addr,
            created_tick=self.tick,
            price=price, supply=supply,
            liquidity=rng.uniform(4_000, 15_000),
            holders=rng.randint(5, 40),
            top10_pct=top10, deployer_pct=deployer,
            mint_revoked=mint_revoked, freeze_revoked=freeze_revoked,
            lp_locked=lp_locked, has_socials=has_socials,
        )
        tok.entry_price0 = price
        tok.price_high = price

        if archetype == "hard_rug":
            tok.rug_tick = self.tick + max(2, int(rng.expovariate(1 / 25)))
            tok.peak_multiple = rng.uniform(1.0, 2.5)
        elif archetype == "slow_bleed":
            tok.drift = -rng.uniform(0.006, 0.02)
            tok.peak_multiple = rng.uniform(1.0, 1.6)
        elif archetype == "pump_dump":
            tok.peak_multiple = rng.uniform(2.0, 10.0)
            tok.rug_tick = self.tick + rng.randint(20, 240)
        elif archetype == "runner":
            tok.peak_multiple = math.exp(rng.uniform(math.log(3), math.log(40)))
        else:  # moonshot
            tok.peak_multiple = math.exp(rng.uniform(math.log(50), math.log(800)))

        # Smart wallets ape good tokens early far more often — but not only
        # them: insiders also seed rugs with clout-wallet buys as bait.
        p_smart = {"runner": 0.50, "moonshot": 0.80, "pump_dump": 0.35}.get(archetype, 0.13)
        if rng.random() < p_smart:
            tok.smart_buys_30m = rng.randint(1, 4)
        return tok

    # ------------------------------------------------------------------
    def step(self) -> None:
        rng = self.rng
        self.tick += 1

        # rotate the hot narrative occasionally (the News agent tracks this)
        self._narrative_ttl -= 1
        if self._narrative_ttl <= 0:
            self.hot_narrative = rng.choice(NARRATIVES)
            self._narrative_ttl = rng.randint(180, 720)

        # launches (poisson-ish)
        n = 0
        lam = self.launches_per_tick
        while lam > 0 and rng.random() < min(1.0, lam):
            n += 1
            lam -= 1.0
        for _ in range(n):
            t = self._spawn_token()
            self.tokens[t.address] = t

        for tok in list(self.tokens.values()):
            if tok.dead:
                continue
            self._evolve(tok)

        # cull long-dead tokens to keep memory bounded
        if len(self.tokens) > 6000:
            dead = [a for a, t in self.tokens.items() if t.dead]
            for a in dead[: len(dead) // 2]:
                del self.tokens[a]

    def _evolve(self, tok: SimToken) -> None:
        rng = self.rng
        age = self.tick - tok.created_tick

        if self.tick >= tok.rug_tick:
            # liquidity pulled / dev dump
            tok.price *= rng.uniform(0.005, 0.06)
            tok.liquidity *= 0.02
            tok.volume_5m = tok.liquidity * rng.uniform(0.5, 2.0)
            tok.dead = True
            return

        mult = tok.price / tok.entry_price0
        target = tok.peak_multiple
        narrative_boost = 1.35 if tok.narrative == self.hot_narrative else 1.0

        if tok.archetype in ("runner", "moonshot", "pump_dump") and mult < target:
            # climb toward peak with noise; speed scales with remaining distance
            speed = {"pump_dump": 0.10, "runner": 0.035, "moonshot": 0.05}[tok.archetype]
            step = speed * narrative_boost * rng.uniform(0.2, 1.8)
            tok.price *= math.exp(step + rng.gauss(0, 0.03))
        elif tok.archetype in ("runner", "moonshot"):
            # plateau: mean-revert around peak with fat-tailed noise
            tok.price *= math.exp(rng.gauss(-0.002, 0.05))
        else:
            tok.price *= math.exp(tok.drift + rng.gauss(0, 0.04))

        tok.price = max(tok.price, tok.entry_price0 * 1e-4)
        tok.price_high = max(tok.price_high, tok.price)

        mcap = tok.price * tok.supply
        # bonding curve completes around ~$69k mcap (pump.fun graduation)
        if not tok.graduated:
            new_progress = min(1.0, mcap / 69_000.0)
            tok.bonding_velocity = new_progress - tok.bonding_progress
            tok.bonding_progress = new_progress
            if tok.bonding_progress >= 1.0:
                tok.graduated = True
                tok.liquidity += 12_000  # migration deposit to PumpSwap
        # liquidity and holders loosely track mcap
        tok.liquidity = max(500.0, tok.liquidity * 0.98 + mcap * 0.004)
        growth = {"runner": 8, "moonshot": 25, "pump_dump": 6}.get(tok.archetype, 1)
        tok.holders += rng.randint(0, growth)
        base_vol = tok.liquidity * rng.uniform(0.05, 0.5)
        climbing = tok.archetype in ("pump_dump", "runner", "moonshot") and mult < target
        spike = rng.uniform(2.5, 7.0) if (climbing and rng.random() < 0.35) else 1.0
        tok.volume_5m = base_vol * spike * rng.uniform(0.5, 1.5)
        # true rolling 1h volume (12 five-minute buckets, exponential window)
        tok.volume_1h = tok.volume_1h * (11.0 / 12.0) + tok.volume_5m

        # smart wallet activity decays / refreshes
        if rng.random() < 0.3:
            tok.smart_buys_30m = max(0, tok.smart_buys_30m - 1)
        if tok.archetype in ("runner", "moonshot") and mult < target and rng.random() < 0.15:
            tok.smart_buys_30m = min(6, tok.smart_buys_30m + 1)

        if tok.archetype == "slow_bleed" and mult < 0.05:
            tok.dead = True

    # ------------------------- feed interface -------------------------
    def new_tokens(self, max_age_ticks: int = 30) -> list[TokenSnapshot]:
        out = []
        for t in self.tokens.values():
            if not t.dead and self.tick - t.created_tick <= max_age_ticks:
                out.append(self._snap(t))
        return out

    def all_alive(self) -> list[TokenSnapshot]:
        return [self._snap(t) for t in self.tokens.values() if not t.dead]

    def snapshot(self, address: str) -> TokenSnapshot | None:
        t = self.tokens.get(address)
        return self._snap(t) if t else None

    def _snap(self, t: SimToken) -> TokenSnapshot:
        s = t.snapshot(self.now_ts)
        s.created_at = 1_700_000_000.0 + t.created_tick * TICK_SECONDS
        return s

    def current_narrative(self) -> str:
        return self.hot_narrative
