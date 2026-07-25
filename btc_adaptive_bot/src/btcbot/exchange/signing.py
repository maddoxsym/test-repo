"""Bybit V5 HMAC request signing.

Verified against ``docs/v5/guide.mdx``:

* GET  → sign ``timestamp + api_key + recv_window + queryString``
* POST → sign ``timestamp + api_key + recv_window + jsonBodyString``
* HMAC-SHA256, lowercase hex
* Headers ``X-BAPI-API-KEY``, ``X-BAPI-TIMESTAMP``, ``X-BAPI-SIGN``,
  ``X-BAPI-RECV-WINDOW``

The exact byte sequence matters: the JSON body that is signed must be the
identical string that is transmitted, so the body is serialised once and both
the signature and the request reuse that string.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode


@dataclass(frozen=True, slots=True)
class SignedRequest:
    """A request ready to send: headers, final query string, and exact body bytes."""

    headers: dict[str, str]
    query_string: str
    body: str | None


def build_query_string(params: dict[str, Any] | None) -> str:
    """Deterministic query string, omitting ``None`` values.

    Insertion order is preserved (not sorted): the signed string must match the
    transmitted URL exactly, so both come from this one function.
    """
    if not params:
        return ""
    filtered = {k: v for k, v in params.items() if v is not None}
    if not filtered:
        return ""
    return urlencode(
        {k: ("true" if v is True else "false" if v is False else str(v)) for k, v in filtered.items()}
    )


def serialize_body(payload: dict[str, Any] | None) -> str | None:
    """Serialise a JSON body exactly as it will be transmitted."""
    if payload is None:
        return None
    return json.dumps({k: v for k, v in payload.items() if v is not None}, separators=(",", ":"))


def sign_payload(secret: str, prehash: str) -> str:
    """HMAC-SHA256 of ``prehash``, lowercase hex."""
    return hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).hexdigest()


def build_prehash(timestamp_ms: int, api_key: str, recv_window: int, payload: str) -> str:
    """The exact string Bybit requires to be signed."""
    return f"{timestamp_ms}{api_key}{recv_window}{payload}"


def sign_request(
    *,
    api_key: str,
    api_secret: str,
    method: str,
    timestamp_ms: int,
    recv_window: int,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
) -> SignedRequest:
    """Produce headers, query string, and body for an authenticated request."""
    method_upper = method.upper()
    if method_upper == "GET":
        query_string = build_query_string(params)
        payload = query_string
        body_str = None
    else:
        query_string = ""
        body_str = serialize_body(body) or "{}"
        payload = body_str

    prehash = build_prehash(timestamp_ms, api_key, recv_window, payload)
    signature = sign_payload(api_secret, prehash)

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": str(timestamp_ms),
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": str(recv_window),
    }
    if method_upper != "GET":
        headers["Content-Type"] = "application/json"

    return SignedRequest(headers=headers, query_string=query_string, body=body_str)


def sign_ws_auth(api_key: str, api_secret: str, expires_ms: int) -> list[Any]:
    """Build the private-WebSocket ``auth`` arguments.

    Bybit's private WS handshake signs the literal string ``GET/realtime{expires}``.
    """
    signature = sign_payload(api_secret, f"GET/realtime{expires_ms}")
    return [api_key, expires_ms, signature]
