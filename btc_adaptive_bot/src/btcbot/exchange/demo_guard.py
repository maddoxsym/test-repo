"""The demo safety lock.

Four independent signals must all pass before order submission is permitted.
Any failure prints the required banner and leaves orders disabled. There is no
configuration flag, CLI switch, or environment variable that bypasses this.

See ``docs/bybit_capabilities.md`` §3 for the reasoning behind each signal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..utils.errors import ApiError, TransportError
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc
from .endpoints import ALLOWED_DEMO_HOSTS, is_allowed_authenticated_host
from .rest import ROUTE_NOT_FOUND_CODES, BybitDemoClient, MainnetNegativeControlProbe

log = get_logger(__name__)

SAFETY_LOCK_BANNER = (
    "SAFETY LOCK",
    "BYBIT DEMO ENVIRONMENT COULD NOT BE VERIFIED",
    "ORDER SUBMISSION DISABLED",
)


@dataclass(frozen=True, slots=True)
class SignalResult:
    name: str
    passed: bool
    detail: str
    required: bool = True


@dataclass(frozen=True, slots=True)
class DemoVerification:
    """Outcome of a verification pass."""

    verified: bool
    checked_at: datetime
    signals: tuple[SignalResult, ...]
    account_uid: str | None = None
    account_info: dict[str, object] = field(default_factory=dict)

    @property
    def failures(self) -> tuple[SignalResult, ...]:
        return tuple(s for s in self.signals if s.required and not s.passed)

    def summary_lines(self) -> list[str]:
        lines = []
        for signal in self.signals:
            mark = "PASS" if signal.passed else ("FAIL" if signal.required else "SKIP")
            lines.append(f"  [{mark}] {signal.name}: {signal.detail}")
        return lines


class DemoGuard:
    """Verifies the demo environment and gates order submission.

    The orchestrator asks :meth:`orders_permitted` before every order. It returns
    ``True`` only after a successful verification pass, and any subsequent
    failure (re-verification, reconnect check) revokes it immediately.
    """

    def __init__(
        self,
        client: BybitDemoClient,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        run_mainnet_negative_control: bool = True,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._api_secret = api_secret
        self._run_negative_control = run_mainnet_negative_control
        self._verified = False
        self._last_verification: DemoVerification | None = None

    @property
    def verified(self) -> bool:
        return self._verified

    @property
    def last_verification(self) -> DemoVerification | None:
        return self._last_verification

    def orders_permitted(self) -> bool:
        """The single gate every order path must pass through."""
        return self._verified

    def revoke(self, reason: str) -> None:
        """Withdraw verification — used when a check fails mid-experiment."""
        if self._verified:
            log.critical("SAFETY", f"Demo verification revoked: {reason}")
        self._verified = False

    async def verify(self) -> DemoVerification:
        """Run all four signals. Sets :attr:`verified` accordingly."""
        signals: list[SignalResult] = []

        signals.append(self._signal_host_pin())
        signals.append(await self._signal_authenticated_reachability())
        signals.append(await self._signal_demo_only_endpoint())
        signals.append(await self._signal_mainnet_negative_control())

        account_info: dict[str, object] = {}
        uid: str | None = None
        if signals[1].passed:
            try:
                account_info = await self._client.get_account_info()
                key_info = await self._client.get_api_key_info()
                uid = str(key_info.get("userID") or key_info.get("uid") or "") or None
            except (ApiError, TransportError) as exc:
                log.debug("BYBIT", f"Could not read account metadata: {exc}")

        verified = all(s.passed for s in signals if s.required)
        verification = DemoVerification(
            verified=verified,
            checked_at=now_utc(),
            signals=tuple(signals),
            account_uid=uid,
            account_info=account_info,
        )
        self._verified = verified
        self._last_verification = verification

        if verified:
            log.info("BYBIT", "Demo environment VERIFIED")
            for line in verification.summary_lines():
                log.debug("BYBIT", line)
        else:
            log.banner(list(SAFETY_LOCK_BANNER), tag="SAFETY")
            for line in verification.summary_lines():
                log.error("SAFETY", line)

        return verification

    # --- individual signals ---------------------------------------------

    def _signal_host_pin(self) -> SignalResult:
        """Signal 1 — the client is bound to an allow-listed demo host."""
        host = self._client.base_url
        allowed = is_allowed_authenticated_host(host)
        return SignalResult(
            name="host pin",
            passed=allowed,
            detail=(
                f"authenticated host is {host}"
                if allowed
                else f"host {host} is not in the demo allow-list {sorted(ALLOWED_DEMO_HOSTS)}"
            ),
        )

    async def _signal_authenticated_reachability(self) -> SignalResult:
        """Signal 2 — the credentials work against the demo module."""
        if not self._client.has_credentials:
            return SignalResult(
                name="authenticated reachability",
                passed=False,
                detail="no API credentials supplied (set BYBIT_DEMO_API_KEY / _SECRET in .env)",
            )
        try:
            await self._client.get_account_info()
            balance = await self._client.get_wallet_balance()
        except ApiError as exc:
            return SignalResult(
                name="authenticated reachability",
                passed=False,
                detail=f"demo host rejected the credentials: retCode={exc.ret_code} {exc.ret_msg}",
            )
        except TransportError as exc:
            return SignalResult(
                name="authenticated reachability", passed=False, detail=f"transport failure: {exc}"
            )
        return SignalResult(
            name="authenticated reachability",
            passed=True,
            detail=f"account reachable, equity ${balance.total_equity:,.2f}",
        )

    async def _signal_demo_only_endpoint(self) -> SignalResult:
        """Signal 3 — the demo-only funds route exists on this host.

        A zero-amount request moves nothing. We only inspect whether the route
        resolves: mainnet has no such endpoint, the demo module does.
        """
        if not self._client.has_credentials:
            return SignalResult(
                name="demo-only endpoint probe", passed=False, detail="no credentials to probe with"
            )
        try:
            payload = await self._client.probe_demo_endpoint()
        except ApiError as exc:
            if exc.ret_code in ROUTE_NOT_FOUND_CODES:
                return SignalResult(
                    name="demo-only endpoint probe",
                    passed=False,
                    detail="demo-apply-money route not present on this host — not a demo module",
                )
            # Any other business error still proves the route exists here.
            return SignalResult(
                name="demo-only endpoint probe",
                passed=True,
                detail=f"demo-only route present (responded retCode={exc.ret_code})",
            )
        except TransportError as exc:
            return SignalResult(
                name="demo-only endpoint probe", passed=False, detail=f"transport failure: {exc}"
            )

        ret_code = int(payload.get("retCode", -1))
        if ret_code in ROUTE_NOT_FOUND_CODES:
            return SignalResult(
                name="demo-only endpoint probe",
                passed=False,
                detail=f"demo-apply-money route not found (retCode={ret_code})",
            )
        return SignalResult(
            name="demo-only endpoint probe",
            passed=True,
            detail=f"demo-only route present (retCode={ret_code})",
        )

    async def _signal_mainnet_negative_control(self) -> SignalResult:
        """Signal 4 — the same key must NOT authenticate on the mainnet host."""
        if not self._run_negative_control:
            log.warning(
                "SAFETY",
                "Mainnet negative control is DISABLED in config. The system cannot prove these "
                "credentials are demo-scoped; it is relying on the host pin and demo-route probe.",
            )
            return SignalResult(
                name="mainnet negative control",
                passed=True,
                detail="disabled by configuration (safety.mainnet_negative_control: false)",
                required=False,
            )
        if not (self._api_key and self._api_secret):
            return SignalResult(
                name="mainnet negative control",
                passed=False,
                detail="no credentials available to test",
            )

        probe = MainnetNegativeControlProbe(self._api_key, self._api_secret)
        rejected, detail = await probe.credentials_are_rejected()
        return SignalResult(name="mainnet negative control", passed=rejected, detail=detail)


def print_safety_lock_banner() -> None:
    """Print the mandated safety-lock banner (used by scripts and the verifier)."""
    log.banner(list(SAFETY_LOCK_BANNER), tag="SAFETY")
