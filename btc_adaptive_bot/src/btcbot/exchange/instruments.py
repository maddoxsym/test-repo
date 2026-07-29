"""Runtime instrument and capability discovery — the "BTCUSD UM X-Perp" finder.

"BTCUSD UM X-Perp" is a *display name* in the OKX UI; the API-level
``instId`` behind it is **never hardcoded** (and was deliberately not assumed
during research — see ``docs/okx_demo_capabilities.md`` §5). On startup — and
on a timer, because size limits can change — the system asks the exchange
which SWAP instruments exist and selects the BTC USD-margined linear
perpetual by its discovered properties:

1. ``GET /api/v5/public/instruments?instType=SWAP``
2. keep instruments whose underlying references the configured base currency
   (BTC), with ``ctType == "linear"`` and ``state == "live"``
3. rank by the configured settle-currency preference (USDT, then USDC, …)
4. exactly one winner is selected and journaled with the full contract spec;
   ambiguity is journaled with the alternatives listed; **zero matches fails
   loudly** with the instrument list logged — there is no guessed fallback.

The result is an :class:`ExchangeCapabilities` snapshot that the execution
layer consults before every order.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..utils.errors import ApiError, InstrumentNotFoundError, TransportError
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc
from .models import Capability, InstrumentSpec, InstType
from .rest import OkxDemoClient

log = get_logger(__name__)


@dataclass(slots=True)
class ExchangeCapabilities:
    """What the connected demo environment actually supports, right now."""

    base_ccy: str
    instrument: InstrumentSpec | None = None
    alternatives: list[InstrumentSpec] = field(default_factory=list)
    discovered_at: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def primary(self) -> InstrumentSpec:
        if self.instrument is None:
            raise InstrumentNotFoundError(
                f"no tradable {self.base_ccy} linear perpetual discovered; "
                "check market.base_currency and market.settle_currency_preference"
            )
        return self.instrument

    @property
    def inst_id(self) -> str:
        return self.primary.inst_id

    @property
    def supports_short_on_exchange(self) -> bool:
        """Perpetual swaps short natively; still asked, never assumed."""
        try:
            return self.primary.supports(Capability.SHORT)
        except InstrumentNotFoundError:
            return False

    def describe(self) -> list[str]:
        lines = [f"Base currency: {self.base_ccy}"]
        if self.instrument is not None:
            spec = self.instrument
            lines.append(
                f"  selected {spec.inst_id} ({spec.ct_type} {spec.inst_type.value}, "
                f"settles {spec.settle_ccy}) state={spec.state}"
            )
            lines.append(
                f"  contract: ctVal={spec.ct_val} {spec.ct_val_ccy} × mult {spec.ct_mult}, "
                f"lot={spec.lot_size}, min={spec.min_size}, tick={spec.tick_size}, "
                f"maxLever={spec.max_leverage}"
            )
        for alt in self.alternatives:
            lines.append(f"  alternative: {alt.inst_id} (settles {alt.settle_ccy})")
        lines.extend(f"  note: {note}" for note in self.notes)
        return lines


class CapabilityDiscovery:
    """Discovers the X-Perp instrument and refreshes it on a timer."""

    def __init__(
        self,
        client: OkxDemoClient,
        *,
        base_ccy: str,
        settle_preference: list[str],
    ) -> None:
        self._client = client
        self._base_ccy = base_ccy.upper()
        self._settle_preference = [s.upper() for s in settle_preference]
        self._capabilities: ExchangeCapabilities | None = None

    @property
    def capabilities(self) -> ExchangeCapabilities:
        if self._capabilities is None:
            raise InstrumentNotFoundError("capability discovery has not run yet")
        return self._capabilities

    async def discover(self) -> ExchangeCapabilities:
        """Query the SWAP instrument list and select the X-Perp."""
        notes: list[str] = []
        try:
            specs = await self._client.get_instruments(InstType.SWAP)
        except (ApiError, TransportError) as exc:
            if self._capabilities is not None:
                raise
            raise InstrumentNotFoundError(f"instrument discovery failed: {exc}") from exc

        matches = [
            spec
            for spec in specs
            if spec.base_ccy.upper() == self._base_ccy
            and spec.ct_type == "linear"
            and spec.is_tradable
        ]

        if not matches:
            available = sorted(s.inst_id for s in specs)[:40]
            raise InstrumentNotFoundError(
                f"no live linear {self._base_ccy} perpetual found among {len(specs)} SWAP "
                f"instruments. This system never falls back to a guessed instId. "
                f"Instruments returned (first 40): {available}"
            )

        def rank(spec: InstrumentSpec) -> int:
            settle = spec.settle_ccy.upper()
            try:
                return self._settle_preference.index(settle)
            except ValueError:
                return len(self._settle_preference)

        matches.sort(key=lambda s: (rank(s), s.inst_id))
        selected = matches[0]
        alternatives = matches[1:]

        if selected.settle_ccy.upper() not in self._settle_preference:
            notes.append(
                f"selected settle currency {selected.settle_ccy} is outside the configured "
                f"preference list {self._settle_preference} — it was the only live match"
            )
        if alternatives:
            notes.append(
                f"{len(alternatives)} alternative linear {self._base_ccy} perpetual(s) exist; "
                "selection followed market.settle_currency_preference"
            )

        capabilities = ExchangeCapabilities(
            base_ccy=self._base_ccy,
            instrument=selected,
            alternatives=alternatives,
            discovered_at=now_utc().isoformat(),
            notes=notes,
        )
        self._capabilities = capabilities

        log.info(
            "OKX",
            f"Instrument discovery complete: {selected.inst_id} "
            f"(ctVal {selected.ct_val} {selected.ct_val_ccy}, lot {selected.lot_size}, "
            f"tick {selected.tick_size}, maxLever {selected.max_leverage})",
            alternatives=[a.inst_id for a in alternatives],
        )
        for note in notes:
            log.info("OKX", f"Discovery note: {note}")

        return capabilities

    async def refresh(self) -> ExchangeCapabilities:
        """Re-run discovery; keeps the previous snapshot if the refresh fails.

        Size limits can change over a 14-day run, so a stale snapshot is a real
        risk — but a transient discovery failure must not stop trading either.
        """
        try:
            return await self.discover()
        except (InstrumentNotFoundError, ApiError, TransportError) as exc:
            if self._capabilities is not None:
                log.warning("OKX", f"Instrument refresh failed, keeping previous snapshot: {exc}")
                return self._capabilities
            raise
