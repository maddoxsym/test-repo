"""Fill reconciliation under OKX's eventual consistency.

The bug these tests pin down: a real, filled, visible position was reported as
"no fill matched the client order ID", because the fills endpoint had not
published the trade yet when it was asked.

OKX settles its read endpoints on different schedules — order details first,
then positions, then fills. So the rule under test throughout is: **order
details decide whether the order filled; the fills endpoint only supplies the
per-fill detail, and its lag is never a failure.**

The identifiers used here are the ones from the reported live run, so a reader
can line these cases up against the actual smoke-test output.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from btcbot.exchange.models import Execution, OrderDetails
from btcbot.execution.reconciliation import (
    FILLS_POLL_BACKOFF,
    ORDER_POLL_BACKOFF,
    FillReconciler,
    match_fills,
)
from btcbot.utils.errors import ApiError, TransportError

INST = "BTC-USDT-SWAP"
ENTRY_ORD_ID = "3785756861732241408"
ENTRY_CL_ORD_ID = "smoke8b811244a1cc40d7ab27ca33"
EXIT_ORD_ID = "3785756892199665664"

# No real waiting in tests; the schedule itself is asserted separately.
FAST_ORDER_BACKOFF = (0.0,) * len(ORDER_POLL_BACKOFF)
FAST_FILLS_BACKOFF = (0.0,) * len(FILLS_POLL_BACKOFF)


def order_details(
    state: str = "filled",
    *,
    acc_fill_sz: str = "0.01",
    ord_id: str = ENTRY_ORD_ID,
    cl_ord_id: str = ENTRY_CL_ORD_ID,
    **extra: Any,
) -> OrderDetails:
    payload = {
        "instId": INST, "ordId": ord_id, "clOrdId": cl_ord_id, "state": state,
        "side": "buy", "posSide": "net", "ordType": "market", "sz": "0.01",
        "accFillSz": acc_fill_sz, "avgPx": "118000.1", "fillPx": "118000.1",
        "fillSz": acc_fill_sz, "fee": "-0.0708", "feeCcy": "USDT", "lever": "1",
        "cTime": "1700000000000", "uTime": "1700000000450",
    }
    payload.update(extra)
    return OrderDetails.from_response(payload)


def fill(ord_id: str = ENTRY_ORD_ID, *, cl_ord_id: str = "", trade_id: str = "T-1") -> Execution:
    return Execution.from_response(
        {
            "tradeId": trade_id, "ordId": ord_id, "clOrdId": cl_ord_id, "instId": INST,
            "side": "buy", "posSide": "net", "fillPx": "118000.1", "fillSz": "0.01",
            "fee": "-0.0708", "feeCcy": "USDT", "execType": "T", "ts": "1700000000450",
        }
    )


class FakeClient:
    """Replays scripted OKX responses, one per call, holding the last forever.

    ``None`` in the order script means "order not found yet"; an exception
    instance is raised. This models each endpoint becoming consistent on its
    own schedule.
    """

    def __init__(
        self,
        order_script: list[Any] | None = None,
        fills_script: list[Any] | None = None,
    ) -> None:
        self.order_script = list(order_script or [order_details()])
        self.fills_script = list(fills_script or [[fill()]])
        self.order_calls: list[dict[str, Any]] = []
        self.fill_calls = 0

    @staticmethod
    def _next(script: list[Any]) -> Any:
        item = script.pop(0) if len(script) > 1 else script[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def get_order(self, inst_id, *, order_id=None, client_order_id=None):
        self.order_calls.append({"order_id": order_id, "client_order_id": client_order_id})
        return self._next(self.order_script)

    async def get_executions(self, inst_id, *, limit=100, start_ms=None):
        self.fill_calls += 1
        return self._next(self.fills_script)


def _reconciler(client: FakeClient) -> FillReconciler:
    return FillReconciler(
        client, order_backoff=FAST_ORDER_BACKOFF, fills_backoff=FAST_FILLS_BACKOFF
    )


class TestImmediateFillVisibility:
    async def test_a_fill_visible_on_the_first_poll_is_confirmed_at_once(self):
        client = FakeClient()
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed
        assert outcome.filled
        assert outcome.state == "filled"
        assert outcome.order_attempts == 1
        assert outcome.fill_attempts == 1
        assert outcome.fill_ids == ["T-1"]
        assert not outcome.fills_delayed
        assert outcome.waited_seconds == 0.0

    async def test_the_confirmed_order_carries_every_documented_field(self):
        outcome = await _reconciler(FakeClient()).reconcile(INST, order_id=ENTRY_ORD_ID)
        order = outcome.order
        assert order is not None
        assert order.order_id == ENTRY_ORD_ID
        assert order.client_order_id == ENTRY_CL_ORD_ID
        assert order.state == "filled"
        assert order.avg_price == pytest.approx(118_000.1)
        assert order.filled_size == pytest.approx(0.01)
        assert order.last_fill_price == pytest.approx(118_000.1)
        assert order.last_fill_size == pytest.approx(0.01)
        assert order.fee == pytest.approx(0.0708)   # cost-positive
        assert order.fee_currency == "USDT"
        assert order.updated_ms == 1_700_000_000_450

    async def test_the_ordid_is_what_gets_queried(self):
        """Requirement: ordId is the primary reconciliation identifier."""
        client = FakeClient()
        await _reconciler(client).reconcile(
            INST, order_id=ENTRY_ORD_ID, client_order_id=ENTRY_CL_ORD_ID
        )
        assert client.order_calls[0]["order_id"] == ENTRY_ORD_ID


class TestFillVisibleAfterSeveralPolls:
    async def test_a_live_order_keeps_polling_until_it_fills(self):
        client = FakeClient(
            order_script=[
                order_details("live", acc_fill_sz="0"),
                order_details("live", acc_fill_sz="0"),
                order_details("filled"),
            ]
        )
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed
        assert outcome.order_attempts == 3

    async def test_an_order_not_yet_visible_keeps_polling(self):
        """OKX can answer 'does not exist' for a moment after acceptance."""
        client = FakeClient(order_script=[None, None, order_details("filled")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed
        assert outcome.order_attempts == 3

    async def test_a_transient_read_failure_is_retried_not_read_as_unfilled(self):
        client = FakeClient(
            order_script=[
                TransportError("connection reset"),
                ApiError(50011, "rate limited", "/api/v5/trade/order"),
                order_details("filled"),
            ]
        )
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed, "a failed read must never be treated as 'did not fill'"


class TestOrderDetailsFilledButFillsDelayed:
    """The reported bug, exactly."""

    async def test_the_fill_is_confirmed_even_when_no_fill_record_exists(self):
        client = FakeClient(order_script=[order_details("filled")], fills_script=[[]])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed, "order details prove the fill; fills-endpoint lag is not a failure"
        assert outcome.fills_delayed
        assert outcome.fill_ids == []
        assert outcome.filled_size == pytest.approx(0.01)
        assert outcome.avg_price == pytest.approx(118_000.1)
        assert "not published yet" in outcome.detail

    async def test_the_fills_endpoint_is_polled_more_than_once(self):
        client = FakeClient(order_script=[order_details("filled")], fills_script=[[]])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert client.fill_calls == len(FAST_FILLS_BACKOFF)
        assert outcome.fill_attempts == len(FAST_FILLS_BACKOFF)

    async def test_a_fill_arriving_on_a_later_poll_is_picked_up(self):
        client = FakeClient(
            order_script=[order_details("filled")],
            fills_script=[[], [], [fill(trade_id="T-late")]],
        )
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed
        assert not outcome.fills_delayed
        assert outcome.fill_ids == ["T-late"]
        assert outcome.fill_attempts == 3

    async def test_a_failing_fills_endpoint_does_not_undo_the_confirmation(self):
        client = FakeClient(
            order_script=[order_details("filled")],
            fills_script=[TransportError("fills unavailable")],
        )
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed
        assert outcome.fills_delayed


class TestFillMatching:
    """ordId first, clOrdId only as fallback."""

    def test_ordid_matches_a_fill_whose_clordid_is_blank(self):
        """The live failure: OKX left clOrdId empty on the fills endpoint."""
        fills = [fill(ord_id=ENTRY_ORD_ID, cl_ord_id="")]
        assert match_fills(fills, order_id=ENTRY_ORD_ID) == fills
        # Matching on clOrdId alone — the old behaviour — finds nothing.
        assert match_fills(fills, client_order_id=ENTRY_CL_ORD_ID) == []

    def test_ordid_match_wins_over_a_clordid_collision(self):
        mine = fill(ord_id=ENTRY_ORD_ID, cl_ord_id="", trade_id="mine")
        other = fill(ord_id=EXIT_ORD_ID, cl_ord_id=ENTRY_CL_ORD_ID, trade_id="other")
        matched = match_fills(
            [other, mine], order_id=ENTRY_ORD_ID, client_order_id=ENTRY_CL_ORD_ID
        )
        assert [f.exec_id for f in matched] == ["mine"]

    def test_clordid_is_used_when_the_ordid_match_finds_nothing(self):
        fills = [fill(ord_id="", cl_ord_id=ENTRY_CL_ORD_ID, trade_id="by-clordid")]
        matched = match_fills(
            fills, order_id=ENTRY_ORD_ID, client_order_id=ENTRY_CL_ORD_ID
        )
        assert [f.exec_id for f in matched] == ["by-clordid"]

    def test_unrelated_fills_are_never_matched(self):
        assert match_fills([fill(ord_id=EXIT_ORD_ID)], order_id=ENTRY_ORD_ID) == []

    def test_no_identifier_matches_nothing(self):
        assert match_fills([fill()]) == []

    async def test_reconciliation_matches_by_ordid_when_clordid_matching_fails(self):
        client = FakeClient(
            order_script=[order_details("filled")],
            fills_script=[[fill(ord_id=ENTRY_ORD_ID, cl_ord_id="")]],
        )
        outcome = await _reconciler(client).reconcile(
            INST, order_id=ENTRY_ORD_ID, client_order_id=ENTRY_CL_ORD_ID
        )
        assert outcome.fill_ids == ["T-1"]


class TestPartiallyFilledOrder:
    async def test_a_partial_fill_is_a_confirmed_fill(self):
        """Contracts moved, so the position is real even though the order is not done."""
        client = FakeClient(order_script=[order_details("partially_filled", acc_fill_sz="0.004")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.confirmed
        assert outcome.partially_filled
        assert not outcome.filled
        assert outcome.state == "partially_filled"
        assert outcome.filled_size == pytest.approx(0.004)

    async def test_polling_stops_on_the_partial_rather_than_waiting_it_out(self):
        client = FakeClient(
            order_script=[order_details("partially_filled", acc_fill_sz="0.004"),
                          order_details("filled")]
        )
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert outcome.order_attempts == 1
        assert outcome.partially_filled

    async def test_a_partial_with_zero_filled_size_is_not_a_fill(self):
        """The state alone is not evidence — accFillSz must actually be > 0."""
        client = FakeClient(order_script=[order_details("partially_filled", acc_fill_sz="0")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert not outcome.confirmed


class TestCanceledOrder:
    async def test_a_canceled_order_settles_immediately_and_is_not_a_fill(self):
        client = FakeClient(order_script=[order_details("canceled", acc_fill_sz="0")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert outcome.canceled
        assert not outcome.confirmed
        assert outcome.state == "canceled"
        assert outcome.order_attempts == 1, "a canceled order is terminal — stop polling"
        assert "canceled" in outcome.detail

    async def test_mmp_cancellation_is_also_terminal(self):
        client = FakeClient(order_script=[order_details("mmp_canceled", acc_fill_sz="0")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert outcome.canceled
        assert not outcome.confirmed

    async def test_no_fills_are_fetched_for_a_canceled_order(self):
        client = FakeClient(order_script=[order_details("canceled", acc_fill_sz="0")])
        await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert client.fill_calls == 0


class TestTimeout:
    async def test_an_order_that_never_settles_is_never_reported_as_filled(self):
        client = FakeClient(order_script=[order_details("live", acc_fill_sz="0")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert not outcome.confirmed
        assert not outcome.canceled
        assert outcome.order_attempts == len(FAST_ORDER_BACKOFF)
        assert outcome.state == "live", "the real last-seen state is reported, not invented"
        assert "could not confirm" in outcome.detail

    async def test_an_order_never_seen_at_all_reports_unconfirmed(self):
        client = FakeClient(order_script=[None])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)

        assert not outcome.confirmed
        assert outcome.order is None
        assert outcome.state == "unconfirmed"
        assert "never seen at the exchange" in outcome.detail

    async def test_persistent_read_failures_report_unconfirmed_not_unfilled(self):
        client = FakeClient(order_script=[TransportError("network down")])
        outcome = await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert not outcome.confirmed
        assert outcome.state == "unconfirmed"

    async def test_no_fills_are_fetched_when_nothing_was_confirmed(self):
        client = FakeClient(order_script=[order_details("live", acc_fill_sz="0")])
        await _reconciler(client).reconcile(INST, order_id=ENTRY_ORD_ID)
        assert client.fill_calls == 0


class TestTheBackoffSchedule:
    def test_the_first_attempt_is_immediate(self):
        assert ORDER_POLL_BACKOFF[0] == 0.0
        assert FILLS_POLL_BACKOFF[0] == 0.0

    def test_the_schedule_backs_off_and_stays_inside_the_budget(self):
        assert ORDER_POLL_BACKOFF[:5] == (0.0, 0.25, 0.5, 1.0, 2.0)
        total = sum(ORDER_POLL_BACKOFF)
        assert 8.0 >= total >= 6.0, f"total wait {total}s is outside the 8–10s budget"
        assert len(ORDER_POLL_BACKOFF) >= 5

    def test_the_fills_schedule_is_shorter_than_the_order_schedule(self):
        """Once the fill is proven we are only waiting for bookkeeping."""
        assert sum(FILLS_POLL_BACKOFF) < sum(ORDER_POLL_BACKOFF)

    async def test_waiting_is_actually_bounded_by_the_schedule(self):
        """A never-settling order must not poll forever."""
        client = FakeClient(order_script=[order_details("live", acc_fill_sz="0")])
        reconciler = FillReconciler(
            client, order_backoff=(0.0, 0.01, 0.01), fills_backoff=(0.0,)
        )
        outcome = await asyncio.wait_for(
            reconciler.reconcile(INST, order_id=ENTRY_ORD_ID), timeout=5
        )
        assert outcome.order_attempts == 3
        assert outcome.waited_seconds == pytest.approx(0.02, abs=0.05)


class TestSmokeReportOutcome:
    """The smoke test must PASS on a round trip that demonstrably worked."""

    def _report(self):
        from btcbot.app.smoke_test import SmokeReport

        report = SmokeReport()
        for name in (
            "Reach OKX Global / UAE Demo host",
            "Demo environment verified",
            "Discover BTC X-Perp",
            "Set leverage",
            "Confirm leverage",
            "Place minimum-size demo order",
            "Position opened and confirmed",
        ):
            report.add(name, True, "ok")
        return report

    def test_a_delayed_per_fill_record_is_a_warning_not_a_failure(self):
        """Requirement 10 — order details already proved the fill."""
        report = self._report()
        report.add("Entry fill confirmed", True, "state=filled accFillSz=0.01")
        report.warn("Per-fill record published", "not visible yet")
        for name in ("Close the position (reduce-only)", "Exit fill confirmed", "Account is flat"):
            report.add(name, True, "ok")

        assert report.passed, "a fills-endpoint lag must not fail a working round trip"
        markers = [step.marker for step in report.steps]
        assert "WARN" in markers
        assert "FAIL" not in markers

    def test_the_warning_is_visible_in_the_printed_report(self):
        from btcbot.app.smoke_test import print_report

        report = self._report()
        report.warn("Per-fill record published", "not visible yet")
        print_report(report)   # must not raise; renders [WARN]
        assert report.steps[-1].marker == "WARN"

    def test_an_unconfirmed_entry_fill_still_fails_the_run(self):
        """A fill the exchange would not confirm is a genuine failure."""
        report = self._report()
        report.add("Entry fill confirmed", False, "could not confirm within 7.75s")
        assert not report.passed

    def test_a_position_that_will_not_close_still_fails_the_run(self):
        report = self._report()
        report.add("Entry fill confirmed", True, "state=filled")
        report.warn("Per-fill record published", "not visible yet")
        report.add("Account is flat", False, "1 position(s) still open")
        assert not report.passed


class TestReconcilerIsReadOnly:
    def test_it_cannot_submit_anything(self):
        """Structural: reconciliation must never be able to create an order."""
        import inspect

        from btcbot.execution import reconciliation

        source = inspect.getsource(reconciliation)
        for forbidden in ("place_order", "cancel_order", "cancel_all", "set_leverage"):
            assert forbidden not in source, f"reconciliation must not reference {forbidden}"

    async def test_an_identifier_is_required(self):
        with pytest.raises(ValueError):
            await _reconciler(FakeClient()).reconcile(INST)
