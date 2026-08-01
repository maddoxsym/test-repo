"""Orchestrator bootstrap and dry-run loop, driven by a mocked exchange.

The orchestrator is the largest integration surface in the system, so it gets
tested directly rather than only through its parts. A mock REST client serves
realistic candles and instrument data, which lets the whole startup sequence run
offline: migrations → client → verification → capability discovery → balance →
backfill → market-data check → registry → layer construction.

Two behaviours matter most and are asserted explicitly:

* dry run **never** starts the 14-day timer
* without verification, the demo layer stays disabled
"""

from __future__ import annotations

import math

import pytest

from btcbot.config.loader import Credentials, LoadedConfig
from btcbot.config.schema import AppConfig
from btcbot.exchange.endpoints import DEFAULT_PROFILE
from btcbot.exchange.models import (
    AccountConfig,
    Candle,
    FeeRates,
    InstrumentSpec,
    InstType,
    PositionMode,
    Ticker,
    WalletBalance,
)
from btcbot.utils.timeutil import now_ms
from tests.integration.test_dry_run_pipeline import realistic_series


def _credentials() -> Credentials:
    return Credentials(api_key="k" * 20, api_secret="s" * 20, passphrase="p" * 12)

pytestmark = pytest.mark.integration


class MockRestClient:
    """Serves deterministic market data; records what was asked for."""

    def __init__(self, instrument: InstrumentSpec, *, equity: float = 10_000.0) -> None:
        self.base_url = DEFAULT_PROFILE.rest_host
        self.profile = DEFAULT_PROFILE
        self.construction_kwargs: dict[str, object] = {}
        self._instrument = instrument
        self._equity = equity
        self.consecutive_errors = 0
        # The account fee schedule the eligibility cost model reads. Demo's
        # real taker rate is 0.25% per side, not the config placeholder.
        self.maker_fee = 0.001
        self.taker_fee = 0.0025
        self.clock_offset_ms = 12
        self.clock_synced = True
        self.closed = False
        self.calls: list[str] = []
        self._series: dict[str, list[Candle]] = {
            timeframe: realistic_series(timeframe, seed=500 + index, bars=700)
            for index, timeframe in enumerate(("1", "3", "5", "15", "30", "60", "240"))
        }

    @property
    def has_credentials(self) -> bool:
        return True

    def demo_header_enforced(self) -> bool:
        return True

    def clock_drift_exceeds(self, max_drift_ms: int) -> bool:
        return abs(self.clock_offset_ms) > max_drift_ms

    async def sync_clock(self) -> int:
        self.calls.append("sync_clock")
        return self.clock_offset_ms

    async def get_instruments(self, inst_type=InstType.SWAP, *, inst_id=None):
        self.calls.append(f"instruments:{inst_type.value}")
        if inst_type is InstType.SWAP:
            return [self._instrument]
        return []

    async def get_ticker(self, inst_id: str) -> Ticker:
        self.calls.append("ticker")
        last = self._series["1"][-1].close
        return Ticker(
            inst_id=inst_id, last_price=last, bid_price=last - 0.5, ask_price=last + 0.5,
            volume_24h=12_000.0, turnover_24h=6e8, price_24h_pct=0.01, ts_ms=now_ms(),
        )

    async def get_klines(self, inst_id, interval, *, start_ms=None, end_ms=None, limit=300):
        self.calls.append(f"klines:{interval}")
        candles = self._series.get(interval, [])
        if start_ms is not None:
            candles = [c for c in candles if c.open_ms >= start_ms]
        if end_ms is not None:
            candles = [c for c in candles if c.open_ms <= end_ms]
        return candles[-limit:]

    def set_balance(
        self, *, usdt: float, btc_usd: float = 0.0, eth_usd: float = 0.0
    ) -> None:
        """Model a realistic demo account: mostly assets that are not USDT."""
        self._usdt = usdt
        self._btc_usd = btc_usd
        self._eth_usd = eth_usd
        self._equity = usdt + btc_usd + eth_usd

    async def get_wallet_balance(self) -> WalletBalance:
        self.calls.append("balance")
        usdt = getattr(self, "_usdt", self._equity)
        coins: dict[str, dict[str, float]] = {
            "USDT": {"eq": usdt, "availEq": usdt, "cashBal": usdt, "eqUsd": usdt},
        }
        if getattr(self, "_btc_usd", 0.0):
            coins["BTC"] = {"eq": 0.5, "availEq": 0.5, "eqUsd": self._btc_usd}
        if getattr(self, "_eth_usd", 0.0):
            coins["ETH"] = {"eq": 5.0, "availEq": 5.0, "eqUsd": self._eth_usd}
        return WalletBalance(
            total_equity=self._equity,
            total_available=usdt,
            unrealized_pnl=0.0,
            coins=coins,
            ts_ms=now_ms(),
        )

    async def get_account_config(self) -> AccountConfig:
        self.calls.append("account_config")
        return AccountConfig(
            uid="demo-123", account_level="2", position_mode=PositionMode.NET,
            raw={"uid": "demo-123", "acctLv": "2", "posMode": "net_mode"},
        )

    async def get_fee_rates(self, inst_id: str) -> FeeRates:
        """OKX Demo's real schedule: 0.25% per side, so 0.5% per round trip."""
        self.calls.append("fee_rates")
        return FeeRates(maker=self.maker_fee, taker=self.taker_fee)

    async def get_positions(self, inst_id=None):
        return []

    async def get_open_orders(self, inst_id):
        return []

    async def get_executions(self, inst_id, *, limit=100, start_ms=None):
        return []

    async def get_funding_bills(self, *, limit=100):
        return []

    async def cancel_all(self, inst_id):
        return {"cancelled": 0, "failed": []}

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def loaded_config(tmp_path) -> LoadedConfig:
    config = AppConfig(
        market={
            "timeframes": ["5", "15", "60", "240"],
            "regime_timeframe": "60",
            "regime_context_timeframe": "240",
        },
        data={
            "history_timeframes": ["5", "15", "60", "240"],
            "candle_buffer": 600,
            "backfill_on_start": True,
            "cache_dir": str(tmp_path / "history"),
        },
        database={"path": str(tmp_path / "boot.db"), "backup_dir": str(tmp_path / "backups")},
        logging={"file": None, "json_file": None, "console": False},
        dashboard={"enabled": False},
        reporting={
            "output_dir": str(tmp_path / "reports"),
            "export_dir": str(tmp_path / "reports" / "exports"),
        },
        news={"enabled": False, "providers": []},
        # The live-environment negative control needs real outbound network
        # access to the region's host; offline it correctly reports "inconclusive"
        # and blocks trading. That behaviour is covered by test_safety_lock.py,
        # so it is switched off here to keep this test about bootstrap wiring.
        safety={"mainnet_negative_control": False},
        # Keep the strategy set small so the test stays quick.
        strategies={"enabled": ["ema_trend_cross_15m", "donchian_breakout_15m",
                                "vwap_reversion_5m", "ema_adx_trend_1h"]},
    )
    return LoadedConfig(
        config=config, config_hash="boothash00000000",
        source_path=tmp_path / "cfg.yaml", raw={},
    )


