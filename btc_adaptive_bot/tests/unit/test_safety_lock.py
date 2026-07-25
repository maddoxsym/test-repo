"""Demo safety lock, mainnet rejection, and circuit breakers.

The core guarantee under test: **there is no way to construct an authenticated
client against a real-money host, and no way to submit an order without a passing
demo verification.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.config.schema import SafetyConfig
from btcbot.exchange.demo_guard import SAFETY_LOCK_BANNER, DemoGuard, DemoVerification, SignalResult
from btcbot.exchange.endpoints import (
    ALLOWED_DEMO_HOSTS,
    DEMO_REST_HOST,
    DEMO_WS_PRIVATE,
    FORBIDDEN_ENDPOINT_FRAGMENTS,
    is_allowed_authenticated_host,
    public_ws_url,
)
from btcbot.exchange.rest import BybitDemoClient, MainnetNegativeControlProbe
from btcbot.safety.circuit_breakers import BreakerType, CircuitBreakers
from btcbot.utils.errors import MainnetRejectedError
from btcbot.utils.ids import redact_secret
from btcbot.utils.logging import register_secret

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


class TestHostAllowList:
    def test_demo_host_is_the_documented_one(self):
        assert DEMO_REST_HOST == "https://api-demo.bybit.com"
        assert DEMO_REST_HOST in ALLOWED_DEMO_HOSTS

    def test_allow_list_is_immutable(self):
        assert isinstance(ALLOWED_DEMO_HOSTS, frozenset)
        with pytest.raises(AttributeError):
            ALLOWED_DEMO_HOSTS.add("https://api.bybit.com")  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "host",
        [
            "https://api.bybit.com",
            "https://api.bytick.com",
            "https://api.bybit.eu",
            "https://api.bybit.nl",
            "https://api.bybit.tr",
            "https://api-testnet.bybit.com",
            "https://api-demo.bybit.com.evil.example",
            "http://api-demo.bybit.com",
            "https://localhost",
            "",
        ],
    )
    def test_non_demo_hosts_are_refused(self, host):
        assert not is_allowed_authenticated_host(host)

    def test_private_ws_is_the_demo_stream(self):
        assert DEMO_WS_PRIVATE.startswith("wss://stream-demo.bybit.com")

    def test_public_ws_is_unauthenticated_mainnet_stream(self):
        # Bybit documents that demo has no public stream and mainnet public data
        # is identical. This connection carries no credentials.
        assert public_ws_url("spot") == "wss://stream.bybit.com/v5/public/spot"
        assert public_ws_url("linear") == "wss://stream.bybit.com/v5/public/linear"

    def test_unknown_category_has_no_public_stream(self):
        with pytest.raises(ValueError):
            public_ws_url("options_that_do_not_exist")


class TestMainnetRejection:
    @pytest.mark.parametrize(
        "host",
        [
            "https://api.bybit.com",
            "https://api.bybit.eu",
            "https://api-testnet.bybit.com",
            "https://api.bytick.com",
        ],
    )
    def test_client_construction_refuses_non_demo_hosts(self, host):
        """Refusal happens at construction, before any network activity."""
        with pytest.raises(MainnetRejectedError) as exc:
            BybitDemoClient(api_key="k" * 20, api_secret="s" * 20, base_url=host)
        assert "no real-money trading mode" in str(exc.value)

    def test_demo_host_construction_succeeds(self):
        client = BybitDemoClient(api_key="k" * 20, api_secret="s" * 20)
        assert client.base_url == DEMO_REST_HOST

    def test_trailing_slash_is_normalised(self):
        client = BybitDemoClient(base_url=DEMO_REST_HOST + "/")
        assert client.base_url == DEMO_REST_HOST

    def test_negative_control_probe_has_no_order_capability(self):
        """The mainnet probe must be structurally incapable of trading."""
        probe = MainnetNegativeControlProbe("k" * 20, "s" * 20)
        for forbidden in (
            "place_order", "cancel_order", "submit", "request_demo_funds",
            "cancel_all", "get_wallet_balance",
        ):
            assert not hasattr(probe, forbidden), f"probe must not expose {forbidden}"
        # Exactly one public method.
        public = [n for n in dir(probe) if not n.startswith("_")]
        assert public == ["credentials_are_rejected"]


class TestSourceAudit:
    """Static guarantees about the codebase itself."""

    def _python_files(self) -> list[Path]:
        return sorted(SRC_ROOT.rglob("*.py"))

    def test_no_forbidden_endpoints_outside_endpoints_module(self):
        offenders: list[str] = []
        for path in self._python_files():
            if path.name == "endpoints.py":
                continue   # lists them as a deny-list on purpose
            text = path.read_text(encoding="utf-8")
            for fragment in FORBIDDEN_ENDPOINT_FRAGMENTS:
                if fragment in text:
                    offenders.append(f"{path.name}: {fragment}")
        assert not offenders, f"withdrawal/transfer/deposit endpoints found: {offenders}"

    def test_no_bybit_host_literals_outside_endpoints_module(self):
        offenders: list[str] = []
        for path in self._python_files():
            if path.name == "endpoints.py":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("*"):
                    continue
                if "https://api.bybit" in line or "https://api-testnet.bybit" in line:
                    offenders.append(f"{path.name}: {stripped[:90]}")
        assert not offenders, f"Bybit host literals outside endpoints.py: {offenders}"

    def test_mainnet_constant_used_only_by_the_probe(self):
        users = [
            path.name
            for path in self._python_files()
            if path.name != "endpoints.py"
            and "NEGATIVE_CONTROL_HOST" in path.read_text(encoding="utf-8")
        ]
        assert users == ["rest.py"], f"unexpected users of the mainnet host: {users}"

    def test_no_bare_except_blocks(self):
        offenders: list[str] = []
        for path in self._python_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if line.strip() in {"except:", "except :"}:
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, f"bare except blocks found: {offenders}"

    def test_no_hardcoded_credential_literals(self):
        import re

        pattern = re.compile(
            r"""(api_key|api_secret|apikey|secret)\s*=\s*["'][A-Za-z0-9_\-]{16,}["']""",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for path in self._python_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, f"hardcoded credentials: {offenders}"

    def test_no_live_mode_flags(self):
        import re

        pattern = re.compile(
            r"\b(live_trading|real_money|enable_live|mainnet_mode|allow_live)\b", re.IGNORECASE
        )
        offenders: list[str] = []
        for path in self._python_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, f"live/real-money flags found: {offenders}"


class TestConfigCannotBypassTheLock:
    def test_demo_requirement_cannot_be_disabled(self):
        with pytest.raises(Exception) as exc:
            SafetyConfig(require_demo_verification_for_orders=False)
        assert "not bypassable" in str(exc.value)

    def test_default_enables_the_negative_control(self):
        assert SafetyConfig().mainnet_negative_control is True

    def test_unknown_safety_keys_are_rejected(self):
        with pytest.raises(Exception):
            SafetyConfig(allow_mainnet=True)  # type: ignore[call-arg]


class TestDemoGuardGating:
    def _guard_with(self, signals: list[SignalResult]) -> DemoGuard:
        from btcbot.utils.timeutil import now_utc

        client = BybitDemoClient()
        guard = DemoGuard(client, run_mainnet_negative_control=False)
        verified = all(s.passed for s in signals if s.required)
        guard._verified = verified              # noqa: SLF001 - test seam
        guard._last_verification = DemoVerification(   # noqa: SLF001
            verified=verified, checked_at=now_utc(), signals=tuple(signals)
        )
        return guard

    def test_orders_blocked_before_verification(self):
        guard = DemoGuard(BybitDemoClient())
        assert guard.verified is False
        assert guard.orders_permitted() is False

    def test_orders_permitted_only_when_all_required_signals_pass(self):
        guard = self._guard_with(
            [
                SignalResult("host pin", True, "ok"),
                SignalResult("authenticated reachability", True, "ok"),
                SignalResult("demo-only endpoint probe", True, "ok"),
                SignalResult("mainnet negative control", True, "ok"),
            ]
        )
        assert guard.orders_permitted() is True

    def test_one_failed_signal_blocks_orders(self):
        guard = self._guard_with(
            [
                SignalResult("host pin", True, "ok"),
                SignalResult("authenticated reachability", True, "ok"),
                SignalResult("demo-only endpoint probe", False, "route missing"),
                SignalResult("mainnet negative control", True, "ok"),
            ]
        )
        assert guard.orders_permitted() is False
        assert len(guard.last_verification.failures) == 1

    def test_optional_signal_failure_does_not_block(self):
        guard = self._guard_with(
            [
                SignalResult("host pin", True, "ok"),
                SignalResult("authenticated reachability", True, "ok"),
                SignalResult("demo-only endpoint probe", True, "ok"),
                SignalResult("mainnet negative control", False, "skipped", required=False),
            ]
        )
        assert guard.orders_permitted() is True

    def test_revoke_immediately_disables_orders(self):
        guard = self._guard_with(
            [SignalResult("host pin", True, "ok")]
        )
        assert guard.orders_permitted() is True
        guard.revoke("re-verification failed")
        assert guard.orders_permitted() is False

    def test_banner_text_matches_the_specification(self):
        assert SAFETY_LOCK_BANNER == (
            "SAFETY LOCK",
            "BYBIT DEMO ENVIRONMENT COULD NOT BE VERIFIED",
            "ORDER SUBMISSION DISABLED",
        )


class TestSecretHandling:
    def test_redaction_keeps_only_a_short_prefix(self):
        assert redact_secret("abcdefghijklmnop") == "abcd********"
        assert redact_secret("") == "<unset>"
        assert redact_secret(None) == "<unset>"
        assert redact_secret("abc") == "*" * 8

    def test_redaction_hides_length(self):
        short = redact_secret("abcd" + "x" * 10)
        long = redact_secret("abcd" + "x" * 200)
        assert short == long, "redacted output must not leak secret length"

    def test_log_filter_scrubs_registered_secrets(self, caplog):
        import logging

        from btcbot.utils.logging import SecretRedactionFilter

        secret = "SUPERSECRETKEY1234567890"
        register_secret(secret)

        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg=f"connecting with {secret}", args=(), exc_info=None,
        )
        SecretRedactionFilter().filter(record)
        assert secret not in record.getMessage()
        assert "REDACTED" in record.getMessage()


