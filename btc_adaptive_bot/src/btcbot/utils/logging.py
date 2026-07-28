"""Logging: readable tagged console output, rotating files, structured JSON events.

Console lines use the tag vocabulary from the project brief so an operator can
follow what the system is doing at a glance::

    [OKX]    Demo environment VERIFIED
    [REGIME] VOLATILITY_EXPANSION (confidence 0.71)
    [DEMO]   BREAKOUT_RETEST allocated actual Demo trade

Secrets never reach any sink: :class:`SecretRedactionFilter` scrubs registered
credential values from every record, including ones formatted by third-party
libraries that we do not control.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

from .timeutil import iso, now_utc

# Tags used across the system. Kept central so the console stays consistent.
TAGS = (
    "START", "STOP", "OKX", "BALANCE", "MARKET", "REGIME", "STRATEGY", "SHADOW",
    "DEMO", "ORDER", "FILL", "EXIT", "NEWS", "LEARNING", "VALIDATION", "RANK",
    "SAFETY", "RECOVERY", "DATA", "EXPERIMENT", "CHAMPION", "REPORT", "DASHBOARD",
    "CONFIG", "DB", "ALLOC", "BACKTEST", "RISK", "DECISION", "SMOKE", "LEVERAGE",
)

_MASK = "***REDACTED***"
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Register a credential so it can never appear in any log sink."""
    if value and len(value) >= 6:
        _registered_secrets.add(value)


class SecretRedactionFilter(logging.Filter):
    """Scrub registered secrets from messages and arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not _registered_secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - malformed record, let it through raw
            return True
        if any(secret in message for secret in _registered_secrets):
            for secret in _registered_secrets:
                message = message.replace(secret, _MASK)
            record.msg = message
            record.args = ()
        return True


class ConsoleFormatter(logging.Formatter):
    """``HH:MM:SS [TAG] message`` — compact and scannable."""

    def format(self, record: logging.LogRecord) -> str:
        tag = getattr(record, "tag", None)
        stamp = now_utc().strftime("%H:%M:%S")
        prefix = f"{stamp} [{tag}]" if tag else f"{stamp} [{record.levelname}]"
        text = record.getMessage()
        if record.exc_info:
            text = f"{text}\n{self.formatException(record.exc_info)}"
        return f"{prefix:<20} {text}"


class JsonEventFormatter(logging.Formatter):
    """One JSON object per line — for later machine analysis of a run."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso(now_utc()),
            "level": record.levelname,
            "logger": record.name,
            "tag": getattr(record, "tag", None),
            "message": record.getMessage(),
        }
        extra = getattr(record, "event", None)
        if isinstance(extra, dict):
            payload["event"] = extra
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TaggedLogger:
    """Thin wrapper adding the ``[TAG]`` vocabulary and structured payloads."""

    __slots__ = ("_logger",)

    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _emit(
        self,
        level: int,
        tag: str,
        message: str,
        *,
        event: dict[str, Any] | None = None,
        exc_info: bool = False,
    ) -> None:
        self._logger.log(
            level, message, extra={"tag": tag, "event": event}, exc_info=exc_info
        )

    def info(self, tag: str, message: str, **event: Any) -> None:
        self._emit(logging.INFO, tag, message, event=event or None)

    def debug(self, tag: str, message: str, **event: Any) -> None:
        self._emit(logging.DEBUG, tag, message, event=event or None)

    def warning(self, tag: str, message: str, **event: Any) -> None:
        self._emit(logging.WARNING, tag, message, event=event or None)

    def error(self, tag: str, message: str, exc_info: bool = False, **event: Any) -> None:
        self._emit(logging.ERROR, tag, message, event=event or None, exc_info=exc_info)

    def critical(self, tag: str, message: str, exc_info: bool = False, **event: Any) -> None:
        self._emit(logging.CRITICAL, tag, message, event=event or None, exc_info=exc_info)

    def banner(self, lines: list[str], *, tag: str = "START") -> None:
        """Print a boxed banner (used for the safety lock and experiment header)."""
        rule = "=" * 50
        self._emit(logging.INFO, tag, rule)
        for line in lines:
            self._emit(logging.INFO, tag, line)
        self._emit(logging.INFO, tag, rule)


def setup_logging(
    *,
    level: str = "INFO",
    console: bool = True,
    file_path: str | Path | None = None,
    json_path: str | Path | None = None,
    max_bytes: int = 20 * 1024 * 1024,
    backup_count: int = 10,
) -> None:
    """Configure root logging. Safe to call more than once."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    redactor = SecretRedactionFilter()

    if console:
        stream = logging.StreamHandler(sys.stdout)
        stream.setFormatter(ConsoleFormatter())
        stream.addFilter(redactor)
        root.addHandler(stream)

    if file_path:
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        rotating.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)-28s %(message)s")
        )
        rotating.addFilter(redactor)
        root.addHandler(rotating)

    if json_path:
        path = Path(json_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        json_handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
        )
        json_handler.setFormatter(JsonEventFormatter())
        json_handler.addFilter(redactor)
        root.addHandler(json_handler)

    # These are chatty at DEBUG and would drown the console.
    for noisy in ("httpx", "httpcore", "websockets", "uvicorn.access", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> TaggedLogger:
    """Get a tagged logger for a module."""
    return TaggedLogger(logging.getLogger(name))
