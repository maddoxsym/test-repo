"""OKX order-response handling — the envelope is not the rejection.

OKX's trade endpoints are batch-shaped even for a single order. The envelope
``code`` describes the *batch* (0 all succeeded, 1 all failed, 2 partial) and
the actual reason lives in each item's ``sCode``/``sMsg``/``subCode``. An
envelope of ``code=1 msg="All operations failed"`` therefore says nothing
useful, and a transport that raises on it destroys the only diagnostic there
is.

These tests drive the **real** :class:`OkxDemoClient` against a stubbed
transport, so the whole path — header injection, signing, envelope handling,
per-item parsing — is exercised exactly as it runs against the exchange.

The guarantees under test:

* codes 1 and 2 reach the endpoint parser **only** on order endpoints, and
  only when a usable data array is present;
* neither is ever treated as success by any caller;
* every other envelope code still fails immediately in the transport layer;
* nothing from the signed request reaches a log.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from btcbot.exchange.endpoints import (
    ITEM_LEVEL_ENVELOPE_CODES,
    ORDER_OPERATION_PATHS,
    Paths,
)
from btcbot.exchange.models import OrderRequest, OrderType, Side, TdMode
from btcbot.exchange.rest import OkxDemoClient
from btcbot.utils.errors import ApiError, OrderRejectedError

API_KEY = "smoke-key-AAAAAAAAAAAAAAAA"
API_SECRET = "smoke-secret-BBBBBBBBBBBBBBBB"
PASSPHRASE = "smoke-passphrase-CCCC"

# The failing response from the reported smoke test, verbatim.
ALL_OPERATIONS_FAILED = {
    "code": "1",
    "msg": "All operations failed",
    "data": [
        {
            "ordId": "",
            "clOrdId": "test-order",
            "sCode": "51000",
            "sMsg": "Example detailed rejection",
            "subCode": "1000",
        }
    ],
}


def _client(responses: list[dict[str, Any]] | dict[str, Any], *, status: int = 200):
    """A real client whose transport replays canned OKX envelopes."""
    queue = [responses] if isinstance(responses, dict) else list(responses)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx.Response(status, json=payload)

    client = OkxDemoClient(
        api_key=API_KEY,
        api_secret=API_SECRET,
        passphrase=PASSPHRASE,
        max_retries=0,
    )
    client._client = httpx.AsyncClient(   # noqa: SLF001 - transport seam
        base_url=client.base_url,
        transport=httpx.MockTransport(handler),
    )
    client.requests = seen                # type: ignore[attr-defined]
    return client


def _order(client_order_id: str = "test-order") -> OrderRequest:
    return OrderRequest(
        inst_id="BTC-USDT-SWAP",
        td_mode=TdMode.ISOLATED,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        sz="1",
        client_order_id=client_order_id,
    )


def _ok(**overrides: Any) -> dict[str, Any]:
    item = {"ordId": "1234567890", "clOrdId": "test-order", "sCode": "0", "sMsg": ""}
    item.update(overrides)
    return {"code": "0", "msg": "", "data": [item]}


class TestTheReportedBug:
    """code=1 must surface the per-order reason, not 'All operations failed'."""

    async def test_place_order_raises_with_the_item_code_and_message(self):
        client = _client(ALL_OPERATIONS_FAILED)
        try:
            with pytest.raises(ApiError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()

        error = exc.value
        # The real code, not the envelope's 1.
        assert error.ret_code == 51000
        assert error.ret_code != 1
        # The real reason, not "All operations failed".
        assert "Example detailed rejection" in str(error)
        assert "All operations failed" not in str(error)

    async def test_the_rejection_carries_subcode_and_client_order_id(self):
        client = _client(ALL_OPERATIONS_FAILED)
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()

        error = exc.value
        assert error.s_code == 51000
        assert error.s_msg == "Example detailed rejection"
        assert error.sub_code == "1000"
        assert error.client_order_id == "test-order"
        assert error.endpoint == Paths.ORDER

    async def test_report_lines_match_the_required_operator_output(self):
        client = _client(ALL_OPERATIONS_FAILED)
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()

        lines = exc.value.report_lines()
        assert lines[0] == "OKX sCode=51000"
        assert lines[1] == "sMsg=Example detailed rejection"
        assert "subCode=1000" in lines

    async def test_smoke_test_renders_the_rejection_as_separate_lines(self):
        """The reported failure output must name sCode, sMsg and subCode."""
        from btcbot.app.smoke_test import SmokeReport, _rejection_lines

        error = OrderRejectedError(
            51000, "Example detailed rejection", Paths.ORDER,
            sub_code="1000", client_order_id="test-order",
        )
        report = SmokeReport()
        report.add(
            "Place minimum-size demo order", False, str(error),
            lines=_rejection_lines(error),
        )
        step = report.steps[0]
        assert not step.passed
        assert "OKX sCode=51000" in step.lines
        assert "sMsg=Example detailed rejection" in step.lines
        assert "subCode=1000" in step.lines

    def test_a_non_order_failure_still_renders_on_one_line(self):
        from btcbot.app.smoke_test import _rejection_lines

        assert _rejection_lines(ApiError(50111, "Invalid signature", Paths.ORDER)) is None


class TestEnvelopeCodeZero:
    async def test_code_zero_with_scode_zero_succeeds(self):
        client = _client(_ok())
        try:
            result = await client.place_order(_order())
        finally:
            await client.close()

        assert result.accepted is True
        assert result.s_code == 0
        assert result.exchange_order_id == "1234567890"
        assert result.client_order_id == "test-order"

    async def test_code_zero_with_an_empty_data_array_is_not_an_acceptance(self):
        """An order we cannot see an ordId for was not confirmed accepted."""
        client = _client({"code": "0", "msg": "", "data": []})
        try:
            with pytest.raises(ApiError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()
        assert exc.value.ret_code != 0, "an error must never report code 0"
        assert "no usable data array" in exc.value.ret_msg

    async def test_code_zero_with_a_nonzero_scode_is_still_rejected(self):
        """A clean envelope does not make a rejected item acceptable."""
        payload = _ok(sCode="51008", sMsg="Insufficient balance", ordId="")
        payload["code"] = "0"
        client = _client(payload)
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()
        assert exc.value.s_code == 51008


class TestEnvelopeCodeOne:
    async def test_nonzero_scode_fails_with_the_item_error(self):
        client = _client(
            {
                "code": "1",
                "msg": "All operations failed",
                "data": [{"ordId": "", "clOrdId": "c1", "sCode": "51008",
                          "sMsg": "Order placement failed due to insufficient balance"}],
            }
        )
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order("c1"))
        finally:
            await client.close()
        assert exc.value.s_code == 51008
        assert "insufficient balance" in exc.value.s_msg

    async def test_an_unparseable_scode_is_treated_as_a_failure(self):
        """A code we cannot read is never assumed to mean success."""
        client = _client(
            {"code": "1", "msg": "All operations failed",
             "data": [{"ordId": "", "clOrdId": "c1", "sCode": "not-a-number", "sMsg": "?"}]}
        )
        try:
            with pytest.raises(OrderRejectedError):
                await client.place_order(_order("c1"))
        finally:
            await client.close()

    async def test_code_one_with_an_empty_data_array_fails_immediately(self):
        """With no item there is nothing to inspect — the envelope must raise."""
        client = _client({"code": "1", "msg": "All operations failed", "data": []})
        try:
            with pytest.raises(ApiError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()
        assert exc.value.ret_code == 1
        assert not isinstance(exc.value, OrderRejectedError)

    @pytest.mark.parametrize("data", [None, "unexpected", [[]], [None], {}])
    async def test_code_one_with_a_malformed_data_array_fails_immediately(self, data):
        client = _client({"code": "1", "msg": "All operations failed", "data": data})
        try:
            with pytest.raises(ApiError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()
        assert exc.value.ret_code == 1


class TestEnvelopeCodeTwoPartialSuccess:
    async def test_a_partial_batch_never_reports_every_cancel_as_cancelled(self):
        """code=2 means *some* failed — the tally must reflect that."""
        pending = {
            "code": "0", "msg": "",
            "data": [
                {"instId": "BTC-USDT-SWAP", "ordId": "A", "clOrdId": "a", "state": "live",
                 "side": "buy", "ordType": "limit", "px": "50000", "sz": "1", "accFillSz": "0",
                 "posSide": "net", "cTime": "1700000000000"},
                {"instId": "BTC-USDT-SWAP", "ordId": "B", "clOrdId": "b", "state": "live",
                 "side": "buy", "ordType": "limit", "px": "50000", "sz": "1", "accFillSz": "0",
                 "posSide": "net", "cTime": "1700000000000"},
            ],
        }
        mixed = {
            "code": "2", "msg": "Bulk operation partially succeeded",
            "data": [
                {"ordId": "A", "clOrdId": "a", "sCode": "0", "sMsg": ""},
                {"ordId": "B", "clOrdId": "b", "sCode": "51400",
                 "sMsg": "Cancellation failed as the order does not exist",
                 "subCode": "2000"},
            ],
        }
        client = _client([pending, mixed])
        try:
            outcome = await client.cancel_all("BTC-USDT-SWAP")
        finally:
            await client.close()

        assert outcome["cancelled"] == 1, "only the sCode=0 item was actually cancelled"
        assert len(outcome["failed"]) == 1
        assert "51400" not in outcome["failed"][0]  # the message, not the code
        assert "does not exist" in outcome["failed"][0]
        assert "B" in outcome["failed"][0]

    async def test_a_single_order_partial_envelope_is_rejected_on_its_item(self):
        client = _client(
            {"code": "2", "msg": "Bulk operation partially succeeded",
             "data": [{"ordId": "", "clOrdId": "c1", "sCode": "51000",
                       "sMsg": "Parameter sz error", "subCode": "1000"}]}
        )
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order("c1"))
        finally:
            await client.close()
        assert exc.value.s_code == 51000

    async def test_a_batch_with_no_usable_data_counts_as_failed_not_cancelled(self):
        pending = {
            "code": "0", "msg": "",
            "data": [
                {"instId": "BTC-USDT-SWAP", "ordId": "A", "clOrdId": "a", "state": "live",
                 "side": "buy", "ordType": "limit", "px": "50000", "sz": "1", "accFillSz": "0",
                 "posSide": "net", "cTime": "1700000000000"},
            ],
        }
        empty = {"code": "1", "msg": "All operations failed", "data": []}
        client = _client([pending, empty])
        try:
            outcome = await client.cancel_all("BTC-USDT-SWAP")
        finally:
            await client.close()
        assert outcome["cancelled"] == 0
        assert len(outcome["failed"]) == 1

    async def test_cancel_all_still_propagates_authentication_errors(self):
        """Folding a failed batch into the tally must not swallow a real error."""
        pending = {
            "code": "0", "msg": "",
            "data": [
                {"instId": "BTC-USDT-SWAP", "ordId": "A", "clOrdId": "a", "state": "live",
                 "side": "buy", "ordType": "limit", "px": "50000", "sz": "1", "accFillSz": "0",
                 "posSide": "net", "cTime": "1700000000000"},
            ],
        }
        denied = {"code": "50113", "msg": "Invalid signature", "data": []}
        client = _client([pending, denied])
        try:
            with pytest.raises(ApiError) as exc:
                await client.cancel_all("BTC-USDT-SWAP")
        finally:
            await client.close()
        assert exc.value.ret_code == 50113


class TestCancelOrder:
    async def test_cancel_surfaces_the_item_rejection(self):
        client = _client(
            {"code": "1", "msg": "All operations failed",
             "data": [{"ordId": "X", "clOrdId": "", "sCode": "51400",
                       "sMsg": "Cancellation failed as the order does not exist"}]}
        )
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.cancel_order("BTC-USDT-SWAP", order_id="X")
        finally:
            await client.close()
        assert exc.value.s_code == 51400
        assert exc.value.endpoint == Paths.CANCEL_ORDER

    async def test_a_clean_cancel_returns_the_item(self):
        client = _client({"code": "0", "msg": "",
                          "data": [{"ordId": "X", "clOrdId": "c", "sCode": "0", "sMsg": ""}]})
        try:
            item = await client.cancel_order("BTC-USDT-SWAP", order_id="X")
        finally:
            await client.close()
        assert item["ordId"] == "X"


class TestTopLevelErrorsStillFailImmediately:
    """Requirement 4: only order endpoints, only codes 1 and 2, only with items."""

    @pytest.mark.parametrize(
        ("code", "msg"),
        [
            (50111, "Invalid OK-ACCESS-KEY"),
            (50113, "Invalid signature"),
            (50102, "Timestamp request expired"),
            (50101, "APIKey does not match current environment"),
            (50119, "API key doesn't exist"),
            (50100, "API frozen, please contact customer service"),
        ],
    )
    async def test_authentication_and_environment_errors_raise_at_the_envelope(
        self, code, msg
    ):
        """These never reach a per-item parser, even with a data array present."""
        client = _client(
            {"code": str(code), "msg": msg,
             "data": [{"sCode": "0", "sMsg": "", "ordId": "should-be-ignored"}]}
        )
        try:
            with pytest.raises(ApiError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()
        assert exc.value.ret_code == code
        assert not isinstance(exc.value, OrderRejectedError)

    async def test_rate_limits_are_not_swallowed(self):
        from btcbot.utils.errors import RateLimitError

        client = _client({"code": "0", "msg": "", "data": []}, status=429)
        try:
            with pytest.raises(RateLimitError):
                await client.place_order(_order())
        finally:
            await client.close()

    async def test_a_malformed_response_body_is_a_transport_error(self):
        from btcbot.utils.errors import TransportError

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"<html>not json</html>")

        client = OkxDemoClient(
            api_key=API_KEY, api_secret=API_SECRET, passphrase=PASSPHRASE, max_retries=0
        )
        client._client = httpx.AsyncClient(   # noqa: SLF001 - transport seam
            base_url=client.base_url, transport=httpx.MockTransport(handler)
        )
        try:
            with pytest.raises((TransportError, ApiError, ValueError)):
                await client.place_order(_order())
        finally:
            await client.close()

    @pytest.mark.parametrize(
        "path",
        [Paths.BALANCE, Paths.POSITIONS, Paths.SET_LEVERAGE, Paths.ACCOUNT_CONFIG],
    )
    async def test_non_order_endpoints_cannot_opt_into_item_level_errors(self, path):
        """A programming guard: the relaxation is structurally order-only."""
        client = _client({"code": "0", "msg": "", "data": []})
        try:
            with pytest.raises(ValueError) as exc:
                await client._request(   # noqa: SLF001 - the guard itself
                    "GET", path, authenticated=True, item_level_errors=True
                )
        finally:
            await client.close()
        assert "not permitted" in str(exc.value)

    async def test_code_one_on_a_non_order_endpoint_still_raises(self):
        """Nothing globally treats 1 as success — only opted-in order paths."""
        client = _client(
            {"code": "1", "msg": "All operations failed",
             "data": [{"sCode": "51000", "sMsg": "detail"}]}
        )
        try:
            with pytest.raises(ApiError) as exc:
                await client.get_wallet_balance()
        finally:
            await client.close()
        assert exc.value.ret_code == 1


class TestOrderDetailsLookup:
    """GET /api/v5/trade/order — the authority reconciliation polls."""

    async def test_it_queries_by_instid_and_ordid(self):
        client = _client(
            {"code": "0", "msg": "", "data": [{
                "instId": "BTC-USDT-SWAP", "ordId": "3785756861732241408",
                "clOrdId": "smoke8b811244a1cc40d7ab27ca33", "state": "filled",
                "side": "buy", "posSide": "net", "ordType": "market", "sz": "0.01",
                "accFillSz": "0.01", "avgPx": "118000.1", "fillPx": "118000.1",
                "fillSz": "0.01", "fee": "-0.0708", "feeCcy": "USDT", "lever": "1",
                "cTime": "1700000000000", "uTime": "1700000000450"}]}
        )
        try:
            details = await client.get_order(
                "BTC-USDT-SWAP", order_id="3785756861732241408"
            )
            query = client.requests[-1].url.params   # type: ignore[attr-defined]
        finally:
            await client.close()

        assert query["instId"] == "BTC-USDT-SWAP"
        assert query["ordId"] == "3785756861732241408"
        assert "clOrdId" not in query, "ordId is available — clOrdId must not be sent too"
        assert details is not None
        assert details.is_filled
        assert details.proves_fill
        assert details.fee == pytest.approx(0.0708)

    async def test_clordid_is_used_only_when_no_ordid_is_known(self):
        client = _client({"code": "0", "msg": "", "data": []})
        try:
            await client.get_order("BTC-USDT-SWAP", client_order_id="smoke8b")
            query = client.requests[-1].url.params   # type: ignore[attr-defined]
        finally:
            await client.close()
        assert query["clOrdId"] == "smoke8b"
        assert "ordId" not in query

    async def test_an_unknown_order_returns_none_rather_than_raising(self):
        """51603 is a real answer during reconciliation, not a fault."""
        client = _client({"code": "51603", "msg": "Order does not exist", "data": []})
        try:
            assert await client.get_order("BTC-USDT-SWAP", order_id="nope") is None
        finally:
            await client.close()

    async def test_other_errors_still_raise(self):
        """'Not found' must never mask an auth or parameter failure."""
        client = _client({"code": "50113", "msg": "Invalid signature", "data": []})
        try:
            with pytest.raises(ApiError) as exc:
                await client.get_order("BTC-USDT-SWAP", order_id="x")
        finally:
            await client.close()
        assert exc.value.ret_code == 50113

    async def test_an_identifier_is_required(self):
        client = _client({"code": "0", "msg": "", "data": []})
        try:
            with pytest.raises(ApiError):
                await client.get_order("BTC-USDT-SWAP")
        finally:
            await client.close()

    async def test_an_empty_data_array_returns_none(self):
        client = _client({"code": "0", "msg": "", "data": []})
        try:
            assert await client.get_order("BTC-USDT-SWAP", order_id="x") is None
        finally:
            await client.close()


class TestTheRelaxationIsNarrow:
    def test_only_trade_endpoints_are_eligible(self):
        expected = frozenset(
            {
                Paths.ORDER,
                Paths.CANCEL_ORDER,
                Paths.CANCEL_BATCH_ORDERS,
                Paths.CLOSE_POSITION,
            }
        )
        assert expected == ORDER_OPERATION_PATHS
        for path in ORDER_OPERATION_PATHS:
            assert path.startswith("/api/v5/trade/")

    def test_only_codes_one_and_two_are_deferred_to_the_item_parser(self):
        assert frozenset({1, 2}) == ITEM_LEVEL_ENVELOPE_CODES
        assert 0 not in ITEM_LEVEL_ENVELOPE_CODES, "code 0 is handled as success, not deferred"
        for auth_code in (50101, 50111, 50113, 50119):
            assert auth_code not in ITEM_LEVEL_ENVELOPE_CODES


class TestNoCredentialLeakage:
    """Requirement 6: a rejection diagnostic must carry response data only."""

    async def test_no_credentials_appear_in_logs_when_an_order_is_rejected(self, caplog):
        client = _client(ALL_OPERATIONS_FAILED)
        caplog.set_level(logging.DEBUG)
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()

        signed = client.requests[-1]          # type: ignore[attr-defined]
        signature = signed.headers["OK-ACCESS-SIGN"]
        secrets = (API_KEY, API_SECRET, PASSPHRASE, signature)

        haystack = "\n".join(
            [caplog.text, str(exc.value), exc.value.ret_msg, *exc.value.report_lines()]
        )
        for secret in secrets:
            assert secret not in haystack, "a credential reached a log or an error message"

    async def test_the_rejection_message_carries_only_allow_listed_fields(self):
        """A future OKX field must not ride along into an operator-facing string."""
        client = _client(
            {
                "code": "1",
                "msg": "All operations failed",
                "data": [
                    {
                        "ordId": "", "clOrdId": "test-order", "sCode": "51000",
                        "sMsg": "Example detailed rejection", "subCode": "1000",
                        "unexpectedFutureField": "DO-NOT-LOG-ME",
                    }
                ],
            }
        )
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order())
        finally:
            await client.close()
        assert "DO-NOT-LOG-ME" not in str(exc.value)
        assert "unexpectedFutureField" not in str(exc.value)

    async def test_the_request_body_is_never_echoed_into_the_error(self):
        client = _client(ALL_OPERATIONS_FAILED)
        try:
            with pytest.raises(OrderRejectedError) as exc:
                await client.place_order(_order())
            body = json.loads(client.requests[-1].content)  # type: ignore[attr-defined]
        finally:
            await client.close()
        assert body["instId"] == "BTC-USDT-SWAP"      # the request really was signed
        assert "instId" not in str(exc.value)         # but it is not echoed back
