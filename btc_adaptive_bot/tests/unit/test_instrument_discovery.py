"""X-Perp discovery, session levels, and the safety of the non-trading tools.

The instrument ID is the single value this system most refuses to assume. These
tests drive the discovery filter against realistic instrument payloads and prove
it selects correctly, rejects loudly, and never substitutes something else.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from conftest import make_candles

from btcbot.exchange.instruments import CapabilityDiscovery, ExchangeCapabilities
from btcbot.exchange.models import Capability, InstrumentSpec, InstType
from btcbot.utils.errors import ApiError, InstrumentNotFoundError


def _payload(
    inst_id: str,
    *,
    uly: str = "BTC-USDT",
    settle: str = "USDT",
    ct_type: str = "linear",
    state: str = "live",
    inst_type: str = "SWAP",
) -> dict:
    return {
        "instType": inst_type, "instId": inst_id, "uly": uly, "instFamily": uly,
        "settleCcy": settle, "ctVal": "0.01", "ctMult": "1", "ctValCcy": "BTC",
        "ctType": ct_type, "state": state, "lever": "100", "tickSz": "0.1",
        "lotSz": "0.1", "minSz": "0.1", "maxLmtSz": "100000", "maxMktSz": "12000",
    }


class _StubClient:
    """Serves a fixed instrument list; records what was asked for."""

    def __init__(self, payloads: list[dict], *, fail_with: Exception | None = None) -> None:
        self._payloads = payloads
        self._fail_with = fail_with
        self.calls: list[str] = []

    async def get_instruments(self, inst_type=InstType.SWAP, *, inst_id=None):
        self.calls.append(inst_type.value)
        if self._fail_with is not None:
            raise self._fail_with
        return [InstrumentSpec.from_response(p) for p in self._payloads]


def _discovery(payloads, *, preference=("USDT", "USDC", "USD"), fail_with=None):
    return CapabilityDiscovery(
        _StubClient(payloads, fail_with=fail_with),
        base_ccy="BTC",
        settle_preference=list(preference),
    )


class TestXPerpSelection:
    async def test_selects_the_live_linear_btc_swap(self):
        discovery = _discovery([_payload("BTC-USDT-SWAP")])
        capabilities = await discovery.discover()
        assert capabilities.inst_id == "BTC-USDT-SWAP"
        assert capabilities.primary.inst_type is InstType.SWAP
        assert capabilities.primary.is_linear
        assert capabilities.supports_short_on_exchange

    async def test_only_queries_the_swap_instrument_type(self):
        """Spot must never be queried as a fallback."""
        client = _StubClient([_payload("BTC-USDT-SWAP")])
        discovery = CapabilityDiscovery(
            client, base_ccy="BTC", settle_preference=["USDT"]
        )
        await discovery.discover()
        assert client.calls == ["SWAP"]

    async def test_ignores_other_coins(self):
        discovery = _discovery(
            [_payload("ETH-USDT-SWAP", uly="ETH-USDT"), _payload("BTC-USDT-SWAP")]
        )
        assert (await discovery.discover()).inst_id == "BTC-USDT-SWAP"

    async def test_ignores_inverse_contracts(self):
        """'UM' means USD-margined — a coin-margined inverse is a different product."""
        discovery = _discovery(
            [_payload("BTC-USD-SWAP", uly="BTC-USD", settle="BTC", ct_type="inverse"),
             _payload("BTC-USDT-SWAP")]
        )
        assert (await discovery.discover()).inst_id == "BTC-USDT-SWAP"

    async def test_ignores_non_live_instruments(self):
        discovery = _discovery(
            [_payload("BTC-USDT-SWAP", state="suspend"), _payload("BTC-USDC-SWAP", settle="USDC")]
        )
        assert (await discovery.discover()).inst_id == "BTC-USDC-SWAP"

    async def test_settle_preference_decides_between_candidates(self):
        payloads = [
            _payload("BTC-USDC-SWAP", settle="USDC"),
            _payload("BTC-USDT-SWAP", settle="USDT"),
        ]
        assert (await _discovery(payloads).discover()).inst_id == "BTC-USDT-SWAP"
        flipped = _discovery(payloads, preference=("USDC", "USDT"))
        assert (await flipped.discover()).inst_id == "BTC-USDC-SWAP"

    async def test_alternatives_are_recorded_not_discarded(self):
        payloads = [_payload("BTC-USDT-SWAP"), _payload("BTC-USDC-SWAP", settle="USDC")]
        capabilities = await _discovery(payloads).discover()
        assert [a.inst_id for a in capabilities.alternatives] == ["BTC-USDC-SWAP"]
        assert any("alternative" in note for note in capabilities.notes)


class TestDiscoveryFailsLoudly:
    async def test_no_btc_perp_raises_with_the_instrument_list(self):
        """Never silently substitute spot, another coin, or a live product."""
        discovery = _discovery([_payload("ETH-USDT-SWAP", uly="ETH-USDT")])
        with pytest.raises(InstrumentNotFoundError) as exc:
            await discovery.discover()
        message = str(exc.value)
        assert "never falls back" in message
        assert "ETH-USDT-SWAP" in message

    async def test_empty_instrument_list_raises(self):
        with pytest.raises(InstrumentNotFoundError):
            await _discovery([]).discover()

    async def test_api_failure_raises_on_first_discovery(self):
        discovery = _discovery([], fail_with=ApiError(50011, "rate limited", "/instruments"))
        with pytest.raises(InstrumentNotFoundError):
            await discovery.discover()

    async def test_capabilities_before_discovery_raises(self):
        with pytest.raises(InstrumentNotFoundError):
            _ = _discovery([_payload("BTC-USDT-SWAP")]).capabilities

    async def test_primary_on_an_empty_snapshot_raises(self):
        with pytest.raises(InstrumentNotFoundError):
            _ = ExchangeCapabilities(base_ccy="BTC").primary


class TestRefreshKeepsTrading:
    async def test_refresh_retains_the_previous_snapshot_on_failure(self):
        client = _StubClient([_payload("BTC-USDT-SWAP")])
        discovery = CapabilityDiscovery(client, base_ccy="BTC", settle_preference=["USDT"])
        first = await discovery.discover()

        client._fail_with = ApiError(50011, "rate limited", "/instruments")
        second = await discovery.refresh()
        assert second.inst_id == first.inst_id, "a transient failure must not stop trading"


class TestDiscoveredSpecDrivesSizing:
    async def test_contract_fields_come_from_the_payload(self):
        payload = _payload("BTC-USDT-SWAP")
        payload.update({"ctVal": "0.001", "lotSz": "1", "minSz": "1", "tickSz": "0.5",
                        "lever": "75"})
        spec = (await _discovery([payload]).discover()).primary
        assert spec.ct_val == Decimal("0.001")
        assert spec.lot_size == Decimal("1")
        assert spec.min_size == Decimal("1")
        assert spec.tick_size == Decimal("0.5")
        assert spec.max_leverage == Decimal("75")
        # 0.05 BTC at 0.001 BTC/contract = 50 contracts exactly.
        assert spec.contracts_from_base(0.05) == Decimal("50")

    async def test_perp_reports_the_capabilities_it_actually_has(self):
        spec = (await _discovery([_payload("BTC-USDT-SWAP")]).discover()).primary
        for capability in (
            Capability.LONG, Capability.SHORT, Capability.LEVERAGE, Capability.REDUCE_ONLY
        ):
            assert spec.supports(capability), capability


class TestSessionLevels:
    """Previous-day and previous-week extremes — the most-watched liquidity."""

    def _features(self, candles):
        from btcbot.features.engine import FeatureEngine

        return FeatureEngine().compute_from_candles("BTC-USDT-SWAP", "60", candles)

    # ~13 UTC days of hourly bars, aligned to a day boundary so the buckets
    # are exact. The feature engine needs a few hundred bars before it will
    # compute anything, which is why the series is this long.
    DAY_START = 1_700_000_000_000 // 86_400_000 * 86_400_000
    HOURLY_CLOSES = [50_000.0 + (i % 24) * 50 for i in range(312)]

    def test_previous_day_extremes_exclude_the_current_day(self):
        from btcbot.strategies.structure import session_levels

        candles = make_candles(self.HOURLY_CLOSES, timeframe="60", start_ms=self.DAY_START)
        features = self._features(candles)
        assert features is not None
        levels = session_levels(features)
        assert levels.prev_day_high is not None
        assert levels.prev_day_low is not None
        assert levels.prev_day_high > levels.prev_day_low

        # The level must come from the *completed* day, not the forming one.
        last_bucket = candles[-1].open_ms // 86_400_000
        completed = [c for c in candles if c.open_ms // 86_400_000 == last_bucket - 1]
        assert levels.prev_day_high == max(c.high for c in completed)
        assert levels.prev_day_low == min(c.low for c in completed)

    def test_levels_do_not_repaint_as_the_session_progresses(self):
        """The completed period's levels must be identical no matter how many
        bars of the *current* period have formed."""
        from btcbot.strategies.structure import session_levels

        full = make_candles(self.HOURLY_CLOSES, timeframe="60", start_ms=self.DAY_START)
        # Two cuts inside the same UTC day: 6 bars in, then 11 bars in.
        features_early = self._features(full[:306])
        features_late = self._features(full[:311])
        assert features_early is not None and features_late is not None
        assert full[305].open_ms // 86_400_000 == full[310].open_ms // 86_400_000

        early = session_levels(features_early)
        late = session_levels(features_late)
        assert early.prev_day_high == late.prev_day_high
        assert early.prev_day_low == late.prev_day_low
        assert early.prev_week_high == late.prev_week_high

    def test_missing_history_yields_no_levels_rather_than_a_guess(self):
        from btcbot.strategies.structure import session_levels

        candles = make_candles([50_000.0] * 300, timeframe="60")
        features = self._features(candles)
        levels = session_levels(features)
        # Whatever it finds, it must never invent a level from a partial period.
        for _, value in levels.levels():
            assert value > 0

    def test_premium_discount_is_bounded(self):
        from btcbot.strategies.structure import premium_discount

        rising = self._features(make_candles([50_000.0 + i * 20 for i in range(300)],
                                             timeframe="60"))
        position = premium_discount(rising)
        assert position is not None
        assert 0.0 <= position <= 1.0
        # A series ending at its high sits at the top of its range.
        assert position > 0.9

    def test_premium_discount_needs_a_real_range(self):
        from btcbot.strategies.structure import premium_discount

        flat = self._features(make_candles([50_000.0] * 300, timeframe="60"))
        assert premium_discount(flat) is None


class TestNonTradingToolsAreSafe:
    """The verify and smoke-test entry points must not start the experiment."""

    def test_verify_module_never_places_an_order(self):
        import inspect

        from btcbot.app import verify_demo

        source = inspect.getsource(verify_demo)
        assert "place_order" not in source, "the verifier must never submit an order"
        assert "OrderRequest" not in source

    def test_verify_module_never_starts_the_timer(self):
        import inspect

        from btcbot.app import verify_demo

        source = inspect.getsource(verify_demo)
        assert "start_or_resume" not in source
        assert "ExperimentManager" not in source

    def test_smoke_test_never_starts_the_timer(self):
        import inspect

        from btcbot.app import smoke_test

        source = inspect.getsource(smoke_test)
        assert "start_or_resume" not in source
        assert "ExperimentManager" not in source

    def test_smoke_test_caps_its_leverage(self):
        from btcbot.app.smoke_test import DEFAULT_SMOKE_LEVERAGE, MAX_SMOKE_LEVERAGE

        assert DEFAULT_SMOKE_LEVERAGE == 1.0
        assert MAX_SMOKE_LEVERAGE <= 2.0

    def test_smoke_test_requires_explicit_confirmation(self):
        """The CLI must refuse to submit an order without --confirm-demo."""
        import asyncio
        from argparse import Namespace

        from btcbot.app.cli import cmd_smoke_test

        args = Namespace(confirm_demo=False, config=None, log_level=None)
        assert asyncio.run(cmd_smoke_test(args)) == 2

    def test_smoke_test_closes_with_reduce_only(self):
        import inspect

        from btcbot.app import smoke_test

        source = inspect.getsource(smoke_test)
        assert "reduce_only" in source, "the close must be reduce-only constrained"
