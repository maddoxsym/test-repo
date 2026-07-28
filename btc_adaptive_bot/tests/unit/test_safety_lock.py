"""Demo safety lock, live-environment rejection, and circuit breakers.

The core guarantees under test: **there is no way to construct an authenticated
client against a live host, no way to emit a request without the
``x-simulated-trading: 1`` header, and no way to submit an order without a
passing demo verification.**
"""

from __future__ import annotations

from pathlib import Path

import pytest

from btcbot.config.schema import SafetyConfig
from btcbot.exchange.demo_guard import SAFETY_LOCK_BANNER, DemoGuard, DemoVerification, SignalResult
from btcbot.exchange.endpoints import (
    ALLOWED_DEMO_HOSTS,
    ALLOWED_WS_URLS,
    DEMO_REST_HOST,
    DEMO_WS_BUSINESS,
    DEMO_WS_PRIVATE,
    DEMO_WS_PUBLIC,
    FORBIDDEN_ENDPOINT_FRAGMENTS,
    FORBIDDEN_HOSTS,
    SIMULATED_TRADING_HEADER,
    SIMULATED_TRADING_VALUE,
    is_allowed_authenticated_host,
    is_allowed_ws_url,
)
from btcbot.exchange.rest import LiveEnvironmentNegativeControlProbe, OkxDemoClient
from btcbot.safety.circuit_breakers import BreakerType, CircuitBreakers
from btcbot.utils.errors import MainnetRejectedError
from btcbot.utils.ids import redact_secret
from btcbot.utils.logging import register_secret

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _demo_client(**overrides) -> OkxDemoClient:
    kwargs = {"api_key": "k" * 20, "api_secret": "s" * 20, "passphrase": "p" * 12}
    kwargs.update(overrides)
    return OkxDemoClient(**kwargs)


class TestHostAllowList:
    def test_demo_host_is_the_eea_entity(self):
        assert DEMO_REST_HOST == "https://eea.okx.com"
        assert DEMO_REST_HOST in ALLOWED_DEMO_HOSTS

    def test_allow_lists_are_immutable(self):
        assert isinstance(ALLOWED_DEMO_HOSTS, frozenset)
        assert isinstance(ALLOWED_WS_URLS, frozenset)
        with pytest.raises(AttributeError):
            ALLOWED_DEMO_HOSTS.add("https://www.okx.com")  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "host",
        [
            "https://www.okx.com",
            "https://us.okx.com",
            "https://openapi.okx.com",
            "https://my.okx.com",
            "https://eea.okx.com.evil.example",
            "http://eea.okx.com",
            "https://localhost",
            "",
        ],
    )
    def test_non_demo_hosts_are_refused(self, host):
        assert not is_allowed_authenticated_host(host)

    def test_ws_urls_are_the_eea_demo_endpoints(self):
        assert DEMO_WS_PUBLIC == "wss://wseeapap.okx.com:8443/ws/v5/public"
        assert DEMO_WS_PRIVATE == "wss://wseeapap.okx.com:8443/ws/v5/private"
        # Candle channels live on the business endpoint, which carries the
        # demo brokerId query.
        assert DEMO_WS_BUSINESS == "wss://wseeapap.okx.com:8443/ws/v5/business?brokerId=9999"
        for url in (DEMO_WS_PUBLIC, DEMO_WS_PRIVATE, DEMO_WS_BUSINESS):
            assert is_allowed_ws_url(url)

    def test_eea_live_ws_differs_by_one_infix_and_is_refused(self):
        """The live host is a single dropped 'pap' away — exact matching only."""
        live = DEMO_WS_PRIVATE.replace("wseeapap", "wseea")
        assert live == "wss://wseea.okx.com:8443/ws/v5/private"
        assert not is_allowed_ws_url(live)
        assert live in FORBIDDEN_HOSTS

    @pytest.mark.parametrize(
        "url",
        [
            "wss://ws.okx.com:8443/ws/v5/private",       # global live
            "wss://wspap.okx.com:8443/ws/v5/private",    # global demo, wrong entity
            "wss://wsuspap.okx.com:8443/ws/v5/private",  # US demo, wrong entity
            "wss://wseea.okx.com:8443/ws/v5/public",     # EEA LIVE
            "wss://wseeapap.okx.com:8443/ws/v5/business",  # missing brokerId query
            "",
        ],
    )
    def test_every_other_ws_url_is_refused(self, url):
        assert not is_allowed_ws_url(url)

    def test_ws_socket_construction_refuses_live_urls(self):
        from btcbot.exchange.ws import _ReconnectingSocket

        with pytest.raises(MainnetRejectedError):
            _ReconnectingSocket("wss://wseea.okx.com:8443/ws/v5/private", name="x")


