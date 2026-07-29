"""The research-equity cap — the bot runs on its own capital, not the account.

The demo account is a junk drawer: BTC, ETH, OKB, AED, leftovers from manual
trades. OKX's ``totalEq`` values all of it. Sizing from that number would let a
BTC price move resize every position and a deposit raise the risk budget.

The rule under test throughout: **research equity is read from the account
exactly once, at experiment start, capped, and USDT-only. After that it moves
only by what this bot itself did.** The account is still read continuously —
but only for margin checks and display.
"""

from __future__ import annotations

import pytest

from btcbot.config.schema import ExecutionConfig, RiskConfig, SafetyConfig
from btcbot.decision.risk_state import PAUSED, RiskStateTracker
from btcbot.exchange.models import WalletBalance
from btcbot.execution.research_equity import (
    RESEARCH_CURRENCY,
    ResearchEquityLedger,
    components_from_records,
    usdt_available,
    usdt_equity,
)
from btcbot.risk.position_sizing import PositionSizer, SizingInputs
from btcbot.safety.circuit_breakers import CircuitBreakers
from btcbot.strategies.base import Direction

CAP = 10_000.0


def _size(equity: float, instrument, *, available: float = 1_000_000.0):
    """Size one long from a given equity, holding everything else constant."""
    return PositionSizer(RiskConfig()).calculate(
        SizingInputs(
            equity=equity,
            available_balance=available,
            entry_price=100_000.0,
            stop_price=99_000.0,
            direction=Direction.LONG,
            confidence=0.7,
            leverage=2.0,
        ),
        instrument,
    )


def balance(
    *,
    usdt: float = 10_000.0,
    usdt_avail: float | None = None,
    btc_usd: float = 59_000.0,
    eth_usd: float = 15_000.0,
    total: float | None = None,
) -> WalletBalance:
    """A realistic OKX demo balance: mostly not USDT."""
    coins: dict[str, dict[str, float]] = {
        "USDT": {"eq": usdt, "availEq": usdt if usdt_avail is None else usdt_avail,
                 "cashBal": usdt},
    }
    if btc_usd:
        coins["BTC"] = {"eq": 0.5, "eqUsd": btc_usd, "availEq": 0.5}
    if eth_usd:
        coins["ETH"] = {"eq": 5.0, "eqUsd": eth_usd, "availEq": 5.0}
    return WalletBalance(
        total_equity=total if total is not None else usdt + btc_usd + eth_usd,
        total_available=usdt if usdt_avail is None else usdt_avail,
        unrealized_pnl=0.0,
        coins=coins,
        ts_ms=1_700_000_000_000,
    )


def started(cap: float = CAP, **kwargs) -> ResearchEquityLedger:
    ledger = ResearchEquityLedger(cap_usdt=cap)
    ledger.start(balance(**kwargs))
    return ledger


