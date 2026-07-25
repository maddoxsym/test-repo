"""Bybit hosts and endpoint paths — the structural boundary of this system.

**This module is the single place where any Bybit hostname appears.** Everything
else imports from here. The safety audit (``scripts/audit_safety.sh``) enforces
that, so a future edit cannot quietly introduce a real-money host elsewhere.

Sources: ``docs/v5/demo.mdx`` and ``docs/v5/guide.mdx`` from the official Bybit
documentation repository. See ``docs/bybit_capabilities.md``.
"""

from __future__ import annotations

from types import MappingProxyType

# =====================================================================
#  AUTHENTICATED HOST ALLOW-LIST
#
#  An authenticated client can only ever be constructed against a host in
#  this frozenset. It is a module constant: not read from YAML, not read
#  from the environment, not settable by a CLI flag. Changing it requires
#  editing this file, which is exactly the level of friction real-money
#  trading should have in a demo-only research system.
#
#  Bybit documents the demo module as a single host. If Bybit ever
#  publishes an additional demo host (e.g. a genuine EEA demo endpoint),
#  add it here — and nowhere else.
# =====================================================================
DEMO_REST_HOST = "https://api-demo.bybit.com"
ALLOWED_DEMO_HOSTS: frozenset[str] = frozenset({DEMO_REST_HOST})

# Private WebSocket for the demo module. Docs: "this only supports the private
# streams". There is no public stream on the demo host.
DEMO_WS_PRIVATE = "wss://stream-demo.bybit.com/v5/private"

# Public market data. Docs: "public data is identical to that found on mainnet
# with wss://stream.bybit.com". This connection is UNAUTHENTICATED and read-only
# — it carries no credentials and cannot place an order.
PUBLIC_WS_BASE = "wss://stream.bybit.com/v5/public"
PUBLIC_REST_HOST = DEMO_REST_HOST  # market endpoints are available on demo too

_PUBLIC_WS_PATHS = MappingProxyType({"spot": "spot", "linear": "linear", "inverse": "inverse"})


def public_ws_url(category: str) -> str:
    """WebSocket URL for public market data in ``category``."""
    try:
        return f"{PUBLIC_WS_BASE}/{_PUBLIC_WS_PATHS[category]}"
    except KeyError:
        raise ValueError(f"no public websocket for category {category!r}") from None


# =====================================================================
#  MAINNET NEGATIVE CONTROL — READ ONLY, MUST FAIL
#
#  Used by exactly one function: MainnetNegativeControlProbe.check(),
#  which sends the credentials to ONE read-only endpoint and REQUIRES an
#  authentication failure. A success means the key can touch real money,
#  and the system refuses to trade.
#
#  This is a safety assertion, not a trading path. No order, transfer,
#  withdrawal, or deposit endpoint is ever combined with this host, and
#  the client class that uses it has no methods capable of doing so.
# =====================================================================
NEGATIVE_CONTROL_HOST = "https://api.bybit.com"
NEGATIVE_CONTROL_PATH = "/v5/user/query-api"


# --- endpoint paths (all verified present in the demo availability list) ---


class Paths:
    """V5 endpoint paths used by this system."""

    # Market data — docs list "Market: All / all endpoints" as demo-available.
    SERVER_TIME = "/v5/market/time"
    KLINE = "/v5/market/kline"
    INSTRUMENTS = "/v5/market/instruments-info"
    TICKERS = "/v5/market/tickers"
    ORDERBOOK = "/v5/market/orderbook"
    RECENT_TRADES = "/v5/market/recent-trade"

    # Account
    WALLET_BALANCE = "/v5/account/wallet-balance"
    ACCOUNT_INFO = "/v5/account/info"
    # Demo-only endpoint: existence is one of the demo-verification signals.
    DEMO_APPLY_MONEY = "/v5/account/demo-apply-money"

    # Trade
    ORDER_CREATE = "/v5/order/create"
    ORDER_CANCEL = "/v5/order/cancel"
    ORDER_CANCEL_ALL = "/v5/order/cancel-all"
    ORDER_REALTIME = "/v5/order/realtime"
    ORDER_HISTORY = "/v5/order/history"
    EXECUTION_LIST = "/v5/execution/list"

    # Position (only reachable when a derivatives category is enabled AND
    # confirmed tradable by runtime discovery)
    POSITION_LIST = "/v5/position/list"

    # Key metadata
    QUERY_API = "/v5/user/query-api"


# Endpoint fragments that must never appear anywhere in this codebase.
# Asserted by tests/unit/test_safety_lock.py and scripts/audit_safety.sh.
FORBIDDEN_ENDPOINT_FRAGMENTS: frozenset[str] = frozenset(
    {
        "/v5/asset/withdraw",
        "/v5/asset/transfer",
        "/v5/asset/deposit",
        "/v5/asset/create-internal-transfer",
        "/v5/asset/create-universal-transfer",
        "/v5/asset/withdraw/create",
        "/v5/asset/withdraw/cancel",
        "/v5/asset/deposit/query-address",
        "/v5/user/create-sub-member",
    }
)


def is_allowed_authenticated_host(host: str) -> bool:
    """Whether ``host`` may be used for authenticated *trading* requests."""
    return host.rstrip("/") in ALLOWED_DEMO_HOSTS
