"""OKX hosts, endpoint paths, and the demo-environment switch — the structural
boundary of this system.

**This module is the single place where any OKX hostname appears.** Everything
else imports from here. The safety audit (``scripts/audit_safety.sh``) enforces
that, so a future edit cannot quietly introduce a live-trading host elsewhere.

Regions
-------

OKX operates several entities, and an API key belongs to exactly one of them.
A key created on a Global/UAE account does not exist on the EEA entity — the
exchange answers ``50119 API key doesn't exist`` — so the region is a
first-class, configurable property rather than a hardcoded constant.

Each region is described by a :class:`DemoProfile` holding **only** demo
endpoints. There is no live profile anywhere in this module; live hosts appear
solely in :data:`FORBIDDEN_WS_URLS`, which exists so they can be rejected.

Two enforcement mechanisms, because OKX uses two
------------------------------------------------

* **REST**: demo and live share the same host *in every region*; the
  environment is selected per-request by the ``x-simulated-trading: 1``
  header. The header is therefore injected by the transport layer for *every*
  request (see ``rest.py``) — there is no code path that builds authenticated
  headers without it, and no configuration that can switch it off.
* **WebSocket**: demo and live use *different* hosts (``wspap`` vs ``ws``,
  ``wseeapap`` vs ``wseea``, ``wsuspap`` vs ``wsus``) — a single infix apart —
  so WS URLs are checked against an exact-string allow-list, never a substring
  match.

Sources: the official ``okxapi/python-okx`` SDK (endpoint paths, signing,
``API_URL = 'https://www.okx.com'``) and the maintained
``tiagosiebler/okx-api`` SDK (the per-region host matrix), corroborated by web
search. The primary docs site was unreachable from the build environment; see
``docs/okx_demo_capabilities.md`` §0 for the full sourcing note.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType

# =====================================================================
#  DEMO ENVIRONMENT SWITCH
#
#  Every REST request — authenticated or public, in every region —
#  carries this header. It is a module constant consumed by exactly one
#  header builder in rest.py; it is not configurable and cannot be
#  turned off.
# =====================================================================
SIMULATED_TRADING_HEADER = "x-simulated-trading"
SIMULATED_TRADING_VALUE = "1"


@dataclass(frozen=True, slots=True)
class DemoProfile:
    """The demo endpoints for one OKX entity. Demo only — by construction.

    ``alt_rest_hosts`` holds additional REST hosts the same entity serves.
    OKX Global is reachable both at ``www.okx.com`` (the official SDK's
    primary) and ``openapi.okx.com`` (the alternative endpoint); either works
    with a Global key, so both are accepted and the operator can pick.
    """

    region: str
    label: str
    rest_host: str
    ws_public: str
    ws_private: str
    ws_business: str
    alt_rest_hosts: frozenset[str] = field(default_factory=frozenset)

    @property
    def rest_hosts(self) -> frozenset[str]:
        return frozenset({self.rest_host}) | self.alt_rest_hosts

    @property
    def ws_urls(self) -> tuple[str, str, str]:
        return (self.ws_public, self.ws_private, self.ws_business)

    def describe(self) -> str:
        return f"{self.label} ({self.region}) — REST {self.rest_host}"


# =====================================================================
#  DEMO PROFILE REGISTRY
#
#  A compile-time table. Not read from YAML, not extendable at runtime:
#  the *selection* is configurable, the *contents* are not. Adding an
#  entity requires editing this file, which is the level of friction a
#  demo-only research system should have.
#
#  Every URL below is a DEMO endpoint. No live endpoint appears here.
# =====================================================================

GLOBAL_DEMO = DemoProfile(
    region="global",
    label="OKX Global / UAE Demo",
    # The user-facing default. OKX Global serves the same API from
    # www.okx.com; both are accepted (see alt_rest_hosts).
    rest_host="https://openapi.okx.com",
    alt_rest_hosts=frozenset({"https://www.okx.com"}),
    ws_public="wss://wspap.okx.com:8443/ws/v5/public",
    ws_private="wss://wspap.okx.com:8443/ws/v5/private",
    # The maintained SDK appends ?brokerId=9999 to every demo business URL;
    # the plain form is also accepted so either documented spelling works.
    ws_business="wss://wspap.okx.com:8443/ws/v5/business?brokerId=9999",
)

EEA_DEMO = DemoProfile(
    region="eea",
    label="OKX Europe (EEA) Demo",
    rest_host="https://eea.okx.com",
    ws_public="wss://wseeapap.okx.com:8443/ws/v5/public",
    ws_private="wss://wseeapap.okx.com:8443/ws/v5/private",
    ws_business="wss://wseeapap.okx.com:8443/ws/v5/business?brokerId=9999",
)

US_DEMO = DemoProfile(
    region="us",
    label="OKX US Demo",
    rest_host="https://us.okx.com",
    ws_public="wss://wsuspap.okx.com:8443/ws/v5/public",
    ws_private="wss://wsuspap.okx.com:8443/ws/v5/private",
    ws_business="wss://wsuspap.okx.com:8443/ws/v5/business?brokerId=9999",
)

DEMO_PROFILES: MappingProxyType[str, DemoProfile] = MappingProxyType(
    {profile.region: profile for profile in (GLOBAL_DEMO, EEA_DEMO, US_DEMO)}
)

#: The region used when configuration does not say otherwise.
DEFAULT_REGION = "global"
DEFAULT_PROFILE = DEMO_PROFILES[DEFAULT_REGION]

#: Kept as a module constant for the many call sites that just want a sane
#: default host; the active profile's host is what the engine actually uses.
DEMO_REST_HOST = DEFAULT_PROFILE.rest_host


def profile_for(region: str) -> DemoProfile:
    """The demo profile for ``region``. Unknown regions fail loudly."""
    try:
        return DEMO_PROFILES[region.strip().lower()]
    except KeyError:
        raise ValueError(
            f"unknown OKX region {region!r}; valid regions: {sorted(DEMO_PROFILES)}"
        ) from None


# =====================================================================
#  AUTHENTICATED HOST ALLOW-LIST
#
#  An authenticated client can only ever be constructed against a host
#  in this frozenset. It is derived from the compile-time profile
#  registry: not read from YAML, not read from the environment, not
#  settable by a CLI flag.
#
#  Note what this does and does not prove. In every OKX region the demo
#  and live environments share a REST host, so membership here does NOT
#  mean "this host cannot trade real money" — it means "this host is an
#  OKX API host we recognise". What keeps the system on demo is the
#  unconditional x-simulated-trading header plus the negative control
#  that must fail without it. The WS allow-list below is the one that
#  genuinely separates environments by host.
# =====================================================================
ALLOWED_DEMO_HOSTS: frozenset[str] = frozenset(
    host for profile in DEMO_PROFILES.values() for host in profile.rest_hosts
)


def _ws_variants(url: str) -> set[str]:
    """Both spellings of a demo WS URL: with and without the brokerId query."""
    base = url.split("?", 1)[0]
    return {url, base, f"{base}?brokerId=9999"}


ALLOWED_WS_URLS: frozenset[str] = frozenset(
    variant
    for profile in DEMO_PROFILES.values()
    for url in profile.ws_urls
    for variant in _ws_variants(url)
)

# =====================================================================
#  FORBIDDEN LIVE WEBSOCKET HOSTS
#
#  These are the *live* streams for each entity. They differ from their
#  demo counterparts by a single infix, which is precisely why matching
#  is exact rather than substring-based. Listed explicitly so tests can
#  assert each one is rejected.
#
#  There is deliberately no equivalent REST list: REST hosts are shared
#  between demo and live, so a REST deny-list would give false comfort.
#  The header is what protects REST.
# =====================================================================
FORBIDDEN_WS_URLS: frozenset[str] = frozenset(
    {
        "wss://ws.okx.com:8443/ws/v5/public",        # Global LIVE
        "wss://ws.okx.com:8443/ws/v5/private",       # Global LIVE
        "wss://ws.okx.com:8443/ws/v5/business",      # Global LIVE
        "wss://wseea.okx.com:8443/ws/v5/public",     # EEA LIVE
        "wss://wseea.okx.com:8443/ws/v5/private",    # EEA LIVE
        "wss://wseea.okx.com:8443/ws/v5/business",   # EEA LIVE
        "wss://wsus.okx.com:8443/ws/v5/public",      # US LIVE
        "wss://wsus.okx.com:8443/ws/v5/private",     # US LIVE
        "wss://wsus.okx.com:8443/ws/v5/business",    # US LIVE
    }
)


def is_allowed_authenticated_host(host: str) -> bool:
    """Whether ``host`` may be used for authenticated *trading* requests."""
    return host.rstrip("/") in ALLOWED_DEMO_HOSTS


def is_allowed_ws_url(url: str) -> bool:
    """Exact-match check against the demo WebSocket allow-list."""
    return url in ALLOWED_WS_URLS


# =====================================================================
#  LIVE-ENVIRONMENT NEGATIVE CONTROL — READ ONLY, MUST FAIL
#
#  Used by exactly one class: LiveEnvironmentNegativeControlProbe, which
#  sends the credentials ONCE, read-only, to the active region's host
#  WITHOUT the x-simulated-trading header and REQUIRES an
#  environment-mismatch rejection (OKX error 50101). A success means the
#  key can act on the live environment, and the system refuses to trade
#  with it.
#
#  This is a safety assertion, not a trading path. The probe class has no
#  order methods and can only issue this one GET.
# =====================================================================
NEGATIVE_CONTROL_PATH = "/api/v5/account/config"
#: "APIKey does not match current environment" — the expected, safe outcome.
ENVIRONMENT_MISMATCH_CODE = 50101
#: "API key doesn't exist" — in the negative control this is equally safe, but
#: on an *authenticated demo* call it means the key belongs to another region.
KEY_NOT_FOUND_CODE = 50119


# --- endpoint paths (verbatim from the official SDK's consts.py) ---------


class Paths:
    """OKX API v5 endpoint paths used by this system.

    Identical across regions — only the host differs.
    """

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
