"""The research-equity ledger — how much money this experiment is actually running.

An OKX Demo account is a junk drawer. It may hold BTC, ETH, OKB, AED, leftovers
from manual experiments, and whatever the exchange topped it up with. OKX's
``totalEq`` is the USD value of **all** of it. Sizing a research trade from that
number would mean:

* a BTC price move silently resizing every position, with no trade taken;
* a deposit silently raising the risk budget;
* "return %" over fourteen days measuring the account, not the strategies;
* loss limits computed against capital the experiment never had.

So the bot does not use ``totalEq``. It runs on a **research equity ledger**:
a USDT-denominated figure, capped at ``execution.research_equity_cap_usdt``,
that is read from the account exactly **once** — when the experiment starts —
and thereafter moves only by what this bot itself did.

The one-read rule is the whole design
-------------------------------------

Because the ledger never re-reads the account after the start, external events
*structurally cannot* touch it. There is no clamp to defeat and no rule to get
wrong: a deposit, an unrelated manual trade, or ETH doubling simply is not an
input. What the ledger adds after the start is only bot-attributable:

    current = starting
            + realized PnL   (this experiment's closed positions)
            + unrealized PnL (this experiment's open positions, marked to market)
            - fees           (this experiment's fills)
            + funding        (signed: positive when the bot received it)

Bot-earned profit therefore *may* carry current equity above the cap — that is
the experiment succeeding, and Day-14 metrics would be meaningless otherwise.
The cap bounds what the bot may *take from the account*, which is exactly the
starting figure and nothing else.

What still uses the real account
--------------------------------

Real balances are still read continuously, and are still authoritative for
safety checks: available USDT margin before every order, margin-ratio and
liquidation monitoring, and the balance snapshots written to the database.
Research equity governs *sizing and scoring*; the real account governs *can
this order actually be placed*. Both gates apply — see
:meth:`ResearchEquityLedger.affordable_margin`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ..exchange.models import WalletBalance
from ..utils.logging import get_logger
from ..utils.numeric import safe_div

log = get_logger(__name__)

#: The only currency research equity is ever denominated in. Everything else in
#: the demo account — BTC, ETH, OKB, AED — is deliberately excluded.
RESEARCH_CURRENCY = "USDT"

#: OKX balance-detail fields for a coin's equity, most specific first.
_EQUITY_FIELDS = ("eq", "cashBal", "availEq", "availBal")
#: OKX balance-detail fields for a coin's *available* (unlocked) balance.
_AVAILABLE_FIELDS = ("availEq", "availBal", "cashBal", "eq")


def usdt_equity(balance: WalletBalance) -> float:
    """The account's USDT equity — never ``totalEq``, never another asset.

    ``totalEq`` is the USD value of every holding. This reads the USDT line of
    the balance details only, so BTC, ETH, OKB and AED contribute nothing.
    """
    return _coin_field(balance, _EQUITY_FIELDS)


def usdt_available(balance: WalletBalance) -> float:
    """The USDT actually free to post as margin right now."""
    return _coin_field(balance, _AVAILABLE_FIELDS)


def _coin_field(balance: WalletBalance, fields: tuple[str, ...]) -> float:
    detail = balance.coins.get(RESEARCH_CURRENCY, {})
    for key in fields:
        value = detail.get(key)
        if value:
            return max(0.0, float(value))
    return 0.0


@dataclass(frozen=True, slots=True)
class EquityComponents:
    """The bot-attributable pieces, recomputed from persisted records.

    ``realized_pnl`` here is **gross of fees**, because ``fees`` is reported
    separately and the ledger subtracts it. The ``positions`` table stores
    ``realized_pnl`` already net of fees, so this adds the closed fees back —
    otherwise every fee would be counted twice.
    """

    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0


def components_from_records(
    *,
    closed_positions: Iterable[Mapping[str, Any]],
    open_positions: Iterable[Mapping[str, Any]] = (),
    unrealized_pnl: float = 0.0,
    funding: float = 0.0,
) -> EquityComponents:
    """Rebuild the ledger's components from the database.

    Deterministic and idempotent, which is what lets a restart land on exactly
    the number the previous run had.
    """
    closed = list(closed_positions)
    open_rows = list(open_positions)
    closed_fees = sum(float(row.get("fees") or 0.0) for row in closed)
    open_fees = sum(float(row.get("fees") or 0.0) for row in open_rows)
    realized_net = sum(float(row.get("realized_pnl") or 0.0) for row in closed)
    return EquityComponents(
        # Back to gross so the ledger's single fee subtraction is correct.
        realized_pnl=realized_net + closed_fees,
        unrealized_pnl=unrealized_pnl,
        fees=closed_fees + open_fees,
        funding=funding,
    )


@dataclass(frozen=True, slots=True)
class ResearchEquitySnapshot:
    """Everything the dashboard, reports and risk engine need, in one object."""

    cap_usdt: float
    starting_equity: float
    current_equity: float
    peak_equity: float
    realized_pnl: float
    unrealized_pnl: float
    fees: float
    funding: float
    # The real account, for display and for margin checks — never for sizing.
    actual_total_equity: float
    actual_available_usdt: float
    actual_usdt_equity: float

    @property
    def net_pnl(self) -> float:
        return self.current_equity - self.starting_equity

    @property
    def return_pct(self) -> float:
        """Return on **research** capital, not on the account."""
        return safe_div(self.net_pnl, self.starting_equity)

    @property
    def drawdown_pct(self) -> float:
        return safe_div(
            max(0.0, self.peak_equity - self.current_equity), self.peak_equity
        )

    @property
    def excluded_equity(self) -> float:
        """Account value deliberately not available to the experiment."""
        return max(0.0, self.actual_total_equity - self.starting_equity)

    def as_dict(self) -> dict[str, float]:
        return {
            "cap_usdt": round(self.cap_usdt, 2),
            "starting_equity": round(self.starting_equity, 2),
            "current_equity": round(self.current_equity, 2),
            "peak_equity": round(self.peak_equity, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "fees": round(self.fees, 2),
            "funding": round(self.funding, 2),
            "net_pnl": round(self.net_pnl, 2),
            "return_pct": round(self.return_pct * 100, 2),
            "drawdown_pct": round(self.drawdown_pct * 100, 2),
            "actual_total_equity": round(self.actual_total_equity, 2),
            "actual_available_usdt": round(self.actual_available_usdt, 2),
            "actual_usdt_equity": round(self.actual_usdt_equity, 2),
            "excluded_equity": round(self.excluded_equity, 2),
        }

    def banner_lines(self) -> list[str]:
        """The startup-banner block mandated by the research protocol."""
        return [
            f"Research capital used by bot: ${self.starting_equity:,.2f}",
            "Other OKX Demo assets: EXCLUDED",
            f"  Research equity cap:        ${self.cap_usdt:,.2f}",
            f"  Actual OKX total equity:    ${self.actual_total_equity:,.2f}",
            f"  Actual usable USDT:         ${self.actual_usdt_equity:,.2f}",
            f"  Excluded from research:     ${self.excluded_equity:,.2f}",
        ]


@dataclass(slots=True)
class ResearchEquityLedger:
    """Tracks the experiment's own capital, independently of the account.

    Construct it, then either :meth:`start` a new experiment (reads the account
    once) or :meth:`restore` an existing one (reads the persisted figure, so a
    restart continues the same ledger rather than re-deriving it from a balance
    that has since moved).
    """

    cap_usdt: float
    starting_equity: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    # Mirrors of the real account. Display and margin checks only.
    actual_total_equity: float = 0.0
    actual_available_usdt: float = 0.0
    actual_usdt_equity: float = 0.0
    _peak_equity: float = field(default=0.0, init=False)
    _started: bool = field(default=False, init=False)

    # --- lifecycle --------------------------------------------------------

    def start(self, balance: WalletBalance) -> float:
        """Fix the experiment's starting capital. The **only** account read.

        ``min(cap, usable USDT)`` — so an account holding $84,000 across BTC,
        ETH and $10,000 USDT starts with $10,000, and an account holding
        $9,994.32 USDT after smoke-test fees starts with $9,994.32 rather than
        failing.
        """
        self.observe_balance(balance)
        usable = self.actual_usdt_equity
        self.starting_equity = min(self.cap_usdt, usable)
        self._peak_equity = self.starting_equity
        self._started = True

        if usable > self.cap_usdt:
            log.info(
                "EQUITY",
                f"Account holds ${usable:,.2f} USDT; research capital capped at "
                f"${self.cap_usdt:,.2f}. The remainder is excluded.",
            )
        elif usable < self.cap_usdt:
            # Expected after a smoke test, and explicitly not an error.
            log.info(
                "EQUITY",
                f"Account holds ${usable:,.2f} USDT, below the ${self.cap_usdt:,.2f} cap — "
                "using the actual usable amount as research capital.",
            )
        if self.actual_total_equity > usable:
            log.info(
                "EQUITY",
                f"Excluded from research: ${self.actual_total_equity - usable:,.2f} of "
                f"non-{RESEARCH_CURRENCY} assets (BTC/ETH/OKB/etc.)",
            )
        return self.starting_equity

    def restore(self, starting_equity: float) -> None:
        """Resume an experiment from its persisted starting capital.

        Re-deriving it from the current balance would silently re-baseline the
        experiment on every restart — the day's losses would vanish, and the
        14-day return would be measured from the wrong number.
        """
        self.starting_equity = starting_equity
        self._peak_equity = max(self._peak_equity, starting_equity)
        self._started = True

    @property
    def started(self) -> bool:
        return self._started

    # --- inputs -----------------------------------------------------------

    def observe_balance(self, balance: WalletBalance) -> None:
        """Record the real account figures. Never changes research equity.

        Called on every balance update. It exists so margin checks and the
        dashboard see live numbers; the research figures are untouched, which
        is what makes deposits and unrelated holdings structurally incapable of
        moving the research ledger.
        """
        self.actual_total_equity = balance.total_equity
        self.actual_usdt_equity = usdt_equity(balance)
        self.actual_available_usdt = usdt_available(balance)

    def update_pnl(
        self,
        *,
        realized_pnl: float | None = None,
        unrealized_pnl: float | None = None,
        fees: float | None = None,
        funding: float | None = None,
    ) -> float:
        """Set the bot-attributable components. Returns the new current equity.

        Each argument is an absolute total for the experiment, not a delta, so
        recomputing from the database is idempotent — the ledger can be rebuilt
        at any time and will land on the same number.
        """
        if realized_pnl is not None:
            self.realized_pnl = realized_pnl
        if unrealized_pnl is not None:
            self.unrealized_pnl = unrealized_pnl
        if fees is not None:
            self.fees = fees
        if funding is not None:
            self.funding = funding
        current = self.current_equity
        self._peak_equity = max(self._peak_equity, current)
        return current

    def apply(self, components: EquityComponents) -> float:
        """Set every component at once from :func:`components_from_records`."""
        self.realized_pnl = components.realized_pnl
        self.unrealized_pnl = components.unrealized_pnl
        self.fees = components.fees
        self.funding = components.funding
        current = self.current_equity
        self._peak_equity = max(self._peak_equity, current)
        return current

    # --- outputs ----------------------------------------------------------

    @property
    def current_equity(self) -> float:
        """Starting capital plus everything this bot did to it.

        Never negative: a research account cannot owe money, and a negative
        figure would invert every downstream percentage.
        """
        return max(
            0.0,
            self.starting_equity
            + self.realized_pnl
            + self.unrealized_pnl
            - self.fees
            + self.funding,
        )

    @property
    def peak_equity(self) -> float:
        return max(self._peak_equity, self.current_equity)

    def affordable_margin(self, required_margin: float) -> tuple[bool, str]:
        """Whether the **real** account can post this margin right now.

        Research equity says how large a position *should* be; this says
        whether the account can actually carry it. Both must pass, and this one
        reads live USDT, not the research ledger.
        """
        if required_margin <= 0:
            return (True, "")
        if required_margin > self.actual_available_usdt:
            return (
                False,
                f"required margin ${required_margin:,.2f} exceeds available "
                f"{RESEARCH_CURRENCY} ${self.actual_available_usdt:,.2f}",
            )
        return (True, "")

    def snapshot(self) -> ResearchEquitySnapshot:
        return ResearchEquitySnapshot(
            cap_usdt=self.cap_usdt,
            starting_equity=self.starting_equity,
            current_equity=self.current_equity,
            peak_equity=self.peak_equity,
            realized_pnl=self.realized_pnl,
            unrealized_pnl=self.unrealized_pnl,
            fees=self.fees,
            funding=self.funding,
            actual_total_equity=self.actual_total_equity,
            actual_available_usdt=self.actual_available_usdt,
            actual_usdt_equity=self.actual_usdt_equity,
        )
