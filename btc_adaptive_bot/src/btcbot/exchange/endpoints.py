"""OKX hosts, endpoint paths, and the demo-environment switch — the structural
boundary of this system.

**This module is the single place where any OKX hostname appears.** Everything
else imports from here. The safety audit (``scripts/audit_safety.sh``) enforces
that, so a future edit cannot quietly introduce a live-trading host elsewhere.

OKX structures its environments differently from a host-per-environment
exchange, and both mechanisms are enforced here:

* **REST**: demo and live share the EEA host ``eea.okx.com``; the environment
  is selected per-request by the ``x-simulated-trading: 1`` header. The header
  is therefore injected by the transport layer for *every* request (see
  ``rest.py``) — there is no code path that builds authenticated headers
  without it.
* **WebSocket**: demo and live use *different* hosts (``wseeapap`` vs
  ``wseea`` — one dropped infix apart), so WS URLs are checked against an
  exact-string allow-list, never a substring match.

Sources: the official ``okxapi/python-okx`` SDK (endpoint paths, signing) and
the maintained ``tiagosiebler/okx-api`` SDK (EEA host matrix), corroborated by
web search. The primary docs site was unreachable from the build environment;
see ``docs/okx_demo_capabilities.md`` §0 for the full sourcing note.
"""

from __future__ import annotations

# =====================================================================
#  DEMO ENVIRONMENT SWITCH
#
#  Every REST request — authenticated or public — carries this header.
#  It is a module constant consumed by exactly one header builder in
#  rest.py; it is not configurable and cannot be turned off.
# =====================================================================
SIMULATED_TRADING_HEADER = "x-simulated-trading"
SIMULATED_TRADING_VALUE = "1"

# =====================================================================
#  AUTHENTICATED HOST ALLOW-LIST
#
#  An authenticated client can only ever be constructed against a host in
#  this frozenset. It is a module constant: not read from YAML, not read
#  from the environment, not settable by a CLI flag. Changing it requires
#  editing this file, which is exactly the level of friction live trading
#  should have in a demo-only research system.
#
#  The user's account is an OKX Europe (EEA entity) account, so the EEA
#  host is the only member. If OKX ever publishes an additional EEA API
#  host, add it here — and nowhere else.
# =====================================================================
DEMO_REST_HOST = "https://eea.okx.com"
ALLOWED_DEMO_HOSTS: frozenset[str] = frozenset({DEMO_REST_HOST})

# EEA *demo* WebSocket endpoints. The ``pap`` infix marks the demo variant;
# the business endpoint carries the brokerId query used by the demo service.
# Candlestick channels live on the *business* endpoint in API v5.
DEMO_WS_PUBLIC = "wss://wseeapap.okx.com:8443/ws/v5/public"
DEMO_WS_PRIVATE = "wss://wseeapap.okx.com:8443/ws/v5/private"
DEMO_WS_BUSINESS = "wss://wseeapap.okx.com:8443/ws/v5/business?brokerId=9999"

ALLOWED_WS_URLS: frozenset[str] = frozenset(
    {DEMO_WS_PUBLIC, DEMO_WS_PRIVATE, DEMO_WS_BUSINESS}
)

# =====================================================================
#  FORBIDDEN HOSTS
#
#  Hosts that exist at OKX but must never be contacted by this system.
#  Listed explicitly so tests can assert each one is rejected — the EEA
#  *live* WS host differs from the demo host by a single dropped "pap"
#  infix, which is precisely why matching is exact, never substring.
# =====================================================================
FORBIDDEN_HOSTS: frozenset[str] = frozenset(
    {
        "https://www.okx.com",        # OKX Global live REST
        "https://us.okx.com",         # OKX US REST
        "https://openapi.okx.com",    # OKX OpenAPI entity
        "wss://wseea.okx.com:8443/ws/v5/public",     # EEA LIVE WS
        "wss://wseea.okx.com:8443/ws/v5/private",    # EEA LIVE WS
        "wss://wseea.okx.com:8443/ws/v5/business",   # EEA LIVE WS
        "wss://ws.okx.com:8443/ws/v5/public",        # Global live WS
        "wss://ws.okx.com:8443/ws/v5/private",       # Global live WS
        "wss://wspap.okx.com:8443/ws/v5/public",     # Global demo WS (wrong entity)
        "wss://wspap.okx.com:8443/ws/v5/private",    # Global demo WS (wrong entity)
        "wss://wsuspap.okx.com:8443/ws/v5/private",  # US demo WS (wrong entity)
    }
)


def is_allowed_authenticated_host(host: str) -> bool:
    """Whether ``host`` may be used for authenticated *trading* requests."""
    return host.rstrip("/") in ALLOWED_DEMO_HOSTS


def is_allowed_ws_url(url: str) -> bool:
    """Exact-match check against the EEA demo WebSocket allow-list."""
    return url in ALLOWED_WS_URLS


