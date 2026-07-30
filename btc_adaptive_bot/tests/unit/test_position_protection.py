"""No real position may stay open without a verified exchange-side stop.

The incident: the bot opened a real OKX Demo position and logged `stop_loss`
and `take_profit` levels — but those existed only in Python. The exchange had
no protection at all. `OrderRequest` had working `sl_trigger_price` /
`tp_trigger_price` fields; the entry path simply never set them.

Every test here asserts the same invariant from a different angle:

    protection is what the EXCHANGE confirms, never what a Python object holds.

So the fake exchange below can accept a placement and still not register it —
`algo_visible=False` — which is exactly the failure a "did the API call
succeed?" check would miss, and the one that leaves a naked position.
"""

from __future__ import annotations

import pytest

from btcbot.exchange.models import AlgoOrder, PositionMode, Side
from btcbot.execution.protection import (
    PositionProtector,
    ProtectionRegistry,
    ProtectionState,
    ProtectionStatus,
    protective_side,
    stop_is_on_the_correct_side,
)
from btcbot.strategies.base import Direction
from btcbot.utils.errors import ApiError, TransportError

INST = "BTC-USDT-SWAP"
ENTRY = 118_000.0
STOP = 117_000.0
TARGET = 120_000.0
SIZE = 0.5  # contracts; the fixture instrument trades in lots of 0.1

NO_WAIT = (0.0, 0.0, 0.0)


class FakeExchange:
    """OKX algo orders, including the ways they silently fail to exist."""

    def __init__(
        self,
        *,
        register: bool = True,
        place_error: Exception | None = None,
        read_error: Exception | None = None,
        register_after: int = 1,
        size_override: float | None = None,
    ) -> None:
        self.register = register
        self.place_error = place_error
        self.read_error = read_error
        self.register_after = register_after
        self.size_override = size_override
        self.placements: list[dict] = []
        self.cancelled: list[str] = []
        self.reads = 0
        self._live: list[AlgoOrder] = []

    async def place_algo_order(
        self, inst_id, *, side, size, td_mode="isolated", pos_side=None,
        sl_trigger_price=None, tp_trigger_price=None, reduce_only=True,
        client_algo_id=None,
    ):
        self.placements.append(
            {
                "inst_id": inst_id, "side": side, "size": size, "pos_side": pos_side,
                "sl": sl_trigger_price, "tp": tp_trigger_price,
                "reduce_only": reduce_only, "client_algo_id": client_algo_id,
            }
        )
        if self.place_error is not None:
            raise self.place_error
        if self.register:
            self._live.append(
                AlgoOrder.from_response(
                    {
                        "algoId": f"algo-{len(self.placements)}",
                        "algoClOrdId": client_algo_id or "",
                        "instId": inst_id,
                        "ordType": "oco" if (sl_trigger_price and tp_trigger_price)
                                   else "conditional",
                        "state": "live", "side": side.value, "posSide": pos_side or "net",
                        "sz": str(self.size_override if self.size_override is not None else size),
                        "reduceOnly": "true",
                        "slTriggerPx": sl_trigger_price or "0",
                        "tpTriggerPx": tp_trigger_price or "0",
                        "cTime": "1700000000000",
                    }
                )
            )
        return self._live[-1] if self._live else None

    async def get_protective_orders(self, inst_id):
        self.reads += 1
        if self.read_error is not None:
            raise self.read_error
        if self.reads < self.register_after:
            return []
        return list(self._live)

    async def get_algo_orders(self, inst_id, *, order_type="oco"):
        return [o for o in await self.get_protective_orders(inst_id)
                if o.order_type == order_type]

    async def cancel_algo_orders(self, inst_id, algo_ids, *, order_type="oco"):
        self.cancelled.extend(algo_ids)
        self._live = [o for o in self._live if o.algo_id not in algo_ids]
        return []


def protector(exchange) -> PositionProtector:
    return PositionProtector(exchange, verify_backoff=NO_WAIT, place_attempts=2)


async def protect(exchange, instrument, **overrides) -> ProtectionState:
    kwargs = dict(
        direction=Direction.LONG, filled_size=SIZE, entry_price=ENTRY,
        stop_price=STOP, target_price=TARGET, position_mode=PositionMode.NET,
    )
    kwargs.update(overrides)
    return await protector(exchange).protect(instrument, **kwargs)


