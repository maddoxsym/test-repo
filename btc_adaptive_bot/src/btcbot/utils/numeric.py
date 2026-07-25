"""Exchange-grade numeric handling: rounding, step sizes, and safe arithmetic.

Order quantities and prices are handled with :class:`~decimal.Decimal` so that a
step size of ``0.000001`` does not acquire binary-float fuzz on the way to the
exchange. Analytics elsewhere in the system use floats, which is fine — but
anything that becomes an order field passes through here first.
"""

from __future__ import annotations

import math
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal, InvalidOperation

__all__ = [
    "to_decimal",
    "round_step_down",
    "round_step_up",
    "round_to_tick",
    "decimals_for_step",
    "format_qty",
    "format_price",
    "safe_div",
    "clamp",
    "is_finite_positive",
    "pct_change",
]


def to_decimal(value: float | int | str | Decimal) -> Decimal:
    """Convert to Decimal without inheriting binary float noise.

    Floats go via ``repr`` so ``0.1`` becomes ``Decimal("0.1")`` rather than the
    exact binary expansion.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"cannot convert non-finite float {value!r} to Decimal")
        return Decimal(repr(value))
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"cannot convert {value!r} to Decimal") from exc


def round_step_down(value: float | Decimal, step: float | Decimal) -> Decimal:
    """Round ``value`` **down** to a multiple of ``step``.

    Always down for order quantities: rounding up could exceed the risk budget or
    the available balance.
    """
    dec_value, dec_step = to_decimal(value), to_decimal(step)
    if dec_step <= 0:
        raise ValueError(f"step must be positive, got {step!r}")
    return (dec_value / dec_step).to_integral_value(rounding=ROUND_DOWN) * dec_step


def round_step_up(value: float | Decimal, step: float | Decimal) -> Decimal:
    """Round ``value`` **up** to a multiple of ``step`` (used to reach a minimum)."""
    dec_value, dec_step = to_decimal(value), to_decimal(step)
    if dec_step <= 0:
        raise ValueError(f"step must be positive, got {step!r}")
    return (dec_value / dec_step).to_integral_value(rounding=ROUND_UP) * dec_step


def round_to_tick(price: float | Decimal, tick: float | Decimal) -> Decimal:
    """Round a price to the nearest valid tick."""
    dec_price, dec_tick = to_decimal(price), to_decimal(tick)
    if dec_tick <= 0:
        raise ValueError(f"tick size must be positive, got {tick!r}")
    return (dec_price / dec_tick).to_integral_value(rounding=ROUND_HALF_UP) * dec_tick


def decimals_for_step(step: float | Decimal | str) -> int:
    """Number of decimal places implied by a step/tick size."""
    exponent = to_decimal(step).normalize().as_tuple().exponent
    if not isinstance(exponent, int):  # pragma: no cover - NaN/Infinity guard
        raise ValueError(f"invalid step {step!r}")
    return max(0, -exponent)


def format_qty(qty: float | Decimal, step: float | Decimal) -> str:
    """Render a quantity exactly as the exchange expects it in JSON.

    Fixed-point, correct number of decimals, never scientific notation — a
    quantity of ``1e-05`` in an order body is rejected by Bybit.
    """
    places = decimals_for_step(step)
    quantum = Decimal(1).scaleb(-places)
    return f"{to_decimal(qty).quantize(quantum, rounding=ROUND_DOWN):f}"


def format_price(price: float | Decimal, tick: float | Decimal) -> str:
    """Render a price exactly as the exchange expects it in JSON."""
    places = decimals_for_step(tick)
    quantum = Decimal(1).scaleb(-places)
    return f"{to_decimal(price).quantize(quantum, rounding=ROUND_HALF_UP):f}"


def safe_div(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Division that cannot raise and cannot return NaN/inf.

    Used throughout the metrics code, where a zero denominator (no losses, no
    trades, zero stop distance) is an ordinary condition rather than an error.
    """
    if denominator == 0 or not math.isfinite(denominator) or not math.isfinite(numerator):
        return default
    result = numerator / denominator
    return result if math.isfinite(result) else default


def clamp(value: float, low: float, high: float) -> float:
    """Constrain ``value`` to ``[low, high]``."""
    if low > high:
        raise ValueError(f"clamp bounds inverted: low={low} high={high}")
    return max(low, min(high, value))


def is_finite_positive(value: float | None) -> bool:
    """True only for a real, finite, strictly positive number."""
    return value is not None and math.isfinite(value) and value > 0


def pct_change(new: float, old: float) -> float:
    """Percentage change from ``old`` to ``new``, 0.0 when undefined."""
    return safe_div(new - old, abs(old), 0.0)
