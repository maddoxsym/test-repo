"""Rug Checker agent — the background-check AI.

Every candidate passes through here BEFORE any strategy may buy it.
Scores 0-100 from on-chain safety signals; anything below the configured
threshold is vetoed. In live mode the fields are populated from RugCheck +
DexScreener; in sim mode from the simulator's observable features.

The single biggest edge in memecoins is not picking winners —
it is refusing to hold things that go to zero by design.
"""
from __future__ import annotations

import json
import os

from ..config import DATA_DIR, StrategyParams
from ..models import SafetyReport, TokenSnapshot

REPUTATION_PATH = os.path.join(DATA_DIR, "deployer_reputation.json")


class RugChecker:
    name = "rug_checker"

    def __init__(self, params: StrategyParams):
        self.params = params
        self._cache: dict[str, SafetyReport] = {}
        # deployer address -> {"rugs": n, "runners": n} — learned from every
        # closed trade and persisted, so "trusted source" status is earned
        self.reputation: dict[str, dict] = {}
        if os.path.exists(REPUTATION_PATH):
            try:
                with open(REPUTATION_PATH) as f:
                    self.reputation = json.load(f)
            except json.JSONDecodeError:
                pass

    # ---------------------- deployer reputation -----------------------
    def record_outcome(self, deployer: str, rugged: bool, ran: bool) -> None:
        if not deployer:
            return
        rep = self.reputation.setdefault(deployer, {"rugs": 0, "runners": 0})
        if rugged:
            rep["rugs"] += 1
        if ran:
            rep["runners"] += 1
        self._dirty = getattr(self, "_dirty", 0) + 1
        if self._dirty >= 20:           # batch writes: 1 file write per ~20 trades
            self.flush_reputation()

    def flush_reputation(self) -> None:
        self._dirty = 0
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(REPUTATION_PATH, "w") as f:
                json.dump(self.reputation, f, indent=2)
        except OSError:
            pass

    def _deployer_adjustment(self, t: TokenSnapshot) -> tuple[int, str | None]:
        rep = self.reputation.get(t.deployer)
        if not rep:
            return 0, None
        if rep["rugs"] >= 2 and rep["runners"] == 0:
            return -45, f"deployer rugged {rep['rugs']} previous launch(es)"
        if rep["rugs"] >= 1 and rep["rugs"] > rep["runners"]:
            return -20, "deployer has a rug on record"
        if rep["runners"] >= 2 and rep["rugs"] == 0:
            return +12, f"trusted deployer ({rep['runners']} previous runners)"
        return 0, None

    def check(self, t: TokenSnapshot) -> SafetyReport:
        score = 100
        flags: list[str] = []

        # --- hard authority checks: these are how most hard rugs happen ---
        if not t.mint_revoked:
            score -= 35
            flags.append("mint authority NOT revoked (infinite supply risk)")
        if not t.freeze_revoked:
            score -= 20
            flags.append("freeze authority NOT revoked (can block your sells)")
        if not t.lp_locked_or_burned and t.graduated:
            score -= 35
            flags.append("LP not locked/burned (classic liquidity pull setup)")

        # --- holder distribution: concentrated supply = exit liquidity ---
        if t.top10_holder_pct > 0.45:
            score -= 30
            flags.append(f"top10 hold {t.top10_holder_pct:.0%} of supply")
        elif t.top10_holder_pct > 0.30:
            score -= 15
            flags.append(f"top10 hold {t.top10_holder_pct:.0%} (elevated)")

        if t.deployer_holds_pct > 0.10:
            score -= 25
            flags.append(f"deployer still holds {t.deployer_holds_pct:.0%}")
        elif t.deployer_holds_pct > 0.05:
            score -= 10
            flags.append(f"deployer holds {t.deployer_holds_pct:.0%}")

        # --- softer signals ---
        if not t.has_socials:
            score -= 10
            flags.append("no socials/website")
        if t.liquidity < self.params.min_liquidity:
            score -= 15
            flags.append(f"thin liquidity ${t.liquidity:,.0f}")
        if t.holders < 20 and t.age_minutes > 30:
            score -= 10
            flags.append("holder count not growing")

        # --- creator background check: earned trust, earned distrust ---
        adj, flag = self._deployer_adjustment(t)
        score += adj
        if flag:
            flags.append(flag)

        score = max(0, min(100, score))
        report = SafetyReport(
            address=t.address, score=score, flags=flags,
            passed=score >= self.params.min_safety_score,
        )
        if len(self._cache) > 20_000:   # bound memory on long live runs
            for k in list(self._cache)[:10_000]:
                del self._cache[k]
        self._cache[t.address] = report
        return report

    def last(self, address: str) -> SafetyReport | None:
        return self._cache.get(address)
