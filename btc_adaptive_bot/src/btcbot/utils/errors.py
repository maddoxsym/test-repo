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
    """A non-demo host or live-environment endpoint was supplied to a client.

    Raised at construction time, before any network call can happen. This is the
    structural guarantee that real-money (live-environment) trading is
    unreachable.
    """


class DemoVerificationError(SafetyError):
    """The OKX demo environment could not be positively verified."""


class SafeModeError(SafetyError):
    """An action was attempted while the system is in SAFE_MODE."""


class CircuitBreakerError(SafetyError):
    """A circuit breaker rejected an action."""


# --- exchange ------------------------------------------------------------


class ExchangeError(BtcBotError):
    """Base class for exchange interaction failures."""


class ApiError(ExchangeError):
    """The exchange returned a non-zero error code.

    ``ret_code`` carries OKX's numeric ``code`` (or per-order ``sCode``).
    """

    def __init__(self, ret_code: int, ret_msg: str, endpoint: str = "") -> None:
        self.ret_code = ret_code
        self.ret_msg = ret_msg
        self.endpoint = endpoint
        super().__init__(f"okx code={ret_code} msg={ret_msg!r} endpoint={endpoint}")


class OrderRejectedError(ApiError):
    """One order operation was rejected by its own per-item ``sCode``.

    OKX's trade endpoints are batch-shaped: the envelope ``code`` reports only
    whether the *batch* succeeded (``1`` = "All operations failed"), while the
    actual reason lives in each item's ``sCode``/``sMsg``/``subCode``. This
    error carries those fields separately so a caller can render them without
    re-parsing a message string.

    It subclasses :class:`ApiError` with ``ret_code`` set to the item's
    ``sCode``, so every existing ``except ApiError`` path keeps working and
    sees the real code rather than a bare ``1``.

    Only exchange *response* fields are carried here. Nothing from the signed
    request — key, signature, passphrase, headers — is ever attached.
    """

    def __init__(
        self,
        s_code: int,
        s_msg: str,
        endpoint: str = "",
        *,
        sub_code: str = "",
        client_order_id: str = "",
        order_id: str = "",
    ) -> None:
        self.s_code = s_code
        self.s_msg = s_msg
        self.sub_code = sub_code
        self.client_order_id = client_order_id
        self.order_id = order_id
        detail = ", ".join(
            f"{label}={value}"
            for label, value in (
                ("subCode", sub_code),
                ("clOrdId", client_order_id),
                ("ordId", order_id),
            )
            if value
        )
        super().__init__(s_code, f"{s_msg} ({detail})" if detail else s_msg, endpoint)

    def report_lines(self) -> list[str]:
        """The rejection as operator-facing lines, one field per line."""
        lines = [f"OKX sCode={self.s_code}", f"sMsg={self.s_msg}"]
        if self.sub_code:
            lines.append(f"subCode={self.sub_code}")
        if self.client_order_id:
            lines.append(f"clOrdId={self.client_order_id}")
        if self.endpoint:
            lines.append(f"endpoint={self.endpoint}")
        return lines


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


class BackfillError(MarketDataError):
    """Historical backfill could not obtain usable candles.

    Raised rather than swallowed: a silent backfill failure would leave
    strategies evaluating on a short or holed series, which looks like a
    working system producing bad signals. The caller keeps trading disabled
    and reports the exchange's actual reason.
    """


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
