"""After a restart, no open position may survive without an exchange stop.

The incident that motivates this file: the bot held a real OKX Demo position
whose stop existed only in Python. A restart makes that state *worse* — the
in-memory stop is gone entirely, and the position is left running with nothing
watching it at all.

So on every boot, before trading resumes, the orchestrator asks OKX (never the
ledger) whether each open position has a live stop, and drives every position
to one of exactly two end states:

    * a verified exchange-side stop exists, or
    * the position is closed and SAFE_MODE is entered.

There is no third state. These tests assert that by walking a recovered
position down each branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.app.orchestrator import Orchestrator
from btcbot.config.loader import Credentials, LoadedConfig
from btcbot.config.schema import AppConfig
from btcbot.exchange.models import AlgoOrder, ExchangePosition, PositionMode
from btcbot.execution.protection import PositionProtector, ProtectionRegistry
from btcbot.safety.circuit_breakers import BreakerType, CircuitBreakers
from btcbot.strategies.base import Direction
from btcbot.utils.errors import ApiError
from tests.conftest import make_perp_instrument

pytestmark = pytest.mark.integration

INST = "BTC-USDT-SWAP"
ENTRY = 118_000.0
CONTRACTS = 0.5

NO_WAIT = (0.0, 0.0)


class RecoveryClient:
    """The exchange as found at startup: a position, and maybe protection."""

    def __init__(
        self,
        *,
        stop_live: bool = False,
        accept_new_protection: bool = True,
        close_error: Exception | None = None,
        read_error: Exception | None = None,
    ) -> None:
        self.accept_new_protection = accept_new_protection
        self.close_error = close_error
        self.read_error = read_error
        self.orders: list[object] = []
        self.algo_placements: list[dict] = []
        self._algo_live: list[AlgoOrder] = []
        if stop_live:
            self._algo_live.append(self._algo("pre-existing", "117000", "120000"))

    @staticmethod
    def _algo(algo_id: str, sl: str, tp: str) -> AlgoOrder:
        return AlgoOrder.from_response(
            {
                "algoId": algo_id,
                "instId": INST,
                "ordType": "oco" if tp else "conditional",
                "state": "live",
                "side": "sell",
                "posSide": "net",
                "sz": str(CONTRACTS),
                "reduceOnly": "true",
                "slTriggerPx": sl,
                "tpTriggerPx": tp,
                "cTime": "1700000000000",
            }
        )

    async def get_protective_orders(self, inst_id):
        if self.read_error is not None:
            raise self.read_error
        return list(self._algo_live)

    async def place_algo_order(
        self, inst_id, *, side, size, td_mode="isolated", pos_side=None,
        sl_trigger_price=None, tp_trigger_price=None, reduce_only=True,
        client_algo_id=None,
    ):
        self.algo_placements.append({"size": size, "sl": sl_trigger_price})
        if not self.accept_new_protection:
            raise ApiError("51000", "algo order rejected")
        order = self._algo(
            f"restored-{len(self.algo_placements)}",
            sl_trigger_price or "0",
            tp_trigger_price or "",
        )
        self._algo_live.append(order)
        return order

    async def cancel_algo_orders(self, inst_id, algo_ids, *, order_type="oco"):
        self._algo_live = [o for o in self._algo_live if o.algo_id not in algo_ids]
        return []

    async def place_order(self, request):
        if self.close_error is not None:
            raise self.close_error
        self.orders.append(request)
        # The position is gone once the close fills, and so is its protection.
        self._algo_live = []
        return type("Ack", (), {"exchange_order_id": "close-1", "client_order_id": "x"})()


class StubExecutor:
    """Only the surface ``_reconcile_protection`` actually touches."""

    def __init__(self, client) -> None:
        self.protector = PositionProtector(
            client, verify_backoff=NO_WAIT, place_attempts=1
        )
        self.protection = ProtectionRegistry()

    def _exit_sides(self, direction: Direction):
        from btcbot.exchange.models import Side

        side = Side.SELL if direction is Direction.LONG else Side.BUY
        return side, None, True


def orchestrator_for(client) -> Orchestrator:
    config = AppConfig()
    loaded = LoadedConfig(
        config=config, config_hash="test", source_path=Path("config/base.yaml"), raw={}
    )
    orchestrator = Orchestrator(
        loaded, Credentials(api_key="k" * 20, api_secret="s" * 20, passphrase="p" * 12)
    )
    orchestrator.client = client
    orchestrator.executor = StubExecutor(client)
    orchestrator.breakers = CircuitBreakers(config.safety)
    orchestrator._position_mode = PositionMode.NET
    return orchestrator


def open_position(contracts: float = CONTRACTS) -> ExchangePosition:
    return ExchangePosition(
        inst_id=INST,
        pos_side="net",
        contracts=contracts,
        avg_price=ENTRY,
        unrealized_pnl=0.0,
        leverage=5.0,
        liq_price=100_000.0,
        margin_mode="isolated",
        margin_ratio=0.4,
        imr=None,
        mmr=None,
        mark_price=ENTRY,
    )


async def reconcile(orchestrator, positions) -> None:
    await orchestrator._reconcile_protection(make_perp_instrument(), positions)


class TestARecoveredPositionEndsProtectedOrClosed:
    async def test_a_position_that_kept_its_stop_is_left_alone(self):
        client = RecoveryClient(stop_live=True)
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        state = orchestrator.executor.protection.get(INST)
        assert state is not None and state.protected
        assert state.algo_id == "pre-existing"
        assert client.orders == [], "a protected position was closed needlessly"
        assert client.algo_placements == [], "a duplicate stop was placed"

    async def test_an_unprotected_position_gets_a_stop_rather_than_a_close(self):
        """Restoring a stop is strictly better than closing at market."""
        client = RecoveryClient(stop_live=False)
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        state = orchestrator.executor.protection.get(INST)
        assert state is not None and state.protected
        assert client.algo_placements, "no attempt was made to restore a stop"
        assert client.orders == [], "the position was closed despite being protectable"

    async def test_a_position_that_cannot_be_protected_is_closed(self):
        client = RecoveryClient(stop_live=False, accept_new_protection=False)
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        assert client.orders, "an unprotectable position was left open"
        close = client.orders[0]
        assert close.reduce_only is True
        assert float(close.sz) == pytest.approx(CONTRACTS)
        assert orchestrator.executor.protection.get(INST) is None

    async def test_closing_an_unprotectable_position_trips_safe_mode(self):
        client = RecoveryClient(stop_live=False, accept_new_protection=False)
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        assert orchestrator.breakers.safe_mode.active
        trips = [
            t for t in orchestrator.breakers.safe_mode.recent_trips()
            if t.breaker is BreakerType.UNPROTECTED_POSITION
        ]
        assert trips, "no unprotected-position breaker was recorded"

    async def test_a_failed_close_still_trips_safe_mode_and_shouts(self, caplog):
        """The worst case: naked *and* uncloseable. It must not pass quietly."""
        import logging

        caplog.set_level(logging.INFO)
        client = RecoveryClient(
            stop_live=False,
            accept_new_protection=False,
            close_error=ApiError("51008", "insufficient balance"),
        )
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        assert orchestrator.breakers.safe_mode.active
        assert "CHECK YOUR OKX DEMO ACCOUNT" in caplog.text

    async def test_a_read_failure_is_treated_as_unprotected(self):
        """A stop we cannot see is a stop we cannot rely on."""
        client = RecoveryClient(stop_live=True, read_error=ApiError("50011", "rate limit"))
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        assert orchestrator.breakers.safe_mode.active
        assert client.orders, "a position with unreadable protection was left open"

    async def test_a_short_is_restored_with_a_stop_above_entry(self):
        client = RecoveryClient(stop_live=False)
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position(contracts=-CONTRACTS)])

        assert client.algo_placements
        assert float(client.algo_placements[0]["sl"]) > ENTRY

    async def test_a_flat_account_clears_stale_protection_state(self):
        client = RecoveryClient(stop_live=True)
        orchestrator = orchestrator_for(client)
        await reconcile(orchestrator, [open_position()])
        assert orchestrator.executor.protection.get(INST) is not None

        await reconcile(orchestrator, [open_position(contracts=0.0)])

        assert orchestrator.executor.protection.get(INST) is None
        assert orchestrator.executor.protection.all_protected()


class TestTheInvariantHolds:
    @pytest.mark.parametrize(
        ("stop_live", "accept_new_protection"),
        [(True, True), (True, False), (False, True), (False, False)],
    )
    async def test_no_reachable_state_leaves_a_naked_position_running(
        self, stop_live, accept_new_protection
    ):
        """Whatever the exchange does, the outcome is protected or closed."""
        client = RecoveryClient(
            stop_live=stop_live, accept_new_protection=accept_new_protection
        )
        orchestrator = orchestrator_for(client)

        await reconcile(orchestrator, [open_position()])

        state = orchestrator.executor.protection.get(INST)
        protected = state is not None and state.protected
        closed = bool(client.orders)
        assert protected or closed, "the position is open with no verified stop"
        assert not (protected and closed), "it was both protected and closed"