def _patch_client(monkeypatch, instrument, **kwargs) -> MockRestClient:
    mock = MockRestClient(instrument, **kwargs)

    def _build(**client_kwargs):
        # Record what the orchestrator asked for so tests can assert the
        # region profile was threaded through rather than defaulted.
        mock.construction_kwargs = client_kwargs
        profile = client_kwargs.get("profile")
        if profile is not None:
            mock.profile = profile
            mock.base_url = profile.rest_host
        return mock

    monkeypatch.setattr("btcbot.app.orchestrator.OkxDemoClient", _build)
    return mock


class TestBootstrap:
    async def test_bootstrap_completes_every_precondition(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        mock = _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        try:
            preconditions = await orchestrator.bootstrap()

            assert preconditions.config_valid
            assert preconditions.migrations_applied
            assert preconditions.demo_authenticated
            assert preconditions.demo_verified
            assert preconditions.market_data_ready
            assert preconditions.all_met, preconditions.unmet()

            # The real balance was read, not invented.
            assert math.isclose(orchestrator._equity, 10_000.0)   # noqa: SLF001
            assert "balance" in mock.calls

            # Instrument discovery ran and found the X-Perp — shorts native.
            capabilities = orchestrator.discovery.capabilities
            assert capabilities.primary.inst_type is InstType.SWAP
            assert capabilities.supports_short_on_exchange
            assert orchestrator._inst_id == capabilities.primary.inst_id  # noqa: SLF001

            # Backfill populated the live series.
            for timeframe in ("5", "15", "60", "240"):
                assert orchestrator.store.bars_available(timeframe) > 200

            assert len(orchestrator.registry) == 4
        finally:
            await orchestrator.shutdown()

    @pytest.mark.parametrize("region", ["global", "eea", "us"])
    async def test_configured_region_reaches_the_client_and_the_dashboard(
        self, monkeypatch, loaded_config, perp_instrument, region
    ):
        """The region must be threaded end to end — not defaulted somewhere."""
        from btcbot.app.orchestrator import Orchestrator
        from btcbot.exchange.endpoints import profile_for

        mock = _patch_client(monkeypatch, perp_instrument)
        config = loaded_config.config.model_copy(
            update={"exchange": loaded_config.config.exchange.model_copy(
                update={"region": region}
            )}
        )
        loaded = LoadedConfig(
            config=config, config_hash=loaded_config.config_hash,
            source_path=loaded_config.source_path, raw={},
        )
        orchestrator = Orchestrator(loaded, _credentials())
        try:
            await orchestrator.bootstrap()
            expected = profile_for(region)

            assert mock.construction_kwargs["profile"] is expected
            assert orchestrator.profile is expected
            assert orchestrator.guard.profile is expected

            system = orchestrator.dashboard_state()["system"]
            assert system["region"] == region
            assert system["rest_host"] == expected.rest_host
            assert system["ws_hosts"] == list(expected.ws_urls)
            # Whatever the region, the dashboard must never show a live host.
            for url in system["ws_hosts"]:
                assert "pap" in url.split("//", 1)[1].split(".", 1)[0]
        finally:
            await orchestrator.shutdown()

    async def test_research_equity_is_capped_and_usdt_only(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """An $84,000 account with $10,000 USDT runs on $10,000."""
        from btcbot.app.orchestrator import Orchestrator

        mock = _patch_client(monkeypatch, perp_instrument)
        mock.set_balance(usdt=10_000.0, btc_usd=59_000.0, eth_usd=15_000.0)
        orchestrator = Orchestrator(loaded_config, _credentials())
        try:
            await orchestrator.bootstrap()
            ledger = orchestrator.research_equity

            assert ledger.actual_total_equity == pytest.approx(84_000.0)
            assert ledger.actual_usdt_equity == pytest.approx(10_000.0)
            assert ledger.cap_usdt == pytest.approx(10_000.0)
            # Before the experiment starts nothing is fixed yet; the account has
            # only been observed.
            assert ledger.starting_equity == 0.0
        finally:
            await orchestrator.shutdown()

    async def test_bootstrap_without_credentials_leaves_demo_unverified(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, None)
        try:
            preconditions = await orchestrator.bootstrap()
            assert not preconditions.demo_verified
            assert not preconditions.all_met
            assert "demo environment verification" in preconditions.unmet()
            assert orchestrator.guard.orders_permitted() is False
        finally:
            await orchestrator.shutdown()

    async def test_actual_balance_is_used_even_when_it_differs(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """The brief: never fake the value; use the real demo balance."""
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument, equity=7_432.19)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        try:
            await orchestrator.bootstrap()
            assert math.isclose(orchestrator._equity, 7_432.19)   # noqa: SLF001
            assert loaded_config.config.experiment.expected_demo_equity == 10_000.0
        finally:
            await orchestrator.shutdown()

    async def test_strategies_are_registered_in_the_database(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        try:
            await orchestrator.bootstrap()
            stored = orchestrator.repos.strategies.all_strategies()
            assert len(stored) == 4
            for row in stored:
                assert row["hypothesis"]
                assert row["exit_mechanisms"]
            versions = orchestrator.repos.strategies.version_map()
            assert len(versions) == 4
        finally:
            await orchestrator.shutdown()


class TestBackfillTiming:
    """Backfill is pagination — it must not wait for candles or touch the timer."""

    async def test_bootstrap_backfill_completes_in_seconds(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """The reported bug took an hour for the 60m timeframe alone."""
        import time

        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, _credentials())
        try:
            started = time.monotonic()
            await orchestrator.bootstrap()
            elapsed = time.monotonic() - started

            # The shortest configured timeframe is 5m; a single candle wait on
            # any timeframe would blow straight past this.
            assert elapsed < 60.0, f"bootstrap took {elapsed:.1f}s"
            for timeframe in ("5", "15", "60", "240"):
                assert orchestrator.store.bars_available(timeframe) > 200
        finally:
            await orchestrator.shutdown()

    async def test_backfill_never_sleeps_for_a_timeframe(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """No sleep during bootstrap may approach a candle interval."""
        import asyncio as _asyncio

        from btcbot.app.orchestrator import Orchestrator

        slept: list[float] = []
        real_sleep = _asyncio.sleep

        async def recording_sleep(seconds, *args, **kwargs):
            slept.append(seconds)
            await real_sleep(0)

        _patch_client(monkeypatch, perp_instrument)
        monkeypatch.setattr(
            "btcbot.market_data.historical.asyncio.sleep", recording_sleep
        )
        orchestrator = Orchestrator(loaded_config, _credentials())
        try:
            await orchestrator.bootstrap()
        finally:
            await orchestrator.shutdown()

        # 60s is the shortest candle this system supports.
        assert all(s < 60.0 for s in slept), f"a backfill sleep waited {max(slept)}s"
        assert sum(slept) < 60.0, f"backfill slept {sum(slept)}s in total"

    async def test_backfill_does_not_create_or_touch_an_experiment(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """Requirement 12: the 14-day timer is not started during backfill."""
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, _credentials())
        try:
            await orchestrator.bootstrap()
            # The experiment manager is not even constructed during bootstrap,
            # let alone asked to start a timer.
            assert orchestrator.experiment is None or orchestrator.experiment.state is None
            assert orchestrator.repos.experiments.find_active() is None, (
                "backfill created an experiment row"
            )
        finally:
            await orchestrator.shutdown()

    async def test_a_backfill_failure_leaves_market_data_unready(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """Requirement 14: report the error and keep trading disabled."""
        from btcbot.app.orchestrator import Orchestrator
        from btcbot.utils.errors import BackfillError

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, _credentials())

        async def boom(*_args, **_kwargs):
            raise BackfillError("BTC-USDT-SWAP 15: page request failed — 429")

        try:
            await orchestrator.bootstrap()  # build the manager first
            monkeypatch.setattr(orchestrator.history, "backfill_series", boom)
            assert await orchestrator._backfill() is False   # noqa: SLF001
        finally:
            await orchestrator.shutdown()

    async def test_a_backfill_failure_is_reported_verbatim(
        self, monkeypatch, loaded_config, perp_instrument, caplog
    ):
        import logging

        from btcbot.app.orchestrator import Orchestrator
        from btcbot.utils.errors import BackfillError

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, _credentials())

        async def boom(*_args, **_kwargs):
            raise BackfillError("exchange said 50011 rate limited")

        try:
            await orchestrator.bootstrap()
            monkeypatch.setattr(orchestrator.history, "backfill_series", boom)
            caplog.set_level(logging.INFO)
            await orchestrator._backfill()   # noqa: SLF001
        finally:
            await orchestrator.shutdown()

        assert "50011 rate limited" in caplog.text
        assert "FAILED" in caplog.text


class TestDryRunDoesNotStartTheTimer:
    async def test_dry_run_never_creates_an_experiment(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """The single most important dry-run guarantee."""
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config,
            _credentials(),
            dry_run=True,
        )
        # Skip the websocket layer; this test is about the timer, not streaming.
        monkeypatch.setattr(orchestrator, "_start_streams", _noop)
        monkeypatch.setattr(orchestrator, "_bar_worker", _noop)

        try:
            await orchestrator.bootstrap()
            orchestrator.experiment = _experiment_manager(orchestrator, loaded_config)
            # _run_dry_run() shuts the engine down when it finishes, so the
            # database is inspected afterwards through a fresh connection —
            # which also proves nothing was persisted.
            await orchestrator._run_dry_run(duration_seconds=0)   # noqa: SLF001
            assert orchestrator.experiment.state is None
        finally:
            await orchestrator.shutdown()

        from btcbot.database.db import Database
        from btcbot.database.repositories import Repositories

        with Database(loaded_config.config.database.path) as reopened:
            assert Repositories(reopened).experiments.find_active() is None, (
                "dry run started the 14-day timer"
            )

    async def test_dry_run_observes_market_data_and_regime(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config,
            _credentials(),
            dry_run=True,
        )
        monkeypatch.setattr(orchestrator, "_start_streams", _noop)
        monkeypatch.setattr(orchestrator, "_bar_worker", _noop)

        try:
            await orchestrator.bootstrap()
            orchestrator.experiment = _experiment_manager(orchestrator, loaded_config)
            await orchestrator._run_dry_run(duration_seconds=6)   # noqa: SLF001

            assert orchestrator.store.ticker is not None
            assert orchestrator.store.last_price > 0
            assert orchestrator._current_regime is not None       # noqa: SLF001
            assert 0.0 <= orchestrator._current_regime.confidence <= 1.0
        finally:
            await orchestrator.shutdown()

    async def test_dashboard_state_is_available_during_dry_run(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        import json

        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config,
            _credentials(),
            dry_run=True,
        )
        try:
            await orchestrator.bootstrap()
            state = orchestrator.dashboard_state()
            assert state["system"]["dry_run"] is True
            assert state["experiment"] is None
            assert state["market"]["last_price"] > 0
            # The instrument panel reflects the discovered contract spec.
            assert state["instrument"]["inst_id"] == orchestrator._inst_id  # noqa: SLF001
            assert state["instrument"]["margin_mode"] == "isolated"
            # Requirement: the five equity figures are shown separately and
            # never conflated with each other.
            equity = state["research_equity"]
            for key in (
                "actual_total_equity", "actual_available_usdt",
                "starting_equity", "current_equity", "cap_usdt",
            ):
                assert key in equity, key
            assert equity["cap_usdt"] == 10_000.0
            # The account panel reports RESEARCH equity, with the real account
            # alongside it under an unambiguous name.
            assert state["demo_account"]["equity"] == equity["current_equity"]
            assert state["demo_account"]["actual_total_equity"] == equity["actual_total_equity"]
            assert state["demo_account"]["risk_state"]["state"]
            json.dumps(state, default=str)
        finally:
            await orchestrator.shutdown()


class TestStreamsStartImmediatelyAfterBackfill:
    async def test_streams_start_with_no_wait_between(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """Requirement 11: backfill finishes, streams start — nothing in between."""
        import time

        from btcbot.app.orchestrator import Orchestrator

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, _credentials(), dry_run=True)
        order: list[str] = []
        started_at: dict[str, float] = {}

        async def record_streams(**_kwargs):
            order.append("streams")
            started_at["streams"] = time.monotonic()

        real_backfill = None
        try:
            await orchestrator.bootstrap()
            order.append("backfill")
            started_at["backfill"] = time.monotonic()
            monkeypatch.setattr(orchestrator, "_start_streams", record_streams)
            orchestrator._shutdown_event.set()          # noqa: SLF001
            await orchestrator._run_dry_run(duration_seconds=0)   # noqa: SLF001
        finally:
            del real_backfill
            await orchestrator.shutdown()

        assert order == ["backfill", "streams"]
        gap = started_at["streams"] - started_at["backfill"]
        assert gap < 60.0, f"waited {gap:.1f}s between backfill and streams"


class TestResearchModeGating:
    async def test_research_refuses_to_start_unverified(
        self, monkeypatch, loaded_config, perp_instrument
    ):
        """No credentials ⇒ no experiment, and a clear refusal."""
        from btcbot.app.orchestrator import Orchestrator
        from btcbot.utils.errors import DemoVerificationError

        _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(loaded_config, None)
        try:
            with pytest.raises(DemoVerificationError) as exc:
                await orchestrator.start()
            assert "preconditions not met" in str(exc.value)
            assert orchestrator.repos.experiments.find_active() is None
        finally:
            await orchestrator.shutdown()

    async def test_shutdown_is_idempotent(self, monkeypatch, loaded_config, perp_instrument):
        from btcbot.app.orchestrator import Orchestrator

        mock = _patch_client(monkeypatch, perp_instrument)
        orchestrator = Orchestrator(
            loaded_config, _credentials()
        )
        await orchestrator.bootstrap()
        orchestrator._running = True     # noqa: SLF001
        await orchestrator.shutdown()
        await orchestrator.shutdown()    # must not raise
        assert mock.closed


async def _noop(*args, **kwargs) -> None:
    return None


def _experiment_manager(orchestrator, loaded_config):
    from btcbot.app.experiment import ExperimentManager

    return ExperimentManager(
        orchestrator.repos.experiments,
        orchestrator.repos.system,
        loaded_config.config,
        config_hash=loaded_config.config_hash,
    )