class TestProtectionRequiresExchangeConfirmation:
    """The core invariant."""

    async def test_a_registered_oco_is_reported_protected(self, perp_instrument):
        exchange = FakeExchange()
        state = await protect(exchange, perp_instrument)

        assert state.protected
        assert state.status is ProtectionStatus.PROTECTED
        assert state.algo_id == "algo-1"
        assert state.sl_trigger_price == pytest.approx(STOP)
        assert state.tp_trigger_price == pytest.approx(TARGET)
        assert state.verified_at is not None

    async def test_an_accepted_but_unregistered_order_is_NOT_protected(
        self, perp_instrument
    ):
        """The incident's shape: the API said yes, the exchange holds nothing."""
        exchange = FakeExchange(register=False)
        state = await protect(exchange, perp_instrument)

        assert not state.protected
        assert state.status is ProtectionStatus.UNPROTECTED
        assert exchange.placements, "it did try to place protection"
        assert "no live algo order" in state.detail

    async def test_internal_stop_values_never_imply_protection(self, perp_instrument):
        """A ProtectionState is only ever built from an exchange read."""
        exchange = FakeExchange(register=False)
        state = await protect(exchange, perp_instrument, stop_price=STOP)

        # The stop price we asked for is nowhere in the resulting state.
        assert state.sl_trigger_price == 0.0
        assert not state.protected

    async def test_a_read_failure_is_not_reported_as_protected(self, perp_instrument):
        exchange = FakeExchange(read_error=TransportError("network down"))
        with pytest.raises(TransportError):
            await protector(exchange).verify(INST)

    async def test_verification_retries_a_late_registration(self, perp_instrument):
        """OKX registers an algo order a beat after accepting it."""
        exchange = FakeExchange(register_after=3)
        state = await protect(exchange, perp_instrument)

        assert state.protected
        assert exchange.reads >= 3

    async def test_placement_is_retried_before_giving_up(self, perp_instrument):
        exchange = FakeExchange(register=False)
        await protect(exchange, perp_instrument)
        assert len(exchange.placements) == 2, "the bounded retry did not happen"

    async def test_a_rejected_placement_is_not_protection(self, perp_instrument):
        """OKX refusing the algo order leaves the position naked — say so."""
        exchange = FakeExchange(place_error=ApiError("51000", "algo order rejected"))
        state = await protect(exchange, perp_instrument)

        assert not state.protected
        assert state.status is ProtectionStatus.UNPROTECTED
        assert "algo order rejected" in state.detail

    async def test_a_transport_failure_while_placing_is_not_protection(
        self, perp_instrument
    ):
        exchange = FakeExchange(place_error=TransportError("connection reset"))
        state = await protect(exchange, perp_instrument)

        assert not state.protected
        assert len(exchange.placements) == 2, "the bounded retry did not happen"


class TestProtectionShape:
    async def test_the_stop_is_placed_reduce_only(self, perp_instrument):
        """Protection may only ever close a position, never open one."""
        exchange = FakeExchange()
        await protect(exchange, perp_instrument)
        assert exchange.placements[0]["reduce_only"] is True

    async def test_a_long_is_protected_by_a_sell(self, perp_instrument):
        exchange = FakeExchange()
        await protect(exchange, perp_instrument, direction=Direction.LONG)
        assert exchange.placements[0]["side"] is Side.SELL

    async def test_a_short_is_protected_by_a_buy(self, perp_instrument):
        exchange = FakeExchange()
        await protect(
            exchange, perp_instrument, direction=Direction.SHORT,
            stop_price=119_000.0, target_price=116_000.0,
        )
        assert exchange.placements[0]["side"] is Side.BUY

    def test_protective_side_closes_the_position(self):
        assert protective_side(Direction.LONG) is Side.SELL
        assert protective_side(Direction.SHORT) is Side.BUY

    async def test_both_legs_produce_one_oco_not_two_orders(self, perp_instrument):
        """Requirement: closing one protection order cancels the other."""
        exchange = FakeExchange()
        state = await protect(exchange, perp_instrument)

        assert len(exchange.placements) == 1, "TP and SL were placed separately"
        assert state.mode == "oco"
        placement = exchange.placements[0]
        assert placement["sl"] and placement["tp"]

    async def test_the_size_matches_the_filled_quantity(self, perp_instrument):
        exchange = FakeExchange()
        await protect(exchange, perp_instrument, filled_size=1.2)
        assert float(exchange.placements[0]["size"]) == pytest.approx(1.2)

    async def test_a_ragged_fill_is_covered_by_rounding_up(self, perp_instrument):
        """Never leave a remainder naked — round the protective size *up*.

        Rounding down would under-cover the position, and a fill below one lot
        would round to zero, i.e. no protection at all.
        """
        exchange = FakeExchange()
        state = await protect(exchange, perp_instrument, filled_size=0.44)

        assert state.protected
        assert float(exchange.placements[0]["size"]) == pytest.approx(0.5)

    async def test_a_position_of_no_size_is_never_protected(self, perp_instrument):
        exchange = FakeExchange()
        state = await protect(exchange, perp_instrument, filled_size=0.0)

        assert not state.protected
        assert exchange.placements == []

    async def test_a_size_mismatch_is_not_full_protection(self, perp_instrument):
        """An OCO covering half the position leaves the other half naked."""
        exchange = FakeExchange(size_override=0.2)
        state = await protect(exchange, perp_instrument, filled_size=SIZE)

        assert state.status is ProtectionStatus.SL_ONLY
        assert "does not match the filled position size" in state.detail

    async def test_protection_uses_the_filled_price_not_the_reference(
        self, perp_instrument
    ):
        """Requirement 3: the stop is placed around what was actually paid."""
        exchange = FakeExchange()
        await protect(
            exchange, perp_instrument, entry_price=118_500.0, stop_price=117_500.0
        )
        assert float(exchange.placements[0]["sl"]) == pytest.approx(117_500.0)