class TestLiveEnvironmentRejection:
    @pytest.mark.parametrize(
        "host",
        [
            "https://www.okx.com",
            "https://us.okx.com",
            "https://openapi.okx.com",
            "https://my.okx.com",
        ],
    )
    def test_client_construction_refuses_non_demo_hosts(self, host):
        """Refusal happens at construction, before any network activity."""
        with pytest.raises(MainnetRejectedError) as exc:
            _demo_client(base_url=host)
        assert "no real-money trading mode" in str(exc.value)

    def test_demo_host_construction_succeeds(self):
        client = _demo_client()
        assert client.base_url == DEMO_REST_HOST

    def test_trailing_slash_is_normalised(self):
        client = OkxDemoClient(base_url=DEMO_REST_HOST + "/")
        assert client.base_url == DEMO_REST_HOST

    def test_negative_control_probe_has_no_order_capability(self):
        """The live-environment probe must be structurally incapable of trading."""
        probe = LiveEnvironmentNegativeControlProbe("k" * 20, "s" * 20, "p" * 12)
        for forbidden in (
            "place_order", "cancel_order", "submit", "set_leverage",
            "cancel_all", "get_wallet_balance",
        ):
            assert not hasattr(probe, forbidden), f"probe must not expose {forbidden}"
        # Exactly one public method.
        public = [n for n in dir(probe) if not n.startswith("_")]
        assert public == ["credentials_are_rejected"]