class TestStartingEquityIsCappedAndUsdtOnly:
    def test_84000_total_with_10000_usdt_gives_10000(self):
        """The headline case: a rich account, a capped experiment."""
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        start = ledger.start(balance(usdt=10_000.0, btc_usd=59_000.0, eth_usd=15_000.0))

        assert start == pytest.approx(10_000.0)
        assert ledger.current_equity == pytest.approx(10_000.0)
        assert ledger.actual_total_equity == pytest.approx(84_000.0)
        assert ledger.snapshot().excluded_equity == pytest.approx(74_000.0)

    def test_a_huge_usdt_balance_is_still_capped(self):
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        assert ledger.start(balance(usdt=250_000.0)) == pytest.approx(CAP)

    def test_slightly_under_the_cap_uses_the_actual_amount(self):
        """After smoke-test fees the account holds a little less. Not an error."""
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        start = ledger.start(balance(usdt=9_994.32))

        assert start == pytest.approx(9_994.32)
        assert ledger.current_equity == pytest.approx(9_994.32)

    def test_no_usdt_at_all_yields_zero_rather_than_the_cap(self):
        """A BTC-only account must not be handed $10,000 it does not have."""
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        assert ledger.start(balance(usdt=0.0, btc_usd=84_000.0, eth_usd=0.0)) == 0.0

    @pytest.mark.parametrize("ccy", ["BTC", "ETH", "OKB", "AED", "USDC"])
    def test_no_other_asset_contributes_to_research_equity(self, ccy):
        held = WalletBalance(
            total_equity=500_000.0, total_available=500_000.0, unrealized_pnl=0.0,
            coins={"USDT": {"eq": 250.0, "availEq": 250.0},
                   ccy: {"eq": 1_000.0, "eqUsd": 499_750.0, "availEq": 1_000.0}},
            ts_ms=0,
        )
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        assert ledger.start(held) == pytest.approx(250.0)

    def test_usdt_helpers_read_the_usdt_line_only(self):
        bal = balance(usdt=7_500.0, usdt_avail=6_000.0)
        assert usdt_equity(bal) == pytest.approx(7_500.0)
        assert usdt_available(bal) == pytest.approx(6_000.0)
        assert RESEARCH_CURRENCY == "USDT"
        # totalEq is much larger and is deliberately not consulted.
        assert bal.total_equity > usdt_equity(bal)

    def test_the_cap_is_configurable_and_defaults_to_ten_thousand(self):
        assert ExecutionConfig().research_equity_cap_usdt == 10_000.0
        assert ExecutionConfig(research_equity_cap_usdt=2_500).research_equity_cap_usdt == 2_500


class TestUnrelatedAccountChangesAreInert:
    def test_a_btc_price_change_does_not_alter_research_equity(self):
        """The whole reason this ledger exists."""
        ledger = started(usdt=10_000.0, btc_usd=59_000.0)
        before = ledger.current_equity

        # BTC doubles. totalEq leaps; research equity must not move.
        ledger.observe_balance(balance(usdt=10_000.0, btc_usd=118_000.0))

        assert ledger.current_equity == pytest.approx(before)
        assert ledger.starting_equity == pytest.approx(10_000.0)
        assert ledger.actual_total_equity == pytest.approx(143_000.0)

    def test_an_eth_collapse_does_not_alter_research_equity(self):
        ledger = started(usdt=10_000.0, eth_usd=15_000.0)
        ledger.observe_balance(balance(usdt=10_000.0, eth_usd=1_000.0))
        assert ledger.current_equity == pytest.approx(10_000.0)

    def test_a_usdt_deposit_does_not_raise_research_equity(self):
        """Requirement: a deposit can never increase the bot's budget."""
        ledger = started(usdt=10_000.0)
        ledger.observe_balance(balance(usdt=250_000.0))

        assert ledger.current_equity == pytest.approx(10_000.0)
        assert ledger.starting_equity == pytest.approx(10_000.0)
        # The real figure is still visible — it is just not an input.
        assert ledger.actual_usdt_equity == pytest.approx(250_000.0)

    def test_a_deposit_cannot_raise_a_below_cap_start_to_the_cap(self):
        """The under-funded case is the one a naive clamp would get wrong."""
        ledger = started(usdt=4_000.0)
        assert ledger.starting_equity == pytest.approx(4_000.0)

        ledger.observe_balance(balance(usdt=100_000.0))
        assert ledger.current_equity == pytest.approx(4_000.0)

    def test_a_manual_withdrawal_does_not_lower_research_equity_either(self):
        """Symmetry: the ledger is not a mirror of the account in any direction."""
        ledger = started(usdt=10_000.0)
        ledger.observe_balance(balance(usdt=10.0))
        assert ledger.current_equity == pytest.approx(10_000.0)

    def test_observing_a_balance_is_the_only_account_read_after_start(self):
        """Structural: nothing but start() and restore() sets starting equity."""
        ledger = started()
        for _ in range(50):
            ledger.observe_balance(balance(usdt=999_999.0, btc_usd=1_000_000.0))
        assert ledger.starting_equity == pytest.approx(10_000.0)


