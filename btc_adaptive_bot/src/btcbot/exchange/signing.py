"""OKX API v5 request signing.

Verified against the official ``okxapi/python-okx`` SDK (``okx/utils.py``):

* Pre-hash: ``timestamp + METHOD.upper() + request_path + body`` where
  ``request_path`` **includes the query string** and ``body`` is the exact
  transmitted JSON (empty string for GET / body-less requests).
* HMAC-SHA256 → **base64** (not hex).
* REST timestamp: ISO-8601 UTC with milliseconds, e.g.
  ``2026-07-28T17:04:05.123Z``.
* Headers: ``OK-ACCESS-KEY``, ``OK-ACCESS-SIGN``, ``OK-ACCESS-TIMESTAMP``,
  ``OK-ACCESS-PASSPHRASE``, plus ``Content-Type: application/json``.
* WebSocket login differs in one respect: it signs
  ``timestamp + 'GET' + '/users/self/verify'`` with the timestamp in Unix
  epoch **seconds** (as a string), not the ISO form.

The exact byte sequence matters: the JSON body that is signed must be the
identical string that is transmitted, so the body is serialised once and both
the signature and the request reuse that string.

The ``x-simulated-trading`` demo header is **not** added here — it is injected
by the transport layer in ``rest.py`` so that the demo switch lives in exactly
one place and applies to every request unconditionally.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

WS_LOGIN_SIGN_PATH = "/users/self/verify"


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """A request ready to send: headers, final query string, and exact body bytes."""

    headers: dict[str, str]
    query_string: str
    body: str | None


def okx_timestamp(*, now_ms_value: int) -> str:
    """The ISO-8601 millisecond UTC timestamp OKX requires, from an epoch-ms value.

    Taking the time as an argument (rather than reading the clock here) lets the
    client apply its measured server-clock offset, and makes signing fully
    deterministic under test.
    """
    dt = datetime.fromtimestamp(now_ms_value / 1000.0, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def build_query_string(params: dict[str, Any] | None) -> str:
    """Deterministic query string, omitting ``None`` values.

    Insertion order is preserved (not sorted): the signed request path must
    match the transmitted URL exactly, so both come from this one function.
    """
    if not params:
        return ""
    filtered = {k: v for k, v in params.items() if v is not None}
    if not filtered:
        return ""
    return urlencode(
        {k: ("true" if v is True else "false" if v is False else str(v)) for k, v in filtered.items()}
    )


def serialize_body(payload: dict[str, Any] | list[Any] | None) -> str | None:
    """Serialise a JSON body exactly as it will be transmitted."""
    if payload is None:
        return None
    if isinstance(payload, list):
        return json.dumps(payload, separators=(",", ":"))
    return json.dumps({k: v for k, v in payload.items() if v is not None}, separators=(",", ":"))


def sign_payload(secret: str, prehash: str) -> str:
    """HMAC-SHA256 of ``prehash``, base64-encoded (OKX format)."""
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def build_prehash(timestamp: str, method: str, request_path: str, body: str) -> str:
    """The exact string OKX requires to be signed.

    ``request_path`` must include the query string; ``body`` is ``""`` when the
    request has no body.
    """
    return f"{timestamp}{method.upper()}{request_path}{body}"


def sign_request(
    *,
    api_key: str,
    api_secret: str,
    passphrase: str,
    method: str,
    path: str,
    timestamp: str,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> SignedRequest:
    """Produce headers, query string, and body for an authenticated request."""
    method_upper = method.upper()
    query_string = build_query_string(params)
    request_path = f"{path}?{query_string}" if query_string else path

    body_str = serialize_body(body)
    signed_body = body_str if body_str is not None else ""

    prehash = build_prehash(timestamp, method_upper, request_path, signed_body)
    signature = sign_payload(api_secret, prehash)

    headers = {
        "OK-ACCESS-KEY": api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": passphrase,
        "Content-Type": "application/json",
    }
    return SignedRequest(headers=headers, query_string=query_string, body=body_str)


def sign_ws_login(
    api_key: str, api_secret: str, passphrase: str, *, epoch_seconds: int
) -> dict[str, str]:
    """Build one entry of the private-WebSocket ``login`` args.

    The WS handshake signs ``{epoch_seconds}GET/users/self/verify`` — epoch
    seconds as a plain string, unlike the ISO timestamp REST uses.
    """
    timestamp = str(epoch_seconds)
    signature = sign_payload(api_secret, f"{timestamp}GET{WS_LOGIN_SIGN_PATH}")
    return {
        "apiKey": api_key,
        "passphrase": passphrase,
        "timestamp": timestamp,
        "sign": signature,
    }