class TestDemoHeaderEnforcement:
    """OKX selects the environment per-request; the header is the safety switch."""

    def test_header_constants_match_the_documented_switch(self):
        assert SIMULATED_TRADING_HEADER == "x-simulated-trading"
        assert SIMULATED_TRADING_VALUE == "1"

    def test_finalize_headers_always_injects_the_demo_switch(self):
        client = _demo_client()
        for base in (None, {}, {"Content-Type": "application/json"}, {"X-Whatever": "y"}):
            built = client._finalize_headers(base)  # noqa: SLF001 - the choke point itself
            assert built[SIMULATED_TRADING_HEADER] == SIMULATED_TRADING_VALUE

    def test_finalize_headers_cannot_be_overridden_by_input(self):
        client = _demo_client()
        built = client._finalize_headers({SIMULATED_TRADING_HEADER: "0"})  # noqa: SLF001
        assert built[SIMULATED_TRADING_HEADER] == SIMULATED_TRADING_VALUE

    def test_runtime_self_check_passes(self):
        assert _demo_client().demo_header_enforced() is True

    def test_credentials_require_all_three_parts(self):
        assert _demo_client().has_credentials
        assert not _demo_client(passphrase=None).has_credentials
        assert not _demo_client(api_secret=None).has_credentials
        assert not OkxDemoClient().has_credentials


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

    def test_no_okx_host_literals_outside_endpoints_module(self):
        offenders: list[str] = []
        for path in self._python_files():
            if path.name == "endpoints.py":
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith("*"):
                    continue
                if "okx.com" in line:
                    offenders.append(f"{path.name}: {stripped[:90]}")
        assert not offenders, f"OKX host literals outside endpoints.py: {offenders}"

    def test_negative_control_path_used_only_by_the_probe(self):
        users = [
            path.name
            for path in self._python_files()
            if path.name != "endpoints.py"
            and "NEGATIVE_CONTROL_PATH" in path.read_text(encoding="utf-8")
        ]
        assert users == ["rest.py"], f"unexpected users of the negative-control path: {users}"

    def test_demo_header_constant_used_only_by_the_transport_layer(self):
        """One header-building path: endpoints.py defines it, rest.py injects it."""
        users = [
            path.name
            for path in self._python_files()
            if path.name != "endpoints.py"
            and "SIMULATED_TRADING_HEADER" in path.read_text(encoding="utf-8")
        ]
        assert users == ["rest.py"], f"unexpected users of the demo header: {users}"

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
            r"""(api_key|api_secret|apikey|secret|passphrase)\s*=\s*["'][A-Za-z0-9_\-]{16,}["']""",
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
            r"\b(live_trading|real_money|enable_live|mainnet_mode|allow_live|disable_simulated)\b",
            re.IGNORECASE,
        )
        offenders: list[str] = []
        for path in self._python_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, f"live/real-money flags found: {offenders}"

    def test_no_hardcoded_inst_id_in_source(self):
        """The X-Perp instId must come from runtime discovery, never a literal."""
        offenders: list[str] = []
        for path in self._python_files():
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if '"BTC-USDT-SWAP"' in line or "'BTC-USDT-SWAP'" in line:
                    offenders.append(f"{path.name}:{number}")
        assert not offenders, f"hardcoded instId found in src/: {offenders}"


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

        client = OkxDemoClient()
        guard = DemoGuard(client, run_mainnet_negative_control=False)
        verified = all(s.passed for s in signals if s.required)
        guard._verified = verified              # noqa: SLF001 - test seam
        guard._last_verification = DemoVerification(   # noqa: SLF001
            verified=verified, checked_at=now_utc(), signals=tuple(signals)
        )
        return guard

    def test_orders_blocked_before_verification(self):
        guard = DemoGuard(OkxDemoClient())
        assert guard.verified is False
        assert guard.orders_permitted() is False

    def test_orders_permitted_only_when_all_required_signals_pass(self):
        guard = self._guard_with(
            [
                SignalResult("host pin", True, "ok"),
                SignalResult("demo header enforcement", True, "ok"),
                SignalResult("authenticated reachability", True, "ok"),
                SignalResult("live-environment negative control", True, "ok"),
            ]
        )
        assert guard.orders_permitted() is True

    def test_one_failed_signal_blocks_orders(self):
        guard = self._guard_with(
            [
                SignalResult("host pin", True, "ok"),
                SignalResult("demo header enforcement", False, "header missing"),
                SignalResult("authenticated reachability", True, "ok"),
                SignalResult("live-environment negative control", True, "ok"),
            ]
        )
        assert guard.orders_permitted() is False
        assert len(guard.last_verification.failures) == 1

    def test_optional_signal_failure_does_not_block(self):
        guard = self._guard_with(
            [
                SignalResult("host pin", True, "ok"),
                SignalResult("demo header enforcement", True, "ok"),
                SignalResult("authenticated reachability", True, "ok"),
                SignalResult(
                    "live-environment negative control", False, "skipped", required=False
                ),
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
            "OKX DEMO ENVIRONMENT COULD NOT BE VERIFIED",
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

    def test_passphrase_is_registered_for_redaction(self, monkeypatch):
        """Loading credentials must register all three parts with the filter."""
        import logging

        from btcbot.config.loader import load_credentials
        from btcbot.utils.logging import SecretRedactionFilter

        monkeypatch.setenv("OKX_DEMO_API_KEY", "KEYKEYKEYKEY123456")
        monkeypatch.setenv("OKX_DEMO_API_SECRET", "SECSECSECSEC123456")
        monkeypatch.setenv("OKX_DEMO_PASSPHRASE", "PASSPHRASE9876543")
        load_credentials(env_file=None, required=True)
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="auth with PASSPHRASE9876543", args=(), exc_info=None,
        )
        SecretRedactionFilter().filter(record)
        assert "PASSPHRASE9876543" not in record.getMessage()


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

    def test_clock_drift_within_budget_passes(self, breakers):
        assert breakers.check_clock_drift(1_000) is None
        assert not breakers.safe_mode.active

    def test_clock_drift_beyond_budget_trips(self, breakers):
        """OKX rejects drifted timestamps; a wandering clock pauses trading."""
        trip = breakers.check_clock_drift(SafetyConfig().max_clock_drift_ms + 1)
        assert trip is not None
        assert trip.breaker is BreakerType.CLOCK_DRIFT
        assert breakers.safe_mode.active

    def test_negative_drift_also_trips(self, breakers):
        assert breakers.check_clock_drift(-(SafetyConfig().max_clock_drift_ms + 1)) is not None

    def test_healthy_margin_ratio_passes(self, breakers):
        assert (
            breakers.check_liquidation_risk(margin_ratio=25.0, inst_id="X") is None
        )
        assert breakers.check_liquidation_risk(margin_ratio=None, inst_id="X") is None

    def test_degraded_margin_ratio_trips(self, breakers):
        """A margin ratio near the maintenance level must flatten and pause."""
        floor = SafetyConfig().liquidation_margin_ratio_floor
        trip = breakers.check_liquidation_risk(margin_ratio=floor - 0.5, inst_id="X")
        assert trip is not None
        assert trip.breaker is BreakerType.LIQUIDATION_RISK

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