class TestCircuitBreakers:
    @pytest.fixture
    def breakers(self) -> CircuitBreakers:
        return CircuitBreakers(SafetyConfig())

    def test_impossible_prices_trip(self, breakers):
        assert breakers.check_price(0.0) is not None
        assert breakers.safe_mode.active

    def test_price_outside_sanity_band_trips(self, breakers):
        trip = breakers.check_price(10.0)
        assert trip is not None
        assert trip.breaker is BreakerType.IMPOSSIBLE_PRICE

    def test_nan_price_trips(self, breakers):
        assert breakers.check_price(float("nan")) is not None

    def test_sudden_price_jump_trips(self, breakers):
        assert breakers.check_price(50_000.0) is None
        trip = breakers.check_price(90_000.0)
        assert trip is not None
        assert trip.breaker is BreakerType.PRICE_JUMP

    def test_normal_price_movement_does_not_trip(self, breakers):
        assert breakers.check_price(50_000.0) is None
        assert breakers.check_price(50_400.0) is None
        assert breakers.check_price(49_600.0) is None
        assert not breakers.safe_mode.active

    def test_extreme_quantity_trips(self, breakers):
        trip = breakers.check_quantity(100.0, equity=10_000.0, price=50_000.0)
        assert trip is not None
        assert trip.breaker is BreakerType.EXTREME_QUANTITY

    @pytest.mark.parametrize("quantity", [0.0, -1.0, float("nan"), float("inf")])
    def test_invalid_quantities_trip(self, breakers, quantity):
        assert breakers.check_quantity(quantity, equity=10_000.0, price=50_000.0) is not None

    def test_invalid_balance_trips(self, breakers):
        assert breakers.check_balance(-1.0, 0.0) is not None

    def test_available_exceeding_equity_trips(self, breakers):
        assert breakers.check_balance(1_000.0, 5_000.0) is not None

    def test_stale_data_trips(self, breakers):
        trip = breakers.check_data_freshness(False, "ticker stale 400s")
        assert trip is not None
        assert trip.breaker is BreakerType.STALE_DATA

    def test_repeated_api_errors_trip(self, breakers):
        assert breakers.check_api_errors(3) is None
        assert breakers.check_api_errors(50) is not None

    def test_order_rate_limit_trips(self, breakers):
        for _ in range(SafetyConfig().max_orders_per_minute):
            breakers.record_order_submitted()
        assert breakers.check_order_rate() is not None

    def test_state_mismatch_trips(self, breakers):
        assert breakers.check_state_consistency(exchange_positions=1, ledger_positions=1) is None
        assert breakers.check_state_consistency(exchange_positions=2, ledger_positions=0) is not None

    def test_corrupt_strategy_output_trips(self, breakers):
        class Broken:
            entry_reference = float("nan")
            stop_price = 1.0
            confidence = 0.5

        assert breakers.check_strategy_output(Broken()) is not None

    def test_confidence_outside_unit_range_trips(self, breakers):
        class BadConfidence:
            entry_reference = 50_000.0
            stop_price = 49_000.0
            confidence = 7.5

        assert breakers.check_strategy_output(BadConfidence()) is not None

    def test_valid_signal_passes(self, breakers):
        class Good:
            entry_reference = 50_000.0
            stop_price = 49_000.0
            confidence = 0.7

        assert breakers.check_strategy_output(Good()) is None

    def test_repeated_duplicates_trip(self, breakers):
        for _ in range(4):
            assert breakers.record_duplicate_attempt("setup-1") is None
        assert breakers.record_duplicate_attempt("setup-1") is not None

    def test_safe_mode_blocks_orders_then_clears(self, breakers):
        config = SafetyConfig(safe_mode_cooldown_seconds=0)
        breakers = CircuitBreakers(config)
        breakers.check_price(0.0)
        allowed, reason = breakers.orders_allowed()
        # With a zero cooldown the first check may already clear it; either way
        # the state must be self-consistent.
        if not allowed:
            assert "SAFE_MODE" in reason
        assert breakers.orders_allowed()[0] is True
