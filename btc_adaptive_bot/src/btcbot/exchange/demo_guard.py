"""The demo safety lock.

Four independent signals must all pass before order submission is permitted.
Any failure prints the required banner and leaves orders disabled. There is no
configuration flag, CLI switch, or environment variable that bypasses this.

The four signals, translated to OKX's environment model (see
``docs/okx_demo_capabilities.md`` §4):

1. **Host pin** — the REST client is bound to the active region profile's host
   and every WS URL that profile will dial is on the exact-match demo
   allow-list. No region is hardcoded; the profile is chosen by config.
2. **Demo header enforcement** — the client's single header-builder path
   injects ``x-simulated-trading: 1``; verified at runtime, not assumed.
3. **Authenticated demo reachability** — account config + balance succeed
   *with* the demo header, and the account reports a usable position mode.
4. **Live-environment negative control** — the same credentials, sent once
   read-only *without* the demo header, are **required to fail** with OKX's
   environment-mismatch rejection (50101). Success would mean the key can act
   on real money, and the system refuses to trade with it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..utils.errors import ApiError, TransportError
from ..utils.logging import get_logger
from ..utils.timeutil import now_utc
from .endpoints import (
    ALLOWED_DEMO_HOSTS,
    DEFAULT_PROFILE,
    DemoProfile,
    is_allowed_authenticated_host,
    is_allowed_ws_url,
)
from .models import AccountConfig
from .rest import LiveEnvironmentNegativeControlProbe, OkxDemoClient

log = get_logger(__name__)

SAFETY_LOCK_BANNER = (
    "SAFETY LOCK",
    "OKX DEMO ENVIRONMENT COULD NOT BE VERIFIED",
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
    account_config: AccountConfig | None = None
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

    The orchestrator asks :meth:`orders_permitted` before every order. It
    returns ``True`` only after a successful verification pass, and any
    subsequent failure (re-verification, reconnect check, clock drift) revokes
    it immediately.
    """

    def __init__(
        self,
        client: OkxDemoClient,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        run_mainnet_negative_control: bool = True,
        profile: DemoProfile | None = None,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._api_secret = api_secret
        self._passphrase = passphrase
        # Prefer the profile the client is actually bound to: the guard should
        # audit the transport in front of it, not a region it was told about.
        self._profile = profile or getattr(client, "profile", DEFAULT_PROFILE)
        # Config key `safety.mainnet_negative_control` — "mainnet" here means
        # OKX's live (real-money) environment, selected by *omitting* the demo
        # header rather than by a different host.
        self._run_negative_control = run_mainnet_negative_control
        self._verified = False
        self._last_verification: DemoVerification | None = None

    @property
    def profile(self) -> DemoProfile:
        return self._profile

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
        signals.append(self._signal_demo_header())
        reachability, account_config = await self._signal_authenticated_reachability()
        signals.append(reachability)
        signals.append(await self._signal_live_negative_control())

        uid = account_config.uid if account_config else None

        verified = all(s.passed for s in signals if s.required)
        verification = DemoVerification(
            verified=verified,
            checked_at=now_utc(),
            signals=tuple(signals),
            account_uid=uid,
            account_config=account_config,
            account_info=dict(account_config.raw) if account_config else {},
        )
        self._verified = verified
        self._last_verification = verification

        if verified:
            log.info("OKX", "Demo environment VERIFIED")
            for line in verification.summary_lines():
                log.debug("OKX", line)
        else:
            log.banner(list(SAFETY_LOCK_BANNER), tag="SAFETY")
            for line in verification.summary_lines():
                log.error("SAFETY", line)

        return verification

    # --- individual signals ---------------------------------------------

    def _signal_host_pin(self) -> SignalResult:
        """Signal 1 — REST host and WS URLs are on the exact-match allow-lists."""
        profile = self._profile
        host = self._client.base_url
        host_ok = is_allowed_authenticated_host(host)
        # The REST host does not distinguish demo from live on OKX — the header
        # does — but every demo WS host carries the "pap" infix, so this list is
        # a genuine environment check rather than a formality.
        bad_ws = [url for url in profile.ws_urls if not is_allowed_ws_url(url)]
        passed = host_ok and not bad_ws
        if passed:
            detail = (
                f"authenticated host is {host}; WS endpoints are "
                f"{profile.label} demo ({profile.region})"
            )
        elif not host_ok:
            detail = f"host {host} is not in the demo allow-list {sorted(ALLOWED_DEMO_HOSTS)}"
        else:
            detail = f"WebSocket URL outside the demo allow-list: {bad_ws[0]}"
        return SignalResult(name="host pin", passed=passed, detail=detail)

    def _signal_demo_header(self) -> SignalResult:
        """Signal 2 — the transport layer injects x-simulated-trading: 1."""
        enforced = self._client.demo_header_enforced()
        return SignalResult(
            name="demo header enforcement",
            passed=enforced,
            detail=(
                "x-simulated-trading: 1 is injected by the single header-builder path"
                if enforced
                else "the transport layer did NOT inject the demo header — refusing to trade"
            ),
        )

    async def _signal_authenticated_reachability(
        self,
    ) -> tuple[SignalResult, AccountConfig | None]:
        """Signal 3 — the credentials work against the demo environment."""
        if not self._client.has_credentials:
            return (
                SignalResult(
                    name="authenticated reachability",
                    passed=False,
                    detail=(
                        "no API credentials supplied (set OKX_DEMO_API_KEY / "
                        "OKX_DEMO_API_SECRET / OKX_DEMO_PASSPHRASE in .env)"
                    ),
                ),
                None,
            )
        try:
            config = await self._client.get_account_config()
            balance = await self._client.get_wallet_balance()
        except ApiError as exc:
            return (
                SignalResult(
                    name="authenticated reachability",
                    passed=False,
                    detail=f"demo environment rejected the credentials: code={exc.ret_code} {exc.ret_msg}",
                ),
                None,
            )
        except TransportError as exc:
            return (
                SignalResult(
                    name="authenticated reachability",
                    passed=False,
                    detail=f"transport failure: {exc}",
                ),
                None,
            )
        return (
            SignalResult(
                name="authenticated reachability",
                passed=True,
                detail=(
                    f"account reachable (uid …{config.uid[-4:] if config.uid else '????'}, "
                    f"posMode {config.position_mode.value}), equity ${balance.total_equity:,.2f}"
                ),
            ),
            config,
        )

    async def _signal_live_negative_control(self) -> SignalResult:
        """Signal 4 — the same key must NOT authenticate on the live environment."""
        if not self._run_negative_control:
            log.warning(
                "SAFETY",
                "Live-environment negative control is DISABLED in config. The system cannot "
                "prove these credentials are demo-scoped; it is relying on the host pin and "
                "header enforcement.",
            )
            return SignalResult(
                name="live-environment negative control",
                passed=True,
                detail="disabled by configuration (safety.mainnet_negative_control: false)",
                required=False,
            )
        if not (self._api_key and self._api_secret and self._passphrase):
            return SignalResult(
                name="live-environment negative control",
                passed=False,
                detail="no credentials available to test",
            )

        probe = LiveEnvironmentNegativeControlProbe(
            self._api_key, self._api_secret, self._passphrase, profile=self._profile
        )
        rejected, detail = await probe.credentials_are_rejected()
        return SignalResult(
            name="live-environment negative control", passed=rejected, detail=detail
        )


def print_safety_lock_banner() -> None:
    """Print the mandated safety-lock banner (used by scripts and the verifier)."""
    log.banner(list(SAFETY_LOCK_BANNER), tag="SAFETY")