class TestPnlFeesAndFundingMoveResearchEquity:
    def test_realized_profit_raises_it(self):
        ledger = started()
        assert ledger.update_pnl(realized_pnl=250.0) == pytest.approx(10_250.0)

    def test_fees_lower_it(self):
        ledger = started()
        assert ledger.update_pnl(realized_pnl=250.0, fees=12.5) == pytest.approx(10_237.5)

    def test_funding_is_signed(self):
        ledger = started()
        assert ledger.update_pnl(funding=-8.0) == pytest.approx(9_992.0)
        assert ledger.update_pnl(funding=8.0) == pytest.approx(10_008.0)

    def test_unrealized_pnl_is_included(self):
        ledger = started()
        assert ledger.update_pnl(unrealized_pnl=-140.0) == pytest.approx(9_860.0)

    def test_the_full_formula(self):
        ledger = started()
        current = ledger.update_pnl(
            realized_pnl=420.0, unrealized_pnl=-60.0, fees=18.25, funding=-3.5
        )
        assert current == pytest.approx(10_000.0 + 420.0 - 60.0 - 18.25 - 3.5)
        assert ledger.snapshot().net_pnl == pytest.approx(338.25)

    def test_bot_profit_may_exceed_the_cap(self):
        """The cap bounds what the bot takes from the account, not what it earns."""
        ledger = started()
        assert ledger.update_pnl(realized_pnl=3_000.0) == pytest.approx(13_000.0)
        assert ledger.snapshot().return_pct == pytest.approx(0.30)

    def test_equity_never_goes_negative(self):
        ledger = started()
        assert ledger.update_pnl(realized_pnl=-50_000.0) == 0.0

    def test_peak_equity_ratchets_and_drives_drawdown(self):
        ledger = started()
        ledger.update_pnl(realized_pnl=1_000.0)
        ledger.update_pnl(realized_pnl=500.0)
        snapshot = ledger.snapshot()
        assert snapshot.peak_equity == pytest.approx(11_000.0)
        assert snapshot.drawdown_pct == pytest.approx(500.0 / 11_000.0)

    def test_updates_are_absolute_totals_not_deltas(self):
        """Idempotence is what lets a restart rebuild the same number."""
        ledger = started()
        for _ in range(5):
            ledger.update_pnl(realized_pnl=100.0)
        assert ledger.current_equity == pytest.approx(10_100.0)


class TestComponentsFromRecords:
    """Rebuilding from the database — the restart path."""

    def test_closed_position_pnl_is_grossed_up_so_fees_count_once(self):
        """`positions.realized_pnl` is stored net of fees; fees are reported
        separately, so counting both raw would double-charge."""
        components = components_from_records(
            closed_positions=[{"realized_pnl": 88.0, "fees": 12.0}],
        )
        assert components.realized_pnl == pytest.approx(100.0)
        assert components.fees == pytest.approx(12.0)

        ledger = ResearchEquityLedger(cap_usdt=CAP, starting_equity=10_000.0)
        assert ledger.apply(components) == pytest.approx(10_088.0)

    def test_open_position_fees_are_counted(self):
        components = components_from_records(
            closed_positions=[],
            open_positions=[{"fees": 4.5}],
            unrealized_pnl=30.0,
        )
        assert components.fees == pytest.approx(4.5)
        assert components.unrealized_pnl == pytest.approx(30.0)

    def test_funding_passes_straight_through(self):
        assert components_from_records(closed_positions=[], funding=-7.25).funding == -7.25

    def test_empty_records_produce_zeroes(self):
        components = components_from_records(closed_positions=[])
        assert (components.realized_pnl, components.unrealized_pnl) == (0.0, 0.0)
        assert (components.fees, components.funding) == (0.0, 0.0)


