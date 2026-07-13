"""Whale Tracker agent — follow the smart money.

Maintains a scored registry of trader wallets. A wallet earns "copyworthy"
status only from its measured track record (win rate, average multiple,
sample size) — never from clout. The copy-trade strategy fires when
enough copyworthy wallets pile into the same fresh token.

Live mode: seed `data/watch_wallets.json` with addresses (from GMGN /
Kolscan leaderboards) and, with a HELIUS_API_KEY, the tracker can poll
their swaps. Sim mode: the simulator surfaces smart-wallet buy counts
directly on each snapshot.
"""
from __future__ import annotations

import json
import os

from ..config import DATA_DIR, StrategyParams
from ..models import TokenSnapshot, WalletProfile

WATCHLIST_PATH = os.path.join(DATA_DIR, "watch_wallets.json")


class WhaleTracker:
    name = "whales"

    def __init__(self, params: StrategyParams):
        self.params = params
        self.wallets: dict[str, WalletProfile] = {}
        self._load_watchlist()

    # ------------------------------------------------------------------
    def _load_watchlist(self) -> None:
        if os.path.exists(WATCHLIST_PATH):
            try:
                with open(WATCHLIST_PATH) as f:
                    for w in json.load(f):
                        self.wallets[w["address"]] = WalletProfile(
                            address=w["address"], label=w.get("label", ""),
                            trades=w.get("trades", 0), wins=w.get("wins", 0),
                            realized_pnl_usd=w.get("realized_pnl_usd", 0.0),
                            sum_multiples=w.get("sum_multiples", 0.0),
                        )
            except (json.JSONDecodeError, KeyError):
                pass

    def save_watchlist(self) -> None:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(WATCHLIST_PATH, "w") as f:
            json.dump([vars(w) for w in self.wallets.values()], f, indent=2)

    # ------------------------------------------------------------------
    def record_wallet_trade(self, address: str, multiple: float, pnl_usd: float) -> None:
        w = self.wallets.setdefault(address, WalletProfile(address=address))
        w.trades += 1
        if multiple > 1.0:
            w.wins += 1
        w.sum_multiples += multiple
        w.realized_pnl_usd += pnl_usd

    def copyworthy_wallets(self) -> list[WalletProfile]:
        return sorted(
            (w for w in self.wallets.values() if w.copyworthy),
            key=lambda w: w.realized_pnl_usd, reverse=True,
        )

    # ------------------------------------------------------------------
    def smart_money_score(self, t: TokenSnapshot) -> float:
        """0..1 — how strongly is smart money signalling this token?"""
        n = t.smart_wallet_buys_30m
        if n <= 0:
            return 0.0
        return min(1.0, n / 4.0)

    def is_copy_signal(self, t: TokenSnapshot) -> bool:
        return t.smart_wallet_buys_30m >= self.params.copy_min_smart_buys
