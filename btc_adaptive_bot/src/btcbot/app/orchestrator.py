"""The engine: bootstrap, reconciliation, the live loop, and Day-14 finalisation.

Responsibilities, in order of execution:

1. **Bootstrap** — database, migrations, exchange client, demo verification,
   capability discovery, historical backfill, strategy registry, all three
   evidence layers.
2. **Reconcile** — compare exchange state with the ledger before trading.
3. **Run** — stream market data, evaluate strategies on closed bars, feed the
   shadow engine, allocate real demo orders, manage exits, poll news, learn.
4. **Finalise** — at exactly the scheduled end, freeze, rank, select a champion,
   report, and transition to champion mode.

The event loop never blocks on database work: SQLite calls go through
``asyncio.to_thread`` via ``Database.run`` where they could be slow.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import timedelta
from typing import Any

from ..backtesting.data_split import split_candles
from ..backtesting.walk_forward import WalkForwardAnalyzer, evaluate_segments
from ..config.loader import Credentials, LoadedConfig
from ..database.db import Database
from ..database.migrations import run_migrations
from ..database.repositories import Repositories
from ..decision.engine import DecisionEngine
from ..decision.risk_state import RiskStateTracker
from ..exchange.demo_guard import DemoGuard
from ..exchange.endpoints import DEFAULT_PROFILE, DemoProfile, profile_for
from ..exchange.instruments import CapabilityDiscovery
from ..exchange.models import Candle, Execution, PositionMode, Ticker, WalletBalance
from ..exchange.rest import OkxDemoClient
from ..exchange.ws import PrivateAccountStream, PublicMarketStream
from ..execution.allocator import DemoAllocator
from ..execution.demo_executor import DemoExecutor
from ..execution.order_safety import OrderSafetyGuard
from ..execution.position_ledger import PositionLedger
from ..execution.trade_manager import TradeManager
from ..features.engine import FeatureEngine, MultiTimeframeFeatures
from ..learning.loss_analysis import ConfidenceCalibrator, LossAnalyzer
from ..learning.metrics import compute_metrics, regime_expectancy_map
from ..market_data.historical import HistoricalDataManager
from ..market_data.store import MarketDataStore
from ..news.engine import NewsEngine
from ..notifications.manager import NotificationManager
from ..regime.classifier import RegimeClassifier, RegimeSnapshot, RegimeTracker
from ..reporting.reports import ReportGenerator
from ..risk.leverage_engine import LeverageEngine
from ..risk.position_sizing import PositionSizer
from ..safety.circuit_breakers import CircuitBreakers
from ..scoring.champion import ChampionSelector
from ..scoring.scorer import StrategyEvidence, StrategyScorer, rank_strategies
from ..shadow.engine import ShadowEngine
from ..strategies.base import Strategy, StrategyContext, StrategySignal
from ..strategies.registry import StrategyRegistry
from ..utils.errors import (
    ApiError,
    BtcBotError,
    DemoVerificationError,
    TransportError,
)
from ..utils.ids import setup_id as make_setup_id
from ..utils.ids import signal_id as make_signal_id
from ..utils.logging import get_logger
from ..utils.numeric import safe_div
from ..utils.timeutil import interval_seconds, iso, now_utc
from .experiment import ExperimentManager, ExperimentMode, Preconditions

log = get_logger(__name__)


class Orchestrator:
    """Runs the whole system."""

    def __init__(
        self,
        loaded: LoadedConfig,
        credentials: Credentials | None,
        *,
        mode: str = ExperimentMode.RESEARCH,
        dry_run: bool = False,
    ) -> None:
        self.loaded = loaded
        self.config = loaded.config
        self.credentials = credentials
        self.mode = mode
        self.dry_run = dry_run

        self.db: Database | None = None
        self.repos: Repositories | None = None
        # Replaced during bootstrap with the profile named by exchange.region.
        # Every candidate is a demo profile — see exchange/endpoints.py.
        self.profile: DemoProfile = DEFAULT_PROFILE
        self.client: OkxDemoClient | None = None
        self.guard: DemoGuard | None = None
        self.discovery: CapabilityDiscovery | None = None
        self.store: MarketDataStore | None = None
        self.history: HistoricalDataManager | None = None
        self.registry: StrategyRegistry | None = None
        self.features = FeatureEngine()
        self.classifier = RegimeClassifier(self.config.regime)
        self.regime_tracker = RegimeTracker()
        self.shadow: ShadowEngine | None = None
        self.allocator: DemoAllocator | None = None
        self.executor: DemoExecutor | None = None
        self.decision: DecisionEngine | None = None
        self.ledger: PositionLedger | None = None
        self.trade_manager: TradeManager | None = None
        self.news: NewsEngine | None = None
        self.experiment: ExperimentManager | None = None
        self.breakers = CircuitBreakers(self.config.safety)
        self.risk_state = RiskStateTracker(self.config.risk, self.breakers)
        self.notifications = NotificationManager(self.config.notifications)
        self.reports: ReportGenerator | None = None
        self.public_stream: PublicMarketStream | None = None
        self.private_stream: PrivateAccountStream | None = None
        # The discovered X-Perp instId — set by capability discovery, never configured.
        self._inst_id: str = ""
        self._position_mode: PositionMode = PositionMode.NET
        self._latest_margin_ratio: float | None = None
        self._latest_funding_rate: float | None = None
        self._next_funding_ms: int | None = None
        # Open interest: None until the exchange delivers it. Strategies that
        # need it stand down rather than assume a value.
        self._latest_open_interest: float | None = None
        self._prev_open_interest: float | None = None

        self._running = False
        self._tasks: list[asyncio.Task[Any]] = []
        self._bar_queue: asyncio.Queue[tuple[str, Candle]] = asyncio.Queue(maxsize=500)
        self._current_regime: RegimeSnapshot | None = None
        self._equity = 0.0
        self._available = 0.0
        self._starting_equity = 0.0
        self._peak_equity = 0.0
        self._last_report_day = 0
        self._outage_id: int | None = None
        self._shutdown_event = asyncio.Event()
        self._finalized = False

    # =================================================================
    #  BOOTSTRAP
    # =================================================================

    async def bootstrap(self) -> Preconditions:
        """Prepare every subsystem and evaluate the timer preconditions."""
        preconditions = Preconditions(config_valid=True)
        log.info("CONFIG", f"Configuration loaded from {self.loaded.source_path} "
                           f"(hash {self.loaded.config_hash})")

        # --- database -------------------------------------------------
        self.db = Database(self.config.database.path, busy_timeout_ms=self.config.database.busy_timeout_ms)
        applied = run_migrations(self.db)
        self.repos = Repositories(self.db)
        preconditions.migrations_applied = True
        if applied:
            log.info("DB", f"Applied {applied} migration(s)")

        # --- exchange client (the demo host for the configured region is
        #     pinned in the constructor; x-simulated-trading is injected by
        #     its transport layer) ---------------------------------------
        self.profile = profile_for(self.config.exchange.region)
        self.client = OkxDemoClient(
            api_key=self.credentials.api_key if self.credentials else None,
            api_secret=self.credentials.api_secret if self.credentials else None,
            passphrase=self.credentials.passphrase if self.credentials else None,
            timeout_seconds=self.config.exchange.request_timeout_seconds,
            max_retries=self.config.exchange.max_retries,
            backoff_base_seconds=self.config.exchange.retry_backoff_base_seconds,
            profile=self.profile,
        )
        await self.client.sync_clock()
        log.info("OKX", f"Connected to {self.client.base_url} ({self.profile.label})")
        self.breakers.check_clock_drift(self.client.clock_offset_ms)

        # --- demo verification ----------------------------------------
        self.guard = DemoGuard(
            self.client,
            api_key=self.credentials.api_key if self.credentials else None,
            api_secret=self.credentials.api_secret if self.credentials else None,
            passphrase=self.credentials.passphrase if self.credentials else None,
            run_mainnet_negative_control=self.config.safety.mainnet_negative_control,
            profile=self.profile,
        )
        if self.credentials is not None:
            verification = await self.guard.verify()
            preconditions.demo_authenticated = any(
                s.name == "authenticated reachability" and s.passed for s in verification.signals
            )
            preconditions.demo_verified = verification.verified
            if verification.account_config is not None:
                # Adapt to the account's position mode — never force a change.
                self._position_mode = verification.account_config.position_mode
                log.info("OKX", f"Account position mode: {self._position_mode.value}")
        else:
            log.warning(
                "SAFETY",
                "No credentials supplied — running without authentication. "
                "Order submission is disabled.",
            )

        # --- instrument discovery (the X-Perp is found, never hardcoded) --
        self.discovery = CapabilityDiscovery(
            self.client,
            base_ccy=self.config.market.base_currency,
            settle_preference=list(self.config.market.settle_currency_preference),
        )
        capabilities = await self.discovery.discover()
        self._inst_id = capabilities.inst_id
        for line in capabilities.describe():
            log.info("OKX", line)

        # --- balance ----------------------------------------------------
        if preconditions.demo_verified:
            balance = await self.client.get_wallet_balance()
            self._equity = balance.total_equity
            self._available = balance.total_available or balance.total_equity
            log.info("BALANCE", f"${self._equity:,.2f}")
            log.info(
                "BALANCE",
                f"Expected research capital: ${self.config.experiment.expected_demo_equity:,.2f} | "
                f"Actual OKX Demo capital: ${self._equity:,.2f}",
            )
            if abs(self._equity - self.config.experiment.expected_demo_equity) > 1.0:
                log.info(
                    "BALANCE",
                    "Actual demo balance differs from the expected figure — the actual "
                    "balance is what the demo execution engine will use.",
                )
            self.risk_state.start_of_day(self._equity)
        else:
            log.warning("BALANCE", "Demo balance unavailable (not verified) — demo layer disabled")

        # --- market data -----------------------------------------------
        self.store = MarketDataStore(
            self._inst_id,
            list(self.config.market.timeframes),
            candle_buffer=self.config.data.candle_buffer,
            staleness_budgets={
                "ticker": self.config.data.max_staleness_seconds.ticker,
                "kline": self.config.data.max_staleness_seconds.kline,
                "orderbook": self.config.data.max_staleness_seconds.orderbook,
            },
        )
        self.history = HistoricalDataManager(
            self.client,
            self.repos.market,
            symbol=self._inst_id,
        )

        # --- strategies -------------------------------------------------
        self.registry = StrategyRegistry.build(
            self.config.strategies, available_timeframes=list(self.config.market.timeframes)
        )
        for strategy in self.registry:
            self.repos.strategies.upsert_strategy(strategy.describe())
            self.repos.strategies.upsert_version(
                strategy.id, strategy.version, strategy.params, is_production=True
            )

        # --- backfill ----------------------------------------------------
        if self.config.data.backfill_on_start:
            await self._backfill()
        preconditions.market_data_ready = await self._verify_market_data()

        return preconditions

    def _required_timeframes(self) -> set[str]:
        """Timeframes that must be populated for the engine to function.

        The union of what the strategies need **and** what the regime engine
        needs — the regime context timeframe is often not used by any individual
        strategy, and omitting it would leave the classifier without its
        higher-timeframe view.
        """
        needed: set[str] = set()
        if self.registry is not None:
            needed |= self.registry.required_timeframes()
        needed.add(self.config.market.regime_timeframe)
        needed.add(self.config.market.regime_context_timeframe)
        return {tf for tf in needed if tf in self.config.market.timeframes}

    async def _backfill(self) -> None:
        """Warm the live candle series so strategies can evaluate immediately."""
        assert self.history and self.store and self.registry
        needed = self._required_timeframes()
        log.info("DATA", f"Backfilling {len(needed)} timeframe(s)…")
        for timeframe in sorted(needed, key=lambda tf: interval_seconds(tf)):
            candles = await self.history.backfill_series(
                timeframe, bars=self.config.data.candle_buffer
            )
            series = self.store.series.get(timeframe)
            if series is not None and candles:
                series.extend(candles)
            log.info("DATA", f"  {timeframe}m: {len(candles)} candles")

    async def _verify_market_data(self) -> bool:
        """Confirm BTC market data is genuinely functioning."""
        assert self.client and self.store
        try:
            ticker = await self.client.get_ticker(self._inst_id)
        except (ApiError, TransportError) as exc:
            log.error("DATA", f"Market data check failed: {exc}")
            return False

        trip = self.breakers.check_price(ticker.last_price)
        if trip is not None:
            return False
        self.store.update_ticker(ticker)
        log.info("MARKET", f"{ticker.symbol} {ticker.last_price:,.2f}")

        for timeframe in self._required_timeframes():
            if self.store.bars_available(timeframe) < self.features.MIN_BARS:
                log.warning(
                    "DATA",
                    f"only {self.store.bars_available(timeframe)} closed {timeframe}m bars "
                    f"(need {self.features.MIN_BARS}) — strategies on this timeframe will wait",
                )
        return True

    # =================================================================
    #  START
    # =================================================================

    async def start(self) -> None:
        """Bootstrap, start or resume the experiment, and run until complete."""
        preconditions = await self.bootstrap()
        assert self.repos and self.registry and self.store

        self.experiment = ExperimentManager(
            self.repos.experiments, self.repos.system, self.config, config_hash=self.loaded.config_hash
        )

        if self.dry_run:
            # The 14-day timer must never start in dry run.
            log.banner(
                [
                    "DRY RUN",
                    "Real public market data, NO authenticated orders",
                    "The 14-day experiment timer is NOT running",
                ],
                tag="START",
            )
            await self._run_dry_run()
            return

        if not preconditions.all_met:
            log.error("SAFETY", "Cannot start the experiment. Unmet preconditions:")
            for line in preconditions.describe():
                log.error("SAFETY", line)
            raise DemoVerificationError(
                "preconditions not met: " + ", ".join(preconditions.unmet())
            )

        capabilities = self.discovery.capabilities
        state = self.experiment.start_or_resume(
            mode=self.mode,
            preconditions=preconditions,
            starting_demo_equity=self._equity,
            enabled_strategies=self.registry.ids,
            strategy_versions=self.registry.version_map(),
            demo_category=capabilities.primary.inst_type.value,
            primary_symbol=self._inst_id,
        )
        self._starting_equity = state.starting_demo_equity or self._equity
        self._peak_equity = max(self._equity, self._starting_equity)

        if not state.resumed:
            log.banner(
                self.experiment.start_banner(
                    strategy_count=len(self.registry), actual_equity=self._equity
                ),
                tag="START",
            )
        await self._init_layers(state.experiment_id)
        await self._reconcile()

        self.reports = ReportGenerator(self.repos, self.config, registry=self.registry)
        await self.notifications.notify(
            "Bot started",
            f"Experiment {state.experiment_id} day {state.day}/{state.duration_days} "
            f"with {len(self.registry)} strategies on OKX Demo ({self._inst_id}).",
        )

        await self._run_live()

    async def _init_layers(self, experiment_id: str) -> None:
        """Construct the shadow, allocator, execution, news, and learning layers."""
        assert self.repos and self.registry and self.client and self.guard

        self.shadow = ShadowEngine(
            self.config.shadow,
            self.repos.shadow,
            experiment_id=experiment_id,
            symbol=self._inst_id,
        )
        self.shadow.initialise(self.registry.ids)

        self.allocator = DemoAllocator(
            self.config.allocator,
            self.repos.allocator,
            self.repos.demo_orders,
            experiment_id=experiment_id,
        )
        self.allocator.initialise(self.registry.ids)

        self.ledger = PositionLedger(self.repos.positions, experiment_id=experiment_id)
        self.trade_manager = TradeManager(self.ledger)
        safety_guard = OrderSafetyGuard(self.repos.demo_orders)
        self.decision = DecisionEngine(self.repos.rejected, experiment_id=experiment_id)
        self.executor = DemoExecutor(
            self.client,
            guard=self.guard,
            breakers=self.breakers,
            sizer=PositionSizer(self.config.risk),
            leverage_engine=LeverageEngine(self.config.risk.leverage),
            ledger=self.ledger,
            safety=safety_guard,
            orders=self.repos.demo_orders,
            leverage_decisions=self.repos.leverage,
            rejected_signals=self.repos.rejected,
            system=self.repos.system,
            risk_config=self.config.risk,
            experiment_id=experiment_id,
            position_mode=self._position_mode,
            dry_run=self.dry_run,
        )

        self.news = NewsEngine(self.config.news, self.repos.news, experiment_id=experiment_id)
        self.news.restore_influence()

    # =================================================================
    #  RECONCILIATION
    # =================================================================

    async def _reconcile(self) -> None:
        """Compare exchange state with our records before trading resumes."""
        assert self.client and self.ledger and self.repos and self.guard
        if not self.guard.orders_permitted():
            return

        log.info("RECOVERY", "Reconciling exchange state with the local ledger…")
        capabilities = self.discovery.capabilities
        instrument = capabilities.primary
        inst_id = self._inst_id

        restored = self.ledger.restore()

        try:
            open_orders = await self.client.get_open_orders(inst_id)
            balance = await self.client.get_wallet_balance()
            executions = await self.client.get_executions(inst_id, limit=50)
            exchange_positions = await self.client.get_positions(inst_id)
        except (ApiError, TransportError) as exc:
            log.error("RECOVERY", f"Reconciliation queries failed: {exc}")
            return

        self._equity = balance.total_equity
        self._available = balance.total_available or balance.total_equity
        self._peak_equity = max(self._peak_equity, self._equity)
        self.repos.market.record_balance(
            {
                "experiment_id": self.experiment.require_state().experiment_id,
                "ts_utc": iso(now_utc()),
                "account_type": "OKX_DEMO",
                "total_equity": balance.total_equity,
                "available": self._available,
                "wallet_balance": balance.total_wallet_balance,
                "unrealized_pnl": balance.unrealized_pnl,
                "coins": {k: v for k, v in list(balance.coins.items())[:10]},
                "source": "rest",
            }
        )

        # Backfill any fills we missed while offline — idempotent by execId.
        for execution in executions:
            self.executor.record_fill(execution)

        if open_orders:
            log.warning(
                "RECOVERY",
                f"{len(open_orders)} order(s) still open at the exchange — cancelling to "
                "restore a known-clean state before resuming",
            )
            with contextlib.suppress(ApiError, TransportError):
                await self.client.cancel_all(inst_id)

        # Resolve in-flight orders whose outcome we never recorded.
        for order in self.repos.demo_orders.in_flight():
            match = next(
                (o for o in open_orders if o.client_order_id == order["client_order_id"]), None
            )
            if match is None:
                filled = any(e.client_order_id == order["client_order_id"] for e in executions)
                self.repos.demo_orders.mark_result(
                    order["client_order_id"],
                    status="filled" if filled else "cancelled",
                    reject_reason=None if filled else "not present at exchange on reconciliation",
                )

        # Exchange positions vs ledger. A mismatch is reported, not
        # auto-corrected: guessing here could double a position.
        live = [p for p in exchange_positions if abs(p.contracts) > 0]
        exchange_base = sum(
            float(instrument.base_from_contracts(abs(p.contracts))) for p in live
        )
        ledger_qty = sum(p.remaining_qty for p in self.ledger.open_positions())
        if abs(exchange_base - ledger_qty) > max(1e-8, ledger_qty * 0.02):
            log.warning(
                "RECOVERY",
                f"Exchange holds {exchange_base:.8f} {instrument.base_ccy} across "
                f"{len(live)} position(s) but the ledger records {ledger_qty:.8f}. Trading "
                "continues with the ledger as the authority for attribution; review "
                "manually if this persists.",
            )
            self.repos.system.event(
                "state_mismatch",
                "exchange position differs from ledger",
                level="WARNING",
                experiment_id=self.experiment.require_state().experiment_id,
                payload={"exchange": exchange_base, "ledger": ledger_qty},
            )
        for position in live:
            if position.margin_ratio is not None:
                self._latest_margin_ratio = position.margin_ratio

        self.breakers.reset_counters()
        log.info(
            "RECOVERY",
            f"Exchange/database reconciled ({len(restored)} open position(s), "
            f"equity ${self._equity:,.2f})",
        )

    # =================================================================
    #  LIVE LOOP
    # =================================================================

    async def _run_live(self) -> None:
        self._running = True
        await self._start_streams()

        self._tasks = [
            asyncio.create_task(self._bar_worker(), name="bars"),
            asyncio.create_task(self._health_loop(), name="health"),
            asyncio.create_task(self._news_loop(), name="news"),
            asyncio.create_task(self._learning_loop(), name="learning"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance"),
            asyncio.create_task(self._experiment_clock(), name="clock"),
        ]
        try:
            await self._shutdown_event.wait()
        finally:
            await self.shutdown()

    async def _run_dry_run(self, duration_seconds: int = 90) -> None:
        """Short validation run: real public data, no authenticated orders."""
        self._running = True
        await self._start_streams(public_only=True)
        self._tasks = [asyncio.create_task(self._bar_worker(), name="bars")]

        log.info("START", f"Dry run active for {duration_seconds}s…")
        deadline = now_utc() + timedelta(seconds=duration_seconds)
        checks = {"ticker": False, "candles": False, "features": False, "regime": False}

        while now_utc() < deadline and not self._shutdown_event.is_set():
            await asyncio.sleep(5)
            assert self.store
            if self.store.ticker is not None:
                checks["ticker"] = True
            timeframe = self.config.market.regime_timeframe
            if self.store.bars_available(timeframe) >= self.features.MIN_BARS:
                checks["candles"] = True
                series = self.store.series[timeframe]
                feature_set = self.features.compute(series)
                if feature_set is not None:
                    checks["features"] = True
                    snapshot = self.classifier.classify(feature_set)
                    self._current_regime = snapshot
                    checks["regime"] = True
                    log.info(
                        "REGIME",
                        f"{snapshot.regime.value} (confidence {snapshot.confidence:.2f})",
                    )
            log.info(
                "MARKET",
                f"{self.store.symbol} {self.store.last_price:,.2f} "
                f"spread {self.store.spread_bps:.2f}bps",
            )

        await self.shutdown()
        log.banner(
            ["DRY RUN COMPLETE"]
            + [f"{name}: {'OK' if ok else 'NOT OBSERVED'}" for name, ok in checks.items()]
            + ["The 14-day timer did NOT start."],
            tag="START",
        )

    async def _start_streams(self, *, public_only: bool = False) -> None:
        assert self.store and self.discovery

        if self.config.exchange.public_ws_enabled:
            self.public_stream = PublicMarketStream(
                inst_id=self._inst_id,
                timeframes=list(self.config.market.timeframes),
                orderbook_depth=self.config.exchange.orderbook_depth,
                ping_interval=self.config.exchange.ws_ping_interval_seconds,
                max_backoff=self.config.exchange.ws_reconnect_max_backoff_seconds,
                profile=self.profile,
            )
            self.public_stream.on_candle(self._on_candle)
            self.public_stream.on_ticker(self._on_ticker)
            self.public_stream.on_orderbook(self._on_orderbook)
            self.public_stream.on_trades(self._on_trades)
            self.public_stream.on_funding(self._on_funding)
            await self.public_stream.start()
            await self.public_stream.wait_connected(timeout=20)

        if not public_only and self.config.exchange.private_ws_enabled and self.credentials:
            self.private_stream = PrivateAccountStream(
                api_key=self.credentials.api_key,
                api_secret=self.credentials.api_secret,
                passphrase=self.credentials.passphrase,
                ping_interval=self.config.exchange.ws_ping_interval_seconds,
                max_backoff=self.config.exchange.ws_reconnect_max_backoff_seconds,
                profile=self.profile,
            )
            self.private_stream.on_execution(self._on_execution)
            self.private_stream.on_wallet(self._on_wallet)
            self.private_stream.on_position(self._on_position)
            await self.private_stream.start()

    # --- stream handlers --------------------------------------------------

    async def _on_candle(self, interval: str, candle: Candle) -> None:
        assert self.store
        is_new = self.store.update_candle(interval, candle)
        if candle.confirmed and is_new:
            with contextlib.suppress(asyncio.QueueFull):
                self._bar_queue.put_nowait((interval, candle))

    async def _on_ticker(self, ticker: Ticker) -> None:
        assert self.store
        self.store.update_ticker(ticker)
        price = self.store.last_price
        if price > 0:
            trip = self.breakers.check_price(price)
            if trip is None:
                await self._manage_open_positions(price)
                if self.shadow:
                    self.shadow.mark_to_market(price)

    async def _on_orderbook(self, data: dict[str, Any], msg_type: str) -> None:
        assert self.store
        self.store.update_orderbook(data, msg_type)

    async def _on_trades(self, trades: list[dict[str, Any]]) -> None:
        assert self.store
        self.store.update_trades(trades)

    async def _on_funding(self, item: dict[str, Any]) -> None:
        """Track live funding, next funding time, and open interest."""
        channel = item.get("channel")
        if channel == "funding-rate":
            with contextlib.suppress(TypeError, ValueError):
                self._latest_funding_rate = float(item.get("fundingRate") or 0.0)
                next_ms = item.get("nextFundingTime") or item.get("fundingTime")
                if next_ms:
                    self._next_funding_ms = int(next_ms)
                # Shadow accounts accrue funding at the exchange's real rate
                # rather than the modelled default.
                if self.shadow is not None and self._latest_funding_rate is not None:
                    self.shadow.set_funding_rate(self._latest_funding_rate)
        elif channel == "open-interest":
            with contextlib.suppress(TypeError, ValueError):
                value = float(item.get("oi") or 0.0)
                if value > 0:
                    # Keep exactly one prior reading so a change can be measured.
                    self._prev_open_interest = self._latest_open_interest
                    self._latest_open_interest = value

    async def _on_execution(self, data: list[dict[str, Any]]) -> None:
        if self.executor is None:
            return
        for item in data:
            self.executor.record_fill(Execution.from_order_update(item))

    async def _on_wallet(self, data: list[dict[str, Any]]) -> None:
        """Private ``account`` channel — same shape as the REST balance data."""
        from ..utils.timeutil import now_ms

        for account in data:
            try:
                balance = WalletBalance.from_response(account, ts_ms=now_ms())
            except (TypeError, ValueError):
                continue
            if balance.total_equity > 0:
                self._equity = balance.total_equity
                self._peak_equity = max(self._peak_equity, self._equity)
            if balance.total_available > 0:
                self._available = balance.total_available

    async def _on_position(self, data: list[dict[str, Any]]) -> None:
        """Private ``positions`` channel — liquidation-protection monitoring."""
        for item in data:
            if item.get("instId") != self._inst_id:
                continue
            ratio = item.get("mgnRatio")
            if ratio in (None, ""):
                continue
            with contextlib.suppress(TypeError, ValueError):
                self._latest_margin_ratio = float(ratio)
                trip = self.breakers.check_liquidation_risk(
                    margin_ratio=self._latest_margin_ratio, inst_id=self._inst_id
                )
                if trip is not None:
                    await self._flatten_for_liquidation_protection(trip.reason)

    async def _flatten_for_liquidation_protection(self, reason: str) -> None:
        """Close every live position at market — margin ratio hit the floor."""
        if not (self.ledger and self.executor and self.store):
            return
        price = self.store.last_price
        for position in self.ledger.open_positions():
            result = await self.executor.submit_exit(
                position,
                exit_reason="liquidation_protection",
                capabilities=self.discovery.capabilities,
            )
            if result.success and price > 0:
                trade = self.ledger.close(
                    position,
                    exit_price=price,
                    exit_reason="liquidation_protection",
                    exit_order_id=result.client_order_id,
                    fees=0.0,
                )
                log.critical(
                    "SAFETY",
                    f"Flattened {position.strategy_id} for liquidation protection "
                    f"({trade['r_multiple']:+.2f}R): {reason}",
                )

    # --- bar processing ---------------------------------------------------

    async def _bar_worker(self) -> None:
        """Process each newly closed bar exactly once."""
        while self._running:
            try:
                interval, candle = await asyncio.wait_for(self._bar_queue.get(), timeout=5.0)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            try:
                await self._on_bar_closed(interval, candle)
            except BtcBotError as exc:
                log.error("STRATEGY", f"Bar processing failed ({interval}m): {exc}")
            except Exception as exc:  # noqa: BLE001 - one bad bar must not kill the run
                log.error(
                    "STRATEGY",
                    f"Unexpected error processing {interval}m bar: {type(exc).__name__}: {exc}",
                    exc_info=True,
                )

    async def _on_bar_closed(self, interval: str, candle: Candle) -> None:
        assert self.store and self.registry and self.shadow and self.repos

        # 1. Regime — recomputed on its own timeframe.
        if interval == self.config.market.regime_timeframe:
            await self._update_regime()
        if self._current_regime is None:
            await self._update_regime()
            if self._current_regime is None:
                return

        # 2. Shadow exits on this timeframe.
        atr_map = self._atr_by_timeframe()
        closed = self.shadow.on_bar(candle, atr_by_timeframe=atr_map)
        for trade in closed:
            log.info(
                "EXIT",
                f"{trade['strategy_id']} {trade['exit_reason']} "
                f"{trade['r_multiple']:+.2f}R (shadow)",
            )

        # 3. Strategies whose primary timeframe just closed.
        strategies = [s for s in self.registry if s.primary_timeframe == interval]
        if not strategies:
            return

        context = self._build_context()
        if context is None:
            return

        demo_candidates: list[tuple[str, StrategySignal]] = []
        for strategy in strategies:
            signal = await self._evaluate_strategy(strategy, context, candle)
            if signal is not None:
                demo_candidates.append((strategy.id, signal))

        # 4. Allocate at most one real demo order.
        if demo_candidates and self._demo_layer_active():
            await self._allocate_demo(demo_candidates)

    async def _evaluate_strategy(
        self, strategy: Strategy, context: StrategyContext, candle: Candle
    ) -> StrategySignal | None:
        """Generate, journal, and shadow-route one strategy's signal."""
        assert self.repos and self.shadow and self.store

        try:
            signal = strategy.generate_signal(context)
        except Exception as exc:  # noqa: BLE001 - a broken strategy must not stop the others
            log.error("STRATEGY", f"{strategy.id} raised {type(exc).__name__}: {exc}")
            return None
        if signal is None:
            return None

        trip = self.breakers.check_strategy_output(signal)
        if trip is not None:
            return None

        features = context.tf(strategy.primary_timeframe)
        assert features is not None
        atr = features.last("atr14")

        signal_uid = make_signal_id(
            strategy.id, strategy.version, signal.symbol, signal.timeframe,
            signal.bar_open_ms, signal.direction.value,
        )
        setup_uid = make_setup_id(
            strategy.id, signal.symbol, signal.timeframe, signal.bar_open_ms, signal.setup_key
        )

        accepted = True
        rejection: str | None = None
        if context.news_blocks_entry:
            accepted, rejection = False, "news_high_impact_window"
        elif not self.store.health().healthy:
            accepted, rejection = False, "stale_market_data"

        experiment_id = self.experiment.require_state().experiment_id
        is_new = self.repos.signals.record(
            {
                "signal_id": signal_uid,
                "experiment_id": experiment_id,
                "setup_id": setup_uid,
                "strategy_id": strategy.id,
                "strategy_version": strategy.version,
                "ts_utc": iso(now_utc()),
                "bar_open_ms": signal.bar_open_ms,
                "symbol": signal.symbol,
                "timeframe": signal.timeframe,
                "direction": signal.direction.value,
                "regime": signal.regime.value,
                "regime_confidence": signal.regime_confidence,
                "entry_reference": signal.entry_reference,
                "stop_price": signal.stop_price,
                "target_price": signal.target_price,
                "confidence": signal.confidence,
                "rr_ratio": signal.rr_ratio,
                "accepted": accepted,
                "rejection_reason": rejection,
                "routed_to": "shadow" if accepted else "none",
                "market_features": signal.features_snapshot,
                "news_features": {
                    "risk": context.news_risk,
                    "blocks_entry": context.news_blocks_entry,
                    "bias": context.news_direction_bias,
                },
                "explanation": strategy.explain_signal(signal),
            }
        )
        if not is_new:
            return None  # already journaled — restart or duplicate bar

        if not accepted:
            return None

        log.info(
            "STRATEGY",
            f"{strategy.id} signal {signal.direction.value.upper()} @ "
            f"{signal.entry_reference:,.2f} (confidence {signal.confidence:.2f})",
        )

        news_state = self._news_label()
        volatility_state = self._volatility_state(features)
        if self.shadow.on_signal(
            strategy,
            signal,
            atr=atr if atr == atr else 0.0,  # NaN-safe
            news_state=news_state,
            volatility_state=volatility_state,
        ):
            log.info("SHADOW", f"{strategy.id} entered {signal.direction.value}")

        return signal

    async def _allocate_demo(self, candidates: list[tuple[str, StrategySignal]]) -> None:
        """Run the decision layers, then let the allocator pick one real order."""
        assert self.allocator and self.executor and self.ledger and self.store

        # --- decision engine layers 1–7 (per candidate, journaled) --------
        risk_state = self.risk_state.evaluate(
            equity=self._equity, peak_equity=self._peak_equity
        )
        health = self.store.health()
        context = self._build_context()
        surviving: list[tuple[str, StrategySignal]] = []
        if self.decision is not None and context is not None:
            for strategy_id, signal in candidates:
                strategy = self.registry.get(strategy_id) if self.registry else None
                if strategy is None:
                    continue
                setup_uid = make_setup_id(
                    strategy_id, signal.symbol, signal.timeframe, signal.bar_open_ms,
                    signal.setup_key,
                )
                signal_uid = make_signal_id(
                    strategy_id, signal.strategy_version, signal.symbol, signal.timeframe,
                    signal.bar_open_ms, signal.direction.value,
                )
                outcome = self.decision.evaluate(
                    strategy,
                    signal,
                    context,
                    data_healthy=health.healthy,
                    data_detail=health.describe(),
                    risk_state=risk_state,
                    signal_id=signal_uid,
                    setup_id=setup_uid,
                )
                if outcome.accepted:
                    surviving.append((strategy_id, signal))
                else:
                    log.info("DECISION", f"{strategy_id}: {outcome.describe()}")
        else:
            surviving = candidates
        if not surviving:
            return

        decision = self.allocator.allocate(
            surviving,
            regime=self._current_regime.regime.value if self._current_regime else "UNCERTAIN",
            position_open=self.ledger.has_open_position,
        )
        if not decision.granted or decision.signal is None or decision.strategy_id is None:
            log.debug("ALLOC", f"No demo allocation: {decision.reason}")
            return

        signal = decision.signal
        strategy_id = decision.strategy_id
        log.info("DEMO", f"{strategy_id} allocated actual Demo trade — {decision.reason}")

        features = self._features_for(signal.timeframe)
        atr = features.last("atr14") if features else 0.0
        arm = self.allocator.arms.get(strategy_id)
        setup_uid = make_setup_id(
            strategy_id, signal.symbol, signal.timeframe, signal.bar_open_ms, signal.setup_key
        )
        signal_uid = make_signal_id(
            strategy_id, signal.strategy_version, signal.symbol, signal.timeframe,
            signal.bar_open_ms, signal.direction.value,
        )
        news_state = self.news.state_at() if self.news else None
        drawdown = safe_div(self._peak_equity - self._equity, self._peak_equity)

        result = await self.executor.submit_entry(
            signal,
            setup_id=setup_uid,
            signal_id=signal_uid,
            capabilities=self.discovery.capabilities,
            equity=self._equity,
            available=self._available,
            atr=atr if atr == atr else 0.0,
            expectancy_r=arm.demo_expectancy if arm else 0.0,
            observations=arm.demo_observations if arm else 0,
            drawdown_pct=drawdown,
            spread_bps=self.store.spread_bps,
            news_size_factor=news_state.size_factor if news_state else 1.0,
            news_state=news_state.label if news_state else None,
            volatility_pct=features.atr_pct if features else None,
            risk_state=risk_state.state,
        )
        if result.success:
            self.allocator.confirm_allocation(strategy_id)
            self.allocator.persist()

    # --- position management ----------------------------------------------

    async def _manage_open_positions(self, price: float) -> None:
        """Apply exit policies to real demo positions on every price update."""
        if not (self.ledger and self.trade_manager and self.executor):
            return
        if not self.ledger.has_open_position:
            return

        health = self.store.health() if self.store else None
        for position in self.ledger.open_positions():
            if health is not None and not health.healthy:
                self.trade_manager.stale_data_action(position)
                continue

            features = self._features_for(self.config.market.regime_timeframe)
            atr = features.last("atr14") if features else None
            decision = self.trade_manager.evaluate(
                position, price=price, atr=atr if atr and atr == atr else None
            )
            if not decision.should_exit:
                continue

            result = await self.executor.submit_exit(
                position,
                exit_reason=decision.reason,
                capabilities=self.discovery.capabilities,
                quantity=decision.partial_quantity,
            )
            if decision.partial_quantity is not None:
                if result.success:
                    self.ledger.reduce(position, decision.partial_quantity)
                continue

            trade = self.ledger.close(
                position,
                exit_price=price,
                exit_reason=decision.reason,
                exit_order_id=result.client_order_id,
                fees=0.0,
            )
            log.info(
                "EXIT",
                f"{position.strategy_id} {decision.reason} {trade['r_multiple']:+.2f}R "
                f"(demo, ${trade['pnl']:+,.2f})",
            )
            if self.allocator:
                self.allocator.record_result(position.strategy_id, trade["r_multiple"])
                self.allocator.persist()

    # --- context helpers ---------------------------------------------------

    def _build_context(self) -> StrategyContext | None:
        assert self.store
        if self._current_regime is None:
            return None

        by_timeframe = {}
        for timeframe, series in self.store.series.items():
            feature_set = self.features.compute(series)
            if feature_set is not None:
                by_timeframe[timeframe] = feature_set
        if not by_timeframe:
            return None

        news_state = self.news.state_at() if self.news else None
        return StrategyContext(
            symbol=self.store.symbol,
            features=MultiTimeframeFeatures(
                symbol=self.store.symbol,
                by_timeframe=by_timeframe,
                orderbook_imbalance=self.store.orderbook.imbalance(),
                trade_flow_imbalance=self.store.trade_flow.imbalance(),
                spread_bps=self.store.spread_bps,
                orderbook_valid=self.store.orderbook.valid,
                funding_rate=self._latest_funding_rate,
                next_funding_ms=self._next_funding_ms,
                open_interest=self._latest_open_interest,
                open_interest_prev=self._prev_open_interest,
            ),
            regime=self._current_regime,
            news_risk=news_state.risk_level if news_state else 0.0,
            news_blocks_entry=news_state.blocks_entry if news_state else False,
            news_direction_bias=news_state.directional_bias if news_state else 0.0,
            spread_bps=self.store.spread_bps,
            equity=self._equity,
        )

    async def _update_regime(self) -> None:
        assert self.store and self.repos
        timeframe = self.config.market.regime_timeframe
        series = self.store.series.get(timeframe)
        if series is None:
            return
        features = self.features.compute(series)
        if features is None:
            return

        context_series = self.store.series.get(self.config.market.regime_context_timeframe)
        context_features = self.features.compute(context_series) if context_series else None

        snapshot = self.classifier.classify(features, context=context_features)
        changed = self.regime_tracker.add(snapshot)
        self._current_regime = snapshot

        self.repos.market.record_regime(
            snapshot.to_row(
                experiment_id=self.experiment.state.experiment_id if self.experiment and self.experiment.state else None,
                ts_utc=iso(now_utc()),
            )
        )
        if changed:
            log.info("REGIME", snapshot.describe())

    def _features_for(self, timeframe: str):
        if self.store is None:
            return None
        series = self.store.series.get(timeframe)
        return self.features.compute(series) if series else None

    def _atr_by_timeframe(self) -> dict[str, float]:
        result: dict[str, float] = {}
        if self.store is None:
            return result
        for timeframe in self.store.series:
            features = self._features_for(timeframe)
            if features is None:
                continue
            atr = features.last("atr14")
            if atr == atr and atr > 0:
                result[timeframe] = atr
        return result

    def _news_label(self) -> str:
        if self.news is None:
            return "calm"
        return self.news.state_at().label

    @staticmethod
    def _volatility_state(features) -> str:
        atr_pct = features.atr_pct
        if not atr_pct or atr_pct != atr_pct:
            return "unknown"
        if atr_pct > 0.02:
            return "high"
        if atr_pct < 0.005:
            return "low"
        return "normal"

    def _demo_layer_active(self) -> bool:
        return bool(
            self.guard
            and self.guard.orders_permitted()
            and not self.dry_run
            and self.breakers.orders_allowed()[0]
        )

    # =================================================================
    #  BACKGROUND LOOPS
    # =================================================================

    async def _health_loop(self) -> None:
        """Watch data freshness, API errors, clock drift, and re-verify demo status."""
        last_verification = now_utc()
        last_clock_sync = now_utc()
        last_funding_poll = now_utc()
        while self._running:
            await asyncio.sleep(20)
            try:
                assert self.store and self.repos
                health = self.store.health()
                experiment_id = self.experiment.require_state().experiment_id

                if not health.healthy:
                    if self._outage_id is None:
                        self._outage_id = self.repos.system.start_outage(
                            experiment_id, "market_data", health.describe()
                        )
                        log.warning("SAFETY", f"Trading paused: {health.describe()}")
                    self.breakers.check_data_freshness(False, health.describe())
                elif self._outage_id is not None:
                    outage = self.repos.system.open_outage(experiment_id, "market_data")
                    if outage:
                        started = outage["started_ts_utc"]
                        from ..utils.timeutil import parse_iso

                        duration = int((now_utc() - parse_iso(started)).total_seconds())
                        self.repos.system.end_outage(self._outage_id, duration)
                        log.info("RECOVERY", f"Market data recovered after {duration}s")
                        await self._recover_after_outage()
                    self._outage_id = None

                if self.client:
                    self.breakers.check_api_errors(self.client.consecutive_errors)

                # Clock drift: re-measure on a schedule; excessive drift trips
                # a breaker (pausing orders) and revokes demo verification
                # until a clean re-verification passes.
                if (
                    self.client
                    and (now_utc() - last_clock_sync).total_seconds()
                    >= self.config.safety.clock_resync_interval_minutes * 60
                ):
                    with contextlib.suppress(ApiError, TransportError):
                        await self.client.sync_clock()
                    last_clock_sync = now_utc()
                    trip = self.breakers.check_clock_drift(self.client.clock_offset_ms)
                    if trip is not None and self.guard:
                        self.guard.revoke(trip.reason)

                # Funding bills: poll and journal — funding is part of PnL.
                if (
                    self.guard
                    and self.guard.orders_permitted()
                    and (now_utc() - last_funding_poll).total_seconds() >= 600
                ):
                    await self._poll_funding(experiment_id)
                    last_funding_poll = now_utc()

                if (
                    self.guard
                    and self.credentials
                    and (now_utc() - last_verification).total_seconds()
                    >= self.config.safety.reverify_interval_minutes * 60
                ):
                    verification = await self.guard.verify()
                    last_verification = now_utc()
                    if not verification.verified:
                        await self.notifications.notify(
                            "SAFETY LOCK",
                            "OKX demo verification failed on re-check — order submission disabled.",
                        )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the health loop must never die
                log.error("SAFETY", f"Health loop error: {type(exc).__name__}: {exc}")

    async def _poll_funding(self, experiment_id: str) -> None:
        """Record realised funding payments and attribute them to positions."""
        if not self.client:
            return
        try:
            bills = await self.client.get_funding_bills(limit=50)
        except (ApiError, TransportError) as exc:
            log.debug("OKX", f"Funding-bill poll failed: {exc}")
            return
        open_position = self.ledger.current() if self.ledger else None
        for bill in bills:
            if bill.get("instId") != self._inst_id:
                continue
            bill_id = bill.get("billId")
            if not bill_id:
                continue
            amount = float(bill.get("pnl") or bill.get("balChg") or 0.0)
            inserted = self.repos.funding.record(
                {
                    "bill_id": str(bill_id),
                    "experiment_id": experiment_id,
                    "ts_utc": iso(now_utc()),
                    "inst_id": self._inst_id,
                    "amount": amount,
                    "currency": bill.get("ccy"),
                    "position_id": open_position.position_id if open_position else None,
                    "strategy_id": open_position.strategy_id if open_position else None,
                    "raw": bill,
                }
            )
            if inserted and open_position is not None:
                # Funding *paid* increases the position's cost; received reduces it.
                self.repos.positions.add_funding_fee(open_position.position_id, -amount)
                open_position.fees += max(0.0, -amount)

    async def _recover_after_outage(self) -> None:
        """Refill missing candles and re-reconcile after connectivity returns."""
        if not (self.history and self.store and self.registry):
            return
        log.info("RECOVERY", "Downloading missing market data…")
        for timeframe in self._required_timeframes():
            series = self.store.series.get(timeframe)
            if series is None:
                continue
            last = series.last_closed()
            since = last.open_ms if last else None
            if since is None:
                continue
            recovered = await self.history.recover_missing(timeframe, since)
            if recovered:
                series.extend(recovered)
        await self._reconcile()

    async def _news_loop(self) -> None:
        if self.news is None or not self.config.news.enabled:
            return
        while self._running:
            try:
                await self.news.poll()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - news must never stop trading
                log.warning("NEWS", f"News poll failed: {type(exc).__name__}: {exc}")
            await asyncio.sleep(self.config.news.poll_interval_seconds)

    async def _learning_loop(self) -> None:
        if not self.config.learning.enabled:
            return
        interval = self.config.learning.evaluation_interval_minutes * 60
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._run_learning_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("LEARNING", f"Learning cycle failed: {type(exc).__name__}: {exc}")

    async def _run_learning_cycle(self) -> None:
        """Recompute metrics, refresh allocator evidence, calibrate, analyse losses."""
        assert self.repos and self.registry and self.allocator and self.shadow
        experiment_id = self.experiment.require_state().experiment_id

        shadow_trades = await self.db.run(
            self.repos.shadow.closed_trades, experiment_id=experiment_id
        )
        by_strategy: dict[str, list[dict[str, Any]]] = {}
        for trade in shadow_trades:
            by_strategy.setdefault(trade["strategy_id"], []).append(trade)

        metrics_map = {}
        shadow_evidence: dict[str, tuple[float, int]] = {}
        for strategy_id, trades in by_strategy.items():
            metrics = compute_metrics(
                trades,
                strategy_id=strategy_id,
                layer="shadow",
                initial_equity=self.config.shadow.initial_equity,
                bootstrap_samples=500,
            )
            metrics_map[strategy_id] = metrics
            shadow_evidence[strategy_id] = (metrics.expectancy_r, metrics.total_trades)
            self.repos.performance.snapshot(
                {
                    "experiment_id": experiment_id,
                    "ts_utc": iso(now_utc()),
                    "day_index": self.experiment.require_state().day,
                    "strategy_id": strategy_id,
                    "layer": "shadow",
                    "metrics": metrics.as_dict(),
                    "score": metrics.expectancy_r,
                }
            )

        regime_map = regime_expectancy_map(metrics_map)
        self.allocator.update_evidence(shadow=shadow_evidence, regime_fitness=regime_map)
        self.allocator.persist()

        weights = {
            sid: max(0.1, 1.0 + m.expectancy_r) for sid, m in metrics_map.items()
        }
        self.registry.update_ensemble_evidence(weights, regime_map)

        analyzer = LossAnalyzer(self.config.learning)
        calibrator = ConfidenceCalibrator(self.config.learning, self.repos.performance)
        for strategy_id, trades in by_strategy.items():
            metrics = metrics_map[strategy_id]
            analysis = analyzer.analyse(strategy_id, trades, metrics)
            for finding in analysis.findings:
                if finding.severity in {"warning", "critical"}:
                    log.info("LEARNING", f"{strategy_id}: {finding.message}")
            calibrator.calibrate(strategy_id, trades, experiment_id=experiment_id)

        if self.news:
            self.news.evaluate_effectiveness(shadow_trades)

        self.shadow.persist()
        log.debug("LEARNING", f"Learning cycle complete for {len(metrics_map)} strategies")

    async def _maintenance_loop(self) -> None:
        """Backups, daily reports, and instrument refresh."""
        last_backup = now_utc()
        last_instruments = now_utc()
        while self._running:
            await asyncio.sleep(300)
            try:
                now = now_utc()
                if (now - last_backup).total_seconds() >= self.config.database.backup_interval_hours * 3600:
                    assert self.db
                    await self.db.run(self.db.backup, self.config.database.backup_dir)
                    await self.db.run(self.db.prune_backups, self.config.database.backup_dir)
                    last_backup = now

                if (now - last_instruments).total_seconds() >= self.config.exchange.instrument_refresh_minutes * 60:
                    if self.discovery:
                        await self.discovery.refresh()
                    last_instruments = now

                state = self.experiment.require_state()
                if state.day > self._last_report_day and self.reports:
                    self._last_report_day = state.day
                    # New UTC experiment day: reset the daily-loss baseline.
                    self.risk_state.start_of_day(self._equity)
                    await self.db.run(self.reports.daily_report, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error("REPORT", f"Maintenance loop error: {type(exc).__name__}: {exc}")

    async def _experiment_clock(self) -> None:
        """Watch the countdown and trigger finalisation at the scheduled end."""
        while self._running:
            await asyncio.sleep(30)
            state = self.experiment.require_state() if self.experiment else None
            if state is None:
                continue
            if state.is_complete and not self._finalized:
                await self.finalize()
                return

    # =================================================================
    #  FINALISATION
    # =================================================================

    async def finalize(self) -> None:
        """Day-14: freeze, evaluate all three layers, rank, choose a champion."""
        if self._finalized:
            return
        self._finalized = True
        assert self.experiment and self.repos and self.registry and self.shadow

        state = self.experiment.begin_finalization()
        log.info("EXPERIMENT", "Stopping new research-exploration demo entries")

        # 1. Handle any open real position per the finalisation policy.
        if self.ledger and self.trade_manager and self.executor and self.store:
            price = self.store.last_price
            for position in self.ledger.open_positions():
                decision = self.trade_manager.finalization_exit(
                    position, self.config.experiment.finalization_policy
                )
                if decision.should_exit and price > 0:
                    result = await self.executor.submit_exit(
                        position,
                        exit_reason="experiment_end",
                        capabilities=self.discovery.capabilities,
                    )
                    trade = self.ledger.close(
                        position,
                        exit_price=price,
                        exit_reason="experiment_end",
                        exit_order_id=result.client_order_id,
                        fees=0.0,
                    )
                    log.info("EXIT", f"Closed {position.strategy_id} at experiment end "
                                     f"({trade['r_multiple']:+.2f}R)")

        # 2. Close shadow positions so every account has a final, comparable figure.
        if self.store and self.store.last_price > 0:
            self.shadow.force_close_all(self.store.last_price)
            self.shadow.persist()

        # 3. Assemble evidence from all three layers.
        log.info("RANK", "Running final validation across all three evidence layers…")
        evidence_map = await self._build_final_evidence(state.experiment_id)

        # 4. Score and rank.
        scorer = StrategyScorer(self.config.scoring)
        breakdowns = [scorer.score(evidence) for evidence in evidence_map.values()]
        ranked = rank_strategies(breakdowns)
        for index, breakdown in enumerate(ranked[:10], start=1):
            log.info(
                "RANK",
                f"#{index} {breakdown.strategy_id} score {breakdown.final_score:.1f} "
                f"({breakdown.confidence} confidence, {breakdown.total_observations} observations)",
            )

        # 5. Champion selection.
        selector = ChampionSelector(self.config.champion, self.repos.champion)
        regime_performance = {
            sid: {
                regime: stats.get("expectancy_r", 0.0)
                for regime, stats in (e.shadow.by_regime.items() if e.shadow else [])
                if stats.get("trades", 0) >= 3
            }
            for sid, e in evidence_map.items()
        }
        selection = selector.select(
            ranked,
            evidence_map,
            experiment_id=state.experiment_id,
            regime_performance=regime_performance,
        )
        log.banner(selection.banner(), tag="CHAMPION")

        # 6. Reports.
        if self.reports:
            paths = await self.db.run(
                self.reports.final_report, state, ranked, selection, evidence_map
            )
            for path in paths:
                log.info("REPORT", f"Written: {path}")

        self.experiment.complete()
        await self.notifications.notify(
            "Day 14 complete",
            f"Champion: {selection.champion_id or 'NONE'} "
            f"({selection.champion_type}, {selection.confidence} confidence)",
        )

        # 7. Transition to champion mode on the same demo account.
        if self.config.experiment.auto_transition_to_champion and selection.champion_id:
            log.info(
                "CHAMPION",
                f"Transitioning to OKX_DEMO_CHAMPION — {selection.champion_id} now controls "
                "actual demo execution; challengers continue in shadow mode.",
            )
            self.repos.system.event(
                "mode_transition",
                f"research → champion ({selection.champion_id})",
                experiment_id=state.experiment_id,
                payload=selection.as_dict(),
            )
        self._shutdown_event.set()

    async def _build_final_evidence(self, experiment_id: str) -> dict[str, StrategyEvidence]:
        """Gather Layer-1, Layer-2 and Layer-3 evidence for every strategy."""
        assert self.repos and self.registry and self.history

        shadow_trades = await self.db.run(
            self.repos.shadow.closed_trades, experiment_id=experiment_id
        )
        demo_positions = await self.db.run(self.repos.positions.closed_positions, experiment_id)

        shadow_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for trade in shadow_trades:
            shadow_by_strategy.setdefault(trade["strategy_id"], []).append(trade)
        demo_by_strategy: dict[str, list[dict[str, Any]]] = {}
        for row in demo_positions:
            demo_by_strategy.setdefault(row["strategy_id"], []).append(
                {
                    "pnl": row.get("realized_pnl"),
                    "r_multiple": row.get("r_multiple"),
                    "regime": row.get("entry_regime"),
                    "entry_regime": row.get("entry_regime"),
                    "confidence": row.get("confidence"),
                    "exit_reason": row.get("exit_reason"),
                    "fees": row.get("fees"),
                    "mfe": row.get("mfe"),
                    "mae": row.get("mae"),
                    "timeframe": "demo",
                    "direction": row.get("direction"),
                    "notional": (row.get("entry_price") or 0) * (row.get("quantity") or 0),
                }
            )

        # Layer 1: historical candles for out-of-sample and walk-forward analysis.
        analyzer = WalkForwardAnalyzer(self.config.backtesting, self.config.regime)
        candles = {
            timeframe: self.history.load(timeframe)
            for timeframe in self.config.data.history_timeframes
        }

        evidence_map: dict[str, StrategyEvidence] = {}
        for strategy in self.registry:
            evidence = StrategyEvidence(
                strategy_id=strategy.id,
                strategy_version=strategy.version,
                parameters=dict(strategy.params),
            )
            shadow_list = shadow_by_strategy.get(strategy.id, [])
            if shadow_list:
                evidence.shadow = compute_metrics(
                    shadow_list,
                    strategy_id=strategy.id,
                    layer="shadow",
                    initial_equity=self.config.shadow.initial_equity,
                    bootstrap_samples=self.config.scoring.bootstrap_samples,
                    bootstrap_confidence=self.config.scoring.bootstrap_confidence,
                )
            demo_list = demo_by_strategy.get(strategy.id, [])
            if demo_list:
                evidence.demo = compute_metrics(
                    demo_list,
                    strategy_id=strategy.id,
                    layer="demo",
                    initial_equity=max(1.0, self._starting_equity),
                    bootstrap_samples=self.config.scoring.bootstrap_samples,
                )

            primary = candles.get(strategy.primary_timeframe, [])
            if len(primary) >= strategy.min_bars * 2:
                try:
                    split = split_candles(
                        primary,
                        train_fraction=self.config.backtesting.train_fraction,
                        validation_fraction=self.config.backtesting.validation_fraction,
                        embargo_bars=self.config.backtesting.embargo_bars,
                    )
                    segments = await self.db.run(
                        evaluate_segments,
                        strategy,
                        candles,
                        split,
                        config=self.config.backtesting,
                        regime_config=self.config.regime,
                        symbol=self._inst_id,
                    )
                    evidence.historical = segments.get("oos")
                    evidence.walk_forward = await self.db.run(
                        analyzer.run, strategy, candles, symbol=self._inst_id
                    )
                except Exception as exc:  # noqa: BLE001 - a failed backtest must not stop ranking
                    log.warning(
                        "BACKTEST", f"Layer-1 evaluation failed for {strategy.id}: {exc}"
                    )

            evidence.regime_stability = self.regime_tracker.stability()
            if evidence.walk_forward:
                evidence.parameter_stability = evidence.walk_forward.expectancy_stability
            evidence_map[strategy.id] = evidence

        return evidence_map

    # =================================================================
    #  SHUTDOWN
    # =================================================================

    def request_shutdown(self) -> None:
        """Signal a graceful stop (called by the Ctrl+C handler)."""
        log.info("STOP", "Shutdown requested — saving state…")
        self._shutdown_event.set()

    async def shutdown(self) -> None:
        """Save all state and close every resource."""
        if not self._running:
            return
        self._running = False

        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self._tasks.clear()

        if self.public_stream:
            await self.public_stream.stop()
        if self.private_stream:
            await self.private_stream.stop()

        with contextlib.suppress(Exception):
            if self.shadow:
                self.shadow.persist()
            if self.allocator:
                self.allocator.persist()

        if self.client:
            await self.client.close()

        if self.db:
            with contextlib.suppress(Exception):
                self.db.backup(self.config.database.backup_dir)
            self.db.close()

        await self.notifications.notify("Bot stopped", "State saved and connections closed.")
        log.info("STOP", "Shutdown complete — state saved")

    # =================================================================
    #  DASHBOARD STATE
    # =================================================================

    def dashboard_state(self) -> dict[str, Any]:
        """Everything the dashboard renders."""
        state = self.experiment.state if self.experiment else None
        health = self.store.health() if self.store else None
        verification = self.guard.last_verification if self.guard else None

        instrument_panel: dict[str, Any] = {}
        if self.discovery is not None:
            try:
                spec = self.discovery.capabilities.primary
                instrument_panel = {
                    "inst_id": spec.inst_id,
                    "inst_type": spec.inst_type.value,
                    "ct_type": spec.ct_type,
                    "ct_val": str(spec.ct_val),
                    "ct_val_ccy": spec.ct_val_ccy,
                    "ct_mult": str(spec.ct_mult),
                    "lot_size": str(spec.lot_size),
                    "min_size": str(spec.min_size),
                    "tick_size": str(spec.tick_size),
                    "max_leverage": str(spec.max_leverage),
                    "settle_ccy": spec.settle_ccy,
                    "position_mode": self._position_mode.value,
                    "margin_mode": "isolated",
                }
            except Exception:  # noqa: BLE001 - discovery may not have run yet
                instrument_panel = {"inst_id": self._inst_id or "not discovered"}

        return {
            "system": {
                "running": self._running,
                "mode": self.mode,
                "dry_run": self.dry_run,
                "region": self.profile.region,
                "environment": self.profile.label,
                "rest_host": self.profile.rest_host,
                "ws_hosts": list(self.profile.ws_urls),
                "demo_verified": bool(self.guard and self.guard.verified),
                "verification_signals": (
                    [
                        {"name": s.name, "passed": s.passed, "detail": s.detail}
                        for s in verification.signals
                    ]
                    if verification
                    else []
                ),
                "data_healthy": bool(health and health.healthy),
                "data_detail": health.describe() if health else "unknown",
                "news_health": self.news.health_summary() if self.news else {"degraded": True},
                "safety": self.breakers.snapshot(),
            },
            "experiment": state.as_dict() if state else None,
            "instrument": instrument_panel,
            "demo_account": {
                "equity": round(self._equity, 2),
                "available": round(self._available, 2),
                "starting_equity": round(self._starting_equity, 2),
                "realized_pnl": round(self._equity - self._starting_equity, 2),
                "peak_equity": round(self._peak_equity, 2),
                "drawdown_pct": round(
                    safe_div(self._peak_equity - self._equity, self._peak_equity) * 100, 2
                ),
                "positions": self.ledger.snapshot() if self.ledger else [],
                "margin_ratio": self._latest_margin_ratio,
                "funding_rate": self._latest_funding_rate,
                "next_funding_ms": self._next_funding_ms,
                "risk_state": self.risk_state.current.as_dict(),
            },
            "decision": {
                "engine": self.decision.stats() if self.decision else {},
                "rejected_recent": (
                    self.repos.rejected.recent(10) if self.repos else []
                ),
                "leverage_recent": (
                    self.repos.leverage.recent(10) if self.repos else []
                ),
            },
            "market": self.store.snapshot() if self.store else {},
            "regime": {
                "current": self._current_regime.regime.value if self._current_regime else "UNKNOWN",
                "confidence": round(self._current_regime.confidence, 3) if self._current_regime else 0.0,
                "distribution": self.regime_tracker.distribution(),
                "stability": round(self.regime_tracker.stability(), 3),
            },
            "research": {
                "strategies": len(self.registry) if self.registry else 0,
                "shadow": self.shadow.snapshot() if self.shadow else {},
                "leaderboard": self.shadow.leaderboard(10) if self.shadow else [],
                "demo_orders": self.executor.stats() if self.executor else {},
                "allocator": self.allocator.snapshot()[:10] if self.allocator else [],
                "allocation_fairness": (
                    round(self.allocator.allocation_fairness(), 3) if self.allocator else 0.0
                ),
            },
            "news": self.news.recent_events(8) if self.news else [],
            "trades": {
                "demo": self.repos.demo_orders.recent(10) if self.repos else [],
                "shadow_open": self.shadow.open_position_count() if self.shadow else 0,
            },
            "learning": {
                "candidates": self.repos.candidates.recent(10) if self.repos else [],
                "news_influence": round(self.news.influence, 3) if self.news else 0.0,
            },
            "champion": self.repos.champion.current() if self.repos else None,
            "updated_at": iso(now_utc()),
        }