class TestRestartRestoresTheSameLedger:
    def test_restore_uses_the_persisted_start_not_the_current_balance(self):
        """Re-deriving from today's balance would erase the day's losses."""
        original = started(usdt=10_000.0)
        original.update_pnl(realized_pnl=-1_500.0)
        assert original.current_equity == pytest.approx(8_500.0)

        # Restart. The account now reads differently — BTC moved, and the
        # bot's own losses have left less USDT.
        resumed = ResearchEquityLedger(cap_usdt=CAP)
        resumed.observe_balance(balance(usdt=8_500.0, btc_usd=120_000.0))
        resumed.restore(original.starting_equity)
        resumed.apply(
            components_from_records(closed_positions=[{"realized_pnl": -1_500.0, "fees": 0.0}])
        )

        assert resumed.starting_equity == pytest.approx(10_000.0)
        assert resumed.current_equity == pytest.approx(8_500.0)
        assert resumed.snapshot().return_pct == pytest.approx(-0.15)

    def test_restore_does_not_re_cap_from_the_account(self):
        """An experiment that started under-funded stays under-funded."""
        resumed = ResearchEquityLedger(cap_usdt=CAP)
        resumed.observe_balance(balance(usdt=100_000.0))
        resumed.restore(4_000.0)
        assert resumed.starting_equity == pytest.approx(4_000.0)

    def test_a_restored_ledger_is_marked_started(self):
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        assert not ledger.started
        ledger.restore(9_000.0)
        assert ledger.started


class TestMarginIsCheckedAgainstTheRealAccount:
    def test_margin_within_available_usdt_is_allowed(self):
        ledger = started(usdt=10_000.0, usdt_avail=8_000.0)
        ok, reason = ledger.affordable_margin(2_000.0)
        assert ok and reason == ""

    def test_margin_beyond_available_usdt_is_refused(self):
        """Requirement 9: the real account, not the research figure, decides."""
        ledger = started(usdt=10_000.0, usdt_avail=500.0)
        ok, reason = ledger.affordable_margin(2_000.0)

        assert not ok
        assert "exceeds available USDT" in reason
        assert "$500.00" in reason

    def test_a_large_research_equity_does_not_excuse_missing_margin(self):
        """Research equity says how big; the account says whether it can be posted."""
        ledger = started(usdt=10_000.0, usdt_avail=100.0)
        ledger.update_pnl(realized_pnl=5_000.0)   # research equity now $15,000
        assert ledger.affordable_margin(1_000.0)[0] is False

    def test_zero_margin_is_trivially_affordable(self):
        assert started().affordable_margin(0.0) == (True, "")

    def test_the_sizer_reduces_size_when_available_usdt_is_thin(
        self, perp_instrument
    ):
        """The production path: available USDT bounds the notional."""
        rich = _size(10_000.0, perp_instrument, available=10_000.0)
        thin = _size(10_000.0, perp_instrument, available=200.0)

        assert rich.approved
        # Either it shrank, or it was refused outright — never unchanged.
        assert (not thin.approved) or thin.quantity < rich.quantity


class TestSizingUsesResearchEquityNotTotalEq:
    def test_position_size_scales_with_research_equity(self, perp_instrument):
        small = _size(10_000.0, perp_instrument)
        large = _size(84_000.0, perp_instrument)
        assert small.approved and large.approved
        assert large.quantity > small.quantity, "sizing does track the equity it is given"

    def test_the_research_figure_is_what_gets_passed(self, perp_instrument):
        """An $84k account with a $10k cap must size as $10k, not $84k."""
        ledger = started(usdt=10_000.0, btc_usd=59_000.0, eth_usd=15_000.0)
        assert ledger.actual_total_equity == pytest.approx(84_000.0)

        research = _size(ledger.current_equity, perp_instrument)
        account = _size(ledger.actual_total_equity, perp_instrument)
        assert research.quantity < account.quantity
        assert research.quantity == _size(10_000.0, perp_instrument).quantity


