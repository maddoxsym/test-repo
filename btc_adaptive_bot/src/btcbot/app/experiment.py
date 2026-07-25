"""Experiment lifecycle — the exact 14-day window.

The timer starts **only** after all five preconditions hold:

1. configuration passes validation
2. Bybit demo authenticates
3. demo status is positively verified
4. BTC market data is functioning
5. database migrations succeed

After that the start instant is written to SQLite and never rewritten. Restarting
Python, the machine, the dashboard, or the network resumes the *same* experiment
— :meth:`ExperimentManager.start_or_resume` looks for an existing running
experiment before it will create a new one.

Duration is 14 real calendar days in UTC. Crypto trades continuously, so
weekends count and no calendar skipping happens. Outages are recorded but do
**not** extend the window unless the configuration explicitly opts in via
``outage_adjustment_policy: extend_by_outage``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..config.schema import AppConfig
from ..database.repositories import ExperimentRepository, SystemRepository
from ..utils.errors import ExperimentError
from ..utils.ids import experiment_id as make_experiment_id
from ..utils.logging import get_logger
from ..utils.timeutil import day_index, dt_to_ms, humanize_duration, iso, now_utc, parse_iso
from ..version import __version__, git_commit

log = get_logger(__name__)


class ExperimentStatus:
    PENDING = "pending"
    RUNNING = "running"
    FINALIZING = "finalizing"
    COMPLETE = "complete"
    ABORTED = "aborted"


class ExperimentMode:
    RESEARCH = "research"
    CHAMPION = "champion"


@dataclass(slots=True)
class ExperimentState:
    """The live view of the running experiment."""

    experiment_id: str
    name: str
    mode: str
    status: str
    start: datetime
    scheduled_end: datetime
    duration_days: int
    starting_demo_equity: float
    expected_demo_equity: float
    shadow_equity_per_strategy: float
    enabled_strategies: list[str]
    strategy_versions: dict[str, str]
    config_hash: str
    software_version: str
    git_commit: str
    primary_symbol: str
    demo_category: str
    outage_policy: str
    resumed: bool = False

    @property
    def elapsed(self) -> timedelta:
        return now_utc() - self.start

    @property
    def remaining(self) -> timedelta:
        return self.scheduled_end - now_utc()

    @property
    def day(self) -> int:
        return min(self.duration_days, day_index(self.start, now_utc()))

    @property
    def progress_pct(self) -> float:
        total = (self.scheduled_end - self.start).total_seconds()
        if total <= 0:
            return 100.0
        return max(0.0, min(100.0, self.elapsed.total_seconds() / total * 100.0))

    @property
    def is_complete(self) -> bool:
        return now_utc() >= self.scheduled_end

    def countdown_lines(self) -> list[str]:
        return [
            f"DAY {self.day} / {self.duration_days}",
            f"TIME ELAPSED    {humanize_duration(self.elapsed)}",
            f"TIME REMAINING  {humanize_duration(self.remaining)}",
            f"START TIME      {iso(self.start)}",
            f"END TIME        {iso(self.scheduled_end)}",
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "name": self.name,
            "mode": self.mode,
            "status": self.status,
            "day": self.day,
            "duration_days": self.duration_days,
            "progress_pct": round(self.progress_pct, 2),
            "start_ts_utc": iso(self.start),
            "scheduled_end_ts_utc": iso(self.scheduled_end),
            "elapsed": humanize_duration(self.elapsed),
            "remaining": humanize_duration(self.remaining),
            "is_complete": self.is_complete,
            "starting_demo_equity": self.starting_demo_equity,
            "expected_demo_equity": self.expected_demo_equity,
            "shadow_equity_per_strategy": self.shadow_equity_per_strategy,
            "strategy_count": len(self.enabled_strategies),
            "config_hash": self.config_hash,
            "software_version": self.software_version,
            "git_commit": self.git_commit,
            "primary_symbol": self.primary_symbol,
            "demo_category": self.demo_category,
            "resumed": self.resumed,
        }


@dataclass(slots=True)
class Preconditions:
    """The five gates that must pass before the timer may start."""

    config_valid: bool = False
    demo_authenticated: bool = False
    demo_verified: bool = False
    market_data_ready: bool = False
    migrations_applied: bool = False

    @property
    def all_met(self) -> bool:
        return all(
            (
                self.config_valid,
                self.demo_authenticated,
                self.demo_verified,
                self.market_data_ready,
                self.migrations_applied,
            )
        )

    def unmet(self) -> list[str]:
        names = {
            "configuration validation": self.config_valid,
            "Bybit demo authentication": self.demo_authenticated,
            "demo environment verification": self.demo_verified,
            "BTC market data": self.market_data_ready,
            "database migrations": self.migrations_applied,
        }
        return [name for name, ok in names.items() if not ok]

    def describe(self) -> list[str]:
        return [
            f"  [{'x' if ok else ' '}] {name}"
            for name, ok in (
                ("configuration validated", self.config_valid),
                ("Bybit demo authenticated", self.demo_authenticated),
                ("demo environment verified", self.demo_verified),
                ("BTC market data functioning", self.market_data_ready),
                ("database migrations applied", self.migrations_applied),
            )
        ]


class ExperimentManager:
    """Creates, resumes, and finalises the experiment."""

    def __init__(
        self,
        repository: ExperimentRepository,
        system: SystemRepository,
        config: AppConfig,
        *,
        config_hash: str,
    ) -> None:
        self.repo = repository
        self.system = system
        self.config = config
        self.config_hash = config_hash
        self.state: ExperimentState | None = None

    def start_or_resume(
        self,
        *,
        mode: str,
        preconditions: Preconditions,
        starting_demo_equity: float,
        enabled_strategies: list[str],
        strategy_versions: dict[str, str],
        demo_category: str,
    ) -> ExperimentState:
        """Resume the existing experiment, or start a new one if none exists."""
        if not preconditions.all_met:
            raise ExperimentError(
                "cannot start the experiment timer — unmet preconditions: "
                + ", ".join(preconditions.unmet())
            )

        name = self.config.experiment.name
        existing = self.repo.find_active(name)
        if existing is not None:
            state = self._hydrate(existing, resumed=True)
            self.state = state
            log.info(
                "EXPERIMENT",
                f"Resumed experiment {state.experiment_id} — the 14-day timer was NOT restarted",
            )
            for line in state.countdown_lines():
                log.info("EXPERIMENT", line)
            self.system.event(
                "experiment_resumed",
                f"resumed {state.experiment_id} on day {state.day}",
                experiment_id=state.experiment_id,
            )
            self._warn_on_config_drift(existing)
            return state

        start = now_utc()
        duration = self.config.experiment.research_duration_days
        scheduled_end = start + timedelta(days=duration)
        new_id = make_experiment_id(name, dt_to_ms(start), self.config_hash)

        record = {
            "experiment_id": new_id,
            "name": name,
            "mode": mode,
            "status": ExperimentStatus.RUNNING,
            "start_ts_utc": iso(start),
            "scheduled_end_ts_utc": iso(scheduled_end),
            "actual_end_ts_utc": None,
            "duration_days": duration,
            "starting_demo_equity": starting_demo_equity,
            "expected_demo_equity": self.config.experiment.expected_demo_equity,
            "shadow_equity_per_strategy": self.config.shadow.initial_equity,
            "enabled_strategies": enabled_strategies,
            "strategy_versions": strategy_versions,
            "config_hash": self.config_hash,
            "software_version": __version__,
            "git_commit": git_commit(),
            "primary_symbol": self.config.market.primary_symbol,
            "demo_category": demo_category,
            "outage_policy": self.config.experiment.outage_adjustment_policy,
            "metadata": {
                "preconditions": {
                    "config_valid": preconditions.config_valid,
                    "demo_authenticated": preconditions.demo_authenticated,
                    "demo_verified": preconditions.demo_verified,
                    "market_data_ready": preconditions.market_data_ready,
                    "migrations_applied": preconditions.migrations_applied,
                },
                "mainnet_negative_control": self.config.safety.mainnet_negative_control,
            },
        }
        self.repo.create(record)
        state = self._hydrate(self.repo.get(new_id) or record, resumed=False)
        self.state = state

        log.info("EXPERIMENT", f"Started experiment {new_id}")
        self.system.event(
            "experiment_started",
            f"started {new_id} for {duration} days",
            experiment_id=new_id,
            payload={"start": iso(start), "end": iso(scheduled_end)},
        )
        return state

    def _warn_on_config_drift(self, existing: dict[str, Any]) -> None:
        """Flag a configuration change mid-experiment.

        Not fatal — an operator may legitimately adjust logging or the dashboard —
        but it is recorded so the final report can disclose that the conditions
        were not identical throughout.
        """
        if existing.get("config_hash") != self.config_hash:
            log.warning(
                "EXPERIMENT",
                f"Configuration changed since this experiment started "
                f"(was {existing.get('config_hash')}, now {self.config_hash}). "
                "The experiment continues; this is recorded in the report.",
            )
            self.system.event(
                "config_drift",
                "configuration hash changed mid-experiment",
                level="WARNING",
                experiment_id=existing["experiment_id"],
                payload={"old": existing.get("config_hash"), "new": self.config_hash},
            )

    def _hydrate(self, row: dict[str, Any], *, resumed: bool) -> ExperimentState:
        return ExperimentState(
            experiment_id=row["experiment_id"],
            name=row["name"],
            mode=row["mode"],
            status=row["status"],
            start=parse_iso(row["start_ts_utc"]),
            scheduled_end=parse_iso(row["scheduled_end_ts_utc"]),
            duration_days=int(row["duration_days"]),
            starting_demo_equity=float(row["starting_demo_equity"]),
            expected_demo_equity=float(row["expected_demo_equity"]),
            shadow_equity_per_strategy=float(row["shadow_equity_per_strategy"]),
            enabled_strategies=list(row.get("enabled_strategies") or []),
            strategy_versions=dict(row.get("strategy_versions") or {}),
            config_hash=row["config_hash"],
            software_version=row["software_version"],
            git_commit=row["git_commit"],
            primary_symbol=row["primary_symbol"],
            demo_category=row["demo_category"],
            outage_policy=row["outage_policy"],
            resumed=resumed,
        )

    # --- lifecycle transitions -------------------------------------------

    def require_state(self) -> ExperimentState:
        if self.state is None:
            raise ExperimentError("no experiment is active")
        return self.state

    def begin_finalization(self) -> ExperimentState:
        state = self.require_state()
        state.status = ExperimentStatus.FINALIZING
        self.repo.update_status(state.experiment_id, ExperimentStatus.FINALIZING)
        log.info("EXPERIMENT", "Day 14 reached — freezing the dataset and finalising")
        self.system.event(
            "experiment_finalizing", "day 14 reached", experiment_id=state.experiment_id
        )
        return state

    def complete(self) -> ExperimentState:
        state = self.require_state()
        state.status = ExperimentStatus.COMPLETE
        self.repo.update_status(
            state.experiment_id, ExperimentStatus.COMPLETE, actual_end_ts_utc=iso(now_utc())
        )
        self.system.event(
            "experiment_complete", "research complete", experiment_id=state.experiment_id
        )
        return state

    def abort(self, reason: str) -> None:
        if self.state is None:
            return
        self.repo.update_status(
            self.state.experiment_id, ExperimentStatus.ABORTED, actual_end_ts_utc=iso(now_utc())
        )
        self.system.event(
            "experiment_aborted", reason, level="ERROR", experiment_id=self.state.experiment_id
        )
        log.warning("EXPERIMENT", f"Experiment aborted: {reason}")

    def apply_outage_policy(self, total_outage_seconds: int) -> bool:
        """Extend the window only when the policy explicitly allows it.

        Default is ``none``: 14 real calendar days regardless of outages. The
        window is never silently extended.
        """
        state = self.require_state()
        if state.outage_policy != "extend_by_outage":
            return False
        if total_outage_seconds <= 0:
            return False

        new_end = state.scheduled_end + timedelta(seconds=total_outage_seconds)
        self.repo.extend_end(state.experiment_id, iso(new_end))
        state.scheduled_end = new_end
        log.info(
            "EXPERIMENT",
            f"Outage-adjustment policy applied: end extended by "
            f"{humanize_duration(total_outage_seconds)} to {iso(new_end)}",
        )
        self.system.event(
            "outage_adjustment",
            f"extended by {total_outage_seconds}s per configured policy",
            experiment_id=state.experiment_id,
        )
        return True

    def start_banner(self, *, strategy_count: int, actual_equity: float) -> list[str]:
        """The mandated research-start block."""
        state = self.require_state()
        return [
            f"{state.name.replace('_', ' ')} — {state.duration_days}-DAY RESEARCH",
            "",
            "Environment:               DEMO",
            f"Starting Demo Equity:      ${actual_equity:,.2f}",
            f"Expected Research Capital: ${state.expected_demo_equity:,.2f}",
            f"Shadow Equity Per Strategy: ${state.shadow_equity_per_strategy:,.2f}",
            f"Strategies:                {strategy_count}",
            f"Primary Market:            {state.primary_symbol}",
            f"Duration:                  {state.duration_days} days",
            "Real Money:                DISABLED",
            f"Scheduled End:             {iso(state.scheduled_end)}",
            f"Experiment ID:             {state.experiment_id}",
        ]
