"""Error taxonomy.

Deliberately specific: the orchestrator reacts differently to a stale feed than
to a rejected order than to a failed demo verification. Broad ``except
Exception`` blocks are avoided in favour of catching these.
"""

from __future__ import annotations


class BtcBotError(Exception):
    """Base class for every error this system raises deliberately."""


# --- configuration -------------------------------------------------------


class ConfigError(BtcBotError):
    """Configuration missing, malformed, or semantically invalid."""


class CredentialsMissingError(BtcBotError):
    """Demo API credentials were not supplied in the environment."""


# --- safety --------------------------------------------------------------


class SafetyError(BtcBotError):
    """Base class for safety-system refusals."""


class MainnetRejectedError(SafetyError):
    """A non-demo host was supplied to an authenticated client.

    Raised at construction time, before any network call can happen. This is the
    structural guarantee that real-money trading is unreachable.
    """


class DemoVerificationError(SafetyError):
    """The Bybit demo environment could not be positively verified."""


class SafeModeError(SafetyError):
    """An action was attempted while the system is in SAFE_MODE."""


class CircuitBreakerError(SafetyError):
    """A circuit breaker rejected an action."""


# --- exchange ------------------------------------------------------------


class ExchangeError(BtcBotError):
    """Base class for exchange interaction failures."""


class ApiError(ExchangeError):
    """Bybit returned a non-zero ``retCode``."""

    def __init__(self, ret_code: int, ret_msg: str, endpoint: str = "") -> None:
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint
        super().__init__(f"bybit retCode={ret_code} retMsg={ret_msg!r} endpoint={endpoint}")


class RateLimitError(ExchangeError):
    """Rate limited by the exchange; the caller should back off."""


class TransportError(ExchangeError):
    """Network/transport failure talking to the exchange."""


class InstrumentNotFoundError(ExchangeError):
    """The configured symbol is not tradable in any enabled category."""


class UnsupportedOperationError(ExchangeError):
    """The connected environment does not support the requested operation.

    Raised, for example, when a SHORT order is routed while only ``spot`` is
    available. Not a bug — an expected, journaled outcome.
    """


# --- market data ---------------------------------------------------------


class MarketDataError(BtcBotError):
    """Base class for market-data problems."""


class StaleDataError(MarketDataError):
    """A stream exceeded its staleness budget; trading must pause."""


class LookAheadError(MarketDataError):
    """Code attempted to read a candle at or beyond the replay cursor.

    Only ever raised by a bug. Surfacing it loudly is the entire point.
    """


# --- execution -----------------------------------------------------------


class ExecutionError(BtcBotError):
    """Base class for order-path failures."""


class DuplicateOrderError(ExecutionError):
    """An order for this setup is already live or already recorded."""


class PositionSizingError(ExecutionError):
    """Position size could not be computed safely; the trade must not be sent."""


class OrderValidationError(ExecutionError):
    """An order failed pre-submission validation."""


# --- persistence ---------------------------------------------------------


class DatabaseError(BtcBotError):
    """Database operation failed."""


class MigrationError(DatabaseError):
    """Schema migration failed."""


# --- experiment ----------------------------------------------------------


class ExperimentError(BtcBotError):
    """Experiment lifecycle violation."""