class TestStopSanity:
    def test_a_long_stop_must_be_below_entry(self):
        assert stop_is_on_the_correct_side(Direction.LONG, entry_price=100.0, stop_price=99.0)
        assert not stop_is_on_the_correct_side(
            Direction.LONG, entry_price=100.0, stop_price=101.0
        )

    def test_a_short_stop_must_be_above_entry(self):
        assert stop_is_on_the_correct_side(Direction.SHORT, entry_price=100.0, stop_price=101.0)
        assert not stop_is_on_the_correct_side(
            Direction.SHORT, entry_price=100.0, stop_price=99.0
        )

    async def test_an_inverted_stop_is_refused_rather_than_placed(self, perp_instrument):
        """It would trigger instantly and market-exit at a loss."""
        exchange = FakeExchange()
        state = await protect(exchange, perp_instrument, stop_price=ENTRY + 500)

        assert not state.protected
        assert exchange.placements == [], "an instantly-triggering stop was sent"
        assert "trigger immediately" in state.detail


class TestNoDuplicateProtection:
    async def test_an_already_protected_position_is_not_re_protected(
        self, perp_instrument
    ):
        exchange = FakeExchange()
        first = await protect(exchange, perp_instrument)
        assert first.protected

        second = await protect(exchange, perp_instrument)

        assert second.protected
        assert len(exchange.placements) == 1, "a duplicate OCO was placed"
        assert second.algo_id == first.algo_id


class TestPartialProtection:
    async def test_a_stop_without_a_target_is_sl_only(self, perp_instrument):
        """Requirement 8: keep the SL, but do not claim full protection."""
        exchange = FakeExchange()
        state = await protect(exchange, perp_instrument, target_price=None)

        assert state.protected, "the stop is real — the position is safe"
        assert state.status is ProtectionStatus.SL_ONLY
        assert not state.has_take_profit
        assert state.sl_trigger_price == pytest.approx(STOP)

    def test_sl_only_still_counts_as_having_a_stop(self):
        assert ProtectionStatus.SL_ONLY.has_stop
        assert ProtectionStatus.PROTECTED.has_stop
        assert not ProtectionStatus.UNPROTECTED.has_stop


class TestVerifiedLogging:
    async def test_the_mandated_lines_are_logged_only_after_verification(
        self, perp_instrument, caplog
    ):
        import logging

        caplog.set_level(logging.INFO)
        await protect(FakeExchange(), perp_instrument)

        assert "SL submitted and verified" in caplog.text
        assert "TP submitted and verified" in caplog.text

    async def test_nothing_is_claimed_verified_when_it_is_not(
        self, perp_instrument, caplog
    ):
        import logging

        caplog.set_level(logging.INFO)
        await protect(FakeExchange(register=False), perp_instrument)

        assert "SL submitted and verified" not in caplog.text
        assert "TP submitted and verified" not in caplog.text

    async def test_a_missing_tp_is_reported_as_an_error(self, perp_instrument, caplog):
        import logging

        caplog.set_level(logging.INFO)
        await protect(FakeExchange(), perp_instrument, target_price=None)

        assert "SL submitted and verified" in caplog.text
        assert "TP NOT verified" in caplog.text


class TestRegistry:
    def test_it_tracks_and_forgets(self):
        registry = ProtectionRegistry()
        assert registry.all_protected(), "no positions means nothing unprotected"

        registry.record(ProtectionState.unprotected(INST, "none found"))
        assert not registry.all_protected()

        registry.forget(INST)
        assert registry.all_protected()

    def test_the_snapshot_exposes_the_dashboard_fields(self):
        """Requirement 13."""
        registry = ProtectionRegistry()
        registry.record(
            ProtectionState(
                status=ProtectionStatus.PROTECTED, inst_id=INST, algo_id="algo-1",
                sl_trigger_price=STOP, tp_trigger_price=TARGET, size=SIZE, mode="oco",
                verified_at=__import__("btcbot.utils.timeutil", fromlist=["now_utc"]).now_utc(),
            )
        )
        snapshot = registry.snapshot()
        position = snapshot["positions"][0]

        assert snapshot["all_protected"] is True
        for key in ("status", "sl_order_id", "sl_price", "tp_order_id", "tp_price",
                    "verified_at"):
            assert key in position, key
        assert position["sl_order_id"] == "algo-1"
        assert position["sl_price"] == pytest.approx(STOP)
        assert position["verified_at"] is not None


class TestProtectionIsStructurallyReduceOnly:
    def test_the_module_never_opens_a_position(self):
        """Protection code must be incapable of increasing exposure."""
        import inspect

        from btcbot.execution import protection

        source = inspect.getsource(protection)
        assert "reduce_only=True" in source
        # It places algo orders and cancels them; it never sends a plain order.
        assert "place_order(" not in source