# =====================================================================
#  LIVE-ENVIRONMENT NEGATIVE CONTROL — READ ONLY, MUST FAIL
#
#  Used by exactly one class: LiveEnvironmentNegativeControlProbe, which
#  sends the credentials ONCE, read-only, to the EEA host WITHOUT the
#  x-simulated-trading header and REQUIRES an environment-mismatch
#  rejection (OKX error 50101). A success means the key can act on the
#  live environment, and the system refuses to trade with it.
#
#  This is a safety assertion, not a trading path. The probe class has no
#  order methods and can only issue this one GET.
# =====================================================================
NEGATIVE_CONTROL_PATH = "/api/v5/account/config"
# "APIKey does not match current environment" — the expected, safe outcome.
ENVIRONMENT_MISMATCH_CODE = 50101


# --- endpoint paths (verbatim from the official SDK's consts.py) ---------


class Paths:
    """OKX API v5 endpoint paths used by this system."""

    # Public
    SERVER_TIME = "/api/v5/public/time"
    INSTRUMENTS = "/api/v5/public/instruments"
    FUNDING_RATE = "/api/v5/public/funding-rate"
    FUNDING_RATE_HISTORY = "/api/v5/public/funding-rate-history"
    MARK_PRICE = "/api/v5/public/mark-price"
    POSITION_TIERS = "/api/v5/public/position-tiers"

    # Market data
    CANDLES = "/api/v5/market/candles"
    HISTORY_CANDLES = "/api/v5/market/history-candles"
    TICKER = "/api/v5/market/ticker"
    ORDERBOOK = "/api/v5/market/books"
    RECENT_TRADES = "/api/v5/market/trades"

    # Account (authenticated)
    ACCOUNT_CONFIG = "/api/v5/account/config"
    BALANCE = "/api/v5/account/balance"
    POSITIONS = "/api/v5/account/positions"
    POSITIONS_HISTORY = "/api/v5/account/positions-history"
    SET_LEVERAGE = "/api/v5/account/set-leverage"
    LEVERAGE_INFO = "/api/v5/account/leverage-info"
    MAX_SIZE = "/api/v5/account/max-size"
    TRADE_FEE = "/api/v5/account/trade-fee"
    BILLS = "/api/v5/account/bills"

    # Trade (authenticated)
    ORDER = "/api/v5/trade/order"
    CANCEL_ORDER = "/api/v5/trade/cancel-order"
    CANCEL_BATCH_ORDERS = "/api/v5/trade/cancel-batch-orders"
    ORDERS_PENDING = "/api/v5/trade/orders-pending"
    ORDERS_HISTORY = "/api/v5/trade/orders-history"
    FILLS = "/api/v5/trade/fills"
    CLOSE_POSITION = "/api/v5/trade/close-position"


# Endpoint fragments that must never appear anywhere in this codebase.
# Asserted by tests/unit/test_safety_lock.py and scripts/audit_safety.sh.
# These are the fund-movement and account-administration surfaces: a demo
# research system has no business holding code that could move real assets.
FORBIDDEN_ENDPOINT_FRAGMENTS: frozenset[str] = frozenset(
    {
        "/api/v5/asset/withdrawal",
        "/api/v5/asset/transfer",
        "/api/v5/asset/deposit-address",
        "/api/v5/asset/deposit-lightning",
        "/api/v5/asset/withdrawal-lightning",
        "/api/v5/asset/convert",
        "/api/v5/asset/subaccount/transfer",
        "/api/v5/users/subaccount",
        "/api/v5/account/borrow-repay",
        "/api/v5/finance/",
    }
)


# --- timeframe translation ------------------------------------------------
#
# The system's internal timeframe notation (minutes as strings, "D"/"W") is
# exchange-independent and used by strategies, config, and the database. The
# OKX ``bar`` notation is a transport detail, translated only here.
#
# Daily/weekly bars use OKX's explicit UTC-aligned variants ("1Dutc"):
# the plain "1D" opens on UTC+8 boundaries, and everything in this system
# is UTC.

_INTERNAL_TO_OKX_BAR: dict[str, str] = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1H",
    "120": "2H",
    "240": "4H",
    "360": "6H",
    "720": "12H",
    "D": "1Dutc",
    "W": "1Wutc",
}

_OKX_BAR_TO_INTERNAL: dict[str, str] = {v: k for k, v in _INTERNAL_TO_OKX_BAR.items()}


def to_okx_bar(interval: str) -> str:
    """Translate an internal timeframe string to the OKX ``bar`` parameter."""
    try:
        return _INTERNAL_TO_OKX_BAR[interval]
    except KeyError:
        raise ValueError(
            f"no OKX bar mapping for internal timeframe {interval!r}; "
            f"supported: {sorted(_INTERNAL_TO_OKX_BAR)}"
        ) from None


def from_okx_bar(bar: str) -> str:
    """Translate an OKX ``bar`` string back to the internal timeframe notation."""
    try:
        return _OKX_BAR_TO_INTERNAL[bar]
    except KeyError:
        raise ValueError(f"unrecognised OKX bar {bar!r}") from None


def candle_channel(interval: str) -> str:
    """The OKX WS candlestick channel name for an internal timeframe."""
    return f"candle{to_okx_bar(interval)}"
