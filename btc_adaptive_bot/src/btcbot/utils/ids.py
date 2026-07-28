"""Deterministic and unique identifier generation.

Two distinct jobs live here:

1. **Deduplication keys** — deterministic hashes that let the system recognise
   "this is the same setup I already acted on" across restarts. These prevent
   duplicate orders after a crash/reconnect.
2. **Client order IDs** — OKX's ``clOrdId`` is capped at **32 characters** of
   case-sensitive letters and digits only (no dashes or underscores) and must
   be unique. That is far too small for full attribution, so we pack a short
   routable prefix into the ID and keep the complete attribution record in
   SQLite keyed by that ID.
"""

from __future__ import annotations

import hashlib
import re
import uuid

# OKX: "A combination of case-sensitive alphanumerics", 1–32 characters.
ORDER_LINK_ID_MAX_LEN = 32
_ORDER_LINK_ID_ALLOWED = re.compile(r"^[A-Za-z0-9]{1,32}$")

_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


def _b36(value: int, width: int) -> str:
    """Base-36 encode ``value``, left-padded/truncated to ``width`` chars."""
    if value < 0:
        raise ValueError("cannot base36-encode a negative value")
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = _ALPHABET[rem] + out
    out = out or "0"
    return out[-width:].rjust(width, "0")


def _slug(value: str, width: int) -> str:
    """Stable short slug of an arbitrary string (lowercase base36 of a digest)."""
    digest = hashlib.blake2b(value.encode("utf-8"), digest_size=8).digest()
    return _b36(int.from_bytes(digest, "big"), width)


def new_uuid() -> str:
    return str(uuid.uuid4())


def signal_id(
    strategy_id: str,
    strategy_version: str,
    symbol: str,
    timeframe: str,
    bar_open_ms: int,
    direction: str,
) -> str:
    """Deterministic ID for a signal.

    Identical inputs always produce the same ID, so a strategy re-evaluating the
    same closed bar after a restart cannot create a second signal record. The
    database has a UNIQUE constraint on this column.
    """
    raw = f"{strategy_id}|{strategy_version}|{symbol}|{timeframe}|{bar_open_ms}|{direction}"
    return "sig_" + hashlib.blake2b(raw.encode("utf-8"), digest_size=16).hexdigest()


def setup_id(
    strategy_id: str,
    symbol: str,
    timeframe: str,
    bar_open_ms: int,
    setup_key: str,
) -> str:
    """Deterministic ID for a trade *setup*.

    ``setup_key`` is supplied by the strategy and describes the structural
    condition (e.g. ``"sweep_low_43120"``). Two signals describing the same
    structural setup collapse to one setup ID, which is what the duplicate-order
    guard keys on.
    """
    raw = f"{strategy_id}|{symbol}|{timeframe}|{bar_open_ms}|{setup_key}"
    return "set_" + hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


def experiment_id(name: str, start_ms: int, config_hash: str) -> str:
    """Unique ID for a new experiment.

    Deliberately **not** deterministic: two experiments with the same name and
    configuration started in the same millisecond (a restart immediately after
    one completes) would otherwise collide on the primary key. Resuming does not
    depend on recomputing this — the active experiment is found by query.
    """
    raw = f"{name}|{start_ms}|{config_hash}|{uuid.uuid4().hex}"
    return "exp_" + hashlib.blake2b(raw.encode("utf-8"), digest_size=12).hexdigest()


def client_order_id(
    experiment_id_value: str,
    strategy_id: str,
    strategy_version: str,
    setup_id_value: str,
    signal_ts_ms: int,
) -> str:
    """Build an OKX-legal ``clOrdId`` (≤32 chars, alphanumerics only) carrying
    attribution.

    Layout (exactly 32 chars, no separators — OKX forbids dashes/underscores):

    ``b`` + exp(4) + strat(6) + ver(2) + setup(6) + ts(6) + rand(7)

    Every component is base36 so the result matches OKX's allowed character
    set. The ID is *routable* — the fixed field offsets let an operator match
    an order in the OKX UI back to its strategy — while the exhaustive
    attribution (full strategy ID, version, setup ID, experiment ID, signal
    timestamp) lives in the ``demo_orders`` table keyed on this value.

    Field widths are chosen so uniqueness is not left to chance: epoch seconds
    fit in 6 base-36 chars (36^6 ≈ 2.2e9, good past the year 2038), which leaves
    7 chars — about 7.8e10 values — for the random block. OKX requires
    ``clOrdId`` to be unique, and a birthday collision across a 14-day run
    must be negligible, so the random field is deliberately generous rather than
    merely "probably fine". The duplicate *guard*, not the ID, is what prevents
    unintended repeats; this only prevents accidental ID reuse.
    """
    parts = (
        "b",
        _slug(experiment_id_value, 4),
        _slug(strategy_id, 6),
        _slug(strategy_version, 2),
        _slug(setup_id_value, 6),
        _b36(max(0, signal_ts_ms) // 1000, 6),
        _b36(uuid.uuid4().int % (36**7), 7),
    )
    candidate = "".join(parts)
    if len(candidate) > ORDER_LINK_ID_MAX_LEN:  # pragma: no cover - guarded by construction
        candidate = candidate[:ORDER_LINK_ID_MAX_LEN]
    if not _ORDER_LINK_ID_ALLOWED.match(candidate):  # pragma: no cover - defensive
        raise ValueError(f"generated clOrdId is not OKX-legal: {candidate!r}")
    return candidate


def is_valid_client_order_id(value: str) -> bool:
    """Whether ``value`` satisfies OKX's documented ``clOrdId`` rules."""
    return bool(_ORDER_LINK_ID_ALLOWED.match(value))


def config_hash(payload: str) -> str:
    """Short stable hash of the resolved configuration, recorded per experiment."""
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def redact_secret(secret: str | None, *, keep: int = 4) -> str:
    """Render a credential safely for logs.

    Only the first ``keep`` characters survive; the rest becomes a fixed mask, so
    log volume cannot leak the secret's length either.
    """
    if not secret:
        return "<unset>"
    if len(secret) <= keep:
        return "*" * 8
    return f"{secret[:keep]}{'*' * 8}"