class TestLossLimitsUseResearchEquity:
    def _tracker(self) -> RiskStateTracker:
        return RiskStateTracker(RiskConfig(), CircuitBreakers(SafetyConfig()))

    def test_the_daily_limit_is_measured_against_research_equity(self):
        tracker = self._tracker()
        tracker.start_of_day(10_000.0)

        # An 11% loss of research capital pauses trading...
        snapshot = tracker.evaluate(equity=8_900.0, peak_equity=10_000.0)
        assert snapshot.state == PAUSED
        assert "daily loss" in snapshot.reason
        assert "research equity" in snapshot.reason

    def test_the_same_dollar_loss_against_totaleq_would_not_have_paused(self):
        """$1,100 is 11% of research capital but only 1.3% of an $84k account."""
        tracker = self._tracker()
        tracker.start_of_day(84_000.0)
        assert tracker.evaluate(equity=82_900.0, peak_equity=84_000.0).state != PAUSED

    def test_the_weekly_limit_pauses_trading(self):
        """A week of small daily losses, none of which trips the daily limit."""
        tracker = self._tracker()
        tracker.start_of_week(10_000.0)
        tracker.start_of_day(8_200.0)      # today opened after a bad week

        snapshot = tracker.evaluate(equity=7_900.0, peak_equity=10_000.0)
        assert snapshot.state == PAUSED
        assert "weekly loss" in snapshot.reason
        assert snapshot.weekly_loss_pct == pytest.approx(0.21, abs=0.005)

    def test_the_daily_limit_trips_before_the_weekly_one(self):
        tracker = self._tracker()
        tracker.start_of_day(10_000.0)
        tracker.start_of_week(10_000.0)
        assert "daily loss" in tracker.evaluate(equity=8_500.0, peak_equity=10_000.0).reason

    def test_weekly_must_not_be_tighter_than_daily(self):
        with pytest.raises(Exception, match="weekly_loss_limit_pct"):
            RiskConfig(daily_loss_limit_pct=0.20, weekly_loss_limit_pct=0.10)

    def test_both_percentages_are_reported(self):
        tracker = self._tracker()
        tracker.start_of_day(10_000.0)
        tracker.start_of_week(10_000.0)
        data = tracker.evaluate(equity=9_700.0, peak_equity=10_000.0).as_dict()
        assert data["daily_loss_pct"] == pytest.approx(3.0)
        assert data["weekly_loss_pct"] == pytest.approx(3.0)


class TestSnapshotAndBanner:
    def test_the_banner_states_the_capital_and_the_exclusion(self):
        """Requirement 11, verbatim."""
        lines = started(usdt=10_000.0).snapshot().banner_lines()
        assert lines[0] == "Research capital used by bot: $10,000.00"
        assert lines[1] == "Other OKX Demo assets: EXCLUDED"

    def test_the_snapshot_separates_research_from_actual(self):
        """Requirement 10: five distinct figures, never conflated."""
        data = started(usdt=10_000.0).snapshot().as_dict()
        for key in (
            "actual_total_equity",
            "actual_available_usdt",
            "starting_equity",
            "current_equity",
            "cap_usdt",
        ):
            assert key in data
        assert data["actual_total_equity"] == pytest.approx(84_000.0)
        assert data["current_equity"] == pytest.approx(10_000.0)
        assert data["cap_usdt"] == pytest.approx(10_000.0)

    def test_return_pct_is_on_research_capital(self):
        ledger = started(usdt=10_000.0)
        ledger.update_pnl(realized_pnl=1_000.0)
        # 10% of research capital, not 1.2% of the $84,000 account.
        assert ledger.snapshot().return_pct == pytest.approx(0.10)

    def test_a_zero_start_does_not_divide_by_zero(self):
        ledger = ResearchEquityLedger(cap_usdt=CAP)
        ledger.start(balance(usdt=0.0, btc_usd=84_000.0, eth_usd=0.0))
        assert ledger.snapshot().return_pct == 0.0
        assert ledger.snapshot().drawdown_pct == 0.0
