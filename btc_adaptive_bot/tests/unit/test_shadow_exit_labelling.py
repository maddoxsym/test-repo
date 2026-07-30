"""A `take_profit` exit recorded at negative R must explain itself.

The observation that prompted this: a shadow log line read

    <strategy> exited take_profit -0.24R

which looks like a contradiction. It is not — the two numbers describe
different scopes:

* ``exit_reason`` names the leg that **closed** the trade (the final one)
* ``pnl`` / ``r_multiple`` are the **whole trade**, summed over every leg and
  net of fees, slippage and spread

So a trade whose last leg touched the target can still be negative overall if
an earlier partial exited at a loss, or if costs outweighed a thin final leg.
That is correct accounting, but it was silent — the reader had to reconstruct
it. These tests pin both halves: the arithmetic, and the explanation that now
accompanies it.
"""

from __future__ import annotations

import logging

import pytest

from btcbot.backtesting.execution_model import ExitEvent, ExitReason, SimulatedPosition
from btcbot.config.schema import ShadowConfig
from btcbot.shadow.account import ShadowAccount
from btcbot.shadow.engine import ShadowEngine
from btcbot.strategies.base import Direction

STRATEGY = "s_test"
ENTRY = 100.0
STOP = 90.0          # $10 of risk per unit
QUANTITY = 10.0      # → initial risk of $100


class NullShadowRepo:
    """The engine persists; this test is about what it *computes*."""

    def load_accounts(self, experiment_id):
        return {}

    def upsert_account(self, row):
        pass

    def open_trade(self, row):
        pass

    def close_trade(self, trade_id, fields):
        self.closed = fields


def engine_with_position(
    *, target: float = 130.0, config: ShadowConfig | None = None
) -> tuple[ShadowEngine, ShadowAccount]:
    # Zero costs by default so each test controls exactly one variable.
    config = config or ShadowConfig(
        fee_rate_taker=0.0, fee_rate_maker=0.0, slippage_bps=0.0, funding_rate_8h=0.0
    )
    engine = ShadowEngine(
        config, NullShadowRepo(), experiment_id="exp_test", symbol="BTC-USDT-SWAP"
    )
    account = ShadowAccount.create(STRATEGY, "exp_test", initial_equity=10_000.0)
    account.open_position = SimulatedPosition(
        strategy_id=STRATEGY,
        strategy_version=1,
        direction=Direction.LONG,
        symbol="BTC-USDT-SWAP",
        timeframe="15",
        entry_price=ENTRY,
        initial_stop=STOP,
        stop_price=STOP,
        target_price=target,
        quantity=QUANTITY,
        remaining_quantity=QUANTITY,
        entry_bar_ms=1_700_000_000_000,
        entry_index=0,
        exit_policy=None,
        atr_at_entry=1.0,
        confidence=0.6,
        entry_regime="trend",
        setup_key="k",
        news_state=None,
    )
    engine.accounts[STRATEGY] = account
    return engine, account


def exit_leg(engine, account, *, reason, price, quantity, partial):
    return engine._apply_exit(   # noqa: SLF001
        account,
        account.open_position,
        ExitEvent(reason, price, quantity),
        None,
    ) if not partial else engine._apply_exit(   # noqa: SLF001
        account,
        account.open_position,
        ExitEvent(reason, price, quantity, is_partial=True),
        None,
    )


class TestTheLabelAndTheNumberDescribeDifferentScopes:
    def test_a_losing_partial_can_drag_a_take_profit_trade_negative(self):
        """The exact shape of the `take_profit -0.24R` line."""
        engine, account = engine_with_position()
        position = account.open_position

        # Leg 1: 6 of the 10 units scratched out below entry — -$36.
        assert exit_leg(
            engine, account, reason=ExitReason.PARTIAL, price=94.0,
            quantity=10.0 * 0.6, partial=True,
        ) is None
        # Leg 2: the remaining 4 units reach a modest target — +$4.
        # Whole trade: -$32 on $100 of initial risk = -0.32R, labelled
        # `take_profit` because that is what closed it.
        record = exit_leg(
            engine, account, reason=ExitReason.TAKE_PROFIT, price=101.0,
            quantity=position.remaining_quantity, partial=False,
        )

        assert record is not None
        assert record["exit_reason"] == "take_profit", "the closing leg was the target"
        assert record["final_leg_pnl"] > 0, "that leg was profitable"
        assert record["pnl"] < 0, "the trade as a whole was not"
        assert record["r_multiple"] == pytest.approx(-0.32)
        # The decomposition adds up to the recorded whole-trade PnL.
        assert record["final_leg_pnl"] + record["partials_pnl"] == pytest.approx(
            record["pnl"]
        )

    def test_costs_alone_can_flip_a_thin_final_leg(self, caplog):
        """No partials at all — slippage and fees sink a marginal target."""
        caplog.set_level(logging.DEBUG)
        costly = ShadowConfig(
            fee_rate_taker=0.0005, fee_rate_maker=0.0002, slippage_bps=20.0,
            funding_rate_8h=0.0,
        )
        engine, account = engine_with_position(target=100.05, config=costly)

        record = exit_leg(
            engine, account, reason=ExitReason.TAKE_PROFIT, price=100.05,
            quantity=QUANTITY, partial=False,
        )

        assert record["exit_reason"] == "take_profit"
        assert record["partials_pnl"] == 0.0, "there were no partials to blame"
        assert record["pnl"] < 0, "costs outweighed the 0.05 of favourable movement"
        assert record["costs"] > 0
        # This is the case a sign comparison alone would miss: the final leg is
        # negative too, so it "agrees" with the total — but a take_profit at
        # negative R still needs its working shown.
        assert "whole trade differs from the exit leg" in caplog.text

    def test_a_clean_take_profit_is_positive_and_needs_no_caveat(self, caplog):
        caplog.set_level(logging.DEBUG)
        engine, account = engine_with_position()

        record = exit_leg(
            engine, account, reason=ExitReason.TAKE_PROFIT, price=130.0,
            quantity=QUANTITY, partial=False,
        )

        assert record["r_multiple"] > 0
        assert "whole trade differs" not in caplog.text, "an ordinary exit was annotated"


class TestTheDiscrepancyIsExplainedNotSilent:
    def test_the_log_line_states_the_arithmetic(self, caplog):
        caplog.set_level(logging.DEBUG)
        engine, account = engine_with_position()
        position = account.open_position

        exit_leg(
            engine, account, reason=ExitReason.PARTIAL, price=94.0,
            quantity=10.0 * 0.6, partial=True,
        )
        exit_leg(
            engine, account, reason=ExitReason.TAKE_PROFIT, price=101.0,
            quantity=position.remaining_quantity, partial=False,
        )

        assert "take_profit" in caplog.text
        assert "whole trade differs from the exit leg" in caplog.text
        assert "final leg" in caplog.text
        assert "earlier partials" in caplog.text
        assert "costs" in caplog.text

    def test_the_record_carries_the_breakdown_for_the_reports(self):
        engine, account = engine_with_position()
        position = account.open_position

        exit_leg(
            engine, account, reason=ExitReason.PARTIAL, price=94.0,
            quantity=10.0 * 0.6, partial=True,
        )
        record = exit_leg(
            engine, account, reason=ExitReason.TAKE_PROFIT, price=101.0,
            quantity=position.remaining_quantity, partial=False,
        )

        for key in ("final_leg_pnl", "partials_pnl", "costs"):
            assert key in record, key
        assert record["partials_pnl"] < 0, "the partial was the losing leg"
