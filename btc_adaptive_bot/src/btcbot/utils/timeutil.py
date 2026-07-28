"""UTC time helpers and timeframe arithmetic.

Everything in this system is UTC. Local time is never used for any decision,
bucketing, or persistence — only for the operator-facing "you started this at"
line in the console banner.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# Internal timeframe notation mapped to seconds. Exchange-independent: the
# OKX `bar` translation lives in exchange/endpoints.py.
# Minute intervals are numeric strings; D/W/M are letters.
INTERVAL_SECONDS: dict[str, int] = {
    "1": 60,
    "3": 180,
    "5": 300,
    "15": 900,
    "30": 1800,
    "60": 3600,
    "120": 7200,
    "240": 14400,
    "360": 21600,
    "720": 43200,
    "D": 86400,
    "W": 604800,
}

SUPPORTED_INTERVALS: tuple[str, ...] = tuple(INTERVAL_SECONDS)


class TimeframeError(ValueError):
    """Raised when an interval string is not a supported timeframe."""


def interval_seconds(interval: str) -> int:
    """Seconds in one candle of ``interval``."""
    try:
        return INTERVAL_SECONDS[interval]
    except KeyError:
        raise TimeframeError(
            f"unsupported timeframe {interval!r}; supported: {', '.join(SUPPORTED_INTERVALS)}"
        ) from None


def interval_ms(interval: str) -> int:
    return interval_seconds(interval) * 1000


def now_utc() -> datetime:
    """Current time, timezone-aware, UTC."""
    return datetime.now(UTC)


def now_ms() -> int:
    """Current UTC time as epoch milliseconds."""
    return int(now_utc().timestamp() * 1000)


def to_utc(dt: datetime) -> datetime:
    """Coerce a datetime to timezone-aware UTC.

    Naive datetimes are *assumed* UTC rather than local — persisted timestamps in
    this system are always UTC, so that assumption is the correct one here.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def ms_to_dt(ms: int | float | str) -> datetime:
    return datetime.fromtimestamp(int(ms) / 1000.0, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    return int(to_utc(dt).timestamp() * 1000)


def iso(dt: datetime) -> str:
    """RFC3339/ISO-8601 UTC string with a trailing Z."""
    return to_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    """Parse an ISO-8601 string, tolerating a trailing ``Z``."""
    return to_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def candle_open_ms(ts_ms: int, interval: str) -> int:
    """Epoch-ms of the open of the candle containing ``ts_ms``."""
    step = interval_ms(interval)
    return (ts_ms // step) * step


def candle_close_ms(open_ms: int, interval: str) -> int:
    """Epoch-ms at which a candle opened at ``open_ms`` closes (exclusive bound)."""
    return open_ms + interval_ms(interval)


def is_candle_closed(open_ms: int, interval: str, *, now: int | None = None) -> bool:
    """True when the candle opened at ``open_ms`` has finished.

    The single most important look-ahead guard in the system: a candle is only
    usable once wall-clock time has passed its closing boundary.
    """
    reference = now_ms() if now is None else now
    return reference >= candle_close_ms(open_ms, interval)


def humanize_duration(delta: timedelta | float) -> str:
    """Render a duration as ``Xd Yh Zm`` for console and reports."""
    total = int(delta.total_seconds() if isinstance(delta, timedelta) else delta)
    sign = "-" if total < 0 else ""
    total = abs(total)
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{sign}{days}d {hours}h {minutes}m"
    if hours:
        return f"{sign}{hours}h {minutes}m"
    return f"{sign}{minutes}m"


def day_index(start: datetime, current: datetime) -> int:
    """1-based calendar-day index of ``current`` within an experiment.

    Day 1 is the first 24 hours after the start instant. Crypto trades 24/7, so
    weekends are ordinary days and no calendar skipping happens anywhere.
    """
    elapsed = to_utc(current) - to_utc(start)
    return max(1, int(elapsed.total_seconds() // 86400) + 1)


def hour_bucket(dt: datetime) -> int:
    """UTC hour 0-23, used for time-of-day performance breakdowns."""
    return to_utc(dt).hour


def weekday_bucket(dt: datetime) -> int:
    """UTC weekday, Monday=0 … Sunday=6."""
    return to_utc(dt).weekday()
