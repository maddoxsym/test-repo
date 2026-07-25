"""Runtime instrument and capability discovery.

Nothing about the connected environment is assumed. On startup — and on a timer,
because Bybit documents that several size limits are adjusted bi-monthly — the
system asks the exchange what exists, what is tradable, and under what rules.

The result is an :class:`ExchangeCapabilities` snapshot that the execution layer
consults before every order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils.errors import ApiError, InstrumentNotFoundError, TransportError
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc
from .models import Capability, Category, InstrumentSpec
from .rest import BybitDemoClient

log = get_logger(__name__)


@dataclass(slots=True)
class ExchangeCapabilities:
    """What the connected demo environment actually supports, right now."""

    symbol: str
    instruments: dict[Category, InstrumentSpec] = field(default_factory=dict)
    tradable_categories: tuple[Category, ...] = ()
    primary_category: Category | None = None
    discovered_at: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def primary(self) -> InstrumentSpec:
        if self.primary_category is None or self.primary_category not in self.instruments:
            raise InstrumentNotFoundError(
                f"no tradable instrument discovered for {self.symbol}; "
                "check market.enabled_categories and the symbol name"
            )
        return self.instruments[self.primary_category]

    @property
    def supports_short_on_exchange(self) -> bool:
        """Whether a real short order can be sent in the primary category.

        Spot cannot short. When this is False, SHORT signals are still researched
        in the shadow engine but never routed to a real demo order.
        """
        try:
            return self.primary.supports(Capability.SHORT)
        except InstrumentNotFoundError:
            return False

    def describe(self) -> list[str]:
        lines = [f"Symbol: {self.symbol}"]
        for category, spec in self.instruments.items():
            lines.append(
                f"  {category.value:<8} status={spec.status} "
                f"tick={spec.tick_size} step={spec.qty_step} "
                f"minQty={spec.min_order_qty} minAmt={spec.min_order_amt} "
                f"caps={sorted(c.value for c in spec.capabilities)}"
            )
        lines.extend(f"  note: {note}" for note in self.notes)
        return lines


class CapabilityDiscovery:
    """Discovers instruments and refreshes them on a timer."""

    def __init__(
        self,
        client: BybitDemoClient,
        *,
        symbol: str,
        enabled_categories: list[str],
        preferred_category: str,
    ) -> None:
        self._client = client
        self._symbol = symbol
        self._enabled = [Category(c) for c in enabled_categories]
        self._preferred = Category(preferred_category)
        self._capabilities: ExchangeCapabilities | None = None

    @property
    def capabilities(self) -> ExchangeCapabilities:
        if self._capabilities is None:
            raise InstrumentNotFoundError("capability discovery has not run yet")
        return self._capabilities

    async def discover(self) -> ExchangeCapabilities:
        """Query every enabled category and build a capability snapshot."""
        instruments: dict[Category, InstrumentSpec] = {}
        notes: list[str] = []

        for category in self._enabled:
            try:
                specs = await self._client.get_instruments(category, self._symbol)
            except ApiError as exc:
                notes.append(f"{category.value}: unavailable on this account ({exc.ret_msg})")
                continue
            except TransportError as exc:
                notes.append(f"{category.value}: discovery failed ({exc})")
                continue

            match = next((s for s in specs if s.symbol == self._symbol), None)
            if match is None:
                notes.append(f"{category.value}: {self._symbol} not listed")
                continue
            if not match.is_tradable:
                notes.append(f"{category.value}: {self._symbol} status={match.status}, not tradable")
                continue
            instruments[category] = match

        tradable = tuple(instruments)
        if not tradable:
            raise InstrumentNotFoundError(
                f"{self._symbol} is not tradable in any enabled category "
                f"{[c.value for c in self._enabled]}. Notes: {notes}"
            )

        primary = self._preferred if self._preferred in instruments else tradable[0]
        if primary is not self._preferred:
            notes.append(
                f"preferred category {self._preferred.value} unavailable; using {primary.value}"
            )

        if not instruments[primary].supports(Capability.SHORT):
            notes.append(
                f"{primary.value} has no short side — SHORT signals will be researched in the "
                "shadow engine only and never sent to the exchange"
            )

        capabilities = ExchangeCapabilities(
            symbol=self._symbol,
            instruments=instruments,
            tradable_categories=tradable,
            primary_category=primary,
            discovered_at=now_utc().isoformat(),
            notes=notes,
        )
        self._capabilities = capabilities

        log.info(
            "BYBIT",
            f"Instrument discovery complete: {self._symbol} on {primary.value} "
            f"(tick {instruments[primary].tick_size}, step {instruments[primary].qty_step})",
            categories=[c.value for c in tradable],
        )
        for note in notes:
            log.info("BYBIT", f"Discovery note: {note}")

        return capabilities

    async def refresh(self) -> ExchangeCapabilities:
        """Re-run discovery; keeps the previous snapshot if the refresh fails.

        Bybit warns that ``maxLimitOrderQty``/``maxMarketOrderQty`` change on a
        schedule, so a stale snapshot is a real risk over a 14-day run.
        """
        try:
            return await self.discover()
        except (InstrumentNotFoundError, ApiError, TransportError) as exc:
            if self._capabilities is not None:
                log.warning("BYBIT", f"Instrument refresh failed, keeping previous snapshot: {exc}")
                return self._capabilities
            raise
