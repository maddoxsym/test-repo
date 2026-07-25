"""Strategy registry — discovery, configuration, and instantiation.

Central place where the 38 strategies are assembled. It enforces uniqueness of
IDs, applies per-strategy enable/disable and parameter overrides from
configuration, and wires member strategies into the ensembles.
"""

from __future__ import annotations

from typing import Any

from ..config.schema import StrategiesConfig
from ..utils.errors import ConfigError
from ..utils.logging import get_logger
from .base import Strategy, StrategyCategory
from .breakout import BREAKOUT_STRATEGIES
from .ensemble import ENSEMBLE_STRATEGIES, EnsembleBase
from .mean_reversion import MEAN_REVERSION_STRATEGIES
from .momentum import MOMENTUM_STRATEGIES
from .structure import STRUCTURE_STRATEGIES
from .trend import TREND_STRATEGIES
from .volume import VOLUME_STRATEGIES

log = get_logger(__name__)

ALL_STRATEGY_CLASSES: tuple[type[Strategy], ...] = (
    *TREND_STRATEGIES,
    *BREAKOUT_STRATEGIES,
    *MOMENTUM_STRATEGIES,
    *MEAN_REVERSION_STRATEGIES,
    *STRUCTURE_STRATEGIES,
    *VOLUME_STRATEGIES,
    *ENSEMBLE_STRATEGIES,
)


def _validate_unique_ids() -> None:
    """Fail loudly on duplicate strategy IDs.

    IDs are primary keys in the database and in every metric breakdown, so a
    collision would silently merge two strategies' results.
    """
    seen: dict[str, str] = {}
    for cls in ALL_STRATEGY_CLASSES:
        if cls.id in seen:
            raise ConfigError(
                f"duplicate strategy id {cls.id!r} used by both {seen[cls.id]} and {cls.__name__}"
            )
        seen[cls.id] = cls.__name__


_validate_unique_ids()


class StrategyRegistry:
    """Builds and holds the active strategy set."""

    def __init__(self, strategies: list[Strategy]) -> None:
        self._strategies = {s.id: s for s in strategies}

    @classmethod
    def build(
        cls,
        config: StrategiesConfig,
        *,
        available_timeframes: list[str] | None = None,
    ) -> StrategyRegistry:
        """Instantiate the configured strategies.

        ``available_timeframes`` filters out strategies whose primary or context
        timeframe is not being collected — running them would guarantee silence
        and pollute the ranking with empty records.
        """
        enabled_ids = set(config.enabled)
        disabled_ids = set(config.disabled)
        known_ids = {c.id for c in ALL_STRATEGY_CLASSES}

        unknown = (enabled_ids | disabled_ids) - known_ids
        if unknown:
            raise ConfigError(
                f"configuration references unknown strategy id(s) {sorted(unknown)}. "
                f"Available: {sorted(known_ids)}"
            )

        instances: list[Strategy] = []
        skipped: list[str] = []

        for strategy_cls in ALL_STRATEGY_CLASSES:
            if enabled_ids and strategy_cls.id not in enabled_ids:
                continue
            if strategy_cls.id in disabled_ids:
                continue

            if available_timeframes is not None:
                required = {strategy_cls.primary_timeframe, *strategy_cls.context_timeframes}
                missing = required - set(available_timeframes)
                if missing:
                    skipped.append(f"{strategy_cls.id} (needs timeframes {sorted(missing)})")
                    continue

            overrides = config.overrides.get(strategy_cls.id, {})
            try:
                instances.append(strategy_cls(**overrides))
            except ValueError as exc:
                raise ConfigError(
                    f"invalid parameter override for strategy {strategy_cls.id!r}: {exc}"
                ) from exc

        if not instances:
            raise ConfigError(
                "no strategies are enabled — check strategies.enabled / strategies.disabled"
            )

        # Ensembles need their members after everything else is constructed.
        members = [s for s in instances if not isinstance(s, EnsembleBase)]
        for strategy in instances:
            if isinstance(strategy, EnsembleBase):
                strategy.set_members(members)

        for note in skipped:
            log.info("STRATEGY", f"Skipped {note}")
        log.info(
            "STRATEGY",
            f"Registry built with {len(instances)} strategies "
            f"({len(members)} base + {len(instances) - len(members)} ensemble)",
        )
        return cls(instances)

    # --- access -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._strategies)

    def __iter__(self):
        return iter(self._strategies.values())

    def __contains__(self, strategy_id: object) -> bool:
        return strategy_id in self._strategies

    @property
    def ids(self) -> list[str]:
        return list(self._strategies)

    def all(self) -> list[Strategy]:
        return list(self._strategies.values())

    def get(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    def require(self, strategy_id: str) -> Strategy:
        strategy = self._strategies.get(strategy_id)
        if strategy is None:
            raise KeyError(f"strategy {strategy_id!r} is not in the active registry")
        return strategy

    def by_category(self, category: StrategyCategory) -> list[Strategy]:
        return [s for s in self._strategies.values() if s.category is category]

    def by_timeframe(self, timeframe: str) -> list[Strategy]:
        return [s for s in self._strategies.values() if s.primary_timeframe == timeframe]

    def ensembles(self) -> list[EnsembleBase]:
        return [s for s in self._strategies.values() if isinstance(s, EnsembleBase)]

    def required_timeframes(self) -> set[str]:
        timeframes: set[str] = set()
        for strategy in self._strategies.values():
            timeframes.add(strategy.primary_timeframe)
            timeframes.update(strategy.context_timeframes)
        return timeframes

    def version_map(self) -> dict[str, str]:
        return {s.id: s.version for s in self._strategies.values()}

    def descriptions(self) -> list[dict[str, Any]]:
        return [s.describe() for s in self._strategies.values()]

    def supports_short(self, strategy_id: str) -> bool:
        strategy = self._strategies.get(strategy_id)
        return bool(strategy and strategy.supports_short)

    def update_ensemble_evidence(
        self, weights: dict[str, float], regime_scores: dict[str, dict[str, float]]
    ) -> None:
        """Push measured performance into the ensembles."""
        for ensemble in self.ensembles():
            ensemble.update_weights(weights)
            ensemble.update_regime_scores(regime_scores)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for strategy in self._strategies.values():
            counts[strategy.category.value] = counts.get(strategy.category.value, 0) + 1
        return counts
