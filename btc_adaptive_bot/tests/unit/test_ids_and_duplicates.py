"""Identifier generation and duplicate-order protection.

Two properties matter:

* IDs are **deterministic** where they must be (so a restart recognises work it
  already did) and **unique** where they must be (so orders never collide).
* Duplicate orders are impossible even under a crash between "sent" and
  "recorded", because the database row is written before the HTTP call.
"""

from __future__ import annotations

import pytest

from btcbot.database.repositories import DemoOrderRepository
from btcbot.execution.order_safety import OrderSafetyGuard
from btcbot.utils.ids import (
    ORDER_LINK_ID_MAX_LEN,
    client_order_id,
    config_hash,
    experiment_id,
    is_valid_client_order_id,
    new_uuid,
    setup_id,
    signal_id,
)
from btcbot.utils.timeutil import now_utc


class TestSignalIds:
    def test_deterministic_for_identical_inputs(self):
        args = ("ema_trend_cross_15m", "1.0", "BTCUSDT", "15", 1_700_000_000_000, "long")
        assert signal_id(*args) == signal_id(*args)

    @pytest.mark.parametrize("index", range(6))
    def test_any_input_change_changes_the_id(self, index):
        base = ["ema_trend_cross_15m", "1.0", "BTCUSDT", "15", 1_700_000_000_000, "long"]
        variant = list(base)
        variant[index] = (
            variant[index] + 1 if isinstance(variant[index], int) else f"{variant[index]}X"
        )
        assert signal_id(*base) != signal_id(*variant)

    def test_prefixed_for_readability(self):
        assert signal_id("s", "1", "BTCUSDT", "5", 1, "long").startswith("sig_")

    def test_direction_matters(self):
        long_id = signal_id("s", "1", "BTCUSDT", "5", 1_000, "long")
        short_id = signal_id("s", "1", "BTCUSDT", "5", 1_000, "short")
        assert long_id != short_id


class TestSetupIds:
    def test_same_structural_setup_collapses(self):
        first = setup_id("liquidity_sweep_5m", "BTCUSDT", "5", 1_700_000_000_000, "sweep_low_43120")
        second = setup_id("liquidity_sweep_5m", "BTCUSDT", "5", 1_700_000_000_000, "sweep_low_43120")
        assert first == second

    def test_different_setup_keys_differ(self):
        first = setup_id("s", "BTCUSDT", "5", 1_000, "sweep_low_43120")
        second = setup_id("s", "BTCUSDT", "5", 1_000, "sweep_low_43500")
        assert first != second


class TestClientOrderIds:
    def test_respects_bybit_length_limit(self):
        """Bybit documents orderLinkId as a maximum of 36 characters."""
        for index in range(200):
            oid = client_order_id(
                f"exp_{index}", f"strategy_with_a_long_name_{index}", "1.0",
                f"set_{index}", 1_700_000_000_000 + index,
            )
            assert len(oid) <= ORDER_LINK_ID_MAX_LEN, f"{oid} is {len(oid)} chars"

    def test_only_uses_legal_characters(self):
        """Numbers, letters, dashes and underscores only."""
        for index in range(100):
            oid = client_order_id(f"e{index}", f"s{index}", "1.0", f"x{index}", 1_700_000_000_000)
            assert is_valid_client_order_id(oid), f"{oid} is not Bybit-legal"

    def test_ids_are_unique_across_many_calls(self):
        ids = {
            client_order_id("exp", "strategy", "1.0", "setup", 1_700_000_000_000)
            for _ in range(2_000)
        }
        assert len(ids) == 2_000, "client order IDs collided"

    def test_same_strategy_yields_a_stable_routable_slug(self):
        """An operator should recognise the owning strategy in the Bybit UI."""
        first = client_order_id("exp1", "vwap_reversion_5m", "1.0", "setupA", 1_000_000)
        second = client_order_id("exp1", "vwap_reversion_5m", "1.0", "setupB", 1_000_000)
        # exp + strategy slug shared, setup slug differs.
        assert first[:8] == second[:8]
        assert first != second

    def test_rejects_illegal_ids(self):
        assert not is_valid_client_order_id("has spaces")
        assert not is_valid_client_order_id("has/slash")
        assert not is_valid_client_order_id("x" * 37)
        assert not is_valid_client_order_id("")

    def test_handles_a_zero_timestamp(self):
        oid = client_order_id("exp", "strategy", "1.0", "setup", 0)
        assert is_valid_client_order_id(oid)


class TestOtherIds:
    def test_experiment_id_is_unique_even_within_one_millisecond(self):
        """Two experiments started in the same millisecond must not collide."""
        ids = {
            experiment_id("BYBIT_DEMO_RESEARCH", 1_700_000_000_000, "abc123")
            for _ in range(500)
        }
        assert len(ids) == 500
        assert all(i.startswith("exp_") for i in ids)

    def test_config_hash_is_stable_and_short(self):
        assert config_hash("payload") == config_hash("payload")
        assert config_hash("payload") != config_hash("payload2")
        assert len(config_hash("payload")) == 16

    def test_uuid_is_unique(self):
        assert len({new_uuid() for _ in range(1_000)}) == 1_000


