"""OKX authentication: signing, timestamp format, headers, WS login.

The signing scheme is the one thing that cannot be validated offline against
the exchange, so it is validated against the *specification* instead: every
expected value below is computed independently in the test (by hand or with
the stdlib) rather than copied from the implementation.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re

import pytest

from btcbot.exchange.endpoints import (
    SIMULATED_TRADING_HEADER,
    from_okx_bar,
    to_okx_bar,
)
from btcbot.exchange.signing import (
    WS_LOGIN_SIGN_PATH,
    build_prehash,
    build_query_string,
    okx_timestamp,
    serialize_body,
    sign_payload,
    sign_request,
    sign_ws_login,
)

KEY = "test-key-0001"
SECRET = "test-secret-0001"
PASSPHRASE = "test-passphrase"


def _expected_signature(secret: str, prehash: str) -> str:
    """Independent reference implementation, straight from the spec."""
    digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class TestTimestampFormat:
    def test_iso8601_with_milliseconds_and_z(self):
        """OKX requires ISO-8601 UTC with exactly three decimal places."""
        stamp = okx_timestamp(now_ms_value=1_700_000_000_123)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", stamp), stamp

    def test_timestamp_is_utc(self):
        # 1_700_000_000_000 ms == 2023-11-14T22:13:20Z
        assert okx_timestamp(now_ms_value=1_700_000_000_000) == "2023-11-14T22:13:20.000Z"

    def test_milliseconds_are_truncated_not_rounded(self):
        assert okx_timestamp(now_ms_value=1_700_000_000_999).endswith(".999Z")
        assert okx_timestamp(now_ms_value=1_700_000_000_001).endswith(".001Z")

    def test_timestamp_is_deterministic_for_a_given_instant(self):
        """Taking the clock as an argument keeps signing reproducible."""
        assert okx_timestamp(now_ms_value=1) == okx_timestamp(now_ms_value=1)


class TestSignatureFormat:
    def test_signature_is_base64_not_hex(self):
        """The single most common OKX porting error: emitting hex."""
        signature = sign_payload(SECRET, "anything")
        # Valid base64 of a 32-byte digest is 44 chars ending in '='.
        assert len(signature) == 44
        assert signature.endswith("=")
        base64.b64decode(signature)  # must not raise
        # A hex digest would be 64 chars of [0-9a-f].
        assert not re.fullmatch(r"[0-9a-f]{64}", signature)

    def test_signature_matches_the_reference_implementation(self):
        prehash = "2023-11-14T22:13:20.000ZGET/api/v5/account/balance"
        assert sign_payload(SECRET, prehash) == _expected_signature(SECRET, prehash)

    def test_different_secrets_produce_different_signatures(self):
        assert sign_payload("a" * 20, "msg") != sign_payload("b" * 20, "msg")


class TestPreHashComposition:
    def test_prehash_is_timestamp_method_path_body(self):
        prehash = build_prehash("TS", "get", "/api/v5/x", "BODY")
        assert prehash == "TSGET/api/v5/xBODY"

    def test_method_is_uppercased(self):
        assert build_prehash("T", "post", "/p", "") == "TPOST/p"

    def test_get_request_path_includes_the_query_string(self):
        signed = sign_request(
            api_key=KEY, api_secret=SECRET, passphrase=PASSPHRASE,
            method="GET", path="/api/v5/account/balance",
            timestamp="2023-11-14T22:13:20.000Z", params={"ccy": "USDT"},
        )
        expected = _expected_signature(
            SECRET,
            "2023-11-14T22:13:20.000ZGET/api/v5/account/balance?ccy=USDT",
        )
        assert signed.headers["OK-ACCESS-SIGN"] == expected
        assert signed.query_string == "ccy=USDT"

    def test_empty_body_signs_as_empty_string_not_braces(self):
        signed = sign_request(
            api_key=KEY, api_secret=SECRET, passphrase=PASSPHRASE,
            method="GET", path="/api/v5/public/time",
            timestamp="2023-11-14T22:13:20.000Z",
        )
        expected = _expected_signature(
            SECRET, "2023-11-14T22:13:20.000ZGET/api/v5/public/time"
        )
        assert signed.headers["OK-ACCESS-SIGN"] == expected
        assert signed.body is None

    def test_post_signs_the_exact_transmitted_body(self):
        body = {"instId": "X", "sz": "1"}
        signed = sign_request(
            api_key=KEY, api_secret=SECRET, passphrase=PASSPHRASE,
            method="POST", path="/api/v5/trade/order",
            timestamp="2023-11-14T22:13:20.000Z", body=body,
        )
        # The transmitted body and the signed body must be byte-identical.
        assert signed.body == json.dumps(body, separators=(",", ":"))
        expected = _expected_signature(
            SECRET, "2023-11-14T22:13:20.000ZPOST/api/v5/trade/order" + signed.body
        )
        assert signed.headers["OK-ACCESS-SIGN"] == expected

    def test_none_values_are_dropped_from_the_body(self):
        signed = sign_request(
            api_key=KEY, api_secret=SECRET, passphrase=PASSPHRASE,
            method="POST", path="/p", timestamp="T",
            body={"a": "1", "b": None},
        )
        assert signed.body == '{"a":"1"}'

    def test_list_bodies_are_serialised_for_batch_endpoints(self):
        assert serialize_body([{"a": 1}]) == '[{"a":1}]'


class TestHeaders:
    def test_all_four_auth_headers_are_present(self):
        signed = sign_request(
            api_key=KEY, api_secret=SECRET, passphrase=PASSPHRASE,
            method="GET", path="/p", timestamp="T",
        )
        for header in (
            "OK-ACCESS-KEY", "OK-ACCESS-SIGN", "OK-ACCESS-TIMESTAMP", "OK-ACCESS-PASSPHRASE"
        ):
            assert header in signed.headers, header
        assert signed.headers["OK-ACCESS-KEY"] == KEY
        assert signed.headers["OK-ACCESS-PASSPHRASE"] == PASSPHRASE
        assert signed.headers["Content-Type"] == "application/json"

    def test_signing_does_not_add_the_demo_header(self):
        """The demo switch belongs to the transport layer, not the signer —
        so there is exactly one place it can be forgotten, and that place
        cannot forget it."""
        signed = sign_request(
            api_key=KEY, api_secret=SECRET, passphrase=PASSPHRASE,
            method="GET", path="/p", timestamp="T",
        )
        assert SIMULATED_TRADING_HEADER not in signed.headers


class TestQueryString:
    def test_none_values_are_omitted(self):
        assert build_query_string({"a": "1", "b": None}) == "a=1"

    def test_insertion_order_is_preserved(self):
        """The signed path must match the transmitted URL exactly."""
        assert build_query_string({"z": "1", "a": "2"}) == "z=1&a=2"

    def test_booleans_are_lowercased(self):
        assert build_query_string({"x": True, "y": False}) == "x=true&y=false"

    def test_empty_inputs_produce_an_empty_string(self):
        assert build_query_string(None) == ""
        assert build_query_string({}) == ""
        assert build_query_string({"a": None}) == ""


class TestWebSocketLogin:
    def test_login_uses_epoch_seconds_not_iso(self):
        """The WS handshake signs a different timestamp format from REST."""
        args = sign_ws_login(KEY, SECRET, PASSPHRASE, epoch_seconds=1_700_000_000)
        assert args["timestamp"] == "1700000000"
        assert "T" not in args["timestamp"]

    def test_login_signs_the_verify_path(self):
        args = sign_ws_login(KEY, SECRET, PASSPHRASE, epoch_seconds=1_700_000_000)
        expected = _expected_signature(SECRET, f"1700000000GET{WS_LOGIN_SIGN_PATH}")
        assert args["sign"] == expected

    def test_login_carries_key_and_passphrase(self):
        args = sign_ws_login(KEY, SECRET, PASSPHRASE, epoch_seconds=1)
        assert args["apiKey"] == KEY
        assert args["passphrase"] == PASSPHRASE
        assert set(args) == {"apiKey", "passphrase", "timestamp", "sign"}


class TestBarTranslation:
    """Internal timeframe notation ↔ OKX `bar` strings."""

    @pytest.mark.parametrize(
        ("internal", "okx"),
        [("1", "1m"), ("5", "5m"), ("15", "15m"), ("60", "1H"), ("240", "4H")],
    )
    def test_round_trip(self, internal, okx):
        assert to_okx_bar(internal) == okx
        assert from_okx_bar(okx) == internal

    def test_daily_and_weekly_use_utc_aligned_variants(self):
        """Plain '1D' opens on a UTC+8 boundary; everything here is UTC."""
        assert to_okx_bar("D") == "1Dutc"
        assert to_okx_bar("W") == "1Wutc"

    def test_unknown_timeframe_is_refused(self):
        with pytest.raises(ValueError):
            to_okx_bar("7")
        with pytest.raises(ValueError):
            from_okx_bar("nonsense")