class TestDuplicateOrderProtection:
    @pytest.fixture
    def orders(self, temp_db) -> DemoOrderRepository:
        return DemoOrderRepository(temp_db)

    def _order(self, setup: str, intent: str = "entry", client_id: str | None = None) -> dict:
        return {
            "client_order_id": client_id or f"b{setup}{intent}"[:36],
            "experiment_id": "exp_test",
            "signal_id": "sig_test",
            "setup_id": setup,
            "strategy_id": "test_strategy",
            "strategy_version": "1.0",
            "signal_ts_utc": "2026-07-24T12:00:00Z",
            "submitted_ts_utc": "2026-07-24T12:00:01Z",
            "symbol": "BTCUSDT",
            "category": "spot",
            "side": "Buy",
            "order_type": "Market",
            "intent": intent,
            "quantity": 0.001,
            "quantity_str": "0.001000",
            "price": None,
            "estimated_notional": 50.0,
            "stop_price": 49_000.0,
            "target_price": 52_000.0,
            "estimated_risk_pct": 0.0075,
            "regime": "TREND_UP",
            "confidence": 0.7,
            "sizing_reasoning": "test",
        }

    def test_first_reservation_succeeds(self, orders):
        assert orders.reserve(self._order("setup_1")) is True

    def test_second_reservation_for_same_setup_and_intent_fails(self, orders):
        """The database constraint is the authoritative duplicate guard."""
        assert orders.reserve(self._order("setup_1")) is True
        assert orders.reserve(self._order("setup_1", client_id="different_id")) is False

    def test_same_setup_with_a_different_intent_is_allowed(self, orders):
        assert orders.reserve(self._order("setup_1", "entry")) is True
        assert orders.reserve(self._order("setup_1", "exit")) is True

    def test_different_setups_are_independent(self, orders):
        assert orders.reserve(self._order("setup_1")) is True
        assert orders.reserve(self._order("setup_2")) is True

    def test_has_open_intent_reflects_reservations(self, orders):
        assert orders.has_open_intent("setup_1", "entry") is False
        orders.reserve(self._order("setup_1"))
        assert orders.has_open_intent("setup_1", "entry") is True

    def test_reservation_survives_a_simulated_crash(self, orders):
        """A crash after reserving but before submitting must still block a repeat."""
        orders.reserve(self._order("setup_crash"))
        # No mark_result() call — this is the crash window.
        in_flight = orders.in_flight()
        assert len(in_flight) == 1
        assert in_flight[0]["status"] == "submitted"
        assert orders.reserve(self._order("setup_crash", client_id="retry_id")) is False

    def test_fills_are_idempotent(self, orders):
        orders.reserve(self._order("setup_1"))
        fill = {
            "fill_id": "exec_123",
            "client_order_id": orders.recent(1)[0]["client_order_id"],
            "exchange_order_id": "ex_1",
            "experiment_id": "exp_test",
            "strategy_id": "test_strategy",
            "symbol": "BTCUSDT",
            "side": "Buy",
            "price": 50_000.0,
            "quantity": 0.001,
            "fee": 0.0275,
            "fee_currency": "USDT",
            "is_maker": 0,
            "exec_ts_utc": "2026-07-24T12:00:02Z",
            "raw": {},
        }
        assert orders.record_fill(fill) is True
        assert orders.record_fill(fill) is False, "the same execId must not be stored twice"

    def test_count_since_excludes_failed_orders(self, orders):
        orders.reserve(self._order("setup_ok"))
        orders.reserve(self._order("setup_bad"))
        bad_id = self._order("setup_bad")["client_order_id"]
        orders.mark_result(bad_id, status="failed", reject_reason="test")
        assert orders.count_since("exp_test", "2026-01-01T00:00:00Z") == 1


class TestOrderSafetyGuard:
    @pytest.fixture
    def guard(self, temp_db) -> OrderSafetyGuard:
        return OrderSafetyGuard(DemoOrderRepository(temp_db), max_signal_age_seconds=60)

    def test_fresh_unseen_setup_is_allowed(self, guard):
        verdict = guard.check(setup_id="s1", intent="entry", signal_time=now_utc())
        assert verdict.allowed

    def test_in_flight_setup_is_blocked(self, guard):
        assert guard.begin("s1", "entry") is True
        verdict = guard.check(setup_id="s1", intent="entry")
        assert not verdict.allowed
        assert "in flight" in verdict.reason

    def test_begin_is_exclusive(self, guard):
        assert guard.begin("s1", "entry") is True
        assert guard.begin("s1", "entry") is False, "two callers claimed the same slot"

    def test_finish_releases_the_slot(self, guard):
        guard.begin("s1", "entry")
        guard.finish("s1", "entry")
        assert guard.begin("s1", "entry") is True

    def test_stale_signal_is_rejected(self, guard):
        from datetime import timedelta

        old = now_utc() - timedelta(seconds=600)
        verdict = guard.check(setup_id="s1", intent="entry", signal_time=old)
        assert not verdict.allowed
        assert "old" in verdict.reason
        assert guard.stale_rejections == 1

    def test_counters_are_tracked(self, guard):
        guard.begin("s1", "entry")
        guard.check(setup_id="s1", intent="entry")
        snapshot = guard.snapshot()
        assert snapshot["in_flight"] == 1
        assert snapshot["duplicate_rejections"] == 1
